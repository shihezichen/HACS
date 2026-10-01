#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L2 适配层：**模型执行器适配** + **Agent 展示名**（Agent 差异的唯一落脚点）。

职责边界（只此两件，别往里加业务）
--------------------------------
① 模型富化执行器 —— 回答「**怎么调**模型」：
     ssh 二进制 / 反向目标 / 私钥与 known_hosts / 远端命令行 / 编码口径 /
     超时与限速默认值 / `subprocess` 调用 / 输出解析。
   **不回答「调什么」**：提示词模板、队列 worker、限速重投、写库、台账 —— 那是 L3
   协作域（chat_svc.py）的事，原样留在那边。

② Agent 展示名 —— 兜底表 + `AGENT_TEAM_DISPLAY_NAMES` 覆盖 + `display_name()` 语义。
   「同一个成员 id 在人眼前叫什么」是 Agent 差异，不是群聊业务。

契约（签名稳定；chat_svc 侧只留薄委托，调用点零改动）
---------------------------------------------------
    enabled()            -> bool                 富化总开关
    timeout_s()          -> float                单次调用超时（夹在 5..1800）
    min_interval_s()     -> float                两次调用的最小间隔（>=0）
    ssh_argv()           -> List[str]            完整命令行（选项顺序是契约的一部分）
    parse(raw)           -> Optional[Dict]       模型输出 → 校验过的判定；不合格返回 None
    call(prompt, timeout=None) -> (Optional[Dict], str)
                                                 (判定, 说明)；**失败一律降级**
    probe(timeout_s=60)  -> Dict                 真打一次最小 prompt 的自检（不写库/不改状态）
    display_name(member_id) -> str               补充表 > id 原样

**失败是正常路径**：超时 / ssh 不通 / 执行器不存在 / 输出不是 JSON —— 一律返回
`(None, 原因)`，绝不抛异常给上层、绝不编造判定。

远端执行器路径取**目标主机**的真值（2026-09-25 刀 4 修的就是这一处）
------------------------------------------------------------------
`REMOTE_CMD` 里的 `claude_exec.py` 跑在 **ssh 目标机**上，不是本机，所以路径必须按目标主机
取：`node_paths.target_claude_exec(目标)`。改之前取的是 `node_paths.claude_exec()`（**本机**
平台路径）—— chat_svc 从 thinkbook 迁到 lobster 后，Linux 本机路径被发给 Windows 目标机，
远端 `can't open file`，整条模型通道静默失效（enrich worker 活着但 submitted/done 恒为 0）。
目标主机未登记在档案表里时仍会给出兜底路径（posix 惯例），`probe()` 会把这件事写进 `why`。

零硬编码
--------
宿主路径一律经 L0 `node_paths`（env 优先）取；本件不写任何盘符/绝对目录字面量。
整条链路可被 8 个环境变量逐段覆盖（排障与降级实测就靠它们）：
    CHAT_ENRICH_ENABLE=0        → 关掉富化（调用方据此不起 worker）
    CHAT_ENRICH_SSH_BIN=...     → 换 ssh 二进制
    CHAT_ENRICH_SSH=user@host   → 换反向 SSH 目标
    CHAT_ENRICH_KEY=...         → 换私钥
    CHAT_ENRICH_KNOWN_HOSTS=... → 换 known_hosts
    CHAT_ENRICH_CMD=...         → 换远端执行器命令行（**指向错路径 = 模型不可用降级实测**）
    CHAT_ENRICH_TIMEOUT_S=...   → 换超时
    CHAT_ENRICH_MIN_INTERVAL_S  → 换限速间隔（默认 1s）
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

# 批① 起被搬件按绝对包名 import，故把仓根一并放进搜索路径（本目录仍在最前）。
for _hacs_p in (os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                os.path.dirname(os.path.abspath(__file__))):
    sys.path.insert(0, _hacs_p)

from node.l0_host import node_paths as _np  # noqa: E402  L0 平台层：宿主默认值唯一出处（env 优先）
from hub.x import view_auth  # X 横切：错误文本脱敏（safe_err）同一套口径

__all__ = ["enabled", "timeout_s", "min_interval_s", "ssh_argv", "parse", "call",
           "probe", "display_name", "DISPLAY_NAMES", "DISPLAY_NAMES_FALLBACK"]

# ------------------------------------------------------------ 宿主默认值（env 优先）

# 兜底默认值集中到 L0 node_paths（env 优先）
AGENT_TEAM_ETC_DIR = os.environ.get("AGENT_TEAM_ETC_DIR") or _np.etc_dir()
# 兜底默认值集中到 L0 node_paths（env 优先）
AGENT_TEAM_SSH_TARGET = os.environ.get("AGENT_TEAM_SSH_TARGET") or ""
#: ⚠️ **目标主机**上的执行器路径（不是本机的）—— 见上文「远端执行器路径取目标主机真值」。
AGENT_TEAM_CLAUDE_EXEC = _np.target_claude_exec(AGENT_TEAM_SSH_TARGET)

# ---------------------------------------------------- 模型执行器：环境变量名与默认值

#: 模型通道 = 目标机上**反向 SSH 回本机**跑 claude_exec.py（原生 claude.exe + 本机代理，
#: 包月额度；本机直连 api.anthropic.com 必 403，别试）。
ENABLE_ENV = "CHAT_ENRICH_ENABLE"
SSH_BIN_ENV = "CHAT_ENRICH_SSH_BIN"
SSH_ENV = "CHAT_ENRICH_SSH"
KEY_ENV = "CHAT_ENRICH_KEY"
KNOWN_ENV = "CHAT_ENRICH_KNOWN_HOSTS"
CMD_ENV = "CHAT_ENRICH_CMD"
TIMEOUT_ENV = "CHAT_ENRICH_TIMEOUT_S"
MIN_INTERVAL_ENV = "CHAT_ENRICH_MIN_INTERVAL_S"

SSH_BIN = "/usr/bin/ssh"
SSH_TARGET = AGENT_TEAM_SSH_TARGET
#: 私钥/known_hosts 放**配置目录下**而不是 /root/.ssh：公网那个实例带 ProtectHome=yes，
#: 家目录被隐藏，默认私钥路径在沙箱里读不到（实测过），显式 -i 才是两个实例都能跑的写法。
KEY_FILE = AGENT_TEAM_ETC_DIR + "/ssh/id_ed25519"
KNOWN_HOSTS = AGENT_TEAM_ETC_DIR + "/ssh/known_hosts"


def _remote_cmd(target: str) -> str:
    """目标机上要跑的那条命令（执行器路径按**目标主机档案**取，不取本机路径）。"""
    return ("python -X utf8 %s" % _np.target_claude_exec(target)) + " --timeout 150 -"


REMOTE_CMD = _remote_cmd(AGENT_TEAM_SSH_TARGET)
TIMEOUT_S = 240.0
MIN_INTERVAL_S = 1.0
NAME_MAX = 18
SUMMARY_MAX = 80
#: ⚠️ 远端执行器必须用 `python -X utf8` 起 —— 本机 Windows 上 python 的 stdin/stdout
#: 被 ssh 接到管道时按 cp936 走，中文提示词与中文回答会在两端各烂一次（实测踩到：
#: task_name 落库成「锟斤拷」）。UTF-8 模式一次性把 stdin/stdout 都钉成 utf-8。


def _scrubbed(exc: Any) -> str:
    """错误文本脱敏：口令类字段一律抹掉，再截断（与 chat_svc 同一实现）。"""
    return view_auth.safe_err(exc)


def enabled() -> bool:
    """富化总开关（`CHAT_ENRICH_ENABLE=0/false/no/off` 关）。"""
    raw = (os.environ.get(ENABLE_ENV) or "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def timeout_s() -> float:
    try:
        value = float((os.environ.get(TIMEOUT_ENV) or "").strip() or TIMEOUT_S)
    except (TypeError, ValueError):
        value = TIMEOUT_S
    return max(5.0, min(1800.0, value))


def min_interval_s() -> float:
    try:
        value = float((os.environ.get(MIN_INTERVAL_ENV) or "").strip()
                      or MIN_INTERVAL_S)
    except (TypeError, ValueError):
        value = MIN_INTERVAL_S
    return max(0.0, value)


def _target() -> str:
    """本次调用的 ssh 目标（`CHAT_ENRICH_SSH` > `AGENT_TEAM_SSH_TARGET` > 回退值）。"""
    return (os.environ.get(SSH_ENV) or "").strip() or SSH_TARGET


def ssh_argv() -> List[str]:
    """模型调用的完整命令行（ssh → 目标机上的 claude_exec.py），提示词走 stdin。

    选项顺序是契约的一部分（排障时人要照着敲同一条命令），别重排。
    远端命令的默认值按**当次生效的目标主机**取 —— `CHAT_ENRICH_SSH` 换了目标，路径跟着换，
    否则换目标就又踩回「拿 A 机的路径去 B 机上跑」那个坑。`CHAT_ENRICH_CMD` 仍是最外层覆盖。
    """
    ssh_bin = (os.environ.get(SSH_BIN_ENV) or "").strip() or SSH_BIN
    target = _target()
    remote = (os.environ.get(CMD_ENV) or "").strip() or _remote_cmd(target)
    key = (os.environ.get(KEY_ENV) or "").strip()
    if not key:
        key = KEY_FILE
    known = (os.environ.get(KNOWN_ENV) or "").strip()
    if not known:
        known = KNOWN_HOSTS
    argv = [ssh_bin, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
    if key:
        argv += ["-i", key, "-o", "IdentitiesOnly=yes"]
    if known:
        argv += ["-o", "UserKnownHostsFile=%s" % known]
    argv += [target, remote]
    return argv


def parse(raw: Any) -> Optional[Dict[str, Any]]:
    """模型输出 → 校验过的判定。**不合格一律返回 None**（调用方据此重试 / 降级）。"""
    text = str(raw or "").strip()
    if not text:
        return None
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except Exception:
        return None
    if not isinstance(data, dict) or "is_task" not in data:
        return None
    flag = data.get("is_task")
    if isinstance(flag, str):
        flag = flag.strip().lower() in ("true", "1", "yes", "y", "是")
    flag = bool(flag)
    name = re.sub(r"\s+", " ", str(data.get("task_name") or "")).strip()
    name = name.strip("，,。.、:：;；\"'“”（）()【】[]")
    summary = re.sub(r"\s+", " ", str(data.get("summary") or "")).strip()
    if flag and not name:
        return None                    # 判是任务却不给名字：不算合格输出
    try:
        conf = float(data.get("confidence"))
    except (TypeError, ValueError):
        conf = 0.0
    return {"is_task": flag,
            "task_name": name[:NAME_MAX],
            "summary": summary[:SUMMARY_MAX],
            "confidence": max(0.0, min(1.0, conf)),
            "raw": text[:600]}


def call(prompt: str, timeout: Optional[float] = None) -> Tuple[Optional[Dict[str, Any]], str]:
    """把提示词交给模型，返回 (判定, 说明)。判定 None = 这次失败（原因写在说明里）。

    **失败是正常路径**：超时 / ssh 不通 / 执行器不存在 / 输出不是 JSON —— 全部只返回
    None，由调用方重试或降级成规则名。这里绝不抛异常给上层。

    `timeout` 省略时取 `timeout_s()`（即原 chat_svc 的口径）。
    """
    if not enabled():
        return None, "disabled"
    argv = ssh_argv()
    timeout = timeout_s() if timeout is None else timeout
    started = time.time()
    try:
        proc = subprocess.run(argv, input=prompt.encode("utf-8"),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, "timeout_%ds" % int(timeout)
    except FileNotFoundError:
        return None, "ssh_missing:%s" % argv[0]
    except Exception as exc:
        return None, "spawn_error:%s" % _scrubbed(exc)
    out = (proc.stdout or b"").decode("utf-8", "replace").strip()
    err = (proc.stderr or b"").decode("utf-8", "replace").strip()
    if proc.returncode != 0:
        return None, "exit_%d:%s" % (proc.returncode, (err or out)[-200:])
    verdict = parse(out)
    if verdict is None:
        return None, "unparsable:%s" % out[-200:]
    verdict["elapsed_s"] = round(time.time() - started, 2)
    return verdict, "ok"


# -------------------------------------------------------------------- 通道自检

#: 自检用的最小提示词：回答内容固定、不含业务语义，只用来证明「这条通道能通且能回 JSON」。
PROBE_PROMPT = ('只回一行 JSON，不要任何解释、不要代码块：'
                '{"is_task": false, "task_name": "", "summary": "", "confidence": 0.9}')


def probe(timeout_s: float = 60) -> Dict[str, Any]:
    """真打一次最小 prompt，报告这条模型通道通不通。**不写库、不改状态、不抛异常。**

    返回 `{"ok", "target", "argv", "elapsed_s", "why", "parsed"}`：
      · `ok`      —— 打通且输出能解析成判定；
      · `argv`    —— 这次实际用的命令行（排障时照着敲同一条）；
      · `why`     —— `call()` 的说明原样（`ok` / `timeout_..` / `exit_..` / `unparsable:..` /
                     `disabled`）；目标主机没登记在档案表里时额外挂一句 `target_unknown`，
                     提醒远端路径是兜底值而不是真值；
      · `parsed`  —— 解析出的判定，失败时 None。
    """
    target = _target()
    argv = ssh_argv()
    started = time.time()
    verdict, why = call(PROBE_PROMPT, timeout=timeout_s)
    if not _np.target_known(target):
        why = "%s|target_unknown:%s" % (why, target)
    return {"ok": verdict is not None,
            "target": target,
            "argv": argv,
            "elapsed_s": round(time.time() - started, 2),
            "why": why,
            "parsed": verdict}


# ---------------------------------------------------------------- Agent 展示名

#: 成员显示名补充表（agents.json 里只有 description 时用；键 = 成员 id）
# 兜底默认值集中到 L0 node_paths（env 优先）（键=成员 id，值=显示名）
_DISPLAY_NAMES_FALLBACK = {
    "lobster": "龙虾",
    "quantum": "量子",
    "top001": "Top001",
    "openclaw": "OpenClaw",
    "openclaw-nb": "笔记本 OpenClaw",
    "claudecode": "Claude Code",
    "codex": "Codex",
    "piagent": "Pi Agent",
    "hermes": "Hermes",
}
DISPLAY_NAMES = dict(_DISPLAY_NAMES_FALLBACK)
#: 公开面：兜底表的别名（**同一个对象**，不是拷贝）—— 上层件要引用就引用它，别再跨件摸 `_` 名字。
DISPLAY_NAMES_FALLBACK = _DISPLAY_NAMES_FALLBACK
_dn_override = os.environ.get("AGENT_TEAM_DISPLAY_NAMES")
if _dn_override:
    DISPLAY_NAMES.update(json.loads(_dn_override))


def display_name(member_id: str) -> str:
    """成员显示名：补充表 > 名册里自带的 display > id 原样。"""
    if not member_id:
        return ""
    return DISPLAY_NAMES.get(member_id) or member_id
