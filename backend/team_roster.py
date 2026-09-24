#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""team_roster.py —— 谁在线：订阅 team.hb.> 收集各成员心跳，打印花名册

用法：
  python team_roster.py --nats-host <host> --user hermes \
      --password-file <home>/agent-team/nats-hermes.pass --seconds 35

说明：心跳 30s 一发，所以 --seconds 至少给 35 才保证不漏。只订阅、只读，不写任何东西。
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nats_client import NatsClient, read_secret_file  # noqa: E402


def main(argv=None):
    # type: (object) -> int
    ap = argparse.ArgumentParser(description="订阅 team.hb.> 打印在线成员")
    ap.add_argument("--nats-host", required=True)
    ap.add_argument("--nats-port", type=int, default=9930)
    ap.add_argument("--user", required=True)
    ap.add_argument("--password-file", required=True)
    ap.add_argument("--seconds", type=float, default=35.0, help="收集时长（默认 35，心跳周期 30s）")
    ap.add_argument("--json", action="store_true", help="逐条打印原始 JSON")
    args = ap.parse_args(argv)

    password = read_secret_file(args.password_file)
    seen = {}  # type: dict

    def on_hb(payload, reply=""):
        try:
            obj = json.loads(payload.decode("utf-8"))
        except Exception:
            return
        member = obj.get("member") or "?"
        seen[member] = obj

    client = NatsClient(host=args.nats_host, port=args.nats_port,
                        user=args.user, password=password)
    client.connect()
    client.subscribe("team.hb.>", on_hb, sid="hb")
    client.flush()
    try:
        deadline = time.time() + max(1.0, float(args.seconds))
        while time.time() < deadline:
            time.sleep(0.2)
    finally:
        try:
            client.close()
        except Exception:
            pass

    if args.json:
        for m in sorted(seen):
            print(json.dumps(seen[m], ensure_ascii=False))
        return 0

    if not seen:
        print("本窗口内没收到任何心跳（检查中枢是否在跑、口令是否正确、成员 teamd 是否起来）")
        return 1
    print("在线成员 %d 个（窗口 %.0fs）：" % (len(seen), args.seconds))
    for m in sorted(seen):
        o = seen[m]
        age = int(time.time() - int(o.get("ts") or 0))
        print("  %-12s host=%-10s uptime=%-6ss pid=%-7s version=%-5s 心跳距今 %ss"
              % (m, str(o.get("host"))[:10], o.get("uptime_s"), o.get("pid"),
                 o.get("version"), age))
    return 0


if __name__ == "__main__":
    sys.exit(main())
