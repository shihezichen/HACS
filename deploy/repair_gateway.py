#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抢修：把配置里被我放错层级的 http 键挪到 gateway.http，恢复三台网关。

错误：写成了顶层 "http" → openclaw 校验 "<root>: Unrecognized key: http" → 网关 exit 78。
正解：gateway.http.endpoints.chatCompletions.enabled = true
"""
import json
import re
import subprocess
import time

NODES = {"lobster": "lobster", "top001": "top001", "quantum": "quantum"}

FIX_REMOTE = r'''
import json, os
p = "/root/.openclaw/openclaw.json"
d = json.load(open(p, encoding="utf-8"))
removed = d.pop("http", None)          # 删掉我错放的顶层 http
gw = d.setdefault("gateway", {})
http = gw.setdefault("http", {})
eps = http.setdefault("endpoints", {})
cc = eps.setdefault("chatCompletions", {})
cc["enabled"] = True
tmp = p + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(d, f, ensure_ascii=False, indent=2)
os.replace(tmp, p)
d2 = json.load(open(p, encoding="utf-8"))
print("removed_toplevel_http=%s" % bool(removed))
print("toplevel_has_http=%s" % ("http" in d2))
print("gateway_http=%s" % json.dumps(d2.get("gateway", {}).get("http"), ensure_ascii=False))
print("gw_port=%s" % (d2.get("gateway", {}).get("port") or 18789))
print("gw_auth_mode=%s" % (d2.get("gateway", {}).get("auth", {}) or {}).get("mode"))
print("gw_token_len=%d" % len(((d2.get("gateway", {}).get("auth", {}) or {}).get("token") or "")))
'''


def run(host, cmd, stdin=None, timeout=180):
    p = subprocess.run(["ssh", "-o", "ConnectTimeout=10", host, cmd],
                       input=stdin, capture_output=True, timeout=timeout)
    clean = lambda b: b.decode("utf-8", "replace").replace("\r", "")
    err = clean(p.stderr)
    err = "\n".join(l for l in err.splitlines()
                    if not any(k in l for k in ("post-quantum", "store now", "may need to be upgraded", "openssh.com/pq")))
    return p.returncode, clean(p.stdout), err


for name, host in NODES.items():
    print("=" * 66)
    print(f"### 抢修 {name} ({host})")
    rc, out, err = run(host, "python3 -", FIX_REMOTE.encode())
    if rc != 0:
        print("  ✗ 改配置失败:", err.strip()[:400] or out.strip()[:400])
        continue
    for line in out.strip().splitlines():
        print("   ", line.strip())
    port = int(re.search(r"gw_port=(\d+)", out).group(1))

    # 清掉重启循环熔断标记：先 reset-failed 再启动
    run(host, "systemctl reset-failed openclaw-gateway; systemctl restart openclaw-gateway && echo RESTARTED", timeout=200)
    ready = False
    for _ in range(24):
        rc2, o2, _ = run(host, f"ss -lnt | grep -qE ':{port}\\b' && echo UP", timeout=30)
        if "UP" in o2:
            ready = True
            break
        time.sleep(3)
    rc3, o3, _ = run(host, "systemctl is-active openclaw-gateway", timeout=30)
    print(f"   网关: active={o3.strip()} | 监听 {port}: {'✓' if ready else '✗ 仍未起'}")

    # 取 token 真调 /v1/chat/completions
    rc4, tok, _ = run(host, "python3 -c \"import json;d=json.load(open('/root/.openclaw/openclaw.json',encoding='utf-8'));print((d.get('gateway',{}).get('auth',{}) or {}).get('token') or '',end='')\"")
    tok = tok.strip()
    body = json.dumps({"model": "openclaw/default",
                       "messages": [{"role": "user", "content": "ping"}],
                       "max_tokens": 30}).encode()
    rc5, out5, _ = run(host,
                       f"curl -s -m 60 -o /tmp/cc.json -w '%{{http_code}}' -X POST "
                       f"-H 'Authorization: Bearer {tok}' -H 'Content-Type: application/json' "
                       f"--data-binary @- http://127.0.0.1:{port}/v1/chat/completions; echo; head -c 200 /tmp/cc.json",
                       stdin=body, timeout=150)
    parts = out5.strip().splitlines()
    print(f"   POST /v1/chat/completions → {parts[0] if parts else '?'} {'✓' if parts and parts[0].strip()=='200' else '✗'}")
    if len(parts) > 1:
        print("   返回:", " ".join(parts[1:])[:180])

    # 飞书通道健康（openclaw status）
    rc6, out6, err6 = run(host,
                          "export PATH=$PATH:/root/.nvm/versions/node/v24.20.0/bin; openclaw status 2>&1 | head -30",
                          timeout=120)
    print("   --- openclaw status ---")
    for line in (out6 or err6).strip().splitlines()[:20]:
        print("     ", line.strip()[:150])
print("=" * 66)
print("抢修结束")
