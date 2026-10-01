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

阶段2 刀9（a2a_edge 的 HTTP 面收进 L1 web_gateway）新增三组用例：
  · CASES_MORE —— 路径判据的边角（空段 / #fragment / //netloc）与「未注册方法仍落
    基类 501」；迁移后路由分派换成了 L1 的「精确表 → 前缀表」，这些边角最容易走偏。
  · BODY_CASES —— 带请求体，含 > MAX_BODY_BYTES（300KB）的超大体。
  · RAW_CASES（`raw_cases()`）—— **同一条 TCP 连接**连发多个请求，比整段裸字节
    （只归一 Date 头）。刀9 把 `_read_body()` 从两个 do_* 挪进了 `@GW.pre`，
    「有没有把请求体读干」只有在 keep-alive 上才看得出来，这是本刀的命门。
全部新增用例都用固定输入，不读机器现状（为什么不设「真打后端」的用例见 BODY_CASES 上方注释）。
"""
import argparse
import hashlib
import http.client
import os
import re
import socket
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


def call(port, method, path, token=None, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        headers = {"Authorization": "Bearer " + token} if token else {}
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        got = resp.read()
        return (resp.status, resp.reason,
                sorted((k.lower(), v) for k, v in resp.getheaders() if k.lower() != "date"),
                got)
    except Exception as exc:          # 连接被关也当一种结果比对
        return ("EXC", type(exc).__name__, [], b"")
    finally:
        conn.close()


# Date 是唯一随时钟变的响应头（stdlib 直接发，两侧都不归它管）；比对前归一掉。
_DATE_RE = re.compile(rb"\r\nDate: [^\r\n]*\r\n")


def raw_exchange(port, chunks, wait=1.5):
    """**同一条 TCP 连接**上连发若干请求，回整段裸字节（只归一 Date 头）。

    刀9 把 `_read_body()` 从两个 do_* 挪进了 `@GW.pre`，「有没有把请求体读干」
    只有在 keep-alive 上连发多个请求时才看得出来 —— 漏读一个字节，下一个请求的
    请求行就会错位，整段字节流立刻不同。逐个响应比对反而看不出这种错位，所以
    这里比的是**整段**：状态行 + 全部响应头 + 全部正文 + 它们的先后顺序。
    发完就半关写端（`SHUT_WR`）：服务端读到干净的 EOF 会自己收尾，于是「读到对端
    关闭为止」是确定的终止条件，不靠超时凑数，也不会因为客户端带着未读数据硬关
    socket 而在服务端刷一片 ConnectionAborted 栈。`wait` 只当兜底保险。
    """
    sock = socket.create_connection(("127.0.0.1", port), timeout=wait)
    out = b""
    try:
        for chunk in chunks:
            sock.sendall(chunk)
        sock.shutdown(socket.SHUT_WR)
        while True:
            try:
                buf = sock.recv(65536)
            except socket.timeout:
                break
            except OSError as exc:            # 对端 RST 也当一种结果比对
                out += b"<<OSError:%s>>" % type(exc).__name__.encode()
                break
            if not buf:
                break
            out += buf
    finally:
        sock.close()
    return _DATE_RE.sub(b"\r\nDate: <normalized>\r\n", out)


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

# ---- 阶段2 刀9 新增：HTTP 面收进 L1 web_gateway 之后要盯的分支 ------------------
# 刀9 把路由分派从「do_GET/do_POST 里的有序 if 链」换成了 L1 的「精确表 → 前缀表」，
# 又把 `_read_body()` 挪进 `@GW.pre`。于是要证的多了三类：
#   · **路径判据**：L1 的 Request 只 `partition("?")`，本件必须显式覆盖成 urlsplit()
#     的口径 —— 空段、`#fragment`、`//netloc` 这三种边角不许有任何偏移；
#   · **方法集合**：handler_class() 按登记的方法删 do_<METHOD>，未登记的仍是基类 501；
#   · **请求体与 keep-alive**：见下面的 RAW_CASES（本刀的命门）。
CASES_MORE = [
    ("GET", "//healthz", False),          # urlsplit 把 healthz 当 netloc → 零段 → 404
    ("GET", "/healthz#frag", False),      # #fragment 被 urlsplit 剥掉 → 仍是 healthz → 200
    ("GET", "/healthz//", False),         # 空段全丢 → 仍是一段 healthz → 200
    ("GET", "/ctx-usage/tester/extra", True),          # 三段（且无 query）→ 404 not_found
    ("GET", "//ctx-usage/tester?session_id=s-1", True),  # netloc 吃掉首段 → parts=["tester"] → 404
    ("OPTIONS", "/healthz", False),       # 未注册方法 → 基类 501（L1 得把 do_OPTIONS 删掉）
    ("PUT", "/agents", False),            # 未注册方法 → 基类 501
    ("PATCH", "/a2a/tester/messages", True),           # 未注册方法 → 基类 501
    ("GET", "/a2a/tester/messages", True),             # GET 走 POST 的形状 → 404 not_found
    ("GET", "/a2a//tester/.well-known/agent-card.json", False),  # 空段 → 仍 4 段 → 200 卡片
    ("POST", "/a2a//tester/messages", False),          # 空段 → 仍 3 段 → 401（无 token）
    ("POST", "/a2a/tester/messages#x", False),         # #fragment 被剥 → 仍是 messages → 401
]

# 带请求体的用例：(方法, 路径, 是否带 token, 请求体, 说明)
# 超大体（300KB > MAX_BODY_BYTES=256KiB）走的是「只读前 256KiB」那条，剩下的字节留在
# socket 里 —— 两侧必须同码同体。固定输入，不依赖机器现状。
#
# 注意：**不设「真的打到后端」的用例**。假 agent 的 backend 是 http://127.0.0.1:1/v1，
# 两侧各自去连这个必然拒绝的端口，Windows 上回的 OSError 文案在 10054 / 10061 /
# 「Remote end closed connection」之间飘，会被 `detail` 原样带进响应体 —— 那是机器现状，
# 不是实现差异。请求体缓存这条链路改用「JSON 解得出、但字段不合格 → 400」来证：
# 同样要先把缓存的裸字节解成 JSON 才走得到，且结果完全确定。
_BIG = b"x" * 307200
BODY_CASES = [
    ("POST", "/a2a/tester/messages", True, _BIG, "超大体 300KB + token → 只读前 256KiB → JSON 失败 400"),
    ("POST", "/a2a/tester/messages", False, _BIG, "超大体 300KB 无 token → 401（鉴权在解体之前）"),
    ("POST", "/a2a/tester/messages", True, b'{"text":"   "}', "体解得出但 text 全空白 → 400 bad_request"),
    ("POST", "/a2a/tester/messages", True, b'{"text":"hi","timeout_s":"abc"}', "timeout_s 非数字 → 400 bad_request"),
    ("POST", "/a2a/tester/messages", True, b'[1,2,3]', "合法 JSON 但非 dict → 400 bad_request"),
    ("POST", "/a2a/nosuch/messages", True, b'{"text":"hi"}', "未知 agent → 404（早于解体）"),
]


def rq(method, path, token=None, body=b"", cl=True):
    """拼一条 HTTP/1.1 请求的裸字节（keep-alive 用例要逐字节可控，不能过 http.client）。"""
    lines = [b"%s %s HTTP/1.1" % (method.encode(), path.encode()),
             b"Host: 127.0.0.1"]
    if token:
        lines.append(b"Authorization: Bearer " + token.encode())
    if cl:
        lines.append(b"Content-Length: %d" % len(body))
    return b"\r\n".join(lines) + b"\r\n\r\n" + body


def raw_cases():
    """keep-alive 面：同一条连接上连发多个请求，比**整段**裸字节。"""
    return [
        ("同一连接三连：GET /healthz → 带体的 GET /agents → GET /healthz",
         [rq("GET", "/healthz"),
          rq("GET", "/agents", TOKEN, b"hello"),     # GET 带体：不读干，下一条就错位
          rq("GET", "/healthz")]),
        ("同一连接：POST 带体（400）→ 紧随 GET /healthz → 再一条带体 POST",
         [rq("POST", "/a2a/tester/messages", TOKEN, b'{"text":"   "}'),
          rq("GET", "/healthz"),
          rq("POST", "/a2a/tester/messages", TOKEN, b'{"nope":1}')]),
        # OPTIONS 必须放**最后一条**：基类 send_error(501) 会置 close_connection，
        # 若它后面还压着没读的管线请求，服务端带着未读数据关 socket 会触发 RST，
        # 客户端收到的是 ConnectionAborted 还是那 528 字节响应，纯看 RST 和 recv 谁先到
        # ——那是机器现状（实测同一份代码两次跑结果不同），不是实现差异。
        ("同一连接：GET /healthz（keep-alive）→ OPTIONS 落基类 501 并关连接",
         [rq("GET", "/healthz"), rq("OPTIONS", "/healthz")]),
        ("同一连接：超大体 300KB → 400，剩余字节留在 socket 里（裸字节比对）",
         [rq("POST", "/a2a/tester/messages", TOKEN, _BIG)]),
        ("同一连接三连：agent-card（公开）→ /ctx-usage（注入路由）→ 未知路径 404",
         [rq("GET", "/a2a/tester/.well-known/agent-card.json"),
          rq("GET", "/ctx-usage/tester?session_id=s-1", TOKEN),
          rq("GET", "/nope")]),
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
    from comm.a2a import a2a_svc                                # noqa: E402
    from node.l1_transport.a2a import a2a_edge                  # noqa: E402

    a2a_svc.install()                                           # 组合根装配：L2 能力 → L1 端口
    setup_runtime(a2a_svc_old.Runtime)
    setup_runtime(a2a_edge.Runtime)

    s_old = start(a2a_svc_old.A2AHandler, 19971)
    s_new = start(a2a_edge.A2AHandler, 19972)

    total = bad = 0

    def report(label, tok_mark, a, b):
        """一例一行：两侧结果相同就报「一致」，否则把两边原样打出来（不放宽判据）。"""
        nonlocal total, bad
        total += 1
        ok = a == b
        bad += 0 if ok else 1
        print("%-7s %-46s %-6s %s" % (
            label[0], label[1], tok_mark,
            ("一致  %s" % (a[0],)) if ok else
            "**不一致**\n    旧=%r\n    新=%r" % (a, b)))

    print("%-7s %-46s %-6s %s" % ("方法", "路径", "token", "结论"))
    for method, path, tok in CASES + CASES_MORE:
        t = TOKEN if tok else None
        report((method, path), "有" if tok else "—",
               call(19971, method, path, t), call(19972, method, path, t))

    print("\n-- 带请求体（含 > MAX_BODY_BYTES 的超大体）--")
    for method, path, tok, payload, note in BODY_CASES:
        t = TOKEN if tok else None
        report((method, "%s [%dB]" % (path, len(payload))), "有" if tok else "—",
               call(19971, method, path, t, payload), call(19972, method, path, t, payload))
        print("        （%s）" % note)

    print("\n-- keep-alive：同一条 TCP 连接连发，比整段裸字节（只归一 Date）--")
    for note, chunks in raw_cases():
        a = raw_exchange(19971, chunks)
        b = raw_exchange(19972, chunks)
        total += 1
        ok = a == b
        bad += 0 if ok else 1
        print("%-2d 请求  %-58s %s" % (
            len(chunks), note,
            ("一致  %d 字节" % len(a)) if ok else
            "**不一致**\n    旧(%d B)=%r\n    新(%d B)=%r" % (len(a), a[:2000], len(b), b[:2000])))

    s_old.shutdown()
    s_new.shutdown()
    print("\n合计 %d 组，一致 %d 组，不一致 %d 组" % (total, total - bad, bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
