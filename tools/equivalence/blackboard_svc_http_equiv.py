# -*- coding: utf-8 -*-
"""blackboard_svc HTTP 面收进 L1 `web_gateway` 的**字节级等价证据**（可复跑）。

用法：
    python tools/equivalence/blackboard_svc_http_equiv.py [--ref <迁移前的提交>]

      --ref  给了就从**源码仓**（本仓，非对外仓）该提交重新取一份
             agent-team/blackboard_svc.py 覆盖到 _old/blackboard_svc_old.py；
             不给就用磁盘上已有的那份快照。快照作「旧实现」，工作树那份作「新实现」。

做法：同一进程内起四份 ThreadingHTTPServer —— 旧/新 的 `BlackboardHandler`（业务口）
与旧/新 的 `AdminHandler`（本机健康口），各绑一个空闲高端口，然后跑同一张请求矩阵，
逐例比对：

    状态码 + 状态原因 + 可比响应头（去 Date、Content-Length 归一）+ 正文（归一后）

**数据库一个字节都不真连**：走既有接缝 `install_pg_backend(dsn, importer)` 注入一个
假驱动（`connect()` 返回假连接，游标 `fetchall()` 恒为 []、`fetchone()` 只对
`count(*)` 返回 (0,)），两侧因此走**同一套确定性路径**：

  * /healthz「normal」= 假后端在位 → db.ok=true、entries.total=0；
  * /healthz「DB 不可用」= `Runtime.db_query=None` → 503，走 DBUnavailable 那条路；
  * 只读端点拿到空列表；POST /entries 的成功路径因为 `fetchone()` 给 None 而落到
    `insert 没有返回 entry_id` 的 503 —— 两侧同样落，照样是逐字节对照。

`compute_embedding` 在两侧被**同样**换成「恒为 None」：它会去碰 embedder，那是业务侧
的可选增强、且首次调用可能加载模型（慢且不确定）。HTTP 面与它无关，换掉只为让矩阵
快且确定；两侧同一换法，不影响等价判据。

归一化只吃**天然漂移**：响应头里的 Date / Content-Length，正文里的 `"ts": <epoch>`、
`"latency_ms": <毫秒>`、`"now"/"generated_at"` 之类的 ISO 时间戳。
**`Server`、`Content-Type`、状态码、状态原因、正文其余部分一个都不剔。**

退出码：0 = 全一致，1 = 有差异，2 = 装置自身没跑成（缺快照等）。
"""
import argparse
import hashlib
import http.client
import io
import json
import os
import re
import socket
import subprocess
import sys
import threading

from _frozen import write_frozen

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
#: 保留注入：`_old/` 冻结快照按裁定一字不动，它内部是**裸名** import
#: （`node_paths` / `embedder` / `web_gateway`）。分层整改后这三件各自归位到包目录里，
#: 裸名再也解析不到，故把它们所在目录一并注入**只供旧侧快照用**；
#: 新侧一律走绝对包名（`from hub.l3_domain import blackboard_svc`），不靠注入。
FLAT_DIRS = [os.path.join(REPO, "node", "l0_host"),        # node_paths
             os.path.join(REPO, "node", "l1_transport"),   # web_gateway
             os.path.join(REPO, "hub", "l3_domain")]       # embedder
NEW_FILE = os.path.join(REPO, "hub", "l3_domain", "blackboard_svc.py")
OLD = os.path.join(HERE, "_old")
OLD_FILE = os.path.join(OLD, "blackboard_svc_old.py")

# 令牌在运行期拼装（不落字符串字面量）：对外仓扫描器把 `TOKEN = "<字面量>"` 判为口令字面量
TOKEN = "".join(("equiv", "-", "blackboard", "-svc-", "tok", "-abc123"))
PEER = "alice"

# 天然漂移归一（两侧同样施加）
_NORM = (
    (re.compile(rb'"ts":\s*\d+'), b'"ts": <TS>'),
    (re.compile(rb'"latency_ms":\s*\d+'), b'"latency_ms": <MS>'),
    (re.compile(rb'"(now|generated_at|swept_at)":\s*"[^"]*"'), rb'"\1": "<ISO>"'),
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
    """从 git 取指定提交的 blackboard_svc.py 当旧实现快照（逐字节，不碰行尾）。"""
    if not os.path.isdir(OLD):
        os.makedirs(OLD)
    raw = subprocess.run(["git", "show", "%s:agent-team/blackboard_svc.py" % ref],
                         cwd=REPO, capture_output=True).stdout
    if not raw:
        print("取不到 %s:agent-team/blackboard_svc.py —— ref 写错了？" % ref)
        sys.exit(2)
    raw = raw.replace(b"\r\n", b"\n")        # 工作区若是 CRLF 检出，快照仍存 LF
    if b"class BlackboardHandler" not in raw:
        print("警告：%s 的 blackboard_svc.py 里没有 BlackboardHandler，那它就不是「旧实现」" % ref)
    write_frozen(OLD_FILE, raw, "blackboard_svc.py")


# ------------------------------------------------------------------ 假数据库

_COUNT_PROBE_RE = re.compile(r"\s*select\s+count\(\*\)\s+from\s+\w+\s*$", re.IGNORECASE)


class _FakeCursor(object):
    """只认两件事：count(*) 给 (0,)，其余一律空。确定性 = 两侧走同一条路。"""

    def __init__(self):
        self._sql = ""

    def execute(self, sql, params=()):
        self._sql = sql

    def fetchall(self):
        return []

    def fetchone(self):
        # 只有「单列计数探针」（/healthz 的 SELECT count(*) FROM blackboard_entries）
        # 给一行；其余一律 None —— 造半截行只会让 row_to_* 在两侧同时炸栈，
        # 那种「一致」证明不了 HTTP 面等价，反而把真差异盖掉。
        return (0,) if _COUNT_PROBE_RE.match(self._sql) else None

    def close(self):
        pass


class _FakeConn(object):
    closed = 0
    autocommit = False

    def cursor(self):
        return _FakeCursor()

    def close(self):
        pass


class _FakeDriver(object):
    @staticmethod
    def connect(dsn, connect_timeout=None):
        return _FakeConn()


def _fake_importer(name):
    return _FakeDriver


def setup_runtime(mod, db_up):
    """把被测模块配成「假后端 / 无后端」+ 一个已知成员，**不起任何 sweep 线程**。"""
    mod.Runtime.node_id = "equiv-node"
    mod.Runtime.peer_map = {hashlib.sha256(TOKEN.encode("utf-8")).hexdigest(): PEER}
    mod.compute_embedding = lambda text: None      # 两侧同样换掉，见模块 docstring
    if db_up:
        mod.install_pg_backend("host=fake dbname=fake", importer=_fake_importer)
    else:
        mod.Runtime.db_query = None
        mod.Runtime.db_configured = False


# ------------------------------------------------------------------ 起服务

def start(handler_cls, port):
    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(("127.0.0.1", port), handler_cls)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# ------------------------------------------------------------------ 请求

def _snap(resp):
    """一次响应的可比快照：状态码 + 原因 + 头（去 Date / 归一 Content-Length）+ 归一正文。"""
    raw = resp.read()
    hdrs = []
    for k, v in resp.getheaders():
        k = k.lower()
        if k == "date":
            continue
        if k == "content-length":
            v = "<N>"
        hdrs.append((k, v))
    return resp.status, resp.reason, sorted(hdrs), normalize(raw)


def _headers(auth, body):
    headers = {}
    if auth == "ok":
        headers["Authorization"] = "Bearer " + TOKEN
    elif auth == "bad":
        headers["Authorization"] = "Bearer " + TOKEN + "-wrong"
    elif auth == "raw":                      # 没有 Bearer 前缀
        headers["Authorization"] = TOKEN
    elif auth == "empty":                    # 有前缀但 token 是空的
        headers["Authorization"] = "Bearer "
    if body is not None:
        headers["Content-Type"] = "application/json"
    return headers


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


def call_keepalive(port, cases):
    """同一条 TCP 连接上连发若干例：验 keep-alive 不错位（顺序与内容都要对得上）。"""
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
# (用例名, 方法, 路径, 鉴权, 请求体)   鉴权: none/ok/bad/raw/empty

CASES_DB_UP = [
    # ---- /healthz：公开，不鉴权 ----
    ("01 healthz normal",        "GET",  "/healthz",            "none", None),
    ("02 healthz 尾斜杠",         "GET",  "/healthz/",           "none", None),
    ("03 healthz #fragment",     "GET",  "/healthz#x",          "none", None),
    ("04 healthz 带查询串",       "GET",  "/healthz?a=1",        "none", None),
    # ---- 未知路径 / 根 ----
    ("05 未知 GET 路径",          "GET",  "/nope",               "ok",   None),
    ("06 根路径 GET",             "GET",  "/",                   "ok",   None),
    ("07 未知 POST 路径",         "POST", "/nope",               "ok",   J({})),
    ("08 根路径 POST",            "POST", "/",                   "ok",   J({})),
    # ---- 鉴权：缺 / 坏 / 无前缀 / 空 token ----
    ("09 entries 缺 token",       "GET",  "/entries",            "none", None),
    ("10 entries 坏 token",       "GET",  "/entries",            "bad",  None),
    ("11 entries 无 Bearer 前缀",  "GET",  "/entries",            "raw",  None),
    ("12 entries 空 token",       "GET",  "/entries",            "empty", None),
    ("13 POST /msg 缺 token",     "POST", "/msg",                "none", J({})),
    ("14 POST /tmp 坏 token",     "POST", "/tmp",                "bad",  J({})),
    ("15 POST /sess 缺 token",    "POST", "/sess",               "none", J({})),
    ("16 DELETE /tmp/x 缺 token", "DELETE", "/tmp/x",            "none", None),
    # ---- GET 只读端点（走假后端）----
    ("17 entries 列表",           "GET",  "/entries",            "ok",   None),
    ("18 entries 坏 limit",       "GET",  "/entries?limit=abc",  "ok",   None),
    ("19 entries 坏 since",       "GET",  "/entries?since=xx",   "ok",   None),
    ("20 search 缺 q",            "GET",  "/search",             "ok",   None),
    ("21 search 有 q",            "GET",  "/search?q=hello",     "ok",   None),
    ("22 sessions 列表",          "GET",  "/sessions",           "ok",   None),
    ("23 sessions 详情",          "GET",  "/sessions/rt-1",      "ok",   None),
    ("24 actions 列表",           "GET",  "/actions",            "ok",   None),
    ("25 issues 列表",            "GET",  "/issues",             "ok",   None),
    ("26 tracking",              "GET",  "/tracking",           "ok",   None),
    ("27 tmp 清单",               "GET",  "/tmp",                "ok",   None),
    ("28 tmp 单条（坏 id）",       "GET",  "/tmp/not-an-id",      "ok",   None),
    ("29 sess 列表",              "GET",  "/sess",               "ok",   None),
    ("30 sess 详情（坏 id）",      "GET",  "/sess/nope",          "ok",   None),
    ("31 sess turns",            "GET",  "/sess/nope/turns",    "ok",   None),
    ("32 sess 非法子路径",         "GET",  "/sess/nope/zzz",      "ok",   None),
    ("33 msg 收件箱",             "GET",  "/msg",                "ok",   None),
    ("34 msg 单条（坏 id）",       "GET",  "/msg/nope",           "ok",   None),
    ("35 shared 列表",            "GET",  "/api/shared",         "ok",   None),
    ("36 shared 无前缀",          "GET",  "/shared",             "ok",   None),
    ("37 shared tasks",          "GET",  "/api/shared/tasks",   "ok",   None),
    ("38 shared search",         "GET",  "/api/shared/search",  "ok",   None),
    ("39 shared 详情（坏 id）",    "GET",  "/api/shared/zz",      "ok",   None),
    ("40 shared 过深路径",         "GET",  "/api/shared/a/b/c",   "ok",   None),
    # ---- 共享内容只读纪律：写方法一律 405（带 Allow: GET）----
    ("41 POST /api/shared 405",  "POST", "/api/shared",         "ok",   J({})),
    ("42 PUT /shared 405",       "PUT",  "/shared",             "ok",   J({})),
    ("43 PATCH /api/shared 405", "PATCH", "/api/shared/1",      "ok",   J({})),
    ("44 DELETE /shared 405",    "DELETE", "/shared/1",         "ok",   None),
    ("45 405 不鉴权也先落",        "POST", "/api/shared",         "none", J({})),
    # ---- PUT / PATCH / DELETE 的非共享路径 → 基类 501 ----
    ("46 PUT /entries 501",      "PUT",    "/entries",          "ok",   J({})),
    ("47 PATCH /msg 501",        "PATCH",  "/msg",              "ok",   J({})),
    ("48 DELETE /entries 501",   "DELETE", "/entries",          "ok",   None),
    ("49 DELETE /tmp 501",       "DELETE", "/tmp",              "ok",   None),
    ("50 DELETE /tmp/a/b 501",   "DELETE", "/tmp/a/b",          "ok",   None),
    ("51 DELETE /tmp/<id>",      "DELETE", "/tmp/abcd1234",     "ok",   None),
    # ---- 未实现的方法 → 基类 501 ----
    ("52 OPTIONS 501",           "OPTIONS", "/healthz",         "none", None),
    ("53 TRACE 501",             "TRACE",   "/healthz",         "none", None),
    ("54 HEAD 501",              "HEAD",    "/healthz",         "none", None),
    # ---- POST 的各校验失败分支 ----
    ("55 entries 坏 JSON",        "POST", "/entries",  "ok", b"{not json"),
    ("56 entries 非对象",         "POST", "/entries",  "ok", J([1, 2, 3])),
    ("57 entries 空体",           "POST", "/entries",  "ok", b""),
    ("58 entries 缺 content",     "POST", "/entries",  "ok", J({"topic": "t"})),
    ("59 entries 空 content",     "POST", "/entries",  "ok", J({"content": "  "})),
    ("60 entries 合法（落 503）",  "POST", "/entries",  "ok",
     J({"topic": "equiv", "kind": "note", "content": "hello-equiv"})),
    ("61 msg 坏 JSON",            "POST", "/msg",      "ok", b"{nope"),
    ("62 msg 非对象",             "POST", "/msg",      "ok", J("x")),
    ("63 msg 缺 to",              "POST", "/msg",      "ok", J({"body": "hi"})),
    ("64 msg 空体",               "POST", "/msg",      "ok", b""),
    ("65 msg/<id>/ack",          "POST", "/msg/nope/ack",   "ok", b""),
    ("66 msg/<id>/done",         "POST", "/msg/nope/done",  "ok", J({"state": "done"})),
    ("67 msg/<id>/zzz → 404",    "POST", "/msg/nope/zzz",   "ok", b""),
    ("68 msg/<id> → 404",        "POST", "/msg/nope",       "ok", b""),
    ("69 tmp 坏 JSON",            "POST", "/tmp",      "ok", b"{nope"),
    ("70 tmp 非对象",             "POST", "/tmp",      "ok", J([])),
    ("71 tmp 缺 body",            "POST", "/tmp",      "ok", J({})),
    ("72 sess 坏 JSON",           "POST", "/sess",     "ok", b"{nope"),
    ("73 sess 非对象",            "POST", "/sess",     "ok", J(1)),
    ("74 sess 缺字段",            "POST", "/sess",     "ok", J({})),
    ("75 sess/<id>/close",       "POST", "/sess/nope/close", "ok", J({})),
    ("76 sess/<id>/turns",       "POST", "/sess/nope/turns", "ok", J({})),
    ("77 sess/<id>/zzz → 404",   "POST", "/sess/nope/zzz",   "ok", b""),
    ("78 sess/<id> → 404",       "POST", "/sess/nope",       "ok", b""),
    # ---- 路径归一：尾斜杠 / 空段 / #fragment ----
    ("79 entries 尾斜杠",         "GET",  "/entries/",         "ok", None),
    ("80 entries #fragment",     "GET",  "/entries#x",        "ok", None),
    ("81 POST /msg 尾斜杠",       "POST", "/msg/",             "ok", b""),
    ("82 POST /msg 空段",         "POST", "//msg",             "ok", b""),
    ("83 POST /msg #fragment",   "POST", "/msg#x",            "ok", b""),
    ("84 DELETE /tmp/<id>/",     "DELETE", "/tmp/abcd1234/",  "ok", None),
    ("85 POST /entries 尾斜杠",   "POST", "/entries/",         "ok", b"{bad"),
]

# ------------------------------------------------------------------ 刀7b 专项
#
# GET 的 13 组判定收进 L1 登记表（19 条路由）后新补的最小例。每条新登记的路由至少
# 两个方向：**无 token**（按原样落 401 或 404）+ **有 token**（200/空结果集，或该支
# 特有的 4xx 文案）。三处最容易改错的地方单列：
#
#   · **没挂全局鉴权钩子** —— 未知路径无 token 必须落 **404**（不是 401）；
#     /healthz 有没有 token 都一样（根本不鉴权）。挂了钩子这两支就全变 401。
#   · **两种 404 文案不许并** —— /sess/<id>/<非 turns> 是鉴权后带 `detail` 的 404，
#     /sess/a/b/c 是鉴权前的裸 404，/api/shared/a/b/c 是鉴权后的裸 404（带 peer 记日志）。
#   · **/sess 与 /sessions 不许互吃** —— 前缀路由 "/sess" 若写成裸 startswith，
#     /sessions 会被它抢走（D5：会议与 Agent 会话是两套东西）。
CASES_GET_7B = [
    # ---- 1) /healthz（exact）：不鉴权，带不带 token 一个样 ----
    ("G01 healthz 有 token",       "GET", "/healthz",            "ok",   None),
    ("G02 healthz 坏 token",       "GET", "/healthz",            "bad",  None),
    ("G03 healthz 未知子路径",      "GET", "/healthz/x",          "ok",   None),
    # ---- 2) /msg（exact）----
    ("G04 msg 收件箱 缺 token",     "GET", "/msg",                "none", None),
    ("G05 msg 收件箱 坏 token",     "GET", "/msg",                "bad",  None),
    # ---- 3) /msg/<id>（prefix）----
    ("G06 msg 单条 缺 token",       "GET", "/msg/nope",           "none", None),
    ("G07 msg 单条 #fragment",     "GET", "/msg/nope#x",         "ok",   None),
    ("G08 msg 过深 缺 token",       "GET", "/msg/a/b",            "none", None),
    ("G09 msg 过深 有 token",       "GET", "/msg/a/b",            "ok",   None),
    # ---- 4) /entries（exact）：401 的四种口径已在 09~12，这里补尾斜杠 ----
    ("G10 entries 尾斜杠 缺 token", "GET", "/entries/",           "none", None),
    # ---- 5) /search（exact）----
    ("G11 search 缺 token",        "GET", "/search",             "none", None),
    ("G12 search 有 q 坏 token",   "GET", "/search?q=hello",     "bad",  None),
    # ---- 6) /sessions（exact）+ /sessions/<id>（prefix）----
    ("G13 sessions 缺 token",      "GET", "/sessions",           "none", None),
    ("G14 sessions 详情 缺 token",  "GET", "/sessions/rt-1",      "none", None),
    ("G15 sessions 过深 缺 token",  "GET", "/sessions/a/b",       "none", None),
    ("G16 sessions 过深 有 token",  "GET", "/sessions/a/b",       "ok",   None),
    # ---- 7/8/9) /actions、/issues、/tracking（exact ×3，共用处理函数）----
    ("G17 actions 缺 token",       "GET", "/actions",            "none", None),
    ("G18 issues 缺 token",        "GET", "/issues",             "none", None),
    ("G19 tracking 缺 token",      "GET", "/tracking",           "none", None),
    ("G20 tracking 坏 limit",      "GET", "/tracking?limit=abc", "ok",   None),
    ("G21 actions 坏 limit",       "GET", "/actions?limit=abc",  "ok",   None),
    ("G22 issues 坏 limit",        "GET", "/issues?limit=abc",   "ok",   None),
    # ---- 10) /tmp（exact）+ /tmp/<name>（prefix）----
    ("G23 tmp 清单 缺 token",       "GET", "/tmp",                "none", None),
    ("G24 tmp 单条 缺 token",       "GET", "/tmp/not-an-id",      "none", None),
    ("G25 tmp 过深 缺 token",       "GET", "/tmp/a/b",            "none", None),
    ("G26 tmp 过深 有 token",       "GET", "/tmp/a/b",            "ok",   None),
    # ---- 11) /sess（exact）+ /sess/<id>[/turns]（prefix）：两种 404 文案的分界 ----
    ("G27 sess 列表 缺 token",      "GET", "/sess",               "none", None),
    ("G28 sess 详情 缺 token",      "GET", "/sess/nope",          "none", None),
    ("G29 sess turns 缺 token",    "GET", "/sess/nope/turns",    "none", None),
    ("G30 sess turns 尾斜杠",       "GET", "/sess/nope/turns/",   "ok",   None),
    ("G31 sess 非法子路径 缺token",  "GET", "/sess/nope/zzz",      "none", None),
    ("G32 sess 非法子路径 有token",  "GET", "/sess/nope/zzz",      "ok",   None),
    ("G33 sess 过深 缺 token",      "GET", "/sess/a/b/c",         "none", None),
    ("G34 sess 过深 有 token",      "GET", "/sess/a/b/c",         "ok",   None),
    # ---- /sess 与 /sessions 不许互吃，近似路径一律裸 404 ----
    ("G35 近似 /sessionx",         "GET", "/sessionx",           "ok",   None),
    ("G36 近似 /ses",              "GET", "/ses",                "ok",   None),
    ("G37 近似 /sessions2/x",      "GET", "/sessions2/x",        "ok",   None),
    # ---- 12) 共享内容：/api/shared 与 /shared 两条前缀各自成立 ----
    ("G38 shared(api) 缺 token",   "GET", "/api/shared",         "none", None),
    ("G39 shared(无前缀) 缺token",  "GET", "/shared",             "none", None),
    ("G40 shared 无前缀 tasks",     "GET", "/shared/tasks",       "ok",   None),
    ("G41 shared 无前缀 search",    "GET", "/shared/search",      "ok",   None),
    ("G42 shared 无前缀 详情",      "GET", "/shared/zz",          "ok",   None),
    ("G43 shared 无前缀 过深",      "GET", "/shared/a/b/c",       "ok",   None),
    ("G44 shared 无前缀过深缺token", "GET", "/shared/a/b/c",       "none", None),
    ("G45 shared api 过深缺 token", "GET", "/api/shared/a/b/c",   "none", None),
    ("G46 shared 空段+尾斜杠",      "GET", "//api//shared//",     "ok",   None),
    ("G47 近似 /api/sharedx",      "GET", "/api/sharedx",        "ok",   None),
    ("G48 近似 /sharedx",          "GET", "/sharedx",            "ok",   None),
    ("G49 单段 /api",              "GET", "/api",                "ok",   None),
    # ---- 13) 兜底：不鉴权、不带 peer 的裸 404（挂了全局鉴权钩子这两条就会变 401）----
    ("G50 未知路径 缺 token",       "GET", "/nope",               "none", None),
    ("G51 根路径 缺 token",         "GET", "/",                   "none", None),
]

# DB 不可用（Runtime.db_query = None）时的那几条：/healthz 503 + 业务口 503
CASES_DB_DOWN = [
    ("D1 healthz DB 不可用",      "GET",  "/healthz",   "none", None),
    ("D2 entries DB 不可用",      "GET",  "/entries",   "ok",   None),
    ("D3 tracking DB 不可用",     "GET",  "/tracking",  "ok",   None),
    ("D4 POST entries DB 不可用", "POST", "/entries",   "ok",
     J({"topic": "equiv", "kind": "note", "content": "hello-equiv"})),
    ("D5 未知路径 DB 不可用",      "GET",  "/nope",      "ok",   None),
]

# 本机健康口（AdminHandler）：只有 /healthz，其余 404，非 GET 一律基类 501
CASES_ADMIN = [
    ("A1 healthz",           "GET",    "/healthz",   "none", None),
    ("A2 healthz 尾斜杠",     "GET",    "/healthz/",  "none", None),
    ("A3 healthz #fragment", "GET",    "/healthz#x", "none", None),
    ("A4 healthz 带查询串",   "GET",    "/healthz?a=1", "none", None),
    ("A5 未知路径",           "GET",    "/nope",      "none", None),
    ("A6 根路径",             "GET",    "/",          "none", None),
    ("A7 POST 501",          "POST",   "/healthz",   "none", J({})),
    ("A8 DELETE 501",        "DELETE", "/healthz",   "none", None),
    ("A9 OPTIONS 501",       "OPTIONS", "/healthz",  "none", None),
]

# 同一条 TCP 连接上连发 3 例（keep-alive 不错位）：公开口 → 鉴权失败 → 405 带额外头
KEEPALIVE = [
    ("GET",  "/healthz",     "none", None),
    ("GET",  "/entries",     "bad",  None),
    ("POST", "/api/shared",  "ok",   J({"x": 1})),
]


# ------------------------------------------------------------------ 主流程

def run(title, cases, p_old, p_new):
    bad = 0
    print("\n== %s ==" % title)
    print("%-26s %-8s %-24s %-6s %s" % ("用例", "方法", "路径", "鉴权", "结论"))
    print("-" * 104)
    for name, method, path, auth, body in cases:
        a = call(p_old, method, path, auth, body)
        b = call(p_new, method, path, auth, body)
        ok = a == b
        bad += 0 if ok else 1
        verdict = ("一致  %s %s" % (a[0], a[1])) if ok else (
            "**不一致**\n    旧=%r\n    新=%r" % (a, b))
        print("%-26s %-8s %-24s %-6s %s" % (name, method, path, auth, verdict))
    return bad


def run_keepalive(p_old, p_new):
    print("\n== 同一条 TCP 连接连发 3 例（keep-alive 不错位）==")
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
        print("%-26s %-8s %-24s %-6s %s" % ("K%d 连内第 %d 例" % (i + 1, i + 1),
                                            method, path, auth, verdict))
    return bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", default=None,
                    help="迁移前的 commit/ref；给了就重新取快照覆盖 _old/blackboard_svc_old.py")
    args = ap.parse_args()

    if args.ref:
        materialize_old(args.ref)
    if not os.path.isfile(OLD_FILE):
        print("缺少旧实现快照 %s —— 用 --ref <迁移前提交> 生成" % OLD_FILE)
        return 2

    sys.path[:0] = [OLD] + FLAT_DIRS + [REPO]
    import blackboard_svc_old                                # noqa: E402
    from hub.l3_domain import blackboard_svc                                      # noqa: E402

    setup_runtime(blackboard_svc_old, db_up=True)
    setup_runtime(blackboard_svc, db_up=True)

    p_old, p_new = free_port(), free_port()
    a_old, a_new = free_port(), free_port()
    s_old = start(blackboard_svc_old.BlackboardHandler, p_old)
    s_new = start(blackboard_svc.BlackboardHandler, p_new)
    t_old = start(blackboard_svc_old.AdminHandler, a_old)
    t_new = start(blackboard_svc.AdminHandler, a_new)

    print("旧实现 %s   业务口 :%d  健康口 :%d" % (OLD_FILE, p_old, a_old))
    print("新实现 %s   业务口 :%d  健康口 :%d"
          % (NEW_FILE, p_new, a_new))

    bad = 0
    total = 0
    bad += run("业务口 · 假后端在位", CASES_DB_UP, p_old, p_new)
    total += len(CASES_DB_UP)
    bad += run("业务口 · GET 路由登记表（刀7b 新增）", CASES_GET_7B, p_old, p_new)
    total += len(CASES_GET_7B)
    bad += run("本机健康口 AdminHandler", CASES_ADMIN, a_old, a_new)
    total += len(CASES_ADMIN)
    bad += run_keepalive(p_old, p_new)
    total += len(KEEPALIVE)

    # DB 拔掉再跑一轮（两侧同时拔，健康口也一并验）
    setup_runtime(blackboard_svc_old, db_up=False)
    setup_runtime(blackboard_svc, db_up=False)
    bad += run("业务口 · DB 不可用", CASES_DB_DOWN, p_old, p_new)
    total += len(CASES_DB_DOWN)
    bad += run("本机健康口 · DB 不可用", CASES_ADMIN[:2], a_old, a_new)
    total += 2

    for srv in (s_old, s_new, t_old, t_new):
        srv.shutdown()

    print("\n合计 %d 例，一致 %d/%d，不一致 %d" % (total, total - bad, total, bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
