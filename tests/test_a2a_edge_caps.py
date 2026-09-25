#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# hacs-test-fixtures: 本文件为测试夹具，允许出现假凭据与网段边界值；危险类命中（真令牌/连接串/真实人名）仍按硬门禁拦截
"""
test_a2a_edge_caps.py —— L1 `a2a_edge` 的能力端口自测（纯标准库，直接 python 跑）

盯三件事：
  ① **未注入时的降级口径**：不得编造数字 —— 水位 → None（= 与「读不到」同形，
     端点侧表现为 no_usage）、后端判定 → "other"、会话头 → {}、水位比 → None。
  ② **注入后**：/ctx-usage 走真实链路，used/window/pct/text 与注入实现给的数一致。
  ③ **残留守卫**（分层不许回退）：a2a_edge.py 源文本里不得出现 agent_usage 字样；
     a2a_svc.py（组合根）源文本里必须出现 import agent_usage。

用例 ① 必须在 ② 之前跑：② 会把能力装进进程级的 _CAPS，装完就回不去「未注入」了。
"""

import hashlib
import io
import json
import os
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
AG = os.path.dirname(HERE)
sys.path.insert(0, AG)

EDGE_SRC = os.path.join(AG, "a2a_edge.py")
ROOT_SRC = os.path.join(AG, "a2a_svc.py")

TOKEN = "caps-test-token-abc123"
TOKEN_SHA = hashlib.sha256(TOKEN.encode("utf-8")).hexdigest()

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", name, ("  -- " + detail) if detail else ""))


def free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def get_json(url, token=None):
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        resp = urllib.request.urlopen(req, timeout=5)
        status, raw = resp.getcode(), resp.read()
        resp.close()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    text = raw.decode("utf-8", "replace")
    try:
        return status, json.loads(text)
    except Exception:
        return status, text


# ---------------------------------------------------------------- ① 未注入
def case_not_installed(a2a_edge):
    a2a_edge.install_capabilities({})     # 明示「什么都没注入」
    usage = a2a_edge._read_ctx_usage("tester", "s-1", {"backend": "http://x/v1"})
    kind = a2a_edge._backend_kind({"backend": "http://x/v1", "model": "hermes/claude"})
    headers = a2a_edge._hermes_headers({"backend": "http://x/v1"}, "tester", "s-1")
    press = a2a_edge._ctx_pressure("tester", "s-1", {"backend": "http://x/v1"})
    routes = a2a_edge._extra_routes()
    check("未注入：read_usage 缺失 → None（与「读不到」同形 = no_usage，不编造数字）",
          usage is None, "usage=%r" % (usage,))
    check("未注入：backend_of 缺失 → \"other\"", kind == "other", "kind=%r" % kind)
    check("未注入：session_headers 缺失 → {}", headers == {}, "headers=%r" % (headers,))
    check("未注入：pressure 缺失 → None（降级为不压缩、照常续跑）",
          press is None, "pressure=%r" % (press,))
    check("未注入：get_routes 缺失 → 无额外路由", routes == [], "routes=%r" % (routes,))


# ---------------------------------------------------------------- ② 注入后
def case_installed(a2a_edge):
    import agent_usage
    import a2a_svc

    used_want, window_want = 123_456, 200_000
    calls = []

    def stub_read_usage(caller, session_id, agent=None):
        calls.append((caller, session_id))
        return (used_want, window_want)

    agent_usage.read_usage = stub_read_usage     # 注入实现的「真值」由本桩给定
    a2a_svc.install()

    a2a_edge.Runtime.node_id = "capsnode"
    a2a_edge.Runtime.agents = {"tester": {"description": "注入测试", "skills": [],
                                          "backend": "http://127.0.0.1:1/v1"}}
    a2a_edge.Runtime.peer_map = {TOKEN_SHA: "hermes"}
    a2a_edge.Runtime.allow_anon = False

    port = free_port()
    srv = ThreadingHTTPServer(("127.0.0.1", port), a2a_edge.A2AHandler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % port
    try:
        status, body = get_json(base + "/ctx-usage/tester?session_id=s-77", TOKEN)
        pct_want = int(round(100.0 * used_want / window_want))
        text_want = "上下文：约 %dk/%dk (%d%%)" % (used_want // 1000, window_want // 1000, pct_want)
        check("注入后：/ctx-usage → 200 ok:true，used/window/pct/text 与注入实现一致",
              status == 200 and isinstance(body, dict) and body.get("ok") is True
              and body.get("agent") == "tester" and body.get("session_id") == "s-77"
              and body.get("used") == used_want and body.get("window") == window_want
              and body.get("pct") == pct_want and body.get("text") == text_want,
              "status=%s body=%s" % (status, body))
        check("注入后：请求确实打到了注入的实现（read_usage 被调用，参数是 agent_id/session_id）",
              calls == [("tester", "s-77")], "calls=%r" % (calls,))
        status2, body2 = get_json(base + "/ctx-usage/tester?session_id=s-77")
        check("注入后：无 token → 401 unauthorized（鉴权仍在 L1 侧）",
              status2 == 401 and isinstance(body2, dict)
              and body2.get("error") == "unauthorized",
              "status=%s body=%s" % (status2, body2))
    finally:
        srv.shutdown()
        srv.server_close()


# ---------------------------------------------------------------- ③ 残留守卫
def case_residue():
    edge = io.open(EDGE_SRC, encoding="utf-8").read()
    root = io.open(ROOT_SRC, encoding="utf-8").read()
    check("残留守卫：a2a_edge.py（L1）源文本内不出现 agent_usage（含注释）",
          "agent_usage" not in edge,
          "命中行：%s" % [l.strip() for l in edge.splitlines() if "agent_usage" in l][:3])
    check("残留守卫：a2a_svc.py（组合根 C）源文本内含 import agent_usage",
          "import agent_usage" in root)


def main():
    import a2a_edge
    case_not_installed(a2a_edge)
    case_installed(a2a_edge)
    case_residue()

    failed = [n for n, ok, _ in RESULTS if not ok]
    print("-" * 60)
    print("合计 %d 项，通过 %d 项，失败 %d 项" % (len(RESULTS), len(RESULTS) - len(failed), len(failed)))
    if failed:
        print("失败项：" + ", ".join(failed))
        return 1
    print("全部 PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
