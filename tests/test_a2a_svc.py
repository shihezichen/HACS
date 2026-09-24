#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# hacs-test-fixtures: 本文件为测试夹具，允许出现假凭据与网段边界值；危险类命中（真令牌/连接串/真实人名）仍按硬门禁拦截
"""
test_a2a_svc.py —— a2a_svc.py 的纯标准库自测（直接 python test_a2a_svc.py 即可跑）

做的事：
  1. 起一个假的 backend（stub），提供 /v1/chat/completions（秒回）和 /slow/v1/chat/completions（sleep 0.5s）
  2. 生成临时 agents.json / peers.json（随机高端口，绝不占 9920）
  3. 以子进程拉起 a2a_svc.py，逐项断言并打印 PASS/FAIL
  4. 无论成败都清理子进程、stub 和临时目录
"""

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
SVC = os.path.join(os.path.dirname(HERE), "a2a_svc.py")

TOKEN = "unit-test-token-abc123"
TOKEN_SHA = hashlib.sha256(TOKEN.encode("utf-8")).hexdigest()
STUB_REPLY = "你好，我是 stub backend 的固定回复。"

RESULTS = []
STUB_CALLS = []   # stub backend 实际收到的请求（含 payload 与关键头）


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", name, ("  -- " + detail) if detail else ""))


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


# ------------------------------------------------------------------ stub backend

class StubHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        # 记下后端实际收到的 payload/头，供「session_id → user 字段」用例断言
        try:
            STUB_CALLS.append({
                "path": self.path,
                "payload": json.loads(raw.decode("utf-8")),
                "channel": self.headers.get("x-openclaw-message-channel"),
                "ctype": self.headers.get("Content-Type"),
            })
        except Exception:
            STUB_CALLS.append({"path": self.path, "payload": {}, "raw": raw[:200]})
        if self.path == "/slow/v1/chat/completions":
            time.sleep(0.5)  # 故意拖慢，用于验证 504 超时分支
        elif self.path != "/v1/chat/completions":
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = json.dumps({
            "choices": [{"message": {"role": "assistant", "content": STUB_REPLY}}]
        }, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# ------------------------------------------------------------------ HTTP 客户端

def request(url, method="GET", token=None, payload=None, timeout=15):
    """返回 (status, body_dict_or_raw_text)。"""
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        status, raw = resp.getcode(), resp.read()
        resp.close()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    text = raw.decode("utf-8", "replace")
    try:
        return status, json.loads(text)
    except Exception:
        return status, text


def wait_ready(base, proc, seconds=20):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        try:
            status, body = request(base + "/healthz", timeout=2)
            if status == 200:
                return True
        except Exception:
            pass
        time.sleep(0.2)
    return False


# ------------------------------------------------------------------ 主流程

def main():
    stub_port = free_port()
    svc_port = free_port()
    tmp = tempfile.mkdtemp(prefix="a2a_test_")
    stub = None
    proc = None

    try:
        stub = ThreadingHTTPServer(("127.0.0.1", stub_port), StubHandler)
        stub.daemon_threads = True
        threading.Thread(target=stub.serve_forever, daemon=True).start()

        agents_path = os.path.join(tmp, "agents.json")
        peers_path = os.path.join(tmp, "peers.json")
        with open(agents_path, "w", encoding="utf-8") as f:
            json.dump({
                "node_id": "testnode",
                "host": "127.0.0.1",
                "port": svc_port,
                "agents": {
                    "tester": {
                        "backend": "http://127.0.0.1:%d/v1" % stub_port,
                        "model": "stub/default", "api_key": "",
                        "description": "测试用 agent", "skills": ["echo"],
                    },
                    "slowbot": {
                        "backend": "http://127.0.0.1:%d/slow/v1" % stub_port,
                        "model": "stub/slow", "api_key": "",
                        "description": "慢速 agent", "skills": ["echo"],
                    },
                },
            }, f, ensure_ascii=False)
        with open(peers_path, "w", encoding="utf-8") as f:
            json.dump({"hermes": TOKEN_SHA}, f)

        env = dict(os.environ)
        env.pop("A2A_ALLOW_ANON", None)  # 确保鉴权生效
        env["PYTHONIOENCODING"] = "utf-8"
        proc = subprocess.Popen(
            [sys.executable, SVC, "--host", "127.0.0.1", "--port", str(svc_port),
             "--config", agents_path, "--peers", peers_path],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)

        base = "http://127.0.0.1:%d" % svc_port
        if not wait_ready(base, proc):
            print("[FATAL] a2a_svc 未能在 20s 内就绪")
            if proc.poll() is not None:
                print(proc.stdout.read().decode("utf-8", "replace"))
            return 1

        # 1) /healthz
        status, body = request(base + "/healthz")
        check("/healthz 返回 200 且 ok=true",
              status == 200 and isinstance(body, dict) and body.get("ok") is True
              and body.get("node") == "testnode" and isinstance(body.get("ts"), int),
              "status=%s body=%s" % (status, body))

        # 2) Agent Card
        status, card = request(base + "/a2a/tester/.well-known/agent-card.json")
        want_url = "http://127.0.0.1:%d/a2a/tester" % svc_port
        required = ["name", "description", "url", "version", "protocolVersion", "capabilities",
                    "defaultInputModes", "defaultOutputModes", "skills",
                    "securitySchemes", "security"]
        missing = [k for k in required if not isinstance(card, dict) or k not in card]
        check("Agent Card 200 且字段齐全、url 正确",
              status == 200 and not missing and card.get("url") == want_url
              and card.get("name") == "tester" and card.get("protocolVersion") == "0.2.5"
              and card.get("skills") == [{"id": "echo", "name": "echo",
                                          "description": "测试用 agent"}],
              "status=%s missing=%s url=%s" % (status, missing,
                                               card.get("url") if isinstance(card, dict) else card))

        # 3) 无 token → 401
        status, body = request(base + "/a2a/tester/messages", "POST", payload={"text": "hi"})
        check("POST messages 无 token → 401",
              status == 401 and body.get("error") == "unauthorized",
              "status=%s body=%s" % (status, body))

        # 4) 错误 token → 401
        status, body = request(base + "/a2a/tester/messages", "POST",
                               token="wrong-token", payload={"text": "hi"})
        check("POST messages 错误 token → 401",
              status == 401 and body.get("error") == "unauthorized",
              "status=%s body=%s" % (status, body))

        # 5) 正确 token → 200 且 reply 与 stub 一致
        status, body = request(base + "/a2a/tester/messages", "POST", token=TOKEN,
                               payload={"text": "任务内容", "from": "hermes",
                                        "session_id": "s-001", "timeout_s": 10})
        check("正确 token + stub backend → 200 且 reply 一致",
              status == 200 and body.get("ok") is True and body.get("reply") == STUB_REPLY
              and body.get("agent") == "tester" and body.get("session_id") == "s-001"
              and isinstance(body.get("elapsed_ms"), int),
              "status=%s body=%s" % (status, body))

        # 6) 未知 agent_id → 404
        status, body = request(base + "/a2a/nosuchagent/messages", "POST", token=TOKEN,
                               payload={"text": "hi"})
        check("未知 agent_id → 404 unknown_agent",
              status == 404 and body.get("error") == "unknown_agent",
              "status=%s body=%s" % (status, body))

        # 7) text 为空 → 400
        status, body = request(base + "/a2a/tester/messages", "POST", token=TOKEN,
                               payload={"text": "", "from": "hermes"})
        check("text 为空 → 400 bad_request",
              status == 400 and body.get("error") == "bad_request",
              "status=%s body=%s" % (status, body))

        # 8) backend sleep 0.5s + timeout_s=0.2 → 504
        status, body = request(base + "/a2a/slowbot/messages", "POST", token=TOKEN,
                               payload={"text": "慢一点", "timeout_s": 0.2})
        check("backend 超时 → 504 backend_timeout",
              status == 504 and body.get("error") == "backend_timeout",
              "status=%s body=%s" % (status, body))

        # 9) 附加：/agents 鉴权行为
        status, _ = request(base + "/agents")
        status2, body2 = request(base + "/agents", token=TOKEN)
        check("/agents 无 token→401，有 token→200 且列出本机 agent",
              status == 401 and status2 == 200
              and sorted((body2.get("agents") or {}).keys()) == ["slowbot", "tester"],
              "no_token=%s with_token=%s body=%s" % (status, status2, body2))

        # 10) 附加：未知路径 → 404
        status, body = request(base + "/whatever")
        check("未知路径 → 404", status == 404, "status=%s body=%s" % (status, body))

        # 11) 多轮上下文：带 session_id → 后端收到 OpenAI user 字段（a2a:<调用方>:<会话id>）+ 渠道头
        STUB_CALLS[:] = []
        status, body = request(base + "/a2a/tester/messages", "POST", token=TOKEN,
                               payload={"text": "第二轮，还记得上一句吗", "session_id": "s-777",
                                        "timeout_s": 10})
        last = STUB_CALLS[-1] if STUB_CALLS else {}
        lp = last.get("payload") or {}
        check("带 session_id → 后端 payload.user = a2a:hermes:s-777，且带 x-openclaw-message-channel: a2a",
              status == 200 and lp.get("user") == "a2a:hermes:s-777"
              and last.get("channel") == "a2a"
              and "charset=utf-8" in (last.get("ctype") or ""),
              "status=%s user=%s channel=%s ctype=%s" % (status, lp.get("user"),
                                                         last.get("channel"), last.get("ctype")))

        # 12) 不带 session_id → 后端 payload 里**没有** user 键（每次新会话，答完即弃）
        STUB_CALLS[:] = []
        status, body = request(base + "/a2a/tester/messages", "POST", token=TOKEN,
                               payload={"text": "一次性提问", "timeout_s": 10})
        lp2 = (STUB_CALLS[-1].get("payload") if STUB_CALLS else {}) or {}
        check("不带 session_id → 后端 payload 无 user 键（物理隔离，不与飞书桥共用会话）",
              status == 200 and "user" not in lp2,
              "status=%s payload_keys=%s" % (status, sorted(lp2.keys())))

        # 13) 超长 session_id 被截断到 128（不让调用方把长串塞进 user 字段）
        STUB_CALLS[:] = []
        status, body = request(base + "/a2a/tester/messages", "POST", token=TOKEN,
                               payload={"text": "长会话 id", "session_id": "x" * 400, "timeout_s": 10})
        lp3 = (STUB_CALLS[-1].get("payload") if STUB_CALLS else {}) or {}
        u3 = lp3.get("user") or ""
        check("超长 session_id 被截断（user 字段长度 = 前缀 + 128）",
              status == 200 and u3 == "a2a:hermes:" + "x" * 128,
              "status=%s len=%d" % (status, len(u3)))

    finally:
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        if stub is not None:
            stub.shutdown()
            stub.server_close()
        shutil.rmtree(tmp, ignore_errors=True)

    failed = [name for name, ok, _ in RESULTS if not ok]
    print("-" * 60)
    print("合计 %d 项，通过 %d 项，失败 %d 项" % (len(RESULTS), len(RESULTS) - len(failed), len(failed)))
    if failed:
        print("失败项：" + ", ".join(failed))
        return 1
    print("全部 PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
