#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# hacs-test-fixtures: 本文件为测试夹具，允许出现假凭据与网段边界值；危险类命中（真令牌/连接串/真实人名）仍按硬门禁拦截
"""
test_teamd.py —— teamd.py / nats_client.py 的纯标准库自测（直接 python tests/test_teamd.py 跑）

不碰真 NATS 服务器、不联网。手段：
  * 协议层：拿**假 socket** 喂字节流，验证 MSG 行解析（4 字段 / 5 字段）与 -ERR 落到 last_err
  * 翻译层：**注入假 NATS 传输** + 本地 stub A2A HTTP 服务（校验路径/Authorization/请求体）
  * 管理口：真起 127.0.0.1 端口，urllib 拉 /healthz 与 /varz，看计数器是否在涨
"""

import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from node.l1_transport.nats_client import NatsClient  # noqa: E402
from teamd import Config, TeamDaemon  # noqa: E402

TOKEN = "teamd-unit-test-token-xyz"
STUB_REPLY = "你好，我是 stub a2a_svc 的固定回复。"

RESULTS = []
A2A_CALLS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", name, ("  -- " + detail) if detail else ""))


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


# ------------------------------------------------------------------ 假 socket

class FakeSock(object):
    """按 chunk 吐字节流；吐完返回 b"" 让 reader 自然收尾。"""

    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.sent = []
        self.closed = False

    def recv(self, _n=65536):
        if self.chunks:
            return self.chunks.pop(0)
        return b""

    def sendall(self, data):
        self.sent.append(data)

    def settimeout(self, _t):
        pass

    def shutdown(self, _how):
        pass

    def close(self):
        self.closed = True


def drive_reader(chunks, subs=None, on_error=None):
    """造一个不联网的 NatsClient，用假 socket 跑一遍 reader 主循环。"""
    client = NatsClient("127.0.0.1", 9930, name="fake")
    client.on_error = on_error
    client.sock = FakeSock(chunks)
    client._running = True
    client._connected = True
    for subject, cb, sid in (subs or []):
        client.subscribe(subject, cb, sid=sid)
    client._reader()
    return client


# ------------------------------------------------------------------ 假 NATS 传输

class FakeNats(object):
    """teamd 注入用的假传输：只记录，不联网。"""

    def __init__(self):
        self.subs = {}
        self.published = []      # [(subject, payload_bytes, reply)]
        self.is_connected = True
        self.last_err = None
        self.closed = False
        self.flushes = 0

    def connect(self):
        self.is_connected = True
        return self

    def subscribe(self, subject, cb, sid="1"):
        self.subs[subject] = cb
        return self

    def publish(self, subject, payload=b"", reply=""):
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        self.published.append((subject, payload, reply))
        return self

    def flush(self, timeout=5.0):
        self.flushes += 1
        return self.is_connected

    def close(self):
        self.closed = True
        self.is_connected = False

    # -- 测试辅助
    def deliver(self, subject, payload, reply=""):
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        self.subs[subject](payload, reply)

    def wait_publish(self, subject, seconds=10.0):
        deadline = time.time() + seconds
        while time.time() < deadline:
            for item in self.published:
                if item[0] == subject:
                    return item
            time.sleep(0.02)
        return None


# ------------------------------------------------------------------ stub a2a_svc

class StubA2AHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            payload = {}
        A2A_CALLS.append({
            "path": self.path,
            "auth": self.headers.get("Authorization"),
            "ctype": self.headers.get("Content-Type"),
            "payload": payload,
        })
        text = str(payload.get("text") or "")
        if text.startswith("BOOM"):
            body = json.dumps({"ok": False, "error": "backend_error",
                               "detail": "stub 故意 500"}, ensure_ascii=False).encode("utf-8")
            self._send(500, body)
            return
        if text.startswith("SLOW"):
            time.sleep(2.0)  # 比 teamd 的 HTTP 读超时长，逼出 timeout 分支
        body = json.dumps({"ok": True, "agent": "hermes", "reply": STUB_REPLY,
                           "elapsed_ms": 12, "peer": {"node": "stubnode"}},
                          ensure_ascii=False).encode("utf-8")
        self._send(200, body)

    def _send(self, status, body):
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            pass


class QuietHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        return  # 超时用例会让客户端先掉线，别刷 traceback


# ------------------------------------------------------------------ HTTP 客户端

def http_get(url, timeout=5):
    req = urllib.request.Request(url, method="GET")
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        status, raw = resp.getcode(), resp.read()
        resp.close()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    text = raw.decode("utf-8", "replace")
    try:
        return status, json.loads(text), text
    except Exception:
        return status, None, text


# ------------------------------------------------------------------ 用例

def test_msg_parsing():
    """1) MSG 行解析：4 字段（无 reply）与 5 字段（有 reply，size 在最后）。"""
    got = []
    stream = (b"MSG team.probe.h 1 5\r\nhello\r\n"
              b"MSG a2a.v1.req.h 2 _INBOX.42 6\r\n\xe4\xb8\xad\xe6\x96\x87\r\n")
    client = drive_reader(
        [stream],
        subs=[("team.probe.h", lambda p, r: got.append((p, r)), "1"),
              ("a2a.v1.req.h", lambda p, r: got.append((p, r)), "2")])
    check("MSG 4 字段（无 reply）解析正确",
          len(got) >= 1 and got[0] == (b"hello", ""),
          "got=%r" % (got[:1],))
    check("MSG 5 字段（有 reply，size 在最后）解析正确",
          len(got) == 2 and got[1] == ("中文".encode("utf-8"), "_INBOX.42"),
          "got=%r" % (got[1:],))
    check("字节流读完后 is_connected 变 False（断线可感知）",
          client.is_connected is False, "is_connected=%s" % client.is_connected)

    # 分片喂：MSG 头与 payload 跨 recv 边界也要拼得回来
    got2 = []
    drive_reader([b"MSG a.b 1 _RE", b"PLY.1 4\r\nab", b"cd\r\n"],
                 subs=[("a.b", lambda p, r: got2.append((p, r)), "1")])
    check("MSG 跨 recv 分片也能正确拼装",
          got2 == [(b"abcd", "_REPLY.1")], "got=%r" % (got2,))


def test_err_path():
    """5) -ERR 必须落到 last_err 且回调出去（权限违规不能被静默吞掉）。"""
    seen = []
    line = (b"-ERR 'Permissions Violation for Publication to \"a2a.v1.req.hermes\"'\r\n")
    client = drive_reader([line], on_error=lambda text: seen.append(text))
    ok = bool(client.last_err) and "Permissions Violation" in (client.last_err or "")
    check("-ERR 记入 last_err", ok, "last_err=%r" % (client.last_err,))
    check("-ERR 回调给使用者（on_error）",
          len(seen) == 1 and "Permissions Violation" in seen[0],
          "callbacks=%d" % len(seen))


def make_daemon(stub_port, **kw):
    cfg = Config(member="hermes", nats_host="127.0.0.1", nats_port=9930,
                 user="hermes", password="nats-secret-pw",
                 a2a_base="http://127.0.0.1:%d" % stub_port, token=TOKEN,
                 admin_host="127.0.0.1", admin_port=kw.pop("admin_port", 0),
                 enable_admin=kw.pop("enable_admin", False),
                 hb_interval=kw.pop("hb_interval", 3600.0),
                 ping_interval=kw.pop("ping_interval", 3600.0),
                 a2a_timeout_margin=kw.pop("a2a_timeout_margin", 0.3))
    fake = FakeNats()
    daemon = TeamDaemon(cfg, nats_factory=lambda: fake)
    daemon._connect_once()
    return daemon, fake


def test_translation(stub_port):
    """2) NATS 消息 → A2A 请求 → 回包（走订阅回调，验证整条线是通的）。"""
    del A2A_CALLS[:]
    daemon, fake = make_daemon(stub_port)
    check("订阅了 a2a.v1.req.<member>",
          "a2a.v1.req.hermes" in fake.subs, "subs=%s" % sorted(fake.subs.keys()))

    req = {"text": "请给我今天的进度", "session_id": "s-teamd-1",
           "timeout_s": 10, "from": "hyperion"}
    fake.deliver("a2a.v1.req.hermes",
                 json.dumps(req, ensure_ascii=False).encode("utf-8"), "_INBOX.T1")
    item = fake.wait_publish("_INBOX.T1", 10.0)
    body = None
    if item is not None:
        try:
            body = json.loads(item[1].decode("utf-8"))
        except Exception:
            body = None
    check("A2A 响应原样回到 reply 主题",
          item is not None and isinstance(body, dict) and body.get("ok") is True
          and body.get("reply") == STUB_REPLY and body.get("agent") == "hermes"
          and body.get("elapsed_ms") == 12,
          "published=%r" % (item[1][:120] if item else None,))

    call = A2A_CALLS[-1] if A2A_CALLS else {}
    payload = call.get("payload") or {}
    check("转发给本机 A2A 的路径/鉴权头/Content-Type 正确",
          call.get("path") == "/a2a/hermes/messages"
          and call.get("auth") == "Bearer " + TOKEN
          and "charset=utf-8" in (call.get("ctype") or ""),
          "path=%s auth=%s ctype=%s" % (call.get("path"), call.get("auth"), call.get("ctype")))
    check("转发的请求体带 text/session_id/timeout_s/from",
          payload.get("text") == "请给我今天的进度"
          and payload.get("session_id") == "s-teamd-1"
          and abs(float(payload.get("timeout_s") or 0) - 10.0) < 1e-6
          and payload.get("from") == "hyperion",
          "payload=%s" % json.dumps(payload, ensure_ascii=False))
    counters = daemon.counters.snapshot()
    check("成功一次后 rx_requests=1、ok=1、errors=0",
          counters["rx_requests"] == 1 and counters["ok"] == 1 and counters["errors"] == 0,
          "counters=%s" % counters)

    # timeout_s 上限 300：传 9999 要被夹到 300
    del A2A_CALLS[:]
    daemon.handle_request(json.dumps({"text": "夹一下超时", "timeout_s": 9999}).encode("utf-8"), "")
    clamped = (A2A_CALLS[-1].get("payload") or {}).get("timeout_s") if A2A_CALLS else None
    check("timeout_s 被夹到上限 300（禁止死等）",
          clamped == 300.0, "timeout_s=%s" % clamped)

    # 不带 timeout_s → 默认 120
    del A2A_CALLS[:]
    daemon.handle_request(json.dumps({"text": "默认超时"}).encode("utf-8"), "")
    default_t = (A2A_CALLS[-1].get("payload") or {}).get("timeout_s") if A2A_CALLS else None
    check("不带 timeout_s → 默认 120", default_t == 120.0, "timeout_s=%s" % default_t)

    daemon.shutdown()


def test_error_branches(stub_port):
    """3) 非法 JSON / 缺 text / A2A 500 / A2A 超时 各回对应错误结构。"""
    daemon, fake = make_daemon(stub_port)

    def roundtrip(payload, reply):
        raw = daemon.handle_request(payload, reply)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return None

    body = roundtrip(b"{not json at all", "_INBOX.E1")
    check("非法 JSON → {ok:false, error:bad_request}",
          isinstance(body, dict) and body.get("ok") is False
          and body.get("error") == "bad_request" and bool(body.get("detail")),
          "body=%s" % json.dumps(body, ensure_ascii=False))

    body = roundtrip(json.dumps({"session_id": "s-1"}).encode("utf-8"), "_INBOX.E2")
    check("缺 text → {ok:false, error:bad_request}",
          isinstance(body, dict) and body.get("error") == "bad_request",
          "body=%s" % json.dumps(body, ensure_ascii=False))

    body = roundtrip(json.dumps({"text": "BOOM 请炸", "timeout_s": 5}).encode("utf-8"), "_INBOX.E3")
    check("A2A 返回 500 → {ok:false, error:http_error, status:500}",
          isinstance(body, dict) and body.get("ok") is False
          and body.get("error") == "http_error" and body.get("status") == 500
          and len(body.get("detail") or "") <= 201,
          "body=%s" % json.dumps(body, ensure_ascii=False))

    started = time.time()
    body = roundtrip(json.dumps({"text": "SLOW 慢死了", "timeout_s": 0.3}).encode("utf-8"),
                     "_INBOX.E4")
    elapsed = time.time() - started
    check("A2A 超时 → {ok:false, error:timeout} 且不死等",
          isinstance(body, dict) and body.get("error") == "timeout" and elapsed < 1.8,
          "body=%s elapsed=%.2fs" % (json.dumps(body, ensure_ascii=False), elapsed))

    published = [s for s, _p, _r in fake.published]
    check("四个错误分支都把回包 publish 到了各自的 reply 主题",
          published == ["_INBOX.E1", "_INBOX.E2", "_INBOX.E3", "_INBOX.E4"],
          "published=%s" % published)

    counters = daemon.counters.snapshot()
    check("错误计数：rx_requests=4 errors=4 timeouts=1 bad_requests=2 ok=0",
          counters["rx_requests"] == 4 and counters["errors"] == 4
          and counters["timeouts"] == 1 and counters["bad_requests"] == 2
          and counters["ok"] == 0,
          "counters=%s" % counters)
    daemon.shutdown()


def test_heartbeat(stub_port):
    """3') 心跳内容：member/host/ts/uptime_s/version 齐全，主题 team.hb.<member>。"""
    daemon, fake = make_daemon(stub_port)
    ok = daemon.send_heartbeat()
    item = fake.published[-1] if fake.published else None
    hb = None
    if item is not None:
        try:
            hb = json.loads(item[1].decode("utf-8"))
        except Exception:
            hb = None
    check("心跳发到 team.hb.<member> 且字段齐全",
          ok and item is not None and item[0] == "team.hb.hermes"
          and isinstance(hb, dict) and hb.get("member") == "hermes"
          and bool(hb.get("host")) and isinstance(hb.get("ts"), int)
          and isinstance(hb.get("uptime_s"), int) and bool(hb.get("version")),
          "subject=%s hb=%s" % (item[0] if item else None,
                                json.dumps(hb, ensure_ascii=False) if hb else None))
    check("hb_sent 计数与 last_hb_ts 都更新了",
          daemon.counters.snapshot()["hb_sent"] == 1 and isinstance(daemon.last_hb_ts, int),
          "hb_sent=%d last_hb_ts=%s" % (daemon.counters.snapshot()["hb_sent"],
                                        daemon.last_hb_ts))
    daemon.shutdown()


def test_admin(stub_port):
    """4) 管理口真起端口：/healthz 与 /varz 都 200，且计数器在涨、不漏口令。"""
    port = free_port()
    daemon, fake = make_daemon(stub_port, admin_port=port, enable_admin=True)
    daemon.start_admin()
    base = "http://127.0.0.1:%d" % port
    try:
        status, body, _text = http_get(base + "/healthz")
        need = ["rx_requests", "ok", "errors", "timeouts", "hb_sent", "reconnects"]
        counters = (body or {}).get("counters") or {}
        missing = [k for k in need if k not in counters]
        check("/healthz → 200 且 ok/member/nats/counters/ts 齐全",
              status == 200 and (body or {}).get("ok") is True
              and body.get("member") == "hermes"
              and isinstance(body.get("nats"), dict)
              and body["nats"].get("connected") is True
              and "last_err" in body["nats"]
              and not missing and isinstance(body.get("ts"), int),
              "status=%s missing=%s body=%s" % (status, missing,
                                                json.dumps(body, ensure_ascii=False)[:200]))
        before = counters.get("rx_requests", 0)

        daemon.handle_request(json.dumps({"text": "计数器走一个"}).encode("utf-8"), "_INBOX.A1")
        daemon.handle_request("{坏的".encode("utf-8"), "_INBOX.A2")

        status2, body2, _t2 = http_get(base + "/healthz")
        after = ((body2 or {}).get("counters") or {})
        check("请求之后 /healthz 计数器在涨（rx_requests +2、ok +1、errors +1）",
              status2 == 200 and after.get("rx_requests") == before + 2
              and after.get("ok") == 1 and after.get("errors") == 1,
              "before=%s after=%s" % (before, after))

        status3, varz, text3 = http_get(base + "/varz")
        cfg_summary = (varz or {}).get("config") or {}
        leaked = ("nats-secret-pw" in text3) or (TOKEN in text3)
        check("/varz → 200，含计数器与配置摘要，且**不含**口令/令牌",
              status3 == 200 and isinstance((varz or {}).get("counters"), dict)
              and cfg_summary.get("member") == "hermes"
              and (cfg_summary.get("nats") or {}).get("port") == 9930
              and (cfg_summary.get("subjects") or {}).get("request") == "a2a.v1.req.hermes"
              and not leaked,
              "status=%s leaked=%s keys=%s" % (status3, leaked, sorted((varz or {}).keys())))

        status4, body4, _t4 = http_get(base + "/nope")
        check("管理口未知路径 → 404", status4 == 404, "status=%s" % status4)

        # NATS 断线时 /healthz 仍返 200，但 connected=false
        fake.is_connected = False
        status5, body5, _t5 = http_get(base + "/healthz")
        check("NATS 断线时 /healthz 仍 200 但 connected=false",
              status5 == 200 and (body5 or {}).get("ok") is True
              and ((body5 or {}).get("nats") or {}).get("connected") is False,
              "status=%s nats=%s" % (status5, (body5 or {}).get("nats")))
    finally:
        daemon.shutdown()


def test_reconnect(stub_port):
    """1') 断线自动重连 + 重连后重新订阅 + reconnects 计数。"""
    created = []

    def factory():
        fake = FakeNats()
        created.append(fake)
        return fake

    cfg = Config(member="hermes", nats_host="127.0.0.1", nats_port=9930,
                 user="hermes", password="pw", a2a_base="http://127.0.0.1:%d" % stub_port,
                 token=TOKEN, admin_port=0, enable_admin=False,
                 hb_interval=3600.0, ping_interval=0.15, a2a_timeout_margin=0.3)
    daemon = TeamDaemon(cfg, nats_factory=factory)
    daemon.start_nats()
    try:
        deadline = time.time() + 3.0
        while time.time() < deadline and not created:
            time.sleep(0.02)
        check("首次建连成功且 reconnects 仍为 0",
              len(created) == 1 and daemon.counters.snapshot()["reconnects"] == 0,
              "created=%d counters=%s" % (len(created), daemon.counters.snapshot()))

        created[0].is_connected = False   # 模拟中枢掉线
        deadline = time.time() + 5.0
        while time.time() < deadline and len(created) < 2:
            time.sleep(0.02)
        ok = len(created) >= 2
        check("断线后自动重连（新建连接）且 reconnects 计到 1",
              ok and daemon.counters.snapshot()["reconnects"] >= 1
              and created[0].closed is True,
              "created=%d counters=%s" % (len(created), daemon.counters.snapshot()))
        check("重连后重新订阅 a2a.v1.req.<member>",
              ok and "a2a.v1.req.hermes" in created[1].subs,
              "subs=%s" % (sorted(created[1].subs.keys()) if ok else "无第二个连接"))
    finally:
        daemon.shutdown()


# ------------------------------------------------------------------ 主流程

def main():
    stub_port = free_port()
    stub = QuietHTTPServer(("127.0.0.1", stub_port), StubA2AHandler)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    try:
        test_msg_parsing()
        test_err_path()
        test_translation(stub_port)
        test_error_branches(stub_port)
        test_heartbeat(stub_port)
        test_admin(stub_port)
        test_reconnect(stub_port)
    finally:
        stub.shutdown()
        stub.server_close()

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
