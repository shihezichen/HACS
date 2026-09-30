#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""群守望器 · 投递侧（L1 通信层，批④ 从 backend/group_watch.py 原地拆出）。

本件只做**投递原语**，不认识「群 / 待回应账 / 谁欠谁回应」这类域概念：
  * NATS 点名封装 `deliver_ask`（复用 L1 team_cli_core 的 call_one / REQ_FMT）
  * 写回群封装 `post_reply`（chat_svc HTTP）
  * 幂等与限流状态 `State`（done / last / chain 三键单文件落盘，格式一字未改）
  * 共享工具 `log` / `http`
域规则（人的 @ 优先 / 链式上限 / 提示词拼装）见 L3 group_watch_pending.py；进程入口见 C group_watch.py。
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

# 批① 起被搬件按绝对包名 import，故把仓根一并放进搜索路径（本目录仍在最前）。
for _hacs_p in (os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                os.path.dirname(os.path.abspath(__file__))):
    sys.path.insert(0, _hacs_p)

from node.l0_host import node_paths as _np  # noqa: E402  L0 平台层：宿主默认值唯一出处（env 优先）
from node.l0_host import node_profile as _npf  # noqa: E402  L0 节点档案：宿主事实唯一真源（协作域基址 / NATS 集群串；env 优先）

from team_cli_core import call_one, REQ_FMT  # noqa: E402  复用现成的 A2A 信封与点名实现（L1 通信层，刀15 起不再经组合根 team.py）

# 兜底默认值集中到 L0 node_profile（env 优先）
AGENT_TEAM_CHAT_BASE = os.environ.get("AGENT_TEAM_CHAT_BASE") or _npf.chat_base_default()
# 兜底默认值集中到 L0 node_profile（env 优先）
AGENT_TEAM_NATS_HOST = os.environ.get("AGENT_TEAM_NATS_HOST") or os.environ.get("AGENT_TEAM_NATS") or _npf.nats_hosts_default()
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


# ---------------- 投递原语（批④ 从 one_round 中抽出，逻辑逐字未改） ----------------
def deliver_ask(member, nats_host, nats_user, nats_pass, payload_bytes, timeout):
    """把一封点名信封投给 member（a2a.v1.req.<member>），返回 call_one 的结果 dict。"""
    host = nats_host.split(",")[0].strip()
    port = int(host.rsplit(":", 1)[1])
    # user/password 是**我们自己的** NATS 身份（总控 hermes）；member 只决定 subject
    return call_one(host.rsplit(":", 1)[0], port, nats_user, nats_pass,
                    member, payload_bytes, timeout)


def post_reply(chat_base, sid, member, reply, mentions):
    """以 member 身份把回复写回群（结构化 mentions 由调用方算好）。"""
    return http("POST", "%s/api/sessions/%s/messages" % (chat_base, sid),
                {"text": reply, "mentions": mentions}, agent_id=member)
