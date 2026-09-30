#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
nats_client.py —— 纯标准库 NATS 客户端（零 pip 依赖，语法兼容 Python 3.9）

从 nats_probe.py 抽出，给 nats_probe.py 与 teamd.py 共用。只实现 TEXT 协议里
团队平面用得到的那部分：INFO / CONNECT / PING / PONG / PUB / SUB / UNSUB / MSG / -ERR。

两个已经踩过的坑（必须保留）：
  1. `MSG <subject> <sid> [reply] <size>` —— 带 reply 时字段会错位，size 永远在**最后**一个字段，
     必须按字段个数（4 或 5）分别取，不能写死 parts[3]。
  2. `-ERR` 必须落到 `last_err` 并回调给使用者。服务端的权限违规（Permissions Violation）
     就是靠 -ERR 报的，静默吞掉会让「消息发不出去」变成查不出来的哑故障。

对外 API：
    c = NatsClient(host, port, user, password, name="teamd-<MEMBER_ID>", on_error=cb)
    c.connect()                                  # 建连 + 鉴权握手（失败抛 NatsError）
    c.subscribe("a2a.v1.req.<MEMBER_ID>", cb, sid="1")  # cb(payload: bytes, reply: str)
    c.publish("team.hb.<MEMBER_ID>", b"...", reply="")
    c.request("a2a.v1.req.x", b"...", timeout=6.0)  # -> bytes 或 None
    c.flush(timeout=5.0)                          # 发 PING 等 PONG，探活用
    c.is_connected                                # 属性，断线会变 False
    c.last_err                                    # 最近一条 -ERR 原文
    c.close()

`NatsConn` 是兼容别名：构造即连接（nats_probe.py 的老用法）。

多地址故障转移（2026-09-20 新增，向后兼容）：
    `host` 除了原来的单个字符串，还可以传逗号分隔字符串或 list/tuple；
    也可以改用 `hosts=` 关键字参数，语义相同，二者二选一。
    每个地址可以是 "host" 或 "host:port"（不带端口时用 `port=` 兜底）。
    `connect()` 按给定顺序逐个尝试**完整握手**（TCP 连接 + INFO + CONNECT + 等 PONG），
    只要有一个地址握手成功就返回；全部失败则抛 `NatsError`，错误信息里按顺序列出
    每个地址各自的失败原因，方便一眼看出是"网络不通"还是"鉴权失败"。
    连接成功后 `self.host` / `self.port` 会更新为**实际连上的那个地址**（其余代码/日志
    读这两个属性时无需关心走的是主还是备）。
"""

import inspect
import json
import os
import socket
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple  # noqa: F401  (3.9 兼容注解)

CRLF = b"\r\n"
DEFAULT_PORT = 9930
CLIENT_VERSION = "nats_client-1.0"

# 进程级自增序号：收件箱必须**进程内唯一**。
# 踩过的坑(2026-09-19)：原先收件箱 = `_INBOX.<pid>.<毫秒%1e6>.<实例内自增>`，
# 当同进程**并发**用多个连接做 request 时（如群发扇出），每个连接实例的自增都从 1 开始，
# 且毫秒相同 → 收件箱字符串**撞车**。NATS 对同一主题的多个订阅者会各自投递，
# 于是「A 的回应」会被 B、C 也收到 → 表现成「所有成员回复内容一模一样」的幽灵应答。
_INBOX_LOCK = threading.Lock()
_INBOX_SEQ = [0]


def _next_inbox():
    # type: () -> str
    with _INBOX_LOCK:
        _INBOX_SEQ[0] += 1
        seq = _INBOX_SEQ[0]
    return "_INBOX.%d.%d.%d.%s" % (
        os.getpid(), seq, threading.get_ident() % 100000, uuid.uuid4().hex[:12])


class NatsError(Exception):
    """连接 / 握手 / 协议层面的错误。"""
    pass


def _parse_addrs(host, hosts, default_port):
    # type: (Any, Any, int) -> List[Tuple[str, int]]
    """把 host/hosts 统一解析成 [(host, port), ...] 有序候选列表（即故障转移尝试顺序）。

    接受：单个字符串（"h"/"h:p"）、逗号分隔字符串（"h1:p1,h2:p2"）、
    list/tuple（元素可以是字符串，也可以是 (host, port) 二元组）。
    """
    raw = hosts if hosts is not None else host
    if raw is None:
        raise NatsError("必须提供 host 或 hosts")
    items = list(raw) if isinstance(raw, (list, tuple)) else str(raw).split(",")
    out = []  # type: List[Tuple[str, int]]
    for item in items:
        if isinstance(item, (list, tuple)):
            if not item:
                continue
            h = str(item[0]).strip()
            p = int(item[1]) if len(item) > 1 and item[1] else default_port
        else:
            s = str(item).strip()
            if not s:
                continue
            if ":" in s:
                h, _, p_str = s.rpartition(":")
                h = h.strip()
                try:
                    p = int(p_str)
                except ValueError:
                    h, p = s, default_port
            else:
                h, p = s, default_port
        if h:
            out.append((h, int(p)))
    if not out:
        raise NatsError("host/hosts 解析结果为空")
    return out


class NatsClient(object):
    """极简 NATS 客户端。一个连接 = 一个后台 reader 线程。"""

    def __init__(self, host=None, port=DEFAULT_PORT, user=None, password=None,
                 name="nats-client", timeout=8.0, on_error=None, on_disconnect=None,
                 hosts=None):
        # type: (Any, int, Optional[str], Optional[str], str, float, Optional[Callable], Optional[Callable], Any) -> None
        self._addrs = _parse_addrs(host, hosts, int(port))
        # 向后兼容属性：初始 = 候选列表里的第一个；connect() 成功后会改写成实际连上的那个。
        self.host = self._addrs[0][0]
        self.port = self._addrs[0][1]
        self.addrs = list(self._addrs)  # 只读式暴露完整候选列表，供调用方自检/打日志
        self.user = user
        self.password = password
        self.name = name
        self.timeout = float(timeout)
        self.on_error = on_error            # cb(err_line: str)
        self.on_disconnect = on_disconnect  # cb(reason: str)

        self.sock = None            # type: Any
        self.buf = b""
        self.pongs = 0
        self.last_err = None        # type: Optional[str]
        self.server_info = {}       # type: Dict[str, Any]

        self.subs = {}              # type: Dict[str, List[Callable]]  sid -> 回调列表
        self.sub_subjects = {}      # type: Dict[str, str]             sid -> subject（重连后重订阅）
        self.lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._running = False
        self._connected = False
        self._thread = None         # type: Any
        self._inbox_seq = 0

    # ------------------------------------------------------------------ 状态
    @property
    def is_connected(self):
        # type: () -> bool
        return bool(self._connected and self._running and self.sock is not None)

    # ------------------------------------------------------------------ 建连
    def connect(self):
        # type: () -> NatsClient
        """建连 + CONNECT 鉴权握手。

        多地址故障转移：按 self._addrs 顺序逐个尝试**完整握手**，任一成功即返回；
        全部失败抛 NatsError，异常信息里按顺序列出每个地址各自的失败原因。
        成功后 self.host/self.port 会更新为实际连上的地址，并自动重订阅历史 sid。
        """
        errors = []  # type: List[str]
        for h, p in self._addrs:
            try:
                self._connect_one(h, p)
            except Exception as exc:
                errors.append("%s:%d —— %s" % (h, p, exc))
                self.close()
                continue
            self.host, self.port = h, p
            self._resend_subs()
            return self
        raise NatsError("连不上任何 NATS 地址（已尝试 %d 个）：%s"
                        % (len(self._addrs), "；".join(errors)))

    handshake = connect  # 语义别名

    def _connect_one(self, host, port):
        # type: (str, int) -> None
        """对单个地址做一次完整握手：TCP 连接 + 读 INFO + 发 CONNECT + 等 PONG。

        任何一步失败都抛 NatsError，由 connect() 负责清理现场再尝试下一个地址。
        """
        self._close_socket()
        self.buf = b""
        self.pongs = 0
        self.last_err = None
        try:
            sock = socket.create_connection((host, port), timeout=self.timeout)
        except Exception as exc:
            raise NatsError("连不上 %s:%d —— %s" % (host, port, exc))
        sock.settimeout(self.timeout)
        self.sock = sock
        self._running = True
        self._connected = True

        try:
            info = self._read_line()
        except Exception as exc:
            raise NatsError("读 INFO 失败: %s" % exc)
        if not info.startswith(b"INFO "):
            raise NatsError("服务端未先发 INFO: %r" % info[:80])
        try:
            self.server_info = json.loads(info[5:].decode("utf-8", "replace"))
        except Exception:
            self.server_info = {}

        connect = {"verbose": False, "pedantic": False, "name": self.name,
                   "lang": "py", "version": CLIENT_VERSION, "protocol": 1}
        if self.user:
            connect["user"] = self.user
            connect["pass"] = self.password or ""
        try:
            self._send(b"CONNECT " + json.dumps(connect).encode("utf-8") + CRLF)
            self._send(b"PING" + CRLF)
        except Exception as exc:
            raise NatsError("发 CONNECT 失败: %s" % exc)

        # 握手完成后转为阻塞读：reader 线程不该因为「一段时间没消息」而超时退出。
        try:
            sock.settimeout(None)
        except Exception:
            pass
        self._thread = threading.Thread(target=self._reader, name="nats-reader")
        self._thread.daemon = True
        self._thread.start()

        if not self._wait_pong(max(3.0, self.timeout)):
            err = self.last_err or "无 -ERR，可能是网络不通"
            raise NatsError("CONNECT 后未收到 PONG（鉴权失败或网络不通）：%s" % err)

    # ------------------------------------------------------------------ 底层 I/O
    def _send(self, data):
        # type: (bytes) -> None
        sock = self.sock
        if sock is None:
            raise NatsError("连接未建立")
        with self._send_lock:
            sock.sendall(data)

    def _read_line(self):
        # type: () -> bytes
        while CRLF not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise NatsError("连接被关闭")
            self.buf += chunk
        line, self.buf = self.buf.split(CRLF, 1)
        return line

    def _read_bytes(self, n):
        # type: (int) -> bytes
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise NatsError("连接被关闭")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _wait_pong(self, seconds):
        # type: (float) -> bool
        deadline = time.time() + seconds
        while time.time() < deadline:
            if self.pongs > 0:
                return True
            if not self._connected:
                return False
            time.sleep(0.02)
        return self.pongs > 0

    # ------------------------------------------------------------------ reader
    def _reader(self):
        # type: () -> None
        reason = "正常关闭"
        try:
            while self._running:
                line = self._read_line()
                if line.startswith(b"MSG "):
                    self._handle_msg(line)
                elif line.startswith(b"PING"):
                    self._send(b"PONG" + CRLF)
                elif line.startswith(b"PONG"):
                    self.pongs += 1
                elif line.startswith(b"-ERR"):
                    self._handle_err(line)
                elif line.startswith(b"INFO "):
                    try:
                        self.server_info = json.loads(line[5:].decode("utf-8", "replace"))
                    except Exception:
                        pass
                # +OK 及其他行忽略
        except Exception as exc:
            reason = str(exc) or exc.__class__.__name__
        finally:
            self._connected = False
            if self.on_disconnect is not None:
                try:
                    self.on_disconnect(reason)
                except Exception:
                    pass

    def _handle_msg(self, line):
        # type: (bytes) -> None
        """MSG <subject> <sid> [reply] <size> —— size 永远在最后，按字段个数取。"""
        parts = line.split()
        if len(parts) < 4:
            return
        sid = parts[2].decode("utf-8", "replace")
        if len(parts) >= 5:
            reply = parts[3].decode("utf-8", "replace")
            size = int(parts[4])
        else:
            reply = ""
            size = int(parts[3])
        payload = self._read_bytes(size + 2)[:-2]
        with self.lock:
            callbacks = list(self.subs.get(sid, []))
        for cb in callbacks:
            try:
                self._invoke(cb, payload, reply)
            except Exception:
                pass  # 回调自己的异常不许打死 reader 线程

    def _handle_err(self, line):
        # type: (bytes) -> None
        text = line.decode("utf-8", "replace")
        self.last_err = text
        with self.lock:
            callbacks = list(self.subs.get("__err__", []))
        for cb in callbacks:
            try:
                cb(line)
            except Exception:
                pass
        if self.on_error is not None:
            try:
                self.on_error(text)
            except Exception:
                pass

    @staticmethod
    def _invoke(cb, payload, reply):
        # type: (Callable, bytes, str) -> None
        """按回调签名的参数个数调用：老代码里有只收 payload 的单参回调。"""
        nargs = 2
        try:
            nargs = len(inspect.signature(cb).parameters)
        except (TypeError, ValueError):
            nargs = 2
        if nargs <= 1:
            cb(payload)
        else:
            cb(payload, reply)

    # ------------------------------------------------------------------ 语义层
    def subscribe(self, subject, cb, sid="1"):
        # type: (str, Callable, str) -> NatsClient
        with self.lock:
            self.subs.setdefault(sid, []).append(cb)
            self.sub_subjects[sid] = subject
        self._send(b"SUB " + subject.encode("utf-8") + b" " + sid.encode("utf-8") + CRLF)
        return self

    def unsubscribe(self, sid):
        # type: (str) -> NatsClient
        with self.lock:
            self.subs.pop(sid, None)
            self.sub_subjects.pop(sid, None)
        try:
            self._send(b"UNSUB " + sid.encode("utf-8") + CRLF)
        except Exception:
            pass
        return self

    def _resend_subs(self):
        # type: () -> None
        """重连后把历史订阅重新发一遍（同一个对象复用时用得上）。"""
        with self.lock:
            items = list(self.sub_subjects.items())
        for sid, subject in items:
            if sid.startswith("_INBOX."):
                continue  # 一次性收件箱不必重建
            try:
                self._send(b"SUB " + subject.encode("utf-8") + b" " + sid.encode("utf-8") + CRLF)
            except Exception:
                return

    def publish(self, subject, payload=b"", reply=""):
        # type: (str, Any, str) -> NatsClient
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        head = (b"PUB " + subject.encode("utf-8") + b" "
                + (reply.encode("utf-8") + b" " if reply else b"")
                + str(len(payload)).encode("ascii") + CRLF)
        self._send(head + payload + CRLF)
        return self

    def request(self, subject, payload, timeout=6.0):
        # type: (str, Any, float) -> Optional[bytes]
        """request/reply：临时收件箱等一条回复；超时返回 None。"""
        self._inbox_seq += 1
        inbox = _next_inbox()
        box = []  # type: List[bytes]
        self.subscribe(inbox, lambda p, r="": box.append(p), sid=inbox)
        try:
            self.publish(subject, payload, reply=inbox)
            deadline = time.time() + timeout
            while time.time() < deadline and not box:
                if not self._connected:
                    break
                time.sleep(0.02)
        finally:
            self.unsubscribe(inbox)
        return box[0] if box else None

    def flush(self, timeout=5.0):
        # type: (float) -> bool
        """发 PING 等 PONG。返回 False 说明链路已经不通了（探活用）。"""
        if not self.is_connected:
            return False
        base = self.pongs
        try:
            self._send(b"PING" + CRLF)
        except Exception as exc:
            self.last_err = "flush 发送失败: %s" % exc
            self._connected = False
            return False
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.pongs > base:
                return True
            if not self._connected:
                return False
            time.sleep(0.02)
        return False

    # ------------------------------------------------------------------ 关闭
    def _close_socket(self):
        # type: () -> None
        sock, self.sock = self.sock, None
        if sock is None:
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            sock.close()
        except Exception:
            pass

    def close(self):
        # type: () -> None
        self._running = False
        self._connected = False
        self._close_socket()


class NatsConn(NatsClient):
    """兼容别名：构造即连接（nats_probe.py 的老用法）。"""

    def __init__(self, host=None, port=DEFAULT_PORT, user=None, password=None,
                 name="probe", timeout=8.0, on_error=None, on_disconnect=None,
                 hosts=None):
        NatsClient.__init__(self, host, port, user, password, name=name, timeout=timeout,
                            on_error=on_error, on_disconnect=on_disconnect, hosts=hosts)
        self.connect()


def read_secret_file(path):
    # type: (str) -> str
    """读口令/令牌文件（去掉首尾空白与 BOM）。找不到给人话报错。"""
    full = os.path.expanduser(path)
    if not os.path.isfile(full):
        raise NatsError("文件不存在: %s" % full)
    with open(full, "r", encoding="utf-8-sig") as f:
        return f.read().strip()
