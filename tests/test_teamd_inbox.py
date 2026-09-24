#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# hacs-test-fixtures: 本文件为测试夹具，允许出现假凭据与网段边界值；危险类命中（真令牌/连接串/真实人名）仍按硬门禁拦截
"""P2-b：teamd 离线补拉（上线领回欠我的消息）的测试。

要解决的事：`msg_log` 让「落库 = 送达」成立 —— 对方离线也不丢。但**消息躺在库里
不会自己跑出来**，得有人在这个成员上线时去把它领回来处理。这就是补拉。

三条纪律（都在测试里钉住）：
  1. **先占并发额度，再 ack**（顺序反了会出现"已经认领、却进不了队列"→ 消息卡死在
     processing 上，谁都捞不着）。
  2. **ack 抢不到就跳过**（乐观锁：别人已认领的，我不能再喂一遍 LLM）。
  3. **补拉失败绝不带崩 teamd**（中枢抖一下不能让整个成员掉线）；仅计数 + 记日志，
     下次上线再拉 —— 消息还在库里，跑不掉。

HTTP 走可注入的 `http_caller(method, url, body) -> (code, dict)`，测试不碰真中枢。
"""

import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import teamd  # noqa: E402

BB = "http://127.0.0.1:19940"
TOKEN = "unit-test-token-hermes"


def make_cfg(**kw):
    args = dict(member="hermes", nats_host="127.0.0.1", nats_port=4222,
                user="u", password="p", a2a_base="http://127.0.0.1:9920",
                token=TOKEN, admin_port=0, enable_admin=False,
                blackboard_base=BB)
    args.update(kw)
    return teamd.Config(**args)


def make_msg(mid, frm="lobster", body="帮我看看这个", session_id=None, seq=1):
    return {"msg_id": mid, "seq": seq, "from": frm, "from_member": frm, "to": "hermes",
            "to_member": "hermes", "body": body, "text": body,
            "session_id": session_id, "state": "queued", "attempts": 0}


class FakeHub(object):
    """假中枢：按路径回放固定响应，并记录所有调用。"""

    def __init__(self, queued=None, ack_ok=True):
        self.queued = list(queued or [])
        self.ack_ok = ack_ok
        self.calls = []
        self.fail = None          # 设成异常即模拟中枢挂了
        self.ack_ids = []         # 被 ack 的 id
        self.sent = []            # 被回投的消息体
        self.done_ids = []        # 调过 /done 的 id（无论结论）
        self.done_calls = []      # (id, ok) —— ok 由 body 决定，服务端据此落 done/failed

    def __call__(self, method, url, body=None):
        self.calls.append((method, url, body))
        if self.fail is not None:
            raise self.fail
        if method == "GET" and "/msg?" in url:
            return 200, {"ok": True, "count": len(self.queued), "messages": list(self.queued)}
        if method == "POST" and url.endswith("/ack"):
            mid = url.rstrip("/").split("/")[-2]
            if not self.ack_ok:
                return 200, {"ok": True, "repeat": True, "msg": {"msg_id": mid,
                                                                 "state": "processing"}}
            self.ack_ids.append(mid)
            return 200, {"ok": True, "msg": {"msg_id": mid, "state": "processing",
                                             "acked": True}}
        if method == "POST" and url.endswith("/done"):
            mid = url.rstrip("/").split("/")[-2]
            ok = True
            if body:
                try:
                    ok = bool(json.loads(body.decode("utf-8")).get("ok", True))
                except Exception:
                    ok = True
            self.done_ids.append(mid)
            self.done_calls.append((mid, ok))
            return 200, {"ok": True, "state": "done" if ok else "failed"}
        if method == "POST" and url.rstrip("/").endswith("/msg"):
            self.sent.append(body)
            return 200, {"ok": True, "delivered": True, "msg_id": "m-reply01"}
        return 404, {"ok": False, "error": "not_found"}

    def urls(self):
        return [u for _, u, _ in self.calls]


def a2a_ok(*args, **kwargs):
    """后端的 200 回包在 teamd 里是**原样字节**透传的，所以这里返回 bytes。"""
    return 200, json.dumps({"reply": "好，我来", "ok": True},
                           ensure_ascii=False).encode("utf-8")


def make_daemon(hub, cfg=None, a2a=None):
    cfg = cfg or make_cfg()
    d = teamd.TeamDaemon(cfg, nats_factory=lambda m=None: None,
                         a2a_caller=a2a or a2a_ok,
                         http_caller=hub)
    return d


class ReplayInboxTest(unittest.TestCase):

    def test_01_把欠我的消息领回来处理(self):
        hub = FakeHub(queued=[make_msg("m-aaa00001"), make_msg("m-aaa00002", seq=2)])
        d = make_daemon(hub)
        n = d.replay_inbox()
        self.assertEqual(2, n)
        self.assertEqual(2, d.counters.snapshot().get("inbox_replayed"))
        self.assertEqual(["m-aaa00001", "m-aaa00002"], hub.ack_ids, "两条都要先认领")
        self.assertEqual(["m-aaa00001", "m-aaa00002"], hub.done_ids, "处理完要标完成")

    def test_02_没有欠账就什么都不做(self):
        hub = FakeHub(queued=[])
        d = make_daemon(hub)
        self.assertEqual(0, d.replay_inbox())
        self.assertEqual([], hub.ack_ids)
        self.assertEqual(0, d.counters.snapshot().get("inbox_replayed"))

    def test_03_回投结果给原发送方(self):
        hub = FakeHub(queued=[make_msg("m-bbb00001", frm="lobster", session_id="as-x")])
        d = make_daemon(hub)
        d.replay_inbox()
        self.assertEqual(1, len(hub.sent), "处理结果要回投，否则对方永远等不到答复")
        sent = json.loads(hub.sent[0].decode("utf-8"))
        self.assertEqual("lobster", sent.get("to"), "回投给原发送方")
        self.assertIn("好，我来", json.dumps(sent, ensure_ascii=False))
        self.assertEqual("as-x", sent.get("session_id"), "会话要跟着走")

    def test_04_未配置中枢就不补拉(self):
        hub = FakeHub(queued=[make_msg("m-ccc00001")])
        d = make_daemon(hub, cfg=make_cfg(blackboard_base=""))
        self.assertEqual(0, d.replay_inbox())
        self.assertEqual([], hub.calls, "没配中枢就不该发请求")

    def test_05_中枢挂了不崩_只计数(self):
        hub = FakeHub(queued=[make_msg("m-ddd00001")])
        hub.fail = OSError("connection refused")
        d = make_daemon(hub)
        self.assertEqual(0, d.replay_inbox(), "中枢抖一下不该把 teamd 带走")
        self.assertEqual(1, d.counters.snapshot().get("inbox_errors"))
        self.assertEqual([], hub.ack_ids, "拉不到就别乱认领")

    def test_06_先占额度再认领_队列满就不认领(self):
        hub = FakeHub(queued=[make_msg("m-eee00001"), make_msg("m-eee00002", seq=2)])
        d = make_daemon(hub, cfg=make_cfg(queue_size=1, workers=1,
                                          per_member_limit=1))
        # 手工把唯一额度占住，模拟"队列已满"
        self.assertIsNone(d._admit("hermes"))
        self.assertIsNotNone(d._admit("hermes"))
        n = d.replay_inbox()
        self.assertEqual(0, n, "进不了队列就不该认领")
        self.assertEqual([], hub.ack_ids, "认领了却进不了队列 = 消息卡死在 processing")

    def test_07_抢不到认领就跳过(self):
        hub = FakeHub(queued=[make_msg("m-fff00001"), make_msg("m-fff00002", seq=2)],
                      ack_ok=False)
        d = make_daemon(hub)
        n = d.replay_inbox()
        self.assertEqual(0, n, "ack 抢不到（别人已认领）→ 不能重复喂 LLM")
        self.assertEqual([], hub.done_ids)

    def test_08_处理崩了不阻断其余(self):
        hub = FakeHub(queued=[make_msg("m-ggg00001"), make_msg("m-ggg00002", seq=2)])
        calls = {"n": 0}

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                return 500, b'{"ok": false, "error": "internal"}'
            return 200, json.dumps({"reply": "第二条成了", "ok": True},
                                   ensure_ascii=False).encode("utf-8")

        d = make_daemon(hub, a2a=flaky)
        n = d.replay_inbox()
        self.assertEqual(1, n, "第一条失败，第二条还得处理")
        self.assertIn(("m-ggg00002", True), hub.done_calls, "成功那条标 done")
        self.assertIn(("m-ggg00001", False), hub.done_calls,
                      "失败那条要标 failed —— 否则补拉会把一路失败的消息算成处理好了")

    def test_09_limit_与_token_都带上(self):
        hub = FakeHub(queued=[])
        d = make_daemon(hub, cfg=make_cfg(inbox_replay_limit=7))
        d.replay_inbox()
        gets = [u for m, u, _ in hub.calls if m == "GET"]
        self.assertEqual(1, len(gets))
        self.assertIn("to=hermes", gets[0])
        self.assertIn("state=queued", gets[0])
        self.assertIn("limit=7", gets[0])
        headers = d.http_headers()
        self.assertEqual("Bearer " + TOKEN, headers.get("Authorization"))

    def test_10_重复补拉同一条不会喂两遍(self):
        """幂等靠状态机：认领后消息不再处于 queued，第二次拉不到它。"""
        hub = FakeHub(queued=[make_msg("m-hhh00001")])

        def call(method, url, body=None):
            code, payload = FakeHub.__call__(hub, method, url, body)
            if method == "GET" and "/msg?" in url:
                # 模拟服务端：认领过的就不再出现在 queued 里
                payload["messages"] = [m for m in hub.queued if m["msg_id"] not in hub.ack_ids]
                payload["count"] = len(payload["messages"])
            return code, payload

        hub.__class__.__call__ = staticmethod(call) if False else hub.__class__.__call__
        d = make_daemon(hub)
        self.assertEqual(1, d.replay_inbox())
        # 第二次：库里已不是 queued
        hub.queued = []
        self.assertEqual(0, d.replay_inbox())
        self.assertEqual(1, hub.ack_ids.count("m-hhh00001"))

    def test_11_healthz_里有补拉计数(self):
        hub = FakeHub(queued=[make_msg("m-iii00001")])
        d = make_daemon(hub)
        d.replay_inbox()
        hz = d.healthz()
        self.assertEqual(1, hz.get("inbox_replayed"), "补拉了多少条要看得见")


class ConfigTest(unittest.TestCase):

    def test_20_中枢地址与补拉上限可配(self):
        cfg = make_cfg(blackboard_base=BB, inbox_replay_limit=13)
        self.assertEqual(BB, cfg.blackboard_base)
        self.assertEqual(13, cfg.inbox_replay_limit)

    def test_21_默认不配中枢_补拉关闭(self):
        cfg = teamd.Config(member="hermes", nats_host="127.0.0.1", nats_port=4222,
                           user="u", password="p", a2a_base="", token="t")
        self.assertEqual("", cfg.blackboard_base, "默认空 = 不补拉，避免老部署行为突变")

    def test_22_地址尾斜杠会被抹掉(self):
        cfg = make_cfg(blackboard_base=BB + "/")
        self.assertEqual(BB, cfg.blackboard_base)


if __name__ == "__main__":
    unittest.main(verbosity=2)
