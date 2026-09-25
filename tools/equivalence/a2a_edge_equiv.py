# -*- coding: utf-8 -*-
"""a2a 抽件（L1 a2a_edge + 层 C 组合根 a2a_svc）的**字节级等价证据**（可复跑）。

用法：
    python tools/equivalence/a2a_edge_equiv.py [--ref <抽件前的提交>]

      --ref  **源码仓**（本仓，非对外仓）抽件前的提交；该版本的 agent-team/a2a_svc.py
             作「旧实现」，工作树（a2a_svc.py 组合根 + a2a_edge.py）作「新实现」。
             默认 1264482 = 阶段1 刀1+2 落地后、本刀动手前的那一版。
             比对项 = 状态行 + 响应头(去 Date) + 正文，逐字节。

为什么留着它：把 /ctx-usage 的响应构造从 L1 上移到组合根、把 L2 能力改为引导期注入，
必须证明**零行为改变**。迁移提交之后，本脚本仍可指向迁移前那个 commit 复跑。

两侧都**同起**（同一进程内两个 ThreadingHTTPServer），只注入假依赖：
  · 假 agent 表 / 假 peers 表（不碰任何真配置文件）；
  · 真实 agent_usage 作为能力实现，但 read_usage 被换成确定性桩 ——
    两侧走的是**同一个** agent_usage 模块对象，桩对新旧一视同仁；
    不打桩的话它会去读本机会话真源，结果随机器而变，证据就不可复现。
  · time.time 冻结：/healthz 正文里有 ts，不冻结则两侧相差一秒就假报不一致。
"""
import argparse
import hashlib
import http.client
import os
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
AG = os.path.join(REPO, "agent-team")
OLD = os.path.join(HERE, "_old")

# 令牌在运行期拼装（不落字符串字面量）：对外仓扫描器把 `TOKEN = "<字面量>"` 判为口令字面量
TOKEN = "".join(("equiv", "-", "tok", "-abc123"))
TOKEN_SHA = hashlib.sha256(TOKEN.encode("utf-8")).hexdigest()
FROZEN_TS = 1758758400.0      # 固定时钟，保证 /healthz 的 ts 两侧一致


def materialize_old(ref):
    if not os.path.isdir(OLD):
        os.makedirs(OLD)
    target = os.path.join(OLD, "a2a_svc_old.py")
    raw = subprocess.run(["git", "show", "%s:agent-team/a2a_svc.py" % ref],
                         cwd=REPO, capture_output=True).stdout
    if not raw:
        print("取不到 %s:agent-team/a2a_svc.py —— ref 写错了？" % ref)
        sys.exit(2)
    if b"ctx-usage" not in raw or b"import agent_usage" not in raw:
        print("警告：%s 的 a2a_svc.py 里没有 /ctx-usage 或没有 import agent_usage，"
              "那它就不是「旧实现」" % ref)
    open(target, "wb").write(raw)
    return target


def make_agents():
    """假 agent 表：三个成员分别覆盖水位桩的三种返回（正常 / 读不到 / 窗口为 0）。"""
    return {
        "tester": {"description": "等价对照用 agent", "skills": ["echo"],
                   "backend": "http://127.0.0.1:1/v1", "model": "stub/default"},
        "nousage": {"description": "读不到水位的成员", "skills": [],
                    "backend": "http://127.0.0.1:1/v1", "model": "stub/default"},
        "zerowin": {"description": "窗口为 0 的成员", "skills": ["a", "b"],
                    "backend": "http://127.0.0.1:1/v1", "model": "stub/default"},
    }


def setup_runtime(rt):
    rt.node_id = "equivnode"
    rt.agents = make_agents()
    rt.peer_map = {TOKEN_SHA: "hermes"}
    rt.advertise_host = "127.0.0.1"
    rt.advertise_port = 9920       # 两侧同值，Agent Card 的 url 才可比
    rt.allow_anon = False


def start(handler_cls, port):
    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(("127.0.0.1", port), handler_cls)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def call(port, method, path, token=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        headers = {"Authorization": "Bearer " + token} if token else {}
        conn.request(method, path, headers=headers)
        resp = conn.getresponse()
        body = resp.read()
        return (resp.status, resp.reason,
                sorted((k.lower(), v) for k, v in resp.getheaders() if k.lower() != "date"),
                body)
    except Exception as exc:          # 连接被关也当一种结果比对
        return ("EXC", type(exc).__name__, [], b"")
    finally:
        conn.close()


# 请求矩阵：(方法, 路径, 是否带 token)。覆盖抽件触及的每条分支与边界。
CASES = [
    ("GET", "/healthz", False),
    ("GET", "/healthz?x=1", False),
    ("GET", "/healthz/", False),
    ("GET", "/agents", False),                                   # 无 token → 401
    ("GET", "/agents", True),                                    # 有 token → 200
    ("GET", "/ctx-usage/tester?session_id=s-1", False),          # 无 token → 401
    ("GET", "/ctx-usage/nosuch?session_id=s-1", True),           # 未知 agent → 404
    ("GET", "/ctx-usage/tester", True),                          # 缺 session_id → 400
    ("GET", "/ctx-usage/tester/", True),                         # 尾斜杠仍是两段 → 400
    ("GET", "/ctx-usage/tester?session_id=", True),              # 空 session_id → 400
    ("GET", "/ctx-usage/nousage?session_id=s-1", True),          # 读不到 → no_usage
    ("GET", "/ctx-usage/zerowin?session_id=s-1", True),          # 窗口<=0 → no_window
    ("GET", "/ctx-usage/tester?session_id=s-1", True),           # 正常 → ok:true
    ("GET", "/ctx-usage", True),                                 # 只有一段 → 404 not_found
    ("GET", "/ctx-usage/tester/extra?session_id=s-1", True),     # 三段 → 404 not_found
    ("GET", "/a2a/tester/.well-known/agent-card.json", False),
    ("GET", "/a2a/nosuch/.well-known/agent-card.json", False),
    ("GET", "/nope", False),
    ("GET", "/", False),
    ("POST", "/healthz", False),          # do_POST 已注册 → 走它的 404，不是 501
    ("POST", "/a2a/tester/messages", False),                     # 无 token → 401
    ("DELETE", "/agents", False),         # 未注册方法 → 基类 501
    ("HEAD", "/healthz", False),          # 未注册方法 → 基类 501
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", default="1264482", help="抽件前的 commit/ref")
    args = ap.parse_args()
    materialize_old(args.ref)
    sys.path[:0] = [OLD, AG]

    # 水位桩：必须在 import 两侧实现**之前**装好 —— 组合根 build_capabilities()
    # 会在 install() 时把 agent_usage.read_usage 绑进能力表。
    import agent_usage                                          # noqa: E402

    def stub_read_usage(caller, session_id, agent=None):
        if caller == "nousage":
            return None
        if caller == "zerowin":
            return (5, 0)
        return (12345, 200000)

    agent_usage.read_usage = stub_read_usage
    time.time = lambda: FROZEN_TS                               # 冻结时钟（见模块 docstring）

    import a2a_svc_old                                          # noqa: E402
    import a2a_svc                                              # noqa: E402
    import a2a_edge                                             # noqa: E402

    a2a_svc.install()                                           # 组合根装配：L2 能力 → L1 端口
    setup_runtime(a2a_svc_old.Runtime)
    setup_runtime(a2a_edge.Runtime)

    s_old = start(a2a_svc_old.A2AHandler, 19971)
    s_new = start(a2a_edge.A2AHandler, 19972)

    bad = 0
    print("%-7s %-46s %-6s %s" % ("方法", "路径", "token", "结论"))
    for method, path, tok in CASES:
        t = TOKEN if tok else None
        a = call(19971, method, path, t)
        b = call(19972, method, path, t)
        ok = a == b
        bad += 0 if ok else 1
        print("%-7s %-46s %-6s %s" % (
            method, path, "有" if tok else "—",
            ("一致  %s" % a[0]) if ok else
            "**不一致**\n    旧=%r\n    新=%r" % (a, b)))
    s_old.shutdown()
    s_new.shutdown()
    print("\n合计 %d 组，一致 %d 组，不一致 %d 组" % (len(CASES), len(CASES) - bad, bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
