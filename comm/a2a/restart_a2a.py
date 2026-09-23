#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""restart_a2a.py —— 重启本机 A2A 服务（a2a_svc.py:9920），保持原有命令行不变。

为什么要脚本化：这个服务是用 pythonw.exe 手工起的，PID 一变命令行就丢；
重启后必须用**完全相同的参数**拉起来，否则 teamd 那边的 a2a_base 会连不上。
本脚本先按端口找到现进程、读出它的完整命令行、原样重启，避免参数走样。

路径口径（2026-09-23）：本脚本**零硬编码路径** —— 解释器 / 脚本 / 配置 / 会话档案
全部来自同目录的 a2a.env；该文件里的变量会注入子进程环境（a2a_svc.py 靠
A2A_SESSIONS_JSON 找会话档案，靠 A2A_ALLOW_ANON 之类开关调试）。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(HERE, "a2a.env")
SVC_REL = "a2a_svc.py"
# DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP：脱离控制台、独立进程组，与手工起等价
DETACHED = 0x00000008 | 0x00000200


def load_env(path):
    """读 KEY=VALUE 配置；返回 dict，文件不存在返回 None。"""
    if not os.path.isfile(path):
        return None
    cfg = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            cfg[key.strip()] = val.strip().strip('"').strip("'")
    return cfg


def ps(cmd):
    return subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                          capture_output=True, text=True).stdout.strip()


def find_pid(port):
    out = ps("(Get-NetTCPConnection -LocalPort %d -State Listen -ErrorAction SilentlyContinue "
             "| Select-Object -First 1).OwningProcess" % port)
    try:
        return int(out)
    except ValueError:
        return None


def cmdline_of(pid):
    return ps("(Get-CimInstance Win32_Process -Filter \"ProcessId=%d\").CommandLine" % pid) or ""


def main():
    cfg = load_env(ENV_FILE)
    if cfg is None:
        print("[restart_a2a] 缺少配置文件：%s（所有路径都在里面，先建它）" % ENV_FILE)
        return 2

    port = int(cfg.get("A2A_PORT") or 9920)
    host = cfg.get("A2A_HOST") or "127.0.0.1"
    pyw = cfg.get("A2A_PYTHONW") or sys.executable
    script = cfg.get("A2A_SVC_SCRIPT")
    if not script:
        print("[restart_a2a] a2a.env 里缺 A2A_SVC_SCRIPT")
        return 2
    default_args = [script, "--host", host, "--port", str(port)]
    if cfg.get("A2A_AGENTS_JSON"):
        default_args += ["--config", cfg["A2A_AGENTS_JSON"]]
    if cfg.get("A2A_PEERS_JSON"):
        default_args += ["--peers", cfg["A2A_PEERS_JSON"]]

    old = find_pid(port)
    args = list(default_args)
    if old:
        cl = cmdline_of(old)
        print("[restart_a2a] 现进程 PID=%s" % old)
        print("[restart_a2a] 原命令行: %s" % cl)
        # 从原命令行里切出参数（去掉解释器与脚本本身），保证参数与前次完全一致
        if cl and SVC_REL in cl:
            tail = cl.split(SVC_REL, 1)[1].strip()
            args = [script] + (tail.split() if tail else default_args[1:])
        subprocess.run(["taskkill", "/PID", str(old), "/F"], capture_output=True, text=True)
        time.sleep(2)
    else:
        print("[restart_a2a] 没找到在听的进程，按 a2a.env 的参数起")

    child_env = dict(os.environ)
    child_env.update({k: v for k, v in cfg.items() if k.startswith("A2A_")})

    log_fh = None
    stdout = subprocess.DEVNULL
    stderr = subprocess.DEVNULL
    if cfg.get("A2A_LOG_FILE"):
        log_fh = open(cfg["A2A_LOG_FILE"], "a", encoding="utf-8")
        stdout = stderr = log_fh
        print("[restart_a2a] 日志落盘: %s" % cfg["A2A_LOG_FILE"])

    print("[restart_a2a] 启动: %s %s" % (pyw, " ".join(args)))
    subprocess.Popen([pyw] + args, cwd=HERE, env=child_env,
                     stdout=stdout, stderr=stderr, creationflags=DETACHED, close_fds=True)
    time.sleep(3)

    new = find_pid(port)
    ok = False
    # 注意：服务只绑 Tailscale 地址（如 100.x），绑的不是 127.0.0.1 ——
    # 拿 127.0.0.1 探活会被"积极拒绝"，那是误报，不是没起来。
    for probe_host in (host, "127.0.0.1"):
        try:
            with urllib.request.urlopen("http://%s:%d/healthz" % (probe_host, port), timeout=5) as r:
                body = r.read().decode("utf-8", "replace")
                print("[restart_a2a] /healthz @%s -> %s %s" % (probe_host, r.status, body[:200]))
                ok = r.status == 200
                if ok:
                    break
        except Exception as exc:
            print("[restart_a2a] /healthz @%s 失败: %s: %s" % (probe_host, type(exc).__name__, exc))

    if log_fh:
        log_fh.close()
    print("[restart_a2a] 新 PID=%s  healthz_ok=%s" % (new, ok))
    return 0 if (new and ok) else 1


if __name__ == "__main__":
    sys.exit(main())
