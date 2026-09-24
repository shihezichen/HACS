#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""serve.py —— 对话台前端本地预览服务（仅开发用）

用途：在把 ui/ 交给 chat 服务同源托管前，本地起一个静态服务，
     并把 /api/*、/healthz 反向代理到 chat 服务，方便直连调试。

用法（Windows PowerShell）：
  cd <repo>\\agent-team\\ui
  python serve.py                      # 默认 8080，代理到 <host>:9980
  python serve.py --port 8080 --backend http://<host>:9980 --token <view token>

注意：本文件是开发脚手架，不是交付物。交付产物 = index.html/api.js/app.js/styles.css
      由 chat 服务同源托管（契约 v1 §9）。
"""
from __future__ import annotations
import argparse
import http.server
import os
import socketserver
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))


class Handler(http.server.SimpleHTTPRequestHandler):
    backend = "http://127.0.0.1:9980"
    cookie = ""

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=HERE, **kw)

    def log_message(self, fmt, *args):
        sys.stderr.write("[ui] " + (fmt % args) + "\n")

    def _proxy(self, method: str) -> None:
        url = self.backend + self.path
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        req = urllib.request.Request(url, data=body, method=method)
        for h in ("Content-Type", "Accept", "Last-Event-ID"):
            v = self.headers.get(h)
            if v:
                req.add_header(h, v)
        if self.cookie:
            req.add_header("Cookie", self.cookie)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
                self.send_response(resp.status)
                for k, v in resp.headers.items():
                    if k.lower() in ("transfer-encoding", "connection", "content-length"):
                        continue
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if method != "HEAD":
                    self.wfile.write(data)
        except urllib.error.HTTPError as e:
            data = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type", e.headers.get("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:  # noqa: BLE001
            msg = ('{"ok":false,"error":{"code":"proxy_error","message":"%s"}}' % str(e).replace('"', "'")).encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)

    def do_GET(self):
        if self.path.startswith("/api/") or self.path.startswith("/healthz"):
            return self._proxy("GET")
        if self.path.startswith("/login") or self.path.startswith("/account"):
            return self._proxy("GET")
        return super().do_GET()

    def do_HEAD(self):
        if self.path.startswith("/api/") or self.path.startswith("/healthz"):
            return self._proxy("HEAD")
        return super().do_HEAD()

    def do_POST(self):
        if self.path.startswith("/api/"):
            return self._proxy("POST")
        self.send_error(404)

    def do_PATCH(self):
        if self.path.startswith("/api/"):
            return self._proxy("PATCH")
        self.send_error(404)

    def do_DELETE(self):
        if self.path.startswith("/api/"):
            return self._proxy("DELETE")
        self.send_error(404)


class ThreadedServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--backend", default="http://127.0.0.1:9980")
    ap.add_argument("--cookie", default="", help="直连调试用的 view_auth 会话 cookie")
    args = ap.parse_args()

    Handler.backend = args.backend.rstrip("/")
    Handler.cookie = args.cookie

    with ThreadedServer((args.bind, args.port), Handler) as httpd:
        print("[ui] 前端预览: http://%s:%d/" % (args.bind, args.port))
        print("[ui] /api/* -> %s" % Handler.backend)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n[ui] 停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
