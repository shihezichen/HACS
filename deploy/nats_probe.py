#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""nats_probe.py —— 纯标准库 NATS 客户端探针（零 pip 依赖，Python 3.9+）

用途：不装 nats-py 也能验证「NATS 平面（默认 9930）在本机/跨机是否可用」。
做四件事并在最后打印判定：
  1) CONNECT 鉴权（user/password）
  2) PUB 一条 → 同连接 SUB 收回来（往返）
  3) request/reply（RP）：在后台订阅 a2a.v1.req.<whoami>，另一连接发请求，验证「点名」能应答
  4) 错误口令必须被拒（负例）

用法：
    python nats_probe.py --host <NODE_IP> --port 9930 \\
        --user hermes --password-file ~/.nats-pass --whoami hermes

注：客户端本体已抽到 nats_client.py（与 teamd.py 共用），本文件只剩探针逻辑。
"""
import argparse
import json
import os
import sys
import time

# 批① 起被搬件按绝对包名 import，故把仓根一并放进搜索路径（本目录仍在最前）。
for _hacs_p in (os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                os.path.dirname(os.path.abspath(__file__))):
    sys.path.insert(0, _hacs_p)

from node.l1_transport.nats_client import NatsConn, NatsError, read_secret_file  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="标准库 NATS 探针")
    ap.add_argument("--host", required=True)
    ap.add_argument("--port", type=int, default=9930)
    ap.add_argument("--user", required=True)
    ap.add_argument("--password-file", required=True)
    ap.add_argument("--whoami", default="probe")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    password = read_secret_file(args.password_file)

    results = []

    def check(name, ok, detail=""):
        results.append({"name": name, "ok": bool(ok), "detail": detail})

    # 1) 往返
    c1 = None
    try:
        c1 = NatsConn(args.host, args.port, args.user, password, name="%s-probe" % args.whoami)
        got = []
        c1.subscribe("team.probe.%s" % args.whoami, lambda p: got.append(p), sid="9")
        time.sleep(0.2)
        c1.publish("team.probe.%s" % args.whoami, "hello-中文-%d" % int(time.time()))
        deadline = time.time() + 4
        while time.time() < deadline and not got:
            time.sleep(0.02)
        check("PUB/SUB 往返（含中文）", bool(got) and got[0].decode("utf-8").startswith("hello-中文"),
              repr(got[0][:40]) if got else "无消息")
    except Exception as exc:
        check("PUB/SUB 往返（含中文）", False, str(exc)[:120])

    # 2) request/reply：先挂一个应答者（另开连接，模拟 teamd）
    responder = None
    try:
        responder = NatsConn(args.host, args.port, args.user, password, name="%s-rr" % args.whoami)

        def on_req(payload, reply):
            responder.publish(reply, ("应答:%s" % payload.decode("utf-8", "replace")))

        responder.subscribe("a2a.v1.req.%s" % args.whoami,
                            lambda p, reply: responder.publish(reply, "应答:%s" % p.decode("utf-8", "replace")),
                            sid="7")
        time.sleep(0.2)
        got_rr = c1.request("a2a.v1.req.%s" % args.whoami, "点名测试-中文", timeout=6.0)
        errs = " | ".join(x for x in (getattr(c1, "last_err", None),
                                     getattr(responder, "last_err", None)) if x)
        check("点名 request/reply（a2a.v1.req.<whoami>）",
              bool(got_rr) and got_rr.decode("utf-8").startswith("应答:点名测试-中文"),
              (repr(got_rr[:40]) if got_rr else "无应答") + (("  服务端报错: " + errs) if errs else ""))
    except Exception as exc:
        check("点名 request/reply（a2a.v1.req.<whoami>）", False, str(exc)[:120])

    # 3) 负例：错误口令必须被拒
    bad_ok = False
    try:
        bad = NatsConn(args.host, args.port, args.user, "definitely-wrong-password", name="bad")
        bad.close()
    except Exception:
        bad_ok = True
    check("错误口令被拒（负例）", bad_ok, "连接被拒 ✓" if bad_ok else "★ 错误口令竟然连上了")

    if c1:
        c1.close()
    if responder:
        responder.close()

    passed = sum(1 for r in results if r["ok"])
    if args.json:
        print(json.dumps({"results": results, "passed": passed, "total": len(results)},
                         ensure_ascii=False, indent=2))
    else:
        for r in results:
            print("[%s] %s %s" % ("PASS" if r["ok"] else "FAIL", r["name"],
                                  ("-- " + r["detail"]) if r["detail"] else ""))
        print("合计 %d 项，通过 %d 项" % (len(results), passed))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
