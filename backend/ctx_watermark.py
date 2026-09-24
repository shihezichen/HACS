#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ctx_watermark.py —— 服务端「上下文水位」渲染器（2026-09-24）。

为什么要有它（问题）：
    群里每条 Agent 回复末尾都带一行「📚 上下文：约 118k/120k (98%)」——
    这行是 **Agent 自己手打** 的：分子是它凭印象估的、分母（120k）是它记错的。
    本机 deepseek-v4-flash 真实窗口是 **200k**（sessions.json 的 contextTokens），
    于是真实水位 57% 被报成 98%，刚哥以为「到 98% 还不压缩」。
    另外这行是可变内容，混进 prompt 前缀会伤 DeepSeek 的 prompt-cache 命中。

怎么做（口径）：
    这行**不再让 Agent 输出**，改由服务端按 **真源** 渲染：
      · openclaw 后端：读本机 OpenClaw 的 sessions.json，
        key = `agent:main:openai-user:a2a:<caller>:<session_id>`，
        取 totalTokens / contextTokens（精确「当前占用 / 窗口」）。
      · hermes 后端：读 Hermes state.db（口径 B 优先：messages active=1 估算；
        回退口径 A：sessions 表累计 token / 调用次数）。
    与 a2a_svc.py 的水位读取**同一套口径**（那边管自愈决策，这边管显示），
    保证「显示的值」和「触发压缩的值」永远一致。

用法：
    wm = ctx_watermark.read_watermark(caller="openclaw-nb", session_id="meet:meet_xxx",
                                      agent_cfg={"backend": "...", "model": "..."})
    # -> {"used": 114082, "window": 200000, "pct": 57, "text": "上下文：约 114k/200k (57%)"}  或 None
    None 表示读不到（不渲染，页面/消息就不挂这行），绝不编造。
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

LOG = logging.getLogger("ctx_watermark")

# ---- 路径：优先环境变量，与本仓其它服务同一套「零硬编码」口径 ----
SESSIONS_JSON_PATH = (os.environ.get("A2A_SESSIONS_JSON")
                      or os.environ.get("CTX_SESSIONS_JSON") or "")
HERMES_STATE_DB = (os.environ.get("A2A_HERMES_STATE_DB")
                   or os.environ.get("CTX_HERMES_STATE_DB") or "")

# ---- 远端 a2a 服务（本机不是 agent 所在节点时用）：/ctx-usage/<agent> ----
# 例：chat_svc 在龙虾，而 openclaw-nb 的会话在另一台节点（<tailnet-ip>:9920）。
A2A_BASE = os.environ.get("CTX_A2A_BASE") or os.environ.get("A2A_BASE") or ""
A2A_TOKEN = (os.environ.get("CTX_A2A_TOKEN") or os.environ.get("A2A_TOKEN") or "")
A2A_TOKEN_FILE = (os.environ.get("CTX_A2A_TOKEN_FILE")
                  or os.environ.get("A2A_TOKEN_FILE") or "")
if not A2A_TOKEN and A2A_TOKEN_FILE and os.path.isfile(A2A_TOKEN_FILE):
    try:
        with open(A2A_TOKEN_FILE, "r", encoding="utf-8") as _fh:
            A2A_TOKEN = _fh.read().strip()
    except Exception:  # noqa: BLE001
        A2A_TOKEN = ""

# ---- 共享本地 token（部署目录下若有 client-token 可直接用）----
for _cand in ((os.environ.get("CTX_TOKEN_FILE") or "client-token"),):
    if not A2A_TOKEN and os.path.isfile(_cand):
        try:
            with open(_cand, "r", encoding="utf-8") as _fh:
                A2A_TOKEN = _fh.read().strip()
        except Exception:  # noqa: BLE001
            pass

# ---- 窗口分母兜底表（取不到 live 值时用）；键为 model 子串（小写）----
CTX_WINDOW_FALLBACK = {
    "deepseek-v4-flash": 200000,
    "deepseek-v4-pro": 200000,
    "deepseek": 128000,
}

# ---- Hermes 估算公式参数（对齐 hermes agent/model_metadata.py）----
_CTX_CHARS_PER_TOKEN = 4   # ASCII：ceil(UTF-8字节 / 4)


def _estimate_tokens_rough(text: str) -> int:
    """对齐 Hermes estimate_tokens_rough：CJK 密集字符 ≈1 token，其余 ceil(UTF-8字节/4)。"""
    if not text:
        return 0
    text = str(text)
    if text.isascii():
        return (len(text) + 3) // _CTX_CHARS_PER_TOKEN
    dense = 0
    for ch in text:
        code = ord(ch)
        if (0x1100 <= code <= 0x11FF or 0x2E80 <= code <= 0x9FFF
                or 0xA960 <= code <= 0xA97F or 0xAC00 <= code <= 0xD7AF
                or 0xF900 <= code <= 0xFAFF or 0xFF00 <= code <= 0xFFEF):
            dense += 1
    stripped_bytes = len("".join(
        ch for ch in text
        if not (0x1100 <= ord(ch) <= 0x11FF or 0x2E80 <= ord(ch) <= 0x9FFF
                or 0xA960 <= ord(ch) <= 0xA97F or 0xAC00 <= ord(ch) <= 0xD7AF
                or 0xF900 <= ord(ch) <= 0xFAFF or 0xFF00 <= ord(ch) <= 0xFFEF)
    ).encode("utf-8", "replace"))
    return dense + ((stripped_bytes + 3) // _CTX_CHARS_PER_TOKEN)


def _window_for(model: str, backend: str) -> int:
    """窗口上限：先试 Hermes 内部表，再兜底。"""
    m = str(model or "").lower()
    # Hermes 侧 import 得到就用它的表（口径与 Hermes 自身一致）
    try:
        import sys as _sys
        hm = os.environ.get("A2A_HERMES_AGENT_DIR", "")
        if hm and hm not in _sys.path:
            _sys.path.insert(0, hm)
        from agent import model_metadata as _mm  # type: ignore
        hit = _mm._longest_key_match(_mm.DEFAULT_CONTEXT_LENGTHS, m)
        if hit and hit[1] > 0:
            return int(hit[1])
    except Exception:  # noqa: BLE001
        pass
    best_key, best_val = "", 0
    for k, v in CTX_WINDOW_FALLBACK.items():
        if k in m and len(k) > len(best_key):
            best_key, best_val = k, v
    return int(best_val)


def _backend_kind(agent_cfg: Optional[Dict[str, Any]]) -> str:
    """按后端特征判定 openclaw / hermes / other（与 a2a_svc 同口径，不认端口只看特征）。"""
    a = agent_cfg or {}
    base = str(a.get("backend") or "").lower()
    model = str(a.get("model") or "").lower()
    if "8642" in base or "hermes" in base:
        return "hermes"
    if "18789" in base or "openclaw" in base or "openclaw" in model:
        return "openclaw"
    if SESSIONS_JSON_PATH and os.path.isfile(SESSIONS_JSON_PATH):
        return "openclaw"
    return "other"


def _read_openclaw(caller: str, session_id: str) -> Optional[tuple]:
    """读 sessions.json，key = agent:main:openai-user:a2a:<caller>:<session_id>。"""
    if not session_id or not SESSIONS_JSON_PATH or not os.path.isfile(SESSIONS_JSON_PATH):
        return None
    try:
        key = "agent:main:openai-user:a2a:%s:%s" % (caller or "unknown", session_id)
        with open(SESSIONS_JSON_PATH, "r", encoding="utf-8") as fh:
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
    except Exception as exc:  # noqa: BLE001
        LOG.debug("ctx_watermark 读 sessions.json 失败(%s)", exc)
        return None


def _read_hermes(caller: str, session_id: str, model: str) -> Optional[tuple]:
    """口径 B 优先（messages active=1 估算），回退口径 A（sessions 累计 / 调用次数）。"""
    if not session_id or not HERMES_STATE_DB or not os.path.isfile(HERMES_STATE_DB):
        return None
    want_key = "a2a:%s:%s" % (caller or "unknown", session_id)
    try:
        uri = "file:%s?mode=ro" % HERMES_STATE_DB.replace("\\", "/")
        conn = sqlite3.connect(uri, uri=True, timeout=3.0)
        try:
            sid_row = conn.execute(
                "SELECT id, model FROM sessions WHERE session_key = ? "
                "ORDER BY COALESCE(last_activity_at, started_at) DESC LIMIT 1",
                (want_key,)).fetchone()
            if sid_row is None:
                return None
            sid, row_model = sid_row[0], (sid_row[1] or "")
            rows = conn.execute(
                "SELECT content, tool_calls, reasoning_content FROM messages "
                "WHERE session_id = ? AND active = 1", (sid,)).fetchall()
            if rows:
                used = 0
                for content, tool_calls, reasoning in rows:
                    used += _estimate_tokens_rough(content or "")
                    used += _estimate_tokens_rough(tool_calls or "")
                    used += _estimate_tokens_rough(reasoning or "")
                win = _window_for(model or row_model, "hermes")
                if win > 0 and used > 0:
                    return int(used), int(win)
            # 口径 A 回退
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
            win = _window_for(model or row_model, "hermes")
            if win <= 0:
                return None
            return int(typical), int(win)
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        LOG.debug("ctx_watermark 读 Hermes state.db 失败(%s)", exc)
        return None


def read_watermark(caller: str, session_id: str,
                   agent_cfg: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """读真源水位 → dict；读不到返回 None（不渲染，绝不编造数字）。

    两级：① 本机直读（若本机就是该 agent 所在节点）；② 远端 a2a 服务
    （/ctx-usage/<agent>，本机读不到时用，例子：chat_svc 在龙虾、会话在 thinkbook）。
    """
    if not caller or not session_id:
        return None
    # ① 本机直读
    kind = _backend_kind(agent_cfg)
    model = str((agent_cfg or {}).get("model") or "")
    usage = None
    if kind == "hermes":
        usage = _read_hermes(caller, session_id, model)
    elif kind == "openclaw":
        usage = _read_openclaw(caller, session_id)
    if usage:
        used, window = int(usage[0]), int(usage[1])
        if window > 0:
            return _fmt(used, window)
    # ② 远端 a2a 服务（本机不是该 agent 所在节点 / 本机读不到）
    return _read_remote(caller, session_id)


def _fmt(used: int, window: int) -> Dict[str, Any]:
    pct = int(round(100.0 * used / window))
    return {
        "used": used,
        "window": window,
        "pct": pct,
        "text": "上下文：约 %dk/%dk (%d%%)" % (used // 1000, window // 1000, pct),
    }


def _read_remote(caller: str, session_id: str) -> Optional[Dict[str, Any]]:
    """调本节点 a2a 服务的 /ctx-usage（同机同源），拿真源水位。读不到返回 None。"""
    base = A2A_BASE
    token = A2A_TOKEN
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
        return _fmt(used, window)
    except Exception as exc:  # noqa: BLE001
        LOG.debug("ctx_watermark 远端 ctx-usage 失败(%s)", exc)
        return None
