# -*- coding: utf-8 -*-
"""刀 22 基线 —— 改前树 c529b32 的**逐字节切片**（由 host_truth_equiv.py --gen 生成，勿手改）。

每条 = 契约 §1 清单里那一项在改前树上的完整语句原文（含它自己的宿主字面量）。
装置用它当「旧实现」：`exec` 进一个只有 `os` / `argparse` 的干净命名空间里求值，
所以它算出来的是**改前那个字面量**，与活树走 L0 的结果相互独立。
"""
import argparse
import os
import textwrap

#: 改前树的 commit
BASE_REV = 'c529b32'

SLICES = {
    'A1': 'AGENT_TEAM_BB_BASE = os.environ.get("AGENT_TEAM_BB_BASE") or "http://<host>:9940"\n',
    'A2a': 'AGENT_TEAM_CHAT_BASE = os.environ.get("AGENT_TEAM_CHAT_BASE") or "http://<host>:9984"\n',
    'A2b': 'AGENT_TEAM_NATS_HOST = os.environ.get("AGENT_TEAM_NATS_HOST") or os.environ.get("AGENT_TEAM_NATS") or "<host>:9930,<node-ip>:9930"\n',
    'A3': 'AGENT_TEAM_NATS_HOST = (os.environ.get("AGENT_TEAM_NATS_HOST")\n                        or "<host>:9930,<node-ip>:9930")  # <NODE_IP_1> 主 / <NODE_IP_2> 备\n',
    'A4a': '    parser.add_argument("--blackboard", default=os.environ.get("AGENT_TEAM_BB_BASE") or "http://<host>:9940",\n                        help="黑板服务地址（写入只走它的 POST /entries）")\n',
    'A4b': '    parser.add_argument("--nats-host", default=os.environ.get("AGENT_TEAM_NATS_HOST") or "<host>", help="NATS 中枢地址")\n',
    'A5': 'CHAT_BASE_DEFAULT = os.environ.get("AGENT_TEAM_CHAT_BASE") or "http://<host>:9984"\n',
    'A6': 'DEFAULT_CHAT_BASE = os.environ.get("AGENT_TEAM_CHAT_BASE") or os.environ.get("TEAM_CHAT_BASE") or "http://<host>:9984"\n',
    'A7': 'DEFAULT_HOST = os.environ.get("AGENT_TEAM_NATS_HOST") or "<host>"\n',
    'A8': 'DEFAULT_HOST = os.environ.get("AGENT_TEAM_NATS_HOST") or "<host>"\n',
    'A9': 'CHAT_BASE_DEFAULT = os.environ.get("CHAT_BASE") or "http://<host>:9984"\n',
    'A10': '    parser.add_argument("--nats-host", default=os.environ.get("AGENT_TEAM_NATS_HOST") or "<host>", help="NATS 中枢地址")\n',
    'A11': 'CHAT_BASE_DEFAULT = os.environ.get("AGENT_TEAM_CHAT_BASE") or "http://<host>:9984"\n',
    'A12': '    ap.add_argument("--nats-host", required=True, help="NATS 中枢地址（如 <host>）")\n',
    'B1a': 'AGENT_TEAM_NATS_DIR = os.environ.get("AGENT_TEAM_NATS_DIR") or "/etc/nats"\n',
    'B1b': 'ROUTE_PASS_FILE = "/root/.nats-route.pass"\n',
    'B2a': 'DB_DEFAULT = "<deploy-dir>/task_svc.db"\n',
    'B2b': 'LOG_DEFAULT = "<deploy-dir>/logs/task_svc.log"\n',
    'B3': 'CODEX_WORKDIR = os.environ.get("AGENT_TEAM_CODEX_WORKDIR") or os.environ.get("CODEX_WORKDIR") or r"<work-dir>"\n',
}


def evaluate(key, kind, name):
    """在**当前 env** 下求值一条旧切片。命名空间只给 os / argparse —— 旧件不认识 L0。

    切片按**原文逐字节**存着（含缩进），求值前只做 `textwrap.dedent`（整体退格，
    不改任何一个有效字符），否则 argparse 那几条带缩进的语句 exec 不了。
    """
    ns = {"os": os, "argparse": argparse}
    code = textwrap.dedent(SLICES[key])
    if kind == "argparse":
        parser = argparse.ArgumentParser(add_help=False)
        ns["parser"] = ns["ap"] = parser
        exec(code, ns)                            # noqa: S102 —— 跑的是自己仓里的冻结切片
        action = parser._actions[-1]
        return action.default, action.help
    exec(code, ns)                                # noqa: S102
    return ns[name], None
