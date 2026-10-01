#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""L1 通信层：HTTP 入口件（**传输不含语义**）。

为什么要有它
------------
分层前 7~8 个件各自造 `BaseHTTPRequestHandler`：a2a_svc / teamd 管理口 /
blackboard_svc / blackboard_view / chat_svc / cli_backend / task_svc / hacs_api，
把「解析请求、鉴权、JSON 编解码、错误信封、路径归一化」各写了一遍。本件把这些
**与业务无关**的传输职责收成一处，各域件只登记「路径 → 处理函数」。

设计约束（handoff v2 判据）
--------------------------
1. **传输不含语义**：本件不出现群 / 会话 / 待办 / 会议等业务词；路径与响应全由调用方给。
2. **零行为改变**：默认回复的 `Content-Type` / `Content-Length` / 状态码 / 路径归一化
   与既有各件逐字一致；迁移只换「谁在写 socket」，不换响应形态。**未注册的 HTTP 方法
   保持基类的 501 行为**（不自行发明 405 信封）。
3. **鉴权可插拔**：各件口径不同（Bearer 令牌 / token 文件 / 会话 cookie），本件只提供
   「路由前调一次钩子」的位置与「短路回复」的契约，不替调用方决定口径。
4. **不持业务状态**：本件不读配置、不连库、不碰文件；一切外部依赖由调用方在闭包里带入。

用法
----
    gw = Gateway("teamd", version=VERSION, quiet=True)

    @gw.route("/healthz")
    def _healthz(req):
        if daemon is None:
            return 503, {"ok": False, "error": "not_ready"}
        return 200, daemon.healthz()

    server = gw.serve("127.0.0.1", 9960)

处理函数返回 `(status, obj)` / `(status, obj, headers)`；`obj` 为 `bytes` 时按原样发出
（调用方自带 `Content-Type`），否则按 `json.dumps(..., ensure_ascii=False)` 编码。
抛出 `HttpError` 即短路为该回复。
"""
import json
import threading

try:                                      # Python 3
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
except ImportError:                       # pragma: no cover - Python 2 兜底
    from BaseHTTPServer import BaseHTTPRequestHandler       # type: ignore
    from SocketServer import ThreadingTCPServer as ThreadingHTTPServer  # type: ignore


class HttpError(Exception):
    """短路回复：在新起的分支里直接返回一个响应。"""

    def __init__(self, status, obj, headers=None):
        # type: (int, object, dict) -> None
        Exception.__init__(self, "%s" % status)
        self.status = status
        self.obj = obj
        self.headers = headers or {}


class Request(object):
    """一次请求的只读视图；`body` 惰性读取，避免 GET 也去读 socket。"""

    def __init__(self, handler):
        # type: (BaseHTTPRequestHandler) -> None
        raw = handler.path or "/"
        path, _, query = raw.partition("?")
        self.method = handler.command or "GET"
        self.raw_path = raw
        self.path = path.rstrip("/") or "/"
        self.query = query
        self.headers = handler.headers
        self.client = handler.client_address
        self._handler = handler
        self._body = None

    def header(self, name, default=None):
        return self.headers.get(name, default)

    @property
    def body(self):
        # type: () -> bytes
        if self._body is None:
            length = self.header("Content-Length")
            try:
                n = int(length) if length else 0
            except (TypeError, ValueError):
                n = 0
            self._body = self._handler.rfile.read(n) if n > 0 else b""
        return self._body

    def json_body(self, max_bytes=4 * 1024 * 1024):
        """读 JSON 请求体；超限或非法一律 400（与既有各件口径一致）。"""
        raw = self.body
        if len(raw) > max_bytes:
            raise HttpError(413, {"ok": False, "error": "payload_too_large"})
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise HttpError(400, {"ok": False, "error": "bad_json"})


def envelope(status, obj, headers=None):
    """把处理函数的返回值化成 (status, body_bytes, headers)。

    本件为 L1 契约面，跨件只许用 `envelope()`（下划线名是旧调用点的兼容别名）。
    公共面契约（承诺什么 / 谁在用 / 变动的兼容规则）见 `docs/l1-contract.md`。
    """
    headers = dict(headers or {})
    if isinstance(obj, bytes):
        return status, obj, headers
    if obj is None:
        return status, b"", headers
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    headers.setdefault("Content-Type", "application/json; charset=utf-8")
    return status, body, headers


_envelope = envelope  # 兼容别名（刀7c 前的外部调用点）；新调用方一律用 envelope()


class Gateway(object):
    """一个端口的 HTTP 入口：路由表 + 前置钩子 + 回复信封。"""

    def __init__(self, name, version=None, quiet=True):
        # type: (str, str, bool) -> None
        self.name = name
        self.version = version
        self.quiet = quiet
        self._exact = {}        # (method, path) -> fn
        self._prefix = []       # [(method, prefix, fn)]
        self._pre = None        # fn(req) -> None | HttpError | (status, obj[, headers])
        self._auth = None       # 同上；语义上只做「放行 / 短路」
        self._not_found = None  # fn(req) -> (status, obj[, headers])
        self._server_version = "%s/%s" % (name, version) if version else name

    # ---------------------------------------------------------------- 登记
    def route(self, path, fn=None, methods=("GET",), prefix=False):
        """登记一条路由；可当装饰器用。`prefix=True` 时按前缀匹配（更长者优先）。"""
        def _register(handler):
            # 登记路径与分发路径用**同一套归一化**：否则 "/api/" 这种带尾斜杠的登记
            # 永远匹配不上（分发侧已 rstrip），会变成**静默死路由**——线上表现为 404，
            # 却看不出是登记写错了。宁可这里归一到 "/api"。
            key_path = path.rstrip("/") or "/"
            for method in methods:
                key = method.upper()
                if prefix:
                    self._prefix.append((key, key_path, handler))
                    self._prefix.sort(key=lambda item: len(item[1]), reverse=True)
                else:
                    self._exact[(key, key_path)] = handler
            return handler
        return _register(fn) if fn is not None else _register

    def pre(self, fn):
        """路由前钩子（如「未就绪统一 503」）。返回回复即短路。"""
        self._pre = fn
        return fn

    def auth(self, fn):
        """鉴权钩子：放行返回 None，否则返回回复或抛 HttpError。"""
        self._auth = fn
        return fn

    def not_found(self, fn):
        """自定义 404。不给时返回统一的 not_found 信封。"""
        self._not_found = fn
        return fn

    def _default_not_found(self, req):
        return 404, {"ok": False, "error": "not_found", "path": req.path}

    # ---------------------------------------------------------------- 分发
    def dispatch(self, req):
        # type: (Request) -> tuple
        for hook in (self._pre, self._auth):
            if hook is None:
                continue
            try:
                verdict = hook(req)
            except HttpError as exc:
                return envelope(exc.status, exc.obj, exc.headers)
            if verdict is not None:
                return envelope(*verdict)
        fn = self._exact.get((req.method, req.path))
        if fn is None:
            for method, prefix, handler in self._prefix:
                if method == req.method and (req.path == prefix or req.path.startswith(prefix + "/")):
                    fn = handler
                    break
        if fn is None:
            fn = self._not_found or self._default_not_found
        try:
            return envelope(*fn(req))
        except HttpError as exc:
            return envelope(exc.status, exc.obj, exc.headers)

    def handler_class(self):
        # type: () -> type
        gw = self
        quiet = self.quiet

        class GatewayHandler(BaseHTTPRequestHandler):
            server_version = gw._server_version
            protocol_version = "HTTP/1.0"

            def log_message(self, fmt, *args):      # noqa: D401
                if quiet:
                    return
                BaseHTTPRequestHandler.log_message(self, fmt, *args)

            def _reply(self, status, body, headers):
                try:
                    self.send_response(status)
                    if "Content-Length" not in headers:
                        headers["Content-Length"] = str(len(body))
                    for key, value in headers.items():
                        self.send_header(key, value)
                    self.end_headers()
                    if body:
                        self.wfile.write(body)
                except Exception:                   # 客户端早断，别把服务带崩
                    pass

            def _handle(self):
                req = Request(self)
                try:
                    status, body, headers = gw.dispatch(req)
                except HttpError as exc:            # 兜底：钩子/处理函数漏网
                    status, body, headers = envelope(exc.status, exc.obj, exc.headers)
                self._reply(status, body, headers)

            def do_GET(self):
                self._handle()

            def do_POST(self):
                self._handle()

            def do_PUT(self):
                self._handle()

            def do_DELETE(self):
                self._handle()

            def do_PATCH(self):
                self._handle()

            def do_HEAD(self):
                self._handle()

            def do_OPTIONS(self):
                self._handle()

        # 未登记的方法保持基类行为：基类只认 do_<METHOD>，没有就给 501
        # （这样「迁移前 501、迁移后 501」逐字一致，不自行发明 405 信封）。
        for method in ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"):
            if not any(key == method for key, _ in gw._exact) and \
               not any(key == method for key, _, _ in gw._prefix):
                delattr(GatewayHandler, "do_" + method)
        return GatewayHandler

    # ---------------------------------------------------------------- 起服务
    def serve(self, host, port, daemon_threads=True):
        # type: (str, int, bool) -> ThreadingHTTPServer
        server = ThreadingHTTPServer((host, port), self.handler_class())
        server.daemon_threads = daemon_threads
        return server

    def serve_in_thread(self, host, port):
        """起在后台线程里，返回 server（便于测试与同机双实例对照）。"""
        server = self.serve(host, port)
        thread = threading.Thread(target=server.serve_forever)
        thread.daemon = True
        thread.start()
        server._gateway_thread = thread  # type: ignore[attr-defined]
        return server
