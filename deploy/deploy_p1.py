#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1 部署：把 a2a_svc 推到龙虾/Top001（systemd）+ 收敛 firewalld zone。

只做规划里 P1 的云机部分（笔记本另开）。幂等：可重复跑。
所有凭据从本机 secrets 目录读，不落到命令行/日志。
"""

import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# PHASE2_TODO: 待节点注入 env 后删除
SECRETS = os.environ.get("AGENT_TEAM_SECRETS") or r"<home>\AppData\Local\hermes\secrets\agent-team"
# PHASE2_TODO: 待节点注入 env 后删除
NODES = dict(kv.split("=", 1) for kv in (os.environ.get("AGENT_TEAM_NODES")
             or "lobster=<host>,top001=<node-ip>").split(","))
# PHASE2_TODO: 待节点注入 env 后删除
AGENT_TEAM_RUNTIME_DIR = os.environ.get("AGENT_TEAM_RUNTIME_DIR") or "<deploy-dir>"
# PHASE2_TODO: 待节点注入 env 后删除
AGENT_TEAM_ETC_DIR = os.environ.get("AGENT_TEAM_ETC_DIR") or "<deploy-dir>"
# PHASE2_TODO: 待节点注入 env 后删除
AGENT_TEAM_LOG_DIR = os.environ.get("AGENT_TEAM_LOG_DIR") or "/var/log/agent-team"
SVC = os.path.join(HERE, "a2a_svc.py")

UNIT = f"""[Unit]
Description=Agent Team A2A service endpoint (port 9920, tailnet only)
After=network-online.target tailscaled.service
Wants=network-online.target tailscaled.service

[Service]
Type=simple
ExecStart=/usr/bin/python3 {AGENT_TEAM_RUNTIME_DIR}/a2a_svc.py --host {{ip}} --port 9920 --config {AGENT_TEAM_ETC_DIR}/agents.json --peers {AGENT_TEAM_ETC_DIR}/peers.json
Restart=always
RestartSec=3
User=root
StandardOutput=append:{AGENT_TEAM_LOG_DIR}/a2a-svc.log
StandardError=append:{AGENT_TEAM_LOG_DIR}/a2a-svc.log

[Install]
WantedBy=multi-user.target
"""


def run(host, script, stdin_bytes=None, timeout=120):
    p = subprocess.run(["ssh", "-o", "ConnectTimeout=10", host, script],
                       input=stdin_bytes, capture_output=True, timeout=timeout)
    return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")


def push(host, local_path, remote_path, mode):
    with open(local_path, "rb") as fh:
        data = fh.read()
    return run(host, "cat > %s && chmod %s %s && wc -c < %s" % (remote_path, mode, remote_path, remote_path), data)


def main():
    tokens = json.load(open(os.path.join(SECRETS, "tokens.json"), encoding="utf-8"))
    peers_path = os.path.join(SECRETS, "peers.json")
    peers = open(peers_path, "rb").read()

    ok_all = True
    for node, ip in NODES.items():
        print("=" * 70)
        print("### 部署 %s (%s)" % (node, ip))
        rc, out, err = run(node, f"mkdir -p {AGENT_TEAM_ETC_DIR} {AGENT_TEAM_RUNTIME_DIR} {AGENT_TEAM_LOG_DIR} && "
                                 f"chmod 700 {AGENT_TEAM_ETC_DIR} && echo READY")
        print("  目录准备:", out.strip() or err.strip())

        # 1) 代码
        rc, out, _ = push(node, SVC, AGENT_TEAM_RUNTIME_DIR + "/a2a_svc.py", "755")
        print("  推 a2a_svc.py:", out.strip() or "FAIL rc=%d" % rc)

        # 2) 本机配置 + peers（只含哈希）+ 自己的 token
        cfg = os.path.join(SECRETS, "agents-%s.json" % node)
        rc, out, _ = push(node, cfg, AGENT_TEAM_ETC_DIR + "/agents.json", "644")
        print("  推 agents.json:", out.strip() or "FAIL rc=%d" % rc)
        rc, out, _ = run(node, f"cat > {AGENT_TEAM_ETC_DIR}/peers.json && chmod 600 {AGENT_TEAM_ETC_DIR}/peers.json && wc -c < {AGENT_TEAM_ETC_DIR}/peers.json", peers)
        print("  推 peers.json:", out.strip() or "FAIL rc=%d" % rc)
        tok = tokens[node].encode()
        rc, out, _ = run(node, f"cat > {AGENT_TEAM_ETC_DIR}/client-token && chmod 600 {AGENT_TEAM_ETC_DIR}/client-token && wc -c < {AGENT_TEAM_ETC_DIR}/client-token", tok)
        print("  推 client-token:", out.strip() or "FAIL rc=%d" % rc)

        # 3) systemd 单元
        unit = UNIT.format(ip=ip).encode()
        rc, out, err = run(node, "cat > /etc/systemd/system/a2a-svc.service && systemctl daemon-reload && echo UNITOK", unit)
        print("  写单元:", out.strip() or err.strip())

        # 4) firewalld：tailscale0 归 trusted zone（新端口在 Tailnet 内不被拦）
        zone_script = ("Z=$(firewall-cmd --get-zone-of-interface=tailscale0 2>/dev/null); "
                       "echo \"before=$Z\"; "
                       "if [ \"$Z\" != \"trusted\" ]; then "
                       "  firewall-cmd --permanent --zone=trusted --add-interface=tailscale0 >/dev/null && firewall-cmd --reload >/dev/null && echo CHANGED; "
                       "else echo NOCHANGE; fi; "
                       "firewall-cmd --get-zone-of-interface=tailscale0")
        rc, out, err = run(node, zone_script)
        print("  firewalld zone:", out.strip().replace("\n", " | ") or err.strip())

        # 5) 起服务 + 本机自检
        rc, out, err = run(node, "systemctl enable --now a2a-svc >/dev/null 2>&1; sleep 2; "
                                 "echo active=$(systemctl is-active a2a-svc); "
                                 "ss -lntp | grep -c 9920 | sed 's/^/listen9920=/'; "
                                 "curl -s -m 5 http://127.0.0.1:9920/healthz; echo")
        print("  启动自检:", out.strip().replace("\n", " | ") or err.strip())
        if "active=active" not in out:
            ok_all = False

    print("=" * 70)
    print("云机部署" + ("全部成功" if ok_all else "存在失败，需要复查"))
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
