#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""post_group.py —— 让 Agent（尤其是派出去干活的子 Agent）把结论发回人机对话群的出口。

背景（为什么要这个脚本）：
    群聊回合已被纪律约束成「秒回 + 把重活 delegate_task 派给子 Agent，然后立刻结束本回合」。
    但子 Agent 是异步跑的，它的结果不会再回到群里 —— 这条回路原来没人接。
    本脚本就是那条回路：任何进程只要跑一次它，就能以某个 Agent 的身份把一段正文发进指定群。

用法：
    python post_group.py --text "要发的内容"
    python post_group.py --as <MEMBER_ID> --session meet_20260921_demo01 --text "..." --mention <USER_ID>
    echo "长正文" | python post_group.py --text -

⚠ 踩坑（2026-09-22 实测踩到）：正文里含反引号（`...`）或 `$` 时，**不要**直接写在 bash 命令行里 ——
   bash 会把反引号当命令替换执行，正文里的路径/代码引用会被整段吃掉（消息照样发出，但内容残缺）。
   长正文一律先写文件，再 `--text - < 文件` 传进来。

默认：以 hermes 身份、发到最近一个 open 的临时群、自动 @ 消息里出现的合法成员。
鉴权：HTTP 头 X-Agent-Id:<成员id>（与 group_watch.py 同一套口径，无需额外口令）。

纪律：
  * 只发文本，不打印任何口令。
  * 发送后**回读校验**（重新拉一次最新消息，确认自己那条真的在），避免「发了但没人看见」。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

# 批⑪ 归位：本件已搬入 node/l1_transport/（裁定⑦：本质是 chat_svc 的 HTTP 客户端 = 传输件）。
# 仓根 + 本目录一并入路径（批① 惯例），跨层件按**绝对包名** import。
_HERE_BOOT = os.path.dirname(os.path.abspath(__file__))
for _hacs_p in (os.path.dirname(os.path.dirname(_HERE_BOOT)), _HERE_BOOT):
    if _hacs_p not in sys.path:
        sys.path.insert(0, _hacs_p)

from node.l0_host import node_profile as _npf  # noqa: E402  L0 节点档案：宿主事实唯一真源（对话台基址；env 优先）

# 兜底默认值集中到 L0 node_profile（env 优先；口径与 group_watch_delivery.py 一致）
CHAT_BASE_DEFAULT = os.environ.get("AGENT_TEAM_CHAT_BASE") or _npf.chat_base_default()
DEFAULT_SESSION = "meet_20260921_demo01"


def log(*a):
    print("[post_group] " + " ".join(str(x) for x in a), flush=True)


def http(method, url, body=None, agent_id=None, timeout=25.0):
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if agent_id:
        headers["X-Agent-Id"] = agent_id
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            return json.loads(raw) if raw.strip() else None
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return json.loads(raw)
        except Exception:
            return {"ok": False, "error": {"code": "http_%d" % e.code, "message": raw[:300]}}
    except Exception as exc:
        return {"ok": False, "error": {"code": "network", "message": "%s: %s" % (type(exc).__name__, exc)}}


def pick_session(base, wanted):
    """选群：显式指定 > env CHAT_SESSION > DEFAULT_SESSION（若它还开着）> 最近更新的 open 临时群。

    为什么要有 DEFAULT_SESSION 这一层：本机常年有多个 open 的临时群（含自测群），
    「取最近更新的那个」会把消息发错群 —— 实测踩过，所以钉死默认群。
    """
    sess = http("GET", "%s/api/sessions?limit=200" % base)
    groups = [s for s in ((sess or {}).get("sessions") or [])
              if (s.get("kind") or "") == "meet" and s.get("state") == "open"]
    if wanted:
        # --session 传的可能是 **群标题**（文档/派活里常写群名而不是 session_id）：
        # 直接当 id 用会 404「会话不存在」。先核对是不是真 id；不是就按 title 精确匹配
        # （要求唯一命中，多个同名群一律拒绝，宁可报错也不发错群）。
        if any(s.get("session_id") == wanted for s in groups):
            return wanted
        by_title = [s for s in groups if (s.get("title") or "") == wanted]
        if len(by_title) == 1:
            log("--session 给的是群标题，已解析为", by_title[0].get("session_id"))
            return by_title[0].get("session_id")
        if len(by_title) > 1:
            log("群标题 %r 命中 %d 个群，歧义，拒绝发送" % (wanted, len(by_title)))
            return None
        return wanted
    if not groups:
        return None
    ids = {s.get("session_id") for s in groups}
    if DEFAULT_SESSION in ids:
        return DEFAULT_SESSION
    groups.sort(key=lambda s: str(s.get("updated_at") or ""), reverse=True)
    return groups[0].get("session_id")


def main():
    ap = argparse.ArgumentParser(description="把一段正文发回人机对话群")
    ap.add_argument("--chat-base", default=os.environ.get("CHAT_BASE") or CHAT_BASE_DEFAULT)
    ap.add_argument("--session", default=os.environ.get("CHAT_SESSION") or "",
                    help="群 session_id；不填则取最近一个 open 的临时群")
    ap.add_argument("--as", dest="agent_id", default=os.environ.get("CHAT_AGENT") or "hermes",
                    help="以谁的身份发（默认 <MEMBER_ID>）")
    ap.add_argument("--text", required=True, help="正文；用 - 表示从 stdin 读")
    ap.add_argument("--mention", action="append", default=[],
                    help="额外要 @ 的成员，可重复；不填则自动取正文里出现的合法成员")
    # ---- #448：发消息的同时把任务队列（9980 todos）那条翻状态，一次调用不留两笔账 ----
    ap.add_argument("--todo-id", default="",
                    help="#448：顺手翻状态的队列条目 id（配合 --todo-status）")
    ap.add_argument("--todo-status", default="", choices=["", "open", "doing", "done"],
                    help="#448：队列条目新状态（open=待办 / doing=进行中 / done=已完成）")
    ap.add_argument("--landing", action="append", default=[],
                    help="#448：机器可验落点，可重复；翻 done 必须至少一条（否则后端 422）")
    ap.add_argument("--no-verify", action="store_true", help="跳过发送后的回读校验")
    args = ap.parse_args()

    text = sys.stdin.read() if args.text == "-" else args.text
    text = (text or "").strip()
    if not text:
        log("正文为空，退出")
        return 2

    sid = pick_session(args.chat_base, args.session)
    if not sid:
        log("拿不到任何 open 的临时群，退出")
        return 3

    # 成员名册（用于过滤 @ 提及：后端对不存在的成员会 422 拒掉整条消息）
    members = set()
    try:
        d = http("GET", "%s/api/sessions/%s/messages?limit=1" % (args.chat_base, sid),
                 agent_id=args.agent_id)
        for m in (d or {}).get("messages") or []:
            au = m.get("author") or {}
            if au.get("id"):
                members.add(au["id"])
    except Exception:
        pass
    try:
        sess = http("GET", "%s/api/sessions?limit=100" % args.chat_base)
        for s in ((sess or {}).get("sessions") or []):
            if s.get("session_id") == sid:
                for m in s.get("members") or []:
                    mid = m.get("id") if isinstance(m, dict) else m
                    if mid:
                        members.add(mid)
    except Exception:
        pass

    mentions, seen = [], set()
    for tok in args.mention + re.findall(r"@([A-Za-z0-9_.\-]+|所有人)", text):
        tid = "@all" if tok == "所有人" else tok
        if tid != "@all" and members and tid not in members:
            continue
        if tid in seen:
            continue
        seen.add(tid)
        mentions.append(tid)

    payload = {"text": text, "mentions": mentions}
    if args.todo_id and args.todo_status:
        decl = {"id": args.todo_id, "status": args.todo_status}
        if args.landing:
            decl["landing"] = list(args.landing)
        payload["todo"] = decl
    elif args.todo_id or args.todo_status:
        log("--todo-id 与 --todo-status 要么都给、要么都不给，退出")
        return 2
    posted = http("POST", "%s/api/sessions/%s/messages" % (args.chat_base, sid),
                  payload, agent_id=args.agent_id)
    ok = bool(posted) and posted.get("ok", True) and not posted.get("error")
    if not ok:
        log("写回群失败:", json.dumps(posted, ensure_ascii=False)[:400])
        return 4
    log("已发回群", sid, "as", args.agent_id, "(%d 字) mentions=%s" % (len(text), mentions or "-"))
    if posted.get("queued"):
        log("队列自动入队:", json.dumps(posted["queued"], ensure_ascii=False)[:200])
    if posted.get("todo"):
        log("队列状态已翻:", (posted["todo"].get("id"), posted["todo"].get("status")))

    if args.no_verify:
        return 0

    # 回读校验：确认自己这条真的落库了（发送成功 != 送达）
    try:
        d = http("GET", "%s/api/sessions/%s/messages?limit=10" % (args.chat_base, sid),
                 agent_id=args.agent_id)
        tail = (d or {}).get("messages") or []
        hit = [m for m in tail
               if (m.get("author") or {}).get("id") == args.agent_id
               and (m.get("text") or "").strip()[:40] == text[:40]]
        if hit:
            log("回读校验通过：已在群里看到自己的消息 seq=%s" % hit[-1].get("seq"))
            return 0
        log("回读校验未命中（可能只是列表还没刷到），已发但请人工确认")
        return 0
    except Exception as exc:
        log("回读校验异常:", exc)
        return 0


if __name__ == "__main__":
    sys.exit(main())
