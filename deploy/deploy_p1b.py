#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1b：① 给三台云机开 OpenClaw 网关的 OpenAI 兼容端点（备份+重启+验证）
        ② 用各自真实的网关地址/token 重建 agents.json 并下发
        ③ 真实后端跨机端到端验证

刚哥 2026-09-19 已批准（"可以"）。
"""
import json
import pathlib
import re
import subprocess
import sys
import time

SECRETS = pathlib.Path(r"<home>\AppData\Local\hermes\secrets\agent-team")
AGENT_DIR = pathlib.Path(r"<repo>\agent-team")
LOCAL_CFG = pathlib.Path(r"<home>\agent-team")
PY = r"<home>/AppData/Roaming/uv/python/cpython-3.11-windows-x86_64-none/python.exe"

NODES = {
    "lobster": {"ssh": "lobster", "ip": "<host>", "agents": ["lobster"]},
    "top001": {"ssh": "top001", "ip": "<node-ip>", "agents": ["top001"]},
    "quantum": {"ssh": "quantum", "ip": "<node-ip>", "agents": ["quantum"]},
}
BACKUP = "/root/backup-openclaw-20260919"

PATCH_REMOTE = r'''
import json, os, shutil, sys, time
p = "/root/.openclaw/openclaw.json"
os.makedirs("__BACKUP__", exist_ok=True)
dst = "__BACKUP__/openclaw.json.bak"
if not os.path.exists(dst):
    shutil.copy2(p, dst)
d = json.load(open(p, encoding="utf-8"))
before = json.dumps(d.get("http"), ensure_ascii=False)
http = d.setdefault("http", {})
eps = http.setdefault("endpoints", {})
cc = eps.setdefault("chatCompletions", {})
cc["enabled"] = True
tmp = p + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(d, f, ensure_ascii=False, indent=2)
os.replace(tmp, p)
d2 = json.load(open(p, encoding="utf-8"))
print("BEFORE_http=%s" % before)
print("AFTER_enabled=%s" % d2["http"]["endpoints"]["chatCompletions"]["enabled"])
print("GW_PORT=%s" % (d2.get("gateway", {}).get("port") or 18789))
print("GW_TOKEN_LEN=%d" % len((d2.get("gateway", {}).get("auth", {}) or {}).get("token") or ""))
'''


def run(host, cmd, stdin=None, timeout=120):
    p = subprocess.run(["ssh", "-o", "ConnectTimeout=10", host, cmd],
                       input=stdin, capture_output=True, timeout=timeout)
    clean = lambda b: b.decode("utf-8", "replace").replace("\r", "")
    err = clean(p.stderr)
    err = "\n".join(l for l in err.splitlines() if "post-quantum" not in l and "store now" not in l
                    and "may need to be upgraded" not in l and "openssh.com/pq" not in l)
    return p.returncode, clean(p.stdout), err


def local(args, stdin=None, timeout=180):
    p = subprocess.run(args, input=stdin, capture_output=True, timeout=timeout)
    return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")


def phase_a():
    print("=" * 70)
    print("A. 开 OpenAI 兼容端点（备份 + 改 3 行 + 重启网关 + 真调验证）")
    info = {}
    for name, meta in NODES.items():
        host = meta["ssh"]
        print(f"--- {name} ({host})")
        body = PATCH_REMOTE.replace("__BACKUP__", BACKUP)
        rc, out, err = run(host, "python3 -", body.encode())
        if rc != 0:
            print("  ✗ 改配置失败:", err.strip()[:300])
            return False
        for line in out.strip().splitlines():
            print("   ", line.strip())
        m_port = re.search(r"GW_PORT=(\d+)", out)
        port = int(m_port.group(1)) if m_port else 18789
        # 重启网关
        rc, out2, err2 = run(host, "systemctl restart openclaw-gateway && echo RESTARTED", timeout=180)
        print("   重启:", out2.strip() or err2.strip()[:200])
        # 等端口就绪（最长 90s）
        ready = False
        for _ in range(30):
            rc, o, _ = run(host, f"ss -lnt | grep -qE ':{port}\\b' && echo UP", timeout=30)
            if "UP" in o:
                ready = True
                break
            time.sleep(3)
        print(f"   监听 {port}: {'就绪 ✓' if ready else '未就绪 ✗'}")
        # 取 token（不打印）
        rc, tok, _ = run(host, "python3 -c \"import json;print(json.load(open('/root/.openclaw/openclaw.json',encoding='utf-8'))['gateway']['auth']['token'],end='')\"")
        tok = tok.strip()
        # 真实调用
        body = json.dumps({"model": "openclaw/default",
                           "messages": [{"role": "user", "content": "自检：一句话说明你是谁"}],
                           "max_tokens": 80}, ensure_ascii=False)
        rc, out3, err3 = run(
            host,
            f"curl -s -m 90 -o /tmp/cc.json -w '%{{http_code}}' -X POST "
            f"-H 'Authorization: Bearer {tok}' -H 'Content-Type: application/json' "
            f"--data-binary @- http://127.0.0.1:{port}/v1/chat/completions; echo; head -c 300 /tmp/cc.json",
            stdin=body.encode(), timeout=180)
        code = out3.strip().splitlines()[0] if out3.strip() else "?"
        preview = " ".join(out3.strip().splitlines()[1:])[:200]
        ok = code.strip() == "200"
        print(f"   POST /v1/chat/completions → {code} {'✓' if ok else '✗'}")
        print(f"   返回: {preview}")
        # 飞书桥存活：网关 active + 日志里 feishu 无报错
        rc, o4, _ = run(host, "systemctl is-active openclaw-gateway; journalctl -u openclaw-gateway --since '3 min ago' --no-pager 2>/dev/null | grep -icE 'feishu|lark' || true")
        print("   网关 active + 日志 feishu 相关行数:", o4.strip().replace("\n", " | "))
        info[name] = {"port": port, "token": tok, "cc_ok": ok}
        if not ok:
            print("   ! 该机端点未通，后续 e2e 会失败，继续跑完再看")
    pathlib.Path("/tmp").mkdir(exist_ok=True)
    json.dump(info, open(r"<home>\AppData\Local\Temp\node_gw.json", "w"), ensure_ascii=False)
    return True


def build_agent_cfg(name, meta, info):
    """按真实网关地址/token 重建 agents.json"""
    if name in info:
        port, token = info[name]["port"], info[name]["token"]
    else:
        port, token = 18789, ""
    return {
        "node_id": name,
        "host": meta["ip"],
        "port": 9920,
        "agents": {
            meta["agents"][0]: {
                "backend": f"http://127.0.0.1:{port}/v1",
                "model": "openclaw/default",
                "api_key": token,
                "description": f"{name} 助手（云）",
                "skills": ["general"],
            }
        },
    }


def phase_b(info):
    print("=" * 70)
    print("B. 重建并下发 agents.json（用各机真实网关端口/token）")
    for name, meta in NODES.items():
        cfg = build_agent_cfg(name, meta, info)
        (SECRETS / f"agents-{name}.json").write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        rc, out, err = run(meta["ssh"],
                           "cat > <deploy-dir>/agents.json && systemctl restart a2a-svc && sleep 2 && systemctl is-active a2a-svc && echo WROTE")
        print(f"  {name}: agents.json 已下发, a2a-svc = {out.strip() or err.strip()[:200]}")
    # 本机：hermes / openclaw-nb / codex
    local_gw = json.load(open(r"<home>\.openclaw\openclaw.json", encoding="utf-8"))
    gw_tok = local_gw["gateway"]["auth"]["token"]
    bridge = json.load(open(r"<home>\.openclaw\agent-bridge\backends.json", encoding="utf-8"))
    hermes_tok = bridge["backends"]["hermes"]["token"]
    think = {
        "node_id": "thinkbook",
        "host": "<node-ip>",
        "port": 9920,
        "agents": {
            "hermes": {"backend": "http://127.0.0.1:8642/v1", "model": "hermes-agent",
                       "api_key": hermes_tok, "description": "Hermes 总控", "skills": ["orchestrate"]},
            "openclaw-nb": {"backend": "http://127.0.0.1:18789/v1", "model": "openclaw/default",
                            "api_key": gw_tok, "description": "笔记本 OpenClaw", "skills": ["openclaw"]},
            "codex": {"backend": "http://127.0.0.1:18789/v1", "model": "openclaw/codex",
                      "api_key": gw_tok, "description": "Codex 编码代理", "skills": ["code"]},
        },
    }
    (SECRETS / "agents-thinkbook.json").write_text(json.dumps(think, ensure_ascii=False, indent=2),
                                                   encoding="utf-8")
    (LOCAL_CFG / "agents.json").write_text(json.dumps(think, ensure_ascii=False, indent=2), encoding="utf-8")
    local(["schtasks", "/end", "/tn", "AgentTeamA2ASvc"])
    time.sleep(1)
    rc, out, err = local(["schtasks", "/run", "/tn", "AgentTeamA2ASvc"])
    time.sleep(3)
    rc, out, err = local(["curl", "-s", "-m", "6", "http://127.0.0.1:9920/healthz"])
    print("  笔记本 agents.json 已更新, healthz =", out.strip() or err.strip()[:200])
    return True


def phase_c():
    print("=" * 70)
    print("C. 真实后端跨机端到端（不是 stub）")
    tok_local = (LOCAL_CFG / "client-token").read_text(encoding="utf-8").strip()
    cases = [
        ("笔记本→龙虾", ["ssh", "lobster"],
         "curl -s -m 120 -X POST -H 'Authorization: Bearer $(cat <deploy-dir>/client-token)' "
         "-H 'Content-Type: application/json' -d '{\"text\":\"自检：用一句话说明你是谁\",\"timeout_s\":110}' "
         "http://<host>:9920/a2a/lobster/messages"),
        ("笔记本→Top001", ["ssh", "top001"],
         "curl -s -m 120 -X POST -H 'Authorization: Bearer $(cat <deploy-dir>/client-token)' "
         "-H 'Content-Type: application/json' -d '{\"text\":\"自检：用一句话说明你是谁\",\"timeout_s\":110}' "
         "http://<node-ip>:9920/a2a/top001/messages"),
    ]
    for label, ssh_args, cmd in cases:
        rc, out, err = local(ssh_args + [cmd], timeout=200)
        print(f"  {label}: {out.strip()[:300] or err.strip()[:200]}")
    # 反向：云机 → 笔记本（hermes）
    rc, out, err = local(["ssh", "lobster",
                          "curl -s -m 120 -X POST -H 'Authorization: Bearer $(cat <deploy-dir>/client-token)' "
                          "-H 'Content-Type: application/json' -d '{\"text\":\"自检：用一句话说明你是谁\",\"timeout_s\":110}' "
                          "http://<node-ip>:9920/a2a/hermes/messages"], timeout=200)
    print(f"  龙虾→笔记本(hermes): {out.strip()[:300] or err.strip()[:200]}")


if __name__ == "__main__":
    ok = phase_a()
    if ok:
        info = json.load(open(r"<home>\AppData\Local\Temp\node_gw.json"))
        phase_b(info)
        phase_c()
    print("=" * 70)
    print("P1b 结束")
