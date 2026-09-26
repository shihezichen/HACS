# -*- coding: utf-8 -*-
"""刀11 · 群聊前端「传输客户端抽出」等价装置（无头浏览器 + CDP，纯 stdlib）。

比什么
------
旧侧 OLD = ``tools/equivalence/_old/group_old_k11.html``（开工前快照，单文件自带传输层）
新侧 NEW = ``agent-team/ui/group.html`` + ``agent-team/ui/group_transport.js``（抽出后）

两侧**来自两个不同的物理文件**，装置开头打印各自 md5 自证不是自比。

夹具
----
同一个 Python 进程里起一台桩后端（`ThreadingHTTPServer`），两侧共用**同一份冻结夹具**：

* ``/old/<f>`` → `_old/` 目录（只有那一个 html）；``/new/<f>`` → `agent-team/ui/`
* 两个页面里的 ``<script src="/md.js">`` 之类走的是**绝对路径**，所以根路径
  ``/md.js`` ``/tex.js`` ``/hl.js`` ``/codeblock.js`` ``/group_transport.js``
  一律从 `agent-team/ui/` 发（两侧同一份；旧页面不引用 group_transport.js，
  所以它在旧侧根本不会被请求 —— 这一点与线上形态一致）
* ``/api/*`` 全部返回冻结 JSON；``/login`` 返 **204**（见下「已知偏差」）

TSVC（``http://<host>:9981``）是**硬编码常量**，改端口就等于改被测代码。
本装置的做法是 **CDP `Fetch` 域请求拦截 + `Fetch.fulfillRequest`**：

    原理：`Fetch.enable` 登记 urlPattern `*<host>*`；浏览器在**发包之前**
    把请求挂起并推 `Fetch.requestPaused`，装置在 Python 侧按同一份夹具直接
    合成应答（含 `Access-Control-Allow-Origin: *`，顺带把 CORS 预检 OPTIONS
    一并合成）回灌给渲染进程。页面看到的 URL 始终是原始的 <host>:9981，
    被测代码一个字节没动，也不碰系统 hosts / 不需要管理员权限。
    选它而不是 `--host-resolver-rules`：后者还要真在本机 9981 起监听，
    多一个可能被别的进程占用的外部条件，不如拦截可自证。

判据
----
文本级一致 ∧ 标识符探针一致 ∧ 启动轨迹一致 ∧ 轮询轨迹一致 ∧ 动作序轨迹一致
∧ 函数级线级（请求行 + 请求头 + 请求体 + Error.message）逐字节一致
∧ 两侧 JS 错误列表一致  → 打印 EQUIV_SENTINEL_OK，退出 0；否则逐条打 diff，退出 1。

用法：``python tools/equivalence/group_frontend_equiv.py``（可加 ``-v`` 打全量轨迹）
"""

import argparse
import base64
import hashlib
import http.client
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
UI_DIR = os.path.join(ROOT, "agent-team", "ui")
OLD_DIR = os.path.join(ROOT, "tools", "equivalence", "_old")
OLD_HTML = os.path.join(OLD_DIR, "group_old_k11.html")
NEW_HTML = os.path.join(UI_DIR, "group.html")
NEW_JS = os.path.join(UI_DIR, "group_transport.js")

TSVC_HOST = "<host>"
TSVC_ORIGIN = "http://" + TSVC_HOST + ":9981"

EDGE_CANDIDATES = [
    r"C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    r"C:/Program Files/Microsoft/Edge/Application/msedge.exe",
    r"C:/Program Files/Google/Chrome/Application/chrome.exe",
    r"C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
]

# ---------------------------------------------------------------- 客户端身份钉死
# 为什么要钉：浏览器构建号**不是被测行为**。Edge 在本机会静默自动升级（实测 153→154），
# 装置两次运行、甚至同一次运行的 OLD / NEW 两侧（两侧各 launch 一次 exe），可能抓到
# 不同构建 → User-Agent 与 sec-ch-ua 天然不等（154 连 brand 列表顺序和
# "Not A(Brand" 的写法都跟 153 不一样）→ 函数级线级逐字节 diff 非空，是**假红**。
# 做法：两侧各自在「任何页面导航 / 任何网络活动之前」把 UA + UA-CH 元数据钉成同一份
# 常量，让 sec-ch-ua / sec-ch-ua-platform / sec-ch-ua-mobile 全部据此**同源生成**，
# Accept-Language 一并钉死（它来自 profile locale，同样不是被测行为）。
# 钉的是**常量**而不是「当前 Edge 版本」：将来 Edge 再升级，装置输出仍逐字节不变。
# 注意这只钉客户端身份头，请求行 / 其余请求头 / 请求体 / Error.message 仍是强比较。
CLIENT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) HeadlessChrome/154.0.0.0 Safari/537.36 Edg/154.0.0.0")
CLIENT_UA_BRANDS = [{"brand": "Chromium", "version": "154"},
                    {"brand": "Microsoft Edge", "version": "154"},
                    {"brand": "Not A(Brand", "version": "99"}]
CLIENT_UA_METADATA = {
    "brands": CLIENT_UA_BRANDS,
    "fullVersionList": [dict(b, version="154.0.0.0") for b in CLIENT_UA_BRANDS],
    "fullVersion": "154.0.0.0",
    "platform": "Windows",
    "platformVersion": "15.0.0",
    "architecture": "x86",
    "model": "",
    "mobile": False,
    "bitness": "64",
    "wow64": False,
}
# 传**语言表**（不带 q）：CDP 的 acceptLanguage 会自己补 q 值，带 q 传进去会补成
# "zh;q=0.9;q=0.9" 这种畸形串。这里的表与本机 Edge 默认一致，落到线上就是
# "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6"。
CLIENT_ACCEPT_LANGUAGE = "zh-CN,zh,en,en-GB,en-US"

SETTLE_BOOT = 2.0          # 启动 settle：主轮询 3000ms，2s 窗口内不会触发
SETTLE_POLL = 7.0          # 轮询窗口：至少覆盖 2 个 3s 周期
SETTLE_STEP = 0.45         # 动作序每步的静置


# ====================================================================== 冻结夹具

FX_SID = "meet_fixture_0001"
FX_SID2 = "meet_fixture_0002"

FX_SESSION = {
    "session_id": FX_SID,
    "title": "夹具群",
    "kind": "meet",
    "state": "open",
    "created_by": "nb001",
    "members": [{"id": "nb001", "display": "机主", "type": "human"},
                {"id": "hermes", "display": "Hermes", "type": "agent"}],
}
FX_SESSION2 = dict(FX_SESSION, session_id=FX_SID2, title="夹具新群")

FX_MESSAGES = [{
    "message_id": "fx-m-0001",
    "seq": 1,
    "author": {"id": "nb001", "display": "机主", "type": "human"},
    "created_at": "2026-09-21T10:00:00+08:00",
    "text": "夹具消息一条",
    "content": "夹具消息一条",
    "body": "夹具消息一条",
    "meta": {"audience": ["hermes"], "needs_response": []},
}]

FX_TODOS = [{"id": "td-1", "seq": 1, "status": "open", "title": "夹具待办",
             "owner": "hermes", "created_at": "2026-09-21T10:01:00+08:00"}]

FX_TASKS = [{"id": "tk-1", "group_id": FX_SID, "title": "夹具任务", "status": "pending",
             "priority": "normal", "msg_seq": 1,
             "created_at": "2026-09-21T10:02:00+08:00"}]

# 夹具里的落盘路径一律走 /data/nbhome/…（不许出现 Unix 家目录 /home/，对外仓扫描器会判 blocking）
FX_WORKDIR = "/data/nbhome/agent-team/fixtures"


def _sess_obj(sid):
    if sid == FX_SID2:
        return FX_SESSION2
    return dict(FX_SESSION, session_id=sid)


def fixture(method, path, body):
    """(status, content_type, bytes) —— 两侧共用，无任何随机/时钟输入。"""
    p = path.split("?", 1)[0]

    if p == "/login":
        # 已知偏差（见模块末「已知偏差」注释）：204 而非 200
        return 204, "text/plain; charset=utf-8", b""

    if p == "/api/me":
        return 200, "application/json", _j({"ok": True, "id": "nb001", "workdir": FX_WORKDIR})
    if p == "/api/whoami":
        return 200, "application/json", _j({"ok": True, "user": {
            "id": "nb001", "name": "nb001", "display": "机主",
            "role": "owner", "role_cn": "机主", "source": "tailnet"}})
    if p == "/api/agents":
        return 200, "application/json", _j({"ok": True, "agents": [
            {"id": "hermes", "display": "Hermes", "type": "agent"},
            {"id": "openclaw-nb", "display": "OpenClaw", "type": "agent"}]})
    if p == "/api/requires_login":
        return 401, "application/json", _j({"ok": False, "error": {"code": "unauthorized"}})
    if p == "/api/boom":
        return 500, "text/plain; charset=utf-8", "内部错误：这不是 JSON".encode("utf-8")

    if p == "/api/sessions":
        if method == "POST":
            return 200, "application/json", _j({"ok": True, "session": FX_SESSION2,
                                                "session_id": FX_SID2})
        # 两个群都在表里 → createGroup() 建完能 find 到并真的 openGroup（多走一段路），
        # 同时列表本身是冻结的（POST 不改变它），轮询签名恒定
        return 200, "application/json", _j({"ok": True, "sessions": [FX_SESSION, FX_SESSION2]})

    m = re.match(r"^/api/sessions/([^/]+)$", p)
    if m:
        return 200, "application/json", _j({"ok": True, "session": _sess_obj(m.group(1))})
    m = re.match(r"^/api/sessions/([^/]+)/messages$", p)
    if m:
        if method == "POST":
            return 200, "application/json", _j({"ok": True, "seq": 1, "message_id": "fx-m-0001"})
        # 冻结：永远同一批（POST 不改变它）→ CURSOR 恒为 1 → 轮询签名恒定
        return 200, "application/json", _j({"ok": True, "me": "nb001", "messages": FX_MESSAGES})
    m = re.match(r"^/api/sessions/([^/]+)/messages/(\d+)$", p)
    if m:
        return 200, "application/json", _j({"ok": True, "message": FX_MESSAGES[0]})
    m = re.match(r"^/api/sessions/([^/]+)/members$", p)
    if m:
        return 200, "application/json", _j({"ok": True, "members": FX_SESSION["members"]})
    m = re.match(r"^/api/sessions/([^/]+)/todos$", p)
    if m:
        return 200, "application/json", _j({"ok": True, "todos": FX_TODOS})
    m = re.match(r"^/api/sessions/([^/]+)/todos/purge$", p)
    if m:
        return 200, "application/json", _j({"ok": True, "purged": 0})

    # ---- 任务服务（TSVC :9981）面 ----
    if p == "/tasks" or p == "/api/tasks":
        if method == "POST":
            return 200, "application/json", _j({"ok": True, "task": FX_TASKS[0]})
        return 200, "application/json", _j({"ok": True, "tasks": FX_TASKS})
    if re.match(r"^/tasks/[^/]+$", p):
        return 200, "application/json", _j({"ok": True, "task": FX_TASKS[0]})

    return 404, "application/json", _j({"ok": False, "error": {
        "code": "not_found", "message": "夹具里没有这个端点"}})


def _j(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True).encode("utf-8")


# ====================================================================== 桩后端

WIRE = []                 # 线级记录：本机桩收到的每个请求
WIRE_LOCK = threading.Lock()


def wire_add(rec):
    with WIRE_LOCK:
        WIRE.append(rec)
        return len(WIRE)


def wire_len():
    with WIRE_LOCK:
        return len(WIRE)


def wire_slice(a, b):
    with WIRE_LOCK:
        return list(WIRE[a:b])


STATIC_ROOTS = {"old": OLD_DIR, "new": UI_DIR}
ROOT_STATIC = {"/md.js", "/tex.js", "/hl.js", "/codeblock.js", "/group_transport.js"}
MIME = {".js": "text/javascript; charset=utf-8", ".html": "text/html; charset=utf-8",
        ".css": "text/css; charset=utf-8"}


class Stub(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "k11stub"
    sys_version = ""

    def log_message(self, *a):        # 别把桩的访问日志喷进装置输出
        pass

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _record(self, body):
        hdrs = ["%s: %s" % (k, v) for k, v in self.headers.items()]
        wire_add({
            "where": "stub",
            "requestline": self.requestline,
            "path": self.path,
            "method": self.command,
            "headers": hdrs,
            "body": body.decode("utf-8", "replace"),
        })

    def _serve(self):
        body = self._body()
        self._record(body)
        p = self.path.split("?", 1)[0]

        # 1) 静态：/old/<f>、/new/<f>、以及根路径上的那几个兄弟脚本
        fs = None
        m = re.match(r"^/(old|new)/([A-Za-z0-9_.-]+)$", p)
        if m:
            fs = os.path.join(STATIC_ROOTS[m.group(1)], m.group(2))
        elif p in ROOT_STATIC:
            fs = os.path.join(UI_DIR, p.lstrip("/"))
        if fs is not None:
            if os.path.isfile(fs):
                data = open(fs, "rb").read()
                ct = MIME.get(os.path.splitext(fs)[1].lower(), "application/octet-stream")
                return self._out(200, ct, data)
            return self._out(404, "text/plain; charset=utf-8", b"no such static")

        # 2) 夹具
        st, ct, data = fixture(self.command, self.path, body)
        return self._out(st, ct, data)

    def _out(self, status, ctype, data):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if data:
            self.wfile.write(data)

    do_GET = do_POST = do_PATCH = do_PUT = do_DELETE = do_HEAD = _serve

    def do_OPTIONS(self):
        self._record(b"")
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,PATCH,PUT,DELETE,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()


class QuietServer(ThreadingHTTPServer):
    """浏览器关连接是常态（keep-alive 到期 / 页面卸载），别把 ConnectionReset 喷成噪音。"""

    daemon_threads = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ====================================================================== 极简 WebSocket（RFC6455 客户端）

class WS(object):
    """够用就行的 WS 客户端：只发文本帧（掩码），收帧支持 126/127 长度与分片、回 pong。"""

    def __init__(self, url):
        m = re.match(r"^ws://([^/:]+):(\d+)(/.*)$", url)
        if not m:
            raise RuntimeError("认不出的 ws url: " + url)
        host, port, path = m.group(1), int(m.group(2)), m.group(3)
        self.sock = socket.create_connection((host, port), timeout=60)
        self.sock.settimeout(60)
        key = base64.b64encode(os.urandom(16)).decode()
        req = ("GET %s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\n"
               "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
               "Sec-WebSocket-Version: 13\r\n\r\n" % (path, host, port, key))
        self.sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("WS 握手时连接被关")
            buf += chunk
        head, rest = buf.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise RuntimeError("WS 握手失败: " + head.split(b"\r\n")[0].decode("latin1"))
        self.buf = rest

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError("WS 连接被对端关闭")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send(self, text):
        payload = text.encode("utf-8")
        n = len(payload)
        head = bytearray([0x81])
        if n < 126:
            head.append(0x80 | n)
        elif n < 65536:
            head.append(0x80 | 126)
            head += struct.pack(">H", n)
        else:
            head.append(0x80 | 127)
            head += struct.pack(">Q", n)
        mask = os.urandom(4)
        head += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(head) + masked)

    def recv(self):
        """返回一条完整文本消息（自动拼分片、吞掉 ping/pong）。"""
        chunks = []
        while True:
            b0, b1 = self._read(2)
            fin = b0 & 0x80
            op = b0 & 0x0F
            ln = b1 & 0x7F
            if ln == 126:
                ln = struct.unpack(">H", self._read(2))[0]
            elif ln == 127:
                ln = struct.unpack(">Q", self._read(8))[0]
            data = self._read(ln) if ln else b""
            if op == 0x9:                       # ping → pong
                self.sock.sendall(b"\x8a\x80" + os.urandom(4))
                continue
            if op == 0xA:
                continue
            if op == 0x8:
                raise RuntimeError("WS 收到 close 帧")
            chunks.append(data)
            if fin:
                return b"".join(chunks).decode("utf-8", "replace")

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


# ====================================================================== CDP

class CDP(object):
    def __init__(self, ws_url):
        self.ws = WS(ws_url)
        self._id = 0
        self.on_event = {}

    def send(self, method, **params):
        self._id += 1
        self.ws.send(json.dumps({"id": self._id, "method": method, "params": params}))
        return self._id

    def call(self, method, **params):
        want = self.send(method, **params)
        while True:
            msg = json.loads(self.ws.recv())
            if "id" in msg:
                if msg["id"] != want:
                    continue
                if "error" in msg:
                    raise RuntimeError("CDP %s 报错: %s" % (method, msg["error"]))
                return msg.get("result", {})
            h = self.on_event.get(msg.get("method"))
            if h:
                h(msg.get("params") or {})

    def pump(self, seconds):
        """在 seconds 内只处理事件（拦截、弹窗…），不发命令。"""
        end = time.time() + seconds
        while True:
            left = end - time.time()
            if left <= 0:
                return
            self.ws.sock.settimeout(left)
            try:
                msg = json.loads(self.ws.recv())
            except socket.timeout:
                return
            finally:
                self.ws.sock.settimeout(60)
            if "id" in msg:
                continue
            h = self.on_event.get(msg.get("method"))
            if h:
                h(msg.get("params") or {})

    def evaluate(self, expr, await_promise=True):
        r = self.call("Runtime.evaluate", expression=expr, returnByValue=True,
                      awaitPromise=await_promise, userGesture=True)
        if r.get("exceptionDetails"):
            ed = r["exceptionDetails"]
            raise RuntimeError("页面里抛异常: " + json.dumps(ed, ensure_ascii=False)[:600])
        return r.get("result", {}).get("value")

    def close(self):
        self.ws.close()


# ====================================================================== 探针脚本（两侧完全一致）

PROBE_JS = r"""
(function () {
  window.__tr = [];
  window.__errs = [];
  var of = window.fetch;
  window.fetch = function (input, init) {
    try {
      var i = init || {};
      var url = (typeof input === "string") ? input
              : (input && input.url) ? input.url : String(input);
      var method = i.method || (input && input.method) || "GET";
      var body = (i.body === undefined) ? null : String(i.body);
      var cred = (i.credentials === undefined) ? null : i.credentials;
      var hs = null;
      if (i.headers) { hs = []; for (var k in i.headers) { hs.push(k + ": " + i.headers[k]); } hs.sort(); }
      window.__tr.push({ method: method, url: url, body: body, credentials: cred, headers: hs });
    } catch (e) { /* 探针自己绝不改变页面行为 */ }
    return of.apply(this, arguments);
  };
  window.addEventListener("error", function (ev) {
    window.__errs.push("error: " + (ev && ev.message));
  });
  window.addEventListener("unhandledrejection", function (ev) {
    var r = ev && ev.reason;
    window.__errs.push("unhandledrejection: " + ((r && r.message) || String(r)));
  });
})();
"""

IDENT_PROBES = ["typeof api", "typeof tApi", "typeof pull", "typeof boot",
                "API", "TSVC", "TGROUP_DEFAULT", "typeof window.api", "typeof window.tApi"]

# 函数级冻结输入（401 那例放最后：它会写 location.href）
FROZEN = [
    ("api",  "GET",   "/api/me", None),
    ("api",  "GET",   "/api/zzz_notexist", None),
    ("api",  "POST",  "/api/sessions", {"name": "x"}),
    ("api",  "GET",   "/api/boom", None),
    ("tApi", "GET",   "/api/tasks", None),
    ("tApi", "GET",   "/api/zzz_notexist", None),
    ("api",  "GET",   "/api/requires_login", None),
]

ACTIONS = [
    ("建群·打开对话框", '$("newbtn").click(); return "ok";'),
    ("建群·填标题并创建", '$("gt").value = "夹具新群"; $("gok").click(); return "ok";'),
    ("开群·点第一个群条目",
     'var g = document.querySelector("#groups .gitem"); if (g) { g.click(); }'
     ' return (g ? "ok" : "no-gitem");'),
    ("发一条消息",
     '$("text").value = "夹具发言"; $("text").dispatchEvent(new Event("input", {bubbles:true}));'
     ' $("send").click(); return "ok";'),
    ("加 Agent·打开对话框", '$("addagent").click(); return "ok";'),
    # 可选项判据抄 addAgentsToGroup() 自己的口径：置灰的那些 style.cursor = "not-allowed"
    ("加 Agent·选一个并加入",
     'var cs = Array.prototype.slice.call(document.querySelectorAll("#addsel .chip"));'
     ' var c = cs.filter(function (x) { return x.style.cursor !== "not-allowed"; })[0];'
     ' if (c) { c.click(); } $("addok").click();'
     ' return (c ? "ok:" + c.dataset.m : "no-candidate");'),
    ("切到「任务队列」TAB", '$("tab-queue").click(); return "ok";'),
    ("刷新", '$("refresh").click(); return "ok";'),
]

# 动作序前后都掐掉主轮询：轮询「发什么」已在 A2 轮询轨迹里单独逐行比过，
# 动作序要的是「动作发不发 / 发什么 / 顺序」，掐掉定时器让它不受调度抖动影响。
KILL_TIMER = 'try { if (typeof TIMER !== "undefined" && TIMER) { clearInterval(TIMER); TIMER = null; } } catch (e) {} "ok"'


# ====================================================================== 归一化

NORM_RULES = [
    # (名字, 正则, 替换) —— 两侧同一套代码、同一顺序
    ("next=<PAGE>（401 跳转带回的本页路径；两侧页面文件名天然不同）",
     re.compile(r"next=[^\s&\"]*"), "next=<PAGE>"),
    ("Referer: <referer>（两侧页面 URL 天然不同）",
     re.compile(r"^(Referer|referer):\s.*$", re.M), r"\1: <referer>"),
    ("<page-url>（正文/URL 里出现的本页绝对地址）",
     re.compile(r"http://127\.0\.0\.1:\d+/(?:old/group_old_k11\.html|new/group\.html)"), "<page-url>"),
    ("<port>（桩后端每次运行端口不同；仅用于两次运行间可比，同一次运行两侧端口相同）",
     re.compile(r"127\.0\.0\.1:\d+"), "127.0.0.1:<port>"),
    ("<ts13>（13 位毫秒时戳，防御性规则）", re.compile(r"\b\d{13}\b"), "<ts13>"),
]
NORM_HITS = {}


def norm(text):
    for name, rx, rep in NORM_RULES:
        text, n = rx.subn(rep, text)
        if n:
            NORM_HITS[name] = NORM_HITS.get(name, 0) + n
    return text


def sig(entry):
    """轨迹签名：只看「发什么」—— 方法 / URL / 正文 / credentials / 头。"""
    return norm("%s %s | body=%s | cred=%s | hdr=%s" % (
        entry.get("method"), entry.get("url"), entry.get("body"),
        entry.get("credentials"), entry.get("headers")))


def collapse(entries):
    """连续重复签名压缩成一条（吸收轮询次数差异，保住「发不发 / 发什么 / 顺序」）。"""
    out = []
    for e in entries:
        s = sig(e)
        if out and out[-1] == s:
            continue
        out.append(s)
    return out


def wire_text(rec):
    if rec["where"] == "stub":
        return norm("\n".join([rec["requestline"]] + rec["headers"] + ["", rec["body"]]))
    hdrs = ["%s: %s" % (k, rec["headers"][k]) for k in sorted(rec["headers"])]
    return norm("\n".join([rec["requestline"]] + hdrs + ["", rec["body"]]))


# ====================================================================== 浏览器

def find_browser():
    for p in EDGE_CANDIDATES:
        if os.path.isfile(p):
            return p
    raise RuntimeError("找不到 Edge / Chrome，改不了就别硬跑")


def launch(browser, port, profile):
    args = [browser, "--headless=new", "--remote-debugging-port=%d" % port,
            "--user-data-dir=" + profile, "--no-first-run", "--no-default-browser-check",
            "--disable-gpu", "--disable-extensions", "--disable-background-networking",
            "--disable-sync", "--disable-component-update", "--no-sandbox",
            "--window-size=1280,900", "about:blank"]
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 40
    while time.time() < deadline:
        try:
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            c.request("GET", "/json/list")
            data = json.loads(c.getresponse().read().decode("utf-8"))
            c.close()
            pages = [t for t in data if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
            if pages:
                return proc, pages[0]["webSocketDebuggerUrl"]
        except Exception:
            time.sleep(0.25)
    proc.kill()
    raise RuntimeError("浏览器 DevTools 端口 %d 起不来" % port)


# ====================================================================== 一侧的完整采集

CLIENT_IDENT_RX = re.compile(
    r"^(User-Agent|sec-ch-ua|sec-ch-ua-platform|sec-ch-ua-mobile|Accept-Language):", re.I)


def client_ident(recs):
    """从线级记录里抽出客户端身份头（去重排序）—— 用来自证 UA override 真的生效了。"""
    seen = set()
    for r in recs:
        hdrs = r["headers"] if isinstance(r["headers"], list) else \
            ["%s: %s" % (k, v) for k, v in r["headers"].items()]
        for h in hdrs:
            if CLIENT_IDENT_RX.match(h):
                seen.add(h)
    return sorted(seen)


def run_side(name, page_url, browser, stub_port, verbose):
    port = free_port()
    profile = tempfile.mkdtemp(prefix="k11_%s_" % name)      # 两侧各自独立 profile，localStorage 天然隔离
    w0 = wire_len()                                          # 本侧线级记录的起点（两侧串行，区间干净）
    proc, ws_url = launch(browser, port, profile)
    res = {"name": name, "url": page_url}
    try:
        cdp = CDP(ws_url)

        # --- 客户端身份钉死：必须排在 Page.navigate / Fetch.enable 之前，
        #     即这条 CDP 会话上发生任何网络活动之前（见 CLIENT_UA 处的注释）---
        cdp.call("Emulation.setUserAgentOverride",
                 userAgent=CLIENT_UA,
                 acceptLanguage=CLIENT_ACCEPT_LANGUAGE,
                 platform="Win32",
                 userAgentMetadata=CLIENT_UA_METADATA)

        # --- Fetch 域拦截：只挂 TSVC，其余请求原样放行走桩后端 ---
        def on_paused(p):
            rid = p["requestId"]
            req = p["request"]
            url = req["url"]
            path = re.sub(r"^https?://[^/]+", "", url) or "/"
            hdrs = {k: v for k, v in (req.get("headers") or {}).items()}
            post = req.get("postData") or ""
            wire_add({"where": "cdp", "requestline": "%s %s HTTP/1.1" % (req["method"], path),
                      "path": path, "method": req["method"], "headers": hdrs, "body": post})
            if req["method"] == "OPTIONS":
                st, body = 204, b""
                rh = [{"name": "Access-Control-Allow-Origin", "value": "*"},
                      {"name": "Access-Control-Allow-Methods", "value": "GET,POST,PATCH,DELETE,OPTIONS"},
                      {"name": "Access-Control-Allow-Headers", "value": "Content-Type"},
                      {"name": "Content-Length", "value": "0"}]
            else:
                st, ct, body = fixture(req["method"], path, post.encode("utf-8"))
                rh = [{"name": "Content-Type", "value": ct},
                      {"name": "Content-Length", "value": str(len(body))},
                      {"name": "Access-Control-Allow-Origin", "value": "*"},
                      {"name": "Cache-Control", "value": "no-store"}]
            cdp.send("Fetch.fulfillRequest", requestId=rid, responseCode=st,
                     responseHeaders=rh, body=base64.b64encode(body).decode())

        def on_dialog(p):
            # createGroup / refresh 的失败分支会 alert()，headless 下不处理会把页面卡住
            cdp.send("Page.handleJavaScriptDialog", accept=True)

        cdp.on_event["Fetch.requestPaused"] = on_paused
        cdp.on_event["Page.javascriptDialogOpening"] = on_dialog

        cdp.call("Page.enable")
        cdp.call("Runtime.enable")
        cdp.call("Fetch.enable", patterns=[{"urlPattern": "*" + TSVC_HOST + "*"}])
        cdp.call("Page.addScriptToEvaluateOnNewDocument", source=PROBE_JS)

        # ---------- A 启动 ----------
        cdp.call("Page.navigate", url=page_url)
        cdp.pump(SETTLE_BOOT)
        res["boot_trace"] = cdp.evaluate("JSON.stringify(window.__tr)")
        res["idents"] = [json.loads(cdp.evaluate(
            "JSON.stringify((function(){try{return (%s);}catch(e){return '<<throw>> '+e.message;}})())" % e))
            for e in IDENT_PROBES]

        # ---------- A2 轮询窗口 ----------
        cdp.evaluate("window.__tr.length = 0; 1")
        cdp.pump(SETTLE_POLL)
        res["poll_trace"] = cdp.evaluate("JSON.stringify(window.__tr)")

        # ---------- B 函数级冻结输入 ----------
        cdp.evaluate(
            "window.__probe = async function (kind, method, path, body) {"
            "  var f = (kind === 'api') ? api : tApi;"
            "  try { var v = (body === null) ? await f(method, path) : await f(method, path, body);"
            "        return {ok: true, value: JSON.stringify(v)}; }"
            "  catch (e) { return {ok: false, message: String(e && e.message)}; } }; 1")
        frozen = []
        for kind, method, path, body in FROZEN:
            a = wire_len()
            # Runtime.evaluate 不是模块，顶层 await 是语法错 → 交给 awaitPromise 去 await
            out = json.loads(cdp.evaluate(
                "window.__probe(%s, %s, %s, %s).then(function (r) { return JSON.stringify(r); })"
                % (json.dumps(kind), json.dumps(method), json.dumps(path),
                   "null" if body is None else json.dumps(body))))
            cdp.pump(0.25)                      # 让 401 分支的 /login 导航请求落到桩上
            b = wire_len()
            # 本探针窗口里，路径正好等于被测路径的那些线级记录（轮询恰好插进来也不会被误收）
            recs = [r for r in wire_slice(a, b)
                    if r["path"].split("?", 1)[0] in (path, "/login")]
            frozen.append({"call": "%s(%r, %r%s)" % (kind, method, path,
                                                     "" if body is None else ", " + json.dumps(body)),
                           "outcome": out,
                           "wire": [wire_text(r) for r in recs]})
        res["frozen"] = frozen

        # ---------- C 动作序 ----------
        cdp.evaluate("window.__tr.length = 0; window.__errs.length = 0; 1")
        cdp.evaluate(KILL_TIMER)
        steps = []
        for label, js in ACTIONS:
            cdp.evaluate(KILL_TIMER)
            r = cdp.evaluate("(function(){ try { return (function(){ %s })(); }"
                             " catch (e) { return '<<throw>> ' + e.message; } })()" % js,
                             await_promise=False)
            cdp.pump(SETTLE_STEP)
            cdp.evaluate(KILL_TIMER)
            steps.append("%s -> %s" % (label, r))
        res["action_steps"] = steps
        res["action_trace"] = cdp.evaluate("JSON.stringify(window.__tr)")
        res["errs"] = json.loads(cdp.evaluate("JSON.stringify(window.__errs)"))
        res["client_ident"] = client_ident(wire_slice(w0, wire_len()))
        cdp.close()
    finally:
        proc.kill()
        proc.wait(timeout=20)
        shutil.rmtree(profile, ignore_errors=True)
    return res


# ====================================================================== 文本级

BLOCKS = [("B1", 555, 555), ("B2", 560, 586), ("B3", 1468, 1468),
          ("B4", 1469, 1469), ("B5", 1475, 1486)]


def text_level():
    """证明「零逻辑改动」：新件里的每一段 == 旧快照同段的那几行，逐字节。"""
    old = open(OLD_HTML, "rb").read().split(b"\n")
    newjs = open(NEW_JS, "rb").read()
    print("---- 3.1 文本级：5 段逐段字节相等 ----")
    off, ok = 0, True
    for nm, a, b in BLOCKS:
        seg = b"".join(old[i - 1] + b"\n" for i in range(a, b + 1))
        got = newjs[off:off + len(seg)]
        eq = got == seg
        ok &= eq
        print("  %s 旧快照行 %4d-%-4d %5d B → 新件偏移 %5d  字节相等: %s" % (nm, a, b, len(seg), off, eq))
        off += len(seg)
    tail = newjs[off:]
    print("  新件尾部残余 %d B（应为 0，证明新件 = 5 段纯拼接、无任何包装）: %s"
          % (len(tail), tail[:40]))
    ok &= len(tail) == 0
    cur = open(NEW_HTML, "rb").read()
    residue = [p for p in (b"fetch(", b"const API = ", b"const TSVC = ",
                           b"const TGROUP_DEFAULT = ", b"async function api(",
                           b"async function tApi(") if p in cur]
    print("  group.html 残留（应为空）: %s" % residue)
    ok &= not residue
    print("  文本级结论: %s" % ("一致" if ok else "不一致"))
    return ok


# ====================================================================== 比对

def diff_lines(tag, a, b, verbose):
    if a == b:
        print("  ✓ %-16s 两侧逐行一致（%d 条）" % (tag, len(a)))
        if verbose:
            for i, x in enumerate(a):
                print("        [%02d] %s" % (i, x))
        return 0
    n = 0
    print("  ✗ %-16s 不一致（旧 %d 条 / 新 %d 条）" % (tag, len(a), len(b)))
    for i in range(max(len(a), len(b))):
        x = a[i] if i < len(a) else "<缺>"
        y = b[i] if i < len(b) else "<缺>"
        if x != y:
            n += 1
            print("      [%02d] OLD: %s" % (i, x))
            print("      [%02d] NEW: %s" % (i, y))
    print("      差异行数: %d" % n)
    return n


def run_once(verbose):

    class args(object):                     # 保持下文 args.verbose 的写法不变
        pass
    args.verbose = verbose

    print("=" * 78)
    print("刀11 · 群聊前端传输客户端抽出 —— 等价装置")
    print("=" * 78)

    for p in (OLD_HTML, NEW_HTML, NEW_JS):
        if not os.path.isfile(p):
            print("缺件: " + p)
            return 1

    def md5(p):
        return hashlib.md5(open(p, "rb").read()).hexdigest()

    old_md5, new_md5, js_md5 = md5(OLD_HTML), md5(NEW_HTML), md5(NEW_JS)
    print("---- 装置自证：两侧来自两个不同的物理文件 ----")
    print("  OLD html  %s  %8d B  md5 %s" % (OLD_HTML, os.path.getsize(OLD_HTML), old_md5))
    print("  NEW html  %s  %8d B  md5 %s" % (NEW_HTML, os.path.getsize(NEW_HTML), new_md5))
    print("  NEW js    %s  %8d B  md5 %s" % (NEW_JS, os.path.getsize(NEW_JS), js_md5))
    self_ok = old_md5.startswith("c7ff8b4e") and new_md5 != old_md5
    print("  旧侧 md5 前缀是 c7ff8b4e: %s ｜ 新侧 md5 != 旧侧: %s ｜ 不是自比: %s"
          % (old_md5.startswith("c7ff8b4e"), new_md5 != old_md5, self_ok))
    if not self_ok:
        print("装置自证不过（旧侧不是开工前快照，或两侧同一个文件）→ 停")
        return 1

    txt_ok = text_level()

    browser = find_browser()
    stub_port = free_port()
    srv = QuietServer(("127.0.0.1", stub_port), Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % stub_port
    old_url = base + "/old/group_old_k11.html"
    new_url = base + "/new/group.html"
    print("---- 桩后端 ----")
    print("  浏览器      : %s" % browser)
    print("  桩监听      : %s" % base)
    print("  OLD 页面 URL: %s" % old_url)
    print("  NEW 页面 URL: %s" % new_url)
    print("  TSVC 拦截   : CDP Fetch 域 urlPattern=*%s* → Fetch.fulfillRequest 合成应答"
          % TSVC_HOST)
    print("  UA 钉死     : %s" % CLIENT_UA)
    print("  UA-CH 钉死  : brands=%s ｜ platform=%s ｜ mobile=%s ｜ 语言表=%s"
          % (json.dumps(CLIENT_UA_BRANDS, ensure_ascii=False, sort_keys=True),
             CLIENT_UA_METADATA["platform"], CLIENT_UA_METADATA["mobile"],
             CLIENT_ACCEPT_LANGUAGE))
    print("  （客户端构建号不是被测行为：两侧各 launch 一次浏览器，Edge 自动升级会让两侧")
    print("    抓到不同构建。钉死后 User-Agent / sec-ch-ua / sec-ch-ua-platform /")
    print("    sec-ch-ua-mobile 由同一份常量同源生成；其余线级字段仍是逐字节强比较。）")

    try:
        print("---- 采集 OLD ----")
        o = run_side("old", old_url, browser, stub_port, args.verbose)
        print("---- 采集 NEW ----")
        n = run_side("new", new_url, browser, stub_port, args.verbose)
    finally:
        srv.shutdown()

    print("---- 动作序执行回执（两侧应完全一致）----")
    for i, (a, b) in enumerate(zip(o["action_steps"], n["action_steps"])):
        print("  [%d] OLD %-40s ｜ NEW %s" % (i, a, b))

    print("---- 逐项比对 ----")
    bad = 0
    ob, nb = collapse(json.loads(o["boot_trace"])), collapse(json.loads(n["boot_trace"]))
    op, np_ = collapse(json.loads(o["poll_trace"])), collapse(json.loads(n["poll_trace"]))
    oa, na = collapse(json.loads(o["action_trace"])), collapse(json.loads(n["action_trace"]))
    oi = ["%s = %s" % (k, json.dumps(v, ensure_ascii=False)) for k, v in zip(IDENT_PROBES, o["idents"])]
    ni = ["%s = %s" % (k, json.dumps(v, ensure_ascii=False)) for k, v in zip(IDENT_PROBES, n["idents"])]

    bad += diff_lines("标识符探针", oi, ni, True)
    bad += diff_lines("启动轨迹", ob, nb, args.verbose)
    bad += diff_lines("轮询轨迹", op, np_, args.verbose)
    bad += diff_lines("动作序轨迹", oa, na, args.verbose)

    print("  —— 函数级冻结输入（请求行 + 请求头 + 请求体 + Error.message 逐字节）——")
    fz = 0
    for a, b in zip(o["frozen"], n["frozen"]):
        la = [a["call"], json.dumps(a["outcome"], ensure_ascii=False, sort_keys=True)] + a["wire"]
        lb = [b["call"], json.dumps(b["outcome"], ensure_ascii=False, sort_keys=True)] + b["wire"]
        if la == lb:
            print("    ✓ %-46s 线级 %d 条逐字节一致 ｜ 结果 %s"
                  % (a["call"], len(a["wire"]),
                     json.dumps(a["outcome"], ensure_ascii=False)[:100]))
        else:
            fz += 1
            print("    ✗ %s" % a["call"])
            for i in range(max(len(la), len(lb))):
                x = la[i] if i < len(la) else "<缺>"
                y = lb[i] if i < len(lb) else "<缺>"
                if x != y:
                    print("        OLD| " + x.replace("\n", "\n        OLD| "))
                    print("        NEW| " + y.replace("\n", "\n        NEW| "))
    bad += fz
    print("  函数级不一致例数: %d / %d" % (fz, len(FROZEN)))

    eo = diff_lines("JS 错误列表", o["errs"], n["errs"], True)
    bad += eo
    print("  两侧错误列表条数: OLD %d / NEW %d" % (len(o["errs"]), len(n["errs"])))

    print("  —— 客户端身份头自证（钉死是否真的生效）——")
    ua_line = "User-Agent: " + CLIENT_UA
    for side, r in (("OLD", o), ("NEW", n)):
        for h in r["client_ident"]:
            print("    %s| %s" % (side, h))
        hit = ua_line in r["client_ident"]
        ua_only = [h for h in r["client_ident"] if h.lower().startswith("user-agent:")]
        print("    %s 侧线上只出现钉死的那一条 User-Agent: %s（实际出现 %d 种）"
              % (side, hit and len(ua_only) == 1, len(ua_only)))
        if not (hit and len(ua_only) == 1):
            bad += 1
    print("    两侧客户端身份头集合相同: %s" % (o["client_ident"] == n["client_ident"]))

    print("---- 装置自证汇总 ----")
    print("  轨迹条数（压缩后）: 启动 OLD %d / NEW %d ｜ 轮询 OLD %d / NEW %d ｜ 动作序 OLD %d / NEW %d"
          % (len(ob), len(nb), len(op), len(np_), len(oa), len(na)))
    print("  轨迹条数（压缩前）: 启动 OLD %d / NEW %d ｜ 轮询 OLD %d / NEW %d ｜ 动作序 OLD %d / NEW %d"
          % (len(json.loads(o["boot_trace"])), len(json.loads(n["boot_trace"])),
             len(json.loads(o["poll_trace"])), len(json.loads(n["poll_trace"])),
             len(json.loads(o["action_trace"])), len(json.loads(n["action_trace"]))))
    print("  归一化实际命中的字段（没命中的规则不列）:")
    if not NORM_HITS:
        print("    （无 —— 一条归一化规则都没触发）")
    for k, v in sorted(NORM_HITS.items()):
        print("    · %s  命中 %d 次" % (k, v))
    print("  文本级一致: %s ｜ 逐条 diff 行数合计: %d" % (txt_ok, bad))

    if txt_ok and bad == 0:
        print("EQUIV_SENTINEL_OK")
        return 0
    print("EQUIV_SENTINEL_FAIL")
    return 1


# ====================================================================== 确定性自证（--runs N）

PORT_RX = re.compile(r"127\.0\.0\.1:\d+")


def repeat(n, verbose):
    """连跑 N 次子进程；每次都必须 OK，且**把随机端口归一后 N 份输出逐字节相同**。"""
    print("=" * 78)
    print("确定性自证：同一份树连跑 %d 次（每次一个干净子进程）" % n)
    print("=" * 78)
    outs, paths, codes = [], [], []
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    cmd = [sys.executable, os.path.abspath(__file__)] + (["-v"] if verbose else [])
    for i in range(1, n + 1):
        t0 = time.time()
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
        txt = p.stdout.decode("utf-8", "replace").replace("\r\n", "\n")
        path = os.path.join(tempfile.gettempdir(), "k11_run%d.txt" % i)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(txt)
        outs.append(txt)
        paths.append(path)
        codes.append(p.returncode)
        print("  第 %d/%d 次 ｜ 退出码 %d ｜ %s ｜ %5.1fs ｜ %s"
              % (i, n, p.returncode,
                 "EQUIV_SENTINEL_OK" if "EQUIV_SENTINEL_OK" in txt else "EQUIV_SENTINEL_FAIL",
                 time.time() - t0, path))
        sys.stdout.flush()

    all_ok = all(c == 0 for c in codes) and all("EQUIV_SENTINEL_OK" in t for t in outs)

    # 随机端口是装置自己的外部条件（free_port），不是被测行为 → 归一后再逐字节比
    normed = [PORT_RX.sub("127.0.0.1:<PORT>", t) for t in outs]
    digests = [hashlib.md5(t.encode("utf-8")).hexdigest() for t in normed]
    same = len(set(digests)) == 1
    print("---- 归一端口后的逐次输出摘要 ----")
    for i, d in enumerate(digests, 1):
        print("  第 %d 次 md5(归一后输出) = %s  长度 %d B" % (i, d, len(normed[i - 1])))
    print("  %d 份输出归一端口后逐字节相同: %s" % (n, same))
    if not same:
        base = normed[0].split("\n")
        for i in range(1, n):
            cur = normed[i].split("\n")
            for k in range(max(len(base), len(cur))):
                x = base[k] if k < len(base) else "<缺>"
                y = cur[k] if k < len(cur) else "<缺>"
                if x != y:
                    print("    第1次 vs 第%d次 首个差异行 %d:" % (i + 1, k))
                    print("      #1 | " + x)
                    print("      #%d | %s" % (i + 1, y))
                    break
    print("  每次都 EQUIV_SENTINEL_OK 且退出码 0: %s" % all_ok)
    if all_ok and same:
        print("EQUIV_DETERMINISM_OK")
        return 0
    print("EQUIV_DETERMINISM_FAIL")
    return 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-v", "--verbose", action="store_true", help="打印全量轨迹")
    ap.add_argument("--runs", type=int, default=1,
                    help="连跑 N 次自证确定性（默认 1；>1 时每次起一个干净子进程，"
                         "要求次次 OK 且归一随机端口后 N 份输出逐字节相同）")
    args = ap.parse_args()
    if args.runs > 1:
        return repeat(args.runs, args.verbose)
    return run_once(args.verbose)


# ---------------------------------------------------------------- 已知偏差
# 1) /login 返 204 而不是 200：200 会让浏览器真的导航走、把页面连同 window.__tr 一起
#    卸掉，后面的采集就没了。204 让浏览器发出那次 GET 但不换文档 —— 401 分支
#    「发了什么、抛了什么」照样被完整记录。两侧同一份夹具，不影响可比性。
# 2) 动作序前后掐 TIMER：轮询「发什么」已在 A2 轮询轨迹里单独逐行比过；动作序要比的是
#    动作本身，掐掉定时器是为了不让调度抖动伪造出 diff。两侧同一套代码、同一时机。
# 3) 根路径 /group_transport.js 对两侧都发得出来，但旧页面压根不引用它 ——
#    它只会出现在新侧的文档子资源请求里，而子资源请求不进 window.__tr（探针只包 fetch）。

if __name__ == "__main__":
    sys.exit(main())
