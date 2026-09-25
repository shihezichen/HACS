#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# hacs-test-fixtures: 本文件为测试夹具，允许出现假凭据与网段边界值；危险类命中（真令牌/连接串/真实人名）仍按硬门禁拦截
"""
test_view_auth.py —— 看板认证（view_auth.py + blackboard_view.py 门禁）的纯标准库 unittest

做的事：
  1. 纯函数层直接调：口令哈希 / 口令策略 / 会话签名 / tailnet 判定 / 角色×密级矩阵。
  2. 端到端：起一个**假黑板**（进程内，只给 127.0.0.1 随机高端口），再用 sys.executable
     把 blackboard_view.py 以 `--mode public --sqlite <临时文件>` 起成**真子进程**，
     用 http.client 打它，验证门禁、登录、cookie、405、密级遮蔽。
  3. 全程不联网、不碰真的 9940、不碰 PostgreSQL、不装任何第三方包。

覆盖清单：
  ① hash_password / verify_password（含畸形哈希不炸）、check_password_policy
  ② sign_token / unsign_token：篡改 -> None；换密钥 -> None
  ③ in_tailnet("100.64.0.1")=True、in_tailnet("127.0.0.1")=False；role_can_see 3×3 矩阵
  ④ safe_next：开放跳转的各种写法都得被打回 "/"
  ⑤ 未登录 GET / -> 302 且 Location 指向 /login（**127.0.0.1 不豁免**）
  ⑥ 未登录 GET /login -> 200 且页面里有表单；GET /healthz -> 免登录 200
  ⑦ 错口令刷满 FAIL_MAX 次 -> 之后被锁定（页面有锁定提示 + locked_until 非空）
  ⑧ 对口令 -> 302 + Set-Cookie；带 cookie 再 GET / -> 200
  ⑨ 带 cookie POST /api/anything -> 405（含 Allow: GET, HEAD）
  ⑩ viewer 看 sensitive 条目 -> masked=True、无标题、正文一个字节都不下发；owner 看得见
  ⑪ --mode tailnet -> 和 v1.3 完全一样：免登录直连，POST 仍然 405

注意：锁定用的计数是**按来源 IP** 的，而测试全从 127.0.0.1 打过去。所以"锁定"这组
单独起一个服务 + 独立的临时账号库，免得把后面那些需要登录成功的用例一起锁死。
"""

import http.client
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import blackboard_view as bv  # noqa: E402
import view_auth  # noqa: E402

# ---------------------------------------------------------------------------
# 测试夹具真值（硬编码清零 Phase 1.5）
# PHASE2_TODO: 待节点注入 env 后删除
# 默认值逐字等于改动前的真值；未注入 env 时测试行为与改动前完全等价。
FIX_NODE_CLOUD_IP = os.environ.get("AGENT_TEAM_TEST_NODE_CLOUD_IP") or "<host>"

UPSTREAM_TOKEN = "view-auth-test-token-DO-NOT-LEAK-91af"
PASSWORD = "GoodPass2026!"
BAD_PASSWORD = "wrong-password-0000"

# 这两个字符串**绝不允许**出现在 viewer 拿到的响应字节里
SECRET_TITLE = "主人画像·绝密标题"
SECRET_BODY = "这段正文只有 owner 能看见"

SHARED_PAYLOAD = {
    "ok": True,
    "items": [
        {"shared_id": "s-public", "scope": "permanent", "kind": "note",
         "sensitivity": "public", "title": "公开条目", "body": "谁都能看",
         "owner": "hermes", "updated_at": "2026-09-19T10:00:00Z", "version": 1},
        {"shared_id": "s-secret", "scope": "permanent", "kind": "profile",
         "sensitivity": "sensitive", "title": SECRET_TITLE, "body": SECRET_BODY,
         "owner": "hermes", "updated_at": "2026-09-19T10:05:00Z", "version": 3},
    ],
}


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


# --- 假黑板（上游）---------------------------------------------------

class FakeUpstream(BaseHTTPRequestHandler):
    """只认 Bearer token 的假 9940：够 /healthz、/entries、/shared 三个端点用。"""

    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        return

    def _json(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.headers.get("Authorization") != "Bearer " + UPSTREAM_TOKEN:
            self._json(401, {"ok": False, "error": "unauthorized"})
            return
        path = urllib.parse.urlsplit(self.path).path
        if path == "/healthz":
            self._json(200, {"ok": True, "version": "fake"})
        elif path == "/entries":
            self._json(200, {"ok": True, "entries": [], "count": 0})
        elif path == "/shared":
            self._json(200, json.loads(json.dumps(SHARED_PAYLOAD)))
        else:
            self._json(404, {"ok": False, "error": "not_found"})


UPSTREAM = None
UPSTREAM_PORT = 0


def setUpModule():
    global UPSTREAM, UPSTREAM_PORT
    UPSTREAM = ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstream)
    UPSTREAM.daemon_threads = True
    UPSTREAM_PORT = UPSTREAM.server_address[1]
    threading.Thread(target=UPSTREAM.serve_forever, daemon=True).start()


def tearDownModule():
    if UPSTREAM is not None:
        UPSTREAM.shutdown()
        UPSTREAM.server_close()


# --- 被测服务：真子进程 ------------------------------------------------

class ViewServer(object):
    """起一个真的 blackboard_view 子进程，附带自己的临时账号库与会话密钥。

    每个实例一套独立的临时目录 —— 锁定计数、会话、审计互不串味。
    """

    def __init__(self, users, mode="public", extra_args=()):
        self.dir = tempfile.mkdtemp(prefix="bbview-auth-")
        self.db = os.path.join(self.dir, "auth.db")
        self.key = os.path.join(self.dir, "session.key")
        self.token_file = os.path.join(self.dir, "client-token")
        self.log = open(os.path.join(self.dir, "server.log"), "w+",
                        encoding="utf-8", errors="replace")
        self.port = free_port()
        self.proc = None

        with open(self.token_file, "w", encoding="utf-8") as fh:
            fh.write(UPSTREAM_TOKEN)
        view_auth.init_key(self.key)

        # 账号直接用 view_auth 的存储层种进去（CLI 要交互输口令，单测里不合适）
        store = view_auth.SqliteStore(self.db)
        try:
            for username, role in users:
                store.add_user(username, role, view_auth.hash_password(PASSWORD),
                               "unittest", "单测账号")
        finally:
            store.close()

        cmd = [sys.executable, os.path.join(ROOT, "blackboard_view.py"),
               "--host", "127.0.0.1", "--port", str(self.port),
               "--mode", mode,
               "--upstream", "http://127.0.0.1:%d" % UPSTREAM_PORT,
               "--token-file", self.token_file,
               "--refresh-seconds", "0"]
        if mode == "public":
            cmd += ["--sqlite", self.db, "--session-key-file", self.key]
        cmd += list(extra_args)
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"       # 子进程日志是中文，Windows 默认码页会炸
        env["PYTHONUNBUFFERED"] = "1"
        self.proc = subprocess.Popen(cmd, cwd=ROOT, env=env,
                                     stdout=self.log, stderr=subprocess.STDOUT)
        self._wait_ready()

    def _wait_ready(self, timeout=30.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("看板子进程启动即退出：\n" + self._tail())
            try:
                status, _headers, _body = self.request("GET", "/healthz")
                if status == 200:
                    return
            except (OSError, http.client.HTTPException):
                pass
            time.sleep(0.15)
        raise RuntimeError("看板子进程 30s 内没起来：\n" + self._tail())

    def _tail(self):
        try:
            self.log.flush()
            self.log.seek(0)
            return self.log.read()[-4000:]
        except Exception:
            return "(读不到子进程日志)"

    def request(self, method, path, body=None, cookies=None, headers=None):
        """打一枪，返回 (状态码, 响应头列表, 响应体 bytes)。每次新开连接，省得 keep-alive 串状态。"""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        sent = dict(headers or {})
        if cookies:
            sent["Cookie"] = "; ".join("%s=%s" % (k, v) for k, v in cookies.items())
        data = None
        if body is not None:
            data = urllib.parse.urlencode(body).encode("utf-8")
            sent["Content-Type"] = "application/x-www-form-urlencoded"
            sent["Content-Length"] = str(len(data))
        try:
            conn.request(method, path, body=data, headers=sent)
            resp = conn.getresponse()
            return resp.status, resp.getheaders(), resp.read()
        finally:
            conn.close()

    # --- 直接读子进程写下的账号库（只读，不打扰服务）---
    def _rows(self, sql, args):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()

    def audit_ip(self, event, detail_like):
        """某条审计的 ip 列 + detail。查不到返回 (None, None)。

        锁定与审计的"键"到底记成了谁，只有翻库才算数 —— 光看 HTTP 响应看不出来。
        """
        rows = self._rows("SELECT ip, detail FROM view_audit "
                          "WHERE event = ? AND detail LIKE ? ORDER BY rowid DESC LIMIT 1",
                          (event, detail_like))
        return (rows[0][0], rows[0][1]) if rows else (None, None)

    def audit_ips(self, event, detail_like):
        """同一批审计的 ip 列去重后的集合（垃圾头那组要**每一条**都退回对端 IP）。"""
        rows = self._rows("SELECT DISTINCT ip FROM view_audit "
                          "WHERE event = ? AND detail LIKE ?", (event, detail_like))
        return sorted(r[0] for r in rows)

    def failure_ip(self, username):
        """view_login_failures 里这个账号最近一次失败记在了哪个 IP 名下 = 锁定键。"""
        rows = self._rows("SELECT ip FROM view_login_failures "
                          "WHERE username = ? ORDER BY rowid DESC LIMIT 1", (username,))
        return rows[0][0] if rows else None

    def stop(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)
        try:
            self.log.close()
        except Exception:
            pass
        shutil.rmtree(self.dir, ignore_errors=True)


def set_cookies(headers):
    """从响应头里把 Set-Cookie 全捞出来，拆成 {名: 值}（Max-Age=0 的算删掉）。"""
    jar = {}
    for name, value in headers:
        if name.lower() != "set-cookie":
            continue
        first = value.split(";")[0]
        if "=" not in first:
            continue
        key, val = first.split("=", 1)
        jar[key.strip()] = val.strip()
    return jar


def header_of(headers, name):
    for key, value in headers:
        if key.lower() == name.lower():
            return value
    return None


def all_headers(headers, name):
    return [v for k, v in headers if k.lower() == name.lower()]


def login(server, username, password, next_url="/"):
    """走完整的两步登录：先 GET /login 拿 CSRF，再 POST。返回 (状态码, 头, 体, cookie 罐)。"""
    status, headers, body = server.request("GET", "/login")
    assert status == 200, "GET /login 应该 200，实际 %d" % status
    jar = set_cookies(headers)
    csrf = _hidden_value(body.decode("utf-8"), bv.CSRF_FIELD)
    status, headers, body = server.request(
        "POST", "/login",
        body={"action": "login", "user": username, "password": password,
              "next": next_url, bv.CSRF_FIELD: csrf},
        cookies=jar)
    jar.update(set_cookies(headers))
    return status, headers, body, jar


def _hidden_value(html, field):
    """从登录页里抠出隐藏域的值（够用就行，不为此引入 HTML 解析器）。"""
    needle = 'name="%s" value="' % field
    at = html.find(needle)
    if at < 0:
        return ""
    at += len(needle)
    return html[at:html.find('"', at)]


# ================================================================ 纯函数层

class TestPasswordHash(unittest.TestCase):
    def test_hash_then_verify(self):
        stored = view_auth.hash_password(PASSWORD)
        self.assertTrue(stored.startswith("scrypt$"))
        self.assertTrue(view_auth.verify_password(PASSWORD, stored))
        self.assertFalse(view_auth.verify_password(PASSWORD + "x", stored))

    def test_salt_is_per_account(self):
        """同一个口令哈希两次，结果必须不一样（否则撞库表就有用了）。"""
        self.assertNotEqual(view_auth.hash_password(PASSWORD),
                            view_auth.hash_password(PASSWORD))

    def test_malformed_hash_returns_false_not_raise(self):
        """库里那列坏了也只能返回 False —— 登录路径上抛异常就是一个账号枚举信号。"""
        for broken in ("", "not-a-hash", "scrypt$", "scrypt$a$b$c$d$e",
                       "bcrypt$1$2$3$4$5", "scrypt$16384$8$1$@@@$@@@", None, 123):
            self.assertFalse(view_auth.verify_password(PASSWORD, broken),
                             "畸形哈希 %r 应该返回 False" % (broken,))

    def test_looks_hashed(self):
        self.assertTrue(view_auth.looks_hashed(view_auth.hash_password(PASSWORD)))
        self.assertFalse(view_auth.looks_hashed(PASSWORD))
        self.assertFalse(view_auth.looks_hashed(""))


class TestPasswordPolicy(unittest.TestCase):
    def test_accepts_reasonable_password(self):
        view_auth.check_password_policy(PASSWORD)          # 不抛就算过

    def test_rejects_short_empty_and_blank(self):
        for bad in ("", "1234567", "   " * 3):
            with self.assertRaises(view_auth.AuthError):
                view_auth.check_password_policy(bad)

    def test_rejects_none_and_too_long(self):
        with self.assertRaises(view_auth.AuthError):
            view_auth.check_password_policy(None)
        with self.assertRaises(view_auth.AuthError):
            view_auth.check_password_policy("a" * (view_auth.PASSWORD_MAX + 1))


class TestTokenSigning(unittest.TestCase):
    def setUp(self):
        self.key = b"0123456789abcdef0123456789abcdef"
        self.other = b"fedcba9876543210fedcba9876543210"

    def test_round_trip(self):
        signed = view_auth.sign_token("abc123", self.key)
        self.assertTrue(signed.startswith("v1."))
        self.assertEqual(view_auth.unsign_token(signed, self.key), "abc123")

    def test_tampered_token_is_none(self):
        signed = view_auth.sign_token("abc123", self.key)
        prefix, token, sig = signed.split(".")
        for bad in ("%s.%sX.%s" % (prefix, token, sig),       # 改 token
                    "%s.%s.%sX" % (prefix, token, sig),       # 改签名
                    "v2.%s.%s" % (token, sig),                # 改版本
                    signed[:-1], "", "v1.onlytwo", "garbage"):
            self.assertIsNone(view_auth.unsign_token(bad, self.key),
                              "篡改后的 %r 必须验不过" % bad)

    def test_other_key_is_none(self):
        signed = view_auth.sign_token("abc123", self.key)
        self.assertIsNone(view_auth.unsign_token(signed, self.other))

    def test_purpose_is_domain_separated(self):
        """会话签名不能当 CSRF 用，反过来也不行。"""
        signed = view_auth.sign_token("abc123", self.key, purpose=b"session")
        self.assertIsNone(view_auth.unsign_token(signed, self.key, purpose=b"csrf"))

    def test_csrf_double_submit(self):
        token = view_auth.issue_csrf(self.key)
        self.assertTrue(view_auth.check_csrf(token, token, self.key))
        self.assertFalse(view_auth.check_csrf(token, view_auth.issue_csrf(self.key),
                                              self.key))
        self.assertFalse(view_auth.check_csrf(token, token, self.other))
        self.assertFalse(view_auth.check_csrf("", "", self.key))


class TestTailnet(unittest.TestCase):
    def test_tailnet_addresses(self):
        for ip in ("100.64.0.1", FIX_NODE_CLOUD_IP, "100.127.255.254",
                   "::ffff:100.64.0.1"):
            self.assertTrue(view_auth.in_tailnet(ip), "%s 应该算 tailnet" % ip)

    def test_loopback_is_not_tailnet(self):
        """这一条是整套门禁的地基：cloudflared 就是从 127.0.0.1 回连的。"""
        self.assertFalse(view_auth.in_tailnet("127.0.0.1"))
        self.assertFalse(view_auth.in_tailnet("::1"))
        self.assertTrue(view_auth.is_loopback("127.0.0.1"))

    def test_outside_addresses(self):
        for ip in ("100.63.255.255", "100.128.0.0", "8.8.8.8", "192.168.1.5",
                   "10.0.0.1", "", None, "not-an-ip", "100.64.0.1 "):
            if ip == "100.64.0.1 ":
                self.assertTrue(view_auth.in_tailnet(ip))      # 两头空白要容忍
                continue
            self.assertFalse(view_auth.in_tailnet(ip), "%r 不该算 tailnet" % (ip,))


class TestRoleMatrix(unittest.TestCase):
    def test_three_by_three(self):
        expect = {
            ("viewer", "public"): True,
            ("viewer", "internal"): False,
            ("viewer", "sensitive"): False,
            ("member", "public"): True,
            ("member", "internal"): True,
            ("member", "sensitive"): False,
            ("owner", "public"): True,
            ("owner", "internal"): True,
            ("owner", "sensitive"): True,
        }
        for (role, level), want in expect.items():
            self.assertEqual(view_auth.role_can_see(role, level), want,
                             "%s 看 %s 应该是 %s" % (role, level, want))

    def test_unknown_role_is_viewer_and_unknown_level_is_internal(self):
        """两边都往收紧了兜：认不出的角色按 viewer，认不出的密级按 internal。"""
        self.assertFalse(view_auth.role_can_see("superadmin", "internal"))
        self.assertTrue(view_auth.role_can_see("superadmin", "public"))
        self.assertFalse(view_auth.role_can_see("viewer", "top-secret"))
        self.assertTrue(view_auth.role_can_see("member", "top-secret"))
        self.assertEqual(view_auth.DEFAULT_SENSITIVITY, "internal")


class TestSafeNext(unittest.TestCase):
    """?next= 是登录页上唯一一个由外人控制的跳转目标，必须只能是站内相对路径。"""

    def test_keeps_relative_paths(self):
        for good in ("/", "/api/shared", "/?tab=meeting", "/account"):
            self.assertEqual(bv.safe_next(good), good)

    def test_blocks_open_redirect(self):
        for bad in ("//evil.com", "https://evil.com/x", "http://evil.com",
                    "/\\evil.com", "evil.com", "", None, "  ",
                    "/x\r\nSet-Cookie: a=b", "/x\\y", "/login", "/login?next=/",
                    "/" + "a" * 600):
            self.assertEqual(bv.safe_next(bad), "/", "%r 必须被打回 /" % (bad,))


# ================================================================ HTTP 端到端

class TestPublicGate(unittest.TestCase):
    """--mode public：门禁、登录、cookie、405、密级遮蔽。"""

    server = None

    @classmethod
    def setUpClass(cls):
        cls.server = ViewServer([("owner1", "owner"), ("viewer1", "viewer"),
                                 ("member1", "member")])

    @classmethod
    def tearDownClass(cls):
        if cls.server is not None:
            cls.server.stop()
            cls.server = None

    # --- ⑤⑥ 未登录 ---
    def test_root_without_cookie_redirects_to_login(self):
        status, headers, _body = self.server.request("GET", "/")
        self.assertEqual(status, 302)
        location = header_of(headers, "Location") or ""
        self.assertTrue(location.startswith(bv.LOGIN_PATH),
                        "Location 应该指向 /login，实际 %r" % location)
        self.assertIn("next=", location)

    def test_loopback_is_not_exempt(self):
        """整套门禁最要命的一条：本机回连**不**免登录。"""
        for path in ("/", "/api/entries", "/api/shared", "/account", "/nope"):
            status, headers, _ = self.server.request("GET", path)
            self.assertEqual(status, 302, "%s 未登录应该 302" % path)
            self.assertTrue((header_of(headers, "Location") or "")
                            .startswith(bv.LOGIN_PATH))

    def test_next_is_preserved_and_sanitised(self):
        _s, headers, _b = self.server.request("GET", "/api/shared?scope=permanent")
        location = header_of(headers, "Location")
        self.assertIn("next=", location)
        nxt = urllib.parse.parse_qs(urllib.parse.urlsplit(location).query)["next"][0]
        self.assertTrue(nxt.startswith("/api/shared"))

    def test_login_page_is_200_with_form(self):
        status, _headers, body = self.server.request("GET", "/login")
        self.assertEqual(status, 200)
        html = body.decode("utf-8")
        self.assertIn("<form", html)
        self.assertIn('name="user"', html)
        self.assertIn('name="password"', html)
        self.assertIn('name="%s"' % bv.CSRF_FIELD, html)
        self.assertIn("viewport", html)             # 手机上要能看
        self.assertNotIn("http://", html)           # 自包含：没有任何外部资源
        self.assertNotIn("https://", html)

    def test_healthz_is_open(self):
        status, _headers, body = self.server.request("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body.decode("utf-8"))["ok"])

    def test_security_headers_on_every_response(self):
        for path in ("/healthz", "/login", "/"):
            _s, headers, _b = self.server.request("GET", path)
            for name in ("Strict-Transport-Security", "Content-Security-Policy",
                         "X-Content-Type-Options", "X-Frame-Options",
                         "Referrer-Policy", "X-Robots-Tag"):
                self.assertIsNotNone(header_of(headers, name),
                                     "%s 上缺了 %s" % (path, name))
            self.assertEqual(header_of(headers, "X-Content-Type-Options"), "nosniff")
            self.assertEqual(header_of(headers, "X-Frame-Options"), "DENY")

    # --- ⑧ 登录成功 ---
    def test_login_sets_cookie_and_then_root_is_200(self):
        status, headers, _body, jar = login(self.server, "owner1", PASSWORD)
        self.assertEqual(status, 302)
        self.assertEqual(header_of(headers, "Location"), "/")
        raw = " ; ".join(all_headers(headers, "Set-Cookie"))
        self.assertIn(view_auth.SESSION_COOKIE + "=", raw)
        self.assertIn("HttpOnly", raw)
        self.assertIn("SameSite=Lax", raw)
        self.assertIn("Path=/", raw)
        self.assertNotIn("Secure", raw)             # 明文 HTTP 上不该打 Secure

        status, _headers, body = self.server.request("GET", "/", cookies=jar)
        self.assertEqual(status, 200)
        html = body.decode("utf-8")
        self.assertIn("团队门户", html)              # v1.5：根路径是 webPortal 卡片墙
        self.assertIn('href="' + bv.BOARD_PATH + '"', html)   # 卡片指向白板
        self.assertIn("退出", html)                 # 登录后页面上有改密/退出
        self.assertIn(bv.ACCOUNT_PATH, html)

        # 白板本尊挪到 /board，登录后照常拿得到
        status, _headers, body = self.server.request("GET", bv.BOARD_PATH, cookies=jar)
        self.assertEqual(status, 200)
        self.assertIn("团队黑板", body.decode("utf-8"))

    def test_secure_flag_when_proxy_says_https(self):
        """外层隧道报 X-Forwarded-Proto: https 时，cookie 要打上 Secure。"""
        status, headers, body = self.server.request("GET", "/login",
                                                    headers={"X-Forwarded-Proto": "https"})
        jar = set_cookies(headers)
        csrf = _hidden_value(body.decode("utf-8"), bv.CSRF_FIELD)
        self.assertEqual(status, 200)
        status, headers, _b = self.server.request(
            "POST", "/login",
            body={"action": "login", "user": "owner1", "password": PASSWORD,
                  "next": "/", bv.CSRF_FIELD: csrf},
            cookies=jar, headers={"X-Forwarded-Proto": "https"})
        self.assertEqual(status, 302)
        raw = " ; ".join(all_headers(headers, "Set-Cookie"))
        self.assertIn("Secure", raw)

    def test_login_honours_next(self):
        status, headers, _body, _jar = login(self.server, "owner1", PASSWORD,
                                             next_url="/?tab=shared")
        self.assertEqual(status, 302)
        self.assertEqual(header_of(headers, "Location"), "/?tab=shared")

    def test_login_refuses_open_redirect_next(self):
        status, headers, _body, _jar = login(self.server, "owner1", PASSWORD,
                                             next_url="https://evil.com/steal")
        self.assertEqual(status, 302)
        self.assertEqual(header_of(headers, "Location"), "/")

    def test_wrong_password_does_not_leak_which_part_is_wrong(self):
        """"没这个账号"和"口令错"必须是同一句话，否则就是白送一个用户名枚举器。"""
        _s, _h, body_a, _j = login(self.server, "owner1", BAD_PASSWORD)
        _s, _h, body_b, _j = login(self.server, "no-such-user", BAD_PASSWORD)
        text_a, text_b = body_a.decode("utf-8"), body_b.decode("utf-8")
        self.assertIn("账号或口令不正确", text_a)
        self.assertEqual(text_a.count("账号或口令不正确"),
                         text_b.count("账号或口令不正确"))
        for leak in ("账号不存在", "口令不匹配", "账号已停用"):
            self.assertNotIn(leak, text_a)
            self.assertNotIn(leak, text_b)

    def test_bad_csrf_is_rejected(self):
        status, _headers, body = self.server.request(
            "POST", "/login",
            body={"action": "login", "user": "owner1", "password": PASSWORD,
                  "next": "/", bv.CSRF_FIELD: "v1.forged.forged"},
            cookies={view_auth.CSRF_COOKIE: view_auth.issue_csrf(b"another-key-32-bytes-long!!")})
        self.assertEqual(status, 403)
        self.assertIn("重新提交", body.decode("utf-8"))

    def test_tampered_session_cookie_is_rejected(self):
        _s, _h, _b, jar = login(self.server, "owner1", PASSWORD)
        jar[view_auth.SESSION_COOKIE] = jar[view_auth.SESSION_COOKIE] + "X"
        status, headers, _body = self.server.request("GET", "/", cookies=jar)
        self.assertEqual(status, 302)
        self.assertTrue((header_of(headers, "Location") or "").startswith(bv.LOGIN_PATH))

    def test_logout_revokes_the_session(self):
        _s, _h, _b, jar = login(self.server, "owner1", PASSWORD)
        self.assertEqual(self.server.request("GET", "/", cookies=jar)[0], 200)

        _s, headers, body = self.server.request("GET", bv.ACCOUNT_PATH, cookies=jar)
        csrf = _hidden_value(body.decode("utf-8"), bv.CSRF_FIELD)
        jar.update(set_cookies(headers))
        status, headers, _b = self.server.request(
            "POST", "/login", body={"action": "logout", bv.CSRF_FIELD: csrf}, cookies=jar)
        self.assertEqual(status, 302)
        self.assertEqual(header_of(headers, "Location"), bv.LOGIN_PATH)

        # 撤销之后，原来那张 cookie 不再好使（服务端撤的，不是靠浏览器删）
        status, _headers, _b = self.server.request("GET", "/", cookies=jar)
        self.assertEqual(status, 302)

    def test_account_page_has_passwd_and_logout(self):
        _s, _h, _b, jar = login(self.server, "member1", PASSWORD)
        status, _headers, body = self.server.request("GET", bv.ACCOUNT_PATH, cookies=jar)
        self.assertEqual(status, 200)
        html = body.decode("utf-8")
        self.assertIn('value="passwd"', html)
        self.assertIn('value="logout"', html)
        self.assertIn("member1", html)

    # --- ⑨ 只读纪律 ---
    def test_post_to_api_is_405_even_when_logged_in(self):
        _s, _h, _b, jar = login(self.server, "owner1", PASSWORD)
        for method, path in (("POST", "/api/anything"), ("POST", "/api/entries"),
                             ("PUT", "/"), ("PATCH", "/api/shared"),
                             ("DELETE", "/api/entries")):
            status, headers, body = self.server.request(
                method, path, body={"x": "1"} if method == "POST" else None, cookies=jar)
            self.assertEqual(status, 405, "%s %s 应该 405" % (method, path))
            self.assertEqual(header_of(headers, "Allow"), "GET, HEAD")
            self.assertEqual(json.loads(body.decode("utf-8"))["error"],
                             "method_not_allowed")

    def test_post_to_api_is_405_without_login_too(self):
        """写请求先撞 405，不会被门禁变成 302 —— 只读纪律跟登录状态无关。"""
        status, headers, _body = self.server.request("POST", "/api/entries",
                                                     body={"x": "1"})
        self.assertEqual(status, 405)
        self.assertEqual(header_of(headers, "Allow"), "GET, HEAD")

    # --- ⑩ 密级遮蔽 ---
    def _shared_items(self, jar):
        status, _headers, body = self.server.request("GET", "/api/shared", cookies=jar)
        self.assertEqual(status, 200, body[:400])
        return body, {i["shared_id"]: i for i in json.loads(body.decode("utf-8"))["items"]}

    def test_viewer_sees_sensitive_item_masked(self):
        _s, _h, _b, jar = login(self.server, "viewer1", PASSWORD)
        raw, items = self._shared_items(jar)

        secret = items["s-secret"]
        self.assertTrue(secret["masked"])
        self.assertEqual(secret["mask_reason"], "role")
        self.assertEqual(secret.get("title"), "")       # 连标题都不给
        self.assertEqual(secret.get("body"), "")
        self.assertNotIn("owner", secret)               # 连"谁在维护"都不漏
        self.assertEqual(secret["shared_id"], "s-secret")   # 但"存在"这件事还在
        # 最硬的一条：秘密的字节根本没离开服务端
        self.assertNotIn(SECRET_TITLE, raw.decode("utf-8"))
        self.assertNotIn(SECRET_BODY, raw.decode("utf-8"))

    def test_viewer_still_sees_public_item(self):
        """遮蔽不是"整条 404 掉"，公开的那条照常给。"""
        _s, _h, _b, jar = login(self.server, "viewer1", PASSWORD)
        _raw, items = self._shared_items(jar)
        self.assertEqual(items["s-public"]["title"], "公开条目")
        self.assertNotIn("masked", items["s-public"])
        self.assertEqual(len(items), 2)

    def test_owner_sees_everything(self):
        _s, _h, _b, jar = login(self.server, "owner1", PASSWORD)
        raw, items = self._shared_items(jar)
        self.assertEqual(items["s-secret"]["title"], SECRET_TITLE)
        self.assertIn(SECRET_BODY, raw.decode("utf-8"))

    def test_member_cannot_see_sensitive(self):
        _s, _h, _b, jar = login(self.server, "member1", PASSWORD)
        raw, items = self._shared_items(jar)
        self.assertTrue(items["s-secret"]["masked"])
        self.assertNotIn(SECRET_TITLE, raw.decode("utf-8"))

    # --- 审计 ---
    def test_audit_records_login_and_denials_with_ip_and_ua(self):
        # 四种事件在这个用例里自己造齐，不依赖别的用例先跑过（unittest 不保证顺序）
        ua = {"User-Agent": "unittest-agent/1.0"}
        login(self.server, "owner1", BAD_PASSWORD)                  # login_fail
        login(self.server, "owner1", PASSWORD)                      # login_ok（顺带清零失败计数）
        self.server.request("GET", "/", headers=ua)                 # access_denied
        self.server.request("POST", "/api/entries", body={"x": "1"}, headers=ua)  # write_blocked

        store = view_auth.SqliteStore(self.server.db)
        try:
            events = {}
            for row in store.list_audit(limit=200):
                events.setdefault(row["event"], []).append(row)
        finally:
            store.close()
        for event in ("login_ok", "login_fail", "access_denied", "write_blocked"):
            self.assertIn(event, events, "审计里应该有 %s" % event)
        for row in events["login_ok"]:
            self.assertEqual(row["ip"], "127.0.0.1")
            self.assertTrue(row["ua"] is not None)


class TestBruteForceLock(unittest.TestCase):
    """锁定是按来源 IP 记的，而所有用例都从 127.0.0.1 打过去 ——
    所以这一组必须用**自己的服务 + 自己的账号库**，否则会把别的用例一起锁死。"""

    server = None

    @classmethod
    def setUpClass(cls):
        cls.server = ViewServer([("owner1", "owner")])

    @classmethod
    def tearDownClass(cls):
        if cls.server is not None:
            cls.server.stop()
            cls.server = None

    def test_repeated_wrong_password_locks_the_ip(self):
        for _ in range(view_auth.FAIL_MAX):
            status, _h, body = login(self.server, "owner1", BAD_PASSWORD)[:3]
            self.assertEqual(status, 200)
            self.assertIn("账号或口令不正确", body.decode("utf-8"))

        # 满了之后再来一次：换成"锁定"提示，而且**连对口令也进不去**
        _s, _h, body, _j = login(self.server, "owner1", BAD_PASSWORD)
        text = body.decode("utf-8")
        self.assertIn("尝试过于频繁", text)
        self.assertIn("分钟后再试", text)

        status, headers, body, _jar = login(self.server, "owner1", PASSWORD)
        self.assertEqual(status, 200, "锁定期间对口令也不该放行")
        self.assertIn("尝试过于频繁", body.decode("utf-8"))
        self.assertIsNone(header_of(headers, "Location"))

        # 库里也确实记着这把锁（进程重启不等于解锁）
        store = view_auth.SqliteStore(self.server.db)
        try:
            locked_until = view_auth.LockTracker(store).locked_until("127.0.0.1")
        finally:
            store.close()
        self.assertIsNotNone(locked_until, "locked_until 应该非空")


class TestTailnetMode(unittest.TestCase):
    """--mode tailnet：完全不装认证，行为必须和 v1.3 一模一样。"""

    server = None

    @classmethod
    def setUpClass(cls):
        cls.server = ViewServer([("owner1", "owner")], mode="tailnet")

    @classmethod
    def tearDownClass(cls):
        if cls.server is not None:
            cls.server.stop()
            cls.server = None

    def test_no_login_required(self):
        status, _headers, body = self.server.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("团队门户", body.decode("utf-8"))      # v1.5：根路径是卡片墙
        status, _headers, body = self.server.request("GET", bv.BOARD_PATH)
        self.assertEqual(status, 200)
        self.assertIn("团队黑板", body.decode("utf-8"))

    def test_portal_cards_and_old_links(self):
        """v1.5 新口径：/ 与 /portal 是卡片墙，白板在 /board，旧带参链接 302 过去。"""
        status, _headers, body = self.server.request("GET", "/")
        self.assertEqual(status, 200)
        html = body.decode("utf-8")
        self.assertIn("团队门户", html)
        self.assertIn("人机对话台", html)                       # 第二张卡：群聊入口
        self.assertIn('href="' + bv.BOARD_PATH + '"', html)

        self.assertEqual(self.server.request("GET", "/portal")[0], 200)

        status, headers, _b = self.server.request("GET", "/?tab=shared")   # 老书签
        self.assertEqual(status, 302)
        self.assertEqual(header_of(headers, "Location"), bv.BOARD_PATH)

    def test_api_is_open_and_unmasked(self):
        status, _headers, body = self.server.request("GET", "/api/shared")
        self.assertEqual(status, 200)
        self.assertIn(SECRET_TITLE, body.decode("utf-8"))   # 内网不做角色遮蔽

    def test_login_route_is_not_mounted(self):
        """没装认证就没有登录路由：GET /login 落到 404，POST /login 照样 405。"""
        self.assertEqual(self.server.request("GET", "/login")[0], 404)
        status, headers, _b = self.server.request("POST", "/login",
                                                  body={"action": "login"})
        self.assertEqual(status, 405)
        self.assertEqual(header_of(headers, "Allow"), "GET, HEAD")


def login_with(server, username, password, headers=None):
    """带自定义请求头走一遍登录（口径同 login()，只是多塞几个头）。"""
    status, resp_headers, body = server.request("GET", "/login", headers=headers)
    assert status == 200, "GET /login 应该 200，实际 %d" % status
    jar = set_cookies(resp_headers)
    csrf = _hidden_value(body.decode("utf-8"), bv.CSRF_FIELD)
    return server.request("POST", "/login",
                          body={"action": "login", "user": username,
                                "password": password, "next": "/",
                                bv.CSRF_FIELD: csrf},
                          cookies=jar, headers=headers)


# ================================================================ 受信反代
#
# 为什么要有这一组：cloudflared 从 127.0.0.1 回连公网实例，**所有公网访客的对端 IP
# 都是 127.0.0.1**。锁定键要是还按对端 IP 记，一个人连错 5 次就把全体用户锁 15 分钟；
# 审计里也人人都是 127.0.0.1，事后追不了责。--trust-proxy 就是为这个加的。
# 这一组盯死三件事：**默认零信任**、**受信时按头记账**、**白名单绝不因此松口**。

class TestTrustProxyParsing(unittest.TestCase):
    """纯函数层：IP 字面量校验 + CIDR 解析。这三个函数错了，上面全是错的。"""

    def test_parse_ip_normalises_equivalent_spellings(self):
        """同一个访客换个写法就换一把锁的话，那锁等于没上。"""
        self.assertEqual(str(bv.parse_ip("8.8.8.8")), "8.8.8.8")
        self.assertEqual(str(bv.parse_ip("  8.8.8.8  ")), "8.8.8.8")
        self.assertEqual(str(bv.parse_ip("[::1]")), "::1")
        self.assertEqual(str(bv.parse_ip("::ffff:8.8.8.8")), "8.8.8.8")
        self.assertEqual(str(bv.parse_ip("2001:0db8::0001")), "2001:db8::1")
        self.assertEqual(str(bv.parse_ip("fe80::1%eth0")), "fe80::1")

    def test_parse_ip_rejects_garbage(self):
        for bad in ("", None, "   ", "not-an-ip", "8.8.8.8.8", "999.1.1.1",
                    "8.8.8.8, 9.9.9.9", "127.0.0.1:9942", "<script>", "9" * 200):
            self.assertIsNone(bv.parse_ip(bad), "%r 不该被当成 IP" % (bad,))

    def test_parse_trust_proxy_default_is_zero_trust(self):
        for empty in ("", None, "   ", ",,,"):
            self.assertEqual(bv.parse_trust_proxy(empty), (),
                             "%r 必须解析成空列表 = 谁都不信" % (empty,))

    def test_parse_trust_proxy_accepts_ip_and_cidr(self):
        nets = bv.parse_trust_proxy(" 127.0.0.1 , 10.0.0.0/8,::1 ")
        self.assertEqual([str(n) for n in nets],
                         ["127.0.0.1/32", "10.0.0.0/8", "::1/128"])

    def test_parse_trust_proxy_drops_bad_segments_with_a_warning(self):
        with self.assertLogs("blackboard_view", level="WARNING") as caught:
            nets = bv.parse_trust_proxy("127.0.0.1,garbage,10.0.0.0/8")
        self.assertEqual([str(n) for n in nets], ["127.0.0.1/32", "10.0.0.0/8"])
        self.assertTrue(any("garbage" in line for line in caught.output))

    def test_ip_in_nets(self):
        nets = bv.parse_trust_proxy("127.0.0.0/8")
        self.assertTrue(bv.ip_in_nets("127.0.0.1", nets))
        self.assertTrue(bv.ip_in_nets("127.9.9.9", nets))
        self.assertTrue(bv.ip_in_nets("::ffff:127.0.0.1", nets))
        self.assertFalse(bv.ip_in_nets("8.8.8.8", nets))
        self.assertFalse(bv.ip_in_nets("not-an-ip", nets))
        self.assertFalse(bv.ip_in_nets("", nets))
        for anything in ("127.0.0.1", "8.8.8.8", "::1"):
            self.assertFalse(bv.ip_in_nets(anything, ()), "空列表必须谁都不信")


class TestTrustProxy(unittest.TestCase):
    """端到端：四个真子进程，四种 --trust-proxy 配置。

    判定一律**翻库**（view_audit.ip / view_login_failures.ip）—— "键记成了谁"
    从 HTTP 响应上看不出来，只有表里那一列算数。
    """

    plain = trusted = cidr = foreign = None

    @classmethod
    def setUpClass(cls):
        users = [("owner1", "owner")]
        cls.plain = ViewServer(users)                       # 不给参数 = 现状
        cls.trusted = ViewServer(users, extra_args=["--trust-proxy", "127.0.0.1"])
        cls.cidr = ViewServer(users, extra_args=["--trust-proxy", "127.0.0.0/8"])
        cls.foreign = ViewServer(users, extra_args=["--trust-proxy", "10.0.0.0/8"])

    @classmethod
    def tearDownClass(cls):
        for name in ("plain", "trusted", "cidr", "foreign"):
            server = getattr(cls, name, None)
            if server is not None:
                server.stop()
                setattr(cls, name, None)

    # --- ① 默认零信任：没给 --trust-proxy 时行为与以前完全一致 ---
    def test_without_trust_proxy_the_header_is_ignored(self):
        hdr = {"CF-Connecting-IP": "8.8.8.8"}
        status, _h, _b = self.plain.request("GET", "/probe-plain", headers=hdr)
        self.assertEqual(status, 302)

        ip, detail = self.plain.audit_ip("access_denied", "%/probe-plain%")
        self.assertEqual(ip, "127.0.0.1", "没配 --trust-proxy 就绝不能采信头")
        self.assertIn("8.8.8.8", detail or "",
                      "头不当判据，但要留在 detail 里供人工对照")

        login_with(self.plain, "lock-plain", BAD_PASSWORD, hdr)
        self.assertEqual(self.plain.failure_ip("lock-plain"), "127.0.0.1",
                         "锁定键同理：默认谁都不信")

    # --- ② 受信反代：头说了算 ---
    def test_trusted_peer_makes_the_header_the_key(self):
        hdr = {"CF-Connecting-IP": "8.8.8.8"}
        self.trusted.request("GET", "/probe-trusted", headers=hdr)

        ip, detail = self.trusted.audit_ip("access_denied", "%/probe-trusted%")
        self.assertEqual(ip, "8.8.8.8", "对端受信时审计要记真实访客 IP")
        self.assertIn("127.0.0.1(via 8.8.8.8)", detail or "",
                      "对照信息（原始对端 + 外层声称）要保留")
        self.assertNotIn("(via", ip, "带 (via ...) 的拼接串绝不能当键")

        login_with(self.trusted, "lock-trusted", BAD_PASSWORD, hdr)
        self.assertEqual(self.trusted.failure_ip("lock-trusted"), "8.8.8.8",
                         "锁定要按真实访客记，不然一人连错全场被锁")

    def test_trusted_peer_without_header_falls_back_to_peer(self):
        self.trusted.request("GET", "/probe-nohdr")
        ip, detail = self.trusted.audit_ip("access_denied", "%/probe-nohdr%")
        self.assertEqual(ip, "127.0.0.1")
        self.assertNotIn("via", detail or "", "没有头就别在 detail 里编一个")

        login_with(self.trusted, "lock-nohdr", BAD_PASSWORD)
        self.assertEqual(self.trusted.failure_ip("lock-nohdr"), "127.0.0.1")

    def test_x_forwarded_for_takes_only_the_first_hop(self):
        self.trusted.request("GET", "/probe-xff",
                             headers={"X-Forwarded-For": "203.0.113.9, 10.0.0.1"})
        ip, _detail = self.trusted.audit_ip("access_denied", "%/probe-xff%")
        self.assertEqual(ip, "203.0.113.9")

    def test_cf_header_wins_over_x_forwarded_for(self):
        self.trusted.request("GET", "/probe-both",
                             headers={"CF-Connecting-IP": "198.51.100.7",
                                      "X-Forwarded-For": "203.0.113.9"})
        ip, _detail = self.trusted.audit_ip("access_denied", "%/probe-both%")
        self.assertEqual(ip, "198.51.100.7")

    # --- ③ 垃圾值：退回对端 IP，而且不许 500 ---
    def test_garbage_header_falls_back_and_never_500s(self):
        for bad in ("not-an-ip", "", "   ", "9" * 200, "8.8.8.8 9.9.9.9",
                    "127.0.0.1:9942", "<script>alert(1)</script>", "8.8.8.8.8"):
            status, headers, _b = self.trusted.request(
                "GET", "/probe-junk", headers={"CF-Connecting-IP": bad})
            self.assertEqual(status, 302,
                             "垃圾头 %r 只能被忽略，不能把请求打崩" % (bad,))
            self.assertTrue((header_of(headers, "Location") or "")
                            .startswith(bv.LOGIN_PATH))
        self.assertEqual(self.trusted.audit_ips("access_denied", "%/probe-junk%"),
                         ["127.0.0.1"], "垃圾头一律退回对端 IP，一条都不许例外")

    # --- ④ 安全负例：伪造 tailnet IP 换不来免登录 ---
    def test_spoofed_tailnet_header_never_grants_a_free_pass(self):
        """这一条是整套改动的红线：白名单只看对端 IP，永远不看头。"""
        for header in ("CF-Connecting-IP", "X-Forwarded-For"):
            for path in ("/", "/api/entries", "/api/shared", "/account"):
                status, headers, body = self.trusted.request(
                    "GET", path, headers={header: "100.64.0.1"})
                self.assertEqual(status, 302,
                                 "%s: %s 伪造 tailnet IP 不该换来免登录" % (header, path))
                self.assertTrue((header_of(headers, "Location") or "")
                                .startswith(bv.LOGIN_PATH))
                self.assertNotIn(SECRET_TITLE, body.decode("utf-8"))

    def test_spoofed_tailnet_header_also_fails_on_the_cidr_server(self):
        """整段 127.0.0.0/8 受信时也一样 —— 受信的是"记账口径"，不是"通行证"。"""
        status, headers, _b = self.cidr.request(
            "GET", "/", headers={"CF-Connecting-IP": "100.64.0.1"})
        self.assertEqual(status, 302)
        self.assertTrue((header_of(headers, "Location") or "")
                        .startswith(bv.LOGIN_PATH))

    # --- ⑤ CIDR 形式 & 不在列表里的对端 ---
    def test_cidr_form_is_accepted(self):
        self.cidr.request("GET", "/probe-cidr",
                          headers={"CF-Connecting-IP": "8.8.4.4"})
        ip, _detail = self.cidr.audit_ip("access_denied", "%/probe-cidr%")
        self.assertEqual(ip, "8.8.4.4", "--trust-proxy 127.0.0.0/8 要能覆盖 127.0.0.1")

        login_with(self.cidr, "lock-cidr", BAD_PASSWORD,
                   {"CF-Connecting-IP": "8.8.4.4"})
        self.assertEqual(self.cidr.failure_ip("lock-cidr"), "8.8.4.4")

    def test_peer_outside_the_list_is_ignored(self):
        self.foreign.request("GET", "/probe-foreign",
                             headers={"CF-Connecting-IP": "8.8.4.4"})
        ip, detail = self.foreign.audit_ip("access_denied", "%/probe-foreign%")
        self.assertEqual(ip, "127.0.0.1",
                         "对端 127.0.0.1 不在 10.0.0.0/8 里，头一律忽略")
        self.assertIn("8.8.4.4", detail or "")      # 但照样留痕

        login_with(self.foreign, "lock-foreign", BAD_PASSWORD,
                   {"CF-Connecting-IP": "8.8.4.4"})
        self.assertEqual(self.foreign.failure_ip("lock-foreign"), "127.0.0.1")

    # --- ⑥ 受信之后，正常登录仍然走得通（别为了记账把门锁死）---
    def test_login_still_works_behind_a_trusted_proxy(self):
        status, headers, _body = login_with(
            self.cidr, "owner1", PASSWORD, {"CF-Connecting-IP": "8.8.4.4"})
        self.assertEqual(status, 302, "受信反代后面照样能正常登录")
        self.assertIn(view_auth.SESSION_COOKIE, set_cookies(headers))
        ip, _detail = self.cidr.audit_ip("login_ok", "%")
        self.assertEqual(ip, "8.8.4.4", "登录成功的审计也该记真实访客 IP")


if __name__ == "__main__":
    unittest.main(verbosity=2)
