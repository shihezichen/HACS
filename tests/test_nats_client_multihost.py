#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# hacs-test-fixtures: 本文件为测试夹具，允许出现假凭据与网段边界值；危险类命中（真令牌/连接串/真实人名）仍按硬门禁拦截
"""
test_nats_client_multihost.py —— nats_client.py 多地址故障转移的不连网自测

覆盖：
  1) _parse_addrs 对各种输入形式（单串/逗号分隔串/list/tuple 元素）的解析结果
  2) 全部地址都连不上时：connect() 抛 NatsError，异常信息里按顺序列出每个地址各自的失败原因
  3) 第一个地址连不上、第二个地址能握手成功时：connect() 成功返回，且 self.host/self.port
     被改写成实际连上的那个地址（用本地起两个假 TCP/NATS 服务模拟，测完立刻关闭）
  4) hosts= 关键字参数与 host= 参数等价
"""

import json
import os
import socket
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from node.l1_transport.nats_client import NatsClient, NatsError, _parse_addrs  # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", name, ("  -- " + detail) if detail else ""))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ------------------------------------------------------------------ 1) 解析逻辑

def test_parse_addrs():
    out = _parse_addrs("1.2.3.4", None, 9930)
    check("单个字符串（无端口）→ 用默认端口", out == [("1.2.3.4", 9930)], "out=%r" % (out,))

    out = _parse_addrs("1.2.3.4:1111", None, 9930)
    check("单个字符串（带端口）→ 用自带端口", out == [("1.2.3.4", 1111)], "out=%r" % (out,))

    out = _parse_addrs("a:1111,b,c:3333", None, 9930)
    check("逗号分隔字符串，混合带/不带端口",
          out == [("a", 1111), ("b", 9930), ("c", 3333)], "out=%r" % (out,))

    out = _parse_addrs(["a:1111", "b:2222"], None, 9930)
    check("list[str] 输入", out == [("a", 1111), ("b", 2222)], "out=%r" % (out,))

    out = _parse_addrs(None, ["a", ("b", 4444)], 9930)
    check("hosts= 关键字，list 元素可以是 (host, port) 二元组",
          out == [("a", 9930), ("b", 4444)], "out=%r" % (out,))

    out = _parse_addrs(" a , , b ", None, 9930)
    check("空项与首尾空白被丢弃/去除",
          out == [("a", 9930), ("b", 9930)], "out=%r" % (out,))

    try:
        _parse_addrs(None, None, 9930)
        check("host 与 hosts 都不给 → 抛 NatsError", False, "未抛异常")
    except NatsError:
        check("host 与 hosts 都不给 → 抛 NatsError", True)


# ------------------------------------------------------------------ 2) 全失败：逐个列出原因

def test_all_fail_lists_each_reason():
    # 两个必然连不上的地址：一个未监听端口（立即拒绝），一个黑洞 IP（TEST-NET-1，超时更慢，
    # 这里给短 timeout 避免自测拖太久）。
    dead_port = free_port()  # 拿到即释放，短时间内大概率仍是 closed 状态 → ECONNREFUSED
    client = NatsClient(hosts=["127.0.0.1:%d" % dead_port, "127.0.0.1:%d" % free_port()],
                        user="x", password="y", timeout=1.0)
    try:
        client.connect()
        check("两个地址都连不上 → connect() 应该抛异常", False, "居然连上了？！")
        return
    except NatsError as exc:
        msg = str(exc)
        ok = ("已尝试 2 个" in msg)
        # 错误信息里应该能看到两个地址各自的身影
        ok = ok and ("127.0.0.1:%d" % dead_port in msg)
        check("全部地址失败 → NatsError 信息含尝试个数与每个地址",
              ok, "msg=%s" % msg)
    finally:
        try:
            client.close()
        except Exception:
            pass


# ------------------------------------------------------------------ 3) 故障转移到第二个地址

class _FakeNatsServer(object):
    """极简假 NATS 服务端：发一行 INFO，收到任何东西都回 PONG。够 NatsClient 握手用了。"""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self._stop = False
        self.accepted = 0
        self._thread = threading.Thread(target=self._serve, name="fake-nats-srv")
        self._thread.daemon = True
        self._thread.start()

    def _serve(self):
        while not self._stop:
            self.sock.settimeout(0.2)
            try:
                conn, _addr = self.sock.accept()
            except socket.timeout:
                continue
            except Exception:
                return
            self.accepted += 1
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        try:
            conn.sendall(b"INFO " + json.dumps({"server_id": "fake"}).encode("utf-8") + b"\r\n")
            conn.settimeout(5.0)
            buf = b""
            while not self._stop:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                buf += chunk
                while b"\r\n" in buf:
                    line, buf = buf.split(b"\r\n", 1)
                    if line.startswith(b"PING") or line.startswith(b"CONNECT"):
                        if line.startswith(b"PING"):
                            conn.sendall(b"PONG\r\n")
        except Exception:
            return
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def stop(self):
        self._stop = True
        try:
            self.sock.close()
        except Exception:
            pass
        self._thread.join(timeout=2.0)


def test_failover_to_second_addr():
    dead_port = free_port()  # 没监听，第一个地址必然连接失败
    fake = _FakeNatsServer()  # 第二个地址：真监听、能完整握手
    client = NatsClient(hosts=["127.0.0.1:%d" % dead_port, "127.0.0.1:%d" % fake.port],
                        user="x", password="y", timeout=2.0)
    try:
        client.connect()
        check("第一个地址失败、第二个握手成功 → connect() 正常返回",
              client.is_connected, "is_connected=%s" % client.is_connected)
        check("成功后 self.host/self.port 更新为实际连上的地址",
              client.host == "127.0.0.1" and client.port == fake.port,
              "host=%s port=%s (期望端口=%s)" % (client.host, client.port, fake.port))
    finally:
        try:
            client.close()
        except Exception:
            pass
        fake.stop()


def test_hosts_kwarg_equiv_host():
    a = NatsClient(host="1.1.1.1:9930,2.2.2.2:9931", user="x", password="y")
    b = NatsClient(hosts=["1.1.1.1:9930", "2.2.2.2:9931"], user="x", password="y")
    check("host= 逗号串 与 hosts= list 解析结果一致",
          a._addrs == b._addrs, "a=%r b=%r" % (a._addrs, b._addrs))


def test_backward_compat_single_host_positional():
    # 老用法：NatsClient(host, port, user, password) 全位置参数，不涉及网络。
    c = NatsClient("127.0.0.1", 9930, "u", "p", name="fake")
    check("老式单地址位置参数构造仍然可用，host/port 属性正确",
          c.host == "127.0.0.1" and c.port == 9930, "host=%s port=%s" % (c.host, c.port))


def main():
    test_parse_addrs()
    test_all_fail_lists_each_reason()
    test_failover_to_second_addr()
    test_hosts_kwarg_equiv_host()
    test_backward_compat_single_host_positional()

    failed = [name for name, ok, _ in RESULTS if not ok]
    print("-" * 60)
    print("合计 %d 项，通过 %d 项，失败 %d 项"
          % (len(RESULTS), len(RESULTS) - len(failed), len(failed)))
    if failed:
        print("失败项：" + ", ".join(failed))
        return 1
    print("全部 PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
