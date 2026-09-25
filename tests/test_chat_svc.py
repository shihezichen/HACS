#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# hacs-test-fixtures: 本文件为测试夹具，允许出现假凭据与网段边界值；危险类命中（真令牌/连接串/真实人名）仍按硬门禁拦截
"""
test_chat_svc.py —— chat_svc.py 的纯标准库 unittest（python tests/test_chat_svc.py 直接跑）

做的事（与 test_blackboard_svc.py 同一套手法）：
  1. 进程内起一个 ThreadingHTTPServer（只绑 127.0.0.1 的随机高端口，绝不占 9980）
  2. 注入**假的 DB / 名册 / 鉴权**，整套测试不需要真 PostgreSQL、不需要真登录
  3. 逐项断言契约 API契约-人机对话台-v1.md 的形状与纪律

覆盖清单（契约条款逐条对号）：
  ① /healthz 公开，未登录可访问，形状对
  ② 未登录访问 /api/* -> 401 unauthorized；信封是对象形状（§8）
  ③ tailnet 直连 = 免登录 owner（§3、§9）
  ④ **127.0.0.1 不豁免**（cloudflared 回环后门纪律）
  ⑤ /api/whoami 的 caps 随角色变（§4.1）
  ⑥ /api/agents 形状：id/display/online/busy/meta（§4.2）
  ⑦ /api/sessions：viewer 看不到 internal 会话（§3 无权即不可见）
  ⑧ /api/sessions/{sid}：无权 -> 404 not_found（**不泄露存在性**，§3）
  ⑨ /api/sessions/{sid}/messages：limit 夹在 1..200，before/after 互斥（§4.5）
  ⑩ **未实现的写端点 -> 501 not_implemented，不是 404**（§12.4）
  ⑪ 未知路径 -> 501（本版口径：不在已实现清单里就算未实现）
  ⑫ id 生成符合 §2 的形态：dm_yyyyMMdd_6位小写base32
  ⑬ 时间一律 ISO8601 带 +08:00
  ⑭ excerpt 截断到 80 字
"""

import datetime
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import types
import unittest
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import chat_svc as cs  # noqa: E402
import view_auth  # noqa: E402

# ---------------------------------------------------------------------------
# 测试夹具真值（硬编码清零 Phase 1.5）
# PHASE2_TODO: 待节点注入 env 后删除
# 默认值逐字等于改动前的真值；未注入 env 时测试行为与改动前完全等价。
FIX_MEMBER_CLOUD   = os.environ.get("AGENT_TEAM_TEST_MEMBER_CLOUD") or "lobster"
FIX_MEMBER_NB      = os.environ.get("AGENT_TEAM_TEST_MEMBER_NB") or "openclaw-nb"
FIX_MEMBER_QUANTUM = os.environ.get("AGENT_TEAM_TEST_MEMBER_QUANTUM") or "quantum"
FIX_NODE_LAPTOP    = os.environ.get("AGENT_TEAM_TEST_NODE_LAPTOP") or "thinkbook"
FIX_NODE_CLOUD_IP  = os.environ.get("AGENT_TEAM_TEST_NODE_CLOUD_IP") or "<host>"
FIX_NODE_LAPTOP_IP = os.environ.get("AGENT_TEAM_TEST_NODE_LAPTOP_IP") or "<node-ip>"
FIX_HUMAN_ID       = os.environ.get("AGENT_TEAM_HUMAN_ID") or "<human-id>"

UTC = datetime.timezone.utc
BJ = datetime.timezone(datetime.timedelta(hours=8))

NOW = datetime.datetime(2026, 9, 20, 10, 30, 0, tzinfo=UTC)


# ------------------------------------------------------------------ 假件

def _session_row(sid="dm_20260920_k7f3qa", kind="dm", title="与 龙虾 的对话 · 9月20日 18:20",
                 sensitivity="internal", members=None, created_by=FIX_HUMAN_ID,
                 created_at=NOW, updated_at=NOW, state="open",
                 ref_table=None, ref_id=None):
    return (sid, kind, title, sensitivity, members if members is not None else [FIX_MEMBER_CLOUD],
            created_by, created_at, updated_at, state, ref_table, ref_id)


def _message_row(mid="msg_20260920183000_abc123", sid="dm_20260920_k7f3qa", seq=1,
                 author_type="agent", author_id=FIX_MEMBER_CLOUD, kind="text",
                 text="连上了。", ref_table=None, ref_id=None, status="done",
                 reply_to=None, run=None, meta=None, created_at=NOW):
    return (mid, sid, seq, author_type, author_id, kind, text, ref_table, ref_id,
            status, reply_to, run, meta, created_at)


class FakeDB(object):
    """只实现 ChatDB 的语义方法（真 SQL 由 verify_chat_e2e.py 打真库验证）。"""

    def __init__(self, sessions=None, messages=None):
        self.sessions = sessions if sessions is not None else {}
        self.messages = messages if messages is not None else {}
        self.last_limit = None
        self.last_before = None
        self.last_after = None
        self.queries = []

    # ChatDB 语义接口
    def list_sessions(self, role, filters):
        out = []
        for row in self.sessions.values():
            if filters.get("kind") and row[1] != filters["kind"]:
                continue
            if filters.get("state") and row[8] != filters["state"]:
                continue
            if filters.get("member") and filters["member"] not in (row[4] or []):
                continue
            if not view_auth.role_can_see(role, row[3]):
                continue
            out.append(cs.ChatDB._row_to_session(self, row))
        return out

    def get_session(self, session_id, role):
        row = self.sessions.get(session_id)
        if row is None:
            return None
        if not view_auth.role_can_see(role, row[3]):
            return None
        return cs.ChatDB._row_to_session(self, row)

    def last_message(self, session_id):
        rows = sorted(self.messages.get(session_id, []), key=lambda r: r[2])
        return cs.ChatDB._row_to_message(self, rows[-1]) if rows else None

    def list_messages(self, session_id, limit, before_seq, after_seq):
        """假件按语义模拟三分支（真 SQL 由 verify_chat_e2e.py 打真库验证）。"""
        self.last_limit, self.last_before, self.last_after = limit, before_seq, after_seq
        rows = sorted(self.messages.get(session_id, []), key=lambda r: r[2])
        if before_seq is not None:
            rows = [r for r in rows if r[2] < before_seq]
            has_more = len(rows) > limit
            if has_more:
                rows = rows[-limit:]
        elif after_seq is not None:
            rows = [r for r in rows if r[2] > after_seq]
            has_more = len(rows) > limit
            if has_more:
                rows = rows[:limit]
        else:
            # 真 SQL 是"倒序取 limit+1 条再正序"，结果本就是升序，多取的那条在最老端
            rows = rows[-(limit + 1):]
            has_more = len(rows) > limit
            if has_more:
                rows = rows[-limit:]
        next_before = rows[0][2] if (rows and has_more) else None
        return [cs.ChatDB._row_to_message(self, r) for r in rows], has_more, next_before

    def query(self, sql, params=()):
        self.queries.append((sql, list(params)))
        if "roundtable_sessions" in sql:
            return []
        return []

    def close(self):
        pass


class FakeRoster(object):
    def __init__(self, members=None):
        self._members = members if members is not None else {
            FIX_MEMBER_CLOUD: {"id": FIX_MEMBER_CLOUD, "display": "龙虾", "node_id": FIX_MEMBER_CLOUD,
                        "host": FIX_NODE_CLOUD_IP, "model": "openclaw/default",
                        "description": FIX_MEMBER_CLOUD + " 助手（云）", "skills": ["general"]},
            "openclaw": {"id": "openclaw", "display": "OpenClaw", "node_id": FIX_NODE_LAPTOP,
                         "host": FIX_NODE_LAPTOP_IP, "model": "openclaw/default",
                         "description": "笔记本 OpenClaw", "skills": ["openclaw"]},
        }

    def get(self, member_id):
        return self._members.get(member_id)

    def all(self):
        return [dict(v) for v in self._members.values()]

    def ids(self):
        return list(self._members.keys())


class FakePrincipal(object):
    def __init__(self, username, role):
        self.username = username
        self.role = role


class FakeAuth(object):
    """check_cookie 只认一个魔法 cookie 值。"""

    def __init__(self, username=FIX_HUMAN_ID, role="owner"):
        self.username = username
        self.role = role
        self.key = b"0" * 32
        self.calls = 0

    def check_cookie(self, cookie_value, ip, ua):
        self.calls += 1
        if cookie_value == "valid-session-cookie":
            return FakePrincipal(self.username, self.role), "ok"
        return None, "bad_cookie"


# ------------------------------------------------------------------ 基类

class ChatServiceCase(unittest.TestCase):
    """起一个真 HTTP 服务，注入假件；默认把来访 IP 当成 tailnet。"""

    as_tailnet = True
    sessions = None
    messages = None

    @classmethod
    def setUpClass(cls):
        cls.db = FakeDB(sessions=cls.sessions, messages=cls.messages)
        cls.roster = FakeRoster()
        cls.auth = FakeAuth()
        cs.Runtime.reset()
        cs.Runtime.db = cls.db
        cs.Runtime.roster = cls.roster
        cs.Runtime.auth = cls.auth
        cs.Runtime.started_at = 0.0

        # 来访 IP 口径：默认全部按 tailnet 直连（免登录 owner），
        # 需要测"非 tailnet"的用例自己把下面这个开关翻掉。
        cls._orig_in_tailnet = view_auth.in_tailnet
        view_auth.in_tailnet = lambda ip: bool(cls.as_tailnet)

        cls.httpd = cs.ThreadingHTTPServer(("127.0.0.1", 0), cs.ChatHandler)
        cls.httpd.daemon_threads = True
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever)
        cls.thread.daemon = True
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        view_auth.in_tailnet = cls._orig_in_tailnet
        cs.Runtime.reset()

    # -- 工具 -------------------------------------------------------
    def url(self, path):
        return "http://127.0.0.1:%d%s" % (self.port, path)

    def call(self, path, method="GET", body=None, cookie=None, headers=None):
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.url(path), data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if cookie:
            req.add_header("Cookie", cookie)
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        try:
            resp = urllib.request.urlopen(req, timeout=10)
            return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8")
            try:
                return exc.code, json.loads(raw)
            except ValueError:
                return exc.code, {"raw": raw}

    def assertEnvelope(self, body, code):
        """契约 §8：错误一律对象信封。"""
        self.assertIn("ok", body)
        self.assertFalse(body["ok"])
        self.assertIn("error", body)
        self.assertEqual(body["error"].get("code"), code)


# ------------------------------------------------------------------ 1. 健康口

class TestHealthz(ChatServiceCase):
    def test_healthz_is_public_and_shaped(self):
        status, body = self.call("/healthz")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["service"], "agent-team-chat")
        self.assertEqual(body["version"], cs.VERSION)
        for key in ("db", "members", "server_time", "phase"):
            self.assertIn(key, body)
        self.assertEqual(body["members"], 2)


# ------------------------------------------------------------------ 2. 鉴权

class TestAuth(ChatServiceCase):
    as_tailnet = False

    def test_api_without_login_is_401(self):
        for path in ("/api/whoami", "/api/agents", "/api/sessions"):
            with self.subTest(path=path):
                status, body = self.call(path)
                self.assertEqual(status, 401)
                self.assertEnvelope(body, "unauthorized")

    def test_loopback_is_not_exempt(self):
        """127.0.0.1 不在免登录名单里：cloudflared 正是从回环回连本服务。"""
        self.assertFalse(view_auth.in_tailnet("127.0.0.1"))
        status, body = self.call("/api/whoami")
        self.assertEqual(status, 401)
        self.assertEnvelope(body, "unauthorized")

    def test_valid_cookie_gets_identity(self):
        status, body = self.call("/api/whoami", cookie="bbview_session=valid-session-cookie")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["user"]["name"], FIX_HUMAN_ID)
        self.assertEqual(body["user"]["role"], "owner")
        self.assertEqual(body["user"]["role_cn"], "主人")
        self.assertEqual(body["user"]["source"], "session")

    def test_bad_cookie_is_401(self):
        status, body = self.call("/api/whoami", cookie="bbview_session=nope")
        self.assertEqual(status, 401)
        self.assertEnvelope(body, "unauthorized")


class TestTailnet(ChatServiceCase):
    as_tailnet = True

    def test_tailnet_is_owner(self):
        status, body = self.call("/api/whoami")
        self.assertEqual(status, 200)
        self.assertEqual(body["user"]["source"], "tailnet")
        self.assertEqual(body["user"]["role"], "owner")
        self.assertTrue(body["caps"]["can_write"])
        self.assertTrue(body["caps"]["can_admin"])


class TestCaps(ChatServiceCase):
    as_tailnet = False

    def test_viewer_caps_are_read_only(self):
        self.auth.username, self.auth.role = "guest", "viewer"
        try:
            status, body = self.call("/api/whoami", cookie="bbview_session=valid-session-cookie")
            self.assertEqual(status, 200)
            self.assertFalse(body["caps"]["can_write"])
            self.assertFalse(body["caps"]["can_create_meeting"])
            self.assertTrue(body["caps"]["can_observe"])
            self.assertFalse(body["caps"]["can_admin"])
            self.assertEqual(body["user"]["role_cn"], "只读访客")
        finally:
            self.auth.username, self.auth.role = FIX_HUMAN_ID, "owner"

    def test_member_can_write_but_not_admin(self):
        self.auth.username, self.auth.role = "team", "member"
        try:
            status, body = self.call("/api/whoami", cookie="bbview_session=valid-session-cookie")
            self.assertEqual(status, 200)
            self.assertTrue(body["caps"]["can_write"])
            self.assertFalse(body["caps"]["can_admin"])
        finally:
            self.auth.username, self.auth.role = FIX_HUMAN_ID, "owner"


# ------------------------------------------------------------------ 3. agents

class TestAgents(ChatServiceCase):
    def test_agents_shape(self):
        status, body = self.call("/api/agents")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        ids = [a["id"] for a in body["agents"]]
        self.assertEqual(ids, sorted(ids), "成员按 id 排序，前端才好稳定渲染")
        self.assertIn(FIX_MEMBER_CLOUD, ids)
        lobster = [a for a in body["agents"] if a["id"] == FIX_MEMBER_CLOUD][0]
        for key in ("id", "display", "online", "busy", "last_hb_at", "meta"):
            self.assertIn(key, lobster)
        self.assertEqual(lobster["display"], "龙虾")
        # 本版不猜在线态：一律 None（v1.1 §B.1 要订阅 NATS 心跳才有）
        self.assertIsNone(lobster["online"])
        self.assertIsNone(lobster["busy"])


# ------------------------------------------------------------------ 4. sessions

def _demo_sessions():
    return {
        "dm_20260920_k7f3qa": _session_row(),
        "dm_20260920_pub001": _session_row(
            sid="dm_20260920_pub001", title="公开会话", sensitivity="public", members=["openclaw"]),
        "meet_20260920_x2m9bd": _session_row(
            sid="meet_20260920_x2m9bd", kind="meet", title="圆桌 · 9月20日",
            sensitivity="internal", members=[FIX_MEMBER_CLOUD, "openclaw"],
            ref_table="roundtable_sessions", ref_id="rt_20260920_aaa"),
    }


def _demo_messages():
    base = []
    for i in range(1, 8):
        base.append(_message_row(mid="msg_2026092018000%d_aaaaaa" % i, seq=i,
                                 text="第 %d 条" % i))
    return {"dm_20260920_k7f3qa": base}


class TestSessionsList(ChatServiceCase):
    as_tailnet = True
    sessions = _demo_sessions()
    messages = _demo_messages()

    def test_list_all_for_owner(self):
        status, body = self.call("/api/sessions")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["count"], 3)
        row = [s for s in body["sessions"] if s["session_id"] == "dm_20260920_k7f3qa"][0]
        # 形状：契约 §4.3
        for key in ("session_id", "kind", "title", "members", "sensitivity", "state",
                    "created_by", "created_at", "updated_at", "last_message",
                    "unread", "observing", "my_role"):
            self.assertIn(key, row)
        self.assertEqual(row["members"], [{"id": FIX_MEMBER_CLOUD, "display": "龙虾"}])
        self.assertEqual(row["last_message"]["seq"], 7)
        self.assertEqual(row["last_message"]["excerpt"], "第 7 条")
        self.assertEqual(row["my_role"], "owner")
        self.assertIsNone(row["unread"], "v1 不做服务端已读游标，初值给 None")
        self.assertNotIn("member_ids", row, "内部字段不该漏给前端")

    def test_kind_filter(self):
        status, body = self.call("/api/sessions?kind=meet")
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["sessions"][0]["session_id"], "meet_20260920_x2m9bd")

    def test_bad_kind_is_422(self):
        status, body = self.call("/api/sessions?kind=nope")
        self.assertEqual(status, 422)
        self.assertEnvelope(body, "bad_kind")

    def test_member_filter(self):
        status, body = self.call("/api/sessions?member=openclaw")
        self.assertEqual(body["count"], 2)

    def test_state_filter(self):
        status, body = self.call("/api/sessions?state=archived")
        self.assertEqual(body["count"], 0)

    def test_times_are_plus0800(self):
        status, body = self.call("/api/sessions")
        row = body["sessions"][0]
        self.assertTrue(row["created_at"].endswith("+08:00"), row["created_at"])
        self.assertTrue(row["updated_at"].endswith("+08:00"), row["updated_at"])
        self.assertEqual(row["created_at"], "2026-09-20T18:30:00+08:00")


class TestSessionsVisibility(ChatServiceCase):
    """viewer 只能看 public；无权即不可见（契约 §3）。"""

    as_tailnet = False
    sessions = _demo_sessions()
    messages = _demo_messages()

    def setUp(self):
        self.auth.username, self.auth.role = "guest", "viewer"

    def tearDown(self):
        self.auth.username, self.auth.role = FIX_HUMAN_ID, "owner"

    def test_viewer_only_sees_public(self):
        status, body = self.call("/api/sessions", cookie="bbview_session=valid-session-cookie")
        self.assertEqual(status, 200)
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["sessions"][0]["session_id"], "dm_20260920_pub001")

    def test_viewer_get_internal_is_404_not_403(self):
        status, body = self.call("/api/sessions/dm_20260920_k7f3qa",
                                 cookie="bbview_session=valid-session-cookie")
        self.assertEqual(status, 404, "无权要伪装成不存在，不泄露存在性")
        self.assertEnvelope(body, "not_found")

    def test_missing_session_is_404(self):
        status, body = self.call("/api/sessions/dm_20260920_nosuch",
                                 cookie="bbview_session=valid-session-cookie")
        self.assertEqual(status, 404)
        self.assertEnvelope(body, "not_found")


class TestSessionDetail(ChatServiceCase):
    sessions = _demo_sessions()
    messages = _demo_messages()

    def test_detail_ok(self):
        status, body = self.call("/api/sessions/dm_20260920_k7f3qa")
        self.assertEqual(status, 200)
        self.assertEqual(body["session"]["session_id"], "dm_20260920_k7f3qa")

    def test_meet_detail_carries_meeting_block(self):
        status, body = self.call("/api/sessions/meet_20260920_x2m9bd")
        self.assertEqual(status, 200)
        self.assertIn("meeting", body["session"])


# ------------------------------------------------------------------ 5. messages

class TestMessages(ChatServiceCase):
    sessions = _demo_sessions()
    messages = _demo_messages()

    def test_default_window(self):
        status, body = self.call("/api/sessions/dm_20260920_k7f3qa/messages")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["session_id"], "dm_20260920_k7f3qa")
        seqs = [m["seq"] for m in body["messages"]]
        self.assertEqual(seqs, sorted(seqs), "历史必须按 seq 升序")
        self.assertEqual(self.db.last_limit, cs.MSG_LIMIT_DEFAULT)
        self.assertFalse(body["has_more"])
        self.assertIsNone(body["next_before_seq"])

    def test_message_shape(self):
        status, body = self.call("/api/sessions/dm_20260920_k7f3qa/messages?limit=1")
        msg = body["messages"][0]
        for key in ("message_id", "session_id", "seq", "author", "kind", "text",
                    "status", "reply_to", "run", "meta", "created_at"):
            self.assertIn(key, msg)
        self.assertEqual(msg["author"]["type"], "agent")
        self.assertEqual(msg["author"]["id"], FIX_MEMBER_CLOUD)
        self.assertEqual(msg["author"]["display"], "龙虾")

    def test_limit_clamped_high(self):
        self.call("/api/sessions/dm_20260920_k7f3qa/messages?limit=9999")
        self.assertEqual(self.db.last_limit, cs.MSG_LIMIT_MAX)

    def test_limit_clamped_low(self):
        self.call("/api/sessions/dm_20260920_k7f3qa/messages?limit=0")
        self.assertEqual(self.db.last_limit, 1)

    def test_before_and_after_are_mutually_exclusive(self):
        status, body = self.call(
            "/api/sessions/dm_20260920_k7f3qa/messages?before_seq=5&after_seq=2")
        self.assertEqual(status, 422)
        self.assertEnvelope(body, "bad_seq")

    def test_non_integer_seq_is_422(self):
        status, body = self.call("/api/sessions/dm_20260920_k7f3qa/messages?before_seq=abc")
        self.assertEqual(status, 422)
        self.assertEnvelope(body, "bad_seq")

    def test_messages_of_unknown_session_is_404(self):
        status, body = self.call("/api/sessions/dm_20260920_nosuch/messages")
        self.assertEqual(status, 404)
        self.assertEnvelope(body, "not_found")


class TestPaging(ChatServiceCase):
    sessions = {"dm_20260920_k7f3qa": _session_row()}
    messages = {"dm_20260920_k7f3qa": [
        _message_row(mid="msg_20260920_%06d" % i, seq=i) for i in range(1, 11)]}

    def test_before_seq_walks_backwards(self):
        status, body = self.call("/api/sessions/dm_20260920_k7f3qa/messages?limit=3&before_seq=8")
        seqs = [m["seq"] for m in body["messages"]]
        self.assertEqual(seqs, [5, 6, 7], "before_seq 取 seq<8 的最近 3 条")
        self.assertTrue(body["has_more"])
        self.assertEqual(body["next_before_seq"], 5)

    def test_after_seq_walks_forwards(self):
        status, body = self.call("/api/sessions/dm_20260920_k7f3qa/messages?limit=3&after_seq=4")
        seqs = [m["seq"] for m in body["messages"]]
        self.assertEqual(seqs, [5, 6, 7], "after_seq 取 seq>4 的最早 3 条")
        self.assertTrue(body["has_more"])

    def test_no_more_when_exhausted(self):
        status, body = self.call("/api/sessions/dm_20260920_k7f3qa/messages?limit=50")
        self.assertEqual(len(body["messages"]), 10)
        self.assertFalse(body["has_more"])
        self.assertIsNone(body["next_before_seq"])


# ------------------------------------------------------------------ 6. 501

class TestNotImplemented(ChatServiceCase):
    def test_write_endpoints_are_501_not_404(self):
        """契约 §12.4：未实现的端点一律 501，前端据此开关按钮。"""
        cases = [
            # POST /api/sessions、POST .../messages、PATCH /api/sessions/{sid}
            # 三件已在契约 §I（临时群）实现并移出本清单 —— 见 TestGroupProtocol。
            ("DELETE", "/api/sessions/dm_20260920_k7f3qa/messages/msg_x", None),
            ("POST", "/api/meetings", {"topic": "x"}),
            ("POST", "/api/sessions/dm_20260920_k7f3qa/observers", None),
            ("POST", "/api/sessions/dm_20260920_k7f3qa/speak-requests", None),
        ]
        for method, path, body in cases:
            with self.subTest(method=method, path=path):
                status, resp = self.call(path, method=method, body=body)
                self.assertEqual(status, 501)
                self.assertEnvelope(resp, "not_implemented")

    def test_sse_endpoints_are_501_for_now(self):
        for path in ("/api/stream", "/api/sessions/dm_20260920_k7f3qa/stream"):
            with self.subTest(path=path):
                status, resp = self.call(path)
                self.assertEqual(status, 501)
                self.assertEnvelope(resp, "not_implemented")

    def test_unknown_path_is_501(self):
        status, resp = self.call("/api/nope")
        self.assertEqual(status, 501)
        self.assertEnvelope(resp, "not_implemented")

    def test_non_api_path_goes_to_static_not_api(self):
        """非 API 路径归同源静态托管（会议 §H 清单第 2 条），不再落 API 的 not_found。

        本类没有部署 ui/，所以静态兜底回 ui_not_deployed —— 这正是给前端
        「产物还没放上来」的可诊断信号。
        """
        status, resp = self.call("/favicon.ico")
        self.assertEqual(status, 404)
        self.assertEnvelope(resp, "ui_not_deployed")


# ------------------------------------------------------------------ 7. 纯函数

class TestHelpers(unittest.TestCase):
    def test_session_id_shape(self):
        sid = cs.gen_session_id("dm")
        self.assertRegex(sid, r"^dm_\d{8}_[a-z2-7]{6}$")

    def test_meet_session_id_shape(self):
        self.assertRegex(cs.gen_session_id("meet"), r"^meet_\d{8}_[a-z2-7]{6}$")

    def test_message_id_shape(self):
        self.assertRegex(cs.gen_message_id(), r"^msg_\d{14}_[a-z2-7]{6}$")

    def test_ids_are_random(self):
        self.assertNotEqual(cs.gen_session_id("dm"), cs.gen_session_id("dm"))

    def test_display_names(self):
        self.assertEqual(cs.display_of(FIX_MEMBER_CLOUD), "龙虾")
        self.assertEqual(cs.display_of("openclaw"), "OpenClaw")
        self.assertEqual(cs.display_of("nobody"), "nobody")
        self.assertEqual(cs.display_of(""), "")

    def test_excerpt_truncates(self):
        self.assertEqual(cs.excerpt("短"), "短")
        long_text = "字" * 200
        out = cs.excerpt(long_text)
        self.assertTrue(out.endswith("…"))
        self.assertEqual(len(out), cs.EXCERPT_LEN + 1)

    def test_excerpt_flattens_whitespace(self):
        self.assertEqual(cs.excerpt("a\n\n  b\tc"), "a b c")

    def test_excerpt_none(self):
        self.assertEqual(cs.excerpt(None), "")

    def test_clamp(self):
        self.assertEqual(cs.clamp("9999", 1, 200, 50), 200)
        self.assertEqual(cs.clamp("0", 1, 200, 50), 1)
        self.assertEqual(cs.clamp("abc", 1, 200, 50), 50)
        self.assertEqual(cs.clamp(None, 1, 200, 50), 50)

    def test_iso_bj(self):
        value = datetime.datetime(2026, 9, 20, 10, 30, 0, tzinfo=UTC)
        self.assertEqual(cs.iso_bj(value), "2026-09-20T18:30:00+08:00")
        self.assertIsNone(cs.iso_bj(None))

    def test_iso_bj_treats_naive_as_utc(self):
        naive = datetime.datetime(2026, 9, 20, 10, 30, 0)
        self.assertEqual(cs.iso_bj(naive), "2026-09-20T18:30:00+08:00")


class TestRosterPaths(unittest.TestCase):
    def test_default_when_env_empty(self):
        os.environ.pop(cs.ROSTER_ENV, None)
        self.assertEqual(cs.resolve_roster_paths(), list(cs.ROSTER_DEFAULTS))

    def test_env_list(self):
        os.environ[cs.ROSTER_ENV] = "/a/agents.json:/b/agents.json"
        try:
            self.assertEqual(cs.resolve_roster_paths(), ["/a/agents.json", "/b/agents.json"])
        finally:
            os.environ.pop(cs.ROSTER_ENV, None)


class TestRosterLoad(unittest.TestCase):
    def test_merges_and_keeps_first(self):
        import tempfile
        paths = []
        try:
            for payload in (
                {"node_id": FIX_NODE_LAPTOP, "host": FIX_NODE_LAPTOP_IP,
                 "agents": {"hermes": {"description": "Hermes 总控"}, FIX_MEMBER_CLOUD: {"description": "本机的"}}},
                {"node_id": FIX_MEMBER_CLOUD, "host": FIX_NODE_CLOUD_IP,
                 "agents": {FIX_MEMBER_CLOUD: {"description": "云上的"}, FIX_MEMBER_QUANTUM: {"description": "量子"}}},
            ):
                handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
                json.dump(payload, handle)
                handle.close()
                paths.append(handle.name)
            roster = cs.Roster(paths)
            self.assertEqual(sorted(roster.ids()), ["hermes", FIX_MEMBER_CLOUD, FIX_MEMBER_QUANTUM])
            self.assertEqual(roster.get(FIX_MEMBER_CLOUD)["description"], "本机的", "先读到的为准")
            self.assertEqual(roster.get(FIX_MEMBER_CLOUD)["display"], "龙虾", "显示名走补充表")
        finally:
            for path in paths:
                try:
                    os.unlink(path)
                except OSError:
                    pass

    def test_missing_file_is_not_fatal(self):
        roster = cs.Roster(["/definitely/not/here/agents.json"])
        self.assertEqual(roster.ids(), [])


# ------------------------------------------------------------------ 12. 同源静态托管（会议 §H · <MEMBER_ID> 8 条清单）

class RawCallMixin(object):
    """静态/HTML 用例要拿原始字节与响应头，不能只吃 JSON。"""

    def call_raw(self, path, method="GET", body=None, headers=None):
        req = urllib.request.Request(self.url(path), data=body, method=method)
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        try:
            resp = urllib.request.urlopen(req, timeout=10)
            return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()


class StaticCase(RawCallMixin, ChatServiceCase):
    """临时 ui/ 目录 + 真 HTTP，逐条打会议清单。"""

    @classmethod
    def _mk(cls, rel, text):
        full = os.path.join(cls.ui, rel)
        parent = os.path.dirname(full)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent)
        with open(full, "w", encoding="utf-8") as handle:
            handle.write(text)

    @classmethod
    def setUpClass(cls):
        cls.ui = tempfile.mkdtemp(prefix="chui_")
        cls._mk("index.html", "<!doctype html><title>team_chat</title><div id=app></div>")
        cls._mk("assets/app.js", "console.log(1)")
        cls._mk("assets/app.css", "body{color:#111}")
        cls._mk("serve.py", "# 开发辅助，对外不得可见")
        cls._mk("mock.js", "// mock 数据，对外不得可见")
        cls._mk("sub/index.html", "SUBINDEX")
        super(StaticCase, cls).setUpClass()          # 起服务（内部会 Runtime.reset）
        cs.Runtime.ui_dir = cls.ui

    @classmethod
    def tearDownClass(cls):
        super(StaticCase, cls).tearDownClass()
        cs.Runtime.ui_dir = None
        shutil.rmtree(cls.ui, ignore_errors=True)


class TestStaticHosting(StaticCase):
    def test_root_serves_index(self):
        status, headers, body = self.call_raw("/")
        self.assertEqual(status, 200)
        self.assertIn(b"team_chat", body)
        self.assertIn("text/html", headers.get("Content-Type", ""))

    def test_js_mime_is_application_javascript(self):
        status, headers, body = self.call_raw("/assets/app.js")
        self.assertEqual(status, 200)
        self.assertTrue(headers.get("Content-Type", "").startswith("application/javascript"),
                        "清单第 7 条：.js 必须是 application/javascript，实际 %r"
                        % headers.get("Content-Type"))

    def test_css_mime(self):
        status, headers, _ = self.call_raw("/assets/app.css")
        self.assertEqual(status, 200)
        self.assertIn("text/css", headers.get("Content-Type", ""))

    def test_index_is_no_cache(self):
        status, headers, _ = self.call_raw("/")
        self.assertEqual(status, 200)
        self.assertIn("no-cache", headers.get("Cache-Control", "").lower(),
                      "清单第 5 条：index.html 必须 no-cache")

    def test_assets_are_no_cache_in_v1(self):
        status, headers, _ = self.call_raw("/assets/app.js")
        self.assertEqual(status, 200)
        self.assertIn("no-cache", headers.get("Cache-Control", "").lower(),
                      "清单第 5 条：v1 阶段 js/css 也要 no-cache")

    def test_spa_fallback_returns_index_200(self):
        status, headers, body = self.call_raw("/some/deep/link")
        self.assertEqual(status, 200, "清单第 3 条：非 API 命中的 GET 回 index.html 200")
        self.assertIn(b"team_chat", body)

    def test_subdir_index_served(self):
        status, _, body = self.call_raw("/sub")
        self.assertEqual(status, 200)
        self.assertIn(b"SUBINDEX", body)

    def test_api_prefix_is_never_shadowed_by_static(self):
        status, headers, body = self.call_raw("/api/whoami")
        self.assertIn("application/json", headers.get("Content-Type", ""),
                      "清单第 2 条：/api/* 优先于静态")
        self.assertNotIn(b"STATIC", body)

    def test_healthz_beats_static(self):
        status, headers, _ = self.call_raw("/healthz")
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers.get("Content-Type", ""))

    def test_serve_py_is_not_served(self):
        status, _, body = self.call_raw("/serve.py")
        self.assertEqual(status, 404, "清单第 1 条：serve.py 不得对外")
        self.assertNotIn("开发辅助", body.decode("utf-8", "replace"))

    def test_mock_js_is_not_served(self):
        status, _, body = self.call_raw("/mock.js")
        self.assertEqual(status, 404, "清单第 1 条：mock.js 不得对外")
        self.assertNotIn("mock 数据", body.decode("utf-8", "replace"))

    def test_path_traversal_blocked(self):
        for evil in ("/../chat_svc.py", "/..%2fchat_svc.py", "/assets/../../chat_svc.py"):
            status, _, body = self.call_raw(evil)
            self.assertIn(status, (400, 404), "目录穿越必须挡住：%s -> %s" % (evil, status))
            self.assertNotIn(b"VIEWER_TOKEN", body)

    def test_missing_asset_is_404_not_index(self):
        """带静态扩展名的未命中回 404 —— 否则浏览器会把 HTML 当 JS 解析报 MIME 错。"""
        status, _, body = self.call_raw("/assets/missing.js")
        self.assertEqual(status, 404)
        self.assertNotIn(b"team_chat", body)

    def test_dotfile_hidden(self):
        status, _, _ = self.call_raw("/.env")
        self.assertEqual(status, 404)

    def test_head_on_static_has_no_body(self):
        status, headers, body = self.call_raw("/", method="HEAD")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")


class TestStaticWithoutUiDir(RawCallMixin, ChatServiceCase):
    """ui/ 还没放产物时：非 API 路径回 404，且要给出可诊断的信息。"""

    def test_missing_ui_dir_is_404_json(self):
        status, headers, body = self.call_raw("/")
        self.assertEqual(status, 404)
        payload = json.loads(body.decode("utf-8"))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "ui_not_deployed")


# ------------------------------------------------- 13. 临时群 · 群发言协议（契约 §I）
#
# 刚哥 2026-09-21 六条规则的可执行版本。这里测**服务端语义**
# （门禁 / @ 展开 / 可见性 / 开合）；真 SQL 由 verify_chat_e2e.py 打真库验证。

GRP = "meet_20260921_grp001"
SESSION_COOKIE_OK = "bbview_session=valid-session-cookie"
GROUP_MEMBERS = [FIX_HUMAN_ID, "hermes", FIX_MEMBER_NB]

GROUP_ROSTER = {
    "hermes": {"id": "hermes", "display": "Hermes", "node_id": FIX_MEMBER_CLOUD,
               "host": FIX_NODE_CLOUD_IP, "model": "test/na",
               "description": "总控", "skills": []},
    FIX_MEMBER_NB: {"id": FIX_MEMBER_NB, "display": "OpenClaw(本机)",
                    "node_id": FIX_NODE_LAPTOP, "host": FIX_NODE_LAPTOP_IP,
                    "model": "test/na", "description": "本机 OpenClaw", "skills": []},
}


def _group_row(sid=GRP, state="open", members=None, created_by=FIX_HUMAN_ID):
    return _session_row(sid=sid, kind="meet", title="临时群 · 对话台前后端分工",
                        members=list(members if members is not None else GROUP_MEMBERS),
                        created_by=created_by, state=state)


class GroupFakeDB(FakeDB):
    """群协议用到的写语义（建群 / 落消息 / 开合状态）。"""

    def create_session(self, sid, kind, title, members, created_by,
                       sensitivity="internal", client_ref=None):
        row = _session_row(sid=sid, kind=kind, title=title, members=list(members),
                           created_by=created_by, sensitivity=sensitivity)
        self.sessions[sid] = row
        return cs.ChatDB._row_to_session(self, row)

    def set_session_state(self, session_id, state):
        row = list(self.sessions[session_id])
        row[8] = state
        self.sessions[session_id] = tuple(row)
        return cs.ChatDB._row_to_session(self, tuple(row))

    def add_message(self, session_id, author_type, author_id, text,
                    mentions=None, audience=None, needs_response=None,
                    reply_to=None, client_msg_id=None):
        rows = self.messages.setdefault(session_id, [])
        seq = (max([r[2] for r in rows]) if rows else 0) + 1
        meta = {"mentions": list(mentions or []),
                "audience": list(audience or []),
                "needs_response": list(needs_response or []),
                "client_msg_id": client_msg_id}
        rows.append(_message_row(mid="msg_20260921000%03d_%s" % (seq, author_id),
                                 sid=session_id, seq=seq, author_type=author_type,
                                 author_id=author_id, text=text, meta=meta,
                                 reply_to=reply_to, status="done"))
        return cs.ChatDB._row_to_message(self, rows[-1])


class GroupBase(ChatServiceCase):
    """一个人 + 两个 Agent 的临时群；每个用例一套干净数据。"""

    def setUp(self):
        self.db = GroupFakeDB(sessions={GRP: _group_row()}, messages={})
        self.roster = FakeRoster(members=dict(GROUP_ROSTER))
        self.auth = FakeAuth(username=FIX_HUMAN_ID, role="owner")
        cs.Runtime.reset()
        cs.Runtime.db = self.db
        cs.Runtime.roster = self.roster
        cs.Runtime.auth = self.auth
        cs.Runtime.started_at = 0.0

    # -- 工具 -------------------------------------------------------

    def say(self, text, mentions=None, agent=None, cookie=None, sid=GRP):
        body = {"text": text}
        if mentions is not None:
            body["mentions"] = mentions
        headers = {"X-Agent-Id": agent} if agent else None
        return self.call("/api/sessions/%s/messages" % sid, method="POST",
                         body=body, cookie=cookie, headers=headers)

    def human_says(self, text, mentions=None):
        return self.say(text, mentions=mentions, cookie=SESSION_COOKIE_OK)

    def read(self, agent=None):
        headers = {"X-Agent-Id": agent} if agent else None
        return self.call("/api/sessions/%s/messages" % GRP, headers=headers)

class GroupCase(GroupBase):
    """群协议的服务端语义（契约 §I）。"""

    # -- 门禁 -------------------------------------------------------

    def test_agent_without_mention_is_422(self):
        """规则 6：Agent 不带 @ 直接发 → 拒收。"""
        status, body = self.say("我先建表了", agent="hermes")
        self.assertEqual(status, 422)
        self.assertEnvelope(body, "mention_required")

    def test_agent_mention_unknown_member_is_422(self):
        status, body = self.say("喂", mentions=["nobody"], agent="hermes")
        self.assertEqual(status, 422)
        self.assertEnvelope(body, "unknown_mention")

    def test_tailnet_without_agent_id_is_the_human(self):
        """tailnet 免登录 = 团队内部网（与看板同口径）：对端按默认人认。

        不这么认的话，刚哥从网页上进来既建不了群也发不了言 ——
        群协议就对发起人关门了。要以 Agent 名义发言仍须显式带 X-Agent-Id。
        """
        status, body = self.say("我是谁", mentions=["@all"])
        self.assertEqual(status, 201)
        self.assertEqual(body["message"]["author"]["id"], FIX_HUMAN_ID)
        self.assertEqual(body["message"]["author"]["type"], "human")

    def test_message_after_archive_is_409(self):
        self.db.set_session_state(GRP, "archived")
        status, body = self.say("还在吗", mentions=["@all"], agent="hermes")
        self.assertEqual(status, 409)
        self.assertEnvelope(body, "session_closed")

    # -- @ 展开（规则 2 / 3 / 6） ------------------------------------

    def test_human_without_mention_reaches_everyone(self):
        """规则 2：不带 @ ⇒ 发给全群，人人都要处理和回应。"""
        status, body = self.human_says("你们俩配合做对话台：Hermes 后台、OpenClaw 前台")
        self.assertEqual(status, 201)
        msg = body["message"]
        self.assertEqual(msg["author"]["type"], "human")
        self.assertEqual(msg["author"]["id"], FIX_HUMAN_ID)
        self.assertEqual(msg["meta"]["mentions"], ["@all"])
        self.assertCountEqual(msg["meta"]["audience"], GROUP_MEMBERS)
        # 自己不必回应自己
        self.assertCountEqual(msg["meta"]["needs_response"], ["hermes", FIX_MEMBER_NB])

    def test_human_mention_single_agent_reaches_only_him(self):
        """规则 3：@ 某人 ⇒ 只有他需要处理和回应。"""
        status, body = self.human_says("前台做到哪了", mentions=[FIX_MEMBER_NB])
        self.assertEqual(status, 201)
        msg = body["message"]
        self.assertEqual(msg["meta"]["mentions"], [FIX_MEMBER_NB])
        self.assertEqual(msg["meta"]["needs_response"], [FIX_MEMBER_NB])

    def test_agent_at_all_reaches_others(self):
        status, body = self.say("后台建表完成", mentions=["@all"], agent="hermes")
        self.assertEqual(status, 201)
        msg = body["message"]
        self.assertEqual(msg["author"]["type"], "agent")
        self.assertCountEqual(msg["meta"]["audience"], GROUP_MEMBERS)
        self.assertCountEqual(msg["meta"]["needs_response"], [FIX_HUMAN_ID, FIX_MEMBER_NB])

    def test_agent_mention_multi_reaches_those(self):
        status, body = self.say("接口冻结了", mentions=[FIX_HUMAN_ID, FIX_MEMBER_NB],
                                agent="hermes")
        self.assertEqual(status, 201)
        self.assertCountEqual(body["message"]["meta"]["needs_response"],
                              [FIX_HUMAN_ID, FIX_MEMBER_NB])

    # -- 可见性（规则 5） -------------------------------------------

    def test_every_member_sees_all_messages(self):
        self.human_says("开始")
        self.say("收到", mentions=[FIX_HUMAN_ID], agent="hermes")
        status, body = self.read(agent=FIX_MEMBER_NB)
        self.assertEqual(status, 200)
        self.assertEqual([m["author"]["id"] for m in body["messages"]],
                         [FIX_HUMAN_ID, "hermes"])

    # -- 待回应（规则 2 的落实） ------------------------------------

    def test_my_pending_lists_what_i_owe(self):
        self.human_says("你们两个配合做对话台")
        status, body = self.read(agent=FIX_MEMBER_NB)
        self.assertEqual(status, 200)
        self.assertEqual([p["seq"] for p in body.get("my_pending") or []], [1])

    def test_my_pending_is_empty_after_i_reply(self):
        self.human_says("你们两个配合做对话台")
        self.say("收到，我看前台", mentions=[FIX_HUMAN_ID], agent=FIX_MEMBER_NB)
        status, body = self.read(agent=FIX_MEMBER_NB)
        self.assertEqual(body.get("my_pending") or [], [])

    # -- 建群 / 关群（规则 4） --------------------------------------

    def test_create_group_includes_creator(self):
        status, body = self.call("/api/sessions", method="POST",
                                 cookie=SESSION_COOKIE_OK,
                                 body={"kind": "meet", "title": "临时群 · 分工",
                                       "members": ["hermes", FIX_MEMBER_NB]})
        self.assertEqual(status, 201)
        sess = body["session"]
        self.assertCountEqual(sess["member_ids"], GROUP_MEMBERS)
        self.assertEqual(sess["created_by"], FIX_HUMAN_ID)
        self.assertEqual(sess["state"], "open")

    def test_only_owner_can_close_group(self):
        """规则 4：群由刚哥发起，结束也只能他说。"""
        status, body = self.call("/api/sessions/%s" % GRP, method="PATCH",
                                 body={"state": "archived"})
        self.assertEqual(status, 403)
        self.assertEnvelope(body, "only_owner_can_close")

    def test_owner_closes_group(self):
        status, body = self.call("/api/sessions/%s" % GRP, method="PATCH",
                                 cookie=SESSION_COOKIE_OK,
                                 body={"state": "archived"})
        self.assertEqual(status, 200)
        self.assertEqual(body["session"]["state"], "archived")


class GroupOutsiderCase(GroupBase):
    """非群成员：无权即不可见（404，不泄露存在性）。"""

    as_tailnet = False

    def test_outsider_read_group_is_404(self):
        self.auth.username = "outsider"
        self.auth.role = "member"
        status, body = self.call("/api/sessions/%s" % GRP,
                                 cookie=SESSION_COOKIE_OK)
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
