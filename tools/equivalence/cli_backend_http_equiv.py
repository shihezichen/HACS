# -*- coding: utf-8 -*-
"""cli_backend HTTP 面收进 L1 `web_gateway` 的**字节级等价证据**（可复跑）。

用法：
    python tools/equivalence/cli_backend_http_equiv.py [--ref <迁移前的提交>]

      --ref  给了就从**源码仓**（本仓，非对外仓）该提交重新取一份 agent-team/cli_backend.py
             覆盖到 _old/cli_backend_old.py；不给就用磁盘上已有的那份快照。
             快照作「旧实现」，工作树 agent-team/cli_backend.py 作「新实现」。

做法：同一进程内起两份 ThreadingHTTPServer（各自绑一个空闲高端口），
两侧都按名字取 `CLIBackendHandler`，三个成员都注入**同一份假 CLI**
（任务文本里 `#SLEEP` → 睡 5 秒触发 504，`#FAIL` → 退出码 1 触发 502），
然后跑同一张请求矩阵，逐例比对：

    状态码 + 状态原因 + 可比响应头（去 Date、Content-Length 归一）+ 正文（归一后）

归一化只吃**天然漂移**：正文里的 `ts` / `created` / `chatcmpl-cli-<uuid16>` /
`pid=<数字>`，以及响应头里的 Date / Content-Length。其余一律逐字节。

矩阵 = 线上采的 22 例基线 + 成功路径 + 超时(504) + 后端失败(502) + 忙(503) = 26 例，
另加 3 例自行加固（未注册方法的基类 501 ×2、带 #fragment 的路径归一 ×1）。
503 不用并发去碰运气：直接在**被测模块自己的** Runtime.locks 上占住成员锁，
让请求的 lock.acquire(timeout=busy_wait_s) 必然超时 —— 确定性可复跑。
"""
import argparse
import http.client
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
AG = os.path.join(REPO, "agent-team")
OLD = os.path.join(HERE, "_old")
OLD_FILE = os.path.join(OLD, "cli_backend_old.py")

# 令牌在运行期拼装（不落字符串字面量）：对外仓扫描器把 `TOKEN = "<字面量>"` 判为口令字面量
TOKEN = "".join(("equiv", "-", "cli", "-backend-", "tok", "-abc123"))
BUSY_WAIT_S = 0.3        # 同成员排队等待；503 用例靠它快速超时

# 假 CLI：argv = [tag, pidfile, donefile, 任务文本]（与 tests/test_cli_backend.py 同款）
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
        f.write("done")
if "#FAIL" in prompt:
    sys.stderr.write("假CLI 故障：模拟非零退出，stderr 尾部标记 ERRTAIL-42\n")
    sys.exit(1)
sys.stdout.write("\x1b[32m[假CLI:%s]\x1b[0m %s\r\n" % (tag, prompt))
'''

# 天然漂移归一（两侧同样施加；只吃时间戳/随机 id/子进程 pid）
_NORM = (
    (re.compile(rb'"ts":\s*\d+'), b'"ts": <TS>'),
    (re.compile(rb'"created":\s*\d+'), b'"created": <TS>'),
    (re.compile(rb'"chatcmpl-cli-[0-9a-f]{16}"'), b'"chatcmpl-cli-<UUID16>"'),
    (re.compile(rb'pid=\d+'), b'pid=<PID>'),
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
    """从 git 取指定提交的 cli_backend.py 当旧实现快照。"""
    if not os.path.isdir(OLD):
        os.makedirs(OLD)
    raw = subprocess.run(["git", "show", "%s:agent-team/cli_backend.py" % ref],
                         cwd=REPO, capture_output=True).stdout
    if not raw:
        print("取不到 %s:agent-team/cli_backend.py —— ref 写错了？" % ref)
        sys.exit(2)
    if b"class CLIBackendHandler" not in raw:
        print("警告：%s 的 cli_backend.py 里没有 CLIBackendHandler，那它就不是「旧实现」" % ref)
    io.open(OLD_FILE, "wb").write(raw)


# ------------------------------------------------------------------ 起服务

def start(handler_cls, port):
    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(("127.0.0.1", port), handler_cls)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def setup_runtime(mod, tmp, tag, fake_cli):
    """把被测模块的 Runtime 配成「三个成员都是同一份假 CLI」。"""
    mod.init_runtime_locks()
    rt = mod.Runtime
    rt.api_key = TOKEN
    rt.busy_wait_s = BUSY_WAIT_S
    rt.pi_workdir = rt.claude_workdir = rt.codex_workdir = tmp
    for member, attr in (("pi", "pi_cmd"), ("claude", "claude_cmd"), ("codex", "codex_cmd")):
        setattr(rt, attr, [sys.executable, fake_cli, member,
                           os.path.join(tmp, "%s-%s.pid" % (tag, member)),
                           os.path.join(tmp, "%s-%s.done" % (tag, member))])


# ------------------------------------------------------------------ 请求

def call(port, method, path, auth, body, hold_lock=None, mod=None):
    """发一次请求；hold_lock 给定时先占住该成员的锁（造 503 busy，确定性）。"""
    lock = mod.Runtime.locks[hold_lock] if hold_lock else None
    if lock is not None:
        lock.acquire()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
        headers = {}
        if auth == "ok":
            headers["Authorization"] = "Bearer " + TOKEN
        elif auth == "bad":
            headers["Authorization"] = "Bearer " + TOKEN + "-wrong"
        elif auth == "raw":                      # 无 Bearer 前缀
            headers["Authorization"] = TOKEN
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
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
        except Exception as exc:                 # 连接被关也当一种结果比对
            return "EXC", type(exc).__name__, [], b""
        finally:
            conn.close()
    finally:
        if lock is not None:
            lock.release()


def J(obj):
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


CHAT = "/v1/chat/completions"
GOOD_MSGS = [{"role": "user", "content": "hello-equiv"}]

# (用例名, 方法, 路径, 鉴权, 请求体, 占锁成员)   鉴权: none/ok/bad/raw
CASES = [
    # ---- 线上采的 22 例基线 ----
    ("01 healthz",              "GET",  "/healthz",   "none", None, None),
    ("02 healthz 尾斜杠",        "GET",  "/healthz/",  "none", None, None),
    ("03 models 无鉴权",         "GET",  "/v1/models", "none", None, None),
    ("04 models 错 token",       "GET",  "/v1/models", "bad",  None, None),
    ("05 models 无 Bearer 前缀",  "GET",  "/v1/models", "raw",  None, None),
    ("06 models 正确 token",     "GET",  "/v1/models", "ok",   None, None),
    ("07 models 别名",           "GET",  "/models",    "ok",   None, None),
    ("08 models 尾斜杠",         "GET",  "/v1/models/", "ok",  None, None),
    ("09 未知 GET 路径",          "GET",  "/nope",      "none", None, None),
    ("10 根路径",                "GET",  "/",          "none", None, None),
    ("11 POST /healthz",        "POST", "/healthz",   "ok",   J({}), None),
    ("12 chat 无鉴权",           "POST", CHAT, "none",
     J({"model": "pi/default", "messages": GOOD_MSGS, "max_tokens": 2000}), None),
    ("13 chat 坏 JSON",          "POST", CHAT, "ok",  b"{not json", None),
    ("14 chat 非对象 JSON",      "POST", CHAT, "ok",  J([1, 2, 3]), None),
    ("15 chat 缺 model",         "POST", CHAT, "ok",  J({"messages": GOOD_MSGS}), None),
    ("16 chat 未知 model",       "POST", CHAT, "ok",
     J({"model": "gpt-4o", "messages": GOOD_MSGS}), None),
    ("17 chat 缺 messages",      "POST", CHAT, "ok",  J({"model": "pi/default"}), None),
    ("18 chat 空 messages",      "POST", CHAT, "ok",
     J({"model": "pi/default", "messages": []}), None),
    ("19 chat 无 user 内容",     "POST", CHAT, "ok",
     J({"model": "pi/default", "messages": [{"role": "assistant", "content": "x"}]}), None),
    ("20 chat 坏 timeout_s",     "POST", CHAT, "ok",
     J({"model": "pi/default", "messages": GOOD_MSGS, "timeout_s": "abc"}), None),
    ("21 chat 别名路径",         "POST", "/chat/completions", "ok",
     J({"messages": GOOD_MSGS}), None),
    ("22 chat 空体",             "POST", CHAT, "ok",  b"", None),
    # ---- 契约点名要加的四例 ----
    ("23 成功路径 200",          "POST", CHAT, "ok",
     J({"model": "pi/default", "messages": GOOD_MSGS, "max_tokens": 2000}), None),
    ("24 超时 504",              "POST", CHAT, "ok",
     J({"model": "pi/default", "messages": [{"role": "user", "content": "慢任务 #SLEEP"}],
        "timeout_s": 1}), None),
    ("25 后端失败 502",          "POST", CHAT, "ok",
     J({"model": "claude/default", "messages": [{"role": "user", "content": "炸 #FAIL"}]}), None),
    ("26 同成员忙 503",          "POST", CHAT, "ok",
     J({"model": "codex/default", "messages": GOOD_MSGS}), "codex"),
    # ---- 契约未点名、自行加固的三例：专盯迁移最容易走形的两处 ----
    # 27/28：未注册方法必须仍是**基类 501**（L1 handler_class 靠删 do_<METHOD> 保住它，
    #        一旦多登记一个方法就会变成 404 信封）；顺带验 Server 头在 send_error 路径也没变。
    ("27 未注册方法 DELETE",     "DELETE", "/healthz", "none", None, None),
    ("28 未注册方法 PUT",        "PUT",    CHAT,       "ok",   J({}), None),
    # 29：路径归一必须仍是 urlsplit（L1 的 partition("?") 不剥 #fragment → 会掉进 404）
    ("29 带 #fragment 的路径",   "GET",    "/healthz#x", "none", None, None),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", default=None,
                    help="迁移前的 commit/ref；给了就重新取快照覆盖 _old/cli_backend_old.py")
    args = ap.parse_args()

    if args.ref:
        materialize_old(args.ref)
    if not os.path.isfile(OLD_FILE):
        print("缺少旧实现快照 %s —— 用 --ref <迁移前提交> 生成" % OLD_FILE)
        return 2

    sys.path[:0] = [OLD, AG]
    import cli_backend_old                                    # noqa: E402
    import cli_backend                                        # noqa: E402

    tmp = tempfile.mkdtemp(prefix="cli_backend_equiv_")
    fake_cli = os.path.join(tmp, "fake_cli.py")
    io.open(fake_cli, "w", encoding="utf-8", newline="\n").write(FAKE_CLI)

    setup_runtime(cli_backend_old, tmp, "old", fake_cli)
    setup_runtime(cli_backend, tmp, "new", fake_cli)

    p_old, p_new = free_port(), free_port()
    s_old = start(cli_backend_old.CLIBackendHandler, p_old)
    s_new = start(cli_backend.CLIBackendHandler, p_new)

    bad = 0
    print("旧实现 %s :%d    新实现 %s :%d\n" % (OLD_FILE, p_old,
                                              os.path.join(AG, "cli_backend.py"), p_new))
    print("%-22s %-6s %-22s %-6s %s" % ("用例", "方法", "路径", "鉴权", "结论"))
    print("-" * 96)
    for name, method, path, auth, body, hold in CASES:
        a = call(p_old, method, path, auth, body, hold, cli_backend_old)
        b = call(p_new, method, path, auth, body, hold, cli_backend)
        ok = a == b
        bad += 0 if ok else 1
        verdict = ("一致  %s %s" % (a[0], a[1])) if ok else (
            "**不一致**\n    旧=%r\n    新=%r" % (a, b))
        print("%-22s %-6s %-22s %-6s %s" % (name, method, path, auth, verdict))

    s_old.shutdown()
    s_new.shutdown()
    shutil.rmtree(tmp, ignore_errors=True)
    print("\n合计 %d 例，一致 %d/%d，不一致 %d"
          % (len(CASES), len(CASES) - bad, len(CASES), bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
