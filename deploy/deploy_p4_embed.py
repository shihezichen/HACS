#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P4 部署：把嵌入能力下发到龙虾黑板服务（同步文件 → 重启单元 → 自报版本）。
用法：python deploy_p4_embed.py            # 正常下发
      python deploy_p4_embed.py --dry-run  # 只看要传什么
"""
import hashlib
import os
import pathlib
import subprocess
import sys

# PHASE2_TODO: 待节点注入 env 后删除
LOCAL = pathlib.Path(os.environ.get("AGENT_TEAM_REPO_DIR") or r"<repo>\agent-team")
# PHASE2_TODO: 待节点注入 env 后删除
REMOTE = os.environ.get("AGENT_TEAM_RUNTIME_DIR") or "<deploy-dir>"
# PHASE2_TODO: 待节点注入 env 后删除
AGENT_TEAM_NODES = dict(kv.split("=", 1) for kv in (os.environ.get("AGENT_TEAM_NODES")
                        or "lobster=<host>").split(","))
FILES = ["embedder.py", "blackboard_svc.py", "backfill_embeddings.py"]
UNIT = "agent-team-blackboard"
SSH = ["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes", "lobster"]


def md5_local(p):
    return hashlib.md5(p.read_bytes()).hexdigest()


def ssh(cmd, timeout=120):
    r = subprocess.run(SSH + [cmd], capture_output=True, timeout=timeout)
    return r.returncode, r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace")


def main():
    dry = "--dry-run" in sys.argv
    missing = [f for f in FILES if not (LOCAL / f).exists()]
    if missing:
        print("缺文件，先别部署:", missing)
        return 1

    for f in FILES:
        src = LOCAL / f
        print("  %-26s %s  %d B" % (f, md5_local(src)[:12], src.stat().st_size))
    if dry:
        print("[dry-run] 未执行任何写操作")
        return 0

    print("== 1) 传文件 ==")
    for f in FILES:
        r = subprocess.run(["scp", "-o", "ConnectTimeout=10", str(LOCAL / f), "lobster:%s/%s" % (REMOTE, f)],
                           capture_output=True, timeout=180)
        print("   scp %-26s rc=%d" % (f, r.returncode))
        if r.returncode != 0:
            print("   STDERR:", r.stderr.decode("utf-8", "replace")[:200])
            return 2

    print("== 2) 远端 md5 核对（防传输变形）==")
    for f in FILES:
        rc, out, _ = ssh("md5sum %s/%s" % (REMOTE, f))
        remote = out.split()[0] if out.split() else "?"
        ok = "✓" if remote == md5_local(LOCAL / f) else "✗ 不一致"
        print("   %-26s 本地=%s 远端=%s %s" % (f, md5_local(LOCAL / f)[:12], remote[:12], ok))

    print("== 3) 语法自检（远端解释器真编译）==")
    rc, out, err = ssh("cd %s && python3 -m py_compile %s && echo COMPILE_OK" % (REMOTE, " ".join(FILES)))
    print("   ", (out + err).strip()[:220])

    print("== 4) 重启单元 ==")
    rc, out, err = ssh("systemctl restart %s && sleep 3 && systemctl is-active %s" % (UNIT, UNIT))
    print("   ", out.strip(), err.strip()[:200])

    print("== 5) 服务自报（版本 + 嵌入状态）==")
    rc, out, err = ssh("curl -s -m 8 http://%s:9940/healthz" % AGENT_TEAM_NODES["lobster"])
    print("   ", out.strip()[:400])

    print("== 6) 监听面（应仍只绑 tailnet）==")
    rc, out, err = ssh("ss -lntp | grep 9940")
    print("   ", out.strip()[:200])
    return 0


if __name__ == "__main__":
    sys.exit(main())
