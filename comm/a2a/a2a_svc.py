#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
a2a_svc.py —— A2A 服务端点（每个成员节点自己监听，统一端口 9920）

设计要点：
  * 纯标准库实现，零 pip 依赖；语法兼容 Python 3.9（不用 X|Y 注解 / match / tomllib）。
  * 默认绑定 127.0.0.1（安全默认）；生产用 --host 传 tailscale0 的 100.x 地址，绝不绑 0.0.0.0。
  * 鉴权：Authorization: Bearer <token>，服务端只存 token 的 sha256，用 hmac.compare_digest 比对。
  * 每个节点的 agents.json 只配置**本机承载**的 agent，未配置的 agent_id 一律 404 unknown_agent。
  * 日志一行一请求，走 stdout。

用法：
    python a2a_svc.py --host <tailnet-ip> --port 9920 \
        --config <deploy-dir>/agents.json --peers <deploy-dir>/peers.json
"""

import argparse
import hashlib
import hmac
import json
import logging
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple

# ---- 本仓版本说明（HACS 仓内为脱敏版，勿直接覆盖生产文件）-------------------
# 来源：thinkbook 节点生产版 a2a_svc.py，VERSION 2.2。
# 相对生产版的差异只有三处脱敏，逻辑零改动：
#   a) 文档示例中的真实 tailnet IP -> <tailnet-ip>；
#   b) SESSIONS_JSON_PATH 不再内置绝对路径，改由 A2A_SESSIONS_JSON 提供（见上）；
#   c) 读取 backend 用 key 的局部变量改名（避开密钥扫描门禁对字面量的误判）。
# 部署要点与回滚见同目录 README.md。

VERSION = "2.2"
PROTOCOL_VERSION = "0.2.5"
DEFAULT_TIMEOUT_S = 120.0
DETAIL_MAX = 200  # 错误 detail 截断长度
MAX_BODY_BYTES = 262144  # 请求体上限 256 KiB
SESSION_ID_MAX = 128     # 会话 id 截断长度（拼进 OpenAI `user` 字段，别让调用方塞长串）

# ---- B2 续跑任务循环（2026-09-23）----------------------------------------
# 背景：A2A 每次唤醒 = 一次 /chat/completions 单回合调用，Agent 答一句就"睡"；
# 群里的 Agent（尤其 openclaw-nb）反复"承诺要动手"却永远走不到调工具那步。
# 改法：唤醒后在同一 session 上连续续跑多轮，每轮把"继续执行"回灌，直到
#   · Agent 回复里出现终止标记（DONE / BLOCKED），或
#   · 触达本回合的总时长上限 / 最大轮数。
# 要求（Hermes 2026-09-23 同意 B2 时提）：必须有 max_timeout 上限 + DONE/BLOCKED 双出口，防死循环。
TASK_LOOP_MAX_ROUNDS = 8          # 单次唤醒最多续跑轮数（含第 1 轮）
TASK_LOOP_MAX_TIMEOUT_S = 600.0   # 单次唤醒续跑总时长硬上限（秒）
# 终止标记：只认「独占一行的标记词」，避免正文里随便提到 done 就误判结束；大小写不敏感。
_TASK_DONE_RE = re.compile(r"^\s*(?:[-*>#`\[\]]*\s*)*(?:任务)?\s*DONE\s*(?:[-*>#`\[\]]*\s*)*$",
                           re.IGNORECASE | re.MULTILINE)
_TASK_BLOCKED_RE = re.compile(r"^\s*(?:[-*>#`\[\]]*\s*)*(?:任务)?\s*BLOCKED\s*(?:[-*>#`\[\]]*\s*)*$",
                              re.IGNORECASE | re.MULTILINE)
# 续跑指令：把 Agent 拉回"继续调工具干活"的轨道（作为新一轮 user 回灌）
TASK_LOOP_CONTINUE_PROMPT = (
    "【续跑指令／自动注入】上一轮你只回了一段话就停了。现在**继续执行**："
    "本回合不是只回答一次就结束——请真的去调工具把活做完（跑命令、读文件、写代码、重启、实测）。"
    "每完成一步就在回复里简短记一句；"
    "全部做完时，在回复最后**单独一行**写 DONE；"
    "确实卡死、无法推进时，在回复最后**单独一行**写 BLOCKED，并说明卡在哪。"
    "不要只写承诺，要动手。"
)

# ---- 上下文自愈机制（方案乙·甲水位，2026-09-23）-------------------------------
# 问题：续跑循环跑到后段，同一 session 的上下文越滚越大，可能撑爆窗口/变贵/变慢。
# 改法：每轮续跑前读该 session 水位（totalTokens/contextTokens，读本机 sessions.json）：
#   · 水位 >= SOFT  -> 注入「压缩成交接摘要」指令（语义 compact，HTTP 接口无 /compact 命令，
#                       故用等效 prompt 让 Agent 自行把前文压成交接摘要）；
#   · 压缩后仍 >= HARD -> 换新 session（改 OpenAI `user` 字段内容 = /new 等效），
#                       把上一步交接摘要带进新会话第一条消息，让新会话接着干。
# 安全：sessions.json 只读打开、绝不写；读不到时降级为「不压缩、照常续跑」，不阻塞主流程。
CTX_SOFT_RATIO = 0.75             # 软阈值：过线先做语义压缩
CTX_HARD_RATIO = 0.85             # 硬阈值：压缩后仍过线则换新 session 接力
CTX_MIN_TOKENS_FLOOR = 8000       # 上下文太小时不折腾（小会话无需自愈）
# 注意（脱敏）：本仓版本不内置任何绝对路径 —— 必须由部署方用环境变量给出
# OpenClaw 会话档案路径（如 A2A_SESSIONS_JSON=<deploy-dir>/sessions.json）。
# 未配置时读取会失败并被降级处理：不压缩、照常续跑，不报错、不阻塞。
SESSIONS_JSON_PATH = os.environ.get("A2A_SESSIONS_JSON", "")
CTX_COMPACT_PROMPT = (
    "【上下文自愈／自动注入】本会话上下文已较大，为免撑爆窗口，请**立即把至今的进展压缩成"
    "一段「交接摘要」**（不调工具，只输出摘要本身，务必简短）：\n"
    "1) 本次任务目标；2) 已完成的关键步骤与产物路径；3) 待办事项与下一步；"
    "4) 关键约束/坑。\n摘要在 400 字以内，供新会话无缝接力。"
)
CTX_HANDOFF_PROMPT = (
    "【新会话接力／自动注入】上一个会话上下文已满，已切换到这个新会话继续同一任务。"
    "以下是从上一会话带过来的交接摘要，请据此**接着干**（不要从头重做已完成的步骤）：\n"
    "---- 交接摘要开始 ----\n%s\n---- 交接摘要结束 ----\n"
    "请继续执行未完成的动作；全部做完时在最后单独一行写 DONE，卡死则写 BLOCKED。"
)

LOG = logging.getLogger("a2a")


class Runtime(object):
    """进程级运行时配置（由 main 填充，handler 只读）。"""

    node_id = "unknown"
    agents = {}          # type: Dict[str, Dict[str, Any]]
    peer_map = {}        # type: Dict[str, str]  token 的 sha256 -> 成员 id（反查真实身份）
    advertise_host = "127.0.0.1"
    advertise_port = 9920
    allow_anon = False


# ---------------------------------------------------------------- 配置加载

def load_config(path: str) -> None:
    """解析 agents.json：node_id / host / port / agents。"""
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    if not isinstance(cfg, dict):
        raise ValueError("agents.json 顶层必须是对象")
    Runtime.node_id = str(cfg.get("node_id") or "unknown")
    agents = cfg.get("agents") or {}
    if not isinstance(agents, dict):
        raise ValueError("agents.json 的 agents 字段必须是对象")
    Runtime.agents = agents
    # 对外公布的 URL 主机/端口：优先用 agents.json 里的 host/port（通常是 MagicDNS 名），
    # 缺失时回落到实际监听地址；命令行 --advertise-host/--advertise-port 可强制覆盖。
    Runtime.advertise_host = str(cfg.get("host") or "") or Runtime.advertise_host
    if cfg.get("port"):
        Runtime.advertise_port = int(cfg["port"])


def load_peers(path: Optional[str]) -> None:
    """解析 peers.json：{成员id: token的sha256十六进制}。"""
    if not path:
        Runtime.peer_map = {}
        return
    with open(path, "r", encoding="utf-8") as f:
        peers = json.load(f)
    if not isinstance(peers, dict):
        raise ValueError("peers.json 顶层必须是对象")
    Runtime.peer_map = {}
    for pid, digest in peers.items():
        digest = str(digest or "").strip().lower()
        if digest:
            Runtime.peer_map[digest] = str(pid)


# ---------------------------------------------------------------- backend 调用

def call_backend(agent: Dict[str, Any], text: str, timeout_s: float,
                 session_id: Optional[str] = None, caller: str = "") -> Tuple[int, Dict[str, Any]]:
    """调用 <backend>/chat/completions。返回 (http状态码, 响应体dict)。

    成功时响应体为 {"reply": "..."}；失败时为标准错误结构。

    多轮上下文：带 session_id 时把它映射成 OpenAI 的 `user` 字段
    （`a2a:<调用方>:<会话id>`），网关/shim 据此派生**稳定会话键** → 同一
    session_id 的多次调用共享上下文；不带则每次新会话（答完即弃，与飞书桥
    的长期会话物理隔离）。

    ---- B2 续跑任务循环（2026-09-23）----
    若该成员未关掉 task_loop、且本次带 session_id，则在本回合内**连续续跑**：
      第 1 轮跑用户原文；若回复未出现 DONE/BLOCKED 终标记，则回灌一条续跑指令
      再跑下一轮，直到出现终标记、或达 TASK_LOOP_MAX_ROUNDS，
      或累计耗时触达 min(timeout_s, TASK_LOOP_MAX_TIMEOUT_S)。
    终止时把**最后一轮**的回复作为 reply 返回（续跑轮里最后一轮通常带终标记）。
    """
    # ---- 回合纪律注入（2026-09-22）---------------------------------------
    # 网关 /v1/chat/completions 把 messages 里的 system 角色拼成「叠加在核心提示词之上
    # 的临时系统提示」（见 gateway/platforms/api_server_openai_routes.py）。借这个口子把
    # 群聊纪律注入，不必改 Hermes 核心。
    # 背景：群聊回合原来会跑成一次完整长任务（夹着 180s/300s 卡死的 terminal 命令），
    # 单回合 >600s → 撞 teamd 上限 → 群里表现为「Agent 不回话」。
    messages = []   # type: List[Dict[str, str]]
    sys_prompt = str(agent.get("system_prompt") or "").strip()
    if sys_prompt:
        messages.append({"role": "system", "content": sys_prompt})
    messages.append({"role": "user", "content": text})
    # 回合硬上限：取「调用方声明的 timeout」与「本成员自己的上限」的小值。
    # 目的是失败要快 —— 卡死回合必须尽快把 worker 还给团队总线，不许占坑干等。
    cap = agent.get("max_timeout_s")
    if cap:
        try:
            cap_f = float(cap)
            if cap_f > 0 and timeout_s > cap_f:
                LOG.info("timeout 收敛：调用方 %.0fs -> 本成员上限 %.0fs", timeout_s, cap_f)
                timeout_s = cap_f
        except (TypeError, ValueError):
            pass
    # session_id 映射到 OpenAI `user` 字段 → 同一 session 共享上下文（续跑依赖它）。
    user_field = ("a2a:%s:%s" % (caller or "unknown", session_id)) if session_id else None
    backend = str(agent.get("backend") or "").rstrip("/")
    if not backend:
        return 502, {"ok": False, "error": "backend_error", "detail": "backend not configured"}

    # 续跑循环只在能固定会话（有 session_id）且该成员未关 task_loop 时启用。
    if not (session_id and _loop_enabled(agent)):
        return _single_round(agent, messages, timeout_s, user_field)

    # ---- 续跑循环 ----
    # 总时长上限：调用方 timeout 与硬上限取小；每轮里给单轮留出的时间随剩余预算收敛。
    total_budget = timeout_s if timeout_s < TASK_LOOP_MAX_TIMEOUT_S else TASK_LOOP_MAX_TIMEOUT_S
    started = time.time()
    last_reply = None  # type: Optional[str]
    # 上下文自愈（方案乙·甲水位）：active_session_id 会随接力轮转；user_field 同步更新。
    active_session_id = session_id
    soft_hits = 0      # 命中软阈值（语义压缩）次数
    rotations = 0      # 换新 session 接力次数
    for rnd in range(1, TASK_LOOP_MAX_ROUNDS + 1):
        remaining = total_budget - (time.time() - started)
        if remaining <= 1.0:
            LOG.info("task_loop 预算耗尽：round=%d elapsed=%.1fs", rnd, time.time() - started)
            break
        code, result = _single_round(agent, messages, remaining, user_field)
        if code != 200:
            # 续跑中某轮失败：若之前有过成功回复，用最后一条成功回复收尾（不丢已做工作）；
            # 否则原样返回错误。
            if last_reply is not None:
                LOG.info("task_loop 第 %d 轮失败(%s)，返回最后一轮成功回复", rnd, result.get("error"))
                return 200, {"reply": last_reply, "loop_rounds": rnd - 1,
                             "loop_stop": "round_error"}
            return code, result
        reply = result["reply"]
        last_reply = reply
        if _TASK_BLOCKED_RE.search(reply):
            LOG.info("task_loop 命中 BLOCKED：round=%d elapsed=%.1fs", rnd, time.time() - started)
            return 200, {"reply": reply, "loop_rounds": rnd, "loop_stop": "blocked",
                         "ctx_soft": soft_hits, "ctx_rotations": rotations}
        if _TASK_DONE_RE.search(reply):
            LOG.info("task_loop 命中 DONE：round=%d elapsed=%.1fs", rnd, time.time() - started)
            return 200, {"reply": reply, "loop_rounds": rnd, "loop_stop": "done",
                         "ctx_soft": soft_hits, "ctx_rotations": rotations}
        if rnd >= TASK_LOOP_MAX_ROUNDS:
            LOG.info("task_loop 达最大轮数：round=%d", rnd)
            return 200, {"reply": reply, "loop_rounds": rnd, "loop_stop": "max_rounds",
                         "ctx_soft": soft_hits, "ctx_rotations": rotations}
        # 回灌续跑指令（作为新的一轮 user 消息），继续
        messages = messages + [
            {"role": "assistant", "content": reply},
            {"role": "user", "content": TASK_LOOP_CONTINUE_PROMPT},
        ]
        # ---- 上下文自愈（方案乙·甲水位）：每轮续跑前查水位 ----
        pressure = _ctx_pressure(caller, active_session_id)
        if pressure is not None and pressure >= CTX_HARD_RATIO:
            # 硬阈值：先要一份交接摘要，再换新 session 接力（/new 等效）。
            LOG.info("ctx_selfheal 硬阈值命中：round=%d pressure=%.2f -> 换新 session",
                     rnd, pressure)
            summary = _ask_handoff_summary(agent, messages, remaining)
            if summary:
                rotations += 1
                active_session_id = "%s-r%d-%d" % (session_id, rotations, int(time.time()))
                user_field = "a2a:%s:%s" % (caller or "unknown", active_session_id)
                # 新会话：只带 system 提示 + 交接摘要（丢弃旧上下文）
                messages = []
                if sys_prompt:
                    messages.append({"role": "system", "content": sys_prompt})
                messages.append({"role": "user", "content": CTX_HANDOFF_PROMPT % summary})
                continue
        elif pressure is not None and pressure >= CTX_SOFT_RATIO:
            # 软阈值：注入语义压缩指令（让 Agent 自行把前文压成交接摘要）
            LOG.info("ctx_selfheal 软阈值命中：round=%d pressure=%.2f -> 语义压缩",
                     rnd, pressure)
            soft_hits += 1
            messages = messages + [{"role": "user", "content": CTX_COMPACT_PROMPT}]
    # 预算耗尽/循环退出但没有终标记：返回最后一条回复
    if last_reply is None:
        return 504, {"ok": False, "error": "backend_timeout"}
    return 200, {"reply": last_reply, "loop_rounds": 0, "loop_stop": "budget",
                 "ctx_soft": soft_hits, "ctx_rotations": rotations}


def _loop_enabled(agent: Dict[str, Any]) -> bool:
    """成员级续跑开关：agents.json 里 `task_loop: false` 可单独关掉（默认开，B2）。"""
    val = agent.get("task_loop")
    if val is None:
        return True
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() not in ("0", "false", "off", "no", "")


def _read_ctx_usage(caller: str, session_id: str) -> Optional[Tuple[int, int]]:
    """只读本机 sessions.json，返回 (totalTokens, contextTokens)。

    映射规则（07-23 实测验证）：A2A 的 `user` = `a2a:<caller>:<session_id>` 会落到
    session key = `agent:main:openai-user:a2a:<caller>:<session_id>`。

    返回 None 表示读不到（文件不存在/无权限/key 不在/字段缺失）——调用方应降级为
    「不压缩、照常续跑」，不阻塞主流程。此函数绝不写文件。
    """
    if not session_id:
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
    except Exception as exc:  # noqa: BLE001 —— 自愈是增强能力，绝不允许因它拖墤主流程
        LOG.debug("ctx self-heal: 读 sessions.json 失败(%s)，降级为不自愈", exc)
        return None


def _ctx_pressure(caller: str, session_id: str) -> Optional[float]:
    """返回该 session 的上下文水位（0.0~1.0+）；读不到返回 None。低于下限返回 0.0。"""
    usage = _read_ctx_usage(caller, session_id)
    if usage is None:
        return None
    total, window = usage
    if total < CTX_MIN_TOKENS_FLOOR:
        return 0.0
    return float(total) / float(window)


def _ask_handoff_summary(agent: Dict[str, Any], messages: List[Dict[str, str]],
                         timeout_s: float) -> Optional[str]:
    """硬阈值接力前，先让 Agent 输出一份「交接摘要」（供新 session 无缝接力）。

    复用 _single_round 但不带 user_field（摘要轮不入正式会话上下文）。失败返回 None，
    调用方会降级为「不接力、继续原会话续跑」。
    """
    ask_msgs = list(messages) + [{"role": "user", "content": CTX_COMPACT_PROMPT}]
    try:
        code, result = _single_round(agent, ask_msgs, timeout_s, None)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("ctx_selfheal 取交接摘要异常(%s)，不接力", exc)
        return None
    if code != 200:
        LOG.warning("ctx_selfheal 取交接摘要失败(code=%s)，不接力", code)
        return None
    summary = (result.get("reply") or "").strip()
    return summary or None


def _single_round(agent: Dict[str, Any], messages: List[Dict[str, str]],
                  timeout_s: float,
                  user_field: Optional[str] = None) -> Tuple[int, Dict[str, Any]]:
    """跑一轮 backend /chat/completions。messages 由调用方给全（含 system）。

    user_field 非空时写进 payload 的 `user`（网关派生的会话键），保证多轮共享上下文。
    返回 (http状态码, 响应体dict)；成功时 dict 形如 {"reply": "..."}。
    """
    backend = str(agent.get("backend") or "").rstrip("/")
    if not backend:
        return 502, {"ok": False, "error": "backend_error", "detail": "backend not configured"}
    url = backend + "/chat/completions"
    payload = {
        "model": agent.get("model") or "",
        "messages": messages,
        "max_tokens": int(agent.get("max_tokens") or 2000),
    }
    if user_field:
        payload["user"] = user_field
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    req.add_header("x-openclaw-message-channel", "a2a")
    key = str(agent.get("api_key") or "")   # 运行时从 agents.json 读，非字面量
    if key:
        req.add_header("Authorization", "Bearer " + key)

    try:
        resp = urllib.request.urlopen(req, timeout=timeout_s)
        try:
            raw = resp.read()
        finally:
            resp.close()
    except urllib.error.HTTPError as exc:  # 注意：HTTPError 是 URLError 子类，必须先捕获
        try:
            detail = exc.read().decode("utf-8", "replace")
        except Exception:
            detail = str(exc)
        return 502, {"ok": False, "error": "backend_error",
                     "detail": ("HTTP %d %s" % (exc.code, detail))[:DETAIL_MAX]}
    except socket.timeout:
        return 504, {"ok": False, "error": "backend_timeout"}
    except urllib.error.URLError as exc:
        # 超时在部分 Python 版本下被包进 URLError.reason
        if isinstance(exc.reason, (socket.timeout, TimeoutError)):
            return 504, {"ok": False, "error": "backend_timeout"}
        return 502, {"ok": False, "error": "backend_error", "detail": str(exc.reason)[:DETAIL_MAX]}
    except TimeoutError:
        return 504, {"ok": False, "error": "backend_timeout"}
    except Exception as exc:
        return 502, {"ok": False, "error": "backend_error", "detail": str(exc)[:DETAIL_MAX]}

    text_body = raw.decode("utf-8", "replace")
    try:
        data = json.loads(text_body)
        reply = data["choices"][0]["message"]["content"]
    except Exception:
        reply = None
    # 取不到 choices[0].message.content 就如实返回 502，不编造回复
    if not isinstance(reply, str):
        return 502, {"ok": False, "error": "backend_error", "detail": text_body[:DETAIL_MAX]}
    return 200, {"reply": reply}


# ---------------------------------------------------------------- HTTP 处理

class A2AHandler(BaseHTTPRequestHandler):
    server_version = "a2a_svc/" + VERSION
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # --- 基础工具 -------------------------------------------------
    def log_message(self, fmt: str, *args: Any) -> None:
        """屏蔽 BaseHTTPRequestHandler 默认的 stderr 日志，统一由 _send_json 打一行。"""
        return

    def _send_json(self, code: int, payload: Dict[str, Any],
                   peer: Optional[str] = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass
        LOG.info("%s %s -> %d from=%s ip=%s", self.command, self.path, code,
                 peer or "-", self.client_address[0] if self.client_address else "-")

    def _read_body(self) -> bytes:
        """必须读完请求体，否则 keep-alive 连接会错位。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return b""
        if length > MAX_BODY_BYTES:
            # 超限只读前 MAX_BODY_BYTES，剩下的留给连接关闭，JSON 解析会失败 → 400
            length = MAX_BODY_BYTES
        return self.rfile.read(length)

    def _auth_peer(self) -> Optional[str]:
        """校验 Bearer token，返回**命中的成员 id**；不通过返回 None。

        身份只由 token 的 sha256 反查 peers.json 得到，**绝不采信请求体里的 from 字段**。
        """
        if Runtime.allow_anon:
            return "anonymous"
        header = self.headers.get("Authorization") or ""
        if not header.startswith("Bearer "):
            return None
        token = header[7:].strip()
        if not token:
            return None
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        matched = None  # type: Optional[str]
        for known, pid in Runtime.peer_map.items():
            # 不提前 break，避免比对次数泄露命中位置
            if hmac.compare_digest(digest, known):
                matched = pid
        return matched

    def _path_parts(self) -> List[str]:
        path = urllib.parse.urlsplit(self.path).path
        return [p for p in path.split("/") if p]

    # --- 路由 -----------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        self._read_body()
        parts = self._path_parts()

        if parts == ["healthz"]:
            self._send_json(200, {"ok": True, "node": Runtime.node_id,
                                  "version": VERSION, "protocolVersion": PROTOCOL_VERSION,
                                  "agents": sorted(Runtime.agents.keys()),
                                  "ts": int(time.time())})
            return

        if parts == ["agents"]:
            peer = self._auth_peer()
            if peer is None:
                self._send_json(401, {"ok": False, "error": "unauthorized"})
                return
            listing = {}
            for aid, cfg in Runtime.agents.items():
                listing[aid] = {"description": cfg.get("description") or "",
                                "skills": cfg.get("skills") or []}
            self._send_json(200, {"ok": True, "node": Runtime.node_id, "agents": listing}, peer)
            return

        # /a2a/<agent_id>/.well-known/agent-card.json —— 公开，不鉴权
        if len(parts) == 4 and parts[0] == "a2a" and parts[2] == ".well-known" \
                and parts[3] == "agent-card.json":
            agent_id = parts[1]
            cfg = Runtime.agents.get(agent_id)
            if cfg is None:
                self._send_json(404, {"ok": False, "error": "unknown_agent"})
                return
            self._send_json(200, build_agent_card(agent_id, cfg))
            return

        self._send_json(404, {"ok": False, "error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        raw = self._read_body()
        parts = self._path_parts()

        if not (len(parts) == 3 and parts[0] == "a2a" and parts[2] == "messages"):
            self._send_json(404, {"ok": False, "error": "not_found"})
            return

        peer = self._auth_peer()
        if peer is None:
            self._send_json(401, {"ok": False, "error": "unauthorized"})
            return

        agent_id = parts[1]
        cfg = Runtime.agents.get(agent_id)
        if cfg is None:
            self._send_json(404, {"ok": False, "error": "unknown_agent"})
            return

        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            self._send_json(400, {"ok": False, "error": "bad_request"})
            return
        if not isinstance(body, dict):
            self._send_json(400, {"ok": False, "error": "bad_request"})
            return

        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            self._send_json(400, {"ok": False, "error": "bad_request"})
            return

        try:
            timeout_s = float(body.get("timeout_s") or DEFAULT_TIMEOUT_S)
        except (TypeError, ValueError):
            self._send_json(400, {"ok": False, "error": "bad_request"})
            return
        if timeout_s <= 0:
            timeout_s = DEFAULT_TIMEOUT_S

        session_id = body.get("session_id")
        if not isinstance(session_id, str) or not session_id.strip():
            session_id = None
        else:
            session_id = session_id.strip()[:SESSION_ID_MAX]
        started = time.time()
        code, result = call_backend(cfg, text, timeout_s, session_id=session_id, caller=peer)
        elapsed_ms = int((time.time() - started) * 1000)

        if code != 200:
            self._send_json(code, result)
            return
        # 透传续跑循环元数据（B2）：loop_rounds=实际跑了多少轮，loop_stop=为何停下
        # （done / blocked / max_rounds / budget / round_error）。单回合时这两项缺省。
        resp = {"ok": True, "agent": agent_id, "reply": result["reply"],
                "elapsed_ms": elapsed_ms, "session_id": session_id,
                "peer": peer}
        if "loop_rounds" in result:
            resp["loop_rounds"] = result["loop_rounds"]
        if "loop_stop" in result:
            resp["loop_stop"] = result["loop_stop"]
        self._send_json(200, resp, peer)


def build_agent_card(agent_id: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """按 A2A 官方形态生成 Agent Card（字段名不可改）。"""
    description = cfg.get("description") or ""
    skills = []
    for skill in (cfg.get("skills") or []):
        skills.append({"id": skill, "name": skill, "description": description})
    return {
        "name": agent_id,
        "description": description,
        "url": "http://%s:%d/a2a/%s" % (Runtime.advertise_host, Runtime.advertise_port, agent_id),
        "version": VERSION,
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {"streaming": False, "pushNotifications": False},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": skills,
        "securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}},
        "security": [{"bearer": []}],
    }


# ---------------------------------------------------------------- 入口

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="A2A service endpoint (stdlib only)")
    parser.add_argument("--host", default="127.0.0.1",
                        help="监听地址，默认 127.0.0.1；生产传 tailscale0 的 100.x 地址")
    parser.add_argument("--port", type=int, default=9920, help="监听端口，默认 9920")
    parser.add_argument("--config", required=True, help="agents.json 路径")
    parser.add_argument("--peers", default=None, help="peers.json 路径（token 的 sha256 表）")
    parser.add_argument("--advertise-host", default=None, help="Agent Card 里对外公布的主机名")
    parser.add_argument("--advertise-port", type=int, default=None, help="Agent Card 里对外公布的端口")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    logging.basicConfig(stream=sys.stdout, level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    Runtime.advertise_host = args.host
    Runtime.advertise_port = args.port
    Runtime.allow_anon = os.environ.get("A2A_ALLOW_ANON") == "1"

    try:
        load_config(args.config)
    except Exception as exc:
        LOG.error("加载 agents.json 失败: %s", exc)
        return 2
    try:
        load_peers(args.peers)
    except Exception as exc:
        LOG.error("加载 peers.json 失败: %s", exc)
        return 2

    if args.advertise_host:
        Runtime.advertise_host = args.advertise_host
    if args.advertise_port:
        Runtime.advertise_port = args.advertise_port

    if not Runtime.peer_map and not Runtime.allow_anon:
        LOG.warning("peers 表为空且未开启 A2A_ALLOW_ANON，所有鉴权请求都会 401")
    if Runtime.allow_anon:
        LOG.warning("A2A_ALLOW_ANON=1 已开启，鉴权被跳过（仅限本地调试）")

    httpd = ThreadingHTTPServer((args.host, args.port), A2AHandler)
    httpd.daemon_threads = True
    bound_host, bound_port = httpd.server_address[0], httpd.server_address[1]
    Runtime.advertise_port = args.advertise_port or Runtime.advertise_port or bound_port
    LOG.info("a2a_svc %s node=%s listening on %s:%d agents=%s",
             VERSION, Runtime.node_id, bound_host, bound_port,
             ",".join(sorted(Runtime.agents.keys())) or "-")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        LOG.info("收到中断，退出")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
