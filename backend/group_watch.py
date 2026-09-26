#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""群守望器：把"群里有人欠回应"变成一次 NATS/A2A 点名，让 Agent 自己回话。

为什么需要它（架构缺口）：
    群聊走 HTTP + PG（chat_svc:9984）—— 只落库、只给页面看；
    NATS 只服务点对点点名（a2a.v1.req.<member>）。
    中间这段桥没人接，所以你在群里 @Agent，它收不到任何信号，永远不回。
    本进程就是把这段桥接上：群消息 → NATS 点名 → Agent 回应 → 写回群。

循环做四件事：
  1. 找出所有 open 的临时群（kind=meet）
  2. 以每个 Agent 的身份拉群消息 → 得到"他欠谁回应"清单（后端算好的 my_pending）
  3. 人的 @ 优先；Agent 发的 @ 也回（--allow-agent，默认开），但每个 (群,成员)
     连续链式回话不超过 --max-chain 次，人一说话链条归零 —— 防 Agent 互相刷屏死循环
  4. 拼最近上下文 → NATS 投 a2a.v1.req.<member> → 拿回复 → 以该 Agent 身份写回群并 @ 回原发送人

幂等与限流：
  * 处理过的 (群, 成员, seq) 落盘，重启不重发（at-least-once 的重复喂 LLM 问题）
  * 每个 (群, 成员) 有最小间隔，且每轮每人最多回一条
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import node_paths as _np  # L0 平台层：宿主默认值唯一出处（env 优先）

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from team_cli_core import call_one, REQ_FMT  # noqa: E402  复用现成的 A2A 信封与点名实现（L1 通行层，刀15 起不再经组合根 team.py）

# 兜底默认值集中到 L0 node_paths（env 优先）
AGENT_TEAM_CHAT_BASE = os.environ.get("AGENT_TEAM_CHAT_BASE") or "http://<host>:9984"
# 兜底默认值集中到 L0 node_paths（env 优先）
AGENT_TEAM_NATS_HOST = os.environ.get("AGENT_TEAM_NATS_HOST") or os.environ.get("AGENT_TEAM_NATS") or "<host>:9930,<node-ip>:9930"
# 兜底默认值集中到 L0 node_paths（env 优先）
AGENT_TEAM_RUNTIME_DIR = os.environ.get("AGENT_TEAM_RUNTIME_DIR") or _np.runtime_dir()

CHAT_BASE_DEFAULT = AGENT_TEAM_CHAT_BASE
NATS_HOST_DEFAULT = AGENT_TEAM_NATS_HOST
STATE_DEFAULT = os.path.join(HERE, "state", "group_watch_state.json")
# 口令可能在源码目录（D 源）也可能在中枢目录（<RUNTIME_DIR>）—— 两处都找
PASS_CANDIDATES = (os.path.join(HERE, "nats-hermes.pass"),
                   os.path.join(os.environ.get("AGENT_TEAM_NATS_DIR")
                                or AGENT_TEAM_RUNTIME_DIR, "nats-hermes.pass"))


def default_pass_file():
    for p in PASS_CANDIDATES:
        if os.path.exists(p):
            return p
    return PASS_CANDIDATES[0]

CTX_LIMIT = 12          # 喂给 Agent 的上下文条数
MAX_PER_ROUND = 1       # 每轮每个 (群,成员) 最多回几条
# 兜底默认值集中到 L0 node_paths（env 优先）（"人"的成员 id 列表，逗号分隔）
HUMAN_IDS = tuple(x.strip() for x in (os.environ.get("AGENT_TEAM_HUMAN_IDS") or "<human-id>").split(",") if x.strip())   # 谁是"人"：只有人发的消息才触发 Agent 回应（防死循环）


LOG_FILE = None


def log(*a):
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), " ".join(str(x) for x in a))
    try:
        print(line, flush=True)
    except Exception:
        pass
    if LOG_FILE:
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


# ---------------- HTTP 小工具 ----------------
def http(method, url, body=None, agent_id=None, timeout=20.0):
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
            return {"ok": False, "error": {"code": "http_%d" % e.code, "message": raw[:200]}}


# ---------------- 状态（幂等） ----------------
class State(object):
    def __init__(self, path):
        self.path = path
        self.done = {}      # "sid|member|seq" -> ts
        self.last = {}      # "sid|member" -> ts（限流用）
        self.chain = {}     # "sid|member" -> 连续被 Agent 触发的回话次数（防死循环）
        self.load()

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                d = json.load(f)
            self.done = d.get("done") or {}
            self.last = d.get("last") or {}
            self.chain = d.get("chain") or {}
        except Exception:
            self.done, self.last, self.chain = {}, {}, {}

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"done": self.done, "last": self.last, "chain": self.chain},
                      f, ensure_ascii=False)
            os.replace(tmp, self.path)
        except Exception as exc:
            log("状态落盘失败:", exc)

    def seen(self, sid, member, seq):
        return ("%s|%s|%d" % (sid, member, seq)) in self.done

    def mark(self, sid, member, seq):
        self.done["%s|%s|%d" % (sid, member, seq)] = int(time.time())

    def gap_ok(self, sid, member, gap_s):
        k = "%s|%s" % (sid, member)
        return (time.time() - float(self.last.get(k) or 0)) >= gap_s

    def touch(self, sid, member):
        self.last["%s|%s" % (sid, member)] = int(time.time())

    def chain_count(self, sid, member):
        return int(self.chain.get("%s|%s" % (sid, member)) or 0)

    def bump_chain(self, sid, member):
        k = "%s|%s" % (sid, member)
        self.chain[k] = self.chain_count(sid, member) + 1
        return self.chain[k]

    def reset_chain(self, sid, member):
        self.chain["%s|%s" % (sid, member)] = 0


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


# ---------------- 单轮 ----------------
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

            for p in trigs:
                seq = p.get("seq")
                frm = p.get("from") or ""
                if not state.gap_ok(sid, member, args.gap):
                    log("限流跳过", sid, member, seq)
                    continue
                if frm in HUMAN_IDS:
                    state.reset_chain(sid, member)     # 人说话了, 链条归零
                else:
                    state.bump_chain(sid, member)

                prompt = build_prompt(sid, g.get("title"), msgs, member, p)
                payload = json.dumps({"text": prompt, "timeout_s": args.timeout,
                                      "from": "hermes", "session_id": "meet:" + sid},
                                     ensure_ascii=False).encode("utf-8")
                log("→ 点名", member, "群", sid, "seq", seq)
                host = args.nats_host.split(",")[0].strip()
                port = int(host.rsplit(":", 1)[1])
                # user/password 是**我们自己的** NATS 身份（总控 hermes）；member 只决定 subject
                res = call_one(host.rsplit(":", 1)[0], port, args.nats_user, args.nats_pass,
                               member, payload, args.timeout)
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
                posted = http("POST", "%s/api/sessions/%s/messages" % (args.chat_base, sid),
                              {"text": reply, "mentions": want}, agent_id=member)
                if posted and posted.get("ok", True) and not (posted.get("error")):
                    log("   ✓ 已回话", member, "→", frm, "(%d 字)" % len(reply))
                    acted += 1
                else:
                    log("   × 写回群失败:", json.dumps(posted, ensure_ascii=False)[:200])
    return acted


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chat-base", default=CHAT_BASE_DEFAULT)
    ap.add_argument("--nats-host", default=NATS_HOST_DEFAULT)
    ap.add_argument("--nats-user", default="hermes")
    ap.add_argument("--nats-pass-file", default=default_pass_file())
    ap.add_argument("--state", default=STATE_DEFAULT)
    ap.add_argument("--interval", type=float, default=8.0)
    ap.add_argument("--gap", type=float, default=30.0, help="同一(群,成员)最小回话间隔秒")
    ap.add_argument("--timeout", type=float, default=600.0, help="点名应答超时秒(2026-09-22 180→600: 180s 会砍掉跑长命令的回合)")
    ap.add_argument("--include-hermes", action="store_true", help="也让 <MEMBER_ID> 自己回话")
    ap.add_argument("--allow-agent", dest="allow_agent", action="store_true", default=True,
                    help="也回应 Agent 发的 @（默认开；群内 Agent 互相 @ 不再石沉大海）")
    ap.add_argument("--no-allow-agent", dest="allow_agent", action="store_false",
                    help="退回旧口径：只回人发的 @")
    ap.add_argument("--max-chain", type=int, default=2,
                    help="同一(群,成员)连续被 Agent 触发的回话次数上限，超过即停(防死循环)")
    ap.add_argument("--log-file", default="", help="同时写日志文件（pythonw 无 stdout）")
    ap.add_argument("--once", action="store_true", help="只跑一轮（调试用）")
    args = ap.parse_args()

    if args.log_file:
        global LOG_FILE
        LOG_FILE = args.log_file

    try:
        with open(args.nats_pass_file, "r", encoding="utf-8") as f:
            args.nats_pass = f.read().strip()
    except Exception as exc:
        log("读不到 NATS 口令:", exc)
        return 2

    state = State(args.state)
    log("群守望器启动 · chat=%s · 每 %.0fs 一轮 · 间隔 %.0fs" % (args.chat_base, args.interval, args.gap))
    while True:
        try:
            n = one_round(args, state)
            if n:
                log("本轮回话", n, "条")
        except KeyboardInterrupt:
            log("收到退出信号，收工")
            state.save()
            return 0
        except Exception as exc:
            log("本轮异常(下一轮继续):", "%s: %s" % (type(exc).__name__, exc))
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
