#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""team_group.py —— 成员侧 CLI 的**协作域**：临时群 + 临时会议（L3）

刀15（阶段3 第二刀）从 L1 team.py 摘出。team.py 原本是「成员侧命令行客户端」，
却把群聊 / 会议这两个协作域压在通行层里 —— 判据 5（传输不含语义）下 L1 业务词
命中一直下不来。本件把这两块**逐字节原样**接过来：

  · 临时会议 meet：make_session_id / build_meeting_body / append_jsonl / cmd_meet
  · 会议回放 history：cmd_history
  · 临时群 group：_chat_call / _chat_member / _chat_error /
    cmd_group_say / cmd_group_pull / cmd_group_show

搬迁只换位置，不换行为：逻辑、字符串、顺序、退出码全不动。

依赖方向（L3 → L1 → L0，不反向）：
  · 通行层管道（身份 / 口令 / 去重 / 限并发扇出 / 单成员呼叫）取自 L1 team_cli_core；
  · 群消息走对话台 HTTP，会议走 NATS —— 两条出站都由本件按域语义组织，
    传输动作本身在 L1。
  · 进程入口 team.py 是组合根 C（只做 argv 解析 + 装配 + 分派 + 打印），
    **本件不得 import 它**。
"""

import json
import os
import random
import string
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import node_profile as _npf  # noqa: E402  L0 节点档案：宿主事实唯一真源（对话台基址；env 优先）

from team_cli_core import (  # noqa: E402
    TeamCliError,
    call_one,
    check_and_mark_dedup,
    clamp_timeout,
    infer_member,
    read_text,
    resolve_member_and_password,
    rollback_dedup,
    run_pool,
)

DEFAULT_MEETINGS_FILE = os.path.join(os.path.expanduser("~"), ".team_meetings.jsonl")
#: 人机对话台（群的家）—— 群消息走 HTTP 落库，NATS 只管唤醒通知（契约 §I）
# 兜底默认值集中到 L0 node_profile（env 优先）
DEFAULT_CHAT_BASE = os.environ.get("AGENT_TEAM_CHAT_BASE") or os.environ.get("TEAM_CHAT_BASE") or _npf.chat_base_default()


# ---------------------------------------------------------------- meet：临时会议

def make_session_id():
    # type: () -> str
    ts = time.strftime("%Y%m%d-%H%M")
    rand = "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(4))
    return "meet-%s-%s" % (ts, rand)


def build_meeting_body(goal, agenda, members, session_id):
    # type: (str, list, list, str) -> str
    lines = ["【临时会议召集】session_id=%s" % session_id, "目标：%s" % goal, "议程："]
    for i, item in enumerate(agenda, 1):
        lines.append("  %d. %s" % (i, item))
    lines.append("参会成员：%s" % ", ".join(members))
    lines.append("请每位参会成员按以下格式回报（一行，不要额外寒暄）：")
    lines.append("<你的成员名>|<真实主机名>|<一句话观点/结论>")
    return "\n".join(lines)


def append_jsonl(path, obj):
    # type: (str, dict) -> None
    full = os.path.expanduser(path)
    d = os.path.dirname(full)
    if d and not os.path.isdir(d):
        os.makedirs(d)
    with open(full, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def cmd_meet(args):
    # type: (argparse.Namespace) -> int
    member, password = resolve_member_and_password(args)
    if member is None:
        return 2
    if not args.goal or not args.goal.strip():
        sys.stderr.write("必须给 --goal\n")
        return 2
    agenda = args.agenda or []
    if not agenda:
        sys.stderr.write("必须至少给一条 --agenda\n")
        return 2
    members = sorted(set(m.strip() for m in (args.members or "").split(",") if m.strip()))
    if not members:
        sys.stderr.write("必须给 --members\n")
        return 2

    session_id = args.session_id or make_session_id()
    timeout = clamp_timeout(args.timeout)
    body = build_meeting_body(args.goal, agenda, members, session_id)

    subject_norm = "meet:%s" % ",".join(members)
    should_send, key = check_and_mark_dedup(args.dedup_file, subject_norm, body, args.force)
    if not should_send:
        print("本窗口内已发过同一内容，跳过（用 --force 强制重发）")
        return 0

    envelope = {"text": body, "session_id": session_id, "timeout_s": timeout, "from": member}
    payload = json.dumps(envelope, ensure_ascii=False).encode("utf-8")

    print("召集会议 session_id=%s　参会 %d 人：%s" % (session_id, len(members), ",".join(members)))
    try:
        args_list = [(args.nats_host, args.nats_port, member, password, m, payload, timeout)
                    for m in members]
        results = run_pool(call_one, args_list, args.concurrency)
    except Exception as exc:
        rollback_dedup(args.dedup_file, key)
        sys.stderr.write("召集失败，已回滚去重标记：%s\n" % exc)
        return 2

    present = [r for r in results if r and r.get("ok")]
    absent = [r for r in results if not (r and r.get("ok"))]

    print("目标：%s" % args.goal)
    for i, item in enumerate(agenda, 1):
        print("  议程 %d：%s" % (i, item))
    print("-" * 60)
    for r in results:
        rep = (r.get("reply") or r.get("error") or "").replace("\n", " ")
        if r.get("ok"):
            print("  [到会] %-14s %6.1fs  %s" % (r.get("member"), r.get("elapsed", 0.0), rep))
        else:
            print("  [缺席] %-14s %6.1fs  %s（不折算进通过率）"
                  % (r.get("member"), r.get("elapsed", 0.0), rep))
    print("汇总：到会 %d / 应到 %d" % (len(present), len(members)))

    record = {
        "session_id": session_id, "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "convener": member, "goal": args.goal, "agenda": agenda, "members": members,
        "body": body, "results": results,
        "present": [r.get("member") for r in present],
        "absent": [r.get("member") for r in absent],
    }
    try:
        append_jsonl(args.out, record)
    except Exception as exc:
        sys.stderr.write("警告：本地留痕写入失败（不影响本次会议结果）：%s\n" % exc)

    if args.json:
        print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0 if not absent else 1


# ---------------------------------------------------------------- history：回放

def cmd_history(args):
    # type: (argparse.Namespace) -> int
    path = os.path.expanduser(args.file)
    if not os.path.isfile(path):
        sys.stderr.write("留痕文件不存在：%s\n" % path)
        return 2

    found = None
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if obj.get("session_id") == args.session_id:
                found = obj  # 同一 session_id 可能重发过，取文件里最后一条

    if found is None:
        sys.stderr.write("没找到 session_id=%s 的会议记录（文件：%s）\n" % (args.session_id, path))
        return 1

    if args.json:
        print(json.dumps(found, ensure_ascii=False, indent=2))
        return 0

    print("会议 session_id=%s　召集人=%s　时间=%s"
          % (found.get("session_id"), found.get("convener"), found.get("ts")))
    print("目标：%s" % found.get("goal"))
    for i, item in enumerate(found.get("agenda") or [], 1):
        print("  议程 %d：%s" % (i, item))
    print("-" * 60)
    for r in found.get("results") or []:
        tag = "到会" if r.get("ok") else "缺席"
        rep = (r.get("reply") or r.get("error") or "").replace("\n", " ")
        print("  [%s] %-14s  %s" % (tag, r.get("member"), rep))
    print("汇总：到会 %d / 应到 %d" % (len(found.get("present") or []), len(found.get("members") or [])))
    return 0


# ---------------------------------------------------------------- 临时群（契约 §I）
#
# 群 = 一个 meet 场会话，消息落库在对话台（不落在 NATS 里）。
# 铁律：**Agent 发言必须 @ 至少一个目标**（规则 6），服务端会硬拒无 @ 的发言。
# 视角：pull 拉的是**全群增量**（谁说的都看得见），不是只给我的。

def _chat_call(path, method="GET", body=None, member=None, base=None, timeout=15.0):
    # type: (str, str, object, object, object, float) -> tuple
    """打对话台 HTTP 接口。Agent 身份用 X-Agent-Id（只在 tailnet 来源被服务端接受）。"""
    url = (base or DEFAULT_CHAT_BASE).rstrip("/") + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if member:
        req.add_header("X-Agent-Id", member)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {"raw": raw}


def _chat_member(args, required=False):
    # type: (argparse.Namespace, bool) -> object
    """群里的"我是谁"：显式 --as 优先，否则自动推断。

    读操作（show/pull）不该因为推断失败就用不了 —— 推断不出来就回 None，
    服务端按"匿名 tailnet"处理：照样能看全群，只是算不出"我欠谁"。
    """
    who = getattr(args, "as_member", None)
    if who:
        return who
    try:
        return infer_member()
    except TeamCliError:
        if required:
            raise
        return None


def _chat_error(status, body):
    # type: (int, object) -> str
    err = (body or {}).get("error") or {}
    return "HTTP %s %s：%s" % (status, err.get("code") or "?", err.get("message") or "")


def cmd_group_say(args):
    # type: (argparse.Namespace) -> int
    member = _chat_member(args, required=True)
    text = read_text(args)
    if not text or not str(text).strip():
        raise TeamCliError("发言不能为空：给位置参数 text 或 --text-file")
    raw_to = getattr(args, "to", None)
    if raw_to is None:
        raw_to = "@all"                       # 只有"没写 --to"才默认全员
    raw_to = raw_to.strip()
    if not raw_to:
        raise TeamCliError("--to 不能为空：写 @all 或成员 id 逗号分隔"
                           "（Agent 发言必须 @，服务端会硬拒无 @ 的发言）")
    mentions = (["@all"] if raw_to == "@all"
                else [t.strip() for t in raw_to.split(",") if t.strip()])
    status, body = _chat_call(
        "/api/sessions/%s/messages" % args.group, "POST",
        {"text": text, "mentions": mentions,
         "reply_to": getattr(args, "reply_to", None),
         "client_msg_id": getattr(args, "client_msg_id", None)},
        member=member, base=args.chat_base)
    if status != 201:
        print("发言失败：%s" % _chat_error(status, body))
        if getattr(args, "json", False):
            print(json.dumps(body, ensure_ascii=False, indent=2))
        return 1
    msg = body["message"]
    if getattr(args, "json", False):
        print(json.dumps(msg, ensure_ascii=False, indent=2))
        return 0
    meta = msg.get("meta") or {}
    print("[%s] %s → %s" % (msg["seq"], member, "、".join(meta.get("mentions") or [])))
    print("已落群：seq=%s id=%s" % (msg["seq"], msg["message_id"]))
    return 0


def cmd_group_pull(args):
    # type: (argparse.Namespace) -> int
    member = _chat_member(args)
    if member is None:
        print("（没认出我是谁：加 --as <成员名> 才能看到“我欠谁回应”）")
    query = "?limit=%d" % max(1, int(getattr(args, "limit", 50) or 50))
    if getattr(args, "since", None) is not None:
        query += "&after_seq=%d" % int(args.since)
    status, body = _chat_call("/api/sessions/%s/messages%s" % (args.group, query),
                              member=member, base=args.chat_base)
    if status != 200:
        print("拉取失败：%s" % _chat_error(status, body))
        return 1
    if getattr(args, "json", False):
        print(json.dumps(body, ensure_ascii=False, indent=2))
        return 0
    messages = body.get("messages") or []
    print("群 %s 共 %d 条（我是 %s）" % (args.group, len(messages), member))
    for msg in messages:
        meta = msg.get("meta") or {}
        who = (msg.get("author") or {}).get("display") or (msg.get("author") or {}).get("id")
        to = "、".join(meta.get("mentions") or []) or "-"
        mark = " *" if member in (meta.get("needs_response") or []) else ""
        print("  [%s]%s %s → %s：%s" % (msg.get("seq"), mark, who, to,
                                       (msg.get("text") or "").replace("\n", " ")))
    pending = body.get("my_pending") or []
    if pending:
        print("\n我还欠 %d 条回应（@我 且我没回过）：" % len(pending))
        for item in pending:
            print("  seq %s 来自 %s：%s" % (item.get("seq"), item.get("from"), item.get("text")))
        print("（回的时候记得 @ 对方，规则 6）")
    else:
        print("\n没有欠回应。")
    return 0


def cmd_group_show(args):
    # type: (argparse.Namespace) -> int
    status, body = _chat_call("/api/sessions/%s" % args.group,
                              member=_chat_member(args), base=args.chat_base)
    if status != 200:
        print("查群失败：%s" % _chat_error(status, body))
        return 1
    if getattr(args, "json", False):
        print(json.dumps(body, ensure_ascii=False, indent=2))
        return 0
    sess = body["session"]
    print("群：%s" % sess.get("title"))
    print("  id     ：%s" % sess.get("session_id"))
    print("  状态   ：%s%s" % (sess.get("state"),
                            "（已结束，只读）" if sess.get("state") != "open" else ""))
    print("  发起人 ：%s" % sess.get("created_by"))
    print("  成员   ：%s" % "、".join(m.get("id") for m in (sess.get("members") or [])))
    return 0
