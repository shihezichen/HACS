#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cloudflare 隧道入口管理（快速隧道 / 命名隧道两档）

用途：把龙虾上只读看板 9941 通过 Cloudflare 隧道暴露到公网（https，真证书，
不用开端口、不用备案）。**认证必须先就绪**（看板跑 --mode public），本脚本只负责隧道。

用法（在龙虾上以 root 跑）：
  python3 tunnel.py quick   # 快速隧道：零账号，秒级给出 https 地址（重启会换地址）
  python3 tunnel.py named --hostname board.example.com --token-file /root/.cf/tunnel.token
  python3 tunnel.py status
  python3 tunnel.py stop

说明：quick 模式用 cloudflared 的 trycloudflare 临时域名，域名形如
https://<随机>.trycloudflare.com ；named 模式需要 Cloudflare 账号里建好隧道并拿到
token（token 走文件，不进命令行、不进日志）。
"""

import argparse
import os
import pathlib
import subprocess
import sys

TARGET = "http://127.0.0.1:9942"      # 看板·公网实例（强制登录）。**绝不能指 9941**
                                     # ——9941 是 tailnet 免登录实例，指过去等于把看板裸奔到公网。
BIN = "/usr/local/bin/cloudflared"
STATE = pathlib.Path("/var/run/agent-team-tunnel.json")
LOG = pathlib.Path("/var/log/agent-team-tunnel.log")


def preflight() -> bool:
    """先认证后暴露：目标必须对「未登录请求」给出 302 → /login，否则拒绝开隧道。"""
    proc = subprocess.run(
        ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code} %{redirect_url}", TARGET + "/"],
        capture_output=True, text=True, timeout=10)
    head = (proc.stdout or "").strip()
    code, _, url = head.partition(" ")
    if code == "302" and "/login" in url:
        print("[preflight] 目标 %s 未登录被挡（302 → %s）✓" % (TARGET, url))
        return True
    print("[preflight] 拒绝开隧道：%s 对未登录请求返回 %r —— 期望 302 且跳 /login。\n"
          "            说明目标不是强制登录的公网实例（9942），或认证没生效。"
          % (TARGET, head), file=sys.stderr)
    return False


def _run(args, **kw):
    print("+ " + " ".join(args))
    return subprocess.run(args, check=False, **kw)


def cmd_quick(_args) -> int:
    if not preflight():
        return 2
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("ab") as fh:
        return _run([BIN, "tunnel", "--no-autoupdate", "--url", TARGET], stdout=fh,
                    stderr=fh, stdin=subprocess.DEVNULL).returncode


def cmd_named(args) -> int:
    if not preflight():
        return 2
    token = pathlib.Path(args.token_file).read_text(encoding="utf-8").strip()
    if not token:
        print("token 文件是空的", file=sys.stderr)
        return 2
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("ab") as fh:
        return _run([BIN, "tunnel", "--no-autoupdate", "--url", TARGET,
                     "run", "--token", token], stdout=fh, stderr=fh,
                    stdin=subprocess.DEVNULL).returncode


def cmd_status(_args) -> int:
    ps = subprocess.run(["pgrep", "-af", "cloudflared"], capture_output=True, text=True)
    print(ps.stdout.strip() or "cloudflared 未运行")
    if LOG.exists():
        print("最近日志：")
        print("\n".join(LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-5:]))
    return 0


def cmd_stop(_args) -> int:
    return _run(["pkill", "-f", "cloudflared tunnel"]).returncode


def main() -> int:
    ap = argparse.ArgumentParser(description="看板公网入口（Cloudflare 隧道）管理")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("quick", help="零账号快速隧道（地址会变）")
    named = sub.add_parser("named", help="命名隧道（固定域名，需要 token）")
    named.add_argument("--hostname", required=True)
    named.add_argument("--token-file", required=True, help="隧道 token 文件（不走命令行）")
    sub.add_parser("status")
    sub.add_parser("stop")
    args = ap.parse_args()
    if not os.path.exists(BIN):
        print("没装 cloudflared（/usr/local/bin/cloudflared）", file=sys.stderr)
        return 2
    return {"quick": cmd_quick, "named": cmd_named,
            "status": cmd_status, "stop": cmd_stop}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
