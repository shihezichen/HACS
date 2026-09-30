#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把**真实登录态**下 https://example.com/ 返回的那一页 HTML 原样存下来，
供渲染截图/存档。临时账号口令机上现生成、只走 stdin，不打印、不进日志。"""
import re
import secrets
import subprocess
import sys
import tempfile
import os

# 批① 起被搬件按绝对包名 import，故把仓根一并放进搜索路径（本目录仍在最前）。
for _hacs_p in (os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                os.path.dirname(os.path.abspath(__file__))):
    sys.path.insert(0, _hacs_p)

from node.l0_host import node_paths as _np  # noqa: E402  L0 平台层：宿主默认值唯一出处（env 优先）

BASE = "https://example.com"
USER = "snapcheck_" + secrets.token_hex(3)
PASS = secrets.token_urlsafe(20)
AUTH = os.path.join(_np.runtime_dir(), "view_auth.py")
JAR = tempfile.mktemp(suffix=".jar")
OUT = sys.argv[1] if len(sys.argv) > 1 else "/tmp/portal_snapshot.html"


def run(args, stdin=None):
    return subprocess.run([sys.executable, AUTH] + args, input=stdin,
                          capture_output=True, timeout=60)


def curl(*args):
    last = (1, "", "")
    for _ in range(4):
        p = subprocess.run(["curl", "-4", "-sS", "--max-time", "25"] + list(args),
                           capture_output=True, timeout=60)
        last = (p.returncode, p.stdout.decode("utf-8", "replace"),
                p.stderr.decode("utf-8", "replace"))
        if last[0] == 0:
            return last
        import time
        time.sleep(2)
    return last


try:
    r = run(["adduser", "--user", USER, "--role", "viewer", "--password-stdin"],
            stdin=PASS.encode())
    if r.returncode != 0:
        print("adduser failed:", (r.stdout + r.stderr).decode()[:200])
        raise SystemExit(1)
    rc, page, err = curl("-c", JAR, BASE + "/login")
    tok = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    rc, out, err = curl("-c", JAR, "-b", JAR, "-o", "/dev/null", "-w", "%{http_code}",
                        "-X", "POST",
                        "--data-urlencode", "action=login",
                        "--data-urlencode", "next=/",
                        "--data-urlencode", "csrf=" + tok,
                        "--data-urlencode", "user=" + USER,
                        "--data-urlencode", "password=" + PASS,
                        BASE + "/login")
    print("login http:", out.strip())
    rc, portal, err = curl("-b", JAR, BASE + "/")
    open(OUT, "w", encoding="utf-8").write(portal)
    print("saved %s bytes=%d cards=%s" % (OUT, len(portal),
          re.findall(r'<a class="card[^"]*"\s+href="([^"]+)"', portal)))
    # 顺带存一份登录页（未登录那一屏），便于对照截图
    rc, loginpage, err = curl(BASE + "/login")
    open(OUT.replace(".html", "_login.html"), "w", encoding="utf-8").write(loginpage)
    print("saved login page bytes=%d" % len(loginpage))
finally:
    d = run(["disable", "--user", USER])
    print("disable:", d.returncode, (d.stdout + d.stderr).decode().strip()[:100])
    try:
        os.unlink(JAR)
    except OSError:
        pass
