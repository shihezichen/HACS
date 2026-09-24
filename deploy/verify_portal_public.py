#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""webPortal 端到端验收（在龙虾上跑，curl 走公网域名，自动 IPv4/IPv6 回退）：
  1. 机上现生成口令，建一个临时 viewer 账号（明文不进聊天/日志）；
  2. 走公网 https://example.com/login 真登录（CSRF + cookie）；
  3. 断言 / 返回的 HTML 里是**卡片墙**，且两张卡都是可点的 <a href>：
       卡1 → /board（原来的看板）  卡2 → https://chat.example.com/（群聊/对话台）
  4. 断言 /board = 200（看板本尊还在）、chat 匿名打 /api/* 被拒（401）；
  5. 收尾：停用临时账号、删掉本地 HTML 副本。
只打印状态与断言结果，绝不打印口令。
"""
import re
import secrets
import subprocess
import sys
import tempfile
import os

BASE = "https://example.com"
CHAT = "https://chat.example.com/"
USER = "portalcheck_" + secrets.token_hex(3)
PASS = secrets.token_urlsafe(20)
AUTH = "<deploy-dir>/view_auth.py"
JAR = tempfile.mktemp(suffix=".jar")
OUT = "/tmp/portal_accept.html"

fails = []


def check(name, cond, extra=""):
    print("%-46s %s %s" % (name, "PASS" if cond else "FAIL", extra))
    if not cond:
        fails.append(name)


def run(args, stdin=None):
    return subprocess.run([sys.executable, AUTH] + args, input=stdin,
                          capture_output=True, timeout=60)


def curl(*args):
    """返回 (exit_code, stdout, stderr)。强制 IPv4 + 失败重试（国内解析器缓存偶发抽风）。"""
    last = (1, "", "")
    for _ in range(4):
        p = subprocess.run(["curl", "-4", "-sS", "--max-time", "25"] + list(args),
                           capture_output=True, timeout=60)
        last = (p.returncode, p.stdout.decode("utf-8", "replace"),
                p.stderr.decode("utf-8", "replace"))
        if last[0] == 0:
            return last
        import time as _t
        _t.sleep(2)
    return last


try:
    # --- 1. 临时账号 ---
    run(["disable", "--user", USER])          # 幂等：先停用可能存在的旧账号
    r = run(["adduser", "--user", USER, "--role", "viewer", "--password-stdin"],
            stdin=PASS.encode())
    check("adduser 临时账号", r.returncode == 0, (r.stdout + r.stderr).decode().strip()[:160])

    # --- 2. 公网登录 ---
    rc, page, err = curl("-c", JAR, BASE + "/login")
    m = re.search(r'name="csrf" value="([^"]+)"', page)
    check("GET /login 200 且拿到 CSRF", rc == 0 and bool(m), err.strip()[:120])
    if not m:
        raise SystemExit(1)
    rc, out, err = curl("-c", JAR, "-b", JAR, "-o", "/dev/null",
                      "-w", "%{http_code}", "-X", "POST",
                      "--data-urlencode", "action=login",
                      "--data-urlencode", "next=/",
                      "--data-urlencode", "csrf=" + m.group(1),
                      "--data-urlencode", "user=" + USER,
                      "--data-urlencode", "password=" + PASS,
                      BASE + "/login")
    check("POST /login 登录成功 (302)", rc == 0 and out.strip() == "302", "code=%s" % out.strip())

    # --- 3. 首页 = 卡片墙 ---
    rc, portal, err = curl("-b", JAR, BASE + "/")
    check("GET / 200（登录后）", rc == 0 and "团队门户 · example.com" in portal,
          err.strip()[:120])
    open(OUT, "w", encoding="utf-8").write(portal)
    check("首页含卡片1『团队看板』", "团队看板" in portal)
    check("首页含卡片2『人机对话台』", "人机对话台" in portal)
    hrefs = re.findall(r'<a class="card[^"]*"\s+href="([^"]+)"', portal)
    print("    可点卡片 href:", hrefs)
    check("卡片1 可点且指向 /board", any(h.endswith("/board") for h in hrefs))
    check("卡片2 可点且指向 chat.example.com",
          any("chat.example.com" in h for h in hrefs))
    check("没有未点亮的 locked 卡", 'data-locked="1"' not in portal)

    # --- 4. 看板本尊 & 对话台 ---
    rc, out, err = curl("-b", JAR, "-o", "/dev/null", "-w", "%{http_code}", BASE + "/board")
    check("GET /board = 200（看板本尊）", rc == 0 and out.strip() == "200", "code=%s" % out.strip())

    rc, body, err = curl(CHAT)
    check("GET chat.example.com/ = 200 且是对话台页面",
          rc == 0 and "人机对话台" in body, err.strip()[:120])

    rc, out, err = curl("-o", "/dev/null", "-w", "%{http_code}", CHAT + "api/sessions")
    check("匿名打 chat /api/sessions 被拒（401）", rc == 0 and out.strip() == "401",
          "code=%s" % out.strip())

    rc, out, err = curl("-o", "/dev/null", "-w", "%{http_code}", BASE + "/")
    check("匿名打 apex / 被导向登录（302）", rc == 0 and out.strip() == "302",
          "code=%s" % out.strip())
finally:
    d = run(["disable", "--user", USER])
    print("disable 临时账号:", d.returncode, (d.stdout + d.stderr).decode().strip()[:120])
    for f in (JAR, OUT):
        try:
            os.unlink(f)
        except OSError:
            pass

print("\n==== %s (%d checks failed) ====" % ("ALL PASS" if not fails else "FAILED", len(fails)))
sys.exit(1 if fails else 0)
