#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""管理口迁移的**字节级等价证据**（可复跑）。

用法：
    python tools/equivalence/admin_port_equiv.py --ref <源码仓迁移前的提交>

      --ref  **源码仓**（本仓，非对外仓）迁移前的提交；该版本作「旧实现」，
             工作树作「新实现」。默认 HEAD = 迁移尚未提交时正好是旧实现。
             比对项 = 状态行 + 响应头(去 Date) + 正文，逐字节。

为什么留着它：抽件（把传输职责收进 L1 web_gateway）必须证明**零行为改变**。
迁移提交之后，本脚本仍可指向迁移前那个 commit 复跑，证据链永久成立。
"""
import argparse
import http.client
import os
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
#: 保留注入：OLD 不是包目录 —— `--ref` 落盘的 `teamd_old.py` 只能按裸名装入；
#: REPO 供新侧绝对包名（`node.l1_transport.teamd`）解析。
OLD = os.path.join(HERE, "_old")


def materialize_old(ref):
    if not os.path.isdir(OLD):
        os.makedirs(OLD)
    target = os.path.join(OLD, "teamd_old.py")
    raw = subprocess.run(["git", "show", "%s:agent-team/teamd.py" % ref],
                         cwd=REPO, capture_output=True).stdout
    if not raw:
        print("取不到 %s:agent-team/teamd.py —— ref 写错了？" % ref)
        sys.exit(2)
    if b"class AdminHandler" not in raw:
        print("警告：%s 的 teamd.py 里已经没有 AdminHandler，那它就不是「旧实现」" % ref)
    open(target, "wb").write(raw)
    return target


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", default="HEAD", help="迁移前的 commit/ref")
    args = ap.parse_args()
    materialize_old(args.ref)
    sys.path[:0] = [OLD, REPO]

    import teamd_old                                            # noqa: E402
    from node.l1_transport import teamd                                                # noqa: E402

    class FakeDaemon(object):
        """只替掉 daemon 的两个只读方法；路由/鉴权/信封全走真实实现。"""
        def healthz(self):
            return {"ok": True, "connected": True, "member": "probe", "queue": 0}

        def varz(self):
            return {"ok": True, "uptime_s": 12, "counters": {"req": 3, "err": 0}}

    def start(mod, daemon, port):
        srv = mod.make_admin_server(daemon, "127.0.0.1", port)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        time.sleep(0.25)
        return srv

    def call(port, method, path):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            conn.request(method, path)
            resp = conn.getresponse()
            body = resp.read()
            return (resp.status, resp.reason,
                    sorted((k.lower(), v) for k, v in resp.getheaders() if k.lower() != "date"),
                    body)
        except Exception as exc:                                # 连接被关也当一种结果比对
            return ("EXC", type(exc).__name__, [], b"")
        finally:
            conn.close()

    cases = [("GET", "/healthz"), ("GET", "/varz"), ("GET", "/nope"), ("GET", "/healthz?x=1"),
             ("GET", "/healthz/"), ("GET", "/"), ("POST", "/healthz"), ("DELETE", "/varz"),
             ("HEAD", "/healthz")]
    notready = [("GET", "/healthz"), ("GET", "/nope")]

    bad = 0
    pair = []
    print("%-8s %-18s %s" % ("方法", "路径", "结论"))
    for daemon, port_old, port_new, group, label in (
            (FakeDaemon(), 19961, 19962, cases, ""),
            (None, 19963, 19964, notready, "（未就绪 503）")):
        s_old = start(teamd_old, daemon, port_old)
        s_new = start(teamd, daemon, port_new)
        for method, path in group:
            a, b = call(port_old, method, path), call(port_new, method, path)
            ok = a == b
            bad += 0 if ok else 1
            print("%-8s %-18s %s" % (method, path, ("一致" + label) if ok else
                                     "**不一致**\n    旧=%r\n    新=%r" % (a, b)))
        s_old.shutdown()
        s_new.shutdown()
    print("\n合计 %d 组，不一致 %d 组" % (len(cases) + len(notready), bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
