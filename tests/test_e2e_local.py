#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# hacs-test-fixtures: 本文件为测试夹具，允许出现假凭据与网段边界值；危险类命中（真令牌/连接串/真实人名）仍按硬门禁拦截
"""端到端本地联调（不碰任何远程主机）：

  stub backend(9941)  <-  a2a_svc(19920, 用真实的 peers.json/tokens.json)  <-  a2a_call.py

覆盖：健康检查 / Agent Card / 带 token 派活拿到 reply / 无 token 401 / 错 token 401 /
      未知 agent 404 / /agents 列本机 agent / 身份回填 peer 字段（token 反查，不信 from）。
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SECRETS = r"<home>\AppData\Local\hermes\secrets\agent-team"
PY = sys.executable
SVC_PORT = 19920
STUB_PORT = 19941

STUB = r'''
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        body = json.dumps({"choices":[{"message":{"role":"assistant","content":"stub-reply-OK"}}]}).encode()
        self.send_response(200); self.send_header("Content-Type","application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
ThreadingHTTPServer(("127.0.0.1", %d), H).serve_forever()
''' % STUB_PORT

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print("[%s] %s%s" % ("PASS" if cond else "FAIL", name, ("  -- " + detail) if detail else ""))


def http(method, url, token=None, body=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return r.getcode(), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def main():
    tokens = json.load(open(os.path.join(SECRETS, "tokens.json"), encoding="utf-8"))
    tmp = tempfile.mkdtemp(prefix="a2a-e2e-")
    cfg = {"node_id": "thinkbook", "host": "127.0.0.1", "port": SVC_PORT,
           "agents": {"hermes": {"backend": "http://127.0.0.1:%d/v1" % STUB_PORT,
                                 "model": "stub", "description": "本地联调用",
                                 "skills": ["echo"]}}}
    cfg_path = os.path.join(tmp, "agents.json")
    json.dump(cfg, open(cfg_path, "w", encoding="utf-8"), ensure_ascii=False)
    tok_path = os.path.join(tmp, "client-token")
    open(tok_path, "w", encoding="utf-8").write(tokens["lobster"])

    procs = []
    try:
        stub_path = os.path.join(tmp, "stub.py")
        open(stub_path, "w", encoding="utf-8").write(STUB)
        procs.append(subprocess.Popen([PY, stub_path], stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL))
        svc = subprocess.Popen([PY, os.path.join(ROOT, "a2a_svc.py"), "--host", "127.0.0.1",
                                "--port", str(SVC_PORT), "--config", cfg_path,
                                "--peers", os.path.join(SECRETS, "peers.json")],
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        procs.append(svc)

        base = "http://127.0.0.1:%d" % SVC_PORT
        for _ in range(60):
            try:
                http("GET", base + "/healthz", timeout=2)
                break
            except Exception:
                time.sleep(0.25)

        call = [PY, os.path.join(ROOT, "a2a_call.py"), "--base", base]

        # 1 健康检查
        c, t = http("GET", base + "/healthz")
        d = json.loads(t)
        check("healthz 200 且列出本机 agent", c == 200 and d.get("agents") == ["hermes"], t[:120])

        # 2 Agent Card
        c, t = http("GET", base + "/a2a/hermes/.well-known/agent-card.json")
        d = json.loads(t)
        check("card 200 且 url 正确", c == 200 and d.get("url") == base + "/a2a/hermes", d.get("url", ""))

        # 3 无 token → 401
        c, t = http("POST", base + "/a2a/hermes/messages", body={"text": "ping"})
        check("无 token → 401", c == 401, t[:80])

        # 4 错 token → 401
        c, t = http("POST", base + "/a2a/hermes/messages", token="wrong-token", body={"text": "ping"})
        check("错 token → 401", c == 401, t[:80])

        # 5 正确 token + stub backend → 200 且 reply 一致
        c, t = http("POST", base + "/a2a/hermes/messages", token=tokens["lobster"],
                    body={"text": "ping", "session_id": "s-1"})
        d = json.loads(t)
        check("正确 token → 200 且 reply 来自 backend",
              c == 200 and d.get("reply") == "stub-reply-OK", t[:140])

        # 6 身份回填：token 属于 lobster，peer 必须是 lobster（不是请求体里的 from）
        c, t = http("POST", base + "/a2a/hermes/messages", token=tokens["lobster"],
                    body={"text": "ping", "from": "顶替者"})
        d = json.loads(t)
        check("身份由 token 反查（peer=lobster，不信 body.from）",
              d.get("peer") == "lobster", "peer=%r" % d.get("peer"))

        # 7 另一个 token → 身份随 token 变
        c, t = http("POST", base + "/a2a/hermes/messages", token=tokens["top001"], body={"text": "ping"})
        check("换 token 身份随之变化（peer=top001）", json.loads(t).get("peer") == "top001")

        # 8 未知 agent → 404
        c, t = http("POST", base + "/a2a/nobody/messages", token=tokens["lobster"], body={"text": "ping"})
        check("未知 agent → 404", c == 404, t[:80])

        # 9 空 text → 400
        c, t = http("POST", base + "/a2a/hermes/messages", token=tokens["lobster"], body={"text": "  "})
        check("空 text → 400", c == 400, t[:80])

        # 10 /agents 需 token
        c1, _ = http("GET", base + "/agents")
        c2, t2 = http("GET", base + "/agents", token=tokens["lobster"])
        check("/agents 无 token 401 / 有 token 200", c1 == 401 and c2 == 200, t2[:100])

        # 11 CLI 端到端：a2a_call.py 派活拿到纯文本 reply
        p = subprocess.run(call + ["call", "hermes", "--token-file", tok_path, "--text", "ping"],
                           capture_output=True, text=True, timeout=60)
        check("CLI 派活 → 纯文本 reply", p.returncode == 0 and p.stdout.strip() == "stub-reply-OK",
              repr(p.stdout.strip()[:80]))

        # 12 CLI 无 token → 退出码 2（本机就拦下，不发请求）
        p = subprocess.run(call + ["call", "hermes", "--text", "ping"],
                           capture_output=True, text=True, timeout=60)
        check("CLI 无 token 直接报错退出码 2", p.returncode == 2, repr(p.stderr.strip()[:60]))

        # 13 CLI 服务端 401 → 退出码 1
        bad = os.path.join(tmp, "bad-token")
        open(bad, "w").write("wrong")
        p = subprocess.run(call + ["call", "hermes", "--token-file", bad, "--text", "ping"],
                           capture_output=True, text=True, timeout=60)
        check("CLI 错 token → 退出码 1", p.returncode == 1, "rc=%d" % p.returncode)

        # 14 日志里有身份
        svc.terminate()
        try:
            out = svc.communicate(timeout=10)[0].decode("utf-8", "replace")
        except Exception:
            out = ""
        check("服务日志记录了 from=<成员>", "from=lobster" in out, out.strip().splitlines()[-1][:110] if out.strip() else "无日志")

    finally:
        for p in procs:
            try:
                p.kill()
            except Exception:
                pass

    ok = sum(1 for _, c, _ in results if c)
    print("-" * 60)
    print("合计 %d 项，通过 %d 项，失败 %d 项" % (len(results), ok, len(results) - ok))
    print("全部 PASS" if ok == len(results) else "存在 FAIL")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
