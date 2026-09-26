#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""L0 平台层：**宿主默认值的唯一出处**（配置零硬编码）。

为什么需要它
------------
分层前每个模块各自写自己的宿主默认值，结果同一份代码里默认值互斥：20 处兜底
`<deploy-dir>`（Linux 惯例）、8 处兜底 `<home>\agent-team`（Windows 惯例）。在 Windows 上
有一半兜底指向不存在的路径 —— 这正是「不同 OS 与通用机制解耦」要消掉的东西。

规则（唯一一条）
----------------
**env 优先 > 平台自适应默认值。** 除本文件外，任何模块都不许再出现宿主路径字面量。

- env 覆盖：`AGENT_TEAM_RUNTIME_DIR` / `AGENT_TEAM_ETC_DIR` / `AGENT_TEAM_NODE_EXE` /
  `AGENT_TEAM_LOG_DIR`（阶段 4 的 L0 nodelet 接管注入后，本文件的默认值退居兜底）。
- 平台默认值 = **该平台的惯例位置**，不引另一平台的惯例：
  Windows 运行目录 `<home>\agent-team`；posix 运行目录 `<deploy-dir>`、配置目录 `<deploy-dir>`。
  于是「在 Linux 上跑」与「在 Windows 上跑」各自拿到正确值，同一份代码两边都不用改。

「本机真值」与「目标主机真值」是两回事
--------------------------------------
上面那组函数回答的全是「**本机**的路径长什么样」。跨主机调用（例如经 ssh 去另一台机上跑
执行器）要的是「**那台机**的路径长什么样」—— 两者拿混了就会静默出事：2026-09-25 实测，
lobster（Linux）把本机的 `/root/.local/.../claude_exec.py` 当成 ssh 目标 thinkbook（Windows）
的路径发过去，远端报 `can't open file`，模型通道整条静默失效。

所以本文件另设一组 `target_*` 函数（阶段 4 节点档案的**雏形**）：按「目标主机名」查档案，
返回那台机的 flavour / 家目录 / 执行器路径。查不到的主机**不编造** —— 按 posix 兜底，
同时 `target_known()` 返回 False，调用方据此记警告。

设计约束
--------
纯函数、**只读**（不建目录、不写状态、不读网络）；所有函数接受可选 `flavour`（`"nt"`/`"posix"`），
便于单测在两种平台语义下断言，不必真换机器。
"""

import json
import os

__all__ = ["runtime_dir", "etc_dirs", "etc_dir", "log_dir", "node_exe",
           "pi_cli_js", "pi_workdir", "claude_exec", "claude_workdir",
           "codex_exe", "codex_cmd", "nats_dir", "flavour",
           "target_known", "target_flavour", "target_home", "target_claude_exec"]

ENV_RUNTIME_DIR = "AGENT_TEAM_RUNTIME_DIR"
ENV_ETC_DIR = "AGENT_TEAM_ETC_DIR"
ENV_NODE_EXE = "AGENT_TEAM_NODE_EXE"
ENV_LOG_DIR = "AGENT_TEAM_LOG_DIR"

# 各平台的惯例位置（字面量只许出现在本文件内）
WIN_RUNTIME_DIR = r"<home>\agent-team"
POSIX_RUNTIME_DIR = "<deploy-dir>"
POSIX_ETC_DIR = "<deploy-dir>"
# PI/Codex 用的 node：刻意钉住 node22 那份（自动更新已关 + 哨兵），不走 PATH 里的 node。
WIN_NODE_EXE = r"<home>\node22\node-v22.23.2-win-x64\node.exe"
POSIX_NODE_EXE = "node"

ENV_CLAUDE_EXEC = "AGENT_TEAM_CLAUDE_EXEC"
ENV_CLAUDE_WORKDIR = "AGENT_TEAM_CLAUDE_WORKDIR"
ENV_CODEX_EXE = "AGENT_TEAM_CODEX_EXE"
ENV_CODEX_CMD = "AGENT_TEAM_CODEX_CMD"
ENV_NATS_DIR = "AGENT_TEAM_NATS_DIR"
ENV_PI_CLI_JS = "AGENT_TEAM_PI_CLI_JS"
ENV_PI_WORKDIR = "AGENT_TEAM_PI_WORKDIR"

# PI（lenserver 那套 CLI）目前只在 Windows 跑；posix 侧给的是**约定值**，未经实机验证，
# 换平台时请用 env 覆盖（AGENT_TEAM_PI_CLI_JS / AGENT_TEAM_PI_WORKDIR）。
WIN_PI_CLI_JS = r"<home>\node22\node-v22.23.2-win-x64\node_modules\@earendil-works\pi-coding-agent\dist\bundle\cli.js"
POSIX_PI_CLI_JS = "/opt/pi-coding-agent/dist/bundle/cli.js"
WIN_PI_WORKDIR = r"<home>\pi-workspace"
POSIX_PI_WORKDIR = "/opt/pi-workspace"


def flavour(value=None):
    """平台语义：`"nt"` / `"posix"`。显式传入优先，便于单测。"""
    if value:
        return str(value)
    return "nt" if os.name == "nt" else "posix"


def runtime_dir(value=None):
    """本节点运行目录（部署件落地的那个目录）。"""
    env = (os.environ.get(ENV_RUNTIME_DIR) or "").strip()
    if env:
        return env
    return WIN_RUNTIME_DIR if flavour(value) == "nt" else POSIX_RUNTIME_DIR


def etc_dirs(value=None):
    """配置目录**候选列表**（按优先级）。

    返回列表而非单值：调用方原本就有「逐个探测存在性」的语义（例如找一个可能存在的令牌文件），
    这里保留该语义，只是把候选来源集中到一处。
    """
    env = (os.environ.get(ENV_ETC_DIR) or "").strip()
    if env:
        return [env]
    if flavour(value) == "nt":
        # Windows 部署把配置直接放在运行目录里（如 <运行目录>\\client-token）
        return [runtime_dir(value)]
    return [POSIX_ETC_DIR]


def etc_dir(value=None):
    """配置目录（候选中的第一个）。需要探测多个位置时请用 `etc_dirs()`。"""
    return etc_dirs(value)[0]


def log_dir(value=None):
    """日志目录（默认 = 运行目录下的 logs/）。"""
    env = (os.environ.get(ENV_LOG_DIR) or "").strip()
    if env:
        return env
    return os.path.join(runtime_dir(value), "logs")


def node_exe(value=None):
    """PI/Codex 子进程用的 node 可执行文件。"""
    env = (os.environ.get(ENV_NODE_EXE) or "").strip()
    if env:
        return env
    return WIN_NODE_EXE if flavour(value) == "nt" else POSIX_NODE_EXE

def pi_cli_js(value=None):
    """PI CLI 的 bundle 入口（`cli.js`）。"""
    env = (os.environ.get(ENV_PI_CLI_JS) or "").strip()
    if env:
        return env
    return WIN_PI_CLI_JS if flavour(value) == "nt" else POSIX_PI_CLI_JS


def pi_workdir(value=None):
    """PI 的工作目录。"""
    env = (os.environ.get(ENV_PI_WORKDIR) or "").strip()
    if env:
        return env
    return WIN_PI_WORKDIR if flavour(value) == "nt" else POSIX_PI_WORKDIR


def _home_sub(*parts):
    """把「每用户工具」默认值写成 `~` 相对形式：同一份代码在两个平台各自落到正确位置。"""
    return os.path.join(os.path.expanduser("~"), *parts)


def claude_exec(value=None):
    """Claude Code 封装脚本（本机调用 Claude 的唯一入口）。"""
    env = (os.environ.get(ENV_CLAUDE_EXEC) or "").strip()
    if env:
        return env
    if flavour(value) == "nt":
        return _home_sub("AppData", "Local", "hermes", "scripts", "claude_exec.py")
    return _home_sub(".local", "share", "hermes", "scripts", "claude_exec.py")


def claude_workdir(value=None):
    """Claude Code 的工作目录。"""
    env = (os.environ.get(ENV_CLAUDE_WORKDIR) or "").strip()
    if env:
        return env
    return _home_sub("claude-workspace")


def codex_exe(value=None):
    """Codex CLI 可执行文件。"""
    env = (os.environ.get(ENV_CODEX_EXE) or "").strip()
    if env:
        return env
    exe = "codex.exe" if flavour(value) == "nt" else "codex"
    return _home_sub(".codex", "packages", "standalone", "current", "bin", exe)


def codex_cmd(value=None):
    """Codex CLI 的启动壳（Windows 为 .cmd，posix 为同名脚本）。"""
    env = (os.environ.get(ENV_CODEX_CMD) or "").strip()
    if env:
        return env
    return _home_sub(".local", "bin", "codex.cmd" if flavour(value) == "nt" else "codex")


def nats_dir(value=None):
    """NATS 服务端配置目录（posix 惯例 /etc/nats；Windows 侧落在运行目录下，env 可覆盖）。"""
    env = (os.environ.get(ENV_NATS_DIR) or "").strip()
    if env:
        return env
    return os.path.join(runtime_dir(value), "nats") if flavour(value) == "nt" else "/etc/nats"


# ============================================================ 目标主机档案（雏形）
# 阶段 4 会把这张表正式化成「节点档案」（与部署表、名册同源）；本刀只落最小可用的一版，
# 让「跨主机取路径」有唯一出处，不再靠调用方拿本机真值去猜远端。

ENV_TARGET_PROFILE_JSON = "AGENT_TEAM_TARGET_PROFILE_JSON"

#: 目标主机的家目录（跨主机真值，字面量只许出现在本文件内）
NT_TARGET_HOME = "<home>"
POSIX_TARGET_HOME = "/root"

#: 家目录 → claude 执行器的相对位置（与本机 `claude_exec()` 同一套惯例）
_TARGET_CLAUDE_SUB = {"nt": "AppData/Local/hermes/scripts/claude_exec.py",
                      "posix": ".local/share/hermes/scripts/claude_exec.py"}

#: 未登记主机的兜底 flavour：**不按本机推**（本机是谁与目标是谁无关），一律 posix，
#: 且 `target_known()` 会返回 False —— 拿到的是兜底值而非真值，调用方该记警告。
TARGET_FALLBACK_FLAVOUR = "posix"

#: 内置档案表。键 = 主机名（小写）；值 = {flavour, home, claude_exec}。
_TARGET_PROFILES = {
    "thinkbook": {"flavour": "nt",
                  "home": NT_TARGET_HOME,
                  "claude_exec": NT_TARGET_HOME + "/AppData/Local/hermes/scripts/claude_exec.py"},
    "lobster": {"flavour": "posix",
                "home": POSIX_TARGET_HOME,
                "claude_exec": POSIX_TARGET_HOME + "/.local/share/hermes/scripts/claude_exec.py"},
    "quantum": {"flavour": "posix",
                "home": POSIX_TARGET_HOME,
                "claude_exec": POSIX_TARGET_HOME + "/.local/share/hermes/scripts/claude_exec.py"},
    "top001": {"flavour": "posix",
               "home": POSIX_TARGET_HOME,
               "claude_exec": POSIX_TARGET_HOME + "/.local/share/hermes/scripts/claude_exec.py"},
}


def _target_host(host_value):
    """`user@host` / `host` → 主机名（小写）。只取 host 部分，用户名不参与查表。"""
    text = str(host_value or "").strip()
    if "@" in text:
        text = text.rsplit("@", 1)[1]
    return text.strip().lower()


def _target_table():
    """档案表（`AGENT_TEAM_TARGET_PROFILE_JSON` **整表覆盖**；解析不了就用内置表）。"""
    env = (os.environ.get(ENV_TARGET_PROFILE_JSON) or "").strip()
    if env:
        try:
            data = json.loads(env)
        except Exception:
            return dict(_TARGET_PROFILES)   # 覆盖值坏了就退回内置表，绝不抛给调用方
        if isinstance(data, dict):
            return {_target_host(k): v for k, v in data.items() if isinstance(v, dict)}
    return dict(_TARGET_PROFILES)


def _target_profile(host_value):
    """→ (档案 dict, 是否登记在案)。档案里缺的键按该 flavour 的惯例补齐。"""
    entry = _target_table().get(_target_host(host_value))
    known = isinstance(entry, dict)
    entry = entry if known else {}
    fl = str(entry.get("flavour") or "").strip().lower()
    if fl not in ("nt", "posix"):
        fl = TARGET_FALLBACK_FLAVOUR
    home = str(entry.get("home") or "").strip() or (
        NT_TARGET_HOME if fl == "nt" else POSIX_TARGET_HOME)
    exe = str(entry.get("claude_exec") or "").strip() or (
        home.rstrip("/\\") + "/" + _TARGET_CLAUDE_SUB[fl])
    return {"flavour": fl, "home": home, "claude_exec": exe}, known


def target_known(host_value):
    """目标主机是否登记在档案表里。False = 下面几个函数给的是**兜底值不是真值**。"""
    return _target_profile(host_value)[1]


def target_flavour(host_value):
    """目标主机的平台语义：`"nt"` / `"posix"`（未登记按 posix 兜底）。"""
    return _target_profile(host_value)[0]["flavour"]


def target_home(host_value):
    """目标主机上的家目录。"""
    return _target_profile(host_value)[0]["home"]


def target_claude_exec(host_value):
    """**目标主机上**的 Claude Code 封装脚本路径（跨主机调用取这个，别取 `claude_exec()`）。"""
    return _target_profile(host_value)[0]["claude_exec"]
