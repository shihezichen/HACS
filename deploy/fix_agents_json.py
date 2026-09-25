#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""修补：把 agents.json 用正文（stdin）正确下发，重启 a2a-svc 并自检。

上次的错：ssh "cat > file" 没给 stdin → 文件被截成空 → 服务 exit 2 崩溃循环。
"""
import hashlib
import json
import os
import pathlib
import subprocess
import time

# PHASE2_TODO: 待节点注入 env 后删除
AGENT_TEAM_SECRETS = os.environ.get("AGENT_TEAM_SECRETS") or r"<home>\AppData\Local\hermes\secrets\agent-team"
# PHASE2_TODO: 待节点注入 env 后删除
AGENT_TEAM_RUNTIME_DIR = os.environ.get("AGENT_TEAM_RUNTIME_DIR") or r"<home>\agent-team"
# PHASE2_TODO: 待节点注入 env 后删除
AGENT_TEAM_ETC_DIR = os.environ.get("AGENT_TEAM_ETC_DIR") or "<deploy-dir>"
SECRETS = pathlib.Path(AGENT_TEAM_SECRETS)
NODES = {"lobster": "lobster", "top001": "top001"}


def run(host, cmd, stdin=None, timeout=180):
    p = subprocess.run(["ssh", "-o", "ConnectTimeout=10", host, cmd],
                       input=stdin, capture_output=True, timeout=timeout)
    clean = lambda b: b.decode("utf-8", "replace").replace("\r", "")
    err = "\n".join(l for l in clean(p.stderr).splitlines()
                    if not any(k in l for k in ("post-quantum", "store now", "may need", "openssh.com/pq")))
    return p.returncode, clean(p.stdout), err


print("=" * 66)
print("1) 正确下发 agents.json（走 stdin）+ 重启 + 自检")
for name, host in NODES.items():
    payload = (SECRETS / f"agents-{name}.json").read_bytes()
    print(f"--- {name}: 本地文件 {len(payload)} B")
    rc, out, err = run(host, f"cat > {AGENT_TEAM_ETC_DIR}/agents.json && wc -c < {AGENT_TEAM_ETC_DIR}/agents.json && python3 -c \"import json;d=json.load(open('{AGENT_TEAM_ETC_DIR}/agents.json'));print('JSON OK, agents=',list(d['agents'].keys()))\" && systemctl restart a2a-svc && sleep 3 && systemctl is-active a2a-svc && ss -lnt | grep -c 9920",
                       payload)
    print("   远端:", (out.strip() or err.strip())[:300])

print("=" * 66)
print("2) token/peers 一致性核对（只比对哈希，不打印明文）")
peers = json.loads((SECRETS / "peers.json").read_text(encoding="utf-8"))
local_peers = json.loads(pathlib.Path(AGENT_TEAM_RUNTIME_DIR + r"\peers.json").read_text(encoding="utf-8"))
print("  本机 peers.json 条目:", len(local_peers))
for name, host in NODES.items():
    rc, tok, _ = run(host, f"cat {AGENT_TEAM_ETC_DIR}/client-token")
    tok = tok.strip()
    h = hashlib.sha256(tok.encode()).hexdigest()
    found_local = h in local_peers.values()
    found_master = h in peers.values()
    print(f"  {name}: token 长度 {len(tok)} | sha256 在 <HOST> peers 中={found_local} | 在主凭据表中={found_master} | 对应成员={[k for k,v in local_peers.items() if v==h]}")
rc, tok_nb, _ = run("lobster", f"cat {AGENT_TEAM_ETC_DIR}/client-token")  # 占位，避免未用变量
print("=" * 66)
print("3) 远端 peers.json 与主凭据表一致？")
for name, host in NODES:
    pass
for name, host in NODES.items():
    rc, o, _ = run(host, f"python3 -c \"import json;d=json.load(open('{AGENT_TEAM_ETC_DIR}/peers.json'));print(len(d), sorted(d.keys()))\"")
    print(f"  {name} 远端 peers: {o.strip()}")
