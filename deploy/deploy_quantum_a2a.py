#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""deploy_quantum_a2a.py —— 让量子可被点名（补上它缺的那半边：A2A 端点 + 入站订阅）

背景（2026-09-19 实测）：量子的 teamd 单元写死 `--no-inbound --no-admin`，机上也**没有**
a2a 端点（9920 无监听），所以群发点名它会 100s 超时。刚哥拍板：给它装上；owner 不执行就
由 Hermes 登入执行（本脚本即此举）。

装什么：
  1. <deploy-dir>/a2a_svc.py            纯标准库 A2A 端点，绑 <node-ip>:9920（只 tailnet）
  2. <deploy-dir>/agents.json           quantum 的本机 agent 定义（backend 指本机 openclaw 网关）
  3. <deploy-dir>/peers.json            token 的 sha256 反查表（决定谁调得动它）
  4. <deploy-dir>/client-token          quantum 自己的明文 token，0600，只落盘不进日志
  5. agent-team-a2a.service                常驻
  6. agent-team-teamd.service              **改**：去掉 --no-inbound --no-admin，
                                            加 --a2a-base/--token-file/--admin-port

凭据处理：token 从本机 secrets 目录读，经 stdin 落到远端 0600 文件；全程不打印明文、
不进命令行、不进日志（红线）。
"""

import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SECRETS = r"<home>\AppData\Local\hermes\secrets\agent-team"
NODE = "quantum"
IP = "<node-ip>"
NATS_HOST = "<host>"

A2A_UNIT = """[Unit]
Description=Agent Team A2A service endpoint (quantum, port 9920, tailnet only)
After=network-online.target tailscaled.service
Wants=network-online.target tailscaled.service

[Service]
Type=simple
ExecStart=/usr/bin/python3 <deploy-dir>/a2a_svc.py --host %(ip)s --port 9920 \\
  --config <deploy-dir>/agents.json --peers <deploy-dir>/peers.json
Restart=always
RestartSec=3
User=root
StandardOutput=append:/var/log/agent-team/a2a-svc.log
StandardError=append:/var/log/agent-team/a2a-svc.log

[Install]
WantedBy=multi-user.target
""" % {"ip": IP}

TEAMD_UNIT = """[Unit]
Description=agent-team teamd (quantum) - NATS 客户端/心跳/点名应答
After=network-online.target tailscaled.service
Wants=network-online.target

[Service]
Type=simple
User=root
# 2026-09-19：去掉 --no-inbound --no-admin，让量子**可被点名**（原来纯出站，群发点名它必超时）。
# 入站请求由本机 a2a_svc(9920) 兜住 → 转本机 openclaw 网关。
ExecStart=/usr/bin/python3 <deploy-dir>/teamd.py --member quantum \\
  --nats-host %(nats)s --nats-port 9930 --user quantum \\
  --password-file /etc/nats/quantum.pass \\
  --a2a-base http://%(ip)s:9920 --token-file <deploy-dir>/client-token \\
  --admin-port 9960
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
""" % {"nats": NATS_HOST, "ip": IP}


def run(script, stdin_bytes=None, timeout=180):
    p = subprocess.run(["ssh", "-o", "ConnectTimeout=10", NODE, script],
                       input=stdin_bytes, capture_output=True, timeout=timeout)
    return (p.returncode,
            p.stdout.decode("utf-8", "replace"),
            p.stderr.decode("utf-8", "replace"))


def clean(text):
    return "\n".join(l for l in text.splitlines()
                     if "post-quantum" not in l and "store now" not in l and "may need" not in l)


def push(path_local, path_remote, mode, data=None):
    if data is None:
        with open(path_local, "rb") as fh:
            data = fh.read().replace(b"\r\n", b"\n")
    rc, out, err = run("cat > %s && chmod %s %s && wc -c < %s"
                       % (path_remote, mode, path_remote, path_remote), data)
    return rc, out.strip()


def main():
    tokens = json.load(open(os.path.join(SECRETS, "tokens.json"), encoding="utf-8"))
    qtoken = str(tokens[NODE]).strip()
    agents = open(os.path.join(SECRETS, "agents-%s.json" % NODE), "rb").read().replace(b"\r\n", b"\n")
    peers = open(os.path.join(SECRETS, "peers.json"), "rb").read().replace(b"\r\n", b"\n")

    print("### 1/6 目录与权限")
    rc, out, err = run("mkdir -p <deploy-dir> <deploy-dir> /var/log/agent-team && "
                       "chmod 700 <deploy-dir> && echo READY")
    print("  " + clean(out).strip()[:120])

    print("### 2/6 推 a2a_svc.py / agents.json / peers.json")
    rc, out = push(os.path.join(HERE, "a2a_svc.py"), "<deploy-dir>/a2a_svc.py", "0755")
    print("  a2a_svc.py -> %s 字节" % out)
    rc, out = push(None, "<deploy-dir>/agents.json", "0600", agents)
    print("  agents.json -> %s 字节" % out)
    rc, out = push(None, "<deploy-dir>/peers.json", "0600", peers)
    print("  peers.json -> %s 字节" % out)

    print("### 3/6 落 quantum 自己的 token（stdin，0600）")
    rc, out = push(None, "<deploy-dir>/client-token", "0600", (qtoken + "\n").encode("utf-8"))
    print("  client-token -> %s 字节" % out)

    print("### 4/6 装两个 systemd 单元（分两次写，一次一个文件）")
    rc, out, err = run("cat > /etc/systemd/system/agent-team-a2a.service && "
                       "systemctl daemon-reload && echo A2A_UNIT_OK",
                       A2A_UNIT.encode("utf-8"))
    print("  a2a.service: " + clean(out).strip()[:120])
    rc, out, err = run("cat > /etc/systemd/system/agent-team-teamd.service && "
                       "systemctl daemon-reload && systemctl restart agent-team-teamd && "
                       "systemctl is-active agent-team-teamd", TEAMD_UNIT.encode("utf-8"))
    print("  teamd: " + clean(out).strip()[:200])

    print("### 5/6 起 a2a 端点")
    rc, out, err = run("systemctl enable --now agent-team-a2a 2>&1 | tail -1; sleep 2; "
                       "systemctl is-active agent-team-a2a; "
                       "python3 -m py_compile <deploy-dir>/a2a_svc.py && echo COMPILE_OK; "
                       "ss -lntp | grep 9920 || echo NO_LISTEN")
    print("  " + clean(out).strip()[:300])

    print("### 6/6 自检：本机 A2A healthz / 后端可达 / 订阅")
    rc, out, err = run("curl -s -o /dev/null -w 'healthz=%{http_code}\\n' http://%(ip)s:9920/healthz; "
                       "curl -s -o /dev/null -w 'openclaw_gw=%{http_code}\\n' http://127.0.0.1:18789/v1/models; "
                       "systemctl is-active agent-team-teamd agent-team-a2a" % {"ip": IP})
    print("  " + clean(out).strip()[:300])
    return 0


if __name__ == "__main__":
    sys.exit(main())
