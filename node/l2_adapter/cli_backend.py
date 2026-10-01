#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cli_backend.py —— 本机「CLI 成员后端」（给 a2a_svc.py 当 backend 用）

背景：a2a_svc.py 的 call_backend() 契约是固定的（见其第 91-146 行）：
    POST {backend}/chat/completions
    Authorization: Bearer <api_key>
    {"model": "...", "messages": [{"role":"user","content":"..."}], "max_tokens": 2000}
    期望返回 {"choices":[{"message":{"content":"..."}}]}，取不到就 502。
本服务把这套 OpenAI 形状的 HTTP 调用，翻译成本机三个**只有命令行接口**的 Agent：

  * piagent   —— @earendil-works/pi-coding-agent，用 node 直接跑包入口 cli.js，
                 `--print -- <任务>`，工作目录 <WORKDIR>。
                 绕开 pi.cmd/pi.ps1 垫片 → 不踩 shell 转义坑（参考 pi-feishu-bridge/src/pi.js）。
  * claudecode —— **必须**走已有封装 claude_exec.py（它负责 HTTPS_PROXY=127.0.0.1:4780
                 + 清掉继承来的 ANTHROPIC_*；直连 api.anthropic.com 必 403）。
                 任务文本用 --file 传临时文件，避免超长命令行与引号转义。
  * codex     —— Codex CLI 0.155 的 `codex exec -- <任务>`，工作目录 <WORKDIR>。
                 它原本被配成 http://127.0.0.1:18789/v1 + model=openclaw/codex，但该网关只有
                 openclaw/default，实测 `Unknown agent 'codex'.`（HTTP 400）→ codex 根本没有
                 HTTP 接口，只能和上面两位一样本地 spawn CLI。
                 config.toml 已是 sandbox=danger-full-access / approval=never，无审批交互。

会话连续性（实测 CLI --help 得到的真实参数，非臆造）：
  * pi   : `--session-id <id>`（帮助原文「Use exact project session ID, creating it if
           missing」）—— 首轮建、后续续，同一 cwd 下同 id 即同会话，**同一个参数用到底**。
           请求体没带 "user" 时用 `--no-session`（ephemeral），不污染任何已有会话。
  * claude: `--session-id <uuid>` 只能**创建**（要求是合法 UUID），续跑要用 `--resume <id>`。
           所以本服务用 uuid5 把会话键映射成固定 UUID：本进程第一次见到 → --session-id，
           之后 → --resume；若 --session-id 撞上「已存在」类报错，自动改用 --resume 重试一次。
           这两个参数由 claude_exec.py 的 parse_known_args 原样透传给 claude.exe。
  * codex : 没有「指定 id 建会话」的参数（`codex exec resume <SESSION_ID>` 只能续已存在的），
           所以反过来做：首轮跑完从 banner 里抓 `session id: <uuid>` 记在内存，后续同一会话键
           走 `codex exec resume <uuid>`。请求体没带 "user" 时加 `--ephemeral`（不落盘）。

其余要点：
  * 纯标准库，语法兼容 Python 3.9（不用 X|Y 注解 / match）。
  * 只绑 127.0.0.1（loopback），不绑 0.0.0.0。
  * 鉴权：Authorization: Bearer <key>，key 从文件读，缺文件/空文件 → **拒绝启动**（fail closed）。
  * 每个成员同一时刻只跑 1 个任务（各自一把锁，成员之间互不阻塞）；排队等不到 → 503 busy。
  * 超时 → taskkill /T /F 杀进程树 → 504 backend_timeout。
  * CLI 非零退出或输出为空 → 502，detail 带 stderr 尾部（截断 DETAIL_MAX）；**绝不编造回复**。
  * 请求体的 max_tokens 被忽略（CLI 没有这个旋钮），仅为兼容 a2a_svc 的固定请求体。

用法：
    python cli_backend.py                          # 127.0.0.1:9970
    python cli_backend.py --host 127.0.0.1 --port 9970 --key-file <PASS_FILE>
    python cli_backend.py --stub                   # 自测用：三个成员都换成内置回声桩
"""

import argparse
import hmac
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import uuid
from http.server import ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple

# 批① 起被搬件按绝对包名 import，故把仓根一并放进搜索路径（本目录仍在最前）。
for _hacs_p in (os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                os.path.dirname(os.path.abspath(__file__))):
    sys.path.insert(0, _hacs_p)

from node.l0_host import node_paths as _np  # noqa: E402  L0 平台层：宿主默认值唯一出处（env 优先）
from node.l0_host import node_profile as _npf  # noqa: E402  L0 节点档案：宿主事实唯一真源（codex 工作目录；env 优先）
from node.l1_transport import web_gateway  # L1 通信层：HTTP 入口（传输职责外置）

VERSION = "1.0"
DEFAULT_TIMEOUT_S = 300.0
MAX_TIMEOUT_S = 1800.0
BUSY_WAIT_S = 2.0          # 同成员排队最长等待，等不到就 503
DETAIL_MAX = 300           # 错误 detail 截断长度
MAX_BODY_BYTES = 262144    # 请求体上限 256 KiB
TASK_LOG_MAX = 40          # 日志里只记任务前 40 字，不记全文

# 兜底默认值集中到 L0 node_paths（env 优先）
AGENT_TEAM_RUNTIME_DIR = os.environ.get("AGENT_TEAM_RUNTIME_DIR") or _np.runtime_dir()

DEFAULT_KEY_FILE = os.environ.get("AGENT_TEAM_PASS_FILE") or AGENT_TEAM_RUNTIME_DIR + r"\cli-backend-key"
DEFAULT_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "logs", "cli_backend.log")

# 兜底默认值集中到 L0 node_paths（env 优先）（本机 node22 运行时路径）
PI_NODE = os.environ.get("AGENT_TEAM_NODE_EXE") or _np.node_exe()
# 兜底默认值集中到 L0 node_paths（env 优先）
PI_CLI_JS = _np.pi_cli_js()
# 兜底默认值集中到 L0 node_paths（env 优先）
PI_WORKDIR = os.environ.get("AGENT_TEAM_PI_WORKDIR") or _np.pi_workdir()
# 兜底默认值集中到 L0 node_paths（env 优先）
CLAUDE_EXEC = _np.claude_exec()
# 兜底默认值集中到 L0 node_paths（env 优先）
CLAUDE_WORKDIR = _np.claude_workdir()

# codex：原生 exe 优先（不需要 shell），退回 PATH 上的 codex.cmd（配套 codex_launch.py 垫片，
# 它 os.execv 到同一个原生 exe）。CODEX_CLI 环境变量可整体顶掉这两个。
# 兜底默认值集中到 L0 node_paths（env 优先）
CODEX_EXE = _np.codex_exe()
# 兜底默认值集中到 L0 node_paths（env 优先）
CODEX_CMD = _np.codex_cmd()
CODEX_WORKDIR = os.environ.get("AGENT_TEAM_CODEX_WORKDIR") or os.environ.get("CODEX_WORKDIR") or _npf.codex_workdir_default()

# codex 拒绝在「非 git 仓库 / 未受信任目录」里跑时的报错特征 → 自动加 --skip-git-repo-check 重试
CODEX_UNTRUSTED_RE = re.compile(
    r"skip[-_ ]git[-_ ]repo[-_ ]check|trusted director|git repo(?:sitory)?", re.I)
# banner 里的 `session id: <uuid>`，用来把 codex 自己生成的会话 id 记下来给 resume 用
CODEX_SESSION_RE = re.compile(r"session id:\s*([0-9a-fA-F]{8}-[0-9a-fA-F-]{27})")

# 内置回声桩：--stub 模式下替代真实 CLI，读 argv 末位当任务，原样回一句
STUB_CODE = (
    "import sys;"
    "sys.stdout.reconfigure(encoding='utf-8');"
    "print('[stub] ' + (sys.argv[-1] if len(sys.argv) > 1 else ''))"
)

# 成员表：member -> {模型前缀, 说明}
MEMBERS = ("piagent", "claudecode", "codex")
MODEL_IDS = ("pi/default", "claude/default", "codex/default")

LOG = logging.getLogger("cli_backend")

# ANSI 转义序列（CSI / OSC / 单字符 ESC 序列）
ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"          # CSI
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC
    r"|\x1b[@-Z\\-_]"                      # 其它单字符 ESC
)


class Runtime(object):
    """进程级运行时配置（由 main 填充，handler 只读；测试可直接改）。"""

    api_key = ""                       # 明文 key，只用于 compare_digest，**绝不写日志**
    pi_cmd = None                      # type: Optional[List[str]]  假 CLI 注入（argv 前缀）
    claude_cmd = None                  # type: Optional[List[str]]
    codex_cmd = None                   # type: Optional[List[str]]
    pi_workdir = PI_WORKDIR
    claude_workdir = CLAUDE_WORKDIR
    codex_workdir = CODEX_WORKDIR
    claude_python = sys.executable     # 跑 claude_exec.py 的解释器
    claude_exec = CLAUDE_EXEC
    busy_wait_s = BUSY_WAIT_S
    locks = {}                         # type: Dict[str, threading.Lock]
    busy = {}                          # type: Dict[str, bool]
    claude_sessions = set()            # type: set  本进程已创建过的 claude 会话 uuid
    claude_sessions_lock = threading.Lock()
    codex_sessions = {}                # type: Dict[str, str]  会话键 -> codex 自己生成的 session id
    codex_sessions_lock = threading.Lock()
    codex_sessions_loaded = False      # 是否已把磁盘映射读进内存缓存（懒加载标记）
    codex_sessions_file = None         # 覆盖落盘路径（测试注入用）；None = 用 codex_workdir()


def init_runtime_locks() -> None:
    Runtime.locks = dict((m, threading.Lock()) for m in MEMBERS)
    Runtime.busy = dict((m, False) for m in MEMBERS)


# ---------------------------------------------------------------- 文本处理

def clean_output(text: str) -> str:
    """去 ANSI 转义、去 \\r，收尾空白。中文原样保留（全程 UTF-8）。"""
    if not text:
        return ""
    return ANSI_RE.sub("", text).replace("\r\n", "\n").replace("\r", "").strip()


def build_prompt(messages: List[Dict[str, Any]]) -> Optional[str]:
    """最后一条 user 内容当任务，**它前面**的消息拼成上下文前缀，保证多轮不丢。

    形如：
        [上下文]
        对方: ...
        你: ...
        [/上下文]

        <任务正文>
    """
    idx = -1
    for i in range(len(messages) - 1, -1, -1):
        item = messages[i]
        if isinstance(item, dict) and item.get("role") == "user" \
                and isinstance(item.get("content"), str) and item["content"].strip():
            idx = i
            break
    if idx < 0:
        return None
    task = messages[idx]["content"].strip()

    lines = []
    role_map = {"user": "对方", "assistant": "你", "system": "系统"}
    for item in messages[:idx]:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        who = role_map.get(str(item.get("role") or ""), "其它")
        lines.append("%s: %s" % (who, content.strip()))
    if not lines:
        return task
    return "[上下文]\n" + "\n".join(lines) + "\n[/上下文]\n\n" + task


def resolve_model(model: str) -> Optional[Tuple[str, str]]:
    """model -> (member, cli_model)。cli_model 为空表示不给 CLI 传 --model。"""
    name = (model or "").strip()
    low = name.lower()
    if low.startswith("pi"):
        member = "piagent"
    elif low.startswith("claude"):
        member = "claudecode"
    elif low.startswith("codex"):
        member = "codex"
    else:
        return None
    cli_model = ""
    if "/" in name:
        tail = name.split("/", 1)[1].strip()
        if tail and tail.lower() != "default":
            cli_model = tail
    return member, cli_model


def safe_session_id(raw: str) -> str:
    """把任意会话键收敛成文件名安全的 id（pi 用）。"""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", raw.strip())[:64].strip("-")
    return "a2a-" + (cleaned or "anon")


def claude_session_uuid(raw: str) -> str:
    """claude 的 --session-id 要求合法 UUID，这里用 uuid5 做稳定映射。"""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "cli_backend/claude/" + raw.strip()))


# ---------------------------------------------------------------- 子进程

def _creationflags() -> int:
    """Windows 下不弹黑窗。"""
    if os.name == "nt":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return 0


def kill_tree(pid: int) -> None:
    """杀整棵进程树（Windows: taskkill /T /F）。"""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/pid", str(pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=_creationflags())
        else:
            import signal as _signal
            os.killpg(os.getpgid(pid), _signal.SIGKILL)
    except Exception as exc:  # 杀不掉也别把主流程带崩
        LOG.warning("kill_tree pid=%s 失败: %s", pid, exc)


def child_env() -> Dict[str, str]:
    """子进程环境：原样透传本进程环境，只加**输出编码/颜色**这类无害项。

    刻意不注入 ANTHROPIC_* / API key 之类越界变量：
      * pi 自己从 ~/.pi 的 auth 配置取凭据；
      * claude 的代理与 ANTHROPIC_* 清理由 claude_exec.py 负责。
    """
    env = dict(os.environ)
    env["NO_COLOR"] = "1"
    env["FORCE_COLOR"] = "0"
    # 子进程若是 Python（claude_exec / 测试桩），管道输出默认走系统 ANSI 码页会把中文写坏
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def run_cli(argv: List[str], cwd: Optional[str], timeout_s: float) -> Dict[str, Any]:
    """跑一次 CLI。返回 {rc, stdout, stderr, pid, timed_out, elapsed_ms}。

    用 Popen 是为了拿 pid —— 超时时必须能 taskkill /T /F 杀掉整棵树。
    """
    started = time.time()
    try:
        proc = subprocess.Popen(argv, cwd=cwd, env=child_env(),
                                stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                creationflags=_creationflags())
    except Exception as exc:
        return {"rc": -1, "stdout": "", "stderr": "spawn failed: %s" % exc,
                "pid": 0, "timed_out": False, "elapsed_ms": 0}

    pid = proc.pid
    timed_out = False
    try:
        out, err = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        kill_tree(pid)
        try:
            out, err = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = b"", b""
    return {
        "rc": proc.returncode,
        "stdout": clean_output((out or b"").decode("utf-8", "replace")),
        "stderr": clean_output((err or b"").decode("utf-8", "replace")),
        "pid": pid,
        "timed_out": timed_out,
        "elapsed_ms": int((time.time() - started) * 1000),
    }


# ---------------------------------------------------------------- 成员执行

def run_piagent(prompt: str, cli_model: str, session_key: Optional[str],
                timeout_s: float) -> Dict[str, Any]:
    """pi：node 直接跑包入口 cli.js，任务文本走 argv，不经 shell。"""
    if Runtime.pi_cmd:  # 测试注入的假 CLI：前缀 + 任务
        return run_cli(list(Runtime.pi_cmd) + [prompt], Runtime.pi_workdir, timeout_s)

    argv = [PI_NODE, PI_CLI_JS, "--print"]
    if cli_model:
        argv += ["--model", cli_model]
    if session_key:
        argv += ["--session-id", safe_session_id(session_key)]
    else:
        argv += ["--no-session"]   # 一次性会话，不落盘、不污染别的会话
    argv += ["--", prompt]         # -- 之后一律当消息，防止任务以 - 开头被当成选项
    return run_cli(argv, Runtime.pi_workdir, timeout_s)


def run_claudecode(prompt: str, cli_model: str, session_key: Optional[str],
                   timeout_s: float) -> Dict[str, Any]:
    """claude：一律走 claude_exec.py（代理 + 清 ANTHROPIC_*），任务用 --file 传。"""
    if Runtime.claude_cmd:  # 测试注入的假 CLI
        return run_cli(list(Runtime.claude_cmd) + [prompt], Runtime.claude_workdir, timeout_s)

    fd, path = tempfile.mkstemp(prefix="cli_backend_", suffix=".txt")
    os.close(fd)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(prompt)

        def build(resume: bool, sid: Optional[str]) -> List[str]:
            argv = [Runtime.claude_python, Runtime.claude_exec, "--file", path,
                    "--workdir", Runtime.claude_workdir,
                    # claude_exec 自己的超时放宽，让外层 taskkill 先生效 → 统一 504 语义
                    "--timeout", str(int(timeout_s) + 30)]
            if cli_model:
                argv += ["--model", cli_model]
            if sid:
                # 未知参数由 claude_exec 的 parse_known_args 原样透传给 claude.exe。
                # 必须写成 --opt=value 的**单 token** 形式：分成两个 token 的话，
                # claude_exec 自己的 argparse 会把值当成它的 prompt 位置参数吃掉，
                # 只剩一个裸 --session-id 传给 claude.exe（实测报 "argument missing"）。
                argv.append(("--resume=" if resume else "--session-id=") + sid)
            return argv

        sid = claude_session_uuid(session_key) if session_key else None
        resume = False
        if sid:
            with Runtime.claude_sessions_lock:
                resume = sid in Runtime.claude_sessions
                if not resume:
                    Runtime.claude_sessions.add(sid)

        res = run_cli(build(resume, sid), Runtime.claude_workdir, timeout_s)
        # --session-id 撞上「已存在」（例如服务重启后内存里的记录丢了）→ 自动改 --resume 重试一次
        if sid and not resume and res["rc"] != 0 and not res["stdout"] \
                and re.search(r"(already|exist|in use)", res["stderr"], re.I):
            LOG.info("claude session=%s 已存在，改用 --resume 重试", sid)
            res = run_cli(build(True, sid), Runtime.claude_workdir, timeout_s)
        return res
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def codex_executable() -> List[str]:
    """codex 可执行文件解析：① CODEX_CLI ② 原生 codex.exe ③ codex.cmd（经 cmd /c）。

    .cmd/.bat 不是 PE 可执行文件，CreateProcess 起不来，必须由 cmd /c 代跑；
    原生 exe 存在就优先用它，省掉这层 shell。
    """
    override = (os.environ.get("CODEX_CLI") or "").strip().strip('"')
    if override:
        if override.lower().endswith((".cmd", ".bat")):
            return ["cmd", "/c", override]
        return [override]
    if os.path.isfile(CODEX_EXE):
        return [CODEX_EXE]
    return ["cmd", "/c", CODEX_CMD]


CODEX_SESSIONS_ENV = "CODEX_SESSIONS_FILE"     # 覆盖落盘路径（测试注入用）


def codex_sessions_path() -> str:
    """codex 会话映射的持久化文件路径。

    原先 `Runtime.codex_sessions` 是**纯内存字典** —— 进程一重启，codex 就「静默失忆」：
    不报错、不提示，只是忽然不记得上一轮说过什么。落盘后重启可续。
    """
    override = os.environ.get(CODEX_SESSIONS_ENV)
    if override:
        return override
    if Runtime.codex_sessions_file:
        return Runtime.codex_sessions_file
    return os.path.join(codex_workdir(), ".codex_sessions.json")


def load_codex_sessions(path=None):
    # type: (Optional[str]) -> Dict[str, str]
    """读映射。文件不存在/损坏一律当空映射（不抛）—— 可用性优先于完整性。"""
    p = path or codex_sessions_path()
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        LOG.warning("codex 会话映射读不出(%s)，当空处理：%s", p, exc)
        return {}
    if not isinstance(data, dict):
        LOG.warning("codex 会话映射格式异常(不是对象)，当空处理：%s", p)
        return {}
    out = {}                    # type: Dict[str, str]
    for k, v in data.items():
        if isinstance(v, dict) and isinstance(v.get("sid"), str):
            out[str(k)] = v["sid"]
        elif isinstance(v, str):    # 兼容裸 {"key": "sid"} 形态
            out[str(k)] = v
    return out


def save_codex_sessions(mapping, path=None) -> bool:
    """原子写映射（临时文件 + os.replace）。失败只打警告，**不抛** —— 不阻断请求。

    原子替换的意义：并发读永远看不到半截文件（否则 crashes 会读出一个损坏 JSON，
    被当成「空映射」，等于会话又丢了）。
    """
    p = path or codex_sessions_path()
    payload = dict((str(k), {"sid": str(v), "updated_at": int(time.time())})
                   for k, v in mapping.items())
    try:
        d = os.path.dirname(p) or "."
        fd, tmp = tempfile.mkstemp(prefix=".codex_sessions_", suffix=".tmp", dir=d)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, p)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return True
    except Exception as exc:
        LOG.warning("codex 会话映射落盘失败(%s)：%s", p, exc)
        return False


def ensure_codex_sessions_loaded() -> None:
    """首次使用时把磁盘映射读进内存缓存（进程重启后 codex 会话仍能续上）。"""
    if Runtime.codex_sessions_loaded:
        return
    with Runtime.codex_sessions_lock:
        if Runtime.codex_sessions_loaded:
            return
        Runtime.codex_sessions.update(load_codex_sessions())
        Runtime.codex_sessions_loaded = True


def remember_codex_session(session_key, stderr_text):
    # type: (Optional[str], Optional[str]) -> Optional[str]
    """从 codex 的 stderr banner 里抓会话 id 并立刻落盘；返回抓到的 id，没抓到返回 None。

    为什么抽成独立函数：`run_codex` 的**假 CLI 分支是 early return**（第 486 行），
    走不到真路径那一段，内联写法根本测不到；独立出来就能直接喂 stderr 文本单测。

    语义与原来的 `dict.setdefault` 一致：只在**首次**（该 key 还没有 sid）时写入。
    """
    if not session_key:
        return None
    found = CODEX_SESSION_RE.search(stderr_text or "")
    if not found:
        return None
    with Runtime.codex_sessions_lock:
        if session_key in Runtime.codex_sessions:
            return Runtime.codex_sessions[session_key]
        Runtime.codex_sessions[session_key] = found.group(1)
        save_codex_sessions(Runtime.codex_sessions)   # 立即落盘，否则重启即静默失忆
        return Runtime.codex_sessions[session_key]


def codex_workdir() -> str:
    """codex 的工作目录；不存在就退回用户 HOME（codex 在不存在的 cwd 里根本起不来）。"""
    directory = Runtime.codex_workdir
    if directory and os.path.isdir(directory):
        return directory
    home = os.path.expanduser("~")
    LOG.warning("codex 工作目录不可用 %s，退回 HOME %s", directory, home)
    return home


def run_codex(prompt: str, cli_model: str, session_key: Optional[str],
              timeout_s: float) -> Dict[str, Any]:
    """codex：`codex exec` 一次性跑完即退出。

    实测（v0.155）：banner（workdir/model/**session id**/tokens used）全走 **stderr**，
    stdout 只有最终答复。所以：
      * 正文优先取 `-o/--output-last-message <FILE>`（最稳），取不到才退回 stdout；
      * 会话 id 从 **stderr** 抓，抓 stdout 抓不到（踩过：第二轮拿不到上文）。
    """
    if Runtime.codex_cmd:  # 测试注入的假 CLI：前缀 + 任务（与 piagent 一致）
        return run_cli(list(Runtime.codex_cmd) + [prompt], codex_workdir(), timeout_s)

    workdir = codex_workdir()
    fd, last_msg = tempfile.mkstemp(prefix="cli_backend_codex_", suffix=".txt")
    os.close(fd)
    try:
        sid = None
        if session_key:
            ensure_codex_sessions_loaded()      # 重启后也要能读回上次的会话
            with Runtime.codex_sessions_lock:
                sid = Runtime.codex_sessions.get(session_key)

        def build(skip_git: bool) -> List[str]:
            argv = codex_executable() + ["exec"]
            if sid:
                argv.append("resume")          # resume 的位置参数是 [SESSION_ID] [PROMPT]
            # 不传 --color：exec 认它、exec resume 不认（实测 "unexpected argument '--color'"）。
            # 颜色由 child_env 的 NO_COLOR=1 + 管道输出压掉，残留也有 clean_output 兜底。
            argv += ["-o", last_msg]
            if cli_model:
                argv += ["--model", cli_model]
            if skip_git:
                argv.append("--skip-git-repo-check")
            if not session_key:
                argv.append("--ephemeral")     # 一次性会话，不落盘、不污染别的会话
            argv.append("--")                  # -- 之后一律当位置参数，防任务以 - 开头
            if sid:
                argv.append(sid)
            argv.append(prompt)
            return argv

        res = run_cli(build(False), workdir, timeout_s)
        # 非 git 仓库 / 未受信任目录 → 加 --skip-git-repo-check 重试一次
        if not res["timed_out"] and res["rc"] != 0 and not res["stdout"] \
                and CODEX_UNTRUSTED_RE.search(res["stderr"]):
            LOG.info("codex 在 %s 被判为非 git 仓库/未受信任目录，加 --skip-git-repo-check 重试", workdir)
            res = run_cli(build(True), workdir, timeout_s)

        if not res["timed_out"] and res["rc"] == 0:
            if session_key and not sid:
                remember_codex_session(session_key, res["stderr"])
            final = ""
            try:
                with open(last_msg, "r", encoding="utf-8", errors="replace") as f:
                    final = clean_output(f.read())
            except OSError as exc:
                LOG.warning("codex 最终答复文件读不到(%s)，退回 stdout", exc)
            if final:
                res["stdout"] = final
        return res
    finally:
        try:
            os.unlink(last_msg)
        except OSError:
            pass


RUNNERS = {"piagent": run_piagent, "claudecode": run_claudecode, "codex": run_codex}


# ---------------------------------------------------------------- HTTP

# 传输职责（读体 / JSON 信封 / 路由 / 404 / 未注册方法 501）交给 L1 `web_gateway`，
# 本件只登记「路径 → 处理函数」。形态与迁移前**逐字一致**：
#   · Server 头仍是 `cli_backend/<VERSION>`（server_version + sys_version="")，
#     protocol_version 仍是 HTTP/1.1（keep-alive → 请求体必须读完）；
#   · 每个响应仍只打一行 `<方法> <原始路径> -> <码> ip=<ip>`（见 CLIBackendHandler._reply）；
#   · 鉴权在**路径白名单之后、解析 body 之前**：迁移前是「先查路径 → 再 401 → 再解析 body」，
#     所以钩子只对 AUTH_PATHS 里的 (方法, 归一化路径) 校验 —— 未登记路径照样落 404 而不是 401。
GW = web_gateway.Gateway("cli_backend", version=VERSION, quiet=True)

# 需要鉴权的 (方法, 归一化路径)，与下面的 @GW.route 一一对应；/healthz 公开。
AUTH_PATHS = frozenset((
    ("GET", "/v1/models"), ("GET", "/models"),
    ("POST", "/v1/chat/completions"), ("POST", "/chat/completions"),
))


def _authed(req: "web_gateway.Request") -> bool:
    """Bearer key 比对；key 只在内存里，日志绝不打印。"""
    header = req.headers.get("Authorization") or ""
    if not header.startswith("Bearer "):
        return False
    token = header[7:].strip()
    if not token:
        return False
    return hmac.compare_digest(token, Runtime.api_key)


@GW.auth
def _auth_hook(req):
    if (req.method, req.path) in AUTH_PATHS and not _authed(req):
        return 401, {"ok": False, "error": "unauthorized"}
    return None


@GW.not_found
def _not_found(req):
    """本件的 404 不带 path 字段（L1 的默认信封带），照迁移前的字面量回。"""
    return 404, {"ok": False, "error": "not_found"}


@GW.route("/healthz")
def _healthz(req):  # 公开，不鉴权
    return 200, {"ok": True, "service": "cli_backend",
                 "version": VERSION, "members": list(MEMBERS),
                 "busy": dict(Runtime.busy), "ts": int(time.time())}


@GW.route("/v1/models")
@GW.route("/models")
def _models(req):
    now = int(time.time())
    data = [{"id": mid, "object": "model", "created": now, "owned_by": "cli_backend"}
            for mid in MODEL_IDS]
    return 200, {"object": "list", "data": data}


# a2a_svc 配 backend=http://127.0.0.1:9970/v1 → 打到 /v1/chat/completions；
# 裸 /chat/completions 也一并接受，省得配错前缀就整条链路断。
@GW.route("/v1/chat/completions", methods=("POST",))
@GW.route("/chat/completions", methods=("POST",))
def _chat_completions(req):
    try:
        body = json.loads(req.body.decode("utf-8"))
    except Exception:
        return 400, {"ok": False, "error": "bad_request", "detail": "invalid json"}
    if not isinstance(body, dict):
        return 400, {"ok": False, "error": "bad_request"}

    model = body.get("model")
    if not isinstance(model, str) or not model.strip():
        return 400, {"ok": False, "error": "bad_request", "detail": "missing model"}
    resolved = resolve_model(model)
    if resolved is None:
        return 404, {"ok": False, "error": "unknown_model",
                     "detail": "supported: " + ",".join(MODEL_IDS)}
    member, cli_model = resolved

    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return 400, {"ok": False, "error": "bad_request", "detail": "missing messages"}
    prompt = build_prompt(messages)
    if not prompt:
        return 400, {"ok": False, "error": "bad_request", "detail": "no user content"}

    try:
        timeout_s = float(body.get("timeout_s") or DEFAULT_TIMEOUT_S)
    except (TypeError, ValueError):
        return 400, {"ok": False, "error": "bad_request", "detail": "bad timeout_s"}
    timeout_s = max(1.0, min(timeout_s, MAX_TIMEOUT_S))

    session_key = body.get("user")
    if not isinstance(session_key, str) or not session_key.strip():
        session_key = None  # 一次性会话

    return _dispatch(member, cli_model, model, prompt, session_key, timeout_s)


def _dispatch(member: str, cli_model: str, model: str, prompt: str,
              session_key: Optional[str], timeout_s: float) -> Tuple[int, Dict[str, Any]]:
    """同一成员串行（各自一把锁，成员之间可并行）；排队等不到 → 503 busy。"""
    lock = Runtime.locks[member]
    if not lock.acquire(timeout=Runtime.busy_wait_s):
        LOG.info("model=%s member=%s task=%s code=503 rc=- pid=- elapsed_ms=0 busy",
                 model, member, _task_head(prompt))
        return 503, {"ok": False, "error": "busy",
                     "detail": "%s 正在执行其它任务" % member}
    Runtime.busy[member] = True
    try:
        res = RUNNERS[member](prompt, cli_model, session_key, timeout_s)
    except Exception as exc:
        LOG.exception("model=%s member=%s 执行异常", model, member)
        return 502, {"ok": False, "error": "backend_error",
                     "detail": str(exc)[:DETAIL_MAX]}
    finally:
        Runtime.busy[member] = False
        lock.release()

    code = 200
    if res["timed_out"]:
        code = 504
    elif res["rc"] != 0 or not res["stdout"]:
        code = 502
    LOG.info("model=%s member=%s task=%s elapsed_ms=%d code=%d rc=%s pid=%s",
             model, member, _task_head(prompt), res["elapsed_ms"], code,
             res["rc"], res["pid"])

    if code == 504:
        return 504, {"ok": False, "error": "backend_timeout",
                     "detail": "%s 超时 %.0fs，已杀进程树 pid=%s"
                               % (member, timeout_s, res["pid"])}
    if code == 502:
        # 绝不编造回复：非零退出或空输出一律如实报错，detail 带 stderr 尾部
        detail = res["stderr"][-DETAIL_MAX:] or ("rc=%s 且输出为空" % res["rc"])
        return 502, {"ok": False, "error": "backend_error", "detail": detail}

    return 200, {
        "id": "chatcmpl-cli-%s" % uuid.uuid4().hex[:16],
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": res["stdout"]}}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


# 必须在全部 @GW.route 之后取：handler_class() 会按「已登记的方法」删掉多余的
# do_<METHOD>，只剩 GET/POST —— 其余方法回基类 501，与迁移前逐字一致。
_GatewayHandler = GW.handler_class()


class CLIBackendHandler(_GatewayHandler):
    """L1 网关处理器 + 本件特有的三处差异，其余（路由/信封/501/日志静默）全部沿用 L1。

    1. **响应形态**：`sys_version=""` + HTTP/1.1，保住 `Server: cli_backend/1.0` 与
       keep-alive 语义（L1 默认 HTTP/1.0 且不清 sys_version）。
    2. **请求体**：keep-alive 下必须读完，否则连接错位；且沿用本件自带的 MAX_BODY_BYTES
       上限 —— L1 的惰性 `Request.body` 不带上限，所以在 _handle 里**预读**并塞回缓存。
    3. **路径归一**：沿用 `urlsplit(...).path`（比 L1 的 partition("?") 多剥 #fragment），
       归一化口径与迁移前逐字一致。
    """

    server_version = "cli_backend/" + VERSION
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def _read_body(self) -> bytes:
        """必须读完请求体，否则 keep-alive 连接会错位。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return b""
        if length > MAX_BODY_BYTES:
            length = MAX_BODY_BYTES
        return self.rfile.read(length)

    def _handle(self) -> None:
        req = web_gateway.Request(self)
        # 归一化口径对齐迁移前的 _path()
        req.path = urllib.parse.urlsplit(self.path).path.rstrip("/") or "/"
        # 预读写进 Request 的惰性缓存：处理函数用 req.body 时不会再去碰 socket
        req._body = self._read_body()
        # 不再兜 HttpError：GW.dispatch 已把钩子/处理函数抛出的 HttpError 全转成回复，
        # 本件自身也不抛它 —— 留个空 except 只会掩盖真异常。
        status, body, headers = GW.dispatch(req)
        self._reply(status, body, headers)

    def _reply(self, status: int, body: bytes, headers: Dict[str, str]) -> None:
        """回复后补一行日志（迁移前由 _send_json 打，格式/字段/取值一字不改）。"""
        _GatewayHandler._reply(self, status, body, headers)
        LOG.info("%s %s -> %d ip=%s", self.command, self.path, status,
                 self.client_address[0] if self.client_address else "-")


def _task_head(prompt: str) -> str:
    """日志里只留任务前 40 字（单行），不记全文。"""
    return prompt.replace("\n", " ")[:TASK_LOG_MAX]


# ---------------------------------------------------------------- 入口

def load_key(path: str) -> str:
    """读 key 文件。不存在 / 空 → 抛异常，由 main 拒绝启动（fail closed）。"""
    with open(path, "r", encoding="utf-8") as f:
        key = f.read().strip()
    if not key:
        raise ValueError("key 文件为空: %s" % path)
    return key


def setup_logging(log_file: str) -> None:
    """单行日志：同时写文件与 stdout（目录自建）。"""
    handlers = [logging.StreamHandler(sys.stdout)]
    try:
        directory = os.path.dirname(log_file)
        if directory:
            os.makedirs(directory, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    except OSError as exc:
        sys.stdout.write("日志文件不可写(%s)，仅输出到 stdout\n" % exc)
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(message)s")


def make_server(host: str, port: int) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), CLIBackendHandler)
    httpd.daemon_threads = True
    return httpd


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="本机 CLI 成员后端（stdlib only）")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认 127.0.0.1（只绑 loopback）")
    parser.add_argument("--port", type=int, default=9970, help="监听端口，默认 9970")
    parser.add_argument("--key-file", default=DEFAULT_KEY_FILE, help="Bearer key 文件路径")
    parser.add_argument("--log-file", default=DEFAULT_LOG_FILE, help="单行日志文件路径")
    parser.add_argument("--pi-workdir", default=PI_WORKDIR, help="pi 的工作目录")
    parser.add_argument("--claude-workdir", default=CLAUDE_WORKDIR, help="claude 的工作目录")
    parser.add_argument("--codex-workdir", default=CODEX_WORKDIR,
                        help="codex 的工作目录（默认取环境变量 CODEX_WORKDIR）")
    parser.add_argument("--busy-wait", type=float, default=BUSY_WAIT_S,
                        help="同成员排队最长等待秒数，超时返回 503")
    parser.add_argument("--stub", action="store_true",
                        help="自测用：三个成员都替换成内置回声桩，不碰真实 CLI")
    parser.add_argument("--pi-cmd", default=None, help="自测用：JSON argv 前缀，替换 pi 的真实命令")
    parser.add_argument("--claude-cmd", default=None, help="自测用：JSON argv 前缀，替换 claude 的真实命令")
    parser.add_argument("--codex-cmd", default=None, help="自测用：JSON argv 前缀，替换 codex 的真实命令")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.log_file)
    init_runtime_locks()

    try:
        Runtime.api_key = load_key(args.key_file)
    except Exception as exc:
        LOG.error("读取 key 文件失败，拒绝启动（fail closed）: %s", exc)
        return 2

    Runtime.pi_workdir = args.pi_workdir
    Runtime.claude_workdir = args.claude_workdir
    Runtime.codex_workdir = args.codex_workdir
    Runtime.busy_wait_s = args.busy_wait
    if args.stub:
        Runtime.pi_cmd = [sys.executable, "-c", STUB_CODE]
        Runtime.claude_cmd = [sys.executable, "-c", STUB_CODE]
        Runtime.codex_cmd = [sys.executable, "-c", STUB_CODE]
    if args.pi_cmd:
        Runtime.pi_cmd = json.loads(args.pi_cmd)
    if args.claude_cmd:
        Runtime.claude_cmd = json.loads(args.claude_cmd)
    if args.codex_cmd:
        Runtime.codex_cmd = json.loads(args.codex_cmd)

    for directory in (Runtime.pi_workdir, Runtime.claude_workdir, Runtime.codex_workdir):
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError as exc:
            LOG.warning("工作目录不可用 %s: %s", directory, exc)

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        LOG.error("只允许绑 loopback，拒绝监听 %s", args.host)
        return 2

    httpd = make_server(args.host, args.port)
    LOG.info("cli_backend %s listening on %s:%d members=%s stub=%s",
             VERSION, httpd.server_address[0], httpd.server_address[1],
             ",".join(MEMBERS),
             bool(Runtime.pi_cmd or Runtime.claude_cmd or Runtime.codex_cmd))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        LOG.info("收到中断，退出")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
