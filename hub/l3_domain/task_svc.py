#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""task_svc.py —— 群任务清单服务（第一版）

设计口径（对齐群消息 #311 <USER> 的要求）：
  * 任务清单由**程序**维护，不依赖 Agent/LLM 记忆；
  * 按群（group_id）隔离任务列表；
  * 字段：id / group_id / msg_seq(#序号) / proposer / created_at / title / content /
          owner / priority(normal|special) / status(pending|running|done|failed) /
          dispatch_count / updated_at；
  * 队列顺序 = 优先级(special 优先) + 创建时间；
  * /tasks/next 取队首并置 running（at-least-once，dispatch_count 计数）；
  * /tasks/<id>/report 汇报完成，**按 task_id 幂等**：重复汇报不改状态，返回 already_reported。

「完成任务后自己回答」（群 #340）：
  * Agent 只需调 POST /tasks/report；**服务端**在同一次请求里把结果以该 Agent 的身份
    回帖到它领取任务的那个群（task.group_id == chat_svc 的 session_id）；
  * 文案格式：任务标题 / 结果摘要 / 执行 Agent 名；@all；
  * 回帖账task_replies 与 report 同一事务落库（主键 task_id = 幂等闸门），
    同一 task 只回帖一次；重复 report 返回 already_reported 且**不重发**；
  * 投递失败 → 记录 attempts/last_error 并由后台线程按间隔重投，
    到上限标记 dead；绝不静默丢。

仅标准库 + sqlite3。SQLite 开 WAL + 事务。

新增（ack/lease + reply，均**只新增不改老接口语义**）：
  * POST /tasks/ack  {task_id, agent, lease_s=120} → 200 + lease_token/lease_expire_at
      （任务置 running；同 agent 重复 ack 幂等返回同一 token；他人持有→409；
       白名单外→403；任务不存在→404）；
  * GET  /tasks/lease            当前租约快照；POST /tasks/lease/reap 手动巡检；
  * lease 超时（默认 120s）→ 自动返队列；dispatch_count 到 3 仍超时 → failed + 冻结 chain_id；
      巡检 = 后台线程（每 10s）+ next_for/ack 惰性检查双保险；
  * GET  /chains 已冻结链；POST /chains/unfreeze {chain_id} 人工解冻；
  * chain_id：report 时写入；result=failed 即冻结该 chain，同 chain 不再派发；
  * kind=reply 任务：report 成功后按 idem_key="reply:<origin_task_id>" 派最小任务
      （kind/origin_task_id/context_digest≤800token/groups/agent=原owner/lease_s=120/
        fallback=L1|L2|L3），**只派任务、绝不代写回答正文**（content 恒为 None）。

启动：python3 task_svc.py --host 0.0.0.0 --port 9981
日志：/opt/agent-team/logs/task_svc.log
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import threading
import uuid
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import urlparse, parse_qs

# 批① 起被搬件按绝对包名 import，故把仓根一并放进搜索路径（本目录仍在最前）。
for _hacs_p in (os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                os.path.dirname(os.path.abspath(__file__))):
    sys.path.insert(0, _hacs_p)

from node.l0_host import node_paths as _np  # noqa: E402  L0 平台层：宿主默认值唯一出处（env 优先）
from node.l0_host import node_profile as _npf  # noqa: E402  L0 节点档案：宿主事实唯一真源（对话台基址；env 优先）

CST = timezone(timedelta(hours=8))
DB_DEFAULT = os.path.join(_np.runtime_dir(), "task_svc.db")
LOG_DEFAULT = os.path.join(_np.log_dir(), "task_svc.log")

# ---------------------------------------------------------------- 回帖（自动答群）
# 口径（群消息 #340 <USER> 问「如何让 Agent 完成任务后自己回答」）：
#   Agent 只管调 POST /tasks/report；**服务端**负责把结果以该 Agent 的身份回帖到
#   它领取任务的那个群（task.group_id == chat_svc 的 session_id）。
#   回帖与 report 同源同事务落库（task_replies），失败进重试队列，绝不静默丢。
CHAT_BASE_DEFAULT = os.environ.get("CHAT_BASE") or _npf.chat_base_default()
CHAT_REPLY_AGENT_FALLBACK = os.environ.get("TASK_CHAT_AGENT") or "hermes"
CHAT_POST_TIMEOUT = float(os.environ.get("TASK_CHAT_TIMEOUT") or 8)
CHAT_RETRY_INTERVAL = int(os.environ.get("TASK_CHAT_RETRY_INTERVAL") or 30)
CHAT_RETRY_MAX = int(os.environ.get("TASK_CHAT_RETRY_MAX") or 20)
CHAT_TEXT_MAX = 3800                      # 与 chat_svc TEXT_MAX(4000) 留余量

VALID_STATUS = ("pending", "running", "done", "failed")
VALID_PRIORITY = ("normal", "special")

# ---------------------------------------------------------------- ack / lease 口径
# 新增能力（**只新增，不改动既有接口语义**）：
#   * POST /tasks/ack  领任务时先 ack，拿 lease_token 与 lease_expire_at；
#       - 服务端把任务置 running + 记录 lease_expire_at；
#       - 同一 agent 重复 ack 幂等（返回同一个 lease_token，already_acked=true）；
#       - 别人持有未过期 lease → 409 leased_by_other；
#       - agent 白名单外 → 403 agent_not_allowed。
#   * lease 超时（默认 120s）：后台线程 + 惰性检查双保险，超时任务自动返队列；
#       dispatch_count 到上限（默认 3）仍超时 → 置 failed **并按 chain_id 冻结**；
#   * chain_id：report 时写入；failed 的 chain_id 进 chain_freezes，同 chain 不再派发；
#   * kind=reply 任务：report 成功后按 idem_key(=reply:<origin_task_id>) 派最小任务，
#       **只派任务、绝不代写回答正文**（没有生成正文的代码路径）。
LEASE_S_DEFAULT = int(os.environ.get("TASK_LEASE_S") or 120)
LEASE_S_MAX = 3600
LEASE_MAX_DISPATCH = int(os.environ.get("TASK_LEASE_MAX_DISPATCH") or 3)
LEASE_REAP_INTERVAL = int(os.environ.get("TASK_LEASE_REAP_INTERVAL") or 10)
# 白名单：逗号分隔；空串=不启用白名单（放行全部）。
AGENT_WHITELIST = tuple(
    x.strip() for x in (os.environ.get("TASK_AGENT_WHITELIST")
                        if os.environ.get("TASK_AGENT_WHITELIST") is not None
                        else "hermes,openclaw-nb").split(",") if x.strip())

REPLY_KIND = "reply"
# reply 任务的 fallback 三级：L1 重派 1 次 → L2 事实性降级通知（仍禁止代写正文）→ L3 告警
REPLY_FALLBACK = {
    "L1": "重派1次",
    "L2": "事实性降级通知（仍禁止代写正文）",
    "L3": "告警",
}
CONTEXT_DIGEST_MAX_TOKEN = 800


def agent_allowed(agent) -> bool:
    if not AGENT_WHITELIST:
        return True
    return (agent or "").strip() in AGENT_WHITELIST


def est_tokens(text: str) -> int:
    """粗估 token：CJK 字符 1:1，其余约 4 字符 1 token（保守偏高）。"""
    s = text or ""
    cjk = 0
    for ch in s:
        o = ord(ch)
        if (0x2E80 <= o <= 0x9FFF) or (0xF900 <= o <= 0xFAFF) or (0xFF00 <= o <= 0xFFEF):
            cjk += 1
    other = len(s) - cjk
    return cjk + (other + 3) // 4


def clip_digest(text: str, max_token: int = CONTEXT_DIGEST_MAX_TOKEN) -> str:
    """把上下文摘要裁到 ≤ max_token（保守估算），保证落库不超限。"""
    s = (text or "").strip()
    if est_tokens(s) <= max_token:
        return s
    # 末尾还要补一个省略号，占 1 token，所以正文只给 max_token-1 的预算。
    budget = max(1, max_token - 1)
    lo, hi = 0, len(s)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if est_tokens(s[:mid]) <= budget:
            lo = mid
        else:
            hi = mid - 1
    return s[:lo].rstrip() + "…"

log = logging.getLogger("task_svc")

# ---------------------------------------------------------------- storage
SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS tasks (
  id             TEXT PRIMARY KEY,
  group_id       TEXT NOT NULL,
  msg_seq        TEXT,
  proposer       TEXT,
  created_at     TEXT NOT NULL,
  title          TEXT NOT NULL,
  content        TEXT,
  owner          TEXT,
  priority       TEXT NOT NULL DEFAULT 'normal',
  status         TEXT NOT NULL DEFAULT 'pending',
  dispatch_count INTEGER NOT NULL DEFAULT 0,
  updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_group_status ON tasks(group_id, status);
CREATE TABLE IF NOT EXISTS task_reports (
  task_id    TEXT PRIMARY KEY,          -- 幂等键就是 task_id
  agent      TEXT,
  result     TEXT,
  note       TEXT,
  created_at TEXT NOT NULL
);
-- 回帖台账：**幂等键同样是 task_id**（PK），report 事务内落库，
-- 保证「同一 task 只回帖一次」；失败留痕并可重试，不静默丢。
CREATE TABLE IF NOT EXISTS task_replies (
  task_id     TEXT PRIMARY KEY,
  group_id    TEXT,
  agent       TEXT,
  text        TEXT,
  status      TEXT NOT NULL DEFAULT 'pending',   -- pending|posted|dead
  attempts    INTEGER NOT NULL DEFAULT 0,
  message_id  TEXT,
  message_seq INTEGER,
  last_error  TEXT,
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_replies_status ON task_replies(status);
-- chain 冻结表：同 chain_id 一经失败即冻结，禁止再次派发（口径：#356 承若）。
CREATE TABLE IF NOT EXISTS chain_freezes (
  chain_id   TEXT PRIMARY KEY,
  reason     TEXT,
  frozen_at  TEXT NOT NULL,
  frozen_by  TEXT
);
-- 幂等闸门：idem_key 唯一索引在 MIGRATE 里建（老库先要 ALTER 出 idem_key 列）。
"""

# ack/lease/reply 新增列（老库靠 ALTER TABLE 迁移；**只加列，不动老列**）。
MIGRATE_COLUMNS = (
    ("chain_id", "TEXT"),
    ("lease_token", "TEXT"),
    ("lease_expire_at", "TEXT"),
    ("lease_agent", "TEXT"),
    ("lease_s", "INTEGER"),
    ("ack_count", "INTEGER DEFAULT 0"),
    ("kind", "TEXT"),
    ("idem_key", "TEXT"),
    ("origin_task_id", "TEXT"),
    ("context_digest", "TEXT"),
    ("groups", "TEXT"),
    ("fallback", "TEXT"),
    ("hold", "INTEGER DEFAULT 0"),
)


def now_iso() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


# ---------------------------------------------------------------- 回帖：文案与投递
def build_reply_text(task, agent, result=None, note=None) -> str:
    """回帖正文：任务标题 / 结果摘要 / 执行 Agent 名（口径：群 #340）。"""
    title = (task.get("title") or "").strip() or "(无标题)"
    seq = (task.get("msg_seq") or "").strip()
    head = "【任务完成】%s %s" % (seq, title) if seq else "【任务完成】%s" % title
    summary = (note or "").strip() or (result or "").strip() or "done"
    if summary in ("done", "success", "ok"):
        summary = "已完成"
    text = "%s\n结果：%s\n执行：%s" % (head, summary, (agent or "?").strip())
    if len(text) > CHAT_TEXT_MAX:
        text = text[:CHAT_TEXT_MAX - 1] + "…"
    return text


def _opener():
    """显式禁代理：本机/容器里的 http_proxy 会让 tailnet 直连莫名 502。"""
    return urlrequest.build_opener(urlrequest.ProxyHandler({}))


def chat_post_message(session_id, agent_id, text, base=None, timeout=None):
    """把一条消息以 agent_id 的身份发进 session_id 群。

    通道（读源码得到，非猜测）：POST {CHAT_BASE}/api/sessions/{sid}/messages
      * 头 X-Agent-Id:<成员id>（只在 tailnet 来源被接受，回环 127.0.0.1 不算 tailnet）
      * 体 {"text": ..., "mentions": [...]}；Agent 发言必须 @ 至少一个目标
    返回 (ok, info)；失败一律带回可读的 error，调用方负责落库。
    """
    base = (base or CHAT_BASE_DEFAULT).rstrip("/")
    url = "%s/api/sessions/%s/messages" % (base, session_id)
    payload = json.dumps({"text": text, "mentions": ["@all"]},
                         ensure_ascii=False).encode("utf-8")
    req = urlrequest.Request(url, data=payload, method="POST", headers={
        "Content-Type": "application/json; charset=utf-8",
        "Accept": "application/json",
        "X-Agent-Id": agent_id or "",
    })
    try:
        with _opener().open(req, timeout=timeout or CHAT_POST_TIMEOUT) as r:
            raw = r.read().decode("utf-8", "replace")
            data = json.loads(raw) if raw.strip() else {}
            if not (data or {}).get("ok", True):
                return False, {"error": json.dumps(data, ensure_ascii=False)[:300]}
            msg = (data or {}).get("message") or {}
            return True, {"message_id": msg.get("message_id"), "message_seq": msg.get("seq"),
                          "http_status": r.status}
    except urlerror.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        return False, {"error": "http_%d: %s" % (e.code, raw[:300])}
    except Exception as exc:
        return False, {"error": "%s: %s" % (type(exc).__name__, exc)}


class Store:
    """单连接 + 锁；WAL 下读写并发可以接受（本用量极小）。"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(db_path, check_same_thread=False, timeout=15)
        self.conn.row_factory = sqlite3.Row
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(SCHEMA)
            # 老库迁移：只新增列，幂等（已存在则忽略）。
            have = {r[1] for r in self.conn.execute("PRAGMA table_info(tasks)")}
            for col, decl in MIGRATE_COLUMNS:
                if col not in have:
                    self.conn.execute("ALTER TABLE tasks ADD COLUMN %s %s" % (col, decl))
                    log.info("schema migrate: tasks.%s %s added", col, decl)
            self.conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_idem_key ON tasks(idem_key) "
                "WHERE idem_key IS NOT NULL")
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_lease ON tasks(status, lease_expire_at)")
            self.conn.commit()

    # --- helpers
    def _rows(self, sql, args=()):
        with self.lock:
            cur = self.conn.execute(sql, args)
            return [dict(r) for r in cur.fetchall()]

    def _one(self, sql, args=()):
        r = self._rows(sql, args)
        return r[0] if r else None

    # --- API
    def create(self, group_id, title, content=None, owner=None, priority="normal",
               msg_seq=None, proposer=None, status="pending", task_id=None,
               chain_id=None, kind=None, idem_key=None, origin_task_id=None,
               context_digest=None, groups=None, fallback=None, lease_s=None, hold=0):
        if priority not in VALID_PRIORITY:
            raise ValueError("bad_priority")
        if status not in VALID_STATUS:
            raise ValueError("bad_status")
        # 幂等：同 idem_key 不重复派发（返回既有任务，already_exists=true）。
        if idem_key:
            prev = self._one("SELECT * FROM tasks WHERE idem_key=?", (idem_key,))
            if prev:
                prev["already_exists"] = True
                prev["idem_key_hit"] = True
                return prev
        tid = task_id or ("T-" + uuid.uuid4().hex[:8])
        ts = now_iso()
        try:
            with self.lock:
                with self.conn:  # 事务
                    self.conn.execute(
                        "INSERT INTO tasks(id,group_id,msg_seq,proposer,created_at,title,content,"
                        "owner,priority,status,dispatch_count,updated_at,chain_id,kind,idem_key,"
                        "origin_task_id,context_digest,groups,fallback,lease_s,hold) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,0,?,?,?,?,?,?,?,?,?,?)",
                        (tid, group_id, msg_seq, proposer, ts, title, content, owner,
                         priority, status, ts, chain_id, kind, idem_key, origin_task_id,
                         context_digest, groups, fallback, lease_s, int(hold or 0)))
        except sqlite3.IntegrityError:
            # 并发下唯一索引兜底：别人刚插进去，返回既有那条。
            prev = self._one("SELECT * FROM tasks WHERE idem_key=?", (idem_key,))
            if prev:
                prev["already_exists"] = True
                prev["idem_key_hit"] = True
                return prev
            raise
        return self.get(tid)

    def get(self, tid):
        return self._one("SELECT * FROM tasks WHERE id=?", (tid,))

    def list(self, group_id, status=None, limit=500):
        sql = ("SELECT * FROM tasks WHERE group_id=?"
               + (" AND status=?" if status else "")
               + " ORDER BY CASE priority WHEN 'special' THEN 0 ELSE 1 END,"
                 " created_at ASC, rowid ASC LIMIT ?")
        args = (group_id, status, limit) if status else (group_id, limit)
        return self._rows(sql, args)

    def next_for(self, group_id, agent):
        """取队首（优先级+时间）派发；只派 pending 且未被别人认领的。

        新增过滤（不动老语义，只多两个排除条件）：
          * COALESCE(hold,0)=0 —— 显式「停手」的任务不派；
          * chain_id 未被冻结 —— failed 过的 chain 不再派发同 chain 任务。
        派发前先做一次惰性 lease 巡检（超时任务返队列）。
        """
        self.reap_leases()
        with self.lock:
            with self.conn:
                cur = self.conn.execute(
                    "SELECT * FROM tasks WHERE group_id=? AND status='pending' "
                    "AND (owner IS NULL OR owner='' OR owner=?) "
                    "AND COALESCE(hold,0)=0 "
                    "AND (chain_id IS NULL OR chain_id='' OR chain_id NOT IN "
                    "(SELECT chain_id FROM chain_freezes)) "
                    "ORDER BY CASE priority WHEN 'special' THEN 0 ELSE 1 END,"
                    " created_at ASC, rowid ASC LIMIT 1", (group_id, agent))
                row = cur.fetchone()
                if not row:
                    return None
                tid = row["id"]
                self.conn.execute(
                    "UPDATE tasks SET status='running', dispatch_count=dispatch_count+1, "
                    "owner=CASE WHEN owner IS NULL OR owner='' THEN ? ELSE owner END, "
                    "updated_at=? WHERE id=?", (agent, now_iso(), tid))
            t = self.get(tid)
        return t

    # --- ack / lease / chain 冻结 -------------------------------------
    def _freeze_chain(self, chain_id, reason, by=None):
        if not chain_id:
            return None
        with self.lock:
            with self.conn:
                self.conn.execute(
                    "INSERT INTO chain_freezes(chain_id,reason,frozen_at,frozen_by) "
                    "VALUES(?,?,?,?) ON CONFLICT(chain_id) DO UPDATE SET "
                    "reason=COALESCE(excluded.reason,reason)", 
                    (chain_id, reason, now_iso(), by))
        log.warning("chain 冻结 chain=%s reason=%s by=%s", chain_id, reason, by)
        return self._one("SELECT * FROM chain_freezes WHERE chain_id=?", (chain_id,))

    def chain_frozen(self, chain_id):
        if not chain_id:
            return None
        return self._one("SELECT * FROM chain_freezes WHERE chain_id=?", (chain_id,))

    def freeze_list(self):
        return self._rows("SELECT * FROM chain_freezes ORDER BY frozen_at DESC")

    def unfreeze_chain(self, chain_id):
        with self.lock:
            with self.conn:
                cur = self.conn.execute("DELETE FROM chain_freezes WHERE chain_id=?", (chain_id,))
            return cur.rowcount > 0

    def ack(self, tid, agent, lease_s=None):
        """ack = 领任务的显式认领 + 租约。

        幂等：同一 agent 在 lease 未过期前重复 ack 返回**同一个 lease_token**。
        """
        t = self.get(tid)
        if not t:
            return {"ok": False, "error": "not_found", "task_id": tid}
        if not agent_allowed(agent):
            return {"ok": False, "error": "agent_not_allowed", "agent": agent,
                    "whitelist": list(AGENT_WHITELIST)}
        try:
            ls = int(lease_s) if lease_s is not None else LEASE_S_DEFAULT
        except (TypeError, ValueError):
            return {"ok": False, "error": "bad_lease_s"}
        ls = max(1, min(ls, LEASE_S_MAX))
        cur_t = datetime.now(CST)
        if t["status"] in ("done", "failed"):
            return {"ok": False, "error": "not_dispatchable", "status": t["status"],
                    "task_id": tid}
        exp = t.get("lease_expire_at")
        active = False
        if t.get("lease_token") and exp:
            try:
                active = datetime.fromisoformat(exp) > cur_t
            except ValueError:
                active = False
        if active:
            if (t.get("lease_agent") or t.get("owner")) == agent:
                return {"ok": True, "already_acked": True, "task_id": tid,
                        "lease_token": t["lease_token"], "lease_expire_at": exp,
                        "lease_s": t.get("lease_s") or ls,
                        "task": t}
            return {"ok": False, "error": "leased_by_other",
                    "holder": t.get("lease_agent"), "lease_expire_at": exp,
                    "task_id": tid}
        token = "LT-" + uuid.uuid4().hex
        exp_iso = (cur_t + timedelta(seconds=ls)).isoformat(timespec="seconds")
        with self.lock:
            with self.conn:
                self.conn.execute(
                    "UPDATE tasks SET status='running', lease_token=?, lease_expire_at=?, "
                    "lease_agent=?, lease_s=?, ack_count=COALESCE(ack_count,0)+1, "
                    "dispatch_count=dispatch_count+1, "
                    "owner=CASE WHEN owner IS NULL OR owner='' THEN ? ELSE owner END, "
                    "updated_at=? WHERE id=?",
                    (token, exp_iso, agent, ls, agent, now_iso(), tid))
        log.info("ack task=%s agent=%s lease_s=%s token=%s exp=%s", tid, agent, ls, token, exp_iso)
        return {"ok": True, "already_acked": False, "task_id": tid, "lease_token": token,
                "lease_expire_at": exp_iso, "lease_s": ls, "agent": agent,
                "task": self.get(tid)}

    def reap_leases(self, now=None):
        """lease 超时巡检：返队列；超过 dispatch 上限 → failed 并冻结同 chain_id。

        同时被后台线程（周期）和 next_for/ack（惰性）调用，幂等安全。
        """
        ts = now or datetime.now(CST)
        out = []
        try:
            with self.lock:
                rows = [dict(r) for r in self.conn.execute(
                    "SELECT * FROM tasks WHERE status='running' AND lease_token IS NOT NULL "
                    "AND lease_expire_at IS NOT NULL AND lease_expire_at < ?",
                    (ts.isoformat(timespec="seconds"),)).fetchall()]
                for row in rows:
                    tid = row["id"]
                    dc = int(row.get("dispatch_count") or 0)
                    if dc >= LEASE_MAX_DISPATCH:
                        with self.conn:
                            self.conn.execute(
                                "UPDATE tasks SET status='failed', lease_token=NULL, "
                                "lease_expire_at=NULL, lease_agent=NULL, updated_at=? "
                                "WHERE id=?", (now_iso(), tid))
                        if row.get("chain_id"):
                            self._freeze_chain(row["chain_id"],
                                               "lease_timeout_exceeded_dispatch_max(%d)" % dc,
                                               "task_svc.lease_reaper")
                        out.append({"task_id": tid, "action": "failed_and_frozen",
                                    "dispatch_count": dc, "chain_id": row.get("chain_id")})
                        log.warning("lease 超限置 failed task=%s chain=%s dc=%s",
                                    tid, row.get("chain_id"), dc)
                    else:
                        with self.conn:
                            self.conn.execute(
                                "UPDATE tasks SET status='pending', lease_token=NULL, "
                                "lease_expire_at=NULL, lease_agent=NULL, updated_at=? "
                                "WHERE id=?", (now_iso(), tid))
                        out.append({"task_id": tid, "action": "requeued",
                                    "dispatch_count": dc, "chain_id": row.get("chain_id")})
                        log.warning("lease 超时返队列 task=%s dc=%s exp=%s",
                                    tid, dc, row.get("lease_expire_at"))
        except Exception:
            log.exception("lease 巡检异常")
        return out

    def lease_snapshot(self):
        """当前持有 lease 的任务（观测用）。"""
        return self._rows(
            "SELECT id,group_id,status,lease_agent,lease_expire_at,dispatch_count,ack_count "
            "FROM tasks WHERE lease_token IS NOT NULL ORDER BY lease_expire_at ASC")

    def dispatch_reply(self, origin, report_agent=None, note=None, result=None):
        """report 成功后按 idem_key 派一条**最小 reply 任务**。

        硬口径（群 #356 承诺）：
          * idem_key = "reply:" + origin_task_id，唯一索引兜底，同 idem_key 不重复派发；
          * 字段固定 kind=reply / idem_key / origin_task_id / context_digest(≤800token) /
            groups / agent(原任务 owner) / lease_s=120 / fallback=L1,L2,L3；
          * **只派任务，不生成任何回答正文**：content 恒为 None，title 是固定模板，
            本函数没有任何产出回答文本的分支（禁止程序代写）。
        """
        if not origin:
            return {"ok": False, "error": "no_origin"}
        oid = origin["id"]
        idem = "reply:%s" % oid
        if (origin.get("kind") or "") == REPLY_KIND:
            # reply 任务自身不再派生 reply，防止套娃。
            return {"ok": False, "error": "origin_is_reply_task", "idem_key": idem}
        owner = (origin.get("owner") or report_agent or "").strip() or None
        digest = clip_digest(
            "任务:%s\n标题:%s\n内容:%s\n上报Agent:%s\n上报结果:%s\n备注:%s" % (
                oid, (origin.get("title") or "")[:200], (origin.get("content") or "")[:1200],
                report_agent or "", result or "", note or ""))
        r = self.create(
            group_id=origin["group_id"],
            title="【reply·待原 Owner 自行作答】%s" % ((origin.get("title") or oid)[:80]),
            content=None,                      # 禁止程序代写：正文只能由 Agent 自己产出
            owner=owner,
            priority="normal",
            msg_seq=origin.get("msg_seq"),
            proposer="task_svc",
            status="pending",
            chain_id=origin.get("chain_id"),
            kind=REPLY_KIND,
            idem_key=idem,
            origin_task_id=oid,
            context_digest=digest,
            groups=origin.get("group_id"),
            fallback=json.dumps(REPLY_FALLBACK, ensure_ascii=False),
            lease_s=LEASE_S_DEFAULT,
        )
        already = bool(r.get("idem_key_hit"))
        return {"ok": True, "idem_key": idem, "already_dispatched": already,
                "reply_task": {k: r.get(k) for k in (
                    "id", "group_id", "kind", "idem_key", "origin_task_id", "owner",
                    "status", "lease_s", "fallback", "context_digest", "groups")},
                "autopost_disabled": True}

    def patch(self, tid, fields):
        t = self.get(tid)
        if not t:
            return None
        sets, args = [], []
        for k in ("status", "owner", "priority", "title", "content", "msg_seq", "proposer"):
            if k in fields and fields[k] is not None:
                v = fields[k]
                if k == "status" and v not in VALID_STATUS:
                    raise ValueError("bad_status")
                if k == "priority" and v not in VALID_PRIORITY:
                    raise ValueError("bad_priority")
                sets.append(k + "=?")
                args.append(v)
        if not sets:
            return t
        sets.append("updated_at=?")
        args.append(now_iso())
        args.append(tid)
        with self.lock:
            with self.conn:
                self.conn.execute("UPDATE tasks SET " + ",".join(sets) + " WHERE id=?", args)
        # #418-1 任务结束报告（PATCH 路径补齐）：状态**首次**落到终态时，无论走
        # POST /tasks/<id>/report 还是前端任务面板的下拉 PATCH，都补一条
        # 「【任务完成】…」回帖进群。与 report 共用 task_replies 的 PK=task_id 幂等闸门
        # —— 两条路径只会发一条，不重复、不静默。
        new_st = fields.get("status")
        if (new_st in ("done", "failed") and new_st != (t.get("status") or "")
                and (t.get("kind") or "") != REPLY_KIND):
            agent = (t.get("owner") or fields.get("owner") or CHAT_REPLY_AGENT_FALLBACK)
            try:
                with self.lock:
                    with self.conn:
                        self.conn.execute(
                            "INSERT OR IGNORE INTO task_replies(task_id,group_id,agent,text,"
                            "status,attempts,created_at,updated_at) VALUES(?,?,?,?, 'pending',0,?,?)",
                            (tid, t["group_id"], agent,
                             build_reply_text(t, agent, new_st, None), now_iso(), now_iso()))
                log.info("终态回帖入队 task=%s status=%s agent=%s", tid, new_st, agent)
            except Exception:
                log.exception("终态回帖入队异常 task=%s", tid)
        out = self.get(tid)
        # 事务外投递（不持库锁跑网络 IO）；失败留给 reply_retry_loop 补投。
        if new_st in ("done", "failed") and (t.get("kind") or "") != REPLY_KIND:
            try:
                row = self.reply_get(tid)
                if row and row["status"] != "posted":
                    out["reply"] = self.reply_deliver(tid)
            except Exception:
                log.exception("终态回帖投递异常 task=%s", tid)
        return out

    def delete(self, tid):
        with self.lock:
            with self.conn:
                cur = self.conn.execute("DELETE FROM tasks WHERE id=?", (tid,))
        return cur.rowcount > 0

    def report(self, tid, agent=None, result="done", note=None, chain_id=None):
        """幂等：同一 task_id 重复汇报不改状态。

        新增（不动既有返回值语义）：
          * chain_id：上报时写入；result=failed → **按 chain_id 冻结**，同 chain 不再派发；
          * 释放 lease（汇报即交付，租约作废）；
          * kind != reply 且 result 成功 → 派一条最小 reply 任务（π 幂等由 idem_key 兜底）；
          * kind == reply → 不做任何自动回帖（正文必须由 Agent 自己产出，禁止程序代写）。
        """
        t = self.get(tid)
        if not t:
            return {"ok": False, "error": "not_found", "task_id": tid}
        cid = (chain_id or "").strip() or t.get("chain_id") or None
        is_reply_origin = (t.get("kind") or "") == REPLY_KIND
        with self.lock:
            with self.conn:
                cur = self.conn.execute("SELECT * FROM task_reports WHERE task_id=?", (tid,))
                prev = cur.fetchone()
                if prev is not None:
                    return {"ok": True, "already_reported": True, "task_id": tid,
                            "task": self.get(tid),
                            "reply": self.reply_get(tid),      # 不回帖，只回显首次回帖账
                            "reply_dispatch": (self._one(
                                "SELECT * FROM tasks WHERE idem_key=?", ("reply:%s" % tid,))
                                or None),
                            "first_report": {"agent": prev["agent"], "result": prev["result"],
                                             "at": prev["created_at"]}}
                self.conn.execute(
                    "INSERT INTO task_reports(task_id,agent,result,note,created_at) VALUES(?,?,?,?,?)",
                    (tid, agent, result, note, now_iso()))
                st = "done" if result in (None, "", "done", "success", "ok") else "failed"
                if st not in VALID_STATUS:
                    st = "done"
                self.conn.execute(
                    "UPDATE tasks SET status=?, chain_id=COALESCE(?,chain_id), lease_token=NULL, "
                    "lease_expire_at=NULL, lease_agent=NULL, updated_at=? WHERE id=?",
                    (st, cid, now_iso(), tid))
                if st == "failed" and cid:
                    # failed 状态按 chain_id 冻结：同 chain 任务禁止再次派发。
                    self._freeze_chain(cid, "report_failed task=%s" % tid, agent)
                # 回帖与 report **同一事务**落库（PK=task_id 即幂等闸门）：
                # 事务成功 = 恰好一条待投递回帖；失败 = 一条 attempts=1 的重试记录。
                # 投递本身在事务外做（见 reply_deliver），避免持库锁跑网络 IO。
                # kind=reply 不回帖：正文只能由 Agent 自己写（禁止程序代写）。
                if not is_reply_origin:
                    self.conn.execute(
                        "INSERT OR IGNORE INTO task_replies(task_id,group_id,agent,text,status,"
                        "attempts,created_at,updated_at) VALUES(?,?,?,?, 'pending',0,?,?)",
                        (tid, t["group_id"], agent or t.get("owner") or CHAT_REPLY_AGENT_FALLBACK,
                         build_reply_text(t, agent or t.get("owner") or CHAT_REPLY_AGENT_FALLBACK,
                                          result, note),
                         now_iso(), now_iso()))
        # --- 事务外：派 reply 任务（只派任务，绝不生成回答正文）
        dispatch = None
        if st == "done" and not is_reply_origin:
            try:
                dispatch = self.dispatch_reply(t, report_agent=agent, note=note, result=result)
            except Exception as exc:
                log.exception("reply 任务派发异常 task=%s", tid)
                dispatch = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
        return {"ok": True, "already_reported": False, "task_id": tid,
                "task": self.get(tid), "reply_queued": not is_reply_origin,
                "autopost_disabled": is_reply_origin,
                "chain_id": cid, "chain_frozen": bool(self.chain_frozen(cid)) if cid else False,
                "reply_dispatch": dispatch}

    def reply_deliver(self, tid):
        """把这个 task 的回帖投出去（幂等：已 posted 直接返回）。"""
        row = self.reply_get(tid)
        if not row:
            return {"ok": False, "status": "missing", "error": "no_reply_row"}
        if row["status"] == "posted":
            return {"ok": True, "status": "posted", "already": True,
                    "message_id": row["message_id"], "message_seq": row["message_seq"]}
        ok, info = chat_post_message(row["group_id"], row["agent"], row["text"])
        if ok:
            r = self.reply_mark(tid, "posted", message_id=info.get("message_id"),
                                message_seq=info.get("message_seq"))
            log.info("回帖成功 task=%s group=%s agent=%s seq=%s",
                     tid, row["group_id"], row["agent"], info.get("message_seq"))
            return {"ok": True, "status": "posted", "message_id": r["message_id"],
                    "message_seq": r["message_seq"]}
        status = "failed" if (row["attempts"] + 1) < CHAT_RETRY_MAX else "dead"
        r = self.reply_mark(tid, status, error=info.get("error"))
        log.warning("回帖失败 task=%s group=%s agent=%s attempts=%s status=%s err=%s",
                    tid, row["group_id"], row["agent"], r["attempts"], status,
                    info.get("error"))
        return {"ok": False, "status": status, "attempts": r["attempts"],
                "error": info.get("error"), "retry_queue": status == "failed"}

    # --- 回帖台账（task_replies） -------------------------------------
    def reply_get(self, tid):
        return self._one("SELECT * FROM task_replies WHERE task_id=?", (tid,))

    def reply_pending(self, limit=20):
        """待投递（含失败待重试）的回帖；dead 与 posted 不再取。"""
        return self._rows(
            "SELECT * FROM task_replies WHERE status IN ('pending','failed') "
            "AND attempts < ? ORDER BY created_at ASC LIMIT ?", (CHAT_RETRY_MAX, limit))

    def reply_mark(self, tid, status, message_id=None, message_seq=None, error=None):
        with self.lock:
            with self.conn:
                self.conn.execute(
                    "UPDATE task_replies SET status=?, attempts=attempts+1, "
                    "message_id=COALESCE(?,message_id), message_seq=COALESCE(?,message_seq), "
                    "last_error=?, updated_at=? WHERE task_id=?",
                    (status, message_id, message_seq, (error or "")[:500] or None,
                     now_iso(), tid))
        return self.reply_get(tid)

    def stats(self, group_id=None):
        sql = ("SELECT group_id, status, COUNT(*) n FROM tasks "
               + ("WHERE group_id=? " if group_id else "")
               + "GROUP BY group_id, status")
        return self._rows(sql, (group_id,) if group_id else ())


# ---------------------------------------------------------------- http
class Handler(BaseHTTPRequestHandler):
    server_version = "task_svc/0.1"
    store: Store = None
    quiet = True

    def log_message(self, fmt, *args):
        log.info("%s - %s", self.address_string(), fmt % args)

    # --- utils
    def _send(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False, indent=1).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,PATCH,DELETE,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type,X-Agent-Id,Authorization")
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        raw = self.rfile.read(n)
        try:
            d = json.loads(raw.decode("utf-8"))
        except Exception:
            raise ValueError("bad_json")
        if not isinstance(d, dict):
            raise ValueError("bad_json")
        return d

    # --- verbs
    def do_OPTIONS(self):
        self._send(204, {})

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        one = lambda k, d=None: (q.get(k) or [d])[0]

        if u.path in ("/", "/health"):
            return self._send(200, {"ok": True, "service": "task_svc", "version": "0.1",
                                    "time": now_iso(),
                                    "groups": sorted({r["group_id"] for r in self.store.stats()})})
        if u.path == "/tasks":
            group = one("group")
            if not group:
                return self._send(400, {"ok": False, "error": "group_required"})
            status = one("status") or None
            if status == "":
                status = None
            if status and status not in VALID_STATUS:
                return self._send(400, {"ok": False, "error": "bad_status"})
            try:
                limit = min(int(one("limit", "500") or 500), 2000)
            except ValueError:
                limit = 500
            items = self.store.list(group, status, limit)
            return self._send(200, {"ok": True, "group": group, "status": status,
                                    "count": len(items), "tasks": items,
                                    "order": "priority(special first) + created_at asc"})
        if u.path == "/tasks/next":
            group = one("group")
            agent = one("agent")
            if not group or not agent:
                return self._send(400, {"ok": False, "error": "group_and_agent_required"})
            t = self.store.next_for(group, agent)
            if not t:
                return self._send(200, {"ok": True, "task": None, "reason": "empty_queue",
                                        "group": group, "agent": agent})
            return self._send(200, {"ok": True, "task": t, "dispatched": True,
                                    "group": group, "agent": agent})
        m = u.path.strip("/").split("/")
        if u.path == "/tasks/lease":                  # GET 当前租约快照（新增）
            return self._send(200, {"ok": True, "leases": self.store.lease_snapshot(),
                                    "lease_s_default": LEASE_S_DEFAULT,
                                    "lease_max_dispatch": LEASE_MAX_DISPATCH,
                                    "whitelist": list(AGENT_WHITELIST)})
        if u.path == "/chains":                       # GET 已冻结链（新增）
            return self._send(200, {"ok": True, "frozen": self.store.freeze_list()})
        if len(m) == 2 and m[0] == "tasks":           # GET /tasks/<id>
            t = self.store.get(m[1])
            if not t:
                return self._send(404, {"ok": False, "error": "not_found"})
            return self._send(200, {"ok": True, "task": t})
        return self._send(404, {"ok": False, "error": "no_route", "path": u.path})

    def do_POST(self):
        u = urlparse(self.path)
        parts = u.path.strip("/").split("/")
        try:
            body = self._body()
        except ValueError as e:
            return self._send(400, {"ok": False, "error": str(e)})

        if u.path == "/tasks":
            group = body.get("group_id") or body.get("group")
            title = (body.get("title") or "").strip()
            if not group or not title:
                return self._send(400, {"ok": False, "error": "group_and_title_required"})
            try:
                t = self.store.create(
                    group_id=group, title=title, content=body.get("content"),
                    owner=body.get("owner"), priority=body.get("priority") or "normal",
                    msg_seq=body.get("msg_seq"), proposer=body.get("proposer"),
                    status=body.get("status") or "pending", task_id=body.get("id"),
                    chain_id=body.get("chain_id"), kind=body.get("kind"),
                    idem_key=body.get("idem_key"), origin_task_id=body.get("origin_task_id"),
                    context_digest=body.get("context_digest"), groups=body.get("groups"),
                    fallback=body.get("fallback"), lease_s=body.get("lease_s"),
                    hold=body.get("hold") or 0)
            except ValueError as e:
                return self._send(400, {"ok": False, "error": str(e)})
            if t.get("already_exists"):
                return self._send(200, {"ok": True, "already_exists": True, "task": t})
            return self._send(201, {"ok": True, "task": t})
        if u.path == "/tasks/ack":                     # 新增：ack + lease
            tid = (body.get("task_id") or body.get("id") or "").strip()
            agent = (body.get("agent") or "").strip()
            if not tid or not agent:
                return self._send(400, {"ok": False, "error": "task_id_and_agent_required"})
            r = self.store.ack(tid, agent, body.get("lease_s"))
            if r.get("ok"):
                return self._send(200, r)
            code = {"not_found": 404, "agent_not_allowed": 403, "leased_by_other": 409,
                    "not_dispatchable": 409, "bad_lease_s": 400}.get(r.get("error"), 400)
            return self._send(code, r)
        if u.path == "/chains/unfreeze":               # 新增：人工解冻 chain
            cid = (body.get("chain_id") or "").strip()
            if not cid:
                return self._send(400, {"ok": False, "error": "chain_id_required"})
            return self._send(200, {"ok": True, "chain_id": cid,
                                    "unfrozen": self.store.unfreeze_chain(cid)})
        if u.path == "/tasks/lease/reap":              # 新增：手动触发一次巡检
            return self._send(200, {"ok": True, "reaped": self.store.reap_leases()})
        if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "report":
            tid = parts[1]
            r = self.store.report(tid, agent=body.get("agent"),
                                  result=body.get("result") or "done",
                                  note=body.get("note"),
                                  chain_id=body.get("chain_id"))
            if not r.get("ok"):
                return self._send(404, r)
            # 首次汇报（非重复）→ 立刻投递回帖；重复汇报**绝不重发**。
            if not r.get("already_reported"):
                try:
                    r["reply"] = self.store.reply_deliver(tid)
                except Exception as exc:          # 投递异常也不许吞掉 report 成功
                    log.exception("回帖投递异常 task=%s", tid)
                    r["reply"] = {"ok": False, "status": "error", "error": str(exc)}
            return self._send(200, r)
        if len(parts) == 2 and parts[0] == "tasks" and parts[1] == "next":
            return self._send(405, {"ok": False, "error": "use_GET"})
        return self._send(404, {"ok": False, "error": "no_route", "path": u.path})

    def do_PATCH(self):
        u = urlparse(self.path)
        parts = u.path.strip("/").split("/")
        try:
            body = self._body()
        except ValueError as e:
            return self._send(400, {"ok": False, "error": str(e)})
        if len(parts) == 2 and parts[0] == "tasks":
            try:
                t = self.store.patch(parts[1], body)
            except ValueError as e:
                return self._send(400, {"ok": False, "error": str(e)})
            if not t:
                return self._send(404, {"ok": False, "error": "not_found"})
            return self._send(200, {"ok": True, "task": t})
        return self._send(404, {"ok": False, "error": "no_route", "path": u.path})

    def do_DELETE(self):
        u = urlparse(self.path)
        parts = u.path.strip("/").split("/")
        if len(parts) == 2 and parts[0] == "tasks":
            ok = self.store.delete(parts[1])
            if not ok:
                return self._send(404, {"ok": False, "error": "not_found"})
            return self._send(200, {"ok": True, "deleted": parts[1]})
        return self._send(404, {"ok": False, "error": "no_route", "path": u.path})


def reply_retry_loop(store: Store, stop: threading.Event, interval: int):
    """回帖重试队列消费线程：扫 task_replies 里 pending/failed 的记录补投。

    存在的意义：report 那一刻群里正好不可用（chat_svc 重启/网络抖动）时，
    回帖不能就这么没了 —— 落库 + 重试，直到 posted 或 attempts 到顶（dead）。
    """
    while not stop.wait(interval):
        try:
            todo = store.reply_pending()
            for row in todo:
                store.reply_deliver(row["task_id"])
        except Exception:
            log.exception("回帖重试线程异常")


def lease_reap_loop(store: Store, stop: threading.Event, interval: int):
    """lease 超时巡检线程：周期把超时任务返队列/置 failed 并冻结 chain。

    与 next_for/ack 里的惰性巡检互为兜底：线程挂了也不会漏检
    （只要有下一次 /tasks/next 或 /tasks/ack 就会被检查到）。
    """
    while not stop.wait(interval):
        try:
            store.reap_leases()
        except Exception:
            log.exception("lease 巡检线程异常")


def main():
    global CHAT_BASE_DEFAULT, AGENT_WHITELIST, LEASE_S_DEFAULT
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=9981)
    ap.add_argument("--db", default=DB_DEFAULT)
    ap.add_argument("--log", default=LOG_DEFAULT)
    ap.add_argument("--chat-base", default=CHAT_BASE_DEFAULT)
    ap.add_argument("--agent-whitelist", default=os.environ.get("TASK_AGENT_WHITELIST", ""),
                    help="ack 白名单，逗号分隔；不给则用 TASK_AGENT_WHITELIST 或默认 hermes,openclaw-nb")
    ap.add_argument("--lease-s", type=int, default=LEASE_S_DEFAULT,
                    help="ack 默认租约秒数（默认 120）")
    ap.add_argument("--no-reply", action="store_true",
                    help="关闭 report 后自动回帖（应急开关，默认开）")
    a = ap.parse_args()

    CHAT_BASE_DEFAULT = a.chat_base
    LEASE_S_DEFAULT = a.lease_s
    if a.agent_whitelist:
        AGENT_WHITELIST = tuple(x.strip() for x in a.agent_whitelist.split(",") if x.strip())

    os.makedirs(os.path.dirname(a.log), exist_ok=True)
    fh = logging.FileHandler(a.log, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(fh)
    log.addHandler(sh)
    log.setLevel(logging.INFO)

    store = Store(a.db)
    Handler.store = store
    log.info("task_svc starting host=%s port=%s db=%s pid=%s", a.host, a.port, a.db, os.getpid())
    stop = threading.Event()
    if a.no_reply:
        log.warning("回帖功能已禁用（--no-reply）：report 只落库不发言")
    else:
        log.info("回帖目标 chat_base=%s 重试间隔=%ss 上限=%s次",
                 CHAT_BASE_DEFAULT, CHAT_RETRY_INTERVAL, CHAT_RETRY_MAX)
        t = threading.Thread(target=reply_retry_loop, args=(store, stop, CHAT_RETRY_INTERVAL),
                             name="reply-retry", daemon=True)
        t.start()
    # lease 巡检线程：与回帖开关无关，永远启（ack/lease 的核心兜底）。
    log.info("lease 巡检启动 间隔=%ss lease_s默认=%s dispatch上限=%s 白名单=%s",
             LEASE_REAP_INTERVAL, LEASE_S_DEFAULT, LEASE_MAX_DISPATCH,
             list(AGENT_WHITELIST) or "(disabled)")
    threading.Thread(target=lease_reap_loop, args=(store, stop, LEASE_REAP_INTERVAL),
                     name="lease-reaper", daemon=True).start()
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    srv.daemon_threads = True
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log.info("task_svc stopping (KeyboardInterrupt)")
    finally:
        stop.set()
        srv.server_close()


if __name__ == "__main__":
    main()
