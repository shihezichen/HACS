#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""_boot.py —— Windows 运行面的环境注入壳（部署件）

为什么要它
----------
本仓整改成分层布局后，各件的 `node_paths.py` 用的是**占位符默认值**
（`<deploy-dir>` / `<home>`），语义是「必须显式配置」；未配置时会退到临时目录，
数据面与日志面会静默错位（写到 `%TEMP%` 而不是运行目录）。因此在运行面上，
计划任务必须经本壳启动，由壳把**运行面真值**注入 `os.environ`。

计划任务实参形态（业务参数照搬原任务，不得改动）::

    <pythonw.exe> "<RUN_DIR>\\app\\_boot.py" "<RUN_DIR>\\app\\node\\...\\xxx.py" <原业务参数...>

本壳只做两件事
--------------
1. 注入运行面真值：`AGENT_TEAM_RUNTIME_DIR` / `AGENT_TEAM_LOG_DIR`
   （运行目录与日志目录 = 壳所在目录的**上一级**）
   以及三个执行器真值 `AGENT_TEAM_NODE_EXE` / `AGENT_TEAM_PI_CLI_JS` /
   `AGENT_TEAM_PI_WORKDIR`（把常量模板里的 `<home>` 展开成真实家目录）。
2. 以 `__main__` 身份**原地**执行目标脚本（`runpy`）—— 不另起子进程，
   计划任务里那个进程就是服务本身，崩溃现场必留痕（异常追加到 `_boot_error.log`）。

设计约束
--------
不解析、不干涉目标脚本的业务参数（`sys.argv` 原样转交）；只注入 env，不改变行为。
所有注入都用 `setdefault`：环境里已显式给出的值**优先**，本壳不覆盖。
"""
import os
import runpy
import sys
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))            # <RUN_DIR>\app
_RUN_DIR = os.path.dirname(_HERE)                             # <RUN_DIR>

# 运行面真值：运行目录、日志目录都在 RUN_DIR（不是占位符，也不是临时目录）
os.environ.setdefault("AGENT_TEAM_RUNTIME_DIR", _RUN_DIR)
os.environ.setdefault("AGENT_TEAM_LOG_DIR", _RUN_DIR)

# 执行器真值：常量模板里是 `<home>` 占位符，部署面按真实家目录展开
_HOME = os.path.expanduser("~")
_NODE_DIR = os.path.join(_HOME, "node22", "node-v22.23.2-win-x64")
os.environ.setdefault("AGENT_TEAM_NODE_EXE", os.path.join(_NODE_DIR, "node.exe"))
os.environ.setdefault("AGENT_TEAM_PI_CLI_JS", os.path.join(
    _NODE_DIR, "node_modules", "@earendil-works", "pi-coding-agent",
    "dist", "bundle", "cli.js"))
os.environ.setdefault("AGENT_TEAM_PI_WORKDIR", os.path.join(_HOME, "pi-workspace"))


def _log_boom(text):
    try:
        with open(os.path.join(_HERE, "_boot_error.log"), "a", encoding="utf-8") as fh:
            fh.write(text)
    except Exception:
        pass


def main():
    if len(sys.argv) < 2:
        sys.stderr.write("用法: _boot.py <目标脚本> [参数...]\n")
        return 2
    target = sys.argv[1]
    if not os.path.isabs(target):
        target = os.path.join(_HERE, target)
    target = os.path.normpath(target)
    if not os.path.isfile(target):
        _log_boom("目标脚本不存在: %s\n" % target)
        sys.stderr.write("目标脚本不存在: %s\n" % target)
        return 2
    # 让目标脚本按「仓内独立脚本」跑：app 根在搜索路径最前，跨层件按绝对包名 import
    if _HERE not in sys.path:
        sys.path.insert(0, _HERE)
    sys.argv = [target] + list(sys.argv[2:])
    try:
        runpy.run_path(target, run_name="__main__")
    except SystemExit:
        raise
    except BaseException:
        _log_boom("目标 %s 启动/运行失败:\n%s\n" % (target, traceback.format_exc()))
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
