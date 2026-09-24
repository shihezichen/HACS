#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# hacs-test-fixtures: 本文件为测试夹具，允许出现假凭据与网段边界值；危险类命中（真令牌/连接串/真实人名）仍按硬门禁拦截
"""
test_cli_backend.py —— cli_backend.py 的纯标准库自测（直接 python test_cli_backend.py 即可跑）

不依赖真实 pi / claude / codex：用 --pi-cmd / --claude-cmd / --codex-cmd 注入一个「假 CLI」
（临时 fake_cli.py），假 CLI 按任务文本里的标记决定行为（正常回声 / 睡 5 秒 / 退出码 1）。

做的事：
  1. 生成临时 key 文件 + 假 CLI，随机高端口拉起 cli_backend.py（绝不占 9920/9970 等既有端口）
  2. 逐项断言并打印 PASS/FAIL
  3. 无论成败都清理子进程与临时目录
"""

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

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SVC = os.path.join(ROOT, "cli_backend.py")

sys.path.insert(0, ROOT)
import cli_backend  # noqa: E402  纯函数级断言（resolve_model 等）直接 in-process 跑

TOKEN = "unit-test-cli-backend-key-xyz789"
RESULTS = []

# 假 CLI：argv = [tag, pidfile, donefile, 任务文本]
FAKE_CLI = r'''# -*- coding: utf-8 -*-
import os, sys, time
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")
tag, pidfile, donefile = sys.argv[1], sys.argv[2], sys.argv[3]
prompt = sys.argv[-1]
with open(pidfile, "w", encoding="utf-8") as f:
    f.write(str(os.getpid()))
if "#SLEEP" in prompt:
    time.sleep(5)
    with open(donefile, "w", encoding="utf-8") as f:
        f.write("done")   # 被杀掉的话这个文件永远不会出现
if "#FAIL" in prompt:
    sys.stderr.write("假CLI 故障：模拟非零退出，stderr 尾部标记 ERRTAIL-42\n")
    sys.exit(1)
sys.stdout.write("\x1b[32m[假CLI:%s]\x1b[0m %s\r\n" % (tag, prompt))
'''


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", name, ("  -- " + detail) if detail else ""))


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


# ------------------------------------------------------------------ HTTP 小工具

def request(port, path, method="GET", payload=None, token=TOKEN, timeout=60):
    url = "http://127.0.0.1:%d%s" % (port, path)
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token is not None:
        req.add_header("Authorization", "Bearer " + token)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        try:
            raw = resp.read()
            code = resp.getcode()
        finally:
            resp.close()
    except urllib.error.HTTPError as exc:
        code, raw = exc.code, exc.read()
    try:
        body = json.loads(raw.decode("utf-8"))
    except Exception:
        body = {"_raw": raw.decode("utf-8", "replace")}
    return code, body


def chat(port, model, text, token=TOKEN, extra=None, timeout=60):
    payload = {"model": model, "messages": [{"role": "user", "content": text}],
               "max_tokens": 2000}
    if extra:
        payload.update(extra)
    return request(port, "/v1/chat/completions", "POST", payload, token, timeout)


def content_of(body):
    try:
        return body["choices"][0]["message"]["content"]
    except Exception:
        return None


def pid_alive(pid):
    out = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid, "/NH"],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout
    return str(pid).encode("ascii") in (out or b"")


def wait_ready(port, proc, deadline=25.0):
    end = time.time() + deadline
    while time.time() < end:
        if proc.poll() is not None:
            return False
        try:
            code, _ = request(port, "/healthz", token=None, timeout=3)
            if code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.25)
    return False


# ------------------------------------------------------------------ 主流程

def main():
    tmp = tempfile.mkdtemp(prefix="cli_backend_test_")
    key_file = os.path.join(tmp, "key")
    fake_cli = os.path.join(tmp, "fake_cli.py")
    pi_pid_file = os.path.join(tmp, "pi.pid")
    pi_done_file = os.path.join(tmp, "pi.done")
    cl_pid_file = os.path.join(tmp, "cl.pid")
    cl_done_file = os.path.join(tmp, "cl.done")
    cx_pid_file = os.path.join(tmp, "cx.pid")
    cx_done_file = os.path.join(tmp, "cx.done")
    log_file = os.path.join(tmp, "cli_backend.log")

    with open(key_file, "w", encoding="utf-8") as f:
        f.write(TOKEN + "\n")
    with open(fake_cli, "w", encoding="utf-8") as f:
        f.write(FAKE_CLI)

    port = free_port()
    proc = None
    try:
        # ---------- 0. fail closed：key 文件不存在必须拒绝启动 ----------
        missing = subprocess.run(
            [sys.executable, SVC, "--port", str(free_port()),
             "--key-file", os.path.join(tmp, "no-such-key"), "--log-file", log_file],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
        check("0. key 文件缺失 → 拒绝启动(fail closed)", missing.returncode == 2,
              "rc=%d out=%s" % (missing.returncode,
                                (missing.stdout or b"").decode("utf-8", "replace").strip()[-120:]))

        # ---------- 0b. 只允许绑 loopback ----------
        bad_host = subprocess.run(
            [sys.executable, SVC, "--host", "0.0.0.0", "--port", str(free_port()),
             "--key-file", key_file, "--log-file", log_file],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
        check("0b. --host 0.0.0.0 → 拒绝启动", bad_host.returncode == 2,
              "rc=%d" % bad_host.returncode)

        # ---------- 拉起被测服务（三个成员都注入假 CLI）----------
        pi_cmd = json.dumps([sys.executable, fake_cli, "pi", pi_pid_file, pi_done_file])
        cl_cmd = json.dumps([sys.executable, fake_cli, "claude", cl_pid_file, cl_done_file])
        cx_cmd = json.dumps([sys.executable, fake_cli, "codex", cx_pid_file, cx_done_file])
        proc = subprocess.Popen(
            [sys.executable, SVC, "--host", "127.0.0.1", "--port", str(port),
             "--key-file", key_file, "--log-file", log_file,
             "--pi-workdir", tmp, "--claude-workdir", tmp, "--codex-workdir", tmp,
             "--busy-wait", "0.5",
             "--pi-cmd", pi_cmd, "--claude-cmd", cl_cmd, "--codex-cmd", cx_cmd],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if not wait_ready(port, proc):
            out = b""
            try:
                proc.kill()
                out = proc.stdout.read()
            except Exception:
                pass
            raise SystemExit("服务没起来: " + out.decode("utf-8", "replace")[-500:])

        # ---------- 1. OpenAI 形状 + 文本一致 ----------
        code, body = chat(port, "pi/default", "hello-from-test")
        text = content_of(body)
        ok = (code == 200 and isinstance(text, str)
              and text == "[假CLI:pi] hello-from-test"
              and body.get("object") == "chat.completion"
              and body["choices"][0]["message"]["role"] == "assistant")
        check("1. 假 CLI → OpenAI 形状且文本一致", ok, "code=%d content=%r" % (code, text))
        check("1b. ANSI/回车已清洗", isinstance(text, str) and "\x1b" not in text and "\r" not in text,
              repr(text))

        # ---------- 2. 鉴权 ----------
        c_none, _ = chat(port, "pi/default", "x", token=None)
        c_bad, _ = chat(port, "pi/default", "x", token="wrong-token")
        c_ok, _ = chat(port, "pi/default", "x")
        check("2. 无 token → 401", c_none == 401, "code=%d" % c_none)
        check("2b. 错 token → 401", c_bad == 401, "code=%d" % c_bad)
        check("2c. 对 token → 200", c_ok == 200, "code=%d" % c_ok)

        # ---------- 3. 未知 model → 404 ----------
        code, body = chat(port, "gpt-4o", "x")
        check("3. 未知 model → 404 unknown_model",
              code == 404 and body.get("error") == "unknown_model",
              "code=%d body=%s" % (code, json.dumps(body, ensure_ascii=False)))

        # ---------- 6. 中文往返 ----------
        zh = "你好，请用中文回答：今天几号？"
        code, body = chat(port, "claude/default", zh)
        text = content_of(body)
        check("6. 中文往返不乱码", code == 200 and text == "[假CLI:claude] " + zh,
              "content=%r" % text)

        # 带上下文的多轮：前面的消息拼成 [上下文] 前缀
        code, body = request(port, "/v1/chat/completions", "POST", {
            "model": "pi/default",
            "messages": [{"role": "user", "content": "第一轮问题"},
                         {"role": "assistant", "content": "第一轮回答"},
                         {"role": "user", "content": "第二轮问题"}],
            "max_tokens": 2000})
        text = content_of(body) or ""
        ok = (code == 200 and "[上下文]" in text and "对方: 第一轮问题" in text
              and "你: 第一轮回答" in text and text.rstrip().endswith("第二轮问题"))
        check("6b. 多轮上下文前缀拼接正确", ok, "content=%r" % text[:160])

        # ---------- 7. /healthz 不需鉴权 ----------
        code, body = request(port, "/healthz", token=None)
        check("7. /healthz 200 且免鉴权",
              code == 200 and body.get("ok") is True and body.get("service") == "cli_backend"
              and body.get("members") == ["piagent", "claudecode", "codex"],
              json.dumps(body, ensure_ascii=False))
        code, _ = request(port, "/v1/models", token=None)
        code2, body2 = request(port, "/v1/models")
        model_ids = [m["id"] for m in body2.get("data", [])]
        check("7b. /v1/models 需鉴权且列出三个模型",
              code == 401 and code2 == 200
              and model_ids == ["pi/default", "claude/default", "codex/default"],
              "anon=%d authed=%d ids=%s" % (code, code2, model_ids))

        # ---------- 5. 退出码 1 → 502 且 detail 含 stderr ----------
        code, body = chat(port, "pi/default", "触发失败 #FAIL")
        check("5. CLI 退出码 1 → 502 且 detail 含 stderr",
              code == 502 and body.get("error") == "backend_error"
              and "ERRTAIL-42" in (body.get("detail") or ""),
              "code=%d detail=%r" % (code, (body.get("detail") or "")[:120]))

        # ---------- 4. 超时 → 504 且进程被杀 ----------
        for path in (pi_pid_file, pi_done_file):
            if os.path.exists(path):
                os.remove(path)
        started = time.time()
        code, body = chat(port, "pi/default", "慢任务 #SLEEP", extra={"timeout_s": 1})
        elapsed = time.time() - started
        check("4. 超时 → 504 backend_timeout",
              code == 504 and body.get("error") == "backend_timeout" and elapsed < 4.5,
              "code=%d elapsed=%.1fs body=%s" % (code, elapsed, json.dumps(body, ensure_ascii=False)))
        child_pid = 0
        if os.path.exists(pi_pid_file):
            with open(pi_pid_file, "r", encoding="utf-8") as f:
                child_pid = int(f.read().strip() or 0)
        time.sleep(5.5)  # 等过假 CLI 原本的 5 秒睡眠窗口
        alive = pid_alive(child_pid) if child_pid else True
        check("4b. 超时后子进程树已被杀（无残留）",
              child_pid > 0 and not alive and not os.path.exists(pi_done_file),
              "pid=%d alive=%s done_file_exists=%s"
              % (child_pid, alive, os.path.exists(pi_done_file)))

        # ---------- 8. 并发：同成员串行/503，跨成员不互相阻塞 ----------
        results = {}

        def fire(key, model, text_, timeout_s):
            results[key] = chat(port, model, text_, extra={"timeout_s": timeout_s}, timeout=60)

        t1 = threading.Thread(target=fire, args=("a", "pi/default", "并发A #SLEEP", 30))
        t2 = threading.Thread(target=fire, args=("b", "pi/default", "并发B", 30))
        t1.start()
        time.sleep(0.6)   # 确保 A 已抢到 pi 的锁
        t2.start()
        t1.join(60)
        t2.join(60)
        code_a, _ = results.get("a", (0, {}))
        code_b, body_b = results.get("b", (0, {}))
        check("8. 同成员并发 → 一个 200、另一个 503 busy",
              code_a == 200 and code_b == 503 and body_b.get("error") == "busy",
              "A=%d B=%d bodyB=%s" % (code_a, code_b, json.dumps(body_b, ensure_ascii=False)))

        results.clear()
        t3 = threading.Thread(target=fire, args=("pi", "pi/default", "占住pi #SLEEP", 30))
        t4 = threading.Thread(target=fire, args=("cl", "claude/default", "claude 并行", 30))
        t3.start()
        time.sleep(0.6)
        started = time.time()
        t4.start()
        t4.join(30)
        cl_elapsed = time.time() - started
        code_cl, _ = results.get("cl", (0, {}))
        t3.join(60)
        check("8b. 跨成员并发互不阻塞（claude 在 pi 忙时秒回）",
              code_cl == 200 and cl_elapsed < 3.0,
              "claude code=%d elapsed=%.1fs" % (code_cl, cl_elapsed))

        # ---------- 10~15. codex 成员 ----------
        check("10. 模型列表含 codex/default", "codex/default" in model_ids, str(model_ids))

        check("11. resolve_model 解析 codex 前缀",
              cli_backend.resolve_model("codex/default") == ("codex", "")
              and cli_backend.resolve_model("codex/gpt-5-codex") == ("codex", "gpt-5-codex")
              and cli_backend.resolve_model("codex") == ("codex", "")
              and cli_backend.resolve_model("pi/default") == ("piagent", "")
              and cli_backend.resolve_model("claude/default") == ("claudecode", ""),
              repr(cli_backend.resolve_model("codex/gpt-5-codex")))

        # 笔记本原本配的就是 openclaw/codex（网关只有 openclaw/default，实测 400）——
        # 它不属于本垫片的任何前缀，必须照样 404，不能被 codex 分支误吞
        code, body = chat(port, "openclaw/codex", "x")
        c2, b2 = chat(port, "gpt-4o-codex-ish", "x")
        check("12. 未知模型仍 404 unknown_model（含 openclaw/codex）",
              code == 404 and body.get("error") == "unknown_model"
              and c2 == 404 and b2.get("error") == "unknown_model",
              "openclaw/codex=%d other=%d" % (code, c2))

        code, body = chat(port, "codex/default", "codex-hello 你好")
        text = content_of(body)
        check("13. codex/default 走完 dispatch 且回包是假 CLI 的 stdout",
              code == 200 and text == "[假CLI:codex] codex-hello 你好"
              and body.get("model") == "codex/default"
              and body["choices"][0]["message"]["role"] == "assistant",
              "code=%d content=%r" % (code, text))

        code, body = chat(port, "codex/default", "触发失败 #FAIL")
        check("14. codex CLI 退出码 1 → 502 且 detail 含 stderr",
              code == 502 and body.get("error") == "backend_error"
              and "ERRTAIL-42" in (body.get("detail") or ""),
              "code=%d detail=%r" % (code, (body.get("detail") or "")[:120]))

        for path in (cx_pid_file, cx_done_file):
            if os.path.exists(path):
                os.remove(path)
        started = time.time()
        code, body = chat(port, "codex/default", "慢任务 #SLEEP", extra={"timeout_s": 1})
        elapsed = time.time() - started
        cx_pid = 0
        if os.path.exists(cx_pid_file):
            with open(cx_pid_file, "r", encoding="utf-8") as f:
                cx_pid = int(f.read().strip() or 0)
        time.sleep(5.5)
        check("15. codex 超时 → 504 且子进程树被杀",
              code == 504 and body.get("error") == "backend_timeout" and elapsed < 4.5
              and cx_pid > 0 and not pid_alive(cx_pid) and not os.path.exists(cx_done_file),
              "code=%d elapsed=%.1fs pid=%d done_file=%s"
              % (code, elapsed, cx_pid, os.path.exists(cx_done_file)))

        # ---------- 9. 日志不记 key、不记全文 ----------
        log_text = ""
        if os.path.exists(log_file):
            with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                log_text = f.read()
        check("9. 日志不含 key 且有单行任务摘要",
              TOKEN not in log_text and "member=piagent" in log_text
              and "member=codex" in log_text,
              "日志 %d 字节" % len(log_text))

    finally:
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=10)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        shutil.rmtree(tmp, ignore_errors=True)

    failed = [r for r in RESULTS if not r[1]]
    print("\n合计 %d 项，PASS %d，FAIL %d" % (len(RESULTS), len(RESULTS) - len(failed), len(failed)))
    for name, _, detail in failed:
        print("  FAIL: %s  %s" % (name, detail))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
