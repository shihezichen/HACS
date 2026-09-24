#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_chat_e2e.py —— 人机对话台后端·真库真服务端到端验收

跟单测的分工：
  * tests/test_chat_svc.py 用假 DB 验**路由/鉴权/信封/501**，不碰 PG；
  * 本脚本打**真 PG（龙虾 9950）+ 真服务（<host>:9980）**，
    专验单测覆盖不到的东西：**真 SQL 的分页方向、seq 升序、append-only、密级过滤**。

在龙虾上跑（服务在本机 tailnet 上）：
  NO_PROXY='*' python3 /tmp/verify_chat_e2e.py

⚠️ chat_messages 是 append-only，测试数据**插了就删不掉**（这是设计）。
   所以测试 session_id 固定带 e2e 字样，方便日后识别与共存。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("CHAT_BASE") or "http://<host>:9980"
SID = "dm_20260920_e2e001"
SID_MEET = "meet_20260920_e2e001"
PSQL = ["docker", "exec", "-i", "agent-team-pg", "psql", "-U", "bb", "-d", "blackboard", "-t", "-A", "-c"]

PASS = 0
FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [PASS] %s" % name)
    else:
        FAIL += 1
        print("  [FAIL] %s %s" % (name, detail))


def sql(statement: str) -> str:
    out = subprocess.run(PSQL + [statement], capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError("psql 失败：%s" % (out.stderr or "").strip()[:200])
    return out.stdout.strip()


def call(path: str, method: str = "GET", body=None, timeout: float = 10.0, headers=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {"raw": raw}


def seed() -> None:
    """造测试数据（幂等：先看有没有，没有才插）。"""
    print("== 0. 造测试数据（真库）==")
    existing = sql("SELECT count(*) FROM chat_sessions WHERE session_id='%s'" % SID)
    if existing.strip() == "1":
        print("  [SKIP] 测试会话已存在（append-only 删不掉，复用）")
    else:
        sql("INSERT INTO chat_sessions(session_id,kind,title,sensitivity,members,created_by,updated_at,state) "
            "VALUES ('%s','dm','e2e 自测会话 · 后端只读验收','internal','[\"lobster\"]'::jsonb,'hermes',now(),'open')" % SID)
        for i in range(1, 11):
            sql("INSERT INTO chat_messages(message_id,session_id,seq,author_type,author_id,kind,text,status) "
                "VALUES ('msg_20260920e2e%06d','%s',%d,%s,'%s',%s,'e2e 第 %d 条',%s)" % (
                    i, SID, i,
                    "'human'" if i % 2 == 1 else "'agent'",
                    "<human-id>" if i % 2 == 1 else "lobster",
                    "'text'", i, "'done'"))
        print("  插入 1 个会话 + 10 条消息")

    # 公开会话（测 viewer 能看见 / internal 看不见）
    if sql("SELECT count(*) FROM chat_sessions WHERE session_id='dm_20260920_e2epub'").strip() == "0":
        sql("INSERT INTO chat_sessions(session_id,kind,title,sensitivity,members,created_by,updated_at,state) "
            "VALUES ('dm_20260920_e2epub','dm','e2e 公开会话','public','[\"lobster\"]'::jsonb,'hermes',now(),'open')")

    # meet 场（ref 指一个不存在的会议，验证"读不到真源不编数"）
    if sql("SELECT count(*) FROM chat_sessions WHERE session_id='%s'" % SID_MEET).strip() == "0":
        sql("INSERT INTO chat_sessions(session_id,kind,title,sensitivity,members,created_by,updated_at,state,ref_table,ref_id) "
            "VALUES ('%s','meet','e2e 会议会话','internal','[\"lobster\"]'::jsonb,'hermes',now(),'open','roundtable_sessions','rt_20260920_notexist')" % SID_MEET)


def group_phase() -> None:
    """§10 临时群（契约 §I）：真 SQL 打真库 + 真服务。

    建群那步直接用 SQL 落库（端到端脚本没有人工会话 cookie，网页那步归 UI）；
    发言/门禁/并发 seq 全部走真 HTTP + 真 PG。
    """
    import threading
    import time as _time

    gid = "meet_%s_e2e" % _time.strftime("%Y%m%d%H%M%S")
    print("\n== 10. 临时群 · 群协议（契约 §I）==")
    sql("INSERT INTO chat_sessions (session_id, kind, title, members, created_by, state)"
        " VALUES ('%s','meet','E2E 临时群',"
        " '[\"<human-id>\",\"hermes\",\"openclaw-nb\"]'::jsonb, '<human-id>','open')" % gid)
    check("建群落库", sql("SELECT state FROM chat_sessions WHERE session_id='%s'" % gid) == "open")

    st, body = call("/api/sessions/%s/messages" % gid, "POST", {"text": "我先建表了"},
                    headers={"X-Agent-Id": "hermes"})
    check("Agent 无 @ -> 422 mention_required",
          st == 422 and (body.get("error") or {}).get("code") == "mention_required",
          "%s %s" % (st, body))

    st, body = call("/api/sessions/%s/messages" % gid, "POST",
                    {"text": "喂", "mentions": ["nobody"]},
                    headers={"X-Agent-Id": "hermes"})
    check("提到非成员 -> 422 unknown_mention",
          st == 422 and (body.get("error") or {}).get("code") == "unknown_mention",
          "%s %s" % (st, body))

    st, body = call("/api/sessions/%s/messages" % gid, "POST",
                    {"text": "后台建表完成", "mentions": ["@all"]},
                    headers={"X-Agent-Id": "hermes"})
    msg = (body or {}).get("message") or {}
    check("Agent @all 落库 201 且 seq=1",
          st == 201 and msg.get("seq") == 1, "%s %s" % (st, body))
    check("needs_response 去掉发送者自己",
          sorted((msg.get("meta") or {}).get("needs_response") or []) == ["<human-id>", "openclaw-nb"],
          str(msg.get("meta")))

    # 并发 12 条：真验 pg_advisory_xact_lock 有没有把 seq 串成唯一连续
    got = []
    guard = threading.Lock()

    def shoot(i: int) -> None:
        st2, body2 = call("/api/sessions/%s/messages" % gid, "POST",
                          {"text": "并发 %d" % i, "mentions": ["@all"]},
                          headers={"X-Agent-Id": "openclaw-nb"}, timeout=25.0)
        with guard:
            got.append((st2, ((body2 or {}).get("message") or {}).get("seq")))

    threads = [threading.Thread(target=shoot, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    seqs = sorted([s for st3, s in got if st3 == 201 and s])
    check("并发 12 条全部 201", len(seqs) == 12, str(got))
    check("seq 无重复且连续（2..13）", seqs == list(range(2, 14)), str(seqs))

    db_cnt = sql("SELECT count(*) FROM chat_messages WHERE session_id='%s'" % gid)
    st, body = call("/api/sessions/%s/messages?limit=200" % gid,
                    headers={"X-Agent-Id": "hermes"})
    check("库 count == 接口条数",
          str(len(body["messages"])) == db_cnt,
          "db=%s api=%s" % (db_cnt, len(body.get("messages") or [])))
    pending = body.get("my_pending") or []
    check("my_pending 含别人 @我 的、不含自己发的",
          len(pending) == 12 and all(p["from"] == "openclaw-nb" for p in pending),
          str(pending)[:160])
    check("库内 meta 有 mentions/audience/needs_response",
          sql("SELECT meta ? 'mentions' AND meta ? 'audience' AND meta ? 'needs_response'"
              " FROM chat_messages WHERE session_id='%s' ORDER BY seq LIMIT 1" % gid) == "t")

    sql("UPDATE chat_sessions SET state='archived' WHERE session_id='%s'" % gid)
    st, body = call("/api/sessions/%s/messages" % gid, "POST",
                    {"text": "还在吗", "mentions": ["@all"]},
                    headers={"X-Agent-Id": "hermes"})
    check("归档后发言 -> 409 session_closed",
          st == 409 and (body.get("error") or {}).get("code") == "session_closed",
          "%s %s" % (st, body))


def main() -> int:
    print("== 目标服务：%s ==" % BASE)
    seed()

    print("\n== 1. healthz（公开）==")
    status, body = call("/healthz")
    check("healthz 200", status == 200, str(status))
    check("healthz ok=true", body.get("ok") is True, str(body))
    check("healthz db=true（真连上 PG）", body.get("db") is True, str(body.get("db")))
    check("healthz service/version", body.get("service") == "agent-team-chat" and body.get("version"), str(body))

    print("\n== 2. tailnet 直连 = owner（免登录）==")
    status, body = call("/api/whoami")
    check("whoami 200", status == 200, str(status))
    check("source=tailnet", body.get("user", {}).get("source") == "tailnet", str(body.get("user")))
    check("role=owner", body.get("user", {}).get("role") == "owner", str(body.get("user")))
    check("caps.can_admin", body.get("caps", {}).get("can_admin") is True, str(body.get("caps")))
    check("时间带 +08:00", str(body.get("server_time", "")).endswith("+08:00"), str(body.get("server_time")))

    print("\n== 3. /api/agents（真名册）==")
    status, body = call("/api/agents")
    check("agents 200", status == 200, str(status))
    ids = [a["id"] for a in body.get("agents", [])]
    check("名册非空", len(ids) > 0, str(ids))
    check("id 排序稳定", ids == sorted(ids), str(ids))
    check("在线态一律 None（本版不猜）", all(a["online"] is None for a in body.get("agents", [])), str(body.get("agents")))

    print("\n== 4. /api/sessions（真 SQL + 密级过滤）==")
    status, body = call("/api/sessions?limit=200")
    check("sessions 200", status == 200, str(status))
    sids = [s["session_id"] for s in body.get("sessions", [])]
    check("能读到 e2e 会话（真 SQL 生效）", SID in sids, str(sids[:8]))
    check("能读到 meet 会话", SID_MEET in sids, str(sids[:8]))
    row = [s for s in body["sessions"] if s["session_id"] == SID][0]
    check("last_message.seq = 10（真 SQL 取最新）", (row.get("last_message") or {}).get("seq") == 10, str(row.get("last_message")))
    check("last_message.excerpt 有内容", bool((row.get("last_message") or {}).get("excerpt")), str(row.get("last_message")))
    check("unread 为 None（v1 不做已读游标）", row.get("unread") is None, str(row.get("unread")))
    check("my_role=owner", row.get("my_role") == "owner", str(row.get("my_role")))
    check("updated_at 带 +08:00", str(row.get("updated_at", "")).endswith("+08:00"), str(row.get("updated_at")))
    meet_row = [s for s in body["sessions"] if s["session_id"] == SID_MEET][0]
    check("meet 条目带 meeting 块", "meeting" in meet_row, str(meet_row.keys()))

    print("\n== 5. 历史分页（真 SQL，重点）==")
    status, body = call("/api/sessions/%s/messages?limit=3" % SID)
    seqs = [m["seq"] for m in body.get("messages", [])]
    check("默认取最近 3 条", seqs == [8, 9, 10], str(seqs))
    check("按 seq 升序", seqs == sorted(seqs), str(seqs))
    check("has_more=True", body.get("has_more") is True, str(body.get("has_more")))
    check("next_before_seq=8", body.get("next_before_seq") == 8, str(body.get("next_before_seq")))

    status, body = call("/api/sessions/%s/messages?limit=3&before_seq=8" % SID)
    seqs = [m["seq"] for m in body.get("messages", [])]
    check("before_seq=8 取 [5,6,7]", seqs == [5, 6, 7], str(seqs))
    check("仍升序", seqs == sorted(seqs), str(seqs))

    status, body = call("/api/sessions/%s/messages?limit=3&after_seq=4" % SID)
    seqs = [m["seq"] for m in body.get("messages", [])]
    check("after_seq=4 取 [5,6,7]", seqs == [5, 6, 7], str(seqs))

    status, body = call("/api/sessions/%s/messages?limit=200" % SID)
    seqs = [m["seq"] for m in body.get("messages", [])]
    check("全量 10 条且升序", seqs == list(range(1, 11)), str(seqs))
    check("取完了 has_more=False", body.get("has_more") is False, str(body.get("has_more")))

    msg = body["messages"][0]
    check("消息作者形状对", msg["author"]["type"] == "human" and msg["author"]["id"] == "<human-id>", str(msg.get("author")))
    check("作者显示名走映射", msg["author"]["display"] == "刚哥", str(msg.get("author")))
    check("消息带 created_at +08:00", str(msg.get("created_at", "")).endswith("+08:00"), str(msg.get("created_at")))

    print("\n== 6. 单会话详情 ==")
    status, body = call("/api/sessions/%s" % SID)
    check("详情 200", status == 200, str(status))
    check("members 带 display", body["session"]["members"] == [{"id": "lobster", "display": "龙虾"}], str(body["session"].get("members")))
    status, body = call("/api/sessions/dm_20260920_nosuch")
    check("不存在 -> 404 not_found", status == 404 and body.get("error", {}).get("code") == "not_found", str((status, body)))

    print("\n== 7. 未实现端点 -> 501（契约 §12.4）==")
    # POST /api/sessions、POST .../messages、PATCH /api/sessions/{sid} 已在
    # 契约 §I（临时群）实现，验证见第 10 段，从本清单移出。
    for method, path, payload in (
        ("POST", "/api/meetings", {"topic": "x"}),
        ("POST", "/api/sessions/%s/observers" % SID, None),
    ):
        status, body = call(path, method=method, body=payload)
        check("%s %s -> 501" % (method, path), status == 501, str(status))
    status, body = call("/api/stream")
    check("GET /api/stream -> 501", status == 501, str(status))

    print("\n== 8. append-only 真拦（直连 PG 试改）==")
    out = subprocess.run(PSQL + ["UPDATE chat_messages SET status='failed' WHERE session_id='%s'" % SID],
                         capture_output=True, text=True)
    check("UPDATE 被拒", out.returncode != 0 and "append-only" in (out.stderr or "").lower(), (out.stderr or "")[:120])
    out = subprocess.run(PSQL + ["DELETE FROM chat_messages WHERE session_id='%s'" % SID],
                         capture_output=True, text=True)
    check("DELETE 被拒", out.returncode != 0 and "append-only" in (out.stderr or "").lower(), (out.stderr or "")[:120])

    print("\n== 9. 库内与接口一致（真数据核对）==")
    db_max = sql("SELECT max(seq) FROM chat_messages WHERE session_id='%s'" % SID).strip()
    status, body = call("/api/sessions/%s/messages?limit=200" % SID)
    api_max = max(m["seq"] for m in body["messages"])
    check("库 max(seq) == 接口 max(seq)", str(api_max) == db_max, "db=%s api=%s" % (db_max, api_max))
    db_cnt = sql("SELECT count(*) FROM chat_messages WHERE session_id='%s'" % SID).strip()
    check("库 count == 接口条数", str(len(body["messages"])) == db_cnt, "db=%s api=%s" % (db_cnt, len(body["messages"])))

    group_phase()

    print("\n" + "=" * 52)
    print("PASS %d / FAIL %d" % (PASS, FAIL))
    print("=" * 52)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
