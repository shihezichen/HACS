#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批④ 等价装置 · 层一：**逐字节切片对账 + 导出面守恒**（+ 负控能力自证）。

判据（不依赖 import 不报错 —— 目标件是常驻进程、仓内 0 import）：
  * 基线 = `git show d445e6f:backend/group_watch.py`（拆分前单体）
  * 改后 = 三件之和（group_watch.py C + group_watch_delivery.py L1 + group_watch_pending.py L3）
  * 断言「改后对应件的同名单元」与基线**逐字节相同**（AST 取 `lineno..end_lineno`，只比单元本体）
  * 断言**导出面守恒**：三件顶层公开名 ⊇ 改前顶层名（不丢名，将来潜在调用方与对账不受影响）

用法：
    PYTHONUTF8=1 python tools/equivalence/group_watch_equiv.py
        # 期望末行：逐字节一致 15/15 · 导出面守恒 OK(18 名) · rc=0
    PYTHONUTF8=1 python tools/equivalence/group_watch_equiv.py --negative
        # 负控：把新侧 1 字符改坏 → 装置**必须**判红；rc=1 = 负控通过（装置判红能力已证）
        #                                        rc=2 = 装置失效（注毒后仍判绿）
口径说明：方案 §3.1 记「13 个可证单元」（把 State 类声明与方法合并计数）；本装置取更细的 15 项
（4 顶层单元 + State 整类 + 10 个方法 + build_prompt），**判据更严不更松**，如实登记此计数差异。
"""
from __future__ import annotations

import argparse
import ast
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BASE_REV = "d445e6f"
BASE_PATH = "backend/group_watch.py"
NEW_FILES = ("node/c/group_watch.py",
             "node/l1_transport/group_watch_delivery.py",
             "hub/l3_domain/group_watch_pending.py")

TOP_UNITS = ("default_pass_file", "log", "http", "build_prompt")
CLASS_NAME = "State"
METHODS = ("__init__", "load", "save", "seen", "mark", "gap_ok", "touch",
           "chain_count", "bump_chain", "reset_chain")
# 改前顶层公开名（含常量）—— 改后三件并集必须全覆盖
EXPORT_NAMES = ("default_pass_file", "log", "http", "State", "build_prompt", "one_round", "main",
                "CHAT_BASE_DEFAULT", "NATS_HOST_DEFAULT", "STATE_DEFAULT", "PASS_CANDIDATES", "LOG_FILE",
                "AGENT_TEAM_CHAT_BASE", "AGENT_TEAM_NATS_HOST", "AGENT_TEAM_RUNTIME_DIR",
                "CTX_LIMIT", "MAX_PER_ROUND", "HUMAN_IDS")
NEGATIVE_OLD = 'lines = ["【临时群《%s》的群聊记录，所有人可见】" % (title or sid)]'
NEGATIVE_NEW = 'lines = ["【临时群《%s》的群聊记录，所有人才可见】" % (title or sid)]'


def base_src() -> str:
    out = subprocess.run(["git", "show", "%s:%s" % (BASE_REV, BASE_PATH)],
                         cwd=ROOT, capture_output=True, check=True)
    return out.stdout.decode("utf-8")


def read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def slice_unit(src: str, node) -> str:
    lines = src.splitlines()
    return "\n".join(lines[node.lineno - 1:node.end_lineno]).rstrip()


def top_nodes(src: str):
    t = ast.parse(src)
    out = {}
    for node in t.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            out[node.name] = node
        elif isinstance(node, ast.Assign):
            for tg in node.targets:
                if isinstance(tg, ast.Name):
                    out[tg.id] = node
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            out[node.target.id] = node
    return out


def method_nodes(src: str, cls: str):
    t = ast.parse(src)
    for node in t.body:
        if isinstance(node, ast.ClassDef) and node.name == cls:
            return {m.name: m for m in node.body if isinstance(m, ast.FunctionDef)}
    return {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--negative", action="store_true", help="负控：注 1 字符差异，装置必须判红")
    a = ap.parse_args()

    base = base_src()
    new_all = "".join(read(p) + "\n" for p in NEW_FILES)
    if a.negative:
        if NEGATIVE_OLD not in new_all:
            print("负控无法执行：注毒锚点未在改后件中找到（装置口径需更新）")
            return 2
        new_all = new_all.replace(NEGATIVE_OLD, NEGATIVE_NEW, 1)

    base_top, new_top = top_nodes(base), top_nodes(new_all)
    base_m, new_m = method_nodes(base, CLASS_NAME), method_nodes(new_all, CLASS_NAME)

    checks, bad = [], []
    for name in TOP_UNITS:
        checks.append(name)
        if name not in base_top or name not in new_top or slice_unit(base, base_top[name]) != slice_unit(new_all, new_top[name]):
            bad.append(name)
    checks.append(CLASS_NAME)
    if CLASS_NAME not in base_top or CLASS_NAME not in new_top or slice_unit(base, base_top[CLASS_NAME]) != slice_unit(new_all, new_top[CLASS_NAME]):
        bad.append(CLASS_NAME)
    for name in METHODS:
        checks.append("%s.%s" % (CLASS_NAME, name))
        if name not in base_m or name not in new_m or slice_unit(base, base_m[name]) != slice_unit(new_all, new_m[name]):
            bad.append("%s.%s" % (CLASS_NAME, name))

    missing = [n for n in EXPORT_NAMES if n not in new_top]

    if a.negative:
        if bad:
            print("负控 OK：注毒 1 字符后装置判红 → %s（装置判红能力已证）" % ", ".join(bad[:3]))
            return 1
        print("负控 FAIL：注毒后装置仍判绿 —— 装置失效")
        return 2

    print("基线 = git show %s:%s（%d 行）· 改后 = %d 件合计 %d 行"
          % (BASE_REV, BASE_PATH, len(base.splitlines()), len(NEW_FILES), len(new_all.splitlines())))
    if bad:
        print("逐字节不一致 %d/%d：%s" % (len(bad), len(checks), ", ".join(bad)))
    else:
        print("逐字节一致 %d/%d" % (len(checks), len(checks)))
    if missing:
        print("导出面守恒 FAIL：缺名 %s" % ", ".join(missing))
    else:
        print("导出面守恒 OK(%d 名)" % len(EXPORT_NAMES))
    rc = 0 if (not bad and not missing) else 1
    print("rc=%d" % rc)
    return rc


if __name__ == "__main__":
    sys.exit(main())
