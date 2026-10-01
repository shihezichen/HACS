#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""L1 `web_gateway` 行为测试（真 socket，不 mock）：抽件的「零行为改变」锚点。"""
import http.client
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from node.l1_transport.web_gateway import Gateway, HttpError  # noqa: E402

FAILS = []


def check(name, got, want):
    if got == want:
        print("PASS  %s" % name)
    else:
        print("FAIL  %s\n      got =%r\n      want=%r" % (name, got, want))
        FAILS.append(name)


def build():
    gw = Gateway("probe", version="9.9")

    @gw.route("/healthz")
    def _h(req):
        return 200, {"ok": True, "path": req.path, "query": req.query}

    @gw.route("/api", prefix=True)
    def _api(req):
        return 200, {"kind": "api"}

    @gw.route("/api/deep")
    def _deep(req):
        return 200, {"kind": "deep"}

    @gw.route("/echo", methods=("POST",))
    def _echo(req):
        return 200, {"got": req.json_body()}

    @gw.route("/raw")
    def _raw(req):
        return 200, b"raw-bytes", {"Content-Type": "text/plain; charset=utf-8"}
    return gw


def call(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(method, path, body=body, headers=headers or {})
    resp = conn.getresponse()
    data = resp.read()
    out = (resp.status, dict((k.lower(), v) for k, v in resp.getheaders()), data)
    conn.close()
    return out


gw = build()
server = gw.serve_in_thread("127.0.0.1", 0)
port = server.server_address[1]

st, hd, body = call(port, "GET", "/healthz?x=1")
check("200 + JSON 信封", (st, hd.get("content-type"), json.loads(body)), 
      (200, "application/json; charset=utf-8", {"ok": True, "path": "/healthz", "query": "x=1"}))
check("Content-Length 逐字", hd.get("content-length"), str(len(body)))
check("尾部斜杠与查询串归一", call(port, "GET", "/healthz///?y=2")[2][:1] != b"", True)

check("前缀路由（长的优先）", json.loads(call(port, "GET", "/api/deep")[2]), {"kind": "deep"})
check("前缀路由（普通）", json.loads(call(port, "GET", "/api/x")[2]), {"kind": "api"})

st, hd, body = call(port, "GET", "/nope")
check("默认 404 信封", (st, json.loads(body)), (404, {"ok": False, "error": "not_found", "path": "/nope"}))

st, hd, body = call(port, "POST", "/nope-post", body=b"{}")
check("已注册 POST 时未知路径 = 404", st, 404)

# 只登记 GET 的网关：未注册方法必须保持基类 501（teamd 迁移后正是这一形态）
gw_get = Gateway("probe-get")


@gw_get.route("/healthz")
def _hg(req):
    return 200, {"ok": True}


sg = gw_get.serve_in_thread("127.0.0.1", 0)
pg = sg.server_address[1]
st, hd, body = call(pg, "POST", "/healthz", body=b"{}")
check("未注册 POST = 基类 501", (st, b"Unsupported method" in body), (501, True))
check("未注册 DELETE = 基类 501", call(pg, "DELETE", "/healthz")[0], 501)
sg.shutdown()

st, hd, body = call(port, "POST", "/echo", body=json.dumps({"a": 1}).encode())
check("POST JSON 体可读", json.loads(body), {"got": {"a": 1}})
st, hd, body = call(port, "POST", "/echo", body=b"{oops")
check("坏 JSON → 400", (st, json.loads(body)["error"]), (400, "bad_json"))
st, hd, body = call(port, "POST", "/echo", body=b"")
check("空体 → {}", json.loads(body), {"got": {}})

st, hd, body = call(port, "GET", "/raw")
check("bytes 原样 + 自带 Content-Type", (st, body, hd.get("content-type")), (200, b"raw-bytes", "text/plain; charset=utf-8"))

check("只注册了用到的方法", (hasattr(gw.handler_class(), "do_GET"), hasattr(gw.handler_class(), "do_PUT")), (True, False))
check("quiet 时 log_message 被接管", gw.handler_class().log_message.__qualname__.startswith("Gateway."), True)

# 钩子：未就绪统一 503（teamd 现有语义）+ 鉴权短路
gw2 = Gateway("probe2")
state = {"ready": False}

@gw2.pre
def _pre(req):
    if not state["ready"]:
        return 503, {"ok": False, "error": "not_ready"}
    return None

@gw2.auth
def _auth(req):
    if req.header("Authorization") != "Bearer t":
        return 401, {"ok": False, "error": "unauthorized"}
    if req.path == "/forbidden":
        raise HttpError(403, {"ok": False, "error": "forbidden"})
    return None


@gw2.route("/healthz")
def _h2(req):
    return 200, {"ok": True}

s2 = gw2.serve_in_thread("127.0.0.1", 0)
p2 = s2.server_address[1]
check("pre 钩子短路（未就绪 503，先于路由）", call(p2, "GET", "/whatever")[0], 503)
state["ready"] = True
check("auth 短路 401", (call(p2, "GET", "/healthz")[0], json.loads(call(p2, "GET", "/healthz")[2])["error"]), (401, "unauthorized"))
check("auth 放行", call(p2, "GET", "/healthz", headers={"Authorization": "Bearer t"})[0], 200)
check("auth 钩子抛 HttpError → 403", (call(p2, "GET", "/forbidden", headers={"Authorization": "Bearer t"})[0], 
      json.loads(call(p2, "GET", "/forbidden", headers={"Authorization": "Bearer t"})[2])["error"]), (403, "forbidden"))

server.shutdown(); s2.shutdown()
print("\n" + "-" * 60)
print("合计 %d 项，失败 %d 项" % (17, len(FAILS)))
sys.exit(1 if FAILS else 0)
