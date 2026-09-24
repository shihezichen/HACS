#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""team_call.py —— 通过 NATS 点名呼叫团队成员（群里喊话式「打电话」）

区别于 a2a_call.py：a2a_call 是**点对点 HTTP**（要知道对方 IP:9920 + 自己的 token）；
team_call 是**走中枢**（只连 NATS 9930，由对方常驻的 teamd 兜住并回结果），
成员换 IP、对方在 NATS 后面都不影响。

用法：
  python team_call.py --member hermes --text "你是谁？" \
      --nats-host <host> --user lobster --password-file /root/.nats-lobster.pass

选项：
  --member <id>        被叫成员（subject = a2a.v1.req.<member>）
  --text <str>         要送达的内容
  --session-id <str>   同一会话 id → 对方记得上下文；不传则一次性问答
  --timeout <sec>      等回包上限（默认 120，最大 300）
  --json               打印对方回的原始 JSON（默认只打印 reply 字段）
退出码：0 成功应答；1 超时/无应答；2 参数或连接错误；3 对方回了错误结构
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nats_client import NatsClient, NatsError, read_secret_file  # noqa: E402

MAX_TIMEOUT = 300.0
DEFAULT_TIMEOUT = 120.0


def main(argv=None):
    # type: (object) -> int
    ap = argparse.ArgumentParser(description="通过 NATS 点名呼叫团队成员")
    ap.add_argument("--member", required=True, help="被叫成员 id，如 hermes")
    ap.add_argument("--text", required=True, help="要送达的内容")
    ap.add_argument("--session-id", default=None, help="会话 id（同一 id 对方有上下文）")
    ap.add_argument("--nats-host", required=True, help="NATS 中枢地址")
    ap.add_argument("--nats-port", type=int, default=9930, help="NATS 端口（默认 9930）")
    ap.add_argument("--user", required=True, help="自己的 NATS 用户名（通常与自己的成员名同名）")
    ap.add_argument("--password-file", required=True, help="自己的 NATS 口令文件")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                    help="等回包上限秒（默认 120，最大 300）")
    ap.add_argument("--json", action="store_true", help="打印原始 JSON 回包")
    args = ap.parse_args(argv)

    timeout = max(1.0, min(float(args.timeout), MAX_TIMEOUT))
    subject = "a2a.v1.req.%s" % args.member
    try:
        password = read_secret_file(args.password_file)
    except Exception as exc:
        sys.stderr.write("读口令文件失败：%s\n" % exc)
        return 2

    envelope = {"text": args.text, "timeout_s": timeout}
    if args.session_id:
        envelope["session_id"] = args.session_id
    raw = json.dumps(envelope, ensure_ascii=False).encode("utf-8")

    client = NatsClient(host=args.nats_host, port=args.nats_port,
                        user=args.user, password=password)
    try:
        client.connect()
    except Exception as exc:
        sys.stderr.write("连不上 NATS 中枢 %s:%d：%s\n" % (args.nats_host, args.nats_port, exc))
        return 2

    try:
        # 回包上限 = 对方处理上限 + 余量（对方还会走一次自身 A2A）
        reply = client.request(subject, raw, timeout=timeout + 15.0)
    except NatsError as exc:
        sys.stderr.write("点名无应答（对方未挂 teamd 或已离线）：%s\n" % exc)
        return 1
    except Exception as exc:
        sys.stderr.write("请求异常：%s\n" % exc)
        return 2
    finally:
        try:
            client.close()
        except Exception:
            pass

    if reply is None:
        sys.stderr.write("点名超时（%ss 内无应答）—— 确认 <member> 的 teamd 在跑、"
                         "且 subject 正确：%s\n" % (timeout, subject))
        return 1

    text_reply = reply.decode("utf-8", "replace")
    try:
        obj = json.loads(text_reply)
    except ValueError:
        # 对方回的不是 JSON（老版本或不认识的实现）→ 原样打印
        sys.stdout.write(text_reply + "\n")
        return 0

    if args.json:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False, indent=2) + "\n")
    else:
        if obj.get("ok") and obj.get("reply") is not None:
            sys.stdout.write(str(obj.get("reply")) + "\n")
        else:
            sys.stderr.write("对方回了错误：%s\n"
                             % json.dumps(obj, ensure_ascii=False))
    if not obj.get("ok"):
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
