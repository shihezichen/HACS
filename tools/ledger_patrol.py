#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""台账巡检器 LedgerPatrol —— 「完成＝自动汇报」三层机制的第三层（cron 巡检点名）。

设计口径：**照 <USER_ID> 群里 #128/#129/#131 的原口径实现，不自行改设计**。
  一、完成＝落点，不认口头（#129 §一）：每条交付必须留机器可验的落点——
      文件+时间戳 / curl 真跑回显 / commit 号 / 服务重启时间戳；落点不存在就等于没完成。
  二、承诺进台账（#129 §二）：群里口头承诺登进 todos 真源（owner + deadline），
      让承诺从「聊天记录」变成「可轮询的数据」。
  三、cron 巡检点名（#129 §三 / #131 §3）：按小时扫**落点与台账**（不轮询人），
      逾期没落点的在群里 @ 责任人一句「某条逾期、无落点」；巡检脚本自带心跳。
  #131 §1：**agent 不能自盖完成章** —— 台账标了完成却没有落点，就是自己给自己盖章。
  #129 失败模式②：落点口径统一（本文件 §落点识别 就是那份口径的机器化实现）。

四条检查（对应上面原口径）：
  A. 漏报   —— 有落点，却没在群里点名报过（落点之后该 owner 没有面向 <USER_ID> 的汇报）
  B. 欠账   —— my_pending 里欠人的回应超时未回应（#129 §三「@ 责任人」）
  C. 自盖章 —— 台账标了完成态，但既无落点字段、群里也找不到落点证据（#131 §1）
  D. 逾期   —— 台账条目带 deadline 且已过期仍未完成（#131 §3「到点自动冒头，不靠谁记得报」）

只读现有端点 + 只发群消息：不改 chat_svc 后端（另一个任务正在改它，会冲突）。
2026-09-23 修正（群 #360/#366 假欠账）：发现「该报没报」时**先经任务服务（9981）入队**
（kind=reply / 普通任务，owner=被点名 agent，idem_key 幂等，已存在不重复建），**再**在群里点名；
入队失败就不点名 —— 没进队列的点名不算派活，也就不会被记成「对方不回」。
发消息复用 post_group.py 的同一套鉴权与端点（X-Agent-Id + POST /api/sessions/{sid}/messages），
发完照 post_group 的规矩**回读校验**，拿 seq。
幂等：每条发现落盘成 key，上过黑板就不再上（--dry-run 不落盘、不发）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
# 批⑪ 归位：本件已搬入 tools/（T 工具面）。模块级原先把**自身目录**插进 sys.path[0]
# （裸名 import 的遗留，会遮蔽同名 stdlib），改为按同目录既有件（tools/nodelet_gen.py）
# 的写法只注入**仓根**，跨层件一律绝对包名。
PROJECT_ROOT = os.path.dirname(HERE)
for _hacs_p in (PROJECT_ROOT,):
    if _hacs_p not in sys.path:
        sys.path.insert(0, _hacs_p)

from node.l0_host import node_profile as _npf  # noqa: E402  L0 节点档案：宿主事实唯一真源（对话台 / 任务服务基址；env 优先）
# 复用现成的「发回群」出口代码：同一套 http()（X-Agent-Id 鉴权）与选群口径，不另造一套
from node.l1_transport.post_group import http as pg_http, pick_session, DEFAULT_SESSION  # noqa: E402

# 兜底默认值集中到 L0 node_profile（env 优先，变量名与读取顺序一字未改）
CHAT_BASE_DEFAULT = os.environ.get("AGENT_TEAM_CHAT_BASE") or os.environ.get("CHAT_BASE") or _npf.chat_base_default()
# 任务服务（9981）：催办的唯一入口。巡检发现「该报没报」时，**必须先在这里建任务**，
# 再在群里点名 —— 没进队列的点名不算派活（2026-09-23 群 #360/#366 假欠账的根因修正）。
TASK_BASE_DEFAULT = os.environ.get("AGENT_TEAM_TASK_BASE") or os.environ.get("TASK_BASE") or _npf.task_base_default()
STATE_DEFAULT = os.path.join(HERE, "state", "ledger_patrol_state.json")
LOG_DEFAULT = os.path.join(HERE, "ledger_patrol.log")

# 需要「被点名的人自己作答」的发现：漏报 / 欠账 → 入队 kind=reply（本人作答，禁程序代写）。
# 无落点 / 逾期 是「交落点」而不是「回话」，入队为普通任务（kind 留空）。
REPLY_KINDS = ("漏报", "欠账")
REPLY_LEASE_S = 120
# reply 字段照群 #356/#357 定死那版，不自造：
# kind=reply / idem_key(唯一) / origin_task_id / context_digest(≤800token) / groups /
# owner=被点名 agent / lease_s=120 / fallback=L1,L2,L3。
REPLY_FALLBACK = {"L1": "重派1次", "L2": "事实性降级通知（仍禁止代写正文）", "L3": "告警"}

HUMAN_IDS = tuple(x.strip() for x in (os.environ.get("AGENT_TEAM_HUMAN_IDS") or "<human-id>").split(",") if x.strip())
# 运行期文案里要 @ 的那个人的**昵称**（刀36 原是真值字面量）。批⑪ 裁定⑥：env 化 + 占位符兜底
# —— 线上文案不变（部署面由 _boot.py 注入 AGENT_TEAM_HUMAN_NICK），仓内零真值。
HUMAN_NICK = os.environ.get("AGENT_TEAM_HUMAN_NICK") or "<human-nick>"
# 本巡检器自己发的消息带这个前缀 —— 解析群消息时一律跳过，免得自己点自己
SENTINEL = "【台账巡检】"
TZ = timezone(timedelta(hours=8))

DONE_STATUSES = {"done", "done-待验收", "completed", "已完成", "closed", "完成"}
OPEN_STATUSES = {"", "pending", "in-progress", "doing", "todo", "open", "blocked", "进行中", "待办"}

# ---------------- #129 失败模式②：落点口径（什么算机器可验落点） ----------------
LANDING_PATTERNS = [
    ("commit", re.compile(r"\bcommit\s*[:：号]?\s*([0-9a-f]{7,40})\b", re.I)),
    ("restart", re.compile(r"(服务重启时间戳|重启时间戳|重启生效|restarted at|uptime_s)")),
    ("curl", re.compile(r"curl[^\n]{0,80}?(回显|→|->|\bHTTP/\d|\b20\d\b|\b501\b)")),
    # 2026-09-22 实测补：真跑了接口但正文里没写字面 "curl"（例如
    # `POST /api/sessions/{sid}/members {"members":["x"]} → 200`）也是机器可验落点。
    # 防误判：必须「方法+以 / 开头的路径」与「状态码」在同一行 120 字符内同时出现；
    # 只提接口名、只说「已上线」不算（见 README §落点识别）。
    ("http", re.compile(r"(?i)\b((?:GET|POST|PUT|PATCH|DELETE)\s+/[\w./{}$%:+-]+)"
                        r"[^\n]{0,120}?(?:(?:→|->|=>|=|:)\s*(?:HTTP/[\d.]+\s+)?|HTTP/[\d.]+\s+)"
                        r"(20\d|4\d\d)\b")),
    ("file", re.compile(r"([A-Za-z]:\\[^\s，,。；;）)】\]]+\.(?:py|js|ts|tsx|json|md|ps1|sh|html|css|yaml|yml)"
                        r"|\b[\w.-]+/[\w./-]+\.(?:py|js|ts|tsx|json|md|ps1|sh|html|css|yaml|yml))"
                        r"[^\n]{0,24}?(时间戳|20\d\d-\d\d-\d\d|\d{2}:\d{2})")),
    ("verify", re.compile(r"(回读校验通过|已在群里看到自己的消息\s*seq=)")),
]

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


# ---------------- 网络：容忍 chat_svc 短暂不可用（并行任务会重启它） ----------------
def http_retry(method, url, body=None, agent_id=None, attempts=4, timeout=20.0):
    last = None
    for i in range(attempts):
        r = pg_http(method, url, body=body, agent_id=agent_id, timeout=timeout)
        if r is not None and not (isinstance(r, dict) and r.get("error")):
            return r
        last = r
        code = ""
        if isinstance(r, dict):
            code = str(((r.get("error") or {}) if isinstance(r.get("error"), dict) else {}).get("code") or "")
        # 4xx（除 5xx/网络）不必重试
        if code.startswith("http_4"):
            return r
        if i < attempts - 1:
            time.sleep(1.5 * (2 ** i))
    return last


# ---------------- 状态（幂等 + 心跳） ----------------
class State(object):
    def __init__(self, path, escalate_after=21600.0, max_escalations=3):
        self.path = path
        # finding key -> {"first": ts, "last": ts, "count": n}
        # （老格式里 value 是裸整数时间戳，load() 会平滑升级，不丢记录）
        self.posted = {}
        self.heartbeat = {}
        self.last_post = {}     # sid -> ts（限流）
        # todos 真源回填的幂等账：todosync|<sid>|<台账条目id> -> 真源里的 todo_id
        self.todos_synced = {}
        # 催办入队的幂等账：taskdispatch|<sid>|<发现key> -> 任务服务里的 task_id
        self.task_dispatched = {}
        self.escalate_after = float(escalate_after)
        self.max_escalations = int(max_escalations)
        self._capped_logged = set()
        self.load()

    @staticmethod
    def upgrade_posted(raw):
        """向后兼容：老格式 {key: 1758000000} → {key: {first, last, count:1}}。"""
        out = {}
        for k, v in (raw or {}).items():
            if isinstance(v, dict):
                first = int(v.get("first") or v.get("last") or 0)
                last = int(v.get("last") or first)
                out[k] = {"first": first, "last": last,
                          "count": max(1, int(v.get("count") or 1))}
            else:
                try:
                    ts = int(v)
                except Exception:
                    ts = 0
                out[k] = {"first": ts, "last": ts, "count": 1}
        return out

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                d = json.load(f)
            self.posted = self.upgrade_posted(d.get("posted") or {})
            self.heartbeat = d.get("heartbeat") or {}
            self.last_post = d.get("last_post") or {}
            self.todos_synced = d.get("todos_synced") or {}
            self.task_dispatched = d.get("task_dispatched") or {}
        except Exception:
            self.posted, self.heartbeat, self.last_post = {}, {}, {}
            self.todos_synced = {}
            self.task_dispatched = {}

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"posted": self.posted, "heartbeat": self.heartbeat,
                           "last_post": self.last_post,
                           "todos_synced": self.todos_synced,
                           "task_dispatched": self.task_dispatched},
                          f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except Exception as exc:
            log("状态落盘失败:", exc)

    def seen(self, key):
        return key in self.posted

    def attempt_for(self, key):
        """#129 §三「不听解释」：不再一票永久去重，改成带升级的提醒。

        返回「本轮该发第几次提醒」；返回 None 表示本轮不发。
          · 从没发过                                   → 1（首次提醒）
          · 距上次提醒不足 --escalate-after            → None（冷却中，静默）
          · 超过 --escalate-after 且本轮仍被发现        → count+1（升级重提）
          · 累计已达 --max-escalations                 → None，log 一行「已达升级上限，转静默」
        能走到这个函数说明该 key 本轮又被检查命中了，即问题仍未闭环。
        """
        rec = self.posted.get(key)
        if not rec:
            return 1
        count = int(rec.get("count") or 1)
        if count >= self.max_escalations:
            if key not in self._capped_logged:
                self._capped_logged.add(key)
                log("  已达升级上限（%d/%d 次），转静默 | key=%s"
                    % (count, self.max_escalations, key))
            return None
        if (time.time() - float(rec.get("last") or 0)) < self.escalate_after:
            return None
        return count + 1

    def mark(self, key):
        ts = int(time.time())
        rec = self.posted.get(key)
        if not isinstance(rec, dict):
            self.posted[key] = {"first": ts, "last": ts, "count": 1}
        else:
            rec["last"] = ts
            rec["count"] = int(rec.get("count") or 0) + 1

    def gap_ok(self, sid, gap_s):
        return (time.time() - float(self.last_post.get(sid) or 0)) >= gap_s

    def touch(self, sid):
        self.last_post[sid] = int(time.time())

    def beat(self, err=""):
        self.heartbeat = {"last_ok": int(time.time()),
                          "last_ok_iso": datetime.now(TZ).isoformat(timespec="seconds"),
                          "rounds": int(self.heartbeat.get("rounds") or 0) + 1,
                          "last_error": err}


# ---------------- 时间 ----------------
def parse_ts(s):
    if not s:
        return None
    try:
        t = s.replace("Z", "+00:00")
        d = datetime.fromisoformat(t)
        if d.tzinfo is None:
            d = d.replace(tzinfo=TZ)
        return d
    except Exception:
        return None


def now():
    return datetime.now(TZ)


# ---------------- 落点识别 ----------------
def find_landings(text):
    hits = []
    for kind, rx in LANDING_PATTERNS:
        m = rx.search(text or "")
        if m:
            tok = m.group(1) if (m.groups() and m.group(1)) else m.group(0)
            hits.append((kind, (tok or "")[:60]))
    return hits


def scan_group(base, sid, agent_id, limit=200):
    """拉群消息（一次），返回 messages + server_time。

    2026-09-22 实测踩到：chat_svc 所在机器的时钟比本机慢约 2.5 分钟
    （本机 12:52:45 发的消息，回来 created_at=12:50:17）。所以"多少秒算超时"
    一律拿服务端 server_time 当基准，不用本机钟，否则 --report-window / --pending-age
    会被时钟差整个吃掉。
    """
    d = http_retry("GET", "%s/api/sessions/%s/messages?limit=%d" % (base, sid, limit),
                   agent_id=agent_id)
    if not isinstance(d, dict) or d.get("error"):
        raise RuntimeError("拉群消息失败: %s" % json.dumps(d, ensure_ascii=False)[:200])
    return d


def collect(messages):
    """把群消息整理成：落点表 / 汇报表（按 owner）。跳过巡检器自己的发言。

    引用去重（2026-09-22 自测实测踩到）：A 把 B 的落点**引用**进自己正文，会被误判成
    A 也交了落点（实测：hermes #12 留落点，openclaw-nb #13 复述了一遍，巡检器就报了
    openclaw-nb「漏报」）。落点只认**最早出现**的那条：同一个 (kind, token) 只记给第一次说的人。
    """
    landings, reports = {}, {}
    claimed = {}                      # (kind, token) -> (seq, owner) 首次声明者
    for m in messages:
        au = m.get("author") or {}
        owner = au.get("id")
        if owner in HUMAN_IDS or (au.get("type") == "human"):
            continue
        text = m.get("text") or ""
        if SENTINEL in text:
            continue                     # 自己发的不算
        seq = m.get("seq")
        ts = parse_ts(m.get("created_at"))
        men = ((m.get("meta") or {}).get("mentions")) or []
        aud = ((m.get("meta") or {}).get("audience")) or []
        directed_to_human = bool((set(men) | set(aud)) & set(HUMAN_IDS))
        if "@所有人" in text or "@all" in men:
            directed_to_human = True
        rec = {"seq": seq, "ts": ts, "text": text, "owner": owner}
        for kind, tok in find_landings(text):
            first = claimed.get((kind, tok))
            if first is None:
                claimed[(kind, tok)] = (seq or 0, owner)
            elif (seq or 0) > first[0] or owner != first[1]:
                continue                 # 复述别人的落点，不算自己的
            landings.setdefault(owner, []).append(dict(rec, kind=kind, token=tok))
        if directed_to_human:
            reports.setdefault(owner, []).append(rec)
    for k in landings:
        landings[k].sort(key=lambda r: (r["ts"] or datetime.min.replace(tzinfo=TZ), r["seq"] or 0))
    for k in reports:
        reports[k].sort(key=lambda r: (r["ts"] or datetime.min.replace(tzinfo=TZ), r["seq"] or 0))
    return landings, reports


# ---------------- 台账读取（**GET /todos 是唯一真源**，退化仅限真源为空） ---------------- #
# 「巡检器切 todos 真源」(#191 第 1 项，#131 §2)：真源只要 count>0 就**自动接管**，
# 不再看 memo（memo 只在真源为空时当退化台账用，且日志里会把 src 写出来，可核）。
def load_memo_tasks(base, sid, agent_id):
    m = http_retry("GET", "%s/api/sessions/%s/memo" % (base, sid), agent_id=agent_id, attempts=2)
    memo = (m or {}).get("memo") or {}
    return (memo.get("open_tasks") or []), "memo.open_tasks"


def todos_items(d):
    """真源的条目全集：以 `items[]` 为准，`groups[]` 只用来兜底补漏。

    `groups[]` 是**按会话成员分组**的视图，owner 不在名册里的条目不会出现在任何一个组里；
    若只从 groups 拼台账，那类条目就会从巡检视野里静默消失。所以这里 items[] 优先、
    groups 只作补充，再按条目 id 去重（同一 id 只留一份，顺序保持 items[] 在前）。
    """
    out, seen = [], set()
    rows = list(d.get("items") or [])
    for g in d.get("groups") or []:
        rows.extend(g.get("items") or [])
    for i, it in enumerate(rows):
        if not isinstance(it, dict):
            continue
        k = str(it.get("id") or it.get("todo_id") or "")
        if k and k in seen:
            continue
        if k:
            seen.add(k)
        out.append(it)
    return out


def load_ledger(base, sid, agent_id):
    items, src = [], None
    d = http_retry("GET", "%s/api/sessions/%s/todos" % (base, sid), agent_id=agent_id, attempts=2)
    if isinstance(d, dict) and d.get("ok") and (d.get("count") or 0) > 0:
        items = todos_items(d)
        if items:
            src = "todos"
    if not items:
        items, src = load_memo_tasks(base, sid, agent_id)
        if not items:
            src = None
    return items, src


# ---------------- 真源回填（#191 第 1 项配套：真源为空时把台账登记进去） ----------------
def existing_keys(base, sid, agent_id):
    """真源里已有的 (owner, text) 与全部 todo_id（回填去重用）。"""
    d = http_retry("GET", "%s/api/sessions/%s/todos" % (base, sid), agent_id=agent_id, attempts=2)
    pairs, ids = set(), set()
    if not isinstance(d, dict):
        return pairs, ids
    items = [it for g in (d.get("groups") or []) for it in (g.get("items") or [])]
    items = items or (d.get("items") or [])
    for it in items:
        ids.add(str(it.get("id") or ""))
        pairs.add((str(it.get("owner") or ""), str(it.get("text") or "").strip()))
    return pairs, ids


def sync_todos(base, sid, agent_id, st, dry_run=False):
    """把台账里**属于自己的未完成条目**登记进 todos 真源（#131 §2「承诺进台账」）。

    「巡检器切 todos 真源」的写侧：读侧早就 todos 优先（count>0 自动接管），但主群真源一直是
    空的 —— 空真源等于前端「待办两视图」没数据。这里把台账（真源 ∪ memo.open_tasks）里属于
    自己的未完成条目登记进去，登记完 count>0，读侧即自动接管。

    幂等三道：① state 里 `todosync|<sid>|<条目id>` 记 todo_id；② 真源已有同 (owner,text) 跳过；
    ③ 完成态条目不登记（不在真源里替人写 done）。**后端只许给自己加条目**（替别人加 403
    not_your_field），别人的条目只报不写 —— 回归属人自己登记。
    """
    items, src = load_ledger(base, sid, agent_id)
    memo_items, _ = load_memo_tasks(base, sid, agent_id)
    merged, seen = [], set()
    for it in list(items) + list(memo_items):
        k = item_id(it, len(merged))
        if k in seen:
            continue
        seen.add(k)
        merged.append(it)
    pairs, ids = existing_keys(base, sid, agent_id)
    created, others, skipped_done, failed = [], [], [], []
    for it in merged:
        iid = item_id(it, 0)
        owner = item_owner(it) or agent_id
        what = item_what(it).strip()
        if not what:
            continue
        if item_status(it) in DONE_STATUSES:
            skipped_done.append((iid, owner))
            continue
        if owner != agent_id:
            others.append((iid, owner, what[:60]))
            continue
        key = "todosync|%s|%s" % (sid, iid)
        if st.todos_synced.get(key) in ids or (owner, what) in pairs:
            continue
        kind = str(it.get("kind") or "").strip()
        body = {"text": what[:400], "owner": owner,
                "kind": kind if kind in ("confirm", "review", "decide") else "confirm"}
        dl = item_deadline(it)
        if dl is not None:
            body["deadline"] = dl.isoformat(timespec="seconds")
        if dry_run:
            created.append((iid, body["text"][:40], "(dry-run)"))
            continue
        r = http_retry("POST", "%s/api/sessions/%s/todos" % (base, sid), body=body,
                       agent_id=agent_id, attempts=2)
        item = ((r or {}).get("item") or {}) if isinstance(r, dict) else {}
        if item.get("id"):
            st.todos_synced[key] = item["id"]
            ids.add(item["id"])
            pairs.add((owner, what))
            created.append((iid, str(item.get("text") or "")[:40], item["id"]))
            log("  真源登记 ✓ %s → todo_id=%s status=%s" % (iid, item["id"], item.get("status")))
        else:
            code = ((r or {}).get("error") or {}).get("code") or "?"
            failed.append((iid, code))
            log("  × 真源登记失败 %s：%s" % (iid, json.dumps(r, ensure_ascii=False)[:160]))
    if created and not dry_run:
        st.save()
    return {"src": src, "created": created, "others": others,
            "skipped_done": skipped_done, "failed": failed}


def item_id(it, i):
    return str(it.get("id") or it.get("task_id") or ("item%d" % i))


def item_owner(it):
    o = it.get("owner") or ""
    return o if isinstance(o, str) else str(o)


def item_status(it):
    return str(it.get("status") or "").strip().lower()


def item_what(it):
    return str(it.get("what") or it.get("title") or it.get("note") or "")


def item_landing(it):
    """台账里的落点字段（#131 §2：落点类型 + 值 + 校验时间），后端还没落，认几种可能字段名。"""
    for k in ("landing", "landings", "evidence", "artifact", "proof", "落点"):
        v = it.get(k)
        if v:
            return v
    return None


def item_deadline(it):
    for k in ("deadline", "due", "due_at", "eta"):
        if it.get(k):
            return parse_ts(str(it[k]))
    return None


def landing_covers(landings, owner, it):
    """群里有没有该 owner 关于这条台账的落点证据。

    判据从严（#131 §1「不能自盖完成章」）：必须是**点名到条目**的落点 ——
    正文里出现条目 id（如 T2），或条目 what 里的**特征词**（长度≥6 的中文短语 /
    长度≥5 且不在通用词表里的 ASCII 词）。通用 HTTP/路径词一律不算证据。
    """
    iid = item_id(it, 0)
    what = item_what(it)
    generic = {"api", "http", "https", "post", "delete", "patch", "sessions", "members",
               "memo", "todos", "curl", "json", "sid", "text", "restart", "commit",
               "write", "read", "list", "items", "group", "chat", "agent", "owner"}
    keys = [iid]
    for t in re.split(r"[\s、,，:：/()（）{}\[\]<>\"']+", what):
        t = t.strip()
        if not t:
            continue
        if re.fullmatch(r"[\u4e00-\u9fff]+", t):
            if len(t) >= 6:
                keys.append(t)
        elif len(t) >= 5 and t.lower() not in generic:
            keys.append(t)
    for r in landings.get(owner) or []:
        text = r["text"] or ""
        if any(k and k in text for k in keys):
            return r
    return None


# ---------------- 四条检查 ----------------
def check_missreport(sid, landings, reports, args, st, srv_now):
    """A. 有落点但没在群里报过（#129 §一 + 上门追责口径）。"""
    out = []
    horizon = srv_now - timedelta(seconds=args.lookback)
    for owner, ls in landings.items():
        cand = [r for r in ls if r["ts"] and r["ts"] >= horizon]
        if not cand:
            continue
        L = cand[-1]                       # 只看最新那条落点
        if (srv_now - L["ts"]).total_seconds() < args.report_window:
            continue                        # 刚落点，给点时间让人自己报
        rep = [r for r in reports.get(owner, [])
               if (r["seq"] or 0) > (L["seq"] or 0)]
        if rep:
            continue
        key = "missreport|%s|%s|%s" % (sid, owner, L["seq"])
        att = st.attempt_for(key)
        if not att:
            continue
        out.append(dict(key=key, attempt=att, kind="漏报", owner=owner,
                        detail="群消息 #%s 有落点（%s: %s），但之后群里没有 %s 面向%s的汇报"
                               % (L["seq"], L["kind"], L["token"], owner, HUMAN_NICK)))
    return out


def check_pending(base, sid, members, args, st, srv_now):
    """B. 欠人回应未回应超时（#129 §三：@ 责任人）。"""
    out = []
    for member in members:
        if member in HUMAN_IDS or member == "@all":
            continue
        d = http_retry("GET", "%s/api/sessions/%s/messages?limit=80" % (base, sid),
                       agent_id=member, attempts=2)
        if not isinstance(d, dict) or d.get("error"):
            log("  取 %s 的待回应失败(跳过本轮):" % member, json.dumps(d, ensure_ascii=False)[:120])
            continue
        byseq = {m.get("seq"): m for m in (d.get("messages") or [])}
        for p in d.get("my_pending") or []:
            seq = p.get("seq")
            src = byseq.get(seq) or {}
            if SENTINEL in (src.get("text") or ""):
                # 巡检自己发的点名帖，不算「别人欠我的回应」——那条债已经由入队任务承载，
                # 再拿它当欠账就是 2026-09-23 #360→#366 那条假欠账的老路（自己点自己）。
                continue
            ts = parse_ts(src.get("created_at"))
            if not ts:
                continue
            age = (srv_now - ts).total_seconds()
            if age < args.pending_age:
                continue
            key = "pending|%s|%s|%s" % (sid, member, seq)
            att = st.attempt_for(key)
            if not att:
                continue
            out.append(dict(key=key, attempt=att, kind="欠账", owner=member,
                            detail="欠 %s 的回应：#%s 已挂 %.0f 分钟没回（欠账超时 %.0f 分钟）"
                                   % (p.get("from"), seq, age / 60.0, args.pending_age / 60.0)))
    return out


def check_noproof(sid, items, src, landings, st):
    """C. 台账标了完成但没有落点（#131 §1：agent 不能自盖完成章）。"""
    out = []
    for i, it in enumerate(items):
        owner = item_owner(it)
        if item_status(it) not in DONE_STATUSES:
            continue
        if item_landing(it):
            continue                        # 台账自带落点字段（后端落地后即生效）
        ev = landing_covers(landings, owner, it)
        if ev:
            continue                        # 群里找得到该条的落点证据，算合规
        iid = item_id(it, i)
        key = "noproof|%s|%s|%s" % (sid, iid, item_status(it))
        att = st.attempt_for(key)
        if not att:
            continue
        out.append(dict(key=key, attempt=att, kind="无落点", owner=owner,
                        detail="台账(%s)条目 %s 标了「%s」但既无落点字段、群里也找不到落点——"
                               "按 #131 §1 不能自盖完成章，请交落点或改回 open"
                               % ((src or "?"), iid, it.get("status"))))
    return out


def check_overdue(sid, items, src, st, srv_now):
    """D. deadline 到点自动冒头（#131 §3）。"""
    out = []
    for i, it in enumerate(items):
        dl = item_deadline(it)
        if not dl or dl > srv_now:
            continue
        if item_status(it) in DONE_STATUSES:
            continue
        iid = item_id(it, i)
        key = "overdue|%s|%s|%s" % (sid, iid, dl.isoformat())
        att = st.attempt_for(key)
        if not att:
            continue
        out.append(dict(key=key, attempt=att, kind="逾期", owner=item_owner(it),
                        detail="台账(%s)条目 %s 已过 deadline %s 仍无落点（status=%s）——逾期、无落点"
                               % ((src or "?"), iid, dl.isoformat(), it.get("status") or "open")))
    return out


# ---------------- 催办先入队（#129 §三 修正：点名之前，先把账变成任务） ----------------
# 2026-09-23 群 #360/#366 假欠账的根因：巡检只「群里点名」、不「进队列派活」，
# 被点名的人在自己队列里看不到这条账（实测 GET /tasks/next?agent=openclaw-nb → empty_queue），
# 于是「点过名」被记成「对方不回」。现在改为：发现即入队（kind=reply/普通任务）→ 再点名；
# 同一条发现重复巡检不重复建（本地 state + 服务端 idem_key 唯一索引双保险）。
def task_post(base, body, attempts=3, timeout=20.0):
    """POST /tasks 建任务。

    task_svc 的错误体是 {"ok": false, "error": "<code>"}，与 post_group.http 的
    {"error": {"code": ...}} 不同 —— 这里各认各的，别把 4xx 当网络故障重试。
    """
    last = None
    for i in range(attempts):
        r = pg_http("POST", "%s/tasks" % base, body=body, timeout=timeout)
        if isinstance(r, dict) and r.get("ok"):
            return r
        last = r
        err = (r or {}).get("error") if isinstance(r, dict) else None
        if isinstance(err, str) and err in ("group_and_title_required", "bad_priority",
                                           "bad_status", "bad_json"):
            return r                      # 口径错误，重试无意义
        if i < attempts - 1:
            time.sleep(1.5 * (2 ** i))
    return last


def finding_idem_key(sid, f):
    """一条发现一个幂等键：同一条账重复巡检不会重复建任务。"""
    ns = "patrol-reply" if f.get("kind") in REPLY_KINDS else "patrol"
    return "%s:%s:%s" % (ns, sid, f["key"])


def finding_task_body(sid, f, agent_id):
    """把一条发现翻译成任务（字段照 #356/#357 定死那版，不自造）。

    * 漏报 / 欠账 → kind=reply（本人作答）；无落点 / 逾期 → 普通任务（要落点，不是回话）；
    * **content 恒为 None**：正文只能由 Agent 自己写（禁止程序代写），
      巡检只给 title（事实性描述）与 context_digest（≤800token 的上下文摘要）。
    """
    owner = f.get("owner") or ""
    body = {"group_id": sid, "title": ("【巡检催办·%s】%s：%s"
                                      % (f.get("kind"), owner, f.get("detail") or ""))[:200],
            "content": None, "owner": owner, "priority": "normal", "proposer": agent_id,
            "idem_key": finding_idem_key(sid, f),
            "context_digest": (f.get("detail") or "")[:1200],
            "groups": sid}
    if f.get("kind") in REPLY_KINDS:
        body.update({"kind": "reply", "origin_task_id": None,
                     "lease_s": REPLY_LEASE_S,
                     "fallback": json.dumps(REPLY_FALLBACK, ensure_ascii=False)})
    return body


def dispatch_findings(task_base, sid, findings, agent_id, st, dry_run=False):
    """**点名之前**把每条发现以任务形式入队（存在则不重复建）。

    返回 (rows, enq)：rows = [(finding, task_id|None, created|already_exists|state|dry-run|failed)]，
    enq = {finding key: task_id}（只有真入队成功的才在 enq 里 —— 没入队的**不许点名**）。
    """
    rows, enq = [], {}
    for f in findings:
        key = "taskdispatch|%s|%s" % (sid, f["key"])
        prev = st.task_dispatched.get(key)
        if prev:
            f["task_id"] = prev
            enq[f["key"]] = prev
            rows.append((f, prev, "state"))
            continue
        body = finding_task_body(sid, f, agent_id)
        if dry_run:
            log("  [dry-run] 催办入队 → owner=%s kind=%s idem_key=%s"
                % (f.get("owner"), body.get("kind") or "-", body.get("idem_key")))
            rows.append((f, None, "dry-run"))
            continue
        r = task_post(task_base, body)
        t = ((r or {}).get("task") or {}) if isinstance(r, dict) else {}
        tid = t.get("id")
        if tid:
            st.task_dispatched[key] = tid
            f["task_id"] = tid
            enq[f["key"]] = tid
            src = "already_exists" if (r or {}).get("already_exists") else "created"
            rows.append((f, tid, src))
            log("  催办入队 ✓ owner=%s task=%s(%s) idem_key=%s"
                % (f.get("owner"), tid, src, body.get("idem_key")))
        else:
            rows.append((f, None, "failed"))
            log("  × 催办入队失败 owner=%s idem_key=%s：%s"
                % (f.get("owner"), body.get("idem_key"), json.dumps(r, ensure_ascii=False)[:200]))
    if not dry_run and any(t for _, t, _ in rows):
        st.save()
    return rows, enq


# ---------------- 发回群（复用 post_group 的端点 + 回读校验） ----------------
def compose(sid, findings, src, cc_human):
    top = max([int(f.get("attempt") or 1) for f in findings] or [1])
    head = "%s 台账巡检（自动 · 只认落点，不认自述）" % SENTINEL
    if top >= 2:
        head += " · 第 %d 次提醒（仍未闭环）" % top
    lines = ["%s：发现 %d 条该报没报／无落点的账。" % (head, len(findings))]
    if src:
        lines.append("台账源：%s" % src)
    lines.append("")
    for n, f in enumerate(findings, 1):
        att = int(f.get("attempt") or 1)
        tag = "（第 %d 次提醒，仍未闭环）" % att if att >= 2 else ""
        tid = f.get("task_id")
        lines.append("%d) [%s]%s %s：%s%s"
                     % (n, f["kind"], tag, f["owner"], f["detail"],
                        ("（已入队 %s，未回应请查队列）" % tid) if tid else ""))
    lines.append("")
    names = []
    for f in findings:
        if f["owner"] and f["owner"] not in names:
            names.append(f["owner"])
    if cc_human:
        for h in HUMAN_IDS:
            if h not in names:
                names.append(h)
    lines.append("点名：" + " ".join("@" + n for n in names))
    return "\n".join(lines), names


def post_and_verify(base, sid, agent_id, text, mentions):
    posted = http_retry("POST", "%s/api/sessions/%s/messages" % (base, sid),
                        {"text": text, "mentions": mentions}, agent_id=agent_id, attempts=2)
    ok = isinstance(posted, dict) and posted.get("ok", True) and not posted.get("error")
    if not ok:
        return None, posted
    # 回读校验（同 post_group 口径）：确认这条真在群里，并取它的 seq
    d = http_retry("GET", "%s/api/sessions/%s/messages?limit=10" % (base, sid),
                   agent_id=agent_id, attempts=2)
    tail = (d or {}).get("messages") or []
    head = text.strip()[:40]
    hit = [m for m in tail
           if (m.get("author") or {}).get("id") == agent_id
           and (m.get("text") or "").strip()[:40] == head]
    return (hit[-1].get("seq") if hit else "未回读到"), posted


# ---------------- 单轮 ----------------
def one_round(args, st):
    base = args.chat_base
    sid = pick_session(base, args.session)
    if not sid:
        log("拿不到任何 open 的临时群，本轮跳过")
        return 0

    g = http_retry("GET", "%s/api/sessions?limit=100" % base, attempts=2)
    members = []
    for s in ((g or {}).get("sessions") or []):
        if s.get("session_id") == sid:
            for m in s.get("members") or []:
                mid = m.get("id") if isinstance(m, dict) else m
                if mid:
                    members.append(mid)
    if not members:
        members = [args.agent_id]

    d = scan_group(base, sid, args.agent_id, limit=args.limit)
    msgs = d.get("messages") or []
    srv_now = parse_ts(d.get("server_time")) or now()
    landings, reports = collect(msgs)
    items, src = load_ledger(base, sid, args.agent_id)

    log("巡检 · 群=%s · 消息=%d 条 · 落点 owner=%s · 台账源=%s(%d 条) · 服务端时间=%s"
        % (sid, len(msgs), ",".join(sorted(landings)) or "-", src or "无", len(items),
           srv_now.isoformat(timespec="seconds")))

    findings = []
    findings += check_missreport(sid, landings, reports, args, st, srv_now)
    findings += check_pending(base, sid, members, args, st, srv_now)
    findings += check_noproof(sid, items, src, landings, st)
    findings += check_overdue(sid, items, src, st, srv_now)

    if not findings:
        log("  无异常（安静，不发群）")
        return 0

    for f in findings:
        log("  发现 [%s] %s | 第 %d 次提醒 | key=%s | %s"
            % (f["kind"], f["owner"], int(f.get("attempt") or 1), f["key"], f["detail"]))

    if args.dry_run:
        if not args.no_task_enqueue:
            dispatch_findings(args.task_base, sid, findings, args.agent_id, st, dry_run=True)
        log("  --dry-run：只打印不发群、不落盘（催办入队也只在上面打印）")
        return len(findings)

    # #129 §三 修正（2026-09-23）：先入队，再点名 —— 没进队列的「催办」不算派活。
    if args.no_task_enqueue:
        log("  --no-task-enqueue：跳过催办入队（仅调试用；此时点名不进队列）")
    else:
        _, enq = dispatch_findings(args.task_base, sid, findings, args.agent_id, st)
        blocked = [f for f in findings if not enq.get(f["key"])]
        if blocked:
            log("  以下发现未能入队，本轮**不点名**（宁可不点名，也不造「没进队列的欠账」）：%s"
                % ", ".join("%s/%s" % (f.get("kind"), f.get("owner")) for f in blocked))
        findings = [f for f in findings if enq.get(f["key"])]
        if not findings:
            log("  全部未能入队，本轮不发群")
            return 0

    if not st.gap_ok(sid, args.min_gap):
        log("  距上次发帖不足 %.0fs，本轮不发（下轮再来）" % args.min_gap)
        return len(findings)

    batch = findings[:args.max_posts]
    text, mentions = compose(sid, batch, src, not args.no_cc_human)
    seq, raw = post_and_verify(base, sid, args.agent_id, text, mentions)
    if seq is None:
        log("  × 发群失败（不落盘，下轮重试）:", json.dumps(raw, ensure_ascii=False)[:200])
        return len(findings)
    log("  ✓ 已发回群 seq=%s mentions=%s" % (seq, ",".join(mentions)))
    for f in batch:
        st.mark(f["key"])
    st.touch(sid)
    st.save()
    if len(findings) > len(batch):
        log("  本轮只发前 %d 条，余 %d 条下轮再报（防刷屏）" % (len(batch), len(findings) - len(batch)))
    return len(findings)


def main():
    ap = argparse.ArgumentParser(description="台账巡检器：#128 三层机制的第三层（cron 巡检点名）")
    ap.add_argument("--chat-base", default=CHAT_BASE_DEFAULT)
    ap.add_argument("--task-base", default=TASK_BASE_DEFAULT,
                    help="任务服务 base（默认 %s）；巡检发现必须先在这里入队，再点名" % TASK_BASE_DEFAULT)
    ap.add_argument("--no-task-enqueue", action="store_true",
                    help="跳过「催办先入队」（仅调试用：此时点名不进队列，会退回 2026-09-23 前的旧行为）")
    ap.add_argument("--session", default=os.environ.get("CHAT_SESSION") or "",
                    help="群 session_id；不填则取默认群（同 post_group 口径）")
    ap.add_argument("--as", dest="agent_id", default=os.environ.get("CHAT_AGENT") or "hermes")
    ap.add_argument("--state", default=STATE_DEFAULT)
    ap.add_argument("--log-file", default=LOG_DEFAULT)
    ap.add_argument("--interval", type=float, default=3600.0, help="循环巡检间隔秒（默认按小时）")
    ap.add_argument("--limit", type=int, default=200, help="每轮拉多少条群消息")
    ap.add_argument("--lookback", type=float, default=86400.0, help="只看最近多少秒内的落点")
    ap.add_argument("--report-window", type=float, default=900.0,
                    help="落点之后给责任人多少秒自己报，超时才判漏报")
    ap.add_argument("--pending-age", type=float, default=1800.0, help="欠回应超多少秒算欠账")
    ap.add_argument("--min-gap", type=float, default=600.0, help="同一群两次发帖最小间隔秒（防刷屏）")
    ap.add_argument("--max-posts", type=int, default=3, help="单轮最多点名几条发现")
    ap.add_argument("--escalate-after", type=float, default=21600.0,
                    help="同一条发现距上次提醒超过多少秒、且本轮仍被发现，就再提醒一次（默认 6 小时）")
    ap.add_argument("--max-escalations", type=int, default=3,
                    help="同一条发现累计提醒几次后转静默（默认 3）")
    ap.add_argument("--no-cc-human", action="store_true", help="点名时不顺带 @ %s（默认 @）" % HUMAN_NICK)
    ap.add_argument("--dry-run", action="store_true", help="只打印发现，不发群、不落盘")
    ap.add_argument("--sync-todos", action="store_true",
                    help="一次性入口：把台账里属于自己的未完成条目登记进 todos 真源（幂等），"
                         "不巡检、不发群；登记完真源 count>0，读侧自动接管")
    ap.add_argument("--once", action="store_true", help="只跑一轮（调试/计划任务用）")
    ap.add_argument("--check-heartbeat", action="store_true",
                    help="只检查心跳：心跳过期则退出码非 0（给外部看门狗用）")
    ap.add_argument("--heartbeat-max-age", type=float, default=10800.0,
                    help="心跳超过多少秒算「守夜的睡过去了」")
    args = ap.parse_args()

    global LOG_FILE
    if args.log_file:
        LOG_FILE = args.log_file

    st = State(args.state, escalate_after=args.escalate_after,
               max_escalations=args.max_escalations)

    if args.check_heartbeat:
        last = float(st.heartbeat.get("last_ok") or 0)
        age = time.time() - last
        log("心跳检查：上次正常巡检 %s（%.0f 秒前，阈值 %.0f）"
            % (st.heartbeat.get("last_ok_iso") or "从未", age, args.heartbeat_max_age))
        return 0 if (last and age <= args.heartbeat_max_age) else 5

    if args.sync_todos:
        sid = pick_session(args.chat_base, args.session)
        if not sid:
            log("找不到可用的群，退出")
            return 2
        log("真源回填 · chat=%s · 群=%s · agent=%s · dry_run=%s"
            % (args.chat_base, sid, args.agent_id, args.dry_run))
        out = sync_todos(args.chat_base, sid, args.agent_id, st, dry_run=args.dry_run)
        log("  台账源=%s · 新登记=%d 条 · 完成态未登记=%d · 非本人未登记=%d · 失败=%d"
            % (out["src"] or "无", len(out["created"]), len(out["skipped_done"]),
               len(out["others"]), len(out["failed"])))
        for iid, owner, what in out["others"]:
            log("  非本人条目（后端只许本人登记，需其自行登记）%s owner=%s %s"
                % (iid, owner, what))
        after, asrc = load_ledger(args.chat_base, sid, args.agent_id)
        log("  回填后读侧：台账源=%s(%d 条)%s"
            % (asrc or "无", len(after), " · 已切 todos 真源" if asrc == "todos" else ""))
        return 0

    log("台账巡检器启动 · chat=%s · 群=%s · 每 %.0fs 一轮 · dry_run=%s · 升级=%.0fs/最多 %d 次"
        % (args.chat_base, args.session or "(默认群)", args.interval, args.dry_run,
           args.escalate_after, args.max_escalations))
    # #131 §3：巡检脚本自带心跳 —— 上一轮离现在太久，说明守夜的睡过去了，先把这句报出去
    last_ok = float(st.heartbeat.get("last_ok") or 0)
    if last_ok and (time.time() - last_ok) > args.heartbeat_max_age and not args.dry_run:
        sid = pick_session(args.chat_base, args.session)
        if sid:
            txt = ("%s 巡检器心跳异常：上一轮正常巡检在 %s（%.0f 小时前），"
                   "中间断过——「守夜的也睡过去」被自己抓到，请人来看。"
                   % (SENTINEL, st.heartbeat.get("last_ok_iso") or "?", (time.time() - last_ok) / 3600.0))
            seq, _ = post_and_verify(args.chat_base, sid, args.agent_id, txt,
                                     list(HUMAN_IDS) + [args.agent_id])
            log("  心跳告警已发回群 seq=%s" % seq)

    while True:
        err = ""
        try:
            one_round(args, st)
        except KeyboardInterrupt:
            st.save()
            log("收到退出信号，收工")
            return 0
        except Exception as exc:
            err = "%s: %s" % (type(exc).__name__, exc)
            log("本轮异常(下一轮继续):", err)
        st.beat(err)
        if not args.dry_run:
            st.save()
        if args.once:
            return 0 if not err else 1
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
