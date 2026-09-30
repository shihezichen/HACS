#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
team.py —— 成员侧统一通信 CLI（"成员平等"的核心）

背景：过去只有总控（<MEMBER_ID>）配齐了 team_call.py / team_broadcast.py / team_roster.py 这套
工具，其他成员只能"被点名"，自己发不出呼叫。本工具让**任意一个成员**在 <MEMBER_ID> 不在线、
主中枢（NATS 主中枢）也不在线（走 <MEMBER_ID> 热备）的情况下，仍能主动：
  * 点对点呼叫队友（say）
  * 群发消息（broadcast）
  * 召集临时会议（meet）并留痕回放（history）
  * 查看谁在线（who）

全程只走 NATS 中枢（<NODE_IP_1>:9930 主 / <NODE_IP_2>:9930 备，逐个尝试到连上），
不依赖总控、不依赖黑板/PG——这是"正式会议才写黑板"之外的降级通道。

复用现有实现，不重造轮子：
  * 多地址故障转移能力来自改造过的 nats_client.NatsClient（host 支持逗号分隔/list）。
  * 群发的并行扇出/在线名单发现（call_one / discover_online / run_pool）内联自总控侧
    同名工具的实现，成员侧只下发本文件 + nats_client.py，不依赖那份工具本身。
  * 点对点请求/回包解析的写法与 team_call.py 保持一致（一致的错误提示与退出码）。

身份与连接参数（尽量少输）：
  --as <成员>       显式指定自己是谁；省略时自动推断——
                    Linux：扫 <PASS_DIR>/*.pass，要求**恰好一个**；
                    Windows：默认 <MEMBER_ID>，但若 <PASS_DIR>\\nats-*.pass 存在多个，
                    视为身份不明确，报错要求显式 --as。
  --password-file   默认按平台 + 上面解出的成员名拼路径；可覆盖。
  --nats-host       默认多地址 "<NODE_IP_1>:9930,<NODE_IP_2>:9930"；可逗号分隔多个覆盖。
  --nats-port       地址未显式带端口时的兜底端口，默认 9930。
  --timeout         等回应上限秒，默认 120，最大 300。
  --json            打印原始 JSON。

节流防轰炸（broadcast / meet 共用）：
  发送前用 sha1(主题归一化 + 正文) 查本地去重文件（默认 ~/.team_broadcast_dedup.json），
  30 分钟窗口内命中就跳过（退出码 0），--force 可强制重发；发送失败会回滚去重标记。

退出码约定（与 team_call.py / team_broadcast.py 一致）：
  0 成功 / 1 无应答或超时（或有成员缺席） / 2 参数或连接错误 / 3 对方回了错误结构（或名单为空）

用法示例：
  python team.py who
  python team.py say <MEMBER_ID> "你在吗？"
  python team.py broadcast --all "全体注意：..."
  python team.py broadcast --to <MEMBER_ID_A>,<MEMBER_ID_B> --text-file msg.md
  python team.py meet --goal "对齐明天的发布计划" --agenda "确认范围" --agenda "定风险点" \\
      --members <MEMBER_ID_A>,<MEMBER_ID_B>,<MEMBER_ID_C>
  python team.py history --session-id <SESSION_ID>
"""

import argparse
import json
import os
import random
import string
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nats_client import NatsClient  # noqa: E402
# 刀15（阶段3）：本件改归组合根 C（进程入口装配），实现按层拆走，这里只做 import 装配。
from team_cli_core import (  # noqa: E402  L1 通信层：连接扇出 / 身份口令 / 去重节流；cmd_who/cmd_say/cmd_broadcast 亦为 L1 实现，C 侧只 re-export（属性面不变）
    AGENT_TEAM_NATS_HOST,
    AGENT_TEAM_RUNTIME_DIR,
    DEDUP_WINDOW_SECONDS,
    DEFAULT_DEDUP_FILE,
    DEFAULT_NATS_HOSTS,
    DEFAULT_PORT,
    DEFAULT_TIMEOUT,
    HB_PREFIX,
    MAX_TIMEOUT,
    REQ_FMT,
    TeamCliError,
    build_say_envelope,
    call_one,
    check_and_mark_dedup,
    clamp_timeout,
    cmd_broadcast,
    cmd_say,
    cmd_who,
    collect_heartbeats,
    derive_default_session,
    discover_online,
    infer_member,
    make_say_session_id,
    read_secret_file,
    read_text,
    resolve_member_and_password,
    resolve_password_file,
    resolve_say_session_id,
    rollback_dedup,
    run_pool,
)   # cmd_who/cmd_say/cmd_broadcast + say 辅助函数：L1 实现，C 侧只做 re-export（属性面不变）
from team_group import (  # noqa: E402  L3 协作域：群聊 + 会议（C 侧只做 re-export，属性面与搬迁前一致）
    DEFAULT_CHAT_BASE,
    DEFAULT_MEETINGS_FILE,
    append_jsonl,
    build_meeting_body,
    cmd_group_pull,
    cmd_group_say,
    cmd_group_show,
    cmd_history,
    cmd_meet,
    make_session_id,
)

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass  # 老版本 Python / 非标准 stream 兜底：拿不到就算了，别把整个命令搞挂


# ---------------------------------------------------------------- who：谁在线



# ---------------------------------------------------------------- 参数解析

def build_common_parser():
    # type: () -> argparse.ArgumentParser
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--as", dest="as_member", default=None,
                   help="显式指定自己是谁；省略时自动推断（Linux 扫 <PASS_DIR>/*.pass 取唯一一个，"
                        "Windows 默认 <MEMBER_ID>，若口令文件不唯一则报错要求显式指定）")
    p.add_argument("--password-file", default=None,
                   help="自己的 NATS 口令文件；默认按平台 + --as 推断出的成员名拼路径")
    p.add_argument("--nats-host", default=DEFAULT_NATS_HOSTS,
                   help="NATS 中枢地址，逗号分隔可给多个做故障转移（默认：<NODE_IP_1> 主/<NODE_IP_2> 备）")
    p.add_argument("--nats-port", type=int, default=DEFAULT_PORT,
                   help="兜底端口，仅用于地址里没显式带端口的项（默认 9930）")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                   help="等回应上限秒（默认 120，最大 300）")
    p.add_argument("--json", action="store_true", help="打印原始 JSON")
    return p


def build_parser():
    # type: () -> argparse.ArgumentParser
    common = build_common_parser()
    ap = argparse.ArgumentParser(
        prog="team.py",
        description="成员侧统一通信 CLI（成员平等）：点对点呼叫 / 群发 / 召集临时会议，"
                     "全程只走 NATS，不依赖总控/黑板")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_who = sub.add_parser("who", parents=[common], help="谁在线：扫心跳打印在线成员")
    p_who.add_argument("--seconds", type=float, default=35.0,
                       help="扫描窗口秒（默认 35，心跳周期 30s，给太短会漏人）")
    p_who.set_defaults(func=cmd_who)

    p_say = sub.add_parser("say", parents=[common], help="点对点呼叫某个成员，等其回应")
    p_say.add_argument("target", help="被叫成员 id，如 <MEMBER_ID>")
    p_say.add_argument("text", nargs="?", default=None, help="正文（中文长文本建议改用 --text-file）")
    p_say.add_argument("--text-file", default=None, help="正文文件（UTF-8）")
    p_say.add_argument("--session-id", default=None,
                       help="会话 id（同一 id 对方有上下文）；不给则按 发送方-目标-小时 自动派生")
    p_say.add_argument("--new-session", action="store_true",
                       help="强制开一段新会话（不接上文）；与 --session-id 互斥")
    p_say.set_defaults(func=cmd_say)

    p_bc = sub.add_parser("broadcast", parents=[common], help="群发一批成员并收回应")
    p_bc.add_argument("text", nargs="?", default=None, help="正文（中文长文本建议改用 --text-file）")
    p_bc.add_argument("--text-file", default=None, help="正文文件（UTF-8）")
    p_bc.add_argument("--all", action="store_true", help="面向全部在线成员（先扫心跳取名单）")
    p_bc.add_argument("--to", default=None, help="指定成员，逗号分隔（与 --all 二选一）")
    p_bc.add_argument("--session-id", default=None, help="会话 id（同一 id 各成员有上下文）")
    p_bc.add_argument("--concurrency", type=int, default=3, help="并行度（默认 3）")
    p_bc.add_argument("--roster-seconds", type=float, default=40.0, help="--all 扫心跳窗口秒（默认 40）")
    p_bc.add_argument("--dedup-file", default=DEFAULT_DEDUP_FILE,
                      help="去重记录文件（默认 ~/.team_broadcast_dedup.json）")
    p_bc.add_argument("--force", action="store_true", help="跳过 30 分钟去重检查，强制重发")
    p_bc.set_defaults(func=cmd_broadcast)

    p_meet = sub.add_parser("meet", parents=[common], help="召集临时会议：广播议程 + 收发言 + 出结论")
    p_meet.add_argument("--goal", required=True, help="会议目标")
    p_meet.add_argument("--agenda", action="append", default=None,
                        help="议程条目（标题+预期产出），可重复给出多条")
    p_meet.add_argument("--members", required=True, help="参会成员，逗号分隔")
    p_meet.add_argument("--session-id", default=None,
                        help="会话 id（默认自动生成 meet-<YYYYMMDD-HHMM>-<4位随机>）")
    p_meet.add_argument("--concurrency", type=int, default=3, help="并行度（默认 3）")
    p_meet.add_argument("--dedup-file", default=DEFAULT_DEDUP_FILE,
                        help="去重记录文件（默认 ~/.team_broadcast_dedup.json）")
    p_meet.add_argument("--force", action="store_true", help="跳过 30 分钟去重检查，强制重发")
    p_meet.add_argument("--out", default=DEFAULT_MEETINGS_FILE,
                        help="本地留痕文件（默认 ~/.team_meetings.jsonl），一行一场会议，只本机可见")
    p_meet.set_defaults(func=cmd_meet)

    p_hist = sub.add_parser("history", parents=[common], help="回放某场会议的召集与各成员发言")
    p_hist.add_argument("--session-id", required=True, help="要回放的会议 session_id")
    p_hist.add_argument("--file", default=DEFAULT_MEETINGS_FILE,
                        help="留痕文件（默认 ~/.team_meetings.jsonl）")
    p_hist.set_defaults(func=cmd_history)

    # -- 临时群（契约 §I）--------------------------------------------
    p_grp = sub.add_parser("group",
                           help="临时群：群里发言 / 拉全群增量（走对话台 HTTP，不是 NATS）")
    grp = p_grp.add_subparsers(dest="group_cmd", required=True)

    g_say = grp.add_parser("say", parents=[common],
                           help="在群里发言（Agent 必须 @；规则 6 由服务端硬门禁）")
    g_say.add_argument("--group", required=True, help="群 id（session_id，如 <SESSION_ID>）")
    g_say.add_argument("text", nargs="?", default=None, help="正文（长文建议 --text-file）")
    g_say.add_argument("--text-file", default=None, help="正文文件（UTF-8）")
    g_say.add_argument("--to", default="@all",
                       help="@all（默认，全员）或成员 id 逗号分隔，如 <MEMBER_ID>,<USER_ID>")
    g_say.add_argument("--reply-to", default=None, help="引用某条的 message_id（登记这是回应）")
    g_say.add_argument("--client-msg-id", default=None, help="幂等键，防重复提交")
    g_say.add_argument("--chat-base", default=DEFAULT_CHAT_BASE, help="对话台地址")
    g_say.set_defaults(func=cmd_group_say)

    g_pull = grp.add_parser("pull", parents=[common],
                            help="拉全群增量：谁说的都看得见＋我欠谁的回应")
    g_pull.add_argument("--group", required=True, help="群 id")
    g_pull.add_argument("--since", type=int, default=None, help="只取 seq 大于它的（增量拉）")
    g_pull.add_argument("--limit", type=int, default=50, help="最多几条（默认 50）")
    g_pull.add_argument("--chat-base", default=DEFAULT_CHAT_BASE, help="对话台地址")
    g_pull.set_defaults(func=cmd_group_pull)

    g_show = grp.add_parser("show", parents=[common],
                            help="看群详情：标题 / 状态 / 发起人 / 成员")
    g_show.add_argument("--group", required=True, help="群 id")
    g_show.add_argument("--chat-base", default=DEFAULT_CHAT_BASE, help="对话台地址")
    g_show.set_defaults(func=cmd_group_show)

    return ap


def main(argv=None):
    # type: (object) -> int
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except TeamCliError as exc:
        # 给 Agent 看的话，一行说清比整个堆栈有用
        print("错误：%s" % exc, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
