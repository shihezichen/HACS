#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
team_roster.py —— 在线成员表（纯标准库，复用 nats_client.py）

做法：订阅 `team.hb.>` 窗口 N 秒，收集各成员心跳（teamd 每 30s 发一条
`team.hb.<member>`，载荷含 member/members/host/ts/uptime_s/version/pid），
窗口结束后按成员去重输出。

用法：
    python team_roster.py --nats-host <NODE_IP> --user <MEMBER_ID> \
        --password-file <PASS_FILE> [--wait 35] [--json]

退出码：0 = 有人在线；3 = 窗口内一个心跳都没收到（判定中枢/凭据侧问题）。
"""

import argparse
import json
import os
import sys
import time

# 批① 起被搬件按绝对包名 import，故把仓根一并放进搜索路径（本目录仍在最前）。
for _hacs_p in (os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                os.path.dirname(os.path.abspath(__file__))):
    sys.path.insert(0, _hacs_p)
from node.l1_transport.nats_client import NatsClient, NatsError, read_secret_file  # noqa: E402
from node.l0_host import node_profile as _npf  # noqa: E402  L0 节点档案：宿主事实唯一真源（NATS 中枢；env 优先）

# PHASE2_TODO: 待节点注入 env 后删除
DEFAULT_HOST = os.environ.get("AGENT_TEAM_NATS_HOST") or _npf.nats_host_default()
DEFAULT_PORT = 9930


def main():
    # type: () -> int
    ap = argparse.ArgumentParser(description="团队在线成员表(订阅 team.hb.>)")
    ap.add_argument("--nats-host", default=DEFAULT_HOST)
    ap.add_argument("--nats-port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--user", required=True)
    ap.add_argument("--password-file", required=True)
    ap.add_argument("--wait", type=float, default=35.0, help="收心跳窗口秒(默认 35)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    try:
        password = read_secret_file(args.password_file)
    except NatsError as exc:
        print("FAIL 读口令失败: %s" % exc, file=sys.stderr)
        return 4

    seen = {}  # type: dict
    errs = []  # type: list

    def on_hb(payload, reply=""):
        try:
            obj = json.loads(payload.decode("utf-8"))
        except Exception:
            return
        m = obj.get("member")
        if not m:
            return
        prev = seen.get(m)
        if prev is None or obj.get("ts", 0) >= prev.get("ts", 0):
            seen[m] = obj

    def on_err(line):
        errs.append(line)

    c = NatsClient(args.nats_host, args.nats_port, user=args.user,
                   password=password, name="team-roster", on_error=on_err)
    try:
        c.connect()
    except Exception as exc:
        print("FAIL 连不上 NATS %s:%d -> %s" % (args.nats_host, args.nats_port, exc),
              file=sys.stderr)
        return 3

    c.subscribe("team.hb.>", on_hb, sid="hb")
    c.flush(timeout=5.0)
    deadline = time.time() + max(1.0, args.wait)
    while time.time() < deadline:
        time.sleep(0.25)
    try:
        c.close()
    except Exception:
        pass

    rows = []
    now = int(time.time())
    for m in sorted(seen.keys()):
        o = seen[m]
        rows.append({
            "member": m,
            "host": o.get("host"),
            "age_s": now - int(o.get("ts", now)),
            "uptime_s": o.get("uptime_s"),
            "version": o.get("version"),
            "pid": o.get("pid"),
            "members": o.get("members"),
        })

    if args.json:
        print(json.dumps({"ok": bool(rows), "count": len(rows), "members": rows,
                          "nats_errors": errs[-5:]}, ensure_ascii=False))
    else:
        if not rows:
            print("在线成员: 0（窗口 %.0fs 内未收到 team.hb.> 心跳）" % args.wait)
            for line in errs[-5:]:
                print("  nats -ERR: %s" % line)
        else:
            print("在线成员 %d 个（窗口 %.0fs, 中枢 %s:%d）:"
                  % (len(rows), args.wait, args.nats_host, args.nats_port))
            print("%-14s %-18s %8s %10s %8s" % ("成员", "主机", "心跳龄s", "在线时长s", "版本"))
            for r in rows:
                print("%-14s %-18s %8s %10s %8s"
                      % (r["member"], r["host"], r["age_s"], r["uptime_s"], r["version"]))
    return 0 if rows else 3


if __name__ == "__main__":
    sys.exit(main())
