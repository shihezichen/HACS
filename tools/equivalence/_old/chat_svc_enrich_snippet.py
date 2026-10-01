from __future__ import annotations
import json
import os
import re
import subprocess
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from node.l0_host import node_paths as _np
from hub.x import view_auth


AGENT_TEAM_ETC_DIR = os.environ.get("AGENT_TEAM_ETC_DIR") or _np.etc_dir()

AGENT_TEAM_SSH_TARGET = os.environ.get("AGENT_TEAM_SSH_TARGET") or ""

AGENT_TEAM_CLAUDE_EXEC = _np.claude_exec()

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
    "<human-id>": "刚哥",
}

DISPLAY_NAMES = dict(_DISPLAY_NAMES_FALLBACK)

_dn_override = os.environ.get("AGENT_TEAM_DISPLAY_NAMES")

if _dn_override:
    DISPLAY_NAMES.update(json.loads(_dn_override))

def scrubbed(exc: Any) -> str:
    """错误文本脱敏：口令类字段一律抹掉，再截断。"""
    return view_auth.safe_err(exc)

def display_of(member_id: str) -> str:
    """成员显示名：补充表 > 名册里自带的 display > id 原样。"""
    if not member_id:
        return ""
    return DISPLAY_NAMES.get(member_id) or member_id

ENRICH_ENABLE_ENV = "CHAT_ENRICH_ENABLE"

ENRICH_SSH_BIN_ENV = "CHAT_ENRICH_SSH_BIN"

ENRICH_SSH_ENV = "CHAT_ENRICH_SSH"

ENRICH_KEY_ENV = "CHAT_ENRICH_KEY"

ENRICH_KNOWN_ENV = "CHAT_ENRICH_KNOWN_HOSTS"

ENRICH_CMD_ENV = "CHAT_ENRICH_CMD"

ENRICH_TIMEOUT_ENV = "CHAT_ENRICH_TIMEOUT_S"

ENRICH_MIN_INTERVAL_ENV = "CHAT_ENRICH_MIN_INTERVAL_S"

ENRICH_SSH_BIN = "/usr/bin/ssh"

ENRICH_SSH_TARGET = AGENT_TEAM_SSH_TARGET

ENRICH_KEY_FILE = AGENT_TEAM_ETC_DIR + "/ssh/id_ed25519"

ENRICH_KNOWN_HOSTS = AGENT_TEAM_ETC_DIR + "/ssh/known_hosts"

ENRICH_REMOTE_CMD = ("python -X utf8 %s" % AGENT_TEAM_CLAUDE_EXEC) + " --timeout 150 -"

ENRICH_TIMEOUT_S = 240.0

ENRICH_MIN_INTERVAL_S = 1.0

ENRICH_NAME_MAX = 18

ENRICH_SUMMARY_MAX = 80

def todo_enrich_enabled() -> bool:
    """富化总开关（`CHAT_ENRICH_ENABLE=0/false/no/off` 关）。"""
    raw = (os.environ.get(ENRICH_ENABLE_ENV) or "1").strip().lower()
    return raw not in ("0", "false", "no", "off")

def todo_enrich_timeout() -> float:
    try:
        value = float((os.environ.get(ENRICH_TIMEOUT_ENV) or "").strip() or ENRICH_TIMEOUT_S)
    except (TypeError, ValueError):
        value = ENRICH_TIMEOUT_S
    return max(5.0, min(1800.0, value))

def todo_enrich_min_interval() -> float:
    try:
        value = float((os.environ.get(ENRICH_MIN_INTERVAL_ENV) or "").strip()
                      or ENRICH_MIN_INTERVAL_S)
    except (TypeError, ValueError):
        value = ENRICH_MIN_INTERVAL_S
    return max(0.0, value)

def todo_enrich_argv() -> List[str]:
    """模型调用的完整命令行（ssh → 本机 claude_exec.py），提示词走 stdin。"""
    ssh_bin = (os.environ.get(ENRICH_SSH_BIN_ENV) or "").strip() or ENRICH_SSH_BIN
    target = (os.environ.get(ENRICH_SSH_ENV) or "").strip() or ENRICH_SSH_TARGET
    remote = (os.environ.get(ENRICH_CMD_ENV) or "").strip() or ENRICH_REMOTE_CMD
    key = (os.environ.get(ENRICH_KEY_ENV) or "").strip()
    if not key:
        key = ENRICH_KEY_FILE
    known = (os.environ.get(ENRICH_KNOWN_ENV) or "").strip()
    if not known:
        known = ENRICH_KNOWN_HOSTS
    argv = [ssh_bin, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
    if key:
        argv += ["-i", key, "-o", "IdentitiesOnly=yes"]
    if known:
        argv += ["-o", "UserKnownHostsFile=%s" % known]
    argv += [target, remote]
    return argv

def todo_enrich_parse(raw: Any) -> Optional[Dict[str, Any]]:
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
            "task_name": name[:ENRICH_NAME_MAX],
            "summary": summary[:ENRICH_SUMMARY_MAX],
            "confidence": max(0.0, min(1.0, conf)),
            "raw": text[:600]}

def todo_enrich_call_model(prompt: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """把提示词交给模型，返回 (判定, 说明)。判定 None = 这次失败（原因写在说明里）。

    **失败是正常路径**：超时 / ssh 不通 / 执行器不存在 / 输出不是 JSON —— 全部只返回
    None，由调用方重试或降级成规则名。这里绝不抛异常给上层。
    """
    if not todo_enrich_enabled():
        return None, "disabled"
    argv = todo_enrich_argv()
    timeout = todo_enrich_timeout()
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
        return None, "spawn_error:%s" % scrubbed(exc)
    out = (proc.stdout or b"").decode("utf-8", "replace").strip()
    err = (proc.stderr or b"").decode("utf-8", "replace").strip()
    if proc.returncode != 0:
        return None, "exit_%d:%s" % (proc.returncode, (err or out)[-200:])
    verdict = todo_enrich_parse(out)
    if verdict is None:
        return None, "unparsable:%s" % out[-200:]
    verdict["elapsed_s"] = round(time.time() - started, 2)
    return verdict, "ok"
