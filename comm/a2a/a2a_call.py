#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""a2a_call.py —— A2A 客户端（纯标准库，零依赖，Python 3.9+）

给团队成员/运维手工调用别的成员用。身份靠 token 文件，不靠请求体里的 from。

用法：
    # 1) 探活（不需要 token）
    python a2a_call.py --base http://<NODE_IP>:9920 healthz

    # 2) 取 Agent Card（不需要 token）
    python a2a_call.py --base http://<NODE_IP>:9920 card top001

    # 3) 派活（需要 token 文件）
    python a2a_call.py --base http://<NODE_IP>:9920 call top001 \
        --token-file <TOKEN_FILE> \
        --text "把 /tmp/x.log 的最后 20 行发我"

    # 4) 列本机对外提供的 agent（需要 token）
    python a2a_call.py --base http://<NODE_IP>:9920 agents --token-file ./client-token

环境变量：A2A_TOKEN 可直接给 token（优先于 --token-file）。
退出码：0 成功；1 服务端返回非 200；2 参数/网络错误。
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_TIMEOUT = 300.0


def _token(args):
    if os.environ.get("A2A_TOKEN"):
        return os.environ["A2A_TOKEN"].strip()
    if args.token_file:
        with open(args.token_file, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    return ""


def _request(method, url, token="", body=None, timeout=DEFAULT_TIMEOUT):
    """返回 (状态码, 原始文本)。网络层错误抛出。"""
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        try:
            return resp.getcode(), resp.read().decode("utf-8", "replace")
        finally:
            resp.close()
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, exc.read().decode("utf-8", "replace")
        finally:
            exc.close()


COMMON_OPTS = ("--base", "--token-file", "--timeout", "--json")


def split_common(argv):
    """把公共参数从任意位置摘出来（子命令前/后都行），返回 (opts, rest)。

    不依赖 argparse 的 parents —— subparser 的默认值会覆盖父解析器已解析的值，且
    required 在子解析器里会重复强制。手工摘取最可靠。
    """
    opts = {"base": None, "token_file": None, "timeout": DEFAULT_TIMEOUT, "json": False}
    rest = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        name, eq, inline = arg.partition("=")
        if name in COMMON_OPTS:
            if eq:
                value = inline
                consumed = 1
            else:
                if i + 1 >= len(argv):
                    raise SystemExit("缺少参数值: " + name)
                value = argv[i + 1]
                consumed = 2
            if name == "--base":
                opts["base"] = value
            elif name == "--token-file":
                opts["token_file"] = value
            elif name == "--timeout":
                opts["timeout"] = float(value)
            else:
                opts["json"] = True
            i += consumed
            continue
        rest.append(arg)
        i += 1
    return opts, rest


def main(argv=None):
    opts, rest = split_common(list(sys.argv[1:] if argv is None else argv))

    parser = argparse.ArgumentParser(description="A2A client (stdlib only)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("healthz")
    sub.add_parser("agents")
    p_card = sub.add_parser("card")
    p_card.add_argument("agent")

    p_call = sub.add_parser("call")
    p_call.add_argument("agent")
    p_call.add_argument("--text", required=True)
    p_call.add_argument("--session", default=None)

    args = parser.parse_args(rest)
    if not opts["base"]:
        parser.error("必须提供 --base（例如 --base http://<NODE_IP>:9920）")
    base = opts["base"].rstrip("/")
    args.json = opts["json"]
    args.timeout = opts["timeout"]
    args.token_file = opts["token_file"]

    if args.cmd == "healthz":
        url, method, body = base + "/healthz", "GET", None
    elif args.cmd == "agents":
        url, method, body = base + "/agents", "GET", None
    elif args.cmd == "card":
        url = base + "/a2a/%s/.well-known/agent-card.json" % args.agent
        method, body = "GET", None
    else:
        url = base + "/a2a/%s/messages" % args.agent
        method = "POST"
        body = {"text": args.text, "session_id": args.session}
        if args.timeout:
            body["timeout_s"] = args.timeout

    token = _token(args)
    if args.cmd in ("call", "agents") and not token:
        sys.stderr.write("需要 token：--token-file 或环境变量 A2A_TOKEN\n")
        return 2

    try:
        code, text = _request(method, url, token=token, body=body, timeout=args.timeout)
    except Exception as exc:
        sys.stderr.write("请求失败: %s\n" % exc)
        return 2

    if args.json:
        sys.stdout.write(text + "\n")
    else:
        try:
            data = json.loads(text)
        except Exception:
            sys.stdout.write(text + "\n")
            data = None
        if isinstance(data, dict):
            if data.get("reply") is not None:
                sys.stdout.write(data["reply"].rstrip() + "\n")
            else:
                sys.stdout.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")

    if code != 200:
        sys.stderr.write("HTTP %d\n" % code)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
