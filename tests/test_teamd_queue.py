#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# hacs-test-fixtures: 本文件为测试夹具，允许出现假凭据与网段边界值；危险类命中（真令牌/连接串/真实人名）仍按硬门禁拦截
"""
test_teamd_queue.py —— teamd.py「有界队列 + worker 池 + busy 拒收」自测（P1-A）

直接跑：python tests/test_teamd_queue.py

契约：消息可靠性与会话-测试用例与接口契约-v1.md §4（Q-01..10）

背景（OpenClaw 评审硬伤 1）：原先 `on_nats_message` 是「每收一条开一个新线程」，
无上限、不排队、不拒绝。一旦改成异步 ACK 或离线补拉（一次投一批），
后端 LLM 会被瞬间打爆。本档把它换成：**有界队列 + 固定 worker 池 + 每成员在途上限**，
满了**明确拒收 busy**（不排队、不静默丢）。

关键语义：**拒收 ≠ 丢弃** —— 消息此刻已在中枢落库（落库=送达），
对方有空时可补投；拒掉的只是「这一刻的并发额度」。
"""

import json
import os
import sys
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import teamd  # noqa: E402
from teamd import Config, TeamDaemon  # noqa: E402

TOKEN = "test-token-not-a-secret"


class FakeNats(object):
    """只记录、不联网的假传输。"""

    def __init__(self):
        self.subs = {}
        self.published = []          # [(subject, payload, reply)]
        self.is_connected = True
        self.last_err = None
        self.closed = False
        self._lock = threading.Lock()

    def connect(self):
        self.is_connected = True
        return self

    def subscribe(self, subject, cb, sid="1"):
        self.subs[subject] = cb
        return self

    def publish(self, subject, payload=b"", reply=""):
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        with self._lock:
            self.published.append((subject, payload, reply))
        return self

    def flush(self, timeout=5.0):
        return self.is_connected

    def close(self):
        self.closed = True
        self.is_connected = False

    def deliver(self, subject, payload, reply=""):
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        self.subs[subject](payload, reply)

    def wait_publish(self, subject, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                for item in self.published:
                    if item[0] == subject:
                        return item
            time.sleep(0.02)
        return None

    def publishes_for(self, subject):
        with self._lock:
            return [it for it in self.published if it[0] == subject]


class CallerStats(object):
    """假 A2A caller 的共享状态：记录调用、并发峰值、可按需阻塞。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.calls = []
        self.live = 0
        self.peak = 0
        self.gate = None          # 设成 Event 时，caller 会一直等到它被 set

    def caller(self, text, session_id, timeout_s, caller):
        with self.lock:
            self.calls.append({"text": text, "session_id": session_id, "caller": caller})
            self.live += 1
            self.peak = max(self.peak, self.live)
        try:
            gate = self.gate
            if gate is not None:
                gate.wait(10.0)
            else:
                time.sleep(0.05)
            body = json.dumps({"ok": True, "reply": "pong:" + text,
                               "session_id": session_id}, ensure_ascii=False).encode("utf-8")
            return 200, body
        finally:
            with self.lock:
                self.live -= 1


def make_daemon(**kw):
    """构造一个不联网的 daemon（假传输 + 假 A2A），默认小池子便于压出上限。"""
    stats = CallerStats()
    cfg = Config(member="hermes", nats_host="127.0.0.1", nats_port=9930,
                 user="hermes", password="nats-secret-pw",
                 a2a_base="http://127.0.0.1:1", token=TOKEN,
                 enable_admin=False, hb_interval=3600.0, ping_interval=3600.0,
                 queue_size=kw.pop("queue_size", 4),
                 workers=kw.pop("workers", 1),
                 per_member_limit=kw.pop("per_member_limit", 1))
    fake = FakeNats()
    daemon = TeamDaemon(cfg, nats_factory=lambda: fake, a2a_caller=stats.caller)
    daemon._connect_once()
    return daemon, fake, stats


def req(text, reply="", session_id="s-q", frm="hyperion"):
    return json.dumps({"text": text, "session_id": session_id, "timeout_s": 5,
                       "from": frm}, ensure_ascii=False).encode("utf-8")


class TeamdQueueTest(unittest.TestCase):

    def tearDown(self):
        if getattr(self, "daemon", None) is not None:
            self.daemon.stop()

    # ---------------------------------------------------------------- Q-01

    def test_01_normal_path_still_works(self):
        """Q-01 正常路径：收包 → 队列 → worker → 回包（整条线仍然通）。"""
        self.daemon, self.fake, _ = make_daemon()
        self.fake.deliver("a2a.v1.req.hermes", req("你好"), "_INBOX.Q1")
        item = self.fake.wait_publish("_INBOX.Q1", 8.0)
        self.assertIsNotNone(item, "正常请求必须能等到回包")
        body = json.loads(item[1].decode("utf-8"))
        self.assertTrue(body.get("ok"), "回包应 ok=true，实得 %r" % (body,))
        self.assertIn("pong:你好", json.dumps(body, ensure_ascii=False))

    # ---------------------------------------------------------------- Q-02

    def test_02_queue_full_rejects_busy(self):
        """Q-02 队列满 → 立刻回 busy，不排队、不静默丢。"""
        self.daemon, self.fake, stats = make_daemon(queue_size=1, workers=1,
                                                    per_member_limit=100)
        stats.gate = threading.Event()          # 让第一条任务卡在 caller 里
        self.fake.deliver("a2a.v1.req.hermes", req("第一条"), "_INBOX.Q2a")
        time.sleep(0.2)                          # 让 worker 取走第一条、队列空出来
        self.fake.deliver("a2a.v1.req.hermes", req("第二条"), "_INBOX.Q2b")   # 占满队列
        self.fake.deliver("a2a.v1.req.hermes", req("第三条"), "_INBOX.Q2c")   # 应被拒
        item = self.fake.wait_publish("_INBOX.Q2c", 5.0)
        self.assertIsNotNone(item, "被拒的请求也必须收到回包（不能让它干等）")
        body = json.loads(item[1].decode("utf-8"))
        self.assertFalse(body.get("ok"))
        self.assertEqual(body.get("error"), "busy", "实得 %r" % (body,))
        stats.gate.set()

    # ---------------------------------------------------------------- Q-03

    def test_03_per_member_limit_rejects_busy(self):
        """Q-03 单成员在途超上限 → busy（一个 agent 不该自我雪崩）。"""
        self.daemon, self.fake, stats = make_daemon(queue_size=100, workers=4,
                                                    per_member_limit=1)
        stats.gate = threading.Event()
        self.fake.deliver("a2a.v1.req.hermes", req("第一条"), "_INBOX.Q3a")
        time.sleep(0.2)                          # 第一条进入在途
        self.fake.deliver("a2a.v1.req.hermes", req("第二条"), "_INBOX.Q3b")
        item = self.fake.wait_publish("_INBOX.Q3b", 5.0)
        self.assertIsNotNone(item, "超限的请求必须收到 busy 回包")
        body = json.loads(item[1].decode("utf-8"))
        self.assertEqual(body.get("error"), "busy", "实得 %r" % (body,))
        self.assertIn("在途", body.get("detail") or "")
        stats.gate.set()

    # ---------------------------------------------------------------- Q-04

    def test_04_busy_body_shape(self):
        """Q-04 busy 回包形态：ok=false / error=busy / retry_after_ms / 可读 detail。"""
        self.daemon, self.fake, stats = make_daemon(queue_size=1, workers=1,
                                                    per_member_limit=100)
        stats.gate = threading.Event()
        self.fake.deliver("a2a.v1.req.hermes", req("第一条"), "_INBOX.Q4a")
        time.sleep(0.2)
        self.fake.deliver("a2a.v1.req.hermes", req("第二条"), "_INBOX.Q4b")
        self.fake.deliver("a2a.v1.req.hermes", req("第三条"), "_INBOX.Q4c")
        item = self.fake.wait_publish("_INBOX.Q4c", 5.0)
        body = json.loads(item[1].decode("utf-8"))
        self.assertEqual(body.get("ok"), False)
        self.assertEqual(body.get("error"), "busy")
        self.assertEqual(body.get("retry_after_ms"), teamd.BUSY_RETRY_AFTER_MS)
        self.assertTrue(body.get("detail"), "detail 不该为空（要能看出为什么被拒）")
        stats.gate.set()

    # ---------------------------------------------------------------- Q-05

    def test_05_on_nats_message_returns_immediately(self):
        """Q-05 收包回调**立刻**返回（reader 线程绝不能被任务耗时拖住）。"""
        self.daemon, self.fake, stats = make_daemon(queue_size=64, workers=2,
                                                    per_member_limit=4)
        stats.gate = threading.Event()           # 任务会一直卡住
        began = time.time()
        self.fake.deliver("a2a.v1.req.hermes", req("卡住的任务"), "_INBOX.Q5")
        elapsed = time.time() - began
        self.assertLess(elapsed, 0.5,
                        "deliver 用了 %.3fs —— 回调里在做耗时的活，会阻塞 NATS reader" % elapsed)
        stats.gate.set()

    # ---------------------------------------------------------------- Q-06

    def test_06_concurrency_capped_by_workers(self):
        """Q-06 并发上限 = workers：投 6 条，同时最多只有 workers 条在跑。"""
        self.daemon, self.fake, stats = make_daemon(queue_size=64, workers=2,
                                                    per_member_limit=64)
        for i in range(6):
            self.fake.deliver("a2a.v1.req.hermes", req("任务%d" % i), "_INBOX.Q6.%d" % i)
        deadline = time.time() + 8.0
        while time.time() < deadline and len(stats.calls) < 6:
            time.sleep(0.05)
        self.assertGreaterEqual(len(stats.calls), 6, "6 条任务都该被处理（只是排队）")
        self.assertLessEqual(stats.peak, 2,
                             "并发峰值 %d 超过 workers=2 —— 闸门没生效" % stats.peak)

    # ---------------------------------------------------------------- Q-07

    def test_07_reject_does_not_lose_the_request(self):
        """Q-07 拒收 ≠ 丢弃：被拒的仍然是完整请求（有 reply 通道、有 caller 可追溯）。"""
        self.daemon, self.fake, stats = make_daemon(queue_size=1, workers=1,
                                                    per_member_limit=100)
        stats.gate = threading.Event()
        self.fake.deliver("a2a.v1.req.hermes", req("第一条"), "_INBOX.Q7a")
        time.sleep(0.2)
        self.fake.deliver("a2a.v1.req.hermes", req("第二条"), "_INBOX.Q7b")
        self.fake.deliver("a2a.v1.req.hermes", req("第三条", reply="", frm="lobster"), "_INBOX.Q7c")
        item = self.fake.wait_publish("_INBOX.Q7c", 5.0)
        self.assertIsNotNone(item, "即使队列满，也必须把结果送回它指定的 reply 通道")
        self.assertEqual(item[0], "_INBOX.Q7c",
                         "回包必须发到原请求指定的 reply 地址(inbox)，不能发丢")
        stats.gate.set()

    # ---------------------------------------------------------------- Q-08

    def test_08_counter_increases(self):
        """Q-08 busy_rejected 计数递增（可观测：能看出被拒了多少次）。"""
        self.daemon, self.fake, stats = make_daemon(queue_size=1, workers=1,
                                                    per_member_limit=100)
        stats.gate = threading.Event()
        before = self.daemon.counters.snapshot().get("busy_rejected", 0)
        self.fake.deliver("a2a.v1.req.hermes", req("第一条"), "_INBOX.Q8a")
        time.sleep(0.2)
        self.fake.deliver("a2a.v1.req.hermes", req("第二条"), "_INBOX.Q8b")
        self.fake.deliver("a2a.v1.req.hermes", req("第三条"), "_INBOX.Q8c")
        after = self.daemon.counters.snapshot().get("busy_rejected", 0)
        self.assertGreater(after, before, "拒收了却没计数")
        stats.gate.set()

    # ---------------------------------------------------------------- Q-09

    def test_09_queue_stats_shape(self):
        """Q-09 queue_stats 快照：healthz 靠它暴露闸门状态。"""
        self.daemon, self.fake, _ = make_daemon(queue_size=7, workers=3,
                                                per_member_limit=5)
        q = self.daemon.queue_stats()
        self.assertEqual(q["capacity"], 7)
        self.assertEqual(q["workers"], 3)
        self.assertEqual(q["per_member_limit"], 5)
        self.assertIn("depth", q)
        self.assertIsInstance(q["inflight"], dict)

    def test_09b_healthz_exposes_queue(self):
        """Q-09b healthz 里能直接看到队列（运维不必猜）。"""
        self.daemon, self.fake, _ = make_daemon()
        h = self.daemon.healthz()
        self.assertIn("queue", h)
        self.assertEqual(h["queue"]["capacity"], self.daemon.cfg.queue_size)

    # ---------------------------------------------------------------- Q-10

    def test_10_workers_lazily_started(self):
        """Q-10 懒启动：没人显式调 start_workers 也能消费（避免静默不处理）。"""
        self.daemon, self.fake, _ = make_daemon()
        self.assertFalse(self.daemon._workers_started, "构造时不该已经起好线程池")
        self.fake.deliver("a2a.v1.req.hermes", req("懒启动"), "_INBOX.Q10")
        self.assertTrue(self.daemon._workers_started, "收到请求后必须自动起消费者")
        item = self.fake.wait_publish("_INBOX.Q10", 8.0)
        self.assertIsNotNone(item, "懒启动之后任务照样要被处理")

    def test_10b_start_workers_is_idempotent(self):
        """Q-10b 重复调用 start_workers 只起一份池子。"""
        self.daemon, self.fake, _ = make_daemon(workers=2)
        self.daemon.start_workers()
        n1 = len([t for t in self.daemon._threads if t.name.startswith("teamd-worker")])
        self.daemon.start_workers()
        n2 = len([t for t in self.daemon._threads if t.name.startswith("teamd-worker")])
        self.assertEqual(n1, 2)
        self.assertEqual(n2, 2, "重复调用不该再起一批 worker")

    # ---------------------------------------------------------------- 默认值

    def test_defaults_are_conservative(self):
        """默认值要保守：后端是本地 LLM 网关，并发开大只会雪崩。"""
        self.assertLessEqual(teamd.DEFAULT_WORKERS, 8)
        self.assertGreaterEqual(teamd.DEFAULT_QUEUE_SIZE, teamd.DEFAULT_WORKERS)
        self.assertGreaterEqual(teamd.DEFAULT_PER_MEMBER_LIMIT, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
