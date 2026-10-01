#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""会话上下文存档器：上下文涨到阈值(默认 120K)时，把现场未完成任务结构化存档，
并写回 chat_svc 的「群记忆锚 RoomMemo」+ 发一条群消息，重启后靠群消息/群记忆恢复现场。

为什么要它（缺口）：
    Hermes 的上下文压缩/清理是**会话进程内**行为（agent/turn_context_compaction.py +
    agent/context_compressor.py），外部进程没有入口去读「这个会话现在多少 token」，
    也没有入口去触发压缩。而 Hermes 会话一旦被压缩/重启，现场（干到哪了、还差什么）
    就只剩日志。本脚本补的就是这段：把"现场"从易失的会话上下文里搬到**持久层**
    （本机 JSON/MD 存档 + chat_svc 群记忆），重启后能照着继续干。

事实依据（探明结论，详见 --facts）：
    * 上下文规模：~/AppData/Local/hermes/logs/agent.log 每行带 [session_id]，
      conversation_loop 行含 "in=<上下文> out=<> total="。
    * 群记忆：chat_svc GET/PUT/PATCH /api/sessions/{sid}/memo（chat_svc.py 契约 §I.6）。
      PUT 整份替换、PATCH **只追加**（数组追加 / goal|resume 覆盖），写口带 If-Match 乐观锁。
    * 群消息：POST /api/sessions/{sid}/messages，X-Agent-Id 署名，Agent 必须 @ 群内成员。
    * 压缩入口：只有会话内 slash 命令 /compress（hermes_cli/commands.py:80），
      外部无 API/CLI —— 本脚本**不会伪造**压缩，只打印事实。

用法：
    # 单会话（手动/被 cron 调用）
    python session_ctx_archiver.py --session-id 20260922_121121_21253d --group meet_20260922_538d06
    # 扫描所有超阈值会话（默认行为）
    python session_ctx_archiver.py --scan
    # 只看不写
    python session_ctx_archiver.py --scan --dry-run
    # 强制存档（忽略幂等去重）
    python session_ctx_archiver.py --session-id xxx --force

纪律：不写任何口令；chat_svc 走 tailnet 免登录 + X-Agent-Id 成员头。
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request

# 批⑪ 归位：本件已搬入 hub/l3_domain/。仓根 + 本目录一并入路径（批① 惯例），
# 跨层件按**绝对包名** import（原：裸名 `import node_profile`）。
_HERE_BOOT = os.path.dirname(os.path.abspath(__file__))
for _hacs_p in (os.path.dirname(os.path.dirname(_HERE_BOOT)), _HERE_BOOT):
    if _hacs_p not in sys.path:
        sys.path.insert(0, _hacs_p)

from node.l0_host import node_profile as _npf  # noqa: E402  L0 节点档案：宿主事实唯一真源（对话台基址；env 优先）

# ---------------- 常量 ----------------
HERMES_HOME = os.environ.get("HERMES_HOME") or os.path.expanduser(
    os.path.join("~", "AppData", "Local", "hermes"))
AGENT_LOG = os.path.join(HERMES_HOME, "logs", "agent.log")
STATE_DB = os.path.join(HERMES_HOME, "state.db")


def set_hermes_home(path):
    """计划任务以 SYSTEM 身份跑时 ~ 会解析到 systemprofile：必须能显式指定 profile 家目录。"""
    global HERMES_HOME, AGENT_LOG, STATE_DB
    HERMES_HOME = path
    AGENT_LOG = os.path.join(path, "logs", "agent.log")
    STATE_DB = os.path.join(path, "state.db")
    return {"hermes_home": HERMES_HOME, "agent_log": AGENT_LOG, "state_db": STATE_DB}

HERE = os.path.dirname(os.path.abspath(__file__))
ARCHIVE_ROOT = os.path.join(HERE, "ctx_archive")
STATE_FILE = os.path.join(HERE, "state", "ctx_archiver_state.json")

CHAT_BASE_DEFAULT = os.environ.get("AGENT_TEAM_CHAT_BASE") or _npf.chat_base_default()
DEFAULT_AGENT_ID = "hermes"

DEFAULT_THRESHOLD = 90000
# 幂等：同一会话再次存档，至少要再多涨这么多 token（否则不重复写）
REARCHIVE_DELTA = 20000

MSG_SCAN_LIMIT = 400          # 从 state.db 取该会话最后多少条消息做提取
TEXT_MAX = 4000               # chat_svc 群消息正文上限（chat_svc.py TEXT_MAX）
SKIP_GROUP_TTL_S = 3600       # 群写失败后多久不再重试（避免刷屏/刷错）
GROUP_MAP = {}                # {Hermes session_id(或前缀*) : chat_svc 群 id}


def resolve_group(sid):
    """把 Hermes 会话映射到 chat_svc 群。今天**没有**自动映射，只能由人/agent 填 group_map.json。
    精确 id 优先；其次取 * 结尾的前缀匹配。都不中就返回 ""（= 只落本机）。"""
    if sid in GROUP_MAP:
        return GROUP_MAP[sid]
    for k, v in GROUP_MAP.items():
        if k.endswith("*") and sid.startswith(k[:-1]):
            return v
    return ""


LOG_FILE = None


def log(*a):
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), " ".join(str(x) for x in a))
    try:
        print(line, flush=True)
    except Exception:
        pass
    if LOG_FILE:
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


# ---------------- HTTP ----------------
def http(method, url, body=None, agent_id=None, extra_headers=None, timeout=20.0):
    """返回 (status, json_or_text)。不抛异常，失败也回结构化结果。"""
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if agent_id:
        headers["X-Agent-Id"] = agent_id
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            try:
                return r.status, (json.loads(raw) if raw.strip() else None)
            except Exception:
                return r.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw
    except Exception as exc:
        return 0, {"error": "%s: %s" % (type(exc).__name__, exc)}


# ---------------- agent.log：读上下文 token ----------------
API_RE = re.compile(
    r"INFO \[([A-Za-z0-9_\-]+)\] agent\.conversation_loop: API call #(\d+): "
    r".*?in=(\d+) out=(\d+) total=(\d+)")


def scan_conflicts(conflict_sid=None, log_path=None):
    """扫 agent.log，返回 {sid: {"last_in":.., "max_in":.., "last_ts":.., "calls":..}}。

    last_in = 该会话**最新**一次 API 调用的 in= —— 这就是"现在上下文多大"。
    """
    log_path = log_path or AGENT_LOG     # 运行期再取，别在 import 时绑定（SYSTEM 跑计划任务会错到 systemprofile）
    out = {}
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                m = API_RE.search(line)
                if not m:
                    continue
                sid, _call, i, _o, _t = m.group(1), m.group(2), int(m.group(3)), m.group(4), m.group(5)
                if conflict_sid and sid != conflict_sid:
                    continue
                rec = out.get(sid)
                if rec is None:
                    out[sid] = {"last_in": i, "max_in": i, "last_ts": line[:19], "calls": 1}
                else:
                    rec["last_in"] = i
                    rec["max_in"] = max(rec["max_in"], i)
                    rec["last_ts"] = line[:19]
                    rec["calls"] += 1
    except FileNotFoundError:
        log("找不到 agent.log：", log_path)
    return out


# ---------------- state.db：会话元信息 + 消息 ----------------
def session_meta(sid, db_path=None):
    db_path = db_path or STATE_DB
    cols = ("id", "source", "title", "session_key", "chat_id", "chat_type", "cwd",
            "parent_session_id", "message_count", "tool_names", "started_at",
            "last_activity_at", "model", "profile_name")
    try:
        c = sqlite3.connect("file:%s?mode=ro" % db_path.replace("\\", "/"), uri=True)
        r = c.execute("select %s from sessions where id=?" % ",".join(cols), (sid,)).fetchone()
        c.close()
        if not r:
            return {}
        return dict(zip(cols, r))
    except Exception as exc:
        log("读 state.db 会话元信息失败:", exc)
        return {}


def session_messages(sid, limit=MSG_SCAN_LIMIT, db_path=None):
    db_path = db_path or STATE_DB
    try:
        c = sqlite3.connect("file:%s?mode=ro" % db_path.replace("\\", "/"), uri=True)
        rows = list(c.execute(
            "select role, tool_name, content, timestamp from messages "
            "where session_id=? order by timestamp desc limit ?", (sid, limit)))
        c.close()
    except Exception as exc:
        log("读 state.db 消息失败:", exc)
        return []
    rows.reverse()
    return [{"role": r[0], "tool_name": r[1], "content": r[2], "ts": r[3]} for r in rows]


# ---------------- 未完成任务提取（机械，不调 LLM） ----------------
# 口径：只认**显式标记**，不做语义猜测 —— 猜错比漏掉更坏。宁可少提也不要编。
OPEN_MARKERS = ("未完成", "未做完", "未做成", "没做成", "待办", "todo", "TODO", "ToDo",
                "待验证", "待确认", "待补", "待处理", "遗留", "还没", "尚未", "下一步",
                "阻塞", "卡在", "受限", "剩下", "剩余", "未落", "未验证", "未实测",
                "没有验证", "待实测", "pending", "remaining", "blocked")
DONE_MARKERS = ("已完成", "已做完", "已完成并", "✅", "已修复", "已落地", "已上线",
                "已把", "已改", "已加", "已建", "已过",
                "done", "DONE", "实测通过", "验证通过", "复现并修复")
DECISION_MARKERS = ("决定", "定稿", "冻结", "口径", "结论是", "拍板", "定为", "选定",
                    "裁决", "采纳", "确认采用")
BLOCKED_BY_RE = re.compile(r"卡在\s*([A-Za-z0-9_\u4e00-\u9fff\-]{2,20})|阻塞(?:于|在)\s*([A-Za-z0-9_\u4e00-\u9fff\-]{2,20})")
# 纯状态词不构成"已完成/未完成"条目，只是协议回执
STATUS_NOISE = {"done", "DONE", "ok", "OK", "yes", "no", "完成", "已发送", "success"}


def _flatten(text):
    """把消息正文压成可扫的行列表（JSON 正文也尽力拆）。"""
    if not text:
        return []
    s = str(text)
    if s.lstrip().startswith("{"):
        try:
            obj = json.loads(s)
            parts = []
            for k in ("findings", "summary", "status", "error", "note", "result", "text"):
                v = obj.get(k)
                if isinstance(v, str) and v.strip():
                    parts.append(v)
            if parts:
                s = "\n".join(parts)
        except Exception:
            pass
    raw = [ln.strip() for ln in s.splitlines() if ln.strip()]
    # 再按粗体小标题/编号项切开：Agent 的收尾报告常是一整行 "1) … 2) … 3) …"，
    # 整行当作一条会既漏标记又超长。
    # 只按「句末 + 编号项」切，**不要**按 ** 切 —— 那会把加粗短语切成一堆 "**。" 碎片。
    out = []
    for ln in raw:
        if len(ln) > 160:
            for piece in re.split(r"(?<=[。；;！])\s*(?=\d+[\)\.、]\s?\S)", ln):
                piece = _clean_line(piece)
                if piece:
                    out.append(piece)
        else:
            out.append(ln)
    return out


_LEAD_JUNK = re.compile(r"^[\s\-\*#>•·]+")
_LEAD_NUM = re.compile(r"^\d+[\)\.、]\s*")
_LEAD_PUNCT = re.compile(r"^[。，、；：,.;:）\)」』】\s]+")
_TAIL_JUNK = re.compile(r"[\s\*#\-]+$")


def _clean_line(ln):
    """去掉 markdown 记号与编号，留下可读句子；去空后返回 ""。"""
    x = str(ln or "").strip()
    x = x.replace("**", "").replace("`", "")
    x = _LEAD_JUNK.sub("", x)
    x = _LEAD_NUM.sub("", x)
    x = _LEAD_PUNCT.sub("", x)
    x = _TAIL_JUNK.sub("", x)
    x = re.sub(r"\s+", " ", x).strip()
    # 只剩标点/极短的碎片没有信息量
    if len(re.sub(r"[\W_]+", "", x)) < 4:
        return ""
    return x


def _clip(s, n=220):
    s = re.sub(r"\s+", " ", str(s)).strip()
    return s[:n] + ("…" if len(s) > n else "")


def extract_scene(sid, msgs):
    """从会话消息里机械提取：未完成 / 已完成 / 关键决定 / 下一步。"""
    open_tasks, done, decisions = [], [], []
    seen_o, seen_d, seen_dec = set(), set(), set()

    def add(bucket, seen, text, src):
        text = _clean_line(text)
        if not text or text in STATUS_NOISE:
            return
        key = re.sub(r"[\s，。；,.;:：、]+", "", text)[:60]
        if not key or key in seen:
            return
        seen.add(key)
        bucket.append({"text": _clip(text, 240), "src": src})

    for idx, m in enumerate(msgs):
        role = m.get("role")
        src = "#%d %s%s" % (idx, role, "/" + (m.get("tool_name") or "") if m.get("tool_name") else "")
        if role == "tool":
            continue                       # 工具回显里全是别的项目的字眼，噪声大
        for ln in _flatten(m.get("content")):
            if len(ln) < 4:
                continue
            low = ln
            hit_open = any(k in low for k in OPEN_MARKERS) or bool(re.match(r"^\s*[-*]?\s*\[\s\]", ln))
            hit_done = any(k in low for k in DONE_MARKERS)
            hit_dec = any(k in low for k in DECISION_MARKERS)
            if hit_open:
                add(open_tasks, seen_o, ln, src)
            if hit_done:
                add(done, seen_d, ln, src)
            if hit_dec:
                add(decisions, seen_dec, ln, src)

    # 最近一次用户交代 = 本会话的任务书（最能代表"现场要干什么"）
    last_user = ""
    for m in reversed(msgs):
        if m.get("role") == "user":
            last_user = _clip(_clean_line(m.get("content")) or str(m.get("content") or ""), 1200)
            break

    # 最近一次 assistant 的收尾 = 下一步（滤掉纯状态词）
    NOISE = set(STATUS_NOISE) | {"下一步"}
    last_asst = ""
    for m in reversed(msgs):
        if m.get("role") == "assistant" and (m.get("content") or "").strip():
            lines = [x for x in (_clean_line(y) for y in _flatten(m.get("content")))
                     if x and x not in NOISE]
            if lines:
                last_asst = _clip(" / ".join(lines[-3:]), 600)
            break

    # blocked_by：从 open_tasks 文本里抽"卡在 X"
    for t in open_tasks:
        bb = []
        for g in BLOCKED_BY_RE.findall(t["text"]):
            v = next((x for x in g if x), "")
            if v and v not in bb:
                bb.append(v)
        t["blocked_by"] = bb

    return {"open_tasks": open_tasks, "done": done, "decisions": decisions,
            "last_user_brief": last_user, "next_step": last_asst}


# ---------------- 存档落盘 ----------------
def write_archive(sid, ctx, meta, scene, extra):
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    d = os.path.join(ARCHIVE_ROOT, sid)
    os.makedirs(d, exist_ok=True)
    doc = {
        "schema": "ctx-archive/v1",
        "session_id": sid,
        "archived_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "context_tokens": ctx.get("last_in"),
        "context_tokens_max": ctx.get("max_in"),
        "threshold": extra["threshold"],
        "api_calls": ctx.get("calls"),
        "last_api_ts": ctx.get("last_ts"),
        "session_meta": meta,
        "last_user_brief": scene["last_user_brief"],
        "next_step": scene["next_step"],
        "open_tasks": scene["open_tasks"],
        "done": scene["done"],
        "decisions": scene["decisions"],
        "extract_note": "机械提取（显式标记词），未用 LLM 归纳；open_tasks 需人复核后再当口径",
        "group": extra.get("group"),
        "group_write": extra.get("group_write"),
        "compression": extra.get("compression"),
    }
    jp = os.path.join(d, "%s.json" % ts)
    mp = os.path.join(d, "%s.md" % ts)
    with open(jp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    with open(mp, "w", encoding="utf-8") as f:
        f.write(render_md(doc))
    # latest 便于恢复时固定路径
    for src, dst in ((jp, os.path.join(d, "latest.json")), (mp, os.path.join(d, "latest.md"))):
        try:
            with open(src, "rb") as a, open(dst, "wb") as b:
                b.write(a.read())
        except Exception as exc:
            log("latest 复制失败:", exc)
    return jp, mp, doc


def render_md(doc):
    L = []
    L.append("# 上下文现场存档 · %s" % doc["session_id"])
    L.append("")
    L.append("- 存档时间：%s" % doc["archived_at"])
    L.append("- 上下文 token：**%s**（峰值 %s，阈值 %s，API 调用 %s 次，最后一次 %s）"
             % (doc["context_tokens"], doc["context_tokens_max"], doc["threshold"],
                doc["api_calls"], doc["last_api_ts"]))
    m = doc.get("session_meta") or {}
    L.append("- 来源：source=%s · title=%s · cwd=%s · 模型=%s · 父子=%s"
             % (m.get("source"), m.get("title"), m.get("cwd"), m.get("model"),
                m.get("parent_session_id")))
    L.append("- 所属群：%s" % (doc.get("group") or "（未指定，本次未写群记忆）"))
    L.append("")
    L.append("## 本会话任务书（最近一次用户交代）")
    L.append("")
    L.append(doc.get("last_user_brief") or "（未取到）")
    L.append("")
    L.append("## 未完成任务（%d）" % len(doc["open_tasks"]))
    L.append("")
    if doc["open_tasks"]:
        for i, t in enumerate(doc["open_tasks"], 1):
            bb = "｜blocked_by=%s" % ",".join(t["blocked_by"]) if t.get("blocked_by") else ""
            L.append("%d. %s  `%s`%s" % (i, t["text"], t.get("src") or "", bb))
    else:
        L.append("（未扫到显式未完成标记 —— 不代表真没有，见下方「下一步」）")
    L.append("")
    L.append("## 已完成（%d）" % len(doc["done"]))
    L.append("")
    for i, t in enumerate(doc["done"], 1):
        L.append("%d. %s  `%s`" % (i, t["text"], t.get("src") or ""))
    if not doc["done"]:
        L.append("（无显式的已完成标记）")
    L.append("")
    L.append("## 关键决定（%d）" % len(doc["decisions"]))
    L.append("")
    for i, t in enumerate(doc["decisions"], 1):
        L.append("%d. %s  `%s`" % (i, t["text"], t.get("src") or ""))
    if not doc["decisions"]:
        L.append("（无显式决定标记）")
    L.append("")
    L.append("## 下一步（最近一次 Agent 收尾）")
    L.append("")
    L.append(doc.get("next_step") or "（未取到）")
    L.append("")
    L.append("## 恢复步骤")
    L.append("")
    L.append("1. 本文件即现场：`%s`" % os.path.join(ARCHIVE_ROOT, doc["session_id"], "latest.md"))
    if doc.get("group"):
        L.append("2. 群记忆：`GET %s/api/sessions/%s/memo`（open_tasks[].blocked_by 先扫外部阻塞）"
                 % (CHAT_BASE_DEFAULT, doc["group"]))
    L.append("3. 压缩限制：Hermes 无外部压缩 API，见下方 compression 事实。")
    L.append("")
    L.append("## 群写入 / 压缩事实")
    L.append("")
    L.append("```json")
    L.append(json.dumps({"group_write": doc.get("group_write"),
                         "compression": doc.get("compression")},
                        ensure_ascii=False, indent=1))
    L.append("```")
    L.append("")
    L.append("> %s" % doc["extract_note"])
    L.append("")
    return "\n".join(L)


# ---------------- 群记忆 / 群消息 ----------------
def memo_get(base, sid, agent_id):
    st, body = http("GET", "%s/api/sessions/%s/memo" % (base, sid), agent_id=agent_id)
    if st == 200 and isinstance(body, dict) and body.get("ok"):
        return body
    return {"ok": False, "status": st, "body": body}


def memo_patch(base, sid, agent_id, patch, tries=3):
    """PATCH = 只追加（数组追加 / goal|resume 覆盖），带 If-Match 乐观锁，409 自动重取重试。"""
    last = None
    for _ in range(tries):
        cur = memo_get(base, sid, agent_id)
        if not cur.get("ok"):
            return {"ok": False, "stage": "memo_get", "detail": cur}
        etag = cur.get("etag") or ""
        st, body = http("PATCH", "%s/api/sessions/%s/memo" % (base, sid), patch,
                        agent_id=agent_id, extra_headers={"If-Match": etag})
        if st == 200 and isinstance(body, dict) and body.get("ok"):
            return {"ok": True, "version": body.get("version"), "etag": body.get("etag")}
        last = {"status": st, "body": body}
        if st == 409:
            time.sleep(0.4)
            continue
        break
    return {"ok": False, "stage": "memo_patch", "detail": last}


def group_members(base, sid):
    st, body = http("GET", "%s/api/sessions/%s" % (base, sid))
    if st == 200 and isinstance(body, dict):
        s = body.get("session") or {}
        return [m.get("id") if isinstance(m, dict) else m for m in (s.get("members") or [])], s
    return [], {}


def _key(t):
    return re.sub(r"[\s，。；,.;:：、]+", "", str(t))[:60]


def archive_to_group(base, sid, agent_id, doc, timeout=20.0):
    """把精简摘要写回群：群记忆 open_tasks/decisions 追加 + 一条群消息。"""
    members, sess = group_members(base, sid)
    if agent_id not in members:
        return {"ok": False, "stage": "member_check",
                "detail": "X-Agent-Id=%s 不是群 %s 成员（members=%s）" % (agent_id, sid, members)}

    cur = memo_get(base, sid, agent_id)
    old = (cur.get("memo") or {}) if cur.get("ok") else {}
    old_tasks = old.get("open_tasks") or []
    old_keys = {_key(t.get("text") if isinstance(t, dict) else t) for t in old_tasks}
    new_tasks = [t for t in doc["open_tasks"] if _key(t["text"]) not in old_keys][:20]
    old_dec_keys = {_key(t) for t in (old.get("decisions") or [])}
    # decisions 也要去重：否则每次重跑都把同一批决定再追加一遍（实测踩到过 2 条变 4 条）
    new_dec = [_clip(t["text"], 240) for t in doc["decisions"]]
    new_dec = [x for x in new_dec if _key(x) not in old_dec_keys][-10:]
    new_tasks_memo = [{"text": t["text"], "owner": agent_id,
                       "blocked_by": t.get("blocked_by") or [],
                       "src": "ctx-archiver:" + doc["session_id"]} for t in new_tasks]

    prev_resume = (old.get("resume") or "").strip()
    # 去掉历史上自己写进去的归档指针 —— 否则 resume 会层层套娃 "［此前］↩ 上下文归档…［此前］…"
    prev_resume = re.sub(
        r"↩\s*上下文归档[^↩]*?未完成\s*\d+\s*项见\s*open_tasks。?\s*(［此前(?:原话)?］)?",
        "", prev_resume)
    prev_resume = re.sub(r"^(［此前(?:原话)?］\s*)+", "", prev_resume).strip()
    resume = ("↩ 上下文归档 %s：会话 %s 已达 %s tok。现场 %s｜未完成 %d 项见 open_tasks。"
              % (doc["archived_at"], doc["session_id"], doc["context_tokens"],
                 os.path.join(ARCHIVE_ROOT, doc["session_id"], "latest.md"),
                 len(doc["open_tasks"])))
    if prev_resume and prev_resume not in resume:
        resume = (resume + "［此前原话］" + prev_resume)[:4000]

    patch = {"open_tasks": new_tasks_memo, "decisions": new_dec, "resume": resume}
    mw = memo_patch(base, sid, agent_id, patch)

    # 群消息：@all，正文 <= 4000 字
    lines = ["【上下文归档 · 自动】会话 %s 上下文已达 %s tok（阈值 %s），现场已存档。"
             % (doc["session_id"], doc["context_tokens"], doc["threshold"]),
             "存档：%s" % os.path.join(ARCHIVE_ROOT, doc["session_id"], "latest.md")]
    if doc["open_tasks"]:
        lines.append("未完成 %d 项（已写入本群记忆 open_tasks）：" % len(doc["open_tasks"]))
        for i, t in enumerate(doc["open_tasks"][:8], 1):
            bb = "〔卡在：%s〕" % ",".join(t["blocked_by"]) if t.get("blocked_by") else ""
            lines.append("%d. %s%s" % (i, t["text"], bb))
    else:
        lines.append("未扫到显式未完成标记 —— 别当没有，见存档「下一步」。")
    if doc.get("next_step"):
        lines.append("下一步：%s" % doc["next_step"])
    lines.append("恢复：GET /api/sessions/%s/memo（先扫 open_tasks[].blocked_by）。" % sid)
    text = "\n".join(lines)[:TEXT_MAX]

    st, body = http("POST", "%s/api/sessions/%s/messages" % (base, sid),
                    {"text": text, "mentions": ["@all"]}, agent_id=agent_id, timeout=timeout)
    posted = st in (200, 201) and isinstance(body, dict) and body.get("ok")
    return {"ok": bool(mw.get("ok") and posted), "memo_write": mw,
            "message_post": {"ok": posted, "status": st,
                             "seq": ((body or {}).get("message") or {}).get("seq")
                             if isinstance(body, dict) else None,
                             "detail": None if posted else body},
            "text": text}


# ---------------- 压缩事实 ----------------
def compression_facts():
    """如实报告：Hermes 提供了什么、外部能触发什么。"""
    cfg = os.path.join(HERMES_HOME, "config.yaml")
    comp = {}
    try:
        with open(cfg, "r", encoding="utf-8") as f:
            txt = f.read()
        m = re.search(r"^compression:\n((?:[ \t]+.*\n|\n)+)", txt, re.M)
        if m:
            for ln in m.group(1).splitlines():
                mm = re.match(r"\s+([a-z_]+):\s*(.+)$", ln)
                if mm:
                    comp[mm.group(1)] = mm.group(2).strip()
    except Exception as exc:
        comp = {"error": str(exc)}
    return {
        "config_path": cfg,
        "config_compression": comp,
        "in_session_slash": "/compress（别名 /compact；hermes_cli/commands.py:80）—— 只作用于**当前交互会话**",
        "external_trigger_available": False,
        "external_trigger_note": (
            "未找到任何外部可用的压缩/清理入口：CLI 无子命令（hermes_cli/main.py 无 compress parser）、"
            "gateway 控制套接字只答 identify/status（gateway/control_socket.py）、"
            "没有 /api/sessions/{id}/compress 之类 HTTP 端点。"
            "本机实测 0/1：不伪造结果。"),
        "auto_compression_exists": True,
        "auto_compression_note": (
            "Hermes 自身会在超限时压缩/清理：config.yaml compression.proactive_prune_tokens="
            "%s 即为自动剪枝阈值；代码 agent/turn_context_compaction.py + agent/context_compressor.py。"
            "也就是说 120K 之前 Hermes 已经先自己动过手 —— 本脚本的存档是**补充**，不是替代。"
            % comp.get("proactive_prune_tokens", "?")),
    }


# ---------------- 幂等状态 ----------------
def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(st):
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
        os.replace(tmp, STATE_FILE)
    except Exception as exc:
        log("状态落盘失败:", exc)


# ---------------- 单会话处理 ----------------
def handle_one(args, sid, ctx, state):
    th = args.threshold
    cur = ctx.get("last_in") or 0
    if cur < th and not args.force:
        log("跳过（未达阈值）", sid, "last_in=%d < %d" % (cur, th))
        return {"skipped": "below_threshold", "session_id": sid, "last_in": cur}

    prev = state.get(sid) or {}
    if not args.force and prev:
        if cur - int(prev.get("last_in") or 0) < REARCHIVE_DELTA:
            log("跳过（幂等：未显著增长）", sid, "cur=%d prev=%d" % (cur, prev.get("last_in") or 0))
            return {"skipped": "no_delta", "session_id": sid, "last_in": cur}

    meta = session_meta(sid)
    msgs = session_messages(sid)
    scene = extract_scene(sid, msgs)
    log("存档中", sid, "tok=%d" % cur, "消息=%d" % len(msgs),
        "未完成=%d" % len(scene["open_tasks"]), "已完成=%d" % len(scene["done"]),
        "决定=%d" % len(scene["decisions"]))

    gid = args.group or resolve_group(sid)      # 显式 --group 优先，其次 group_map.json
    extra = {"threshold": th, "group": gid, "group_write": None,
             "compression": compression_facts()}
    jp, mp, doc = write_archive(sid, ctx, meta, scene, extra)
    log("已落盘:", jp)
    log("已落盘:", mp)

    if args.dry_run:
        log("--dry-run：跳过群写入")
        return {"archived": [jp, mp], "last_in": cur, "group_write": None}

    if gid and not args.no_group:
        gw = archive_to_group(args.chat_base, gid, args.agent_id, doc,
                              timeout=args.timeout)
        doc["group_write"] = gw
        with open(jp, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=1)
        with open(mp, "w", encoding="utf-8") as f:
            f.write(render_md(doc))
        log("群写入:", "OK" if gw.get("ok") else "失败",
            "memo=%s" % gw.get("memo_write"), "msg=%s" % gw.get("message_post"))
    else:
        log("未指定 --group：只落本机存档，不写群")

    state[sid] = {"last_in": cur, "at": doc["archived_at"], "archive": jp,
                  "open_tasks": len(scene["open_tasks"])}
    save_state(state)
    return {"archived": [jp, mp], "last_in": cur,
            "open_tasks": len(scene["open_tasks"]),
            "group_write": doc.get("group_write")}


# ---------------- main ----------------
def main():
    global ARCHIVE_ROOT
    ap = argparse.ArgumentParser(description="会话上下文 120K 存档器")
    ap.add_argument("--session-id", default="", help="单个 Hermes 会话 id；不给则整体扫描")
    ap.add_argument("--scan", action="store_true", help="扫描 agent.log 里所有超阈值会话（默认行为）")
    ap.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD)
    ap.add_argument("--group", default="", help="chat_svc 群 id（meet_xxx）：写群记忆 + 发群消息")
    ap.add_argument("--chat-base", default=CHAT_BASE_DEFAULT)
    ap.add_argument("--agent-id", default=DEFAULT_AGENT_ID, help="X-Agent-Id 署名（必须是群成员）")
    ap.add_argument("--archive-dir", default=ARCHIVE_ROOT)
    ap.add_argument("--force", action="store_true", help="忽略阈值与幂等去重")
    ap.add_argument("--dry-run", action="store_true", help="只落本机存档，不写群")
    ap.add_argument("--no-group", action="store_true", help="即使给了 --group 也不写群")
    ap.add_argument("--log-file", default="", help="同时写日志文件（计划任务下 pythonw 无 stdout）")
    ap.add_argument("--hermes-home", default="", help="Hermes profile 家目录（SYSTEM 跑计划任务时必须显式给）")
    ap.add_argument("--group-map", default="", help='JSON：{"<session id 或前缀*>":"meet_xxx"}，让 cron 也能把会话写回对应群')
    ap.add_argument("--facts", action="store_true", help="只打印探明事实（压缩入口/群记忆契约）")
    ap.add_argument("--timeout", type=float, default=20.0)
    args = ap.parse_args()

    if args.hermes_home:
        set_hermes_home(args.hermes_home)

    ARCHIVE_ROOT = args.archive_dir
    if args.log_file:
        global LOG_FILE
        LOG_FILE = args.log_file
    gm = args.group_map or os.path.join(HERE, "ctx_group_map.json")
    if os.path.exists(gm):
        try:
            GROUP_MAP.update(json.load(open(gm, encoding="utf-8")) or {})
            log("已加载 group_map:", gm, "条目=%d" % len(GROUP_MAP))
        except Exception as exc:
            log("group_map 读取失败:", exc)

    if args.facts:
        print(json.dumps({"compression": compression_facts(),
                          "chat_base": args.chat_base,
                          "archive_root": ARCHIVE_ROOT,
                          "agent_log": AGENT_LOG,
                          "state_db": STATE_DB},
                         ensure_ascii=False, indent=1))
        return 0

    state = load_state()
    all_ctx = scan_conflicts(args.session_id or None)
    if args.session_id:
        ctx = all_ctx.get(args.session_id) or {}
        if not ctx:
            log("agent.log 里没有该会话的 API 记录：", args.session_id)
            ctx = {}
        return 0 if not handle_one(args, args.session_id, ctx, state) else 0

    hits = sorted([(s, c) for s, c in all_ctx.items() if (c.get("last_in") or 0) >= args.threshold],
                  key=lambda x: -x[1]["last_in"])
    log("扫描完成：%d 个会话有 API 记录，其中 %d 个 >= %d tok"
        % (len(all_ctx), len(hits), args.threshold))
    for sid, c in hits:
        log("· %s last_in=%d max=%d %s" % (sid, c["last_in"], c["max_in"], c["last_ts"]))
    n = 0
    for sid, c in hits:
        r = handle_one(args, sid, c, state)
        if r.get("archived"):
            n += 1
    log("本轮存档 %d 个会话" % n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
