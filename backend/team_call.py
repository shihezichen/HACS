#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
team_call.py —— 点名要答复（NATS request/reply，纯标准库，复用 nats_client.py）

走 `a2a.v1.req.<member>`，由对方的 teamd 转成本机 A2A 调用，答复原样回投到 reply 收件箱。
载荷与 A2A 平面一致：{text, session_id?, timeout_s}。

用法：
    python team_call.py --member <MEMBER_ID> --text "..." \
        --nats-host <NODE_IP> --user <MEMBER_ID> --password-file <PASS_FILE> \
        [--session-id meet-xxx] [--timeout 120] [--text-file f.txt] [--dry-run] [--json]

陷阱提醒：这是**同步**点名，等对方 run 跑完；调用方一超时，对端 run 会被 abort。
长任务/要对方改配置的活请改走异步（对方 CLI 独立会话），别在这里等。
中文正文别经 bash argv 传（GBK 会乱码）—— 用 --text-file 或从 Python 调。
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
MAX_TIMEOUT_S = 300.0


def main():
    # type: () -> int
    ap = argparse.ArgumentParser(description="点名要答复(NATS request/reply)")
    ap.add_argument("--member", required=True, help="目标成员 id, 如 <MEMBER_ID>")
    ap.add_argument("--text", default=None)
    ap.add_argument("--text-file", default=None, help="从 UTF-8 文件读正文(中文优先用这个)")
    ap.add_argument("--session-id", default=None, help="透传给对方以续上下文(可空)")
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("--nats-host", default=DEFAULT_HOST)
    ap.add_argument("--nats-port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--user", required=True)
    ap.add_argument("--password-file", required=True)
    ap.add_argument("--dry-run", action="store_true", help="只打印将发送的载荷,不连 NATS")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.text_file:
        with open(args.text_file, "r", encoding="utf-8-sig") as f:
            text = f.read().strip()
    else:
        text = (args.text or "").strip()
    if not text and not args.dry_run:
        print("FAIL 正文为空: 给 --text 或 --text-file", file=sys.stderr)
        return 4

    subject = "a2a.v1.req.%s" % args.member
    timeout = max(0.1, min(float(args.timeout), MAX_TIMEOUT_S))
    payload = {"text": text, "timeout_s": timeout}
    if args.session_id:
        payload["session_id"] = args.session_id
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    if args.dry_run:
        print(json.dumps({"subject": subject, "bytes": len(raw),
                          "payload": payload}, ensure_ascii=False))
        return 0

    try:
        password = read_secret_file(args.password_file)
    except NatsError as exc:
        print("FAIL 读口令失败: %s" % exc, file=sys.stderr)
        return 4

    errs = []  # type: list

    def on_err(line):
        errs.append(line)

    c = NatsClient(args.nats_host, args.nats_port, user=args.user,
                   password=password, name="team-call", on_error=on_err)
    try:
        c.connect()
    except Exception as exc:
        print("FAIL 连不上 NATS %s:%d -> %s" % (args.nats_host, args.nats_port, exc),
              file=sys.stderr)
        return 3

    t0 = time.time()
    try:
        reply = c.request(subject, raw, timeout=timeout + 10.0)
    finally:
        try:
            c.close()
        except Exception:
            pass
    elapsed = round(time.time() - t0, 1)

    if reply is None:
        out = {"ok": False, "member": args.member, "elapsed_s": elapsed,
               "error": "timeout 或对端无应答", "nats_errors": errs[-5:]}
        print(json.dumps(out, ensure_ascii=False) if args.json
              else "TIMEOUT %ss 无应答: %s%s" % (elapsed, args.member,
                  ("  nats -ERR: " + errs[-1]) if errs else ""))
        return 3

    text_out = reply.decode("utf-8", "replace")
    if args.json:
        try:
            print(json.dumps({"ok": True, "member": args.member, "elapsed_s": elapsed,
                              "reply": json.loads(text_out)}, ensure_ascii=False))
        except Exception:
            print(json.dumps({"ok": True, "member": args.member, "elapsed_s": elapsed,
                              "reply_raw": text_out}, ensure_ascii=False))
    else:
        print("OK %ss 来自 %s:" % (elapsed, args.member))
        print(text_out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
