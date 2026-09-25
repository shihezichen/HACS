#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""deploy_auth.py —— 把「看板认证」(E1 产物) 部署到龙虾，并起双实例。

设计（为什么是两个实例，别合并）：
  * 9941 继续绑 tailnet(<NODE_IP>)，`--mode tailnet` —— 内网 Agent 照旧免登录直连，
    这条路**一点不动**（旧行为零回归）。
  * 新增 9942 只绑 127.0.0.1，`--mode public` —— 专供 cloudflared 回连；公网流量全部
    从这里进，必须登录（127.0.0.1 在 public 模式下**不在免登录白名单**，白名单只认
    tailnet <tailnet-cidr>）。
这样「先认证后暴露」在部署形态上就是硬隔离：公网那根管子接的是一个强制登录的实例。

幂等：可重复跑。所有凭据只在机器人/服务端本地，不进命令行、不进日志。
用法：
  python deploy_auth.py            # 全流程
  python deploy_auth.py --check    # 只做部署后自检，不推文件
"""

import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
HOST = "lobster"
STAGE = "/tmp/agent-team-auth"
# PHASE2_TODO: 待节点注入 env 后删除
OPT = os.environ.get("AGENT_TEAM_RUNTIME_DIR") or "<deploy-dir>"
UNIT_DIR = "/etc/systemd/system"
# PHASE2_TODO: 待节点注入 env 后删除
AGENT_TEAM_ETC_DIR = os.environ.get("AGENT_TEAM_ETC_DIR") or "<deploy-dir>"
# PHASE2_TODO: 待节点注入 env 后删除
AGENT_TEAM_NODES = dict(kv.split("=", 1) for kv in (os.environ.get("AGENT_TEAM_NODES")
                        or "lobster=<host>").split(","))
TAILNET_IP = AGENT_TEAM_NODES[HOST]
PUB_PORT = 9942
# PHASE2_TODO: 待节点注入 env 后删除
KEY_FILE = os.environ.get("AGENT_TEAM_SESSION_KEY_FILE") or AGENT_TEAM_ETC_DIR + "/view-session.key"

FILES = [
    ("view_auth.py", "0644"),
    # 阶段2 刀6 起 blackboard_view 的 HTTP 面走 L1 网关，这个件必须一起推：
    # 少了它 py_compile 照样过（编译不 import），但服务一重启就 ImportError。
    ("web_gateway.py", "0644"),
    ("blackboard_view.py", "0755"),
    ("sql/view_auth_schema.sql", "0644"),
    ("tests/test_view_auth.py", "0644"),
]

PUB_UNIT = """[Unit]
Description=Agent-Team blackboard view · 公网入口实例 (127.0.0.1:%(port)d, mode=public)
Documentation=file:%(opt)s/blackboard_view.py
After=network-online.target tailscaled.service agent-team-blackboard.service
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=%(opt)s
# 只绑回环：公网流量只能经 cloudflared 从 127.0.0.1 进来，这一层强制登录。
# tailnet 那条路不受影响（另一个实例 9941 --mode tailnet 照旧）。
ExecStart=/usr/bin/python3 %(opt)s/blackboard_view.py \\
  --host 127.0.0.1 \\
  --port %(port)d \\
  --mode public \\
  --trust-proxy 127.0.0.1 \\
  --upstream http://%(ip)s:9940 \\
  --token-file %(etc)s/client-token \\
  --refresh-seconds 30
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
Environment=PYTHONUNBUFFERED=1

NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
ProtectKernelTunables=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
LockPersonality=yes

[Install]
WantedBy=multi-user.target
""" % {"port": PUB_PORT, "opt": OPT, "ip": TAILNET_IP, "etc": AGENT_TEAM_ETC_DIR}

# 9941 那份照抄现网单元，只补一行 `--mode tailnet`（内网免登录这条行为一字不改）。
TAIL_UNIT = """[Unit]
Description=Agent-Team blackboard read-only view (port 9941)
Documentation=file:%(opt)s/blackboard_view.py
# 绑的是 tailnet 地址，必须等 tailscaled 把 100.x 挂上来再启动；
# 起早了也没事，靠 Restart=always 重试绑定（与 a2a-svc.service 同一套口径）。
After=network-online.target tailscaled.service agent-team-blackboard.service
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=%(opt)s
# --host 换成本机 tailnet 的 100.x 地址；--upstream 指向黑板服务的 9940。
# token 只从 --token-file 读进本进程内存，绝不会发给浏览器、也不会进日志。
ExecStart=/usr/bin/python3 %(opt)s/blackboard_view.py \\
  --host %(ip)s \\
  --port 9941 \\
  --mode tailnet \\
  --upstream http://%(ip)s:9940 \\
  --token-file %(etc)s/client-token \\
  --refresh-seconds 30
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
Environment=PYTHONUNBUFFERED=1

# 只读服务就按只读来收权限：整个文件系统只读挂载，不需要任何可写路径。
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
ProtectKernelTunables=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
LockPersonality=yes

[Install]
WantedBy=multi-user.target
""" % {"opt": OPT, "ip": TAILNET_IP, "etc": AGENT_TEAM_ETC_DIR}


def ssh(script, stdin_bytes=None, timeout=180):
    p = subprocess.run(["ssh", "-o", "ConnectTimeout=10", HOST, script],
                       input=stdin_bytes, capture_output=True, timeout=timeout)
    return (p.returncode,
            p.stdout.decode("utf-8", "replace"),
            p.stderr.decode("utf-8", "replace"))


def push(local_rel, remote_path, mode):
    with open(os.path.join(HERE, local_rel.replace("/", os.sep)), "rb") as fh:
        data = fh.read()
    data = data.replace(b"\r\n", b"\n")   # Windows CRLF 一律清掉，否则远程跑不动
    rc, out, err = ssh("mkdir -p %s && cat > %s && chmod %s %s && wc -c < %s"
                       % (os.path.dirname(remote_path), remote_path, mode,
                          remote_path, remote_path), data)
    return rc, out.strip(), err


def step(title):
    print("=" * 72)
    print("### " + title)


def main(argv=None):
    ap = argparse.ArgumentParser(description="部署看板认证（E1）到 <HOST>")
    ap.add_argument("--check", action="store_true", help="只做部署后自检")
    args = ap.parse_args(argv)
    ok = True

    if not args.check:
        step("1/6 推文件到 <HOST> %s" % STAGE)
        ssh("mkdir -p %s" % STAGE)
        for rel, mode in FILES:
            local_ok = os.path.exists(os.path.join(HERE, rel.replace("/", os.sep)))
            if not local_ok:
                print("  ⚠️ 本地缺 %s（跳过，稍后补推）" % rel)
                continue
            rc, out, err = push(rel, "%s/%s" % (STAGE, os.path.basename(rel)), mode)
            print("  %s %s -> %s 字节" % ("✅" if rc == 0 else "❌", rel, out))
            ok = ok and rc == 0

        step("2/6 装进 %s" % OPT)
        rc, out, err = ssh("cd %s && cp -f view_auth.py web_gateway.py blackboard_view.py %s/ && "
                           "mkdir -p %s/tests && cp -f test_view_auth.py %s/tests/ 2>/dev/null; "
                           "python3 -m py_compile %s/view_auth.py %s/web_gateway.py "
                           "%s/blackboard_view.py && echo COMPILE_OK"
                           % (STAGE, OPT, OPT, OPT, OPT, OPT, OPT))
        print("  " + (out.strip() or err.strip())[:300])
        ok = ok and ("COMPILE_OK" in out)

        step("3/6 落认证表（幂等 DDL，经 docker 里的 PG16）")
        rc, out, err = ssh(
            "docker exec -i agent-team-pg psql -U bb -d blackboard "
            "-v ON_ERROR_STOP=1 -f - 2>&1 | tail -10",
            stdin_bytes=open(os.path.join(HERE, "sql", "view_auth_schema.sql"), "rb").read())
        print("  " + (out.strip() or err.strip())[:500])
        ok = ok and rc == 0

        step("4/6 会话签名密钥（存在就不动，避免全场掉线）")
        rc, out, err = ssh("test -s %s && echo KEY_EXISTS || "
                           "(umask 077 && python3 %s/view_auth.py init-key --key-file %s && echo KEY_CREATED)"
                           % (KEY_FILE, OPT, KEY_FILE))
        print("  " + (out.strip() or err.strip())[:300])
        ok = ok and ("KEY_EXISTS" in out or "KEY_CREATED" in out)

        step("5/6 写两个单元（9941 tailnet 免登录 / 9942 公网强制登录）")
        rc1, out1, err1 = ssh(
            "cat > %s/agent-team-blackboard-view.service <<'EOF_TAIL'\n%s\nEOF_TAIL\n"
            "cat > %s/agent-team-blackboard-view-pub.service <<'EOF_PUB'\n%s\nEOF_PUB\n"
            "systemctl daemon-reload && echo UNIT_OK"
            % (UNIT_DIR, TAIL_UNIT.rstrip(), UNIT_DIR, PUB_UNIT.rstrip()))
        print("  " + (out1.strip() or err1.strip())[:300])
        ok = ok and ("UNIT_OK" in out1)

        step("6/6 重启两个实例")
        # 公网实例必须 restart 而不是 enable --now —— 单元已 active 时 enable --now 什么都不做，
        # 老进程会一直占着 9942 端口（旧命令行、旧代码），这正是「改了命令行却没生效」的坑。
        rc, out, err = ssh("systemctl enable agent-team-blackboard-view-pub >/dev/null 2>&1; "
                           "systemctl restart agent-team-blackboard-view; "
                           "systemctl restart agent-team-blackboard-view-pub; sleep 3; "
                           "systemctl is-active agent-team-blackboard-view agent-team-blackboard-view-pub; "
                           "echo '--- 运行命令行（要能看到 --mode/--trust-proxy）---'; "
                           "ps -eo pid,args | grep blackboard_view.py | grep -v grep")
        print("  " + (out.strip() or err.strip())[:300])

    step("自检")
    t_ip, pub = TAILNET_IP, PUB_PORT
    check = (
        "echo '-- tailnet 实例(" + str(9941) + ", 免登录) --'; "
        "curl -s -o /dev/null -w 'sessions=%{http_code}\\n' http://" + t_ip + ":9941/api/sessions?limit=1; "
        "echo '-- 公网实例(" + str(pub) + ", 必须登录) --'; "
        "curl -s -o /dev/null -w 'root=%{http_code} ' http://127.0.0.1:" + str(pub) + "/ ; "
        "curl -s -o /dev/null -w 'api=%{http_code}\\n' http://127.0.0.1:" + str(pub) + "/api/sessions?limit=1; "
        "echo '-- 监听 --'; ss -lntp | grep -E ':(" + str(9941) + "|" + str(pub) + ")'")
    rc, out, err = ssh(check)
    print(out.strip()[:800] or err.strip()[:400])
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
