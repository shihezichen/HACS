# -*- coding: utf-8 -*-
"""chat_svc 的**应答出口 + 非 GET 分派**收进 L1 `web_gateway` 的字节级等价证据（可复跑）。

用法：
    python tools/equivalence/chat_svc_http_equiv.py [--ref <迁移前的提交>]

      --ref  给了就从**源码仓**该提交重新取一份 agent-team/chat_svc.py 覆盖到
             _old/chat_svc_old_8a.py；不给就用磁盘上已有的那份快照。
             快照作「旧实现」，工作树那份作「新实现」。

做法：同一进程内起两份 ThreadingHTTPServer —— 旧 / 新 的 `ChatHandler`，各绑一个
空闲高端口，跑同一张请求矩阵，逐例比对：

    状态码 + 状态原因 + **可比响应头（含顺序）** + 正文（归一后逐字节）

**头的顺序也比**（不是排序后比）：本刀把「逐条 send_header」换成了「把头塞进 dict
交给 L1 的 `_reply()` 按插入序发」，顺序正是最容易无声漂的地方，所以 `_snap()` 保留
`getheaders()` 的原始顺序。

**数据库一个字节都不真连**：走该件既有的注入缝 —— `Runtime.db` / `Runtime.roster` /
`Runtime.auth` 是模块级可赋值的类属性（`agent-team/tests/test_chat_svc.py` 用的就是
这条缝），这里塞一套**无状态、确定性**的假件。两侧各自一份实例，互不串状态。
另外 `view_auth.in_tailnet` 用 monkeypatch 换掉（它是 L2 的网段判定，两侧共享同一个
模块对象，所以一次替换对新旧同时生效 —— 这正是我们要的「同一条件」）。

计算器反代不打真 9985：起一个**本地假上游**，并把两侧的
`ChatHandler.CHAT_CALC_UPSTREAM` 指过去；另有一相把它指到死端口，验 502 那一支。

归一化只吃**天然漂移**（两侧同样施加，逐条写明）：
  · 响应头里的 `Date`（每次都不同）；
  · 正文里的 ISO 时间戳（`server_time` / `now` / `created_at` / `updated_at` / `ts`）；
  · 正文里的毫秒 / 秒级数字（`uptime_s` / `*_ms` / `elapsed*`）；
  · 建群时 `secrets.token_hex(3)` 随机出来的 `meet_<日期>_<6位hex>` 会话 id；
  · 500 兜底文案里可能带的行号。
**`Server`、`Content-Type`、`Content-Length`、`Cache-Control`、`X-Content-Type-Options`、
状态码、状态原因、头的顺序、正文其余部分一个都不剔。**

退出码：0 = 全一致，1 = 有差异，2 = 装置自身没跑成（缺快照等）。
"""
import argparse
import http.client
import io
import json
import os
import re
import socket
import subprocess
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
AG = os.path.join(REPO, "agent-team")
OLD = os.path.join(HERE, "_old")
OLD_FILE = os.path.join(OLD, "chat_svc_old_8a.py")

SID = "dm_20260920_k7f3qa"          # 假件里「存在且可见」的那个会话
SID_MISS = "dm_20260920_nosuch"     # 假件里不存在的会话
COOKIE = "".join(("valid", "-", "session", "-", "cookie"))   # 运行期拼装，不落口令字面量
HUMAN = "<human-id>"
AGENT = "lobster"

# 「本件有没有流式面」的判据：出现任何一个就说明有流式/长连接写法，
# 那一支就绝不能走 L1 的缓冲封装（硬约束 3）。本装置把它做成一条**可复跑的断言**。
STREAM_MARKERS = (b"text/event-stream", b"Transfer-Encoding", b"chunked",
                  b"wfile.flush", b"iter_content", b"yield ")

# 天然漂移归一（两侧同样施加）
_NORM = (
    (re.compile(rb'"(server_time|now|created_at|updated_at|generated_at|started_at)":\s*"[^"]*"'),
     rb'"\1": "<ISO>"'),
    # /healthz 的 build_* 取的是**源文件 mtime**：旧快照与工作树那份必然不同 mtime，
    # 这是装置自身的产物（两份文件），不是行为差异 —— 归一掉。`build_source`
    # （"chat_svc.py mtime"）与 `process_start` 不归一，它们本来就该一致。
    (re.compile(rb'"(build_time|build_time_iso)":\s*"[^"]*"'), rb'"\1": "<BUILD>"'),
    (re.compile(rb'"build_epoch":\s*\d+'), b'"build_epoch": <BUILD_EPOCH>'),
    (re.compile(rb'"(uptime_s|latency_ms|elapsed_ms|request_ms|took_ms)":\s*[\d.]+'),
     rb'"\1": <NUM>'),
    (re.compile(rb'"ts":\s*[\d.]+'), b'"ts": <TS>'),
    (re.compile(rb'meet_\d{8}_[0-9a-f]{6}'), b'meet_<DATE>_<RAND>'),
    (re.compile(rb'line \d+'), b'line <N>'),
)


def normalize(raw):
    for rx, repl in _NORM:
        raw = rx.sub(repl, raw)
    return raw


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def materialize_old(ref):
    """从 git 取指定提交的 chat_svc.py 当旧实现快照（逐字节，不碰行尾）。"""
    if not os.path.isdir(OLD):
        os.makedirs(OLD)
    raw = subprocess.run(["git", "show", "%s:agent-team/chat_svc.py" % ref],
                         cwd=REPO, capture_output=True).stdout
    if not raw:
        print("取不到 %s:agent-team/chat_svc.py —— ref 写错了？" % ref)
        sys.exit(2)
    raw = raw.replace(b"\r\n", b"\n")        # 工作区若是 CRLF 检出，快照仍存 LF
    if b"class ChatHandler" not in raw:
        print("警告：%s 的 chat_svc.py 里没有 ChatHandler，那它就不是「旧实现」" % ref)
    io.open(OLD_FILE, "wb").write(raw)


# ------------------------------------------------------------------ 假件（无状态）

def _session(state="open"):
    return {
        "id": SID, "kind": "dm", "title": "等价装置用会话",
        "sensitivity": "internal", "member_ids": [HUMAN, AGENT],
        "created_by": HUMAN, "created_at": "2026-09-20T18:20:00+08:00",
        "updated_at": "2026-09-20T18:20:00+08:00", "state": state,
        "ref_table": None, "ref_id": None, "meta": {},
    }


class FakeDB(object):
    """只给确定性的常量答复：两侧走**同一条**代码路径。

    故意**无状态** —— 矩阵里前一例的写不会改变后一例的读，新旧两侧各跑一遍
    也不会因为「谁先跑」而分叉。少数方法猜不准真实形状时，两侧会同样落到
    某个 5xx，那依然是逐字节对照（对照的是 HTTP 面，不是业务正确性）。
    """

    def query(self, sql, params=()):
        return []

    def close(self):
        pass

    # --- 会话 ---
    def create_session(self, session_id, kind, title, members, author_id, **kw):
        out = _session()
        out["id"] = session_id          # 带上生成的 id（随机段由 normalize 归一）
        out["kind"] = kind
        out["title"] = title
        out["member_ids"] = list(members)
        return out

    def get_session(self, session_id, role=None):
        return _session() if session_id == SID else None

    def list_sessions(self, role, filters):
        return []

    def set_session_meta(self, *a, **kw):
        return True

    def members_write(self, *a, **kw):
        return {"ok": True, "member_ids": [HUMAN, AGENT]}

    # --- 消息 ---
    def add_message(self, *a, **kw):
        return {"id": "msg_20260920183000_abc123", "session_id": SID, "seq": 1,
                "author_type": "human", "author_id": HUMAN, "kind": "text",
                "text": "hi", "status": "done",
                "created_at": "2026-09-20T18:30:00+08:00"}

    def list_messages(self, *a, **kw):
        return []

    def last_message(self, session_id):
        return None

    # --- 群记忆锚 ---
    def memo_get(self, *a, **kw):
        return None

    def memo_write(self, *a, **kw):
        return {"ok": True}

    # --- 待办 ---
    def todos_list(self, *a, **kw):
        return []

    def todos_rows(self, *a, **kw):
        return []

    def todos_unenriched(self, *a, **kw):
        return []

    def todos_skips_list(self, *a, **kw):
        return []

    def todos_clear(self, *a, **kw):
        return 0

    def todos_purge(self, *a, **kw):
        return 0

    def todo_create(self, *a, **kw):
        return {"id": "todo_0001", "session_id": SID, "status": "todo"}

    def todo_get(self, *a, **kw):
        return None

    def todo_skip_create(self, *a, **kw):
        return None

    def todo_delete_pristine(self, *a, **kw):
        return 0

    def todo_bump_followup(self, *a, **kw):
        return None

    def todo_set_enrichment(self, *a, **kw):
        return None

    def todo_mark_done(self, *a, **kw):
        return None

    def todo_mark_doing(self, *a, **kw):
        return None

    def todo_reopen(self, *a, **kw):
        return None

    # --- 建表（build_runtime 会调；这里用不到，留着防漏）---
    def ensure_memo_schema(self):
        pass

    def ensure_todo_schema(self):
        pass

    def ensure_meta_schema(self):
        pass


class FakeRoster(object):
    _M = {
        AGENT: {"id": AGENT, "display": "龙虾", "node_id": "cloud",
                "host": "<tailnet-cidr>", "model": "x/default",
                "description": "等价装置用", "skills": []},
    }

    def get(self, member_id):
        return self._M.get(member_id)

    def all(self):
        return [dict(v) for v in self._M.values()]

    def ids(self):
        return list(self._M.keys())


class FakePrincipal(object):
    def __init__(self, username, role):
        self.username = username
        self.role = role


class FakeAuth(object):
    """check_cookie 只认一个魔法 cookie 值（与 tests/test_chat_svc.py 同一手法）。"""

    key = b"0" * 32

    def check_cookie(self, cookie_value, ip, ua):
        if cookie_value == COOKIE:
            return FakePrincipal(HUMAN, "owner"), "ok"
        return None, "bad_cookie"


def setup_runtime(mod, ui_dir):
    """把被测模块配成「假库 / 假名册 / 假鉴权 / 已部署 ui」，**不起任何后台线程**。"""
    mod.Runtime.reset()
    mod.Runtime.db = FakeDB()
    mod.Runtime.roster = FakeRoster()
    mod.Runtime.auth = FakeAuth()
    mod.Runtime.started_at = 0.0
    mod.Runtime.ui_dir = ui_dir


# ------------------------------------------------------------------ 假上游（计算器反代）

class _CalcUpstream(object):
    """一个最小的假计算器后台：/api/ok → 200 JSON，其余 → 404 JSON。

    为的是让 `_calc_proxy()` 的**成功支**也走一遍 `_send_bytes()`（本刀改过的出口），
    而且响应是非 JSON 默认头以外的 Content-Type，能验「反代原样透传 content-type」。
    """

    @staticmethod
    def make():
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                return

            def _go(self):
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                if self.path.startswith("/api/ok"):
                    body = b'{"ok":true,"from":"fake-calc"}'
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                else:
                    body = b'{"ok":false,"error":"nope"}'
                    self.send_response(404)
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = _go

        port = free_port()
        srv = ThreadingHTTPServer(("127.0.0.1", port), H)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv, port


class _DeadUpstream(object):
    """「后台没起」的**确定性**替身：接上就立刻关，两侧必得同一个 RemoteDisconnected。

    为什么不直接指一个没人听的端口 —— 试过，不稳：Windows 上同一个刚释放的端口，
    第一次连拿到的是 `RemoteDisconnected`（"Remote end closed..."），第二次是
    `WinError 10054`，于是旧侧新侧的 502 正文（`scrubbed(exc)` 把 OS 原文带进了
    message）差了 23 个字节 —— 一条**纯环境噪声**的假不一致。
    这里自己造一个「accept 完就 close」的黑洞口，错误原文就固定了，
    502 那一支照样是真的走「上游不可用」分支，只是不再随 OS 心情漂。
    """

    @staticmethod
    def make():
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(8)
        port = srv.getsockname()[1]

        def loop():
            while True:
                try:
                    conn, _ = srv.accept()
                except OSError:
                    return
                conn.close()

        threading.Thread(target=loop, daemon=True).start()
        return srv, port


# ------------------------------------------------------------------ 起服务

def start(handler_cls, port):
    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(("127.0.0.1", port), handler_cls)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# ------------------------------------------------------------------ 请求

def _snap(resp):
    """一次响应的可比快照：状态码 + 原因 + **有序**头（只去 Date）+ 归一正文。

    `Content-Length` **不归一**：本刀动的正是「谁来填 Content-Length」，
    归一掉等于把最该盯的那一格蒙上（HEAD 尤其 —— 它不写体但要报真实长度）。
    """
    raw = resp.read()
    hdrs = [(k.lower(), v) for k, v in resp.getheaders() if k.lower() != "date"]
    return resp.status, resp.reason, hdrs, normalize(raw)


def _headers(auth, body):
    headers = {}
    if auth == "cookie":
        headers["Cookie"] = "%s=%s" % (COOKIE_NAME, COOKIE)
    elif auth == "badcookie":
        headers["Cookie"] = "%s=%s" % (COOKIE_NAME, "not-a-valid-cookie")
    elif auth == "agent":                    # tailnet 下以 Agent 名义发言
        headers["X-Agent-Id"] = AGENT
    if body is not None:
        headers["Content-Type"] = "application/json"
    return headers


#: 会话 cookie 的名字。**必须取 view_auth 的真值**（`_resolve_visitor()` 读的就是
#: `view_auth.SESSION_COOKIE`）；名字写错的话每一例「有 cookie」都会静静地退化成
#: 「无 cookie」，矩阵表面全绿、其实从没验过登录态那一半。由 main() 覆盖。
COOKIE_NAME = "bbview_session"


def call(port, method, path, auth, body):
    """发一次请求（每例独立连接）。"""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        conn.request(method, path, body=body, headers=_headers(auth, body))
        return _snap(conn.getresponse())
    except Exception as exc:                 # 连接被关也当一种结果比对
        return "EXC", type(exc).__name__, [], b""
    finally:
        conn.close()


def call_raw(port, method, path, auth, body):
    """同 `call()`，但**不归一化正文** —— 给「Content-Length 对不对得上体长」用。"""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        conn.request(method, path, body=body, headers=_headers(auth, body))
        resp = conn.getresponse()
        raw = resp.read()
        return resp.status, [(k.lower(), v) for k, v in resp.getheaders()], raw
    finally:
        conn.close()


def call_keepalive(port, cases):
    """同一条 TCP 连接上连发若干例：验 keep-alive 不错位。

    这一组专门盯 `@GW.pre` 里那句 `_drain_body()` —— 写请求带着 body 打到一条
    405/501 的路径上，body 要是没排空，同一连接上的下一个请求就会读到半截字节。
    """
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    out = []
    try:
        for method, path, auth, body in cases:
            conn.request(method, path, body=body, headers=_headers(auth, body))
            out.append(_snap(conn.getresponse()))
    except Exception as exc:
        out.append(("EXC", type(exc).__name__, [], b""))
    finally:
        conn.close()
    return out


def J(obj):
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


# ------------------------------------------------------------------ 矩阵
#
# (用例名, 方法, 路径, 鉴权, 请求体)    鉴权: none / cookie / badcookie / agent
#
# 「无 token 一例、有 token 一例」在本件的口径 = 「非 tailnet 且无 cookie」一例、
# 「带合法 cookie（或 tailnet 免登录）」一例。前者在 A 相跑，后者在 A 相用 cookie、
# B 相用 tailnet 各跑一遍。

MSG = J({"text": "等价装置的一条发言", "mentions": [AGENT]})
BODY_EMPTY = J({})
BAD_JSON = b"{not-json"

# ---- A 相：非 tailnet（严格鉴权）。**每条新登记的路由在这里拿到「无 token」那一例** ----
CASES_NOAUTH = [
    # 1) _gw_write_healthz —— 迁移前 /healthz 不分方法、不鉴权，写方法照样 200
    ("A01 POST healthz 无token",   "POST",   "/healthz", "none", BODY_EMPTY),
    ("A02 PUT healthz 无token",    "PUT",    "/healthz", "none", BODY_EMPTY),
    ("A03 PATCH healthz 无token",  "PATCH",  "/healthz", "none", BODY_EMPTY),
    ("A04 DELETE healthz 无token", "DELETE", "/healthz", "none", None),
    ("A05 POST healthz 尾斜杠",     "POST",   "/healthz/", "none", BODY_EMPTY),
    ("A06 POST healthz #frag",     "POST",   "/healthz#x", "none", BODY_EMPTY),
    ("A07 POST healthz 带查询",     "POST",   "/healthz?a=1", "none", BODY_EMPTY),
    # 2) _gw_post_sessions —— 建群
    ("A08 POST sessions 无token",  "POST", "/api/sessions", "none", J({"kind": "meet"})),
    ("A09 POST sessions 坏cookie", "POST", "/api/sessions", "badcookie", J({"kind": "meet"})),
    ("A10 POST sessions 有cookie", "POST", "/api/sessions", "cookie", J({"kind": "meet"})),
    ("A11 POST sessions 坏kind",   "POST", "/api/sessions", "cookie", J({"kind": "zzz"})),
    ("A12 POST sessions 坏JSON",   "POST", "/api/sessions", "cookie", BAD_JSON),
    ("A13 POST sessions 无JSON体", "POST", "/api/sessions", "cookie", None),
    # 3) _gw_write_sessions —— 九条子路径，逐条一例无 token + 一例有 cookie
    ("A14 POST messages 无token",  "POST", "/api/sessions/%s/messages" % SID, "none", MSG),
    ("A15 POST messages 有cookie", "POST", "/api/sessions/%s/messages" % SID, "cookie", MSG),
    ("A16 POST messages 会话不存在", "POST", "/api/sessions/%s/messages" % SID_MISS, "cookie", MSG),
    ("A17 POST messages 空text",   "POST", "/api/sessions/%s/messages" % SID, "cookie",
     J({"text": "   "})),
    ("A18 PATCH session 无token",  "PATCH", "/api/sessions/%s" % SID, "none", J({"title": "改名"})),
    ("A19 PATCH session 有cookie", "PATCH", "/api/sessions/%s" % SID, "cookie", J({"title": "改名"})),
    ("A20 POST members 无token",   "POST", "/api/sessions/%s/members" % SID, "none",
     J({"member_ids": [AGENT]})),
    ("A21 POST members 有cookie",  "POST", "/api/sessions/%s/members" % SID, "cookie",
     J({"member_ids": [AGENT]})),
    ("A22 DELETE members 无token", "DELETE", "/api/sessions/%s/members" % SID, "none",
     J({"member_ids": [AGENT]})),
    ("A23 DELETE members 有cookie", "DELETE", "/api/sessions/%s/members" % SID, "cookie",
     J({"member_ids": [AGENT]})),
    ("A24 PUT memo 无token",       "PUT", "/api/sessions/%s/memo" % SID, "none", J({"memo": "x"})),
    ("A25 PUT memo 有cookie",      "PUT", "/api/sessions/%s/memo" % SID, "cookie", J({"memo": "x"})),
    ("A26 PATCH memo 无token",     "PATCH", "/api/sessions/%s/memo" % SID, "none", J({"memo": "y"})),
    ("A27 PATCH memo 有cookie",    "PATCH", "/api/sessions/%s/memo" % SID, "cookie", J({"memo": "y"})),
    ("A28 POST todos 无token",     "POST", "/api/sessions/%s/todos" % SID, "none",
     J({"title": "一条待办"})),
    ("A29 POST todos 有cookie",    "POST", "/api/sessions/%s/todos" % SID, "cookie",
     J({"title": "一条待办"})),
    ("A30 POST todos/clear 无token", "POST", "/api/sessions/%s/todos/clear" % SID, "none",
     BODY_EMPTY),
    ("A31 POST todos/clear 有cookie", "POST", "/api/sessions/%s/todos/clear" % SID, "cookie",
     BODY_EMPTY),
    ("A32 POST todos/purge 无token", "POST", "/api/sessions/%s/todos/purge" % SID, "none",
     BODY_EMPTY),
    ("A33 POST todos/purge 有cookie", "POST", "/api/sessions/%s/todos/purge" % SID, "cookie",
     BODY_EMPTY),
    ("A34 POST todos/backfill 无token", "POST", "/api/sessions/%s/todos/backfill" % SID, "none",
     BODY_EMPTY),
    ("A35 POST todos/backfill 有cookie", "POST", "/api/sessions/%s/todos/backfill" % SID, "cookie",
     BODY_EMPTY),
    ("A36 POST todos/enrich 无token", "POST", "/api/sessions/%s/todos/enrich" % SID, "none",
     BODY_EMPTY),
    ("A37 POST todos/enrich 有cookie", "POST", "/api/sessions/%s/todos/enrich" % SID, "cookie",
     BODY_EMPTY),
    # 4) _gw_patch_todo
    ("A38 PATCH todo 无token",     "PATCH", "/api/todos/todo_0001", "none", J({"status": "done"})),
    ("A39 PATCH todo 有cookie",    "PATCH", "/api/todos/todo_0001", "cookie", J({"status": "done"})),
    ("A40 PATCH todos 无id",       "PATCH", "/api/todos", "cookie", J({"status": "done"})),
    ("A41 PATCH todos 过深",       "PATCH", "/api/todos/a/b", "cookie", J({"status": "done"})),
    # 5) 形状不对 → 501 not_implemented（**不是 404**；前端据这个码开关按钮）
    ("A42 PUT messages 方法不对",   "PUT", "/api/sessions/%s/messages" % SID, "cookie", MSG),
    ("A43 POST 未知子路径",         "POST", "/api/sessions/%s/zzz" % SID, "cookie", BODY_EMPTY),
    ("A44 POST sessions 过深",     "POST", "/api/sessions/%s/todos/zzz" % SID, "cookie", BODY_EMPTY),
    ("A45 PATCH sessions 裸路径",   "PATCH", "/api/sessions", "cookie", BODY_EMPTY),
    ("A46 DELETE sessions 裸路径",  "DELETE", "/api/sessions", "cookie", None),
    ("A47 POST sid 超长(65位)",     "POST", "/api/sessions/%s/messages" % ("x" * 65), "cookie", MSG),
    ("A48 POST 未知 api 路径",      "POST", "/api/zzz", "cookie", BODY_EMPTY),
    ("A49 POST /api 裸段",         "POST", "/api", "cookie", BODY_EMPTY),
    ("A50 DELETE 未知 api 路径",    "DELETE", "/api/zzz", "cookie", None),
    # 6) _gw_not_found 的「非 /api 一律走静态」支 —— 写方法落 405 method_not_allowed
    ("A51 POST 根路径",            "POST", "/", "cookie", BODY_EMPTY),
    ("A52 POST /login",           "POST", "/login", "cookie", BODY_EMPTY),
    ("A53 POST /logout",          "POST", "/logout", "cookie", BODY_EMPTY),
    ("A54 POST 未知非api路径",      "POST", "/nope", "cookie", BODY_EMPTY),
    ("A55 PUT 静态文件路径",        "PUT", "/group.html", "cookie", BODY_EMPTY),
    ("A56 DELETE 静态文件路径",     "DELETE", "/group.html", "cookie", None),
    # 7) 计算器前缀的**护栏**：/calc/api、/calc/admin 本身不在反代/后台支上
    ("A57 POST /calc",            "POST", "/calc", "cookie", BODY_EMPTY),
    ("A58 POST /calc/api 裸前缀",  "POST", "/calc/api", "cookie", BODY_EMPTY),
    ("A59 POST /calc/api/ 尾斜杠", "POST", "/calc/api/", "cookie", BODY_EMPTY),
    ("A60 POST /calc/admin 裸前缀", "POST", "/calc/admin", "cookie", BODY_EMPTY),
    ("A61 POST /calc/admin/x",     "POST", "/calc/admin/x", "cookie", BODY_EMPTY),
    ("A62 POST /calc/adminx 近似",  "POST", "/calc/adminx", "cookie", BODY_EMPTY),
    ("A63 POST /calc/apix 近似",    "POST", "/calc/apix", "cookie", BODY_EMPTY),
    # 8) _gw_write_calc_api —— 反代真走一遍（成功支 + 上游 404 支）
    ("A64 POST calc 反代 200",     "POST", "/calc/api/ok", "cookie", J({"expr": "1+1"})),
    ("A65 POST calc 反代 404",     "POST", "/calc/api/nope", "cookie", J({"expr": "1+1"})),
    ("A66 PUT calc 反代 200",      "PUT",  "/calc/api/ok", "cookie", BODY_EMPTY),
    ("A67 DELETE calc 反代 200",   "DELETE", "/calc/api/ok", "cookie", None),
    ("A68 POST calc 反代带查询",    "POST", "/calc/api/ok?a=1&b=2", "cookie", BODY_EMPTY),
    # 9) HEAD —— 本刀把方法分派接进 L1，表仍是 GET 的表（留给 8b）
    ("A69 HEAD healthz 无token",   "HEAD", "/healthz", "none", None),
    ("A70 HEAD whoami 无token",    "HEAD", "/api/whoami", "none", None),
    ("A71 HEAD whoami 有cookie",   "HEAD", "/api/whoami", "cookie", None),
    ("A72 HEAD 根路径",            "HEAD", "/", "cookie", None),
    ("A73 HEAD 静态文件",          "HEAD", "/group.html", "cookie", None),
    ("A74 HEAD 未知非api路径",      "HEAD", "/nope", "cookie", None),
    ("A75 HEAD 未知 api 路径",      "HEAD", "/api/zzz", "cookie", None),
    ("A76 HEAD /login 302",       "HEAD", "/login", "none", None),
    ("A77 HEAD 静态 404",          "HEAD", "/nope.js", "cookie", None),
    ("A78 HEAD calc 反代",         "HEAD", "/calc/api/ok", "cookie", None),
    # 10) 未登记的方法：照旧落基类 501（L1 的 handler_class 删 do_OPTIONS 后行为不变）
    ("A79 OPTIONS healthz",       "OPTIONS", "/healthz", "none", None),
    ("A80 OPTIONS 根路径",         "OPTIONS", "/", "none", None),
    # 11) 超大 body（触发 BODY_MAX 截断 + close_connection）
    ("A81 POST 超大body到501路径",  "POST", "/api/zzz", "cookie", b'{"x":"' + b"z" * 200000 + b'"}'),
]

# ---- 未登记路由（留给刀8b 的 GET 面）：证明没被顺手改掉 ----
CASES_GET_8B = [
    ("G01 GET healthz",           "GET", "/healthz", "none", None),
    ("G02 GET healthz 尾斜杠",     "GET", "/healthz/", "none", None),
    ("G03 GET whoami 无token",    "GET", "/api/whoami", "none", None),
    ("G04 GET whoami 有cookie",   "GET", "/api/whoami", "cookie", None),
    ("G05 GET agents 有cookie",   "GET", "/api/agents", "cookie", None),
    ("G06 GET sessions 有cookie", "GET", "/api/sessions", "cookie", None),
    ("G07 GET sessions 带筛选",    "GET", "/api/sessions?kind=dm&state=open", "cookie", None),
    ("G08 GET session 详情",       "GET", "/api/sessions/%s" % SID, "cookie", None),
    ("G09 GET session 不存在",     "GET", "/api/sessions/%s" % SID_MISS, "cookie", None),
    ("G10 GET messages",          "GET", "/api/sessions/%s/messages" % SID, "cookie", None),
    ("G11 GET messages limit",    "GET", "/api/sessions/%s/messages?limit=9999" % SID, "cookie", None),
    ("G12 GET messages 互斥",      "GET",
     "/api/sessions/%s/messages?before_seq=5&after_seq=2" % SID, "cookie", None),
    ("G13 GET memo",              "GET", "/api/sessions/%s/memo" % SID, "cookie", None),
    ("G14 GET todos",             "GET", "/api/sessions/%s/todos" % SID, "cookie", None),
    ("G15 GET todos/intents",     "GET", "/api/sessions/%s/todos/intents" % SID, "cookie", None),
    ("G16 GET members",           "GET", "/api/sessions/%s/members" % SID, "cookie", None),
    ("G17 GET 未知 api 路径",      "GET", "/api/zzz", "cookie", None),
    ("G18 GET /api 裸段",          "GET", "/api", "cookie", None),
    # 302 重定向（本刀改过的出口之一，由 8b 的 GET 路由够到）
    ("G19 GET /login 302",        "GET", "/login", "none", None),
    ("G20 GET /login 带 next",    "GET", "/login?next=/g/abc", "none", None),
    ("G21 GET /login 外站 next",   "GET", "/login?next=http://evil.example/x", "none", None),
    ("G22 GET /logout 302",       "GET", "/logout", "none", None),
    # 静态托管（_send_file → _send_bytes，本刀改过的出口；**前端页要求一字不动**）
    ("G23 GET 根 → group.html",   "GET", "/", "cookie", None),
    ("G24 GET group.html 直取",    "GET", "/group.html", "cookie", None),
    ("G25 GET 子目录 index",       "GET", "/sub", "cookie", None),
    ("G26 GET css MIME",          "GET", "/a.css", "cookie", None),
    ("G27 GET js MIME",           "GET", "/a.js", "cookie", None),
    ("G28 GET 二进制 png",         "GET", "/a.png", "cookie", None),
    ("G29 GET 静态 404 带扩展名",   "GET", "/nope.js", "cookie", None),
    ("G30 GET SPA fallback",      "GET", "/nope", "cookie", None),
    ("G31 GET 目录穿越",           "GET", "/../secret.txt", "cookie", None),
    ("G32 GET 隐藏文件",           "GET", "/.hidden", "cookie", None),
    ("G33 GET 排除表 serve.py",    "GET", "/serve.py", "cookie", None),
    ("G34 GET calc 前台页",        "GET", "/calc", "cookie", None),
    ("G35 GET calc/admin",        "GET", "/calc/admin", "cookie", None),
    ("G36 GET calc 反代 200",      "GET", "/calc/api/ok", "cookie", None),
    ("G37 GET 大文件(1MB)",        "GET", "/big.bin", "cookie", None),
]

# ---- B 相：tailnet 免登录（= 「有 token」的另一种口径），能到 2xx 的都在这里 ----
CASES_TAILNET = [
    ("B01 POST sessions tailnet",  "POST", "/api/sessions", "none", J({"kind": "meet"})),
    ("B02 POST sessions 带成员",   "POST", "/api/sessions", "none",
     J({"kind": "meet", "members": [AGENT], "title": "等价群"})),
    ("B03 POST sessions 未知成员",  "POST", "/api/sessions", "none",
     J({"kind": "meet", "members": ["nosuch"]})),
    ("B04 POST messages tailnet",  "POST", "/api/sessions/%s/messages" % SID, "none", MSG),
    ("B05 POST messages 以agent名", "POST", "/api/sessions/%s/messages" % SID, "agent", MSG),
    ("B06 POST messages 超长text", "POST", "/api/sessions/%s/messages" % SID, "none",
     J({"text": "字" * 9000})),
    ("B07 PATCH session tailnet",  "PATCH", "/api/sessions/%s" % SID, "none", J({"title": "改名"})),
    ("B08 POST members tailnet",   "POST", "/api/sessions/%s/members" % SID, "none",
     J({"member_ids": [AGENT]})),
    ("B09 DELETE members tailnet", "DELETE", "/api/sessions/%s/members" % SID, "none",
     J({"member_ids": [AGENT]})),
    ("B10 PUT memo tailnet",       "PUT", "/api/sessions/%s/memo" % SID, "none", J({"memo": "x"})),
    ("B11 PATCH memo tailnet",     "PATCH", "/api/sessions/%s/memo" % SID, "none", J({"memo": "y"})),
    ("B12 POST todos tailnet",     "POST", "/api/sessions/%s/todos" % SID, "none",
     J({"title": "一条待办"})),
    ("B13 POST todos/clear",       "POST", "/api/sessions/%s/todos/clear" % SID, "none", BODY_EMPTY),
    ("B14 POST todos/purge",       "POST", "/api/sessions/%s/todos/purge" % SID, "none", BODY_EMPTY),
    ("B15 POST todos/backfill",    "POST", "/api/sessions/%s/todos/backfill" % SID, "none",
     BODY_EMPTY),
    ("B16 POST todos/enrich",      "POST", "/api/sessions/%s/todos/enrich" % SID, "none",
     BODY_EMPTY),
    ("B17 PATCH todo tailnet",     "PATCH", "/api/todos/todo_0001", "none", J({"status": "done"})),
    ("B18 GET whoami tailnet",     "GET", "/api/whoami", "none", None),
    ("B19 HEAD whoami tailnet",    "HEAD", "/api/whoami", "none", None),
    ("B20 POST healthz tailnet",   "POST", "/healthz", "none", BODY_EMPTY),
]

# ---- C 相：计算器后台不可用（上游指向死端口）→ 502，绝不影响本服务其它功能 ----
CASES_CALC_DOWN = [
    ("C01 POST calc 上游死",       "POST", "/calc/api/ok", "cookie", BODY_EMPTY),
    ("C02 GET calc 上游死",        "GET",  "/calc/api/ok", "cookie", None),
    ("C03 HEAD calc 上游死",       "HEAD", "/calc/api/ok", "cookie", None),
    ("C04 POST healthz 仍正常",    "POST", "/healthz", "cookie", BODY_EMPTY),
]

# ---- keep-alive：@GW.pre 的 _drain_body() 有没有把 socket 排空 ----
KEEPALIVE = [
    ("POST", "/api/zzz", "cookie", J({"padding": "x" * 5000})),      # 501，带大 body
    ("POST", "/nope", "cookie", J({"padding": "y" * 5000})),         # 405 静态，带大 body
    ("POST", "/api/sessions/%s/messages" % SID, "cookie", MSG),      # 正常写
    ("GET",  "/healthz", "none", None),                              # 连内最后一例要对得上
    ("HEAD", "/api/whoami", "cookie", None),
]


# ------------------------------------------------------------------ 主流程

def run(title, cases, p_old, p_new):
    bad = 0
    print("\n== %s ==" % title)
    print("%-34s %-8s %-46s %-10s %s" % ("用例", "方法", "路径", "鉴权", "结论"))
    print("-" * 130)
    for name, method, path, auth, body in cases:
        a = call(p_old, method, path, auth, body)
        b = call(p_new, method, path, auth, body)
        ok = a == b
        bad += 0 if ok else 1
        verdict = ("一致  %s %s" % (a[0], a[1])) if ok else (
            "**不一致**\n    旧=%r\n    新=%r" % (a, b))
        print("%-34s %-8s %-46s %-10s %s" % (name, method, path[:46], auth, verdict))
    return bad


def run_keepalive(p_old, p_new):
    print("\n== 同一条 TCP 连接连发 5 例（keep-alive 不错位 = 请求体排空了）==")
    a = call_keepalive(p_old, KEEPALIVE)
    b = call_keepalive(p_new, KEEPALIVE)
    bad = 0
    for i, (method, path, auth, _body) in enumerate(KEEPALIVE):
        ax = a[i] if i < len(a) else None
        bx = b[i] if i < len(b) else None
        ok = ax == bx and ax is not None
        bad += 0 if ok else 1
        verdict = ("一致  %s %s" % (ax[0], ax[1])) if ok else (
            "**不一致**\n    旧=%r\n    新=%r" % (ax, bx))
        print("%-34s %-8s %-46s %-10s %s" % ("K%d 连内第 %d 例" % (i + 1, i + 1),
                                             method, path[:46], auth, verdict))
    return bad


_TRIPLE_RX = re.compile(rb'"""(?:.|\n)*?"""|\'\'\'(?:.|\n)*?\'\'\'')
_COMMENT_RX = re.compile(rb'#[^\n]*')


def _code_only(raw):
    """只留**可执行代码**：去掉三引号块（文档串）与 `#` 注释。

    不去不行 —— 本刀在 chat_svc.py 的注释里写了「本件搜不到 text/event-stream /
    chunked / Transfer-Encoding」这句话本身，不剥注释的话断言会被自己的注释绊倒
    （真有流式面的话，标记会出现在 `send_header(...)` 这类**代码**里，不在注释里）。
    """
    return _COMMENT_RX.sub(b"", _TRIPLE_RX.sub(b"", raw))


def check_no_streaming():
    """硬约束 3 的**可复跑断言**：本件新旧两侧都没有流式 / 长连接写法。

    有流式面的话，那一支绝不能走 L1 的缓冲封装；本件一个标记都不命中，
    所以「整条应答路径走 L1」是安全的 —— 这条断言就是它的证据。
    """
    print("\n== 硬约束 3 · 流式面存在性断言（只扫可执行代码，剥掉注释与文档串）==")
    bad = 0
    for label, path in (("旧实现", OLD_FILE), ("新实现", os.path.join(AG, "chat_svc.py"))):
        raw = _code_only(io.open(path, "rb").read())
        hits = [m.decode("utf-8") for m in STREAM_MARKERS if m in raw]
        if hits:
            bad += 1
            print("  ✗ %s 命中流式标记：%s —— 那一支必须原样直写 socket" % (label, hits))
        else:
            print("  ✓ %s 无 text/event-stream / chunked / Transfer-Encoding / flush / yield" % label)
    return bad


def check_content_length_always(p_new):
    """补一条：每个响应都带确切的 `Content-Length`，没有一例是缺省 / chunked。

    这是「没被流式化、也没被 L1 改成分块」的正面证据（硬约束 3 的另一半）。
    HEAD 的 Content-Length 报的是**真实体长**而不是 0 —— 这条也一起验。
    """
    print("\n== 硬约束 3 · 每例都有确切 Content-Length（无 chunked / 无缺省）==")
    bad = 0
    probes = [("GET", "/healthz", "none"), ("GET", "/group.html", "cookie"),
              ("GET", "/big.bin", "cookie"), ("POST", "/healthz", "none"),
              ("HEAD", "/group.html", "cookie"), ("HEAD", "/healthz", "none")]
    for method, path, auth in probes:
        # 这里要的是**原始**体长（`call()` 会归一化正文，长度就不是发出去的那个了）
        st, hdrs, body = call_raw(p_new, method, path, auth, None)
        h = dict(hdrs)
        cl, te = h.get("content-length"), h.get("transfer-encoding")
        ok = cl is not None and te is None and cl.isdigit()
        if ok and method != "HEAD":
            ok = int(cl) == len(body)
        if ok and method == "HEAD":
            ok = len(body) == 0 and int(cl) > 0      # 不写体，但报真实长度
        bad += 0 if ok else 1
        print("  %s %-6s %-16s -> %s  Content-Length=%s  Transfer-Encoding=%s  体长=%d"
              % ("✓" if ok else "✗", method, path, st, cl, te, len(body)))
    return bad


def make_ui(root):
    """造一棵确定性的 ui/ 树（内容固定，两侧共用同一棵 —— 前端页要求一字不动）。"""
    os.makedirs(os.path.join(root, "sub"), exist_ok=True)
    io.open(os.path.join(root, "group.html"), "wb").write(
        b"<!doctype html><html><head><meta charset=utf-8><title>group</title></head>"
        b"<body><div id=app>\xe7\xbe\xa4\xe9\xa1\xb5\xe9\x9d\xa2</div></body></html>\n")
    io.open(os.path.join(root, "sub", "group.html"), "wb").write(b"<html>sub</html>\n")
    io.open(os.path.join(root, "a.css"), "wb").write(b"body{margin:0}\n")
    io.open(os.path.join(root, "a.js"), "wb").write(b"console.log(1)\n")
    io.open(os.path.join(root, "a.png"), "wb").write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    io.open(os.path.join(root, "calc.html"), "wb").write(b"<html>calc</html>\n")
    io.open(os.path.join(root, "serve.py"), "wb").write(b"print(1)\n")
    io.open(os.path.join(root, ".hidden"), "wb").write(b"secret\n")
    io.open(os.path.join(root, "big.bin"), "wb").write(b"A" * (1024 * 1024))
    return root


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", default=None,
                    help="迁移前的 commit/ref；给了就重新取快照覆盖 _old/chat_svc_old_8a.py")
    args = ap.parse_args()

    if args.ref:
        materialize_old(args.ref)
    if not os.path.isfile(OLD_FILE):
        print("缺少旧实现快照 %s —— 用 --ref <迁移前提交> 生成" % OLD_FILE)
        return 2

    sys.path[:0] = [OLD, AG]
    import chat_svc_old                                        # noqa: E402
    import chat_svc                                            # noqa: E402
    import view_auth                                           # noqa: E402

    global COOKIE_NAME
    COOKIE_NAME = view_auth.SESSION_COOKIE

    import tempfile
    ui = make_ui(tempfile.mkdtemp(prefix="k8a-ui-"))
    setup_runtime(chat_svc_old, ui)
    setup_runtime(chat_svc, ui)

    # 计算器上游：假后台（A/B 相）
    calc_srv, calc_port = _CalcUpstream.make()
    up_live = "http://127.0.0.1:%d" % calc_port
    for mod in (chat_svc_old, chat_svc):
        mod.ChatHandler.CHAT_CALC_UPSTREAM = up_live
        mod.ChatHandler.CHAT_ADMIN_UI_DIR = os.path.join(ui, "admin-ui-absent")

    p_old, p_new = free_port(), free_port()
    s_old = start(chat_svc_old.ChatHandler, p_old)
    s_new = start(chat_svc.ChatHandler, p_new)

    print("旧实现 %s   :%d" % (OLD_FILE, p_old))
    print("新实现 %s   :%d" % (os.path.join(AG, "chat_svc.py"), p_new))
    print("假计算器上游 :%d    ui/ = %s    cookie 名 = %s" % (calc_port, ui, COOKIE_NAME))

    bad = 0
    bad += check_no_streaming()

    # --- A 相 + GET 面：非 tailnet（严格鉴权）---
    view_auth.in_tailnet = lambda ip: False
    bad += run("A 相 · 非 tailnet · 新登记的非 GET 路由（无 token / 有 cookie 各一）",
               CASES_NOAUTH, p_old, p_new)
    bad += run("未登记路由 · 留给刀8b 的 GET 面（证明没被顺手改掉）",
               CASES_GET_8B, p_old, p_new)
    bad += check_content_length_always(p_new)
    bad += run_keepalive(p_old, p_new)

    # --- B 相：tailnet 免登录 ---
    view_auth.in_tailnet = lambda ip: True
    bad += run("B 相 · tailnet 免登录（能到 2xx 的都在这里）", CASES_TAILNET, p_old, p_new)

    # --- C 相：计算器后台不可用 ---
    #
    view_auth.in_tailnet = lambda ip: False
    dead_srv, dead_port = _DeadUpstream.make()
    up_dead = "http://127.0.0.1:%d" % dead_port
    for mod in (chat_svc_old, chat_svc):
        mod.ChatHandler.CHAT_CALC_UPSTREAM = up_dead
    bad += run("C 相 · 计算器后台不可用 → 502", CASES_CALC_DOWN, p_old, p_new)

    total = (len(CASES_NOAUTH) + len(CASES_GET_8B) + len(CASES_TAILNET)
             + len(CASES_CALC_DOWN) + len(KEEPALIVE) + 6 + 2)
    for srv in (s_old, s_new, calc_srv):
        srv.shutdown()
        srv.server_close()
    dead_srv.close()

    print("\n" + "=" * 130)
    if bad:
        print("✗ 有 %d 处不一致（共 %d 例）" % (bad, total))
        return 1
    print("✓ 全部 %d 例逐字节一致（状态码 + 状态原因 + 有序响应头 + 归一后正文）" % total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
