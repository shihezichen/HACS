#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# hacs-test-fixtures: 本文件为测试夹具，允许出现假凭据与网段边界值；危险类命中（真令牌/连接串/真实人名）仍按硬门禁拦截
"""
test_blackboard_view.py —— blackboard_view.py 的纯标准库 unittest

做的事：
  1. 在进程内起一个**假黑板**（FakeUpstream，只绑 127.0.0.1 随机高端口），
     记录它收到的每一个请求（路径 / 查询参数 / 有没有 Authorization 头）
  2. 再起一个被测的看板服务，--upstream 指向这个假黑板
  3. 全程不联网、不碰真的 9940、不碰数据库、不装任何第三方包

覆盖清单（对应交付要求 1~8）：
  ① 正文含 <script>/"/& → 接口与页面输出都不含裸标签，且 JSON.parse 后正文不失真
  ② POST /api/entries -> 405；DELETE / -> 405（含 Allow 头与"只读"说明）
  ③ 上游连不上 / 超时 -> 502，且进程不崩（之后的请求照常 200）
  ④ limit=999 -> 传给上游的是 50；limit=abc -> 退回默认 20
  ⑤ 空 q 走 /entries；非空 q 走 /search
  ⑥ 上游 401 -> 本服务 502，且错误体里没有 token
  ⑦ 源码自检：没有 psycopg2、没有 method="POST" 之类写操作
  ⑧ 页面里没有任何外部资源引用（只有 /api/ 相对路径）
另加：HEAD、/healthz、404、日志不含 token、clamp_limit 边界、上游 500。

v1.2 追加（会议四要素 + 跟踪页签）：
  ⑨ /api/tracking、/api/actions、/api/issues 透传：入参夹取、overdue 开关归一、
     上游故障 -> 502、写方法 -> 405（含 POST /api/tracking）
  ⑩ 四要素（goal/agenda_items/confirmations/actions/issues）照样转义、token 不外泄
  ⑪ 老会议（上游标 degraded）仍能正常返回，不当成故障
  ⑫ 页面壳子：三个页签、跟踪表元素、逾期红标样式、北京时间换算、空数据文案
"""

import calendar
import json
import os
import socket
import sys
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import blackboard_view as bv  # noqa: E402

TOKEN = "unit-test-token-DO-NOT-LEAK-6f3a1c"

# v1.5 起白板本尊挪到 /board，根路径让给 webPortal 卡片墙 —— 所有"取看板那张
# 大页面"的用例统一走这个常量，别再写死 "/"。
BOARD_PATH = bv.BOARD_PATH

# 各 Agent 往黑板里写的"不可信正文"：脚本标签 + 引号 + & + 换行
NASTY = '<script>alert(1)</script> & "引号" \'单引号\' <img src=x onerror=alert(2)>\n第二行'

# ---------------------------------------------------------------------------
# 测试夹具真值（硬编码清零 Phase 1.5）
# PHASE2_TODO: 待节点注入 env 后删除
# 默认值逐字等于改动前的真值；未注入 env 时测试行为与改动前完全等价。
FIX_MEMBER_CLOUD = os.environ.get("AGENT_TEAM_TEST_MEMBER_CLOUD") or "lobster"
FIX_MEMBER_TOP001 = os.environ.get("AGENT_TEAM_TEST_MEMBER_TOP001") or "top001"
FIX_NODE_CLOUD_IP = os.environ.get("AGENT_TEAM_TEST_NODE_CLOUD_IP") or "<host>"
FIX_ETC_DIR = os.environ.get("AGENT_TEAM_ETC_DIR") or "<deploy-dir>"

HEALTH = {
    "ok": True, "version": "1.1", "node": FIX_MEMBER_CLOUD,
    "db": {"ok": True, "latency_ms": 15},
    "entries": {"total": 12},
    "embed": {"available": True, "model": "BAAI/bge-small-zh-v1.5",
              "dim": 512, "loaded": True, "last_error": None},
}


def entry(entry_id=1, content="普通内容", score=None):
    row = {"entry_id": entry_id, "ts": "2026-09-19T11:18:36Z", "author": "hermes",
           "topic": "infra", "kind": "announce", "content": content,
           "refs": [{"entry_id": 3}], "status": "active"}
    if score is not None:
        row["score"] = score
    return row


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


# --- 会议（P5）：问题、发言正文、refs 全都是不可信内容 ---------------

SESSION_LIST = [
    {"session_id": "rt-20260919-120000-aaaa", "topic": "p5", "question": NASTY,
     "moderator": "hermes", "members": ["hermes", FIX_MEMBER_CLOUD, FIX_MEMBER_TOP001],
     "state": "converged", "round_no": 3, "max_rounds": 4,
     "started_at": "2026-09-19T11:00:00Z", "ended_at": "2026-09-19T11:20:00Z",
     "turns_total": 9, "speakers_total": 3, "conclusion_entry": 77},
    {"session_id": "rt-20260919-110000-bbbb", "topic": "p5", "question": "要不要上异地副本？",
     "moderator": FIX_MEMBER_CLOUD, "members": ["hermes", FIX_MEMBER_CLOUD],
     "state": "running", "round_no": 1, "max_rounds": 4,
     "started_at": "2026-09-19T10:00:00Z", "ended_at": None,
     "turns_total": 2, "speakers_total": 2, "conclusion_entry": None},
    # UTC 16:30 = 北京 9/20 00:30 —— 专门用来验"日历按北京时间切日"，不是按 UTC
    {"session_id": "rt-20260920-003000-cccc", "topic": "p6", "question": "跨零点的那一场",
     "moderator": FIX_MEMBER_TOP001, "members": ["hermes", FIX_MEMBER_TOP001],
     "state": "converged", "round_no": 2, "max_rounds": 4,
     "started_at": "2026-09-19T16:30:00Z", "ended_at": "2026-09-19T17:10:00Z",
     "turns_total": 4, "speakers_total": 2, "conclusion_entry": 88},
]

SESSION_TURNS = [
    {"turn_id": 1, "round_no": 1, "seq": 1, "speaker": "hermes", "status": "ok",
     "kind": "opinion", "stance": "support", "vote": "continue", "confidence": 0.8,
     "content": NASTY, "refs": [{"type": "file", "value": "<script>alert(3)</script>"}],
     "entry_id": 61, "error": None, "ts": "2026-09-19T11:01:00Z", "elapsed_ms": 1200},
    {"turn_id": 2, "round_no": 1, "seq": 2, "speaker": FIX_MEMBER_CLOUD, "status": "absent",
     "kind": None, "stance": None, "vote": None, "confidence": None,
     "content": None, "refs": [], "entry_id": None,
     "error": "点名探针无应答，判定离线", "ts": "2026-09-19T11:01:30Z", "elapsed_ms": 6000},
]


# --- v1.2：会议四要素 + 跨会跟踪（正文照样是各 Agent 写的不可信内容）---
#
# overdue 由上游算好后透传，看板不自己比时间；这里刻意给一条 true、一条 false，
# 用来验"红标只跟着上游的 overdue 走"。

AGENDA_ITEMS = [
    {"item_id": 11, "session_id": "rt-20260919-120000-aaaa", "seq": 1,
     "title": NASTY, "expected": "给个数", "conclusion": "每天 03:00 一次",
     "conclusion_entry": 61, "status": "concluded", "note": None},
    {"item_id": 12, "session_id": "rt-20260919-120000-aaaa", "seq": 2,
     "title": "要不要异地副本", "expected": "定方案", "conclusion": None,
     "conclusion_entry": None, "status": "deferred", "note": "等预算"},
]

CONFIRMATIONS = [
    {"confirm_id": 21, "session_id": "rt-20260919-120000-aaaa", "target": "overall",
     "member": "hermes", "verdict": "agree", "note": None, "ts": "2026-09-19T11:19:00Z"},
    {"confirm_id": 22, "session_id": "rt-20260919-120000-aaaa", "target": "overall",
     "member": FIX_MEMBER_TOP001, "verdict": "object", "note": NASTY, "ts": "2026-09-19T11:19:10Z"},
]

TRACK_ACTIONS = [
    {"action_id": 31, "session_id": "rt-20260919-120000-aaaa", "agenda_seq": 1,
     "what": NASTY, "owner": FIX_MEMBER_CLOUD, "due_at": "2020-01-01T00:00:00Z", "status": "open",
     "evidence": None, "note": None, "created_at": "2026-09-19T11:20:00Z",
     "closed_at": None, "closed_by": None, "session_topic": "p5", "overdue": True},
    {"action_id": 32, "session_id": "rt-20260919-120000-aaaa", "agenda_seq": 1,
     "what": "做一次恢复演练", "owner": "hermes", "due_at": "2099-01-01T00:00:00Z",
     "status": "doing", "evidence": None, "note": None,
     "created_at": "2026-09-19T11:20:00Z", "closed_at": None, "closed_by": None,
     "session_topic": "p5", "overdue": False},
]

TRACK_ISSUES = [
    {"issue_id": 41, "session_id": "rt-20260919-120000-aaaa", "title": NASTY,
     "detail": "预算没批", "owner": FIX_MEMBER_CLOUD, "status": "open",
     "due_at": "2020-01-01T00:00:00Z", "next_check_at": "2020-02-01T00:00:00Z",
     "refs": [{"type": "entry", "value": 61}], "created_at": "2026-09-19T11:20:00Z",
     "closed_at": None, "closed_by": None, "note": None, "session_topic": "p5",
     "overdue": True},
]

TRACK_COUNTS = {"actions_open": 2, "actions_overdue": 1,
                "issues_open": 1, "issues_overdue": 1}

CONFIRM_SUMMARY = {"agree": ["hermes", FIX_MEMBER_CLOUD], "object": [FIX_MEMBER_TOP001], "abstain": [],
                   "absent": [], "required": 3, "passed": False}


# --- D2：共享内容（与会议是两回事）-----------------------------------
#
# 这两个串一旦出现在任何响应体里就是泄密：它们只属于 sensitive + 未授权的那两条。
SECRET_TITLE = "主人关键信息-手机号档案-DO-NOT-LEAK-9c2f"
SECRET_BODY = "主人手机号 13800000000，家庭住址……（这段正文永远不该出浏览器）"

SHARED_PERMANENT = [
    {"shared_id": 101, "scope": "permanent", "kind": "node", "title": NASTY,
     "body": "龙虾 " + FIX_NODE_CLOUD_IP + " / " + FIX_MEMBER_TOP001 + " 100.x.x.x", "tags": ["infra", "节点"],
     "owner": "hermes", "updated_at": "2026-09-18T03:00:00Z",
     "expires_at": "2026-09-20T00:00:00Z", "review_at": "2026-09-01T00:00:00Z",
     "version": 3, "sensitive": False, "authorized": True, "revoked": False,
     "expired": False, "expiring_soon": True, "review_due": True,
     "history": [{"version": 3, "by": "hermes", "at": "2026-09-18T03:00:00Z",
                  "fields": ["body", "tags"]},
                 {"version": 2, "by": FIX_MEMBER_CLOUD, "at": "2026-09-10T03:00:00Z",
                  "fields": ["title"]}]},
    {"shared_id": 102, "scope": "permanent", "kind": "owner_key_info",
     "title": SECRET_TITLE, "body": SECRET_BODY, "tags": ["私密", "主人"],
     "owner": FIX_MEMBER_CLOUD, "updated_at": "2026-09-17T03:00:00Z",
     "expires_at": None, "review_at": "2026-12-01T00:00:00Z", "version": 1,
     "sensitive": True, "authorized": False, "revoked": False,
     "history": [{"version": 1, "by": FIX_MEMBER_CLOUD, "at": "2026-09-17T03:00:00Z",
                  "fields": ["body"]}]},
    {"shared_id": 103, "scope": "permanent", "kind": "config", "title": "已撤下的旧配置",
     "body": "老网关配置", "tags": [], "owner": FIX_MEMBER_TOP001,
     "updated_at": "2026-08-01T03:00:00Z", "expires_at": "2026-09-01T00:00:00Z",
     "review_at": None, "version": 5, "sensitive": False, "authorized": True,
     "revoked": True, "expired": True, "history": []},
]

SHARED_TASKS = [
    {"task_id": "T-D2", "title": "D2 看板重构", "owner": FIX_MEMBER_TOP001, "status": "active",
     "ttl_expire_at": "2026-09-20T00:00:00Z",
     "destroy_when": "任务验收通过后 24 小时自动销毁",
     "items": [
         {"shared_id": 201, "scope": "task", "task_id": "T-D2", "kind": "other",
          "title": "本次重构的口径备忘", "body": NASTY, "tags": ["d2"],
          "owner": "hermes", "updated_at": "2026-09-19T03:00:00Z",
          "expires_at": "2026-09-20T00:00:00Z", "review_at": None, "version": 2,
          "sensitive": False, "authorized": True, "revoked": False, "history": []},
         {"shared_id": 202, "scope": "task", "task_id": "T-D2", "kind": "owner_profile",
          "title": SECRET_TITLE, "body": SECRET_BODY, "tags": ["私密"],
          "owner": FIX_MEMBER_CLOUD, "updated_at": "2026-09-19T03:30:00Z",
          "expires_at": None, "review_at": None, "version": 1,
          "sensitive": True, "authorized": False, "revoked": False,
          "history": [{"version": 1, "by": FIX_MEMBER_CLOUD, "at": "2026-09-19T03:30:00Z",
                       "fields": ["body"]}]},
     ]},
    {"task_id": "T-OLD", "title": "已结束的老任务", "owner": "hermes", "status": "done",
     "ttl_expire_at": "2026-09-01T00:00:00Z", "destroy_when": "任务结束即销毁",
     "destroyed": True, "items": []},
]


# --- 假上游 ---------------------------------------------------------

class FakeState(object):
    """假黑板的行为开关 + 请求流水（测试之间在 setUp 里重置）。"""

    mode = "ok"          # ok / 401 / 500 / hang / badjson
    hang_seconds = 1.5
    calls = []           # [(path, query_dict), ...]
    legacy_session = False   # True = 模拟"老会议"（库里还没有四要素那几列）
    shared_missing = False   # True = 模拟"上游还没上线 /shared 端点"（回 404）


class FakeUpstream(BaseHTTPRequestHandler):
    server_version = "fake_blackboard/0"
    sys_version = ""

    def log_message(self, fmt, *args):
        return

    def _json(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        split = urllib.parse.urlsplit(self.path)
        query = {k: v[0] for k, v in urllib.parse.parse_qs(split.query).items()}
        FakeState.calls.append((split.path, query, self.headers.get("Authorization")))

        if FakeState.mode == "hang":
            time.sleep(FakeState.hang_seconds)
        if FakeState.mode == "401":
            self._json(401, {"ok": False, "error": "unauthorized"})
            return
        if FakeState.mode == "500":
            self._json(500, {"ok": False, "error": "internal"})
            return
        if FakeState.mode == "badjson":
            body = b"<html>not json</html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if split.path == "/healthz":
            self._json(200, HEALTH)
        elif split.path == "/entries":
            self._json(200, {"ok": True, "entries": [entry(1), entry(2, NASTY)], "count": 2})
        elif split.path == "/search":
            self._json(200, {"ok": True, "mode": "semantic",
                             "entries": [entry(7, NASTY, score=0.8123)], "count": 1})
        elif split.path == "/sessions":
            self._json(200, {"ok": True, "count": len(SESSION_LIST),
                             "sessions": SESSION_LIST})
        elif split.path.startswith("/sessions/"):
            sid = split.path[len("/sessions/"):]
            if sid == "rt-missing":
                self._json(404, {"ok": False, "error": "not_found",
                                 "detail": "没有这场会议"})
                return
            session = dict(SESSION_LIST[0])
            session["session_id"] = urllib.parse.unquote(sid)
            session["conclusion_text"] = NASTY
            if FakeState.legacy_session:
                # 老会议：库里还没有 goal/agenda 这些列，上游据实以告（degraded）
                self._json(200, {"ok": True, "session": session,
                                 "turns": SESSION_TURNS, "count": len(SESSION_TURNS),
                                 "agenda_items": [], "confirmations": [],
                                 "actions": [], "issues": [],
                                 "degraded": ["session_goal", "agenda_items"],
                                 "now": "2026-09-19T12:00:00+00:00", "tz": "UTC"})
                return
            session["goal"] = NASTY
            session["agenda"] = ["备份频率", "异地副本"]
            session["confirmed_at"] = "2026-09-19T11:19:30Z"
            session["confirm_summary"] = CONFIRM_SUMMARY
            self._json(200, {"ok": True, "session": session,
                             "turns": SESSION_TURNS, "count": len(SESSION_TURNS),
                             "agenda_items": AGENDA_ITEMS, "confirmations": CONFIRMATIONS,
                             "actions": TRACK_ACTIONS, "issues": TRACK_ISSUES,
                             "degraded": [], "now": "2026-09-19T12:00:00+00:00",
                             "tz": "UTC"})
        elif split.path == "/tracking":
            self._json(200, {"ok": True, "actions": TRACK_ACTIONS, "issues": TRACK_ISSUES,
                             "counts": TRACK_COUNTS, "degraded": [],
                             "now": "2026-09-19T12:00:00+00:00", "tz": "UTC"})
        elif split.path == "/actions":
            self._json(200, {"ok": True, "count": len(TRACK_ACTIONS),
                             "actions": TRACK_ACTIONS, "degraded": [],
                             "now": "2026-09-19T12:00:00+00:00", "tz": "UTC"})
        elif split.path == "/issues":
            self._json(200, {"ok": True, "count": len(TRACK_ISSUES),
                             "issues": TRACK_ISSUES, "degraded": [],
                             "now": "2026-09-19T12:00:00+00:00", "tz": "UTC"})
        elif split.path in ("/shared", "/shared/search"):
            if FakeState.shared_missing:
                self._json(404, {"ok": False, "error": "not_found"})
                return
            self._json(200, {"ok": True, "items": SHARED_PERMANENT,
                             "tasks": SHARED_TASKS, "count": len(SHARED_PERMANENT),
                             "degraded": [], "now": "2026-09-19T12:00:00+00:00",
                             "tz": "UTC"})
        else:
            self._json(404, {"ok": False, "error": "not_found"})


UPSTREAM = None
VIEW = None
UPSTREAM_PORT = 0
VIEW_PORT = 0


def setUpModule():
    global UPSTREAM, VIEW, UPSTREAM_PORT, VIEW_PORT
    UPSTREAM = ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstream)
    UPSTREAM.daemon_threads = True
    UPSTREAM_PORT = UPSTREAM.server_address[1]
    threading.Thread(target=UPSTREAM.serve_forever, daemon=True).start()

    bv.Runtime.upstream = "http://127.0.0.1:%d" % UPSTREAM_PORT
    bv.Runtime.token = TOKEN
    bv.Runtime.token_loaded = True
    bv.Runtime.refresh_seconds = 30

    VIEW = bv.make_server("127.0.0.1", 0)
    VIEW_PORT = VIEW.server_address[1]
    threading.Thread(target=VIEW.serve_forever, daemon=True).start()


def tearDownModule():
    for server in (VIEW, UPSTREAM):
        if server is not None:
            server.shutdown()
            server.server_close()


def call(path, method="GET", data=None):
    """打被测服务一枪，返回 (状态码, 原始字节, 响应头 dict)。"""
    url = "http://127.0.0.1:%d%s" % (VIEW_PORT, path)
    req = urllib.request.Request(url, data=data, method=method)
    try:
        resp = urllib.request.urlopen(req, timeout=10)
    except urllib.error.HTTPError as exc:
        body = exc.read()
        headers = dict(exc.headers)
        exc.close()
        return exc.code, body, headers
    body = resp.read()
    headers = dict(resp.headers)
    code = resp.getcode()
    resp.close()
    return code, body, headers


class BaseCase(unittest.TestCase):
    def setUp(self):
        FakeState.mode = "ok"
        FakeState.hang_seconds = 1.5
        FakeState.calls = []
        FakeState.legacy_session = False
        FakeState.shared_missing = False
        bv.Runtime.upstream = "http://127.0.0.1:%d" % UPSTREAM_PORT
        bv.Runtime.token = TOKEN

    def last_call(self):
        self.assertTrue(FakeState.calls, "上游一次都没被调用")
        return FakeState.calls[-1]


# --- ① 转义 ---------------------------------------------------------

class TestEscaping(BaseCase):
    def test_api_body_has_no_bare_script_tag(self):
        code, body, _ = call("/api/entries")
        self.assertEqual(code, 200)
        self.assertNotIn(b"<script>", body)
        self.assertNotIn(b"<script", body)
        self.assertNotIn(b"<img", body)
        self.assertNotIn(b"<", body)          # 整个响应体里一个裸尖括号都没有
        self.assertNotIn(b"&", body)
        self.assertIn(b"\\u003cscript\\u003e", body)

    def test_escaped_json_round_trips_without_loss(self):
        """转义只是字节层面的：JSON.parse（=json.loads）解出来必须是原文。"""
        code, body, _ = call("/api/entries")
        self.assertEqual(code, 200)
        payload = json.loads(body.decode("utf-8"))
        contents = [e["content"] for e in payload["entries"]]
        self.assertIn(NASTY, contents)

    def test_search_results_also_escaped(self):
        code, body, _ = call("/api/search?q=%E6%B5%8B%E8%AF%95")
        self.assertEqual(code, 200)
        self.assertNotIn(b"<script>", body)
        self.assertIn(b"\\u003c", body)

    def test_page_does_not_embed_any_entry_content(self):
        """页面是静态壳子：正文一个字节都不由服务端拼进 HTML。"""
        call("/api/entries")                 # 先让上游数据流过一遍
        code, body, headers = call(BOARD_PATH)
        self.assertEqual(code, 200)
        page = body.decode("utf-8")
        self.assertNotIn("alert(1)", page)
        self.assertNotIn("alert(2)", page)
        self.assertNotIn("onerror", page)
        # 页面自己只有一个 <script>（应用逻辑），没有第二个来自数据的
        self.assertEqual(page.count("<script"), 1)
        self.assertIn("text/html", headers.get("Content-Type", ""))

    def test_response_has_csp_and_nosniff(self):
        """v1.4 起 CSP 要放开自带样式/脚本（登录页要能渲染），但对象/框架/表单/祖先仍锁死。"""
        _, _, headers = call(BOARD_PATH)
        csp = headers.get("Content-Security-Policy", "")
        self.assertIn("default-src 'self'", csp)
        self.assertIn("object-src 'none'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertIn("form-action 'self'", csp)
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")


# --- ② 只读：写方法一律 405 -----------------------------------------

class TestReadOnly(BaseCase):
    def test_post_api_entries_405(self):
        code, body, headers = call("/api/entries", method="POST", data=b'{"content":"x"}')
        self.assertEqual(code, 405)
        self.assertIn("GET", headers.get("Allow", ""))
        payload = json.loads(body.decode("utf-8"))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "method_not_allowed")
        self.assertIn("只读", payload["detail"])

    def test_delete_root_405(self):
        code, body, _ = call("/", method="DELETE")
        self.assertEqual(code, 405)
        self.assertIn("只读", json.loads(body.decode("utf-8"))["detail"])

    def test_put_and_patch_405(self):
        for method in ("PUT", "PATCH"):
            code, _, _ = call("/api/entries", method=method, data=b"{}")
            self.assertEqual(code, 405, "%s 应该被拒" % method)

    def test_write_methods_never_touch_upstream(self):
        call("/api/entries", method="POST", data=b"{}")
        call("/", method="DELETE")
        self.assertEqual(FakeState.calls, [], "只读服务不该因为写请求去碰上游")

    def test_get_still_works_after_rejected_writes(self):
        call("/api/entries", method="POST", data=b'{"content":"x"}')
        code, _, _ = call("/api/entries")
        self.assertEqual(code, 200)


# --- ③ 上游不可用 ---------------------------------------------------

class TestUpstreamFailures(BaseCase):
    def test_connection_refused_gives_502(self):
        bv.Runtime.upstream = "http://127.0.0.1:%d" % free_port()   # 没人监听的端口
        code, body, _ = call("/api/entries")
        self.assertEqual(code, 502)
        payload = json.loads(body.decode("utf-8"))
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["error"].startswith("upstream_"))

    def test_timeout_gives_502(self):
        FakeState.mode = "hang"
        FakeState.hang_seconds = 1.5
        original = bv.UPSTREAM_TIMEOUT_S
        bv.UPSTREAM_TIMEOUT_S = 0.4          # 别让单测真等 5 秒
        try:
            started = time.time()
            code, body, _ = call("/api/health")
        finally:
            bv.UPSTREAM_TIMEOUT_S = original
        self.assertEqual(code, 502)
        self.assertLess(time.time() - started, 1.4, "超时没有按 UPSTREAM_TIMEOUT_S 生效")
        self.assertTrue(json.loads(body.decode("utf-8"))["error"].startswith("upstream_"))

    def test_upstream_500_gives_502(self):
        FakeState.mode = "500"
        code, body, _ = call("/api/entries")
        self.assertEqual(code, 502)
        self.assertEqual(json.loads(body.decode("utf-8"))["upstream_status"], 500)

    def test_upstream_non_json_gives_502(self):
        FakeState.mode = "badjson"
        code, body, _ = call("/api/entries")
        self.assertEqual(code, 502)
        self.assertEqual(json.loads(body.decode("utf-8"))["error"], "upstream_bad_json")

    def test_process_survives_and_recovers(self):
        """连着打几种故障，服务不能崩：恢复后照常 200。"""
        bv.Runtime.upstream = "http://127.0.0.1:%d" % free_port()
        for _ in range(3):
            self.assertEqual(call("/api/entries")[0], 502)
        bv.Runtime.upstream = "http://127.0.0.1:%d" % UPSTREAM_PORT
        FakeState.mode = "500"
        self.assertEqual(call("/api/entries")[0], 502)
        FakeState.mode = "ok"
        code, body, _ = call("/api/entries")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body.decode("utf-8"))["count"], 2)

    def test_page_still_renders_when_upstream_down(self):
        bv.Runtime.upstream = "http://127.0.0.1:%d" % free_port()
        code, body, _ = call(BOARD_PATH)
        self.assertEqual(code, 200)
        self.assertIn("banner", body.decode("utf-8"))   # 黄条元素在页面里备着


# --- ④ limit 夹取 ---------------------------------------------------

class TestLimitClamp(BaseCase):
    def test_limit_999_clamped_to_50(self):
        call("/api/entries?limit=999")
        _, query, _ = self.last_call()
        self.assertEqual(query.get("limit"), "50")

    def test_limit_zero_and_negative_clamped_to_1(self):
        call("/api/entries?limit=0")
        self.assertEqual(self.last_call()[1].get("limit"), "1")
        call("/api/entries?limit=-5")
        self.assertEqual(self.last_call()[1].get("limit"), "1")

    def test_limit_abc_falls_back_to_default(self):
        call("/api/entries?limit=abc")
        self.assertEqual(self.last_call()[1].get("limit"), str(bv.DEFAULT_LIMIT))

    def test_limit_clamped_on_search_too(self):
        call("/api/search?q=hi&limit=999")
        path, query, _ = self.last_call()
        self.assertEqual(path, "/search")
        self.assertEqual(query.get("limit"), "50")

    def test_clamp_limit_unit(self):
        self.assertEqual(bv.clamp_limit(None), bv.DEFAULT_LIMIT)
        self.assertEqual(bv.clamp_limit(""), bv.DEFAULT_LIMIT)
        self.assertEqual(bv.clamp_limit("abc"), bv.DEFAULT_LIMIT)
        self.assertEqual(bv.clamp_limit("999"), bv.MAX_LIMIT)
        self.assertEqual(bv.clamp_limit("0"), 1)
        self.assertEqual(bv.clamp_limit("7"), 7)
        self.assertEqual(bv.MAX_LIMIT, 50)


# --- ⑤ q 的路由 -----------------------------------------------------

class TestSearchRouting(BaseCase):
    def test_empty_q_goes_to_entries(self):
        code, _, _ = call("/api/search?q=")
        self.assertEqual(code, 200)
        self.assertEqual(self.last_call()[0], "/entries")

    def test_whitespace_q_goes_to_entries(self):
        call("/api/search?q=%20%20")
        self.assertEqual(self.last_call()[0], "/entries")

    def test_missing_q_goes_to_entries(self):
        call("/api/search")
        self.assertEqual(self.last_call()[0], "/entries")

    def test_nonempty_q_goes_to_search(self):
        call("/api/search?q=%E9%BE%99%E8%99%BE%20%E5%A4%87%E4%BB%BD")
        path, query, _ = self.last_call()
        self.assertEqual(path, "/search")
        self.assertEqual(query.get("q"), "龙虾 备份")

    def test_entries_passes_through_filters(self):
        call("/api/entries?topic=infra&kind=announce&author=hermes&since=2026-09-01")
        path, query, _ = self.last_call()
        self.assertEqual(path, "/entries")
        self.assertEqual(query.get("topic"), "infra")
        self.assertEqual(query.get("kind"), "announce")
        self.assertEqual(query.get("author"), "hermes")
        self.assertEqual(query.get("since"), "2026-09-01")

    def test_health_passes_through(self):
        code, body, _ = call("/api/health")
        self.assertEqual(code, 200)
        payload = json.loads(body.decode("utf-8"))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["entries"]["total"], 12)
        self.assertEqual(self.last_call()[0], "/healthz")


# --- ⑥ token 不外泄 -------------------------------------------------

class TestTokenNeverLeaks(BaseCase):
    def test_upstream_401_becomes_502_without_token(self):
        FakeState.mode = "401"
        code, body, headers = call("/api/entries")
        self.assertEqual(code, 502)
        text = body.decode("utf-8")
        self.assertNotIn(TOKEN, text)
        self.assertNotIn("Bearer " + TOKEN, text)
        payload = json.loads(text)
        self.assertEqual(payload["upstream_status"], 401)
        self.assertIn("凭据", payload["detail"])
        self.assertNotIn(TOKEN, json.dumps(headers))

    def test_token_is_sent_to_upstream_only(self):
        call("/api/entries")
        _, _, auth = self.last_call()
        self.assertEqual(auth, "Bearer " + TOKEN)      # 上游确实收到了
        code, body, headers = call("/api/entries")
        self.assertNotIn(TOKEN, body.decode("utf-8"))  # 但浏览器侧一个字都看不到
        self.assertNotIn(TOKEN, json.dumps(headers))

    def test_token_absent_from_page_and_healthz(self):
        for path in ("/", "/healthz"):
            _, body, _ = call(path)
            self.assertNotIn(TOKEN, body.decode("utf-8"), "%s 里出现了 token" % path)

    def test_token_absent_from_logs(self):
        import logging

        records = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(self.format(record))

        handler = Capture()
        handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        bv.LOG.addHandler(handler)
        bv.LOG.setLevel(logging.INFO)
        try:
            FakeState.mode = "401"
            call("/api/entries")
            call("/api/health")
            call("/api/entries", method="POST", data=b"{}")
        finally:
            bv.LOG.removeHandler(handler)
        self.assertTrue(records, "一条日志都没打出来")
        joined = "\n".join(records)
        self.assertNotIn(TOKEN, joined)
        self.assertNotIn("Bearer", joined)
        self.assertIn("-> 502", joined)

    def test_scrub_masks_token_anywhere(self):
        self.assertNotIn(TOKEN, bv._scrub("boom: Bearer %s failed" % TOKEN))
        self.assertNotIn(TOKEN, bv._scrub("raw %s" % TOKEN))
        self.assertLessEqual(len(bv._scrub("x" * 5000)), bv.DETAIL_MAX + 3)


# --- ⑦ 源码自检 -----------------------------------------------------

class TestSourceIsReadOnly(BaseCase):
    @classmethod
    def setUpClass(cls):
        path = os.path.join(os.path.dirname(HERE), "blackboard_view.py")
        with open(path, "r", encoding="utf-8") as fh:
            cls.src = fh.read()

    def test_no_database_driver(self):
        for forbidden in ("psycopg2", "sqlite3", "pymysql", "BB_PG_DSN"):
            self.assertNotIn(forbidden, self.src, "源码里不该出现 %s" % forbidden)

    def test_no_write_http_methods(self):
        for forbidden in ('method="POST"', "method='POST'",
                          'method="PUT"', 'method="PATCH"', 'method="DELETE"',
                          "urlopen(req, data", "Request(url, data"):
            self.assertNotIn(forbidden, self.src, "源码里不该出现 %s" % forbidden)

    def test_no_sql_write_statements(self):
        upper = self.src.upper()
        for forbidden in ("INSERT INTO", "DELETE FROM", "UPDATE SET", " COMMIT("):
            self.assertNotIn(forbidden, upper, "源码里不该出现 %s" % forbidden)

    def test_no_third_party_imports(self):
        stdlib_ok = {
            "argparse", "json", "logging", "os", "re", "socket", "sys", "time",
            "urllib", "http", "typing",
            "ipaddress",     # 标准库；--trust-proxy 的 CIDR 判断用它，不引第三方
            "view_auth",     # 本仓自己的模块（账号/会话/角色/审计），不是第三方依赖
        }
        for line in self.src.splitlines():
            stripped = line.strip()
            if stripped.startswith("import ") and not stripped.startswith("import ("):
                module = stripped.split()[1].split(".")[0]
                self.assertIn(module, stdlib_ok, "第三方依赖：%s" % stripped)
            elif stripped.startswith("from ") and " import " in stripped:
                module = stripped.split()[1].split(".")[0]
                self.assertIn(module, stdlib_ok, "第三方依赖：%s" % stripped)

    def test_does_declare_write_method_handlers(self):
        """反过来确认：写方法不是"没实现"，是**显式拒绝**。"""
        for name in ("do_POST", "do_PUT", "do_PATCH", "do_DELETE", "_reject_write"):
            self.assertIn(name, self.src)


# --- ⑧ 页面自包含 ---------------------------------------------------

class TestPageIsSelfContained(BaseCase):
    def setUp(self):
        BaseCase.setUp(self)
        self.page = call(BOARD_PATH)[1].decode("utf-8")

    def test_no_external_resources(self):
        for forbidden in ("http://", "https://", "//cdn", "//fonts", "@import", "<link"):
            self.assertNotIn(forbidden, self.page, "页面引用了外部资源：%s" % forbidden)

    def test_only_relative_api_calls(self):
        self.assertIn('api("/api/health")', self.page)
        self.assertIn('"/api/entries?limit="', self.page)
        self.assertIn('"/api/search?q="', self.page)

    def test_inline_css_and_js(self):
        self.assertIn("<style>", self.page)
        self.assertIn("<script>", self.page)
        self.assertIn("__CFG_JSON__", bv.PAGE_HTML)          # 模板里是占位符
        self.assertNotIn("__CFG_JSON__", self.page)          # 渲染后被替换掉

    def test_refresh_seconds_injected(self):
        bv.Runtime.refresh_seconds = 45
        try:
            page = bv.render_page()
            self.assertIn('"refresh": 45', page)
        finally:
            bv.Runtime.refresh_seconds = 30

    def test_renders_with_textcontent_not_innerhtml(self):
        """条目正文只能走 textContent；innerHTML 只允许用来清空容器。"""
        for line in self.page.splitlines():
            code = line.split("//")[0]                 # 去掉行内注释再看
            if "innerHTML" not in code:
                continue
            self.assertIn('innerHTML = ""', code,
                          "innerHTML 只能用于清空容器，实际：%r" % line.strip())

    def test_page_mentions_readonly(self):
        self.assertIn("只读", self.page)


# --- 其它杂项 -------------------------------------------------------

class TestMisc(BaseCase):
    def test_head_root_has_no_body(self):
        code, body, headers = call("/", method="HEAD")
        self.assertEqual(code, 200)
        self.assertEqual(body, b"")
        self.assertNotEqual(headers.get("Content-Length"), "0")

    def test_local_healthz(self):
        code, body, _ = call("/healthz")
        self.assertEqual(code, 200)
        payload = json.loads(body.decode("utf-8"))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["service"], "blackboard_view")
        self.assertTrue(payload["token_loaded"])

    def test_unknown_path_404(self):
        code, body, _ = call("/nope")
        self.assertEqual(code, 404)
        self.assertEqual(json.loads(body.decode("utf-8"))["error"], "not_found")

    def test_clamp_refresh(self):
        self.assertEqual(bv.clamp_refresh(0), 0)
        self.assertEqual(bv.clamp_refresh(-3), 0)
        self.assertEqual(bv.clamp_refresh(1), bv.MIN_REFRESH_S)
        self.assertEqual(bv.clamp_refresh(30), 30)

    def test_parser_defaults(self):
        args = bv.build_parser().parse_args([])
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, 9941)
        self.assertEqual(args.upstream, "http://" + FIX_NODE_CLOUD_IP + ":9940")
        self.assertEqual(args.token_file, FIX_ETC_DIR + "/client-token")
        self.assertEqual(args.refresh_seconds, 30)

    def test_main_rejects_bad_upstream_scheme(self):
        rc = bv.main(["--upstream", "file:///etc/passwd"])
        self.assertEqual(rc, 2)

    def test_json_bytes_escapes(self):
        body = bv.json_bytes({"content": "a<b>c&d"})
        self.assertNotIn(b"<", body)
        self.assertEqual(json.loads(body.decode("utf-8"))["content"], "a<b>c&d")


# --- P5：会议视图 ---------------------------------------------------

class TestSessionsPassthrough(BaseCase):
    def test_sessions_list_passes_through(self):
        code, body, _ = call("/api/sessions")
        self.assertEqual(code, 200)
        payload = json.loads(body.decode("utf-8"))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["count"], 3)
        self.assertEqual(payload["sessions"][0]["session_id"], "rt-20260919-120000-aaaa")
        self.assertEqual(self.last_call()[0], "/sessions")

    def test_sessions_filters_pass_through(self):
        call("/api/sessions?state=converged&topic=p5&since=2026-09-01")
        path, query, _ = self.last_call()
        self.assertEqual(path, "/sessions")
        self.assertEqual(query.get("state"), "converged")
        self.assertEqual(query.get("topic"), "p5")
        self.assertEqual(query.get("since"), "2026-09-01")

    def test_sessions_limit_is_clamped(self):
        call("/api/sessions?limit=999")
        self.assertEqual(self.last_call()[1].get("limit"), "50")
        call("/api/sessions?limit=abc")
        self.assertEqual(self.last_call()[1].get("limit"), str(bv.DEFAULT_LIMIT))
        call("/api/sessions?limit=0")
        self.assertEqual(self.last_call()[1].get("limit"), "1")

    def test_session_detail_passes_through(self):
        code, body, _ = call("/api/sessions/rt-20260919-120000-aaaa")
        self.assertEqual(code, 200)
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(payload["session"]["session_id"], "rt-20260919-120000-aaaa")
        self.assertEqual(len(payload["turns"]), 2)
        self.assertEqual(self.last_call()[0], "/sessions/rt-20260919-120000-aaaa")

    def test_upstream_404_becomes_404_not_502(self):
        """"没这场会议"是业务事实，不能跟"上游挂了"混成 502。"""
        code, body, _ = call("/api/sessions/rt-missing")
        self.assertEqual(code, 404)
        payload = json.loads(body.decode("utf-8"))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "not_found")

    def test_upstream_500_on_sessions_still_502(self):
        FakeState.mode = "500"
        self.assertEqual(call("/api/sessions")[0], 502)
        self.assertEqual(call("/api/sessions/anything")[0], 502)

    def test_sessions_unreachable_gives_502(self):
        bv.Runtime.upstream = "http://127.0.0.1:%d" % free_port()
        code, body, _ = call("/api/sessions")
        self.assertEqual(code, 502)
        self.assertTrue(json.loads(body.decode("utf-8"))["error"].startswith("upstream_"))

    def test_session_id_is_url_encoded_towards_upstream(self):
        call("/api/sessions/" + urllib.parse.quote("a/b?c=1", safe=""))
        path, _query, _ = self.last_call()
        self.assertEqual(path, "/sessions/a%2Fb%3Fc%3D1")

    def test_token_reaches_upstream_but_not_the_browser(self):
        code, body, headers = call("/api/sessions")
        self.assertEqual(code, 200)
        self.assertEqual(self.last_call()[2], "Bearer " + TOKEN)
        self.assertNotIn(TOKEN, body.decode("utf-8"))
        self.assertNotIn(TOKEN, json.dumps(headers))

    def test_write_methods_on_sessions_are_405(self):
        for method, path in (("POST", "/api/sessions"), ("DELETE", "/api/sessions/x"),
                             ("PUT", "/api/sessions"), ("PATCH", "/api/sessions/x")):
            code, _, _ = call(path, method=method, data=b"{}")
            self.assertEqual(code, 405, "%s %s 应该被拒" % (method, path))
        self.assertEqual(FakeState.calls, [], "写请求不该碰上游")


class TestSessionsEscaping(BaseCase):
    def test_session_list_body_has_no_bare_tags(self):
        code, body, _ = call("/api/sessions")
        self.assertEqual(code, 200)
        self.assertNotIn(b"<script", body)
        self.assertNotIn(b"<", body)
        self.assertNotIn(b"&", body)
        self.assertIn(b"\\u003cscript\\u003e", body)

    def test_turn_content_and_refs_are_escaped(self):
        code, body, _ = call("/api/sessions/rt-20260919-120000-aaaa")
        self.assertEqual(code, 200)
        self.assertNotIn(b"<script", body)
        self.assertNotIn(b"<img", body)
        self.assertNotIn(b"<", body)
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(payload["turns"][0]["content"], NASTY)
        self.assertEqual(payload["turns"][0]["refs"][0]["value"],
                         "<script>alert(3)</script>", "refs 转义后仍须原样解回")
        self.assertEqual(payload["session"]["conclusion_text"], NASTY)

    def test_page_does_not_embed_any_session_content(self):
        call("/api/sessions")
        call("/api/sessions/rt-20260919-120000-aaaa")
        page = call(BOARD_PATH)[1].decode("utf-8")
        self.assertNotIn("alert(1)", page)
        self.assertNotIn("alert(3)", page)
        self.assertNotIn("rt-20260919-120000-aaaa", page)
        self.assertEqual(page.count("<script"), 1)


class TestMeetingTab(BaseCase):
    def setUp(self):
        BaseCase.setUp(self)
        self.page = call(BOARD_PATH)[1].decode("utf-8")

    def test_two_tabs_exist(self):
        """v1.3：一级导航是三个模块，黑板流水降级成会议模块里的「完整留痕」。"""
        self.assertIn('data-mod="meet"', self.page)
        self.assertIn('data-sub="board"', self.page)
        self.assertIn(">会议<", self.page)
        self.assertIn(">完整留痕<", self.page)

    def test_meeting_pane_elements_exist(self):
        for marker in ('id="pane-meet"', 'id="sub-cal"', 'id="sub-board"',
                       'id="m-list"', 'id="m-detail"', 'id="m-state"', 'id="m-q"'):
            self.assertIn(marker, self.page, "页面缺少 %s" % marker)

    def test_meeting_api_is_called_relatively(self):
        self.assertIn('"/api/sessions?limit=50"', self.page)
        self.assertIn('api("/api/sessions/"', self.page)

    def test_state_badges_have_four_distinct_styles(self):
        for state in ("running", "converged", "timeout", "aborted"):
            self.assertIn(".badge." + state, self.page, "%s 没有专属徽标样式" % state)
        for status in ("ok", "absent", "error"):
            self.assertIn(".badge." + status, self.page)

    def test_empty_states_are_present(self):
        self.assertIn("暂无会议记录", self.page)
        self.assertIn("本场暂无发言记录", self.page)
        self.assertIn("尚未产出结论", self.page)

    def test_round_grouping_and_entry_link(self):
        self.assertIn('"第 " + current + " 轮"', self.page)
        self.assertIn("jumpToEntry", self.page)
        self.assertIn('"黑板 #"', self.page)

    def test_refresh_keeps_tab_and_open_session(self):
        self.assertIn("function reloadCurrent()", self.page)
        self.assertIn('state.mod === "meet"', self.page)
        self.assertIn("if (state.sid) loadDetail(state.sid);", self.page)

    def test_page_still_has_no_external_resources(self):
        for forbidden in ("http://", "https://", "//cdn", "//fonts", "@import", "<link"):
            self.assertNotIn(forbidden, self.page, "页面引用了外部资源：%s" % forbidden)

    def test_meeting_rendering_uses_textcontent_only(self):
        for line in self.page.splitlines():
            code = line.split("//")[0]
            if "innerHTML" not in code:
                continue
            self.assertIn('innerHTML = ""', code,
                          "innerHTML 只能用于清空容器，实际：%r" % line.strip())


# --- v1.2：跟踪端点（跨会落实 / 遗留）-------------------------------

class TestTrackingPassthrough(BaseCase):
    def test_tracking_passes_through_with_counts(self):
        code, body, _ = call("/api/tracking")
        self.assertEqual(code, 200)
        payload = json.loads(body.decode("utf-8"))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["counts"], TRACK_COUNTS)
        self.assertEqual(len(payload["actions"]), 2)
        self.assertEqual(len(payload["issues"]), 1)
        self.assertEqual(self.last_call()[0], "/tracking")

    def test_overdue_flag_is_computed_upstream_not_here(self):
        """逾期与否直接用上游算好的 overdue，看板不自己比时间。"""
        payload = json.loads(call("/api/tracking")[1].decode("utf-8"))
        flags = dict((a["action_id"], a["overdue"]) for a in payload["actions"])
        self.assertIs(flags[31], True)
        self.assertIs(flags[32], False)
        self.assertIs(payload["issues"][0]["overdue"], True)

    def test_tracking_limit_is_clamped(self):
        call("/api/tracking?limit=999")
        self.assertEqual(self.last_call()[1].get("limit"), "50")
        call("/api/tracking?limit=abc")
        self.assertEqual(self.last_call()[1].get("limit"), str(bv.MAX_LIMIT))
        call("/api/tracking?limit=0")
        self.assertEqual(self.last_call()[1].get("limit"), "1")

    def test_overdue_switch_is_normalized_to_0_or_1(self):
        call("/api/tracking?overdue=on")
        self.assertEqual(self.last_call()[1].get("overdue"), "1")
        call("/api/tracking?overdue=1")
        self.assertEqual(self.last_call()[1].get("overdue"), "1")
        call("/api/tracking")
        self.assertIsNone(self.last_call()[1].get("overdue"), "没开开关就别往上游传")
        call("/api/tracking?overdue=whatever")
        self.assertIsNone(self.last_call()[1].get("overdue"), "看不懂的值当没开")

    def test_actions_and_issues_pass_filters_through(self):
        call("/api/actions?status=open&owner=" + FIX_MEMBER_CLOUD + "&overdue=1&limit=5")
        path, query, _ = self.last_call()
        self.assertEqual(path, "/actions")
        self.assertEqual(query.get("status"), "open")
        self.assertEqual(query.get("owner"), FIX_MEMBER_CLOUD)
        self.assertEqual(query.get("overdue"), "1")
        self.assertEqual(query.get("limit"), "5")
        code, body, _ = call("/api/issues?status=watching")
        self.assertEqual(code, 200)
        self.assertEqual(self.last_call()[0], "/issues")
        self.assertEqual(len(json.loads(body.decode("utf-8"))["issues"]), 1)

    def test_tracking_does_not_forward_status(self):
        """/tracking 两张表的状态枚举不一样，status 不该被转发上去（上游会 400）。"""
        call("/api/tracking?status=doing")
        self.assertIsNone(self.last_call()[1].get("status"))

    def test_tracking_upstream_failures_become_502(self):
        FakeState.mode = "500"
        for path in ("/api/tracking", "/api/actions", "/api/issues"):
            self.assertEqual(call(path)[0], 502, path)
        FakeState.mode = "ok"
        bv.Runtime.upstream = "http://127.0.0.1:%d" % free_port()
        code, body, _ = call("/api/tracking")
        self.assertEqual(code, 502)
        self.assertTrue(json.loads(body.decode("utf-8"))["error"].startswith("upstream_"))

    def test_write_methods_on_tracking_are_405(self):
        for method, path in (("POST", "/api/tracking"), ("PUT", "/api/tracking"),
                             ("PATCH", "/api/actions"), ("DELETE", "/api/issues")):
            code, body, headers = call(path, method=method, data=b"{}")
            self.assertEqual(code, 405, "%s %s 应该被拒" % (method, path))
            self.assertIn("GET", headers.get("Allow", ""))
            self.assertIn("只读", json.loads(body.decode("utf-8"))["detail"])
        self.assertEqual(FakeState.calls, [], "写请求不该碰上游")

    def test_tracking_token_reaches_upstream_but_not_the_browser(self):
        code, body, headers = call("/api/tracking")
        self.assertEqual(code, 200)
        self.assertEqual(self.last_call()[2], "Bearer " + TOKEN)
        self.assertNotIn(TOKEN, body.decode("utf-8"))
        self.assertNotIn(TOKEN, json.dumps(headers))

    def test_tracking_content_is_escaped(self):
        code, body, _ = call("/api/tracking")
        self.assertEqual(code, 200)
        self.assertNotIn(b"<script", body)
        self.assertNotIn(b"<", body)
        self.assertNotIn(b"&", body)
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(payload["actions"][0]["what"], NASTY, "转义后必须原样解回")
        self.assertEqual(payload["issues"][0]["title"], NASTY)


class TestFourElementsPassthrough(BaseCase):
    def detail(self, sid="rt-20260919-120000-aaaa"):
        code, body, _ = call("/api/sessions/" + sid)
        self.assertEqual(code, 200)
        return json.loads(body.decode("utf-8"))

    def test_detail_carries_four_elements(self):
        payload = self.detail()
        session = payload["session"]
        self.assertEqual(session["goal"], NASTY)
        self.assertEqual(session["agenda"], ["备份频率", "异地副本"])
        self.assertEqual(session["confirm_summary"], CONFIRM_SUMMARY)
        self.assertIn("2026-09-19", session["confirmed_at"])
        self.assertEqual(len(payload["agenda_items"]), 2)
        self.assertEqual(len(payload["confirmations"]), 2)
        self.assertEqual(len(payload["actions"]), 2)
        self.assertEqual(len(payload["issues"]), 1)
        self.assertEqual(payload["degraded"], [])

    def test_four_elements_are_escaped(self):
        code, body, _ = call("/api/sessions/rt-20260919-120000-aaaa")
        self.assertEqual(code, 200)
        self.assertNotIn(b"<script", body)
        self.assertNotIn(b"<img", body)
        self.assertNotIn(b"<", body)
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(payload["session"]["goal"], NASTY)
        self.assertEqual(payload["agenda_items"][0]["title"], NASTY)
        self.assertEqual(payload["confirmations"][1]["note"], NASTY)

    def test_legacy_session_still_renders(self):
        """老会议（上游报 degraded）必须照常 200，字段缺就是缺，别把看板打崩。"""
        FakeState.legacy_session = True
        payload = self.detail()
        self.assertTrue(payload["ok"])
        self.assertNotIn("goal", payload["session"])
        self.assertEqual(payload["agenda_items"], [])
        self.assertEqual(payload["degraded"], ["session_goal", "agenda_items"])
        self.assertEqual(len(payload["turns"]), 2, "发言照常能看")


class TestTrackingTab(BaseCase):
    """跟踪页签与会议四要素在页面上的落点（页面是静态壳子，这里查的是壳子齐不齐）。"""

    def setUp(self):
        BaseCase.setUp(self)
        self.page = call(BOARD_PATH)[1].decode("utf-8")

    def test_three_tabs_exist(self):
        """三个模块：会议 / 共享内容 / 跟踪。"""
        for marker in ('data-mod="meet"', 'data-mod="shared"', 'data-mod="track"'):
            self.assertIn(marker, self.page)
        for name in (">会议<", ">共享内容<", ">跟踪<"):
            self.assertIn(name, self.page)

    def test_track_pane_elements_exist(self):
        for marker in ('id="pane-track"', 'id="k-actions"', 'id="k-issues"',
                       'id="k-owner"', 'id="k-status"', 'id="k-overdue"',
                       'id="k-aopen"', 'id="k-alate"', 'id="k-iopen"', 'id="k-ilate"'):
            self.assertIn(marker, self.page, "页面缺少 %s" % marker)

    def test_track_api_is_called_relatively(self):
        self.assertIn('"/api/tracking?limit=50"', self.page)
        self.assertIn("function loadTracking()", self.page)
        self.assertIn('state.mod === "shared"', self.page)

    def test_counts_summary_is_on_top(self):
        for text in ("落实未闭环", "落实逾期", "遗留未闭环", "遗留逾期"):
            self.assertIn(text, self.page)

    def test_overdue_rows_are_marked_red(self):
        self.assertIn(".trow.late", self.page, "逾期行要有专门的红色样式")
        self.assertIn('"已逾期"', self.page)
        self.assertIn('a.overdue ? "trow late" : "trow"', self.page)
        self.assertIn('it.overdue ? "trow late" : "trow"', self.page)

    def test_meeting_four_elements_are_rendered(self):
        for fn in ("function renderAgenda(", "function renderConfirm(",
                   "function actionRow(", "function issueRow("):
            self.assertIn(fn, self.page, "页面缺少 %s" % fn)
        for text in ("会议目标：", "议程", "总体结论与当场确认", "落实事项", "遗留问题"):
            self.assertIn(text, self.page)

    def test_agenda_and_verdict_badges_have_styles(self):
        for status in ("pending", "concluded", "deferred"):
            self.assertIn(".badge." + status, self.page, "%s 没有专属徽标样式" % status)
        for verdict in ("agree", "object", "abstain"):
            self.assertIn(".badge." + verdict, self.page)
        self.assertIn(".badge.late", self.page)

    def test_passed_is_never_faked(self):
        """未确认 / 未通过一律标黄（vote fail），只有 passed === true 才是绿的。"""
        self.assertIn("passed === true", self.page)
        self.assertIn("未通过（当场确认没过）", self.page)
        self.assertIn("未确认（没有当场确认记录）", self.page)
        self.assertIn(".vote.pass", self.page)
        self.assertIn(".vote.fail", self.page)

    def test_times_are_beijing_and_labelled(self):
        self.assertIn("北京时间", self.page)
        self.assertIn("8 * 3600 * 1000", self.page, "换算到 UTC+8 的那一行没了")
        self.assertIn("getUTCHours", self.page, "必须用 UTC 取值再加 8 小时")

    def test_empty_states_are_present(self):
        for text in ("（未记录目标/议程）", "暂无落实事项", "暂无遗留问题"):
            self.assertIn(text, self.page)

    def test_track_tab_still_self_contained(self):
        for forbidden in ("http://", "https://", "//cdn", "//fonts", "@import", "<link"):
            self.assertNotIn(forbidden, self.page, "页面引用了外部资源：%s" % forbidden)
        self.assertEqual(self.page.count("<script"), 1)

    def test_track_rendering_uses_textcontent_only(self):
        for line in self.page.splitlines():
            code = line.split("//")[0]
            if "innerHTML" not in code:
                continue
            self.assertIn('innerHTML = ""', code,
                          "innerHTML 只能用于清空容器，实际：%r" % line.strip())

    def test_version_is_1_5(self):
        """v1.5：根路径改成 webPortal 卡片墙，白板挪到 /board —— 版本号跟着走。"""
        self.assertEqual(bv.VERSION, "1.5")
        payload = json.loads(call("/healthz")[1].decode("utf-8"))
        self.assertEqual(payload["version"], "1.5")


# --- D2：会议日历 ---------------------------------------------------

def beijing_day(ts):
    """与页面 dayKey() 同一套口径：先 +8 小时，再取日期。日历就是按这个切日的。"""
    epoch = calendar.timegm(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")) + 8 * 3600
    return time.strftime("%Y-%m-%d", time.gmtime(epoch))


class TestCalendarModule(BaseCase):
    """会议模块 = 月历 + 当天会议列表 + 详情面板。"""

    def setUp(self):
        BaseCase.setUp(self)
        self.page = call(BOARD_PATH)[1].decode("utf-8")

    def test_calendar_elements_exist(self):
        for marker in ('id="cal"', 'id="cal-list"', 'id="cal-mon"', 'id="cal-prev"',
                       'id="cal-next"', 'id="cal-recent"', 'id="m-dayhead"'):
            self.assertIn(marker, self.page, "页面缺少 %s" % marker)

    def test_day_bucketing_is_beijing_not_utc(self):
        """按北京时间切日：UTC 16:30 那场算 9/20，不算 9/19。"""
        self.assertIn("function dayKey(", self.page)
        self.assertIn("d.getTime() + 8 * 3600 * 1000", self.page)
        payload = json.loads(call("/api/sessions")[1].decode("utf-8"))
        buckets = {}
        for s in payload["sessions"]:
            buckets.setdefault(beijing_day(s["started_at"]), []).append(s["session_id"])
        self.assertEqual(sorted(buckets.keys()), ["2026-09-19", "2026-09-20"])
        self.assertEqual(len(buckets["2026-09-19"]), 2, "9/19 当天两场")
        self.assertEqual(len(buckets["2026-09-20"]), 1, "UTC 16:30 = 北京 9/20 00:30")

    def test_marker_and_count_badge(self):
        """有会议的日期要有标记点，角标显示的就是当天场次数（list.length）。"""
        self.assertIn("function byDay(", self.page)
        self.assertIn('el("div", "mark")', self.page)
        self.assertIn('el("div", "cnt", list.length)', self.page)
        self.assertIn(".day .cnt{", self.page)
        self.assertIn(".day.has{", self.page)

    def test_same_day_sessions_sorted_by_time(self):
        self.assertIn('String(a.started_at || "").localeCompare(String(b.started_at || ""))',
                      self.page)

    def test_month_switch_and_default_month(self):
        for fn in ("function shiftMonth(", "function jumpRecentMonth(",
                   "function renderCalendar(", "function selectDay("):
            self.assertIn(fn, self.page, "页面缺少 %s" % fn)
        self.assertIn("默认定位到最近有会议的月份", self.page)

    def test_top_filters_exist(self):
        for marker in ('id="m-range"', 'id="m-member"', 'id="m-unconf"', 'id="m-risk"'):
            self.assertIn(marker, self.page, "页面缺少筛选 %s" % marker)
        for text in ("只看未当场确认", "只看有遗留/有逾期", "近 7 天", "近 30 天"):
            self.assertIn(text, self.page)

    def test_day_list_then_detail(self):
        self.assertIn("function renderDayList()", self.page)
        self.assertIn("function renderDetail()", self.page)
        self.assertIn("暂无会议记录", self.page)


class TestMeetingDetailElements(BaseCase):
    """详情面板：四要素齐全、顺序对、未通过不许看起来像通过。"""

    def setUp(self):
        BaseCase.setUp(self)
        self.page = call(BOARD_PATH)[1].decode("utf-8")

    def test_five_blocks_in_order(self):
        order = ["会议目标：", "renderAgenda(box", "renderConfirm(box",
                 '"落实事项"', '"遗留问题"', '"附：出席情况"', "完整留痕 · 本场全部发言"]
        cursor = -1
        for marker in order:
            index = self.page.find(marker, cursor + 1)
            self.assertGreater(index, cursor, "详情里 %s 没有按四要素顺序出现" % marker)
            cursor = index

    def test_agenda_item_shows_expected_and_conclusion(self):
        for text in ('"预期产出"', '"该项结论"'):
            self.assertIn(text, self.page)
        for status in ("pending", "concluded", "deferred"):
            self.assertIn(".badge." + status, self.page)

    def test_not_passed_is_never_shown_as_passed(self):
        self.assertIn("passed === true", self.page)
        self.assertIn("未通过（当场确认没过）", self.page)
        self.assertIn("未确认（没有当场确认记录）", self.page)
        self.assertIn(".vote.fail{border-color:var(--warn)", self.page)
        self.assertIn("function sessionPassed(s)", self.page)
        self.assertIn('s.confirm_summary.passed === true', self.page)

    def test_attendance_block_present(self):
        for text in ("应到 ", "实到 ", "缺席 ", "出席率 "):
            self.assertIn(text, self.page)
        self.assertIn(".att.bad{", self.page, "出席率 <100% 要标黄")
        self.assertIn(".miss{color:#ff8a80", self.page, "缺席名单要红标")

    def test_full_trace_is_collapsible(self):
        self.assertIn('el("details", "man")', self.page)
        self.assertIn("完整留痕 · 本场全部发言", self.page)


# --- D2：响应式（手机 / 平板 / 桌面）---------------------------------

class TestResponsive(BaseCase):
    def setUp(self):
        BaseCase.setUp(self)
        self.page = call(BOARD_PATH)[1].decode("utf-8")

    def test_viewport_meta(self):
        self.assertIn('<meta name="viewport" content="width=device-width,initial-scale=1">',
                      self.page)

    def test_at_least_three_breakpoints(self):
        for query in ("@media (min-width:1024px)", "@media (max-width:1023px)",
                      "@media (max-width:767px)", "@media (max-width:479px)"):
            self.assertIn(query, self.page, "缺少断点 %s" % query)
        self.assertGreaterEqual(self.page.count("@media"), 3)

    def test_fluid_type_uses_clamp(self):
        self.assertIn("clamp(", self.page)
        self.assertGreaterEqual(self.page.count("clamp("), 4, "只有一两处 clamp 不算流体字号")

    def test_long_content_never_breaks_the_page(self):
        self.assertIn("overflow-x:auto", self.page, "curl/JSON 片段要放在可横滚的代码块里")
        self.assertIn("overflow-x:hidden", self.page, "整页不许横向滚动")
        self.assertIn("word-break:break-word", self.page)
        self.assertIn("text-overflow:ellipsis", self.page)
        self.assertIn("overflow-wrap:anywhere", self.page, "长 ID / 成员名要能断行")

    def test_mobile_hides_desktop_only_bits(self):
        self.assertIn(".d-none-mobile{display:none !important}", self.page)
        self.assertIn('class="lb d-none-mobile"', self.page)
        self.assertIn(".trow.head{display:none}", self.page, "窄屏表头要收掉")

    def test_tables_become_single_column_cards(self):
        self.assertIn(".trow{grid-template-columns:1fr;padding:10px}", self.page)
        self.assertIn(".vote .names{display:flex;flex-direction:column", self.page)

    def test_calendar_turns_into_day_cards_on_phone(self):
        self.assertIn(".cal{display:none}", self.page, "手机上不许挤 7 列小方块")
        self.assertIn(".callist{display:flex}", self.page)
        self.assertIn(".dcard{", self.page)

    def test_touch_targets_are_44px(self):
        self.assertIn("min-height:44px", self.page)
        self.assertIn("button,select,input[type=text]{min-height:44px}", self.page)

    def test_still_self_contained(self):
        for forbidden in ("http://", "https://", "//cdn", "//fonts", "@import", "<link"):
            self.assertNotIn(forbidden, self.page, "页面引用了外部资源：%s" % forbidden)
        self.assertEqual(self.page.count("<script"), 1)


# --- D2：共享内容（只读透传 + sensitive 遮罩）------------------------

class TestSharedPassthrough(BaseCase):
    def test_shared_passes_through(self):
        code, body, _ = call("/api/shared")
        self.assertEqual(code, 200)
        payload = json.loads(body.decode("utf-8"))
        self.assertTrue(payload["ok"])
        self.assertEqual(self.last_call()[0], "/shared")
        self.assertEqual(len(payload["tasks"]), 2)
        self.assertEqual(len(payload["items"]), 3)

    def test_filters_pass_through_and_limit_clamped(self):
        call("/api/shared?scope=permanent&kind=node&owner=hermes&tag=infra&limit=999")
        path, query, _ = self.last_call()
        self.assertEqual(path, "/shared")
        self.assertEqual(query.get("scope"), "permanent")
        self.assertEqual(query.get("kind"), "node")
        self.assertEqual(query.get("owner"), "hermes")
        self.assertEqual(query.get("tag"), "infra")
        self.assertEqual(query.get("limit"), "50")

    def test_bad_scope_is_dropped(self):
        call("/api/shared?scope=whatever")
        self.assertIsNone(self.last_call()[1].get("scope"), "看不懂的 scope 不该转发")

    def test_empty_q_goes_to_shared_not_search(self):
        call("/api/shared/search?q=")
        self.assertEqual(self.last_call()[0], "/shared")

    def test_nonempty_q_goes_to_shared_search(self):
        call("/api/shared/search?q=%E5%A4%87%E4%BB%BD&semantic=on")
        path, query, _ = self.last_call()
        self.assertEqual(path, "/shared/search")
        self.assertEqual(query.get("q"), "备份")
        self.assertEqual(query.get("semantic"), "1", "semantic 要归一成 0/1")
        call("/api/shared/search?q=x")
        self.assertIsNone(self.last_call()[1].get("semantic"), "没开语义就别传")

    def test_upstream_without_shared_endpoint_degrades(self):
        """上游还没上线 /shared：给一条能看懂的 502，页面据此降级，不白屏。"""
        FakeState.shared_missing = True
        code, body, _ = call("/api/shared")
        self.assertEqual(code, 502)
        payload = json.loads(body.decode("utf-8"))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "shared_unavailable")
        self.assertIn("尚未上线", payload["detail"])
        self.assertEqual(call("/api/shared/search?q=x")[0], 502)

    def test_upstream_failures_become_502(self):
        FakeState.mode = "500"
        self.assertEqual(call("/api/shared")[0], 502)
        FakeState.mode = "ok"
        bv.Runtime.upstream = "http://127.0.0.1:%d" % free_port()
        code, body, _ = call("/api/shared")
        self.assertEqual(code, 502)
        self.assertTrue(json.loads(body.decode("utf-8"))["error"].startswith("upstream_"))

    def test_write_methods_are_405(self):
        for method, path in (("POST", "/api/shared"), ("PUT", "/api/shared"),
                             ("PATCH", "/api/shared/search"), ("DELETE", "/api/shared")):
            code, body, headers = call(path, method=method, data=b"{}")
            self.assertEqual(code, 405, "%s %s 应该被拒" % (method, path))
            self.assertIn("GET", headers.get("Allow", ""))
        self.assertEqual(FakeState.calls, [], "写请求不该碰上游")

    def test_content_is_escaped_and_token_never_leaks(self):
        code, body, headers = call("/api/shared")
        self.assertEqual(code, 200)
        self.assertNotIn(b"<script", body)
        self.assertNotIn(b"<", body)
        self.assertNotIn(b"&", body)
        self.assertEqual(self.last_call()[2], "Bearer " + TOKEN)
        self.assertNotIn(TOKEN, body.decode("utf-8"))
        self.assertNotIn(TOKEN, json.dumps(headers))
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(payload["items"][0]["title"], NASTY, "转义后要原样解回")


class TestSharedSensitive(BaseCase):
    """sensitive 且未授权：标题与正文在**服务端**就没了，浏览器一个字节都拿不到。"""

    def test_secret_never_appears_in_any_response(self):
        for path in ("/api/shared", "/api/shared/search?q=%E5%A4%87%E4%BB%BD"):
            code, body, _ = call(path)
            self.assertEqual(code, 200)
            text = body.decode("utf-8")
            self.assertNotIn(SECRET_TITLE, text, "%s 泄露了 sensitive 标题" % path)
            self.assertNotIn(SECRET_BODY, text, "%s 泄露了 sensitive 正文" % path)
            self.assertNotIn("13800000000", text)

    def test_masked_item_keeps_only_the_skeleton(self):
        payload = json.loads(call("/api/shared")[1].decode("utf-8"))
        item = [i for i in payload["items"] if i["shared_id"] == 102][0]
        self.assertTrue(item["masked"])
        self.assertEqual(item["title"], "")
        self.assertEqual(item["body"], "")
        self.assertEqual(item["tags"], [])
        self.assertNotIn("history", item, "变更历史也可能带正文，一并抹掉")
        # 但"存在"这件事本身要留下：谁维护、归哪个 kind、什么时候更新过
        self.assertEqual(item["owner"], FIX_MEMBER_CLOUD)
        self.assertEqual(item["kind"], "owner_key_info")
        self.assertIn("2026-09-17", item["updated_at"])

    def test_task_items_are_masked_too(self):
        payload = json.loads(call("/api/shared")[1].decode("utf-8"))
        task = [t for t in payload["tasks"] if t["task_id"] == "T-D2"][0]
        masked = [i for i in task["items"] if i["shared_id"] == 202][0]
        self.assertTrue(masked["masked"])
        self.assertEqual(masked["title"], "")
        self.assertEqual(masked["body"], "")
        kept = [i for i in task["items"] if i["shared_id"] == 201][0]
        self.assertEqual(kept["body"], NASTY, "没标 sensitive 的照常给")

    def test_authorized_sensitive_item_is_not_masked(self):
        item = bv._mask_item({"shared_id": 1, "sensitive": True, "authorized": True,
                              "title": "看得见", "body": "正文"})
        self.assertEqual(item["title"], "看得见")
        masked = bv._mask_item({"shared_id": 1, "sensitive": True, "authorized": False,
                                "title": "看不见", "body": "正文", "owner": "x"})
        self.assertEqual(masked["title"], "")
        self.assertTrue(masked["masked"])
        self.assertEqual(masked["owner"], "x")

    def test_page_shows_existence_without_content(self):
        page = call(BOARD_PATH)[1].decode("utf-8")
        self.assertIn("存在但无权查看", page)
        self.assertIn(".sitem.locked{", page)
        self.assertIn("标题与正文都不下发", page)


class TestSharedPane(BaseCase):
    def setUp(self):
        BaseCase.setUp(self)
        self.page = call(BOARD_PATH)[1].decode("utf-8")

    def test_pane_elements_exist(self):
        for marker in ('id="pane-shared"', 'id="s-body"', 'id="s-q"', 'id="s-sem"',
                       'id="s-kind"', 'id="s-owner"', 'id="s-tag"', 'id="s-count"',
                       'data-scope="task"', 'data-scope="permanent"'):
            self.assertIn(marker, self.page, "页面缺少 %s" % marker)
        self.assertIn(">任务级<", self.page)
        self.assertIn(">永久<", self.page)

    def test_task_group_shows_ttl_and_destroy_rule(self):
        for text in ("销毁条件：", "（未写明销毁条件）", "TTL 剩 ", "TTL 已到期", "已销毁",
                     "负责人 "):
            self.assertIn(text, self.page, "任务分组缺少 %s" % text)

    def test_permanent_groups_by_kind(self):
        for name in ("节点", "Agent", "主人画像", "主人关键信息", "配置", "其它"):
            self.assertIn(name, self.page, "永久区缺少分组 %s" % name)

    def test_每条内容的字段齐全(self):
        for text in ("维护责任人", "最后更新", "过期时间", "下次复审", "变更历史"):
            self.assertIn(text, self.page, "共享内容缺少字段 %s" % text)
        self.assertIn("改了：", self.page, "变更历史要写清改了哪些字段")

    def test_highlight_styles(self):
        for cls in (".sitem.expired", ".sitem.soon", ".sitem.review", ".sitem.revoked",
                    ".sitem.locked", ".badge.expired", ".badge.review", ".badge.revoked"):
            self.assertIn(cls, self.page, "缺少高亮样式 %s" % cls)
        for text in ("已过期", "7 天内到期", "待复审", "已撤下"):
            self.assertIn(text, self.page)

    def test_search_supports_semantic_switch(self):
        self.assertIn('"/api/shared/search?q="', self.page)
        self.assertIn('"&semantic=1"', self.page)
        self.assertIn('"/api/shared"', self.page)

    def test_degrade_message_when_service_absent(self):
        self.assertIn("数据暂时不可用：", self.page)
        self.assertIn("等它上线这里就有了", self.page)


# --- D2：Agent 查询手册 ---------------------------------------------

class TestAgentManual(BaseCase):
    def setUp(self):
        BaseCase.setUp(self)
        self.page = call(BOARD_PATH)[1].decode("utf-8")

    def test_manual_block_exists(self):
        for marker in ('id="man"', 'id="man-body"', 'id="man-top"', "Agent 查询手册",
                       "本页数据接口"):
            self.assertIn(marker, self.page, "页面缺少 %s" % marker)

    def test_all_endpoints_are_listed(self):
        for endpoint in ("/api/shared?scope=task|permanent",
                         "/api/shared/search?q=&semantic=1",
                         "/api/sessions?state=", "/api/sessions/<session_id>",
                         "/api/tracking?overdue=", "/api/actions?status=",
                         "/api/issues?status=", "/api/entries?topic=", "/api/search?q="):
            self.assertIn(endpoint, self.page, "手册缺少端点 %s" % endpoint)

    def test_每个模块都有可复制的_curl(self):
        self.assertGreaterEqual(self.page.count("curl -s"), 5, "每个模块至少一条 curl")
        self.assertIn("/api/sessions?limit=50", self.page)
        self.assertIn("/api/shared?scope=permanent&limit=50", self.page)
        self.assertIn("/api/tracking?overdue=1&limit=50", self.page)
        self.assertIn("Authorization: Bearer <token>", self.page)

    def test_readonly_and_auth_notes(self):
        self.assertIn("只读", self.page)
        self.assertIn("405", self.page)
        self.assertIn("token → 成员名", self.page)
        self.assertIn("audience", self.page)


# --- D2：降级文案 ---------------------------------------------------

class TestDegradeCopy(BaseCase):
    def test_page_has_degrade_copy_for_every_module(self):
        page = call(BOARD_PATH)[1].decode("utf-8")
        for text in ("数据暂时不可用：", "没有取到跟踪数据（见顶部提示）",
                     "没有取到任何条目", "暂无会议记录"):
            self.assertIn(text, page, "缺少降级文案 %s" % text)

    def test_page_renders_even_when_upstream_is_dead(self):
        bv.Runtime.upstream = "http://127.0.0.1:%d" % free_port()
        code, body, _ = call(BOARD_PATH)
        self.assertEqual(code, 200)
        page = body.decode("utf-8")
        self.assertIn('id="banner"', page)
        self.assertIn('id="pane-shared"', page)


# --- v1.5：首页 = webPortal 卡片墙 ------------------------------------

class TestPortalHome(BaseCase):
    """根路径只做入口：几张卡片，不进任何黑板数据；白板本尊在 /board。"""

    def test_root_is_card_wall_not_board(self):
        code, body, _ = call("/")
        self.assertEqual(code, 200)
        page = body.decode("utf-8")
        self.assertIn("团队门户", page)
        self.assertIn('href="' + BOARD_PATH + '"', page)   # 第一张卡：看板
        self.assertIn("人机对话台", page)                   # 第二张卡：群聊/对话台
        self.assertIn('<div class="grid">', page)

    def test_board_moved_to_board_path(self):
        code, body, _ = call(BOARD_PATH)
        self.assertEqual(code, 200)
        self.assertIn("团队黑板", body.decode("utf-8"))

    def test_portal_alias_and_old_query_links(self):
        self.assertEqual(call("/portal")[0], 200)          # /portal 与 / 同页
        code, body, _ = call("/?tab=shared")               # 白板时代的老书签
        # call() 会跟随 302：旧链接应该落到白板，而不是停在卡片墙或 404
        self.assertEqual(code, 200)
        self.assertIn("团队黑板", body.decode("utf-8"))

    def test_portal_renders_no_entry_content(self):
        """卡片墙是静态壳子：上游数据一个字节都不进 HTML。"""
        call("/api/entries")
        page = call("/")[1].decode("utf-8")
        self.assertNotIn("alert(1)", page)
        self.assertNotIn("存在但无权查看", page)             # 那是看板页的文案

    def test_portal_still_renders_when_upstream_dead(self):
        bv.Runtime.upstream = "http://127.0.0.1:%d" % free_port()
        code, body, _ = call("/")
        self.assertEqual(code, 200)
        self.assertIn("团队门户", body.decode("utf-8"))

    def test_card_wall_has_no_write_entry(self):
        code, _body, headers = call("/", method="DELETE")
        self.assertEqual(code, 405)                        # 只读纪律在首页一样成立
        self.assertEqual(headers.get("Allow"), "GET, HEAD")


if __name__ == "__main__":
    unittest.main(verbosity=2)
