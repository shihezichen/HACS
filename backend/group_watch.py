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

批④ 分层（本件 = 组合根 C，只做进程入口）：
  * 投递侧 L1 → backend/group_watch_delivery.py（口令探针 / 日志 / HTTP / 状态 / 点名 / 写回）
  * 待回应账域侧 L3 → backend/group_watch_pending.py（扫账 / 域规则 / 提示词 / 单轮编排）
  本件只保留 argv 解析 + 装配注入 + 分派 + 入口呈现。argv 与输出语义一字未改
  （Windows 计划任务 AgentTeamGroupWatch 的 <Arguments> 指向本文件名 group_watch.py，不可改名）。
"""
from __future__ import annotations

import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
# 批⑤b：L1 投递侧已归位 node/l1_transport/，故仓根也要入搜索路径（HERE 仍是本目录、语义不变）。
for _hacs_p in (os.path.dirname(HERE), HERE):
    if _hacs_p not in sys.path:
        sys.path.insert(0, _hacs_p)

from node.l1_transport import group_watch_delivery as _delivery  # noqa: E402  L1 投递侧：argv 默认值 / 口令探针 / 状态 / 日志
from group_watch_pending import one_round  # noqa: E402  L3 待回应账域侧：单轮编排（C→L3→L1，方向合法）


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chat-base", default=_delivery.CHAT_BASE_DEFAULT)
    ap.add_argument("--nats-host", default=_delivery.NATS_HOST_DEFAULT)
    ap.add_argument("--nats-user", default="hermes")
    ap.add_argument("--nats-pass-file", default=_delivery.default_pass_file())
    ap.add_argument("--state", default=_delivery.STATE_DEFAULT)
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
        _delivery.LOG_FILE = args.log_file

    try:
        with open(args.nats_pass_file, "r", encoding="utf-8") as f:
            args.nats_pass = f.read().strip()
    except Exception as exc:
        _delivery.log("读不到 NATS 口令:", exc)
        return 2

    state = _delivery.State(args.state)
    _delivery.log("群守望器启动 · chat=%s · 每 %.0fs 一轮 · 间隔 %.0fs" % (args.chat_base, args.interval, args.gap))
    while True:
        try:
            n = one_round(args, state)
            if n:
                _delivery.log("本轮回话", n, "条")
        except KeyboardInterrupt:
            _delivery.log("收到退出信号，收工")
            state.save()
            return 0
        except Exception as exc:
            _delivery.log("本轮异常(下一轮继续):", "%s: %s" % (type(exc).__name__, exc))
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
