"""L2 · Agent 适配层 —— 上下文水位真源（单一权威口径）。

职责（只此一件）：把「某个 Agent 在某个会话上用了多少上下文、分母是多少」这件事，
按不同 Agent 后端的特征算出唯一答案。**不含任何投递/会话/轮转逻辑**（那是 L1），
也**不写任何文件、不碰任何状态**（只读）。

契约
----
    read_usage(caller, session_id, agent_cfg=None, model="") -> (used, window) | None
        后端分派：hermes / openclaw / other。读不到一律返回 None，
        调用方必须降级为「不自愈」，绝不编造数字。
    read_watermark(caller, session_id, agent_cfg=None) -> dict | None
        本机直读 → 远端 /ctx-usage 回退 → 渲染成 {"used","window","pct","text"}。
    pressure(caller, session_id, agent_cfg=None) -> float | None
        水位 = used / window；低于下限返回 0.0；读不到返回 None。
    window_for(model, backend="") -> int
        分母。**唯一口径链**：Hermes 生效配置 → Hermes 模型目录 → 本模块兜底表。
    backend_kind(agent_cfg) -> "hermes" | "openclaw" | "other"
    hermes_session_headers(agent_cfg, caller, session_id) -> dict
        Hermes OpenAI 兼容路由认的会话头（适配细节，不该长在通行层里）。

为什么这一层单独存在
--------------------
2026-09-25 实测：同一套读法原先在 `a2a_svc.py`（L1 通行层）与 `ctx_watermark.py`
（渲染器）里各长一份，且**兜底窗口表互相矛盾**（1,000,000 vs 200,000）。分母差 5 倍，
水位就差 5 倍 —— 自愈行为取决于你在哪台机器、走哪条路径，这是"口径分裂"而非"重复代码"。
现在只有这里一份；两份调用方都改成薄委托，调用面不变。

零硬编码
--------
路径/凭据一律**调用时**从环境变量解析（不在 import 期定死），便于后续节点注入。
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional, Tuple

LOG = logging.getLogger("agent_usage")


# ---------------------------------------------------------------- 路径与凭据

def _first_env(*names: str) -> str:
    """按序取第一个非空环境变量。"""
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return ""


def sessions_json_path() -> str:
    """OpenClaw 会话档案路径（未配置 → 该后端降级为不自愈）。"""
    return _first_env("A2A_SESSIONS_JSON", "CTX_SESSIONS_JSON")


def hermes_state_db() -> str:
    """Hermes state.db 路径（未配置 → Hermes 后端降级为不自愈）。"""
    return _first_env("A2A_HERMES_STATE_DB", "CTX_HERMES_STATE_DB")


def a2a_remote_base() -> str:
    """本节点 a2a 服务基址（远端水位回退用）。"""
    return _first_env("CTX_A2A_BASE", "A2A_BASE")


def a2a_remote_token() -> str:
    """a2a 服务令牌：先 env，再令牌文件。

    遗留：最后一条候选是 Linux 部署的 <deploy-dir>/client-token，待节点注入 env 后删除。
    """
    tok = _first_env("CTX_A2A_TOKEN", "A2A_TOKEN")
    if tok:
        return tok
    candidates = [_first_env("CTX_A2A_TOKEN_FILE"), _first_env("A2A_TOKEN_FILE"),
                  _first_env("AGENT_TEAM_TOKEN_FILE"),
                  os.path.join(_first_env("AGENT_TEAM_ETC_DIR") or "<deploy-dir>",
                               "client-token")]
    for path in candidates:
        if path and os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    value = fh.read().strip()
                if value:
                    return value
            except Exception:  # noqa: BLE001 —— 令牌文件读不到就当没配
                continue
    return ""


# ---------------------------------------------------------------- 窗口分母

# 兜底表（取不到生效值时用）。键为 model 子串（小写，最长匹配优先）。
# ⚠️ 这一张表是**统一后的唯一口径**（2026-09-25）：分母必须是 Agent「实际生效」的窗口，
#    不是模型目录里的理论上限。
#    实测依据：Hermes 模型目录（agent/model_metadata.py）对 deepseek-v4-flash 写 1,000,000，
#    而本机 Hermes 生效配置把窗口覆盖成 200,000，Hermes 自己的压缩阈值就按 200,000 算
#    （群里「📚 上下文：约 Xk/200k」那行也是这个分母）。
#    自愈若按 1,000,000 算，水位被低估 5 倍 —— 等于把自愈静默关掉。
CTX_WINDOW_FALLBACK = {
    "deepseek-v4-flash": 200000,
    "deepseek-v4-pro": 200000,
    "deepseek": 128000,
}


def hermes_home() -> str:
    """Hermes 主目录：先 env，再按平台惯例探测（读不到返回空串）。"""
    explicit = _first_env("A2A_HERMES_HOME", "HERMES_HOME")
    if explicit:
        return explicit
    home = os.path.expanduser("~")
    for candidate in (os.path.join(home, "AppData", "Local", "hermes"),
                      os.path.join(home, ".hermes")):
        if os.path.isdir(candidate):
            return candidate
    return ""


def effective_window_from_config() -> Tuple[str, int]:
    """Hermes 部署生效窗口：config.yaml 的 `model:` 段里声明的 (模型名, context_length)。

    `model.context_length` **只对 `model.default` 那一个模型生效**，不是全局分母，
    所以这里连模型名一起返回，由 window_for 判命中后才采用。读不到返回 ("", 0)，不猜。
    """
    home = hermes_home()
    if not home:
        return "", 0
    path = os.path.join(home, "config.yaml")
    if not os.path.isfile(path):
        return "", 0
    name, ctx_len, in_model = "", 0, False
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.rstrip("\n")
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if not line[:1].isspace():            # 顶层键：判断是否仍在 model: 段内
                    in_model = (stripped.split(":", 1)[0] == "model")
                    continue
                if not in_model:
                    continue
                key, _, val = stripped.partition(":")
                val = val.strip().strip("\"'")
                if key == "default":
                    name = val
                elif key == "context_length":
                    try:
                        ctx_len = int(val)
                    except (TypeError, ValueError):
                        ctx_len = 0
    except Exception as exc:  # noqa: BLE001
        LOG.debug("agent_usage: 读 Hermes config 失败(%s)，跳过生效窗口覆盖", exc)
        return "", 0
    return name, ctx_len


def window_for(model: str, backend: str = "") -> int:
    """窗口分母，一条链三个来源（先权威、后兜底）：

    ① Hermes **部署生效**窗口 —— config.yaml 的 `model.context_length`，仅当模型名命中；
    ② Hermes 自带模型目录 `DEFAULT_CONTEXT_LENGTHS`（能 import agent 时）；
    ③ 本模块兜底表（保守值，偏小 = 自愈偏早，属安全方向）。

    全都不命中返回 0，表示「不知道」——调用方必须降级为不自愈，绝不编造分母。
    口径说明：①优先于②是有意为之。目录里的窗口是模型**理论上限**，而 Hermes 自身
    的压缩阈值按生效窗口算；自愈若按理论上限估水位，会系统性低估压力（本机实测低估
    5 倍），自愈等于静默关闭。
    """
    m = str(model or "").lower()
    if not m:
        return 0
    if backend in ("", "hermes"):
        cfg_model, cfg_window = effective_window_from_config()
        cm = str(cfg_model or "").lower()
        if cfg_window > 0 and cm and (cm == m or cm in m):
            return int(cfg_window)
    try:
        import sys as _sys
        hm = _first_env("A2A_HERMES_AGENT_DIR")
        if hm and hm not in _sys.path:
            _sys.path.insert(0, hm)
        from agent import model_metadata as _mm  # type: ignore
        hit = _mm._longest_key_match(_mm.DEFAULT_CONTEXT_LENGTHS, m)
        if hit and hit[1] > 0:
            return int(hit[1])
    except Exception:  # noqa: BLE001 —— 拿不到就继续往下兜底
        pass
    best_key, best_val = "", 0
    for k, v in CTX_WINDOW_FALLBACK.items():
        if k in m and len(k) > len(best_key):
            best_key, best_val = k, v
    return int(best_val)


# ---- Hermes 估算公式参数（对齐 Hermes 0.21.3 agent/model_metadata.py）----
# ⚠️ 近似值（±15%）；阈值有缓冲。Hermes 升级若改公式，这里必须同步。
_CTX_CHARS_PER_TOKEN = 4   # ASCII：ceil(UTF-8 字节 / 4)
_CJK_DENSE_RE = re.compile("[\u1100-\u11ff\u2e80-\u9fff\ua960-\ua97f\uac00-\ud7af\uf900-\ufaff\uff00-\uffef]")


def estimate_tokens_rough(text: str) -> int:
    """对齐 Hermes estimate_tokens_rough：CJK 密集字符 ≈1 token，其余 ceil(UTF-8字节/4)。

    Hermes 版本 0.21.3；公式为近似值（±15%），调用方阈值有缓冲。
    """
    if not text:
        return 0
    text = str(text)
    if text.isascii():
        return (len(text) + 3) // _CTX_CHARS_PER_TOKEN
    stripped = _CJK_DENSE_RE.sub("", text)
    dense = len(text) - len(stripped)
    return dense + ((len(stripped.encode("utf-8", "replace")) + 3) // _CTX_CHARS_PER_TOKEN)


# ---------------------------------------------------------------- 后端判定与会话头

def backend_kind(agent_cfg: Optional[Dict[str, Any]]) -> str:
    """按后端特征判定 openclaw / hermes / other（分派水位读法用）。

    判定顺序：端口/URL 关键词 → 模型名关键词 → 会话档案兜底。
    **不能只认端口**：各机 OpenClaw 网关端口并不统一（本机 18789、龙虾 19805…），
    只认端口会把它们误判成 other，连带把上下文自愈整个关掉（2026-09-23 实测）。
    """
    a = agent_cfg or {}
    base = str(a.get("backend") or "").lower()
    model = str(a.get("model") or "").lower()
    if "8642" in base or "hermes" in base:
        return "hermes"
    if "18789" in base or "openclaw" in base or "openclaw" in model:
        return "openclaw"
    sessions = sessions_json_path()
    if sessions and os.path.isfile(sessions):
        return "openclaw"
    return "other"


def hermes_session_headers(agent_cfg: Optional[Dict[str, Any]], caller: str,
                           session_id: Optional[str]) -> Dict[str, str]:
    """Hermes OpenAI 兼容路由认的会话头（OpenClaw 认 OpenAI 的 `user` 字段，Hermes 不认）。

      · X-Hermes-Session-Key —— 会话/记忆域键（跨轮复用同一会话）
      · X-Hermes-Session-Id  —— 显式接续/新建某个 transcript
    两者都要求 API key 认证（已由 Authorization: Bearer 提供）。
    session_id 为空时不注入任何头（一次性会话，与原行为一致）。
    """
    if not session_id:
        return {}
    key = "a2a:%s:%s" % (caller or "unknown", session_id)
    return {"X-Hermes-Session-Key": key, "X-Hermes-Session-Id": key}


# ---------------------------------------------------------------- 读数：Hermes

def _read_hermes_ctx_b(caller: str, session_id: str,
                       model: str = "") -> Optional[Tuple[int, int]]:
    """口径 B：只读 state.db，估算该会话「当前上下文占用」→ (占用tokens, 窗口)。

    累加 active=1 消息的 content/tool_calls/reasoning_content 估算值。
    Hermes 的 messages.token_count 字段全库不填充（NULL），只能按其自身公式重算。
    取 active=1（当前生效上下文，随压缩轮转会变，**切勿缓存**）。
    读不到返回 None。
    """
    db = hermes_state_db()
    if not session_id or not db or not os.path.isfile(db):
        return None
    try:
        uri = "file:%s?mode=ro" % db.replace("\\", "/")
        conn = sqlite3.connect(uri, uri=True, timeout=3.0)
        try:
            sid_row = conn.execute(
                "SELECT id, model FROM sessions WHERE session_key = ? "
                "ORDER BY COALESCE(last_activity_at, started_at) DESC LIMIT 1",
                ("a2a:%s:%s" % (caller or "unknown", session_id),)).fetchone()
            if sid_row is None:
                return None
            sid, row_model = sid_row[0], (sid_row[1] or "")
            rows = conn.execute(
                "SELECT content, tool_calls, reasoning_content FROM messages "
                "WHERE session_id = ? AND active = 1", (sid,)).fetchall()
            if not rows:
                return None
            used = 0
            for content, tool_calls, reasoning in rows:
                used += estimate_tokens_rough(content or "")
                used += estimate_tokens_rough(tool_calls or "")
                used += estimate_tokens_rough(reasoning or "")
            win = window_for(model or row_model, "hermes")
            if win <= 0 or used <= 0:
                return None
            return int(used), int(win)
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        LOG.debug("口径B 读 Hermes messages 失败(%s)，降级", exc)
        return None


def _read_hermes_usage_a(caller: str, session_id: str,
                         model: str = "") -> Optional[Tuple[int, int]]:
    """口径 A：sessions 表累计 token / 调用次数 = 单轮典型占用 → (占用, 窗口)。读不到 None。"""
    db = hermes_state_db()
    if not session_id or not db or not os.path.isfile(db):
        return None
    try:
        # 只读 URI 打开，杜绝意外写入（sqlite 只读模式下写操作会直接报错）。
        uri = "file:%s?mode=ro" % db.replace("\\", "/")
        conn = sqlite3.connect(uri, uri=True, timeout=3.0)
        try:
            want_key = "a2a:%s:%s" % (caller or "unknown", session_id)
            row = conn.execute(
                "SELECT input_tokens, cache_read_tokens, api_call_count, model "
                "FROM sessions WHERE session_key = ? "
                "ORDER BY COALESCE(last_activity_at, started_at) DESC LIMIT 1",
                (want_key,)).fetchone()
            if row is None:
                return None
            inp, cache_read, calls, row_model = (int(row[0] or 0), int(row[1] or 0),
                                                 int(row[2] or 0), (row[3] or ""))
            if calls <= 0:
                return None
            typical = (inp + cache_read) / float(calls)
            win = window_for(model or row_model, "hermes")
            if win <= 0:
                return None
            return int(typical), int(win)
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        LOG.debug("口径A 读 Hermes state.db 失败(%s)，降级为不自愈", exc)
        return None


def read_hermes(caller: str, session_id: str,
                model: str = "") -> Optional[Tuple[int, int]]:
    """Hermes 水位总入口：口径 B 优先，拿不到回退口径 A。"""
    b = _read_hermes_ctx_b(caller, session_id, model)
    if b is not None:
        return b
    return _read_hermes_usage_a(caller, session_id, model)


# ---------------------------------------------------------------- 读数：OpenClaw

def read_openclaw(caller: str, session_id: str) -> Optional[Tuple[int, int]]:
    """读 OpenClaw 会话档案，key = agent:main:openai-user:a2a:<caller>:<session_id>。

    映射规则（07-23 实测）：A2A 的 `user` = `a2a:<caller>:<session_id>` 落到该 key。
    """
    path = sessions_json_path()
    if not session_id or not path or not os.path.isfile(path):
        return None
    try:
        key = "agent:main:openai-user:a2a:%s:%s" % (caller or "unknown", session_id)
        with open(path, "r", encoding="utf-8") as fh:
            store = json.load(fh)
        entry = store.get(key)
        if not isinstance(entry, dict):
            return None
        total = entry.get("totalTokens")
        window = entry.get("contextTokens")
        if total is None or window is None:
            return None
        total_i, window_i = int(total), int(window)
        if window_i <= 0:
            return None
        return total_i, window_i
    except Exception as exc:  # noqa: BLE001 —— 自愈是增强能力，绝不允许因它拖垮主流程
        LOG.debug("读 sessions.json 失败(%s)，降级为不自愈", exc)
        return None


# ---------------------------------------------------------------- 读数：总入口

def read_usage(caller: str, session_id: str,
               agent_cfg: Optional[Dict[str, Any]] = None,
               model: str = "") -> Optional[Tuple[int, int]]:
    """按后端分派读水位，返回 (占用, 窗口) 或 None。

    agent_cfg 为 None 时按 openclaw 试（与原 `_read_ctx_usage` 默认一致）。
    返回 None 表示读不到（未配置/文件不存在/无权限/key 不在/字段缺失）——
    调用方应降级为「不压缩、照常续跑」，不阻塞主流程。本模块绝不写文件。
    """
    kind = backend_kind(agent_cfg) if agent_cfg is not None else "openclaw"
    if kind == "hermes":
        m = model or str((agent_cfg or {}).get("model") or "")
        return read_hermes(caller, session_id, m)
    if kind == "other":
        return None   # 未知后端：无水位数据源，降级不自愈
    return read_openclaw(caller, session_id)


def pressure(caller: str, session_id: str,
             agent_cfg: Optional[Dict[str, Any]] = None,
             min_tokens_floor: int = 8000) -> Optional[float]:
    """该 session 的上下文水位（0.0~1.0+）；读不到返回 None；低于下限返回 0.0。"""
    usage = read_usage(caller, session_id, agent_cfg)
    if usage is None:
        return None
    total, window = usage
    if total < min_tokens_floor:
        return 0.0
    return float(total) / float(window)


# ---------------------------------------------------------------- 渲染与远端回退

def fmt(used: int, window: int) -> Dict[str, Any]:
    """渲染成群里「📚 上下文」那行用的结构。"""
    pct = int(round(100.0 * used / window))
    return {
        "used": used,
        "window": window,
        "pct": pct,
        "text": "上下文：约 %dk/%dk (%d%%)" % (used // 1000, window // 1000, pct),
    }


def read_remote(caller: str, session_id: str) -> Optional[Dict[str, Any]]:
    """调本节点 a2a 服务的 /ctx-usage（同机同源）拿真源水位。读不到返回 None。"""
    base = a2a_remote_base()
    token = a2a_remote_token()
    if not base or not token:
        return None
    try:
        url = "%s/ctx-usage/%s?session_id=%s" % (
            base.rstrip("/"), urllib.parse.quote(caller, safe=""),
            urllib.parse.quote(session_id, safe=""))
        req = urllib.request.Request(url)
        req.add_header("Authorization", "Bearer " + token)
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        if not isinstance(data, dict) or not data.get("ok"):
            return None
        used, window = int(data.get("used") or 0), int(data.get("window") or 0)
        if window <= 0 or used < 0:
            return None
        return fmt(used, window)
    except Exception as exc:  # noqa: BLE001
        LOG.debug("远端 ctx-usage 失败(%s)", exc)
        return None


def read_watermark(caller: str, session_id: str,
                   agent_cfg: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """读真源水位 → dict；读不到返回 None（不渲染，绝不编造数字）。

    两级：① 本机直读（本机就是该 agent 所在节点）；② 远端 a2a `/ctx-usage/<agent>`
    （本机读不到时用，例子：chat_svc 在云端、会话在笔记本）。
    """
    if not caller or not session_id:
        return None
    usage = read_usage(caller, session_id, agent_cfg)
    if usage:
        used, window = int(usage[0]), int(usage[1])
        if window > 0:
            return fmt(used, window)
    return read_remote(caller, session_id)
