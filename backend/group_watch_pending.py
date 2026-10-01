#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""群守望器 · 待回应账域侧（L3 协作域，批④ 从 backend/group_watch.py 原地拆出）。

域规则都在这里（这一半认识「群 / 待回应账 / 链式」）：
  * 扫所有 open 的临时群（kind=meet）
  * 以每个 Agent 的身份拉群消息 → 得到"他欠谁回应"清单（后端算好的 my_pending）
  * 人的 @ 优先；Agent 发的 @ 也回（--allow-agent，默认开），但每个 (群,成员)
    连续链式回话不超过 --max-chain 次，人一说话链条归零 —— 防 Agent 互相刷屏死循环
  * 拼最近上下文 → 投递意图（prompt / 信封）→ 交投递侧执行

单轮编排壳 `one_round` 仍在此件：内部 = `plan_round` 纯决策 + 投递侧原语执行（deliver_ask / post_reply）。
依赖方向 L3 → L1（group_watch_delivery / team_cli_core）与 L3 → L0（经投递侧），全部向下，方向合法。
"""
from __future__ import annotations

import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# group_watch_delivery 已归位 node/l1_transport/，本件改绝对包名 import，故把仓根一并放进搜索路径。
for _hacs_p in (os.path.dirname(HERE), HERE):
    if _hacs_p not in sys.path:
        sys.path.insert(0, _hacs_p)

from node.l1_transport.group_watch_delivery import deliver_ask, http, log, post_reply  # noqa: E402  L3 → L1（向下）

CTX_LIMIT = 12          # 喂给 Agent 的上下文条数
MAX_PER_ROUND = 1       # 每轮每个 (群,成员) 最多回几条
# 兜底默认值集中到 L0 node_paths（env 优先）（"人"的成员 id 列表，逗号分隔）
HUMAN_IDS = tuple(x.strip() for x in (os.environ.get("AGENT_TEAM_HUMAN_IDS") or "<human-id>").split(",") if x.strip())   # 谁是"人"：只有人发的消息才触发 Agent 回应（防死循环）


# ---------------- 提示词 ----------------
def build_prompt(sid, title, msgs, member, trigger):
    lines = ["【临时群《%s》的群聊记录，所有人可见】" % (title or sid)]
    for m in msgs[-CTX_LIMIT:]:
        au = m.get("author") or {}
        who = au.get("display") or au.get("id") or "?"
        kind = "人" if au.get("type") == "human" else "Agent"
        aud = (m.get("meta") or {}).get("audience") or []
        lines.append("#%s %s(%s) → %s: %s" % (m.get("seq"), who, kind,
                                              ",".join(aud) or "-", m.get("text") or ""))
    lines.append("")
    lines.append("现在轮到**你（%s）**回应 #%s（%s 发的）。" % (
        member, trigger.get("seq"), (trigger.get("from") or "")[:40]))
    lines.append("要求：")
    lines.append("1. 直接给出你要发到群里的正文，第一行以 @%s 开头点名他。" % (trigger.get("from") or ""))
    lines.append("2. 只说结论/承诺/要点，别复述本提示，别写书面报告。")
    lines.append("3. 这是群聊，全员可见 —— 有别的事要讲也在这里讲，不要点对点。")
    return "\n".join(lines)


# ---------------- 单轮：决策 ----------------
def plan_round(sid, member, pend, msgs, state, args, title=""):
    """纯决策：从「待回应账」里挑出本轮该投递的点名意图。

    不改 state（只读 seen/chain_count）、不发起投递 —— 返回投递意图列表：
    [{sid, member, seq, from, payload(bytes)}]，无意图时返回 []。
    """
    # 头部阻塞修复(2026-09-21 实测踩到): 不能"取 pend[:1] 再过滤" —— 一条 Agent 发的
    # 待回应会把它后面**人发的**待回应一起堵死, 表现为"<USER_ID> @ 了它, 它却一直不回"。
    # 现在: 先筛出所有可用条目 → 人的优先, Agent 发的按 --allow-agent 兜底(带链式上限)。
    usable = [p for p in pend
              if p.get("seq") and p.get("from") and p.get("from") != member
              and not state.seen(sid, member, p["seq"])]
    human_trigs = [p for p in usable if p.get("from") in HUMAN_IDS]
    agent_trigs = [p for p in usable if p.get("from") not in HUMAN_IDS]
    trigs = human_trigs[:MAX_PER_ROUND]
    if not trigs and agent_trigs and args.allow_agent:
        if state.chain_count(sid, member) < args.max_chain:
            trigs = agent_trigs[:MAX_PER_ROUND]
        else:
            log("链式回话达上限跳过", sid, member, "上限", args.max_chain)
    if not trigs and usable:
        log("有待回应但按规则不回", sid, member,
            "人发%d/Agent发%d" % (len(human_trigs), len(agent_trigs)))

    intents = []
    for p in trigs:
        prompt = build_prompt(sid, title, msgs, member, p)
        payload = json.dumps({"text": prompt, "timeout_s": args.timeout,
                              "from": "hermes", "session_id": "meet:" + sid},
                             ensure_ascii=False).encode("utf-8")
        intents.append({"sid": sid, "member": member, "seq": p.get("seq"),
                        "from": p.get("from") or "", "payload": payload})
    return intents


# ---------------- 单轮：编排 ----------------
def one_round(args, state):
    sess = http("GET", "%s/api/sessions?limit=100" % args.chat_base)
    groups = [s for s in ((sess or {}).get("sessions") or [])
              if (s.get("kind") or "") == "meet" and s.get("state") == "open"]
    if not groups:
        return 0

    acted = 0
    for g in groups:
        sid = g.get("session_id")
        members = [m.get("id") if isinstance(m, dict) else m for m in (g.get("members") or [])]
        for member in members:
            if not member:
                continue
            if member in HUMAN_IDS:
                continue                      # 人是发起方，不是被点名的 Agent
            if member == "hermes" and not args.include_hermes:
                continue                      # 默认不让 hermes 自己回（免自问自答）

            d = http("GET", "%s/api/sessions/%s/messages?limit=60" % (args.chat_base, sid),
                     agent_id=member)
            if not d:
                continue
            pend = d.get("my_pending") or []
            if not pend:
                continue
            msgs = d.get("messages") or []

            for intent in plan_round(sid, member, pend, msgs, state, args, g.get("title")):
                seq = intent["seq"]
                frm = intent["from"]
                if not state.gap_ok(sid, member, args.gap):
                    log("限流跳过", sid, member, seq)
                    continue
                if frm in HUMAN_IDS:
                    state.reset_chain(sid, member)     # 人说话了, 链条归零
                else:
                    state.bump_chain(sid, member)

                log("→ 点名", member, "群", sid, "seq", seq)
                res = deliver_ask(member, args.nats_host, args.nats_user, args.nats_pass,
                                  intent["payload"], args.timeout)
                state.mark(sid, member, seq)
                state.touch(sid, member)
                state.save()

                if not res.get("ok") or not (res.get("reply") or "").strip():
                    log("   × 没拿到回复:", res.get("error"))
                    continue
                reply = (res.get("reply") or "").strip()
                if not reply.startswith("@"):
                    reply = "@%s %s" % (frm, reply)     # Agent 发言必须 @（后端 422 硬拦）
                # 投递只认**结构化 mentions**，正文里的 "@某人" 后端不解析（chat_svc 的
                # audience = members ∩ mentions）。原先这里写死 mentions=[frm]，等于把
                # Agent 回复正文里 @ 的其他人全部丢掉 —— 表现为"我在群里 @ 了它，它却像没收到"
                # （2026-09-21 实测踩到：hermes 回复里写 @openclaw-nb 请确认，对方零唤醒）。
                # 现在：正文解析出的 @ 与 frm 求并集，并且**必须过滤成群内成员**，
                # 否则后端 unknown_mention 422 把整条消息拒掉。
                want, seen_m = [], set()
                for tok in re.findall(r"@([A-Za-z0-9_.\-]+|所有人)", reply):
                    tid = "@all" if tok == "所有人" else tok
                    if tid != "@all" and tid not in members:
                        continue
                    if tid in seen_m:
                        continue
                    seen_m.add(tid)
                    want.append(tid)
                if frm not in want:
                    want.insert(0, frm)
                posted = post_reply(args.chat_base, sid, member, reply, want)
                if posted and posted.get("ok", True) and not (posted.get("error")):
                    log("   ✓ 已回话", member, "→", frm, "(%d 字)" % len(reply))
                    acted += 1
                else:
                    log("   × 写回群失败:", json.dumps(posted, ensure_ascii=False)[:200])
    return acted
