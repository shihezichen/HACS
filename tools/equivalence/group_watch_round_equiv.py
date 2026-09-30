#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批④ 等价装置 · 层二：**投递意图对账**（注入桩，零网络、零 NATS、零 PG）。

层一证明「同名单元逐字节相同」，但 one_round 里的**编排顺序**与两处**新抽出的原语**
（deliver_ask / post_reply）不在层一的覆盖面上 —— 本装置补这一段：
  改前件（`git show d445e6f:backend/group_watch.py`）与改后三件，跑**同一批冻结 fixture**，
  逐个对账：① 外呼 HTTP 序列 ② 点名序列（host/port/user/subject/信封正文/timeout）
  ③ 写回群序列（正文 + 结构化 mentions + 身份头）④ 幂等状态文件逐字节 ⑤ 日志文本 ⑥ 返回值。
全空即 rc=0。

用法：
    PYTHONUTF8=1 python tools/equivalence/group_watch_round_equiv.py
    PYTHONUTF8=1 python tools/equivalence/group_watch_round_equiv.py --verbose
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BACKEND = os.path.join(ROOT, "backend")
BASE_REV = "d445e6f"
BASE_PATH = "backend/group_watch.py"
TMP = os.environ.get("TMPDIR") or tempfile.gettempdir()
HUMAN = "human1"
T0 = 1700000000.0

for _p in (BACKEND, ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)
os.environ["AGENT_TEAM_HUMAN_IDS"] = HUMAN


# ---------------------------------------------------------------- fixture 冻结批
def M(seq, who, text, kind="agent", aud=("a1",)):
    return {"seq": seq, "author": {"id": who, "display": who, "type": kind},
            "text": text, "meta": {"audience": list(aud)}}


G1 = {"session_id": "s1", "kind": "meet", "state": "open", "title": "T1",
      "members": ["human1", "a1", "a2", "hermes"]}
MSGS = [M(1, "human1", "@a1 看下", "human"), M(2, "a2", "@a1 帮忙"), M(3, "a2", "@a1 再帮")]
PEND_H, PEND_A = {"seq": 1, "from": "human1"}, {"seq": 2, "from": "a2"}


def sess(groups=(G1,)):
    return {"sessions": list(groups)}


def msg(pend, msgs=MSGS):
    return {"my_pending": list(pend), "messages": list(msgs)}


CASES = [
    {"name": "R1 人发的 @：点名 + 回写，mentions 取并集且过滤群外成员",
     "rounds": [{"sessions": sess(), "msgs": {"a1": msg([PEND_H])},
                 "ask": {"ok": True, "reply": "@a2 收到，请确认 @OUTSIDER @所有人"}}]},
    {"name": "R2 链式上限：Agent 触发连回 2 次后停（max_chain=2）",
     "rounds": [{"t": 0.0, "sessions": sess(), "msgs": {"a1": msg([PEND_A])}, "ask": {"ok": True, "reply": "@a2 ok"}},
                {"t": 40.0, "sessions": sess(), "msgs": {"a1": msg([{"seq": 3, "from": "a2"}])}, "ask": {"ok": True, "reply": "@a2 ok"}},
                {"t": 80.0, "sessions": sess(), "msgs": {"a1": msg([{"seq": 4, "from": "a2"}])}, "ask": {"ok": True, "reply": "@a2 ok"}}]},
    {"name": "R3 限流：gap 未到则跳过（不点名、不落盘）",
     "rounds": [{"t": 0.0, "sessions": sess(), "msgs": {"a1": msg([PEND_H])}, "ask": {"ok": True, "reply": "@human1 好"}},
                {"t": 5.0, "sessions": sess(), "msgs": {"a1": msg([{"seq": 2, "from": "human1"}])}, "ask": {"ok": True, "reply": "@human1 好"}},
                {"t": 40.0, "sessions": sess(), "msgs": {"a1": msg([{"seq": 2, "from": "human1"}])}, "ask": {"ok": True, "reply": "@human1 好"}}]},
    {"name": "R4 幂等：同一 (群,成员,seq) 二次出现不再点名",
     "rounds": [{"t": 0.0, "sessions": sess(), "msgs": {"a1": msg([PEND_H])}, "ask": {"ok": True, "reply": "@human1 好"}},
                {"t": 40.0, "sessions": sess(), "msgs": {"a1": msg([PEND_H])}, "ask": {"ok": True, "reply": "@human1 好"}}]},
    {"name": "R5 自指 / 空 from / 无 seq 全过滤",
     "rounds": [{"sessions": sess(), "msgs": {"a1": msg([{"seq": 5, "from": "a1"}, {"seq": 0, "from": "human1"}, {"seq": 6, "from": ""}])},
                 "ask": {"ok": True, "reply": "@human1 好"}}]},
    {"name": "R6 只扫 open 的 meet 群",
     "rounds": [{"sessions": sess(({"session_id": "s2", "kind": "chat", "state": "open", "members": ["a1"]},
                                   {"session_id": "s3", "kind": "meet", "state": "closed", "members": ["a1"]})),
                 "msgs": {"a1": msg([PEND_H])}, "ask": {"ok": True, "reply": "x"}}]},
    {"name": "R7 点名超时(kind=无回复)：已 mark、不写回、acted 不增",
     "rounds": [{"sessions": sess(), "msgs": {"a1": msg([PEND_H])}, "ask": {"ok": False, "error": "timeout"}}]},
    {"name": "R8 回复不以 @ 开头：自动补 @触发人",
     "rounds": [{"sessions": sess(), "msgs": {"a1": msg([PEND_H])}, "ask": {"ok": True, "reply": "收到"}}]},
    {"name": "R9 写回失败：acted 不增",
     "rounds": [{"sessions": sess(), "msgs": {"a1": msg([PEND_H])},
                 "ask": {"ok": True, "reply": "@human1 好"}, "post": {"ok": False, "error": {"code": "422"}}}]},
    {"name": "R10 人说话后链归零：Agent 又可被触发",
     "rounds": [{"t": 0.0, "sessions": sess(), "msgs": {"a1": msg([PEND_A])}, "ask": {"ok": True, "reply": "@a2 ok"}},
                {"t": 40.0, "sessions": sess(), "msgs": {"a1": msg([{"seq": 1, "from": "human1"}])}, "ask": {"ok": True, "reply": "@human1 好"}},
                {"t": 80.0, "sessions": sess(), "msgs": {"a1": msg([{"seq": 3, "from": "a2"}])}, "ask": {"ok": True, "reply": "@a2 ok"}}]},
    {"name": "R11 hermes 成员默认跳过 / include_hermes 打开则回",
     "rounds": [{"sessions": sess(), "msgs": {"hermes": msg([PEND_H])}, "ask": {"ok": True, "reply": "@human1 好"},
                 "include_hermes": False},
                {"t": 40.0, "sessions": sess(), "msgs": {"hermes": msg([PEND_H])}, "ask": {"ok": True, "reply": "@human1 好"},
                 "include_hermes": True}]},
]


# ---------------------------------------------------------------- 桩
class Args(object):
    def __init__(self, **kw):
        self.chat_base = "http://chat.test"
        self.nats_host = "1.2.3.4:4222"
        self.nats_user = "hermes"
        self.nats_pass = "pw"
        self.timeout = 600.0
        self.gap = 30.0
        self.allow_agent = True
        self.max_chain = 2
        self.include_hermes = False
        self.state = ""
        for k, v in kw.items():
            setattr(self, k, v)


class Stub(object):
    """把 http / call_one / log 全换成记录器；按轮喂响应。"""

    def __init__(self, now):
        self.now = now
        self.cur = {}
        self.http_calls, self.asks, self.posts, self.logs = [], [], [], []

    def http(self, method, url, body=None, agent_id=None, timeout=20.0):
        self.http_calls.append([method, url, body, agent_id])
        if method == "GET" and "/api/sessions?" in url:
            return self.cur.get("sessions")
        if method == "GET":
            return (self.cur.get("msgs") or {}).get(agent_id)
        return self.cur.get("post") or {"ok": True}

    def call_one(self, host, port, user, pwd, subject, payload, timeout):
        env = json.loads(payload.decode("utf-8"))
        self.asks.append([host, port, user, pwd, subject, env.get("text"), env.get("timeout_s"),
                          env.get("from"), env.get("session_id"), timeout, len(payload)])
        return dict(self.cur.get("ask") or {"ok": True, "reply": ""})

    def log(self, *a):
        self.logs.append(" ".join(str(x) for x in a))


def snapshot(trace, stub, state_file, acted):
    data = None
    if os.path.exists(state_file):
        with open(state_file, encoding="utf-8") as f:
            data = json.load(f)
    posts = [[c[1], (c[2] or {}).get("text"), (c[2] or {}).get("mentions"), c[3]]
             for c in stub.http_calls if c[0] == "POST"]
    trace.append({"acted": acted, "asks": stub.asks, "posts": posts,
                  "http": [[c[0], c[1], c[2], c[3]] for c in stub.http_calls],
                  "logs": stub.logs, "state": data})


# ---------------------------------------------------------------- 跑一侧
def run_case(mod, kind, case):
    if kind == "base":
        state = mod.State(os.path.join(TMP, "gw_eq_base.json"))
        one_round, patch_http, patch_ask, patch_log = mod.one_round, [mod], [mod], [mod]
    else:
        P, D = mod["pending"], mod["delivery"]
        state = D.State(os.path.join(TMP, "gw_eq_new.json"))
        one_round, patch_http, patch_ask, patch_log = P.one_round, [P, D], [D], [P, D]

    saved = []
    stub = Stub(None)
    for holder in patch_http:
        saved.append((holder, "http", holder.http)); holder.http = stub.http
    for holder in patch_ask:
        saved.append((holder, "call_one", holder.call_one)); holder.call_one = stub.call_one
    for holder in patch_log:
        saved.append((holder, "log", holder.log)); holder.log = stub.log
    t_saved = time.time

    trace, now = [], [T0]
    try:
        time.time = lambda: now[0]
        for i, rd in enumerate(case["rounds"]):
            if i == 0:
                if os.path.exists(state.path):
                    os.remove(state.path)
                state = type(state)(state.path)
            now[0] = T0 + float(rd.get("t") or 0.0)
            stub.cur = rd
            stub.asks, stub.posts, stub.http_calls, stub.logs = [], [], [], []
            args = Args(gap=rd.get("gap", 30.0), max_chain=rd.get("max_chain", 2),
                        allow_agent=rd.get("allow_agent", True),
                        include_hermes=rd.get("include_hermes", False))
            acted = one_round(args, state)
            snapshot(trace, stub, state.path, acted)
    finally:
        time.time = t_saved
        for holder, name, old in saved:
            setattr(holder, name, old)
    return trace


# ---------------------------------------------------------------- 装载
def load_base():
    import subprocess
    out = subprocess.run(["git", "show", "%s:%s" % (BASE_REV, BASE_PATH)], cwd=ROOT,
                         capture_output=True, check=True).stdout.decode("utf-8")
    path = os.path.join(TMP, "gw_base_snapshot.py")
    with open(path, "w", encoding="utf-8") as f:
        f.write(out)
    spec = importlib.util.spec_from_file_location("gw_base_snapshot", path)
    m = importlib.util.module_from_spec(spec)
    sys.modules["gw_base_snapshot"] = m
    spec.loader.exec_module(m)
    return m


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--negative", action="store_true",
                    help="负控：把改后侧的域规则注毒一格（人的判定改废）→ 装置必须判红")
    a = ap.parse_args()

    base = load_base()
    import group_watch_delivery as D
    import group_watch_pending as P
    mod = {"pending": P, "delivery": D}
    if a.negative:
        P.HUMAN_IDS = ("nobody",)   # 注毒：域规则「谁是人的判定」改废 → 行为必偏

    fails = []
    for case in CASES:
        tb = run_case(base, "base", case)
        tn = run_case(mod, "new", case)
        same = json.dumps(tb, sort_keys=True, ensure_ascii=False) == json.dumps(tn, sort_keys=True, ensure_ascii=False)
        asks = sum(len(r["asks"]) for r in tn)
        posts = sum(len(r["posts"]) for r in tn)
        print("%-4s %s（轮 %d · 点名 %d · 写回 %d）"
              % ("一致" if same else "**不一致**", case["name"], len(case["rounds"]), asks, posts))
        if not same:
            fails.append(case["name"])
            for i, (x, y) in enumerate(zip(tb, tn)):
                if json.dumps(x, sort_keys=True, ensure_ascii=False) != json.dumps(y, sort_keys=True, ensure_ascii=False):
                    print("    轮%d 基线=%s" % (i + 1, json.dumps(x, ensure_ascii=False)[:400]))
                    print("    轮%d 改后=%s" % (i + 1, json.dumps(y, ensure_ascii=False)[:400]))
                    break
        elif a.verbose:
            for i, r in enumerate(tn):
                print("    轮%d acted=%d 点名=%s 写回=%s state=%s" % (i + 1, r["acted"],
                      [(x[4], x[5][:28]) for x in r["asks"]],
                      [(x[3], (x[1] or "")[:20], x[2]) for x in r["posts"]],
                      json.dumps(r["state"], ensure_ascii=False)[:160]))
    print("")
    if a.negative:
        if fails:
            print("负控 OK：注毒后装置判红 %d/%d 例 → %s（判红能力已证）"
                  % (len(fails), len(CASES), ", ".join(fails[:3])))
            return 1
        print("负控 FAIL：注毒后装置仍判绿 —— 装置失效")
        return 2
    print("对账 %d/%d 例逐字节一致%s" % (len(CASES) - len(fails), len(CASES),
                                   "" if not fails else " · 不一致：" + ", ".join(fails)))
    rc = 0 if not fails else 1
    print("rc=%d" % rc)
    return rc


if __name__ == "__main__":
    sys.exit(main())
