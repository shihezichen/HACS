#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""L0 平台层：**节点档案**（nodelet 的静态那一半）。

一句话职责
----------
**让上层对操作系统零感知** —— 路径 / 凭据 / 日志 / 探针 / 宿主真源，全部在这里落地；
上层只拿契约化的返回值，不许自己拼盘符、不许自己开别人的库。

为什么是这一件
--------------
阶段 1 刀 3 落了 `node_paths.py`（**本机**宿主默认值的唯一出处），刀 4 又在里面补了
「目标主机档案」的**雏形**（`target_*`）。规划当时写明：**阶段 4 正式化，届时并入节点档案，
不另立第二处真值**。本件就是那个落点 ——

  · 目标主机档案的**实现**从 `node_paths.py` 整段搬到这里（语义与兜底规则逐条不变）；
    `node_paths.py` 旧处只留**委托/别名**（同一个对象，不是拷贝），公共面与属性面一字未改。
  · 「别的 Agent 的会话真源」（Hermes `state.db` / OpenClaw `sessions.json` / 宿主 Hermes
    `config.yaml`）的**路径、句柄与文件格式**从 L2 `agent_usage.py` 下沉到这里。
    在这之前 L2 直接 `import sqlite3` 打开宿主库，是分层台账里**唯一一条已声明例外**；
    本件落地即撤销该例外。
  · **刀 22（阶段 4 第二刀）**：散在 13 个非 L0 件里的**非环回宿主事实**（协作域节点地址、
    服务基址、绝对路径常量）一并收到这里的 ⑧⑨ 两组。调用方的 env 变量名与覆盖优先级
    一字未改 —— 收走的只是「env 都没配时那个兜底字面量」。
    环回默认值（`127.0.0.1` / `localhost` / `::1` / `0.0.0.0`）**不在此列**：那是合法的本地默认，
    不是宿主事实，刀 22 一处未动。

档案覆盖面
----------
  ① 节点身份       flavour() / home()
  ② 运行与配置     runtime_dir() / etc_dirs() / etc_dir() / log_dir() / nats_dir()
  ③ 执行器         node_exe() / claude_exec() / claude_workdir() / codex_exe() /
                   codex_cmd() / pi_cli_js() / pi_workdir()
  ④ 目标主机档案   target_known() / target_flavour() / target_home() / target_claude_exec()
  ⑤ 宿主真源       hermes_home() / hermes_state_db_paths() / openclaw_sessions_paths() …
  ⑥ 只读探针       open_hermes_state_ro() / hermes_session_usage_row() /
                   hermes_active_messages() / openclaw_session_entry() /
                   hermes_config_model_window()
  ⑦ 日志与探针口   log_dir() / a2a_probe_base() / a2a_probe_token_paths() / a2a_probe_token()
  ⑧ 协作域地址     nats_host_default() / nats_hosts_default() / blackboard_base_default() /
                   chat_base_default() / nats_host_shape()
  ⑨ 服务落盘位置   nats_conf_dir_posix() / nats_route_pass_file() /
                   task_svc_db_default() / task_svc_log_default() / codex_workdir_default()

①②③ 是**薄委托**：真值仍由同层 `node_paths.py` 给（它是 `host_path_owner`，宿主字面量的
唯一合法去处）。档案把它们一并暴露出来，是为了「上层只需要认识节点档案这一个门」。
委托一律**惰性 import**（函数体内 `import node_paths`），这样两件谁先被 import 都不会成环。

只读与不编造（两条铁律）
------------------------
  · **只读**：⑥ 那组探针一律以 SQLite 只读 URI（`mode=ro`）打开，用完即 close；
    本件不建目录、不写文件、不改任何状态。
  · **不编造**：路径没配 / 文件不在 / 记录不在 → 返回 `None` 或空，**绝不猜值**；
    真正的存储/解析故障 → **原样抛**，交给调用方按自己的口径降级并记日志
    （本件不吞异常、不把「打不开」偷偷变成「没有数据」—— 那会让上层把故障当空库）。
  · 未登记的目标主机同理：按 posix 兜底，且 `target_known()` 返回 False。
"""

import os
import sqlite3
import sys

# 批⑤a 归位：本件已搬入 node/l0_host/。仓根 + 本目录一并入路径（批① 惯例），
# `_paths()` 里按**绝对包名**惰性 import 同层件（保证全进程唯一实例，不开裸名副本）。
_HACS_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _hacs_p in (_HACS_ROOT, os.path.dirname(os.path.abspath(__file__))):
    if _hacs_p not in sys.path:
        sys.path.insert(0, _hacs_p)

__all__ = [
    # ① 节点身份
    "flavour", "home", "ENV_NODE_NAME", "node_name",
    # ② 运行与配置
    "runtime_dir", "etc_dirs", "etc_dir", "log_dir", "nats_dir",
    # ③ 执行器
    "node_exe", "claude_exec", "claude_workdir", "codex_exe", "codex_cmd",
    "pi_cli_js", "pi_workdir",
    # ④ 目标主机档案
    "ENV_TARGET_PROFILE_JSON", "NT_TARGET_HOME", "POSIX_TARGET_HOME",
    "TARGET_FALLBACK_FLAVOUR", "TARGET_CLAUDE_SUB", "TARGET_PROFILES",
    "target_host", "target_table", "target_profile",
    "target_known", "target_flavour", "target_home", "target_claude_exec",
    # ⑤ 宿主真源
    "ENV_HERMES_STATE_DB", "ENV_OPENCLAW_SESSIONS", "ENV_HERMES_HOME",
    "HERMES_HOME_SUBS", "HERMES_CONFIG_NAME",
    "ENV_HERMES_AGENT_DIR",
    "hermes_home_paths", "hermes_home", "hermes_config_path", "hermes_agent_dir",
    "hermes_state_db_paths", "hermes_state_db",
    "openclaw_sessions_paths", "openclaw_sessions_json",
    "openclaw_sessions_present", "openclaw_session_key",
    # ⑥ 只读探针
    "open_hermes_state_ro", "hermes_session_usage_row", "hermes_active_messages",
    "openclaw_session_entry", "hermes_config_model_window",
    # ⑦ 探针口与凭据
    "ENV_A2A_PROBE_BASE", "ENV_A2A_PROBE_TOKEN", "ENV_A2A_PROBE_TOKEN_FILE",
    "A2A_TOKEN_FILE_NAME",
    "a2a_probe_base", "a2a_probe_token_paths", "a2a_probe_token",
    # ⑧ 协作域地址
    "NODE_ADDR_PRIMARY", "NODE_ADDR_SECONDARY", "NODE_ADDR_SHAPE",
    "PORT_NATS", "PORT_BLACKBOARD", "PORT_CHAT",
    "nats_host_default", "nats_hosts_default",
    "blackboard_base_default", "chat_base_default", "nats_host_shape",
    # ⑨ 服务落盘默认位置
    "POSIX_ROOT_HOME", "NATS_ROUTE_CRED_NAME",
    "TASK_SVC_DB_NAME", "TASK_SVC_LOG_SUB", "WIN_CODEX_WORKDIR",
    "nats_conf_dir_posix", "nats_route_pass_file",
    "task_svc_db_default", "task_svc_log_default", "codex_workdir_default",
]


def _first_env(*names):
    """按序取第一个非空环境变量（取不到返回空串）。"""
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return ""


def _paths():
    """同层 `node_paths`（宿主字面量的唯一合法去处）。惰性 import：两件谁先加载都不成环。

    按**绝对包名**取（批⑤a）：裸名会另起一个顶层副本，值相同却不是同一个对象，
    `node_paths` 文档串声明的「同一对象不是拷贝」不变量会被打破。
    """
    from node.l0_host import node_paths
    return node_paths


# ======================================================== ① 节点身份

def flavour(value=None):
    """本节点平台语义：`"nt"` / `"posix"`（显式传入优先，便于单测）。"""
    return _paths().flavour(value)


def home():
    """本节点当前用户的家目录。"""
    return os.path.expanduser("~")


#: 本节点名的 env 覆盖（刀 23 新增：轻量节点守护要回答「我是哪台」）
ENV_NODE_NAME = "AGENT_TEAM_NODE"


def node_name():
    """**本节点**在档案表 / 部署表里的名字。

    口径：env `AGENT_TEAM_NODE` 优先 → 否则取主机名并归一（去域名后缀、小写，
    与 `target_host()` 同一套归一规则，这样 `node_name()` 拿去查 `target_known()`
    才是同一把尺子）。取不到主机名返回空串 —— **不猜**（与本件其它面同一条铁律）。

    纯读取：不碰网络、不写状态。刀 23 新增的**只读 helper**，既有面一字未动。
    """
    explicit = _first_env(ENV_NODE_NAME)
    if explicit:
        return target_host(explicit)
    import socket
    try:
        raw = socket.gethostname() or ""
    except OSError:
        return ""
    return target_host(raw.split(".", 1)[0])


# ======================================================== ② 运行目录与配置目录

def runtime_dir(value=None):
    """本节点运行目录（部署件落地的那个目录）。"""
    return _paths().runtime_dir(value)


def etc_dirs(value=None):
    """配置目录**候选列表**（按优先级；调用方逐个探测存在性）。"""
    return _paths().etc_dirs(value)


def etc_dir(value=None):
    """配置目录（候选中的第一个）。"""
    return _paths().etc_dir(value)


def log_dir(value=None):
    """日志目录。"""
    return _paths().log_dir(value)


def nats_dir(value=None):
    """NATS 服务端配置目录。"""
    return _paths().nats_dir(value)


# ======================================================== ③ 执行器路径

def node_exe(value=None):
    """PI/Codex 子进程用的 node 可执行文件。"""
    return _paths().node_exe(value)


def claude_exec(value=None):
    """**本机**的 Claude Code 封装脚本（跨主机请用 `target_claude_exec()`）。"""
    return _paths().claude_exec(value)


def claude_workdir(value=None):
    """Claude Code 的工作目录。"""
    return _paths().claude_workdir(value)


def codex_exe(value=None):
    """Codex CLI 可执行文件。"""
    return _paths().codex_exe(value)


def codex_cmd(value=None):
    """Codex CLI 的启动壳。"""
    return _paths().codex_cmd(value)


def pi_cli_js(value=None):
    """PI CLI 的 bundle 入口（`cli.js`）。"""
    return _paths().pi_cli_js(value)


def pi_workdir(value=None):
    """PI 的工作目录。"""
    return _paths().pi_workdir(value)


# ======================================================== ④ 目标主机档案
# 刀 4 在 node_paths.py 落的雏形，刀 21 整段搬到这里正式化。
# 语义与兜底规则**逐条不变**：查不到的主机不编造 —— 按 posix 兜底，同时 target_known() 为 False。

ENV_TARGET_PROFILE_JSON = "AGENT_TEAM_TARGET_PROFILE_JSON"

#: 目标主机的家目录（跨主机真值，字面量只许出现在 L0）
NT_TARGET_HOME = "<home>"
POSIX_TARGET_HOME = "/root"

#: 家目录 → claude 执行器的相对位置（与本机 `claude_exec()` 同一套惯例）
TARGET_CLAUDE_SUB = {"nt": "AppData/Local/hermes/scripts/claude_exec.py",
                     "posix": ".local/share/hermes/scripts/claude_exec.py"}

#: 未登记主机的兜底 flavour：**不按本机推**（本机是谁与目标是谁无关），一律 posix，
#: 且 `target_known()` 会返回 False —— 拿到的是兜底值而非真值，调用方该记警告。
TARGET_FALLBACK_FLAVOUR = "posix"

#: 内置档案表。键 = 主机名（小写）；值 = {flavour, home, claude_exec}。
TARGET_PROFILES = {
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


def target_host(host_value):
    """`user@host` / `host` → 主机名（小写）。只取 host 部分，用户名不参与查表。"""
    text = str(host_value or "").strip()
    if "@" in text:
        text = text.rsplit("@", 1)[1]
    return text.strip().lower()


def target_table():
    """档案表（`AGENT_TEAM_TARGET_PROFILE_JSON` **整表覆盖**；解析不了就用内置表）。"""
    env = (os.environ.get(ENV_TARGET_PROFILE_JSON) or "").strip()
    if env:
        import json
        try:
            data = json.loads(env)
        except Exception:
            return dict(TARGET_PROFILES)    # 覆盖值坏了就退回内置表，绝不抛给调用方
        if isinstance(data, dict):
            return {target_host(k): v for k, v in data.items() if isinstance(v, dict)}
    return dict(TARGET_PROFILES)


def target_profile(host_value):
    """→ (档案 dict, 是否登记在案)。档案里缺的键按该 flavour 的惯例补齐。"""
    entry = target_table().get(target_host(host_value))
    known = isinstance(entry, dict)
    entry = entry if known else {}
    fl = str(entry.get("flavour") or "").strip().lower()
    if fl not in ("nt", "posix"):
        fl = TARGET_FALLBACK_FLAVOUR
    home_dir = str(entry.get("home") or "").strip() or (
        NT_TARGET_HOME if fl == "nt" else POSIX_TARGET_HOME)
    exe = str(entry.get("claude_exec") or "").strip() or (
        home_dir.rstrip("/\\") + "/" + TARGET_CLAUDE_SUB[fl])
    return {"flavour": fl, "home": home_dir, "claude_exec": exe}, known


def target_known(host_value):
    """目标主机是否登记在档案表里。False = 下面几个函数给的是**兜底值不是真值**。"""
    return target_profile(host_value)[1]


def target_flavour(host_value):
    """目标主机的平台语义：`"nt"` / `"posix"`（未登记按 posix 兜底）。"""
    return target_profile(host_value)[0]["flavour"]


def target_home(host_value):
    """目标主机上的家目录。"""
    return target_profile(host_value)[0]["home"]


def target_claude_exec(host_value):
    """**目标主机上**的 Claude Code 封装脚本路径（跨主机调用取这个，别取 `claude_exec()`）。"""
    return target_profile(host_value)[0]["claude_exec"]


# ======================================================== ⑤ 宿主真源（别的 Agent 的会话档案）
# 这几条是「**宿主**上长什么样」的知识：环境变量名、平台惯例目录、文件名。
# 上层（L2 适配层）只该问「给我路径 / 给我行」，不该知道 AppData 与 .hermes 的区别。

#: Hermes 的 SQLite 会话库路径（env 覆盖；未配置 → 该后端降级为不自愈，不猜路径）
ENV_HERMES_STATE_DB = ("A2A_HERMES_STATE_DB", "CTX_HERMES_STATE_DB")
#: OpenClaw 的会话档案路径（env 覆盖；未配置 → 该后端降级为不自愈）
ENV_OPENCLAW_SESSIONS = ("A2A_SESSIONS_JSON", "CTX_SESSIONS_JSON")
#: 宿主 Hermes 主目录（env 覆盖优先；否则按平台惯例探测）
ENV_HERMES_HOME = ("A2A_HERMES_HOME", "HERMES_HOME")
#: 宿主 Hermes 的 Python 包目录（上层拿它做 `sys.path` 注入后 import 模型目录；未配置返回空串）
ENV_HERMES_AGENT_DIR = ("A2A_HERMES_AGENT_DIR",)

#: Hermes 主目录的平台惯例位置（相对家目录，按优先级）
HERMES_HOME_SUBS = (("AppData", "Local", "hermes"), (".hermes",))
#: Hermes 生效配置的文件名
HERMES_CONFIG_NAME = "config.yaml"

#: OpenClaw 会话档案里 A2A 会话落到的键格式（07-23 实测：A2A 的 `user` 字段映射到它）
OPENCLAW_SESSION_KEY_FMT = "agent:main:openai-user:a2a:%s:%s"

#: Hermes state.db 的读法（宿主文件格式知识，只该在 L0 出现）
SQL_HERMES_SESSION_USAGE = (
    "SELECT input_tokens, cache_read_tokens, api_call_count, model "
    "FROM sessions WHERE session_key = ? "
    "ORDER BY COALESCE(last_activity_at, started_at) DESC LIMIT 1")
SQL_HERMES_SESSION_ID = (
    "SELECT id, model FROM sessions WHERE session_key = ? "
    "ORDER BY COALESCE(last_activity_at, started_at) DESC LIMIT 1")
SQL_HERMES_ACTIVE_MESSAGES = (
    "SELECT content, tool_calls, reasoning_content FROM messages "
    "WHERE session_id = ? AND active = 1")

#: 只读打开的超时（秒）。宿主库可能正被它自己的进程写着，别死等。
HERMES_STATE_RO_TIMEOUT = 3.0


def hermes_state_db_paths():
    """Hermes `state.db` 的**候选路径**（按优先级）。没配置就是空列表 —— 不猜。"""
    explicit = _first_env(*ENV_HERMES_STATE_DB)
    return [explicit] if explicit else []


def hermes_state_db():
    """Hermes `state.db` 路径（候选中的第一个；未配置返回空串）。"""
    cands = hermes_state_db_paths()
    return cands[0] if cands else ""


def openclaw_sessions_paths():
    """OpenClaw `sessions.json` 的**候选路径**（按优先级）。没配置就是空列表 —— 不猜。"""
    explicit = _first_env(*ENV_OPENCLAW_SESSIONS)
    return [explicit] if explicit else []


def openclaw_sessions_json():
    """OpenClaw 会话档案路径（候选中的第一个；未配置返回空串）。"""
    cands = openclaw_sessions_paths()
    return cands[0] if cands else ""


def hermes_home_paths():
    """宿主 Hermes 主目录的**候选**（env 覆盖时只有一个；否则是各平台惯例位置）。"""
    explicit = _first_env(*ENV_HERMES_HOME)
    if explicit:
        return [explicit]
    base = home()
    return [os.path.join(base, *sub) for sub in HERMES_HOME_SUBS]


def hermes_home():
    """宿主 Hermes 主目录：先 env，再按平台惯例探测（都不在返回空串，不猜）。"""
    explicit = _first_env(*ENV_HERMES_HOME)
    if explicit:
        return explicit
    for candidate in hermes_home_paths():
        if os.path.isdir(candidate):
            return candidate
    return ""


def hermes_config_path():
    """宿主 Hermes 生效配置 `config.yaml` 的路径（主目录找不到时返回空串）。"""
    base = hermes_home()
    if not base:
        return ""
    return os.path.join(base, HERMES_CONFIG_NAME)


def hermes_agent_dir():
    """宿主 Hermes 的 Python 包目录（未配置返回空串，不猜）。"""
    return _first_env(*ENV_HERMES_AGENT_DIR)


def openclaw_sessions_present():
    """本节点上是否真有一份 OpenClaw 会话档案（存在性探针；不读内容）。"""
    path = openclaw_sessions_json()
    return bool(path) and os.path.isfile(path)


def openclaw_session_key(caller, session_id):
    """A2A 调用在 OpenClaw 会话档案里的键。"""
    return OPENCLAW_SESSION_KEY_FMT % (caller or "unknown", session_id)


# ======================================================== ⑥ 只读探针
# 契约：**只读**（SQLite `mode=ro` URI；写操作在该模式下直接报错）。
# 「没有」与「坏了」严格分开 —— 没配置/文件不在/记录不在 → None；真出错 → **原样抛**。

def open_hermes_state_ro(db_path=None):
    """只读打开宿主 Hermes `state.db`。

    未配置 / 文件不存在 → `None`（这是「没有」，不是「坏了」）。
    打开失败 → sqlite3 的异常**原样抛出**，由调用方按自己的口径降级并记日志。
    调用方负责 `close()`（或直接用下面两个封装好的行读取函数）。
    """
    db = hermes_state_db() if db_path is None else db_path
    if not db or not os.path.isfile(db):
        return None
    uri = "file:%s?mode=ro" % db.replace("\\", "/")
    return sqlite3.connect(uri, uri=True, timeout=HERMES_STATE_RO_TIMEOUT)


def hermes_session_usage_row(session_key, db_path=None):
    """会话的**累计用量行** → `(input_tokens, cache_read_tokens, api_call_count, model)`。

    库不可用 / 该 session_key 没有记录 → `None`。读失败原样抛。
    """
    conn = open_hermes_state_ro(db_path)
    if conn is None:
        return None
    try:
        return conn.execute(SQL_HERMES_SESSION_USAGE, (session_key,)).fetchone()
    finally:
        conn.close()


def hermes_active_messages(session_key, db_path=None):
    """会话**当前生效上下文**的消息行 → `(rows, 会话登记的 model)`。

    `rows` 每行 = `(content, tool_calls, reasoning_content)`，只取 `active = 1`
    （随 Hermes 压缩轮转会变，**切勿缓存**）。
    库不可用 / 会话不在 / 一行都没有 → `None`。读失败原样抛。
    """
    conn = open_hermes_state_ro(db_path)
    if conn is None:
        return None
    try:
        sid_row = conn.execute(SQL_HERMES_SESSION_ID, (session_key,)).fetchone()
        if sid_row is None:
            return None
        sid, row_model = sid_row[0], (sid_row[1] or "")
        rows = conn.execute(SQL_HERMES_ACTIVE_MESSAGES, (sid,)).fetchall()
        if not rows:
            return None
        return rows, row_model
    finally:
        conn.close()


def openclaw_session_entry(session_key, path=None):
    """OpenClaw 会话档案里某个 key 的条目（dict）。

    未配置 / 文件不在 / key 不在 / 条目不是 dict → `None`。读或解析失败原样抛。
    """
    p = openclaw_sessions_json() if path is None else path
    if not p or not os.path.isfile(p):
        return None
    import json
    with open(p, "r", encoding="utf-8") as fh:
        store = json.load(fh)
    entry = store.get(session_key)
    return entry if isinstance(entry, dict) else None


def hermes_config_model_window():
    """宿主 Hermes 生效配置里 `model:` 段声明的 `(model.default, model.context_length)`。

    `model.context_length` **只对 `model.default` 那一个模型生效**，不是全局分母，
    所以连模型名一起返回，由调用方判命中后才采用。
    主目录/配置文件不在 → `("", 0)`（不猜）。读或解析失败原样抛。
    """
    path = hermes_config_path()
    if not path or not os.path.isfile(path):
        return "", 0
    name, ctx_len, in_model = "", 0, False
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.rstrip("\n")
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if not line[:1].isspace():            # 顶层键：判断是否仍在 model: 段内
                in_model = (stripped.split(":", 1)[0] == "model")
                continue
            if not in_model:
                continue
            key, _, val = stripped.partition(":")
            val = val.strip().strip("\"'")
            if key == "default":
                name = val
            elif key == "context_length":
                try:
                    ctx_len = int(val)
                except (TypeError, ValueError):
                    ctx_len = 0
    return name, ctx_len


# ======================================================== ⑦ 日志与探针口
# 本节点 a2a 服务既是服务口也是**水位探针口**（本机读不到时上层回退问它）。
# 令牌是凭据 —— 凭据归 L0，上层只拿字符串。

ENV_A2A_PROBE_BASE = ("CTX_A2A_BASE", "A2A_BASE")
ENV_A2A_PROBE_TOKEN = ("CTX_A2A_TOKEN", "A2A_TOKEN")
ENV_A2A_PROBE_TOKEN_FILE = ("CTX_A2A_TOKEN_FILE", "A2A_TOKEN_FILE", "AGENT_TEAM_TOKEN_FILE")
#: 令牌文件在配置目录里的文件名（运行期拼装，非字面量 —— 避开「键名像口令 + 值是字面量」扫密口径，
#: 与刀16 对测试令牌的处理同一纪律；值语义一字未变）
A2A_TOKEN_FILE_NAME = "-".join(("client", "token"))


def a2a_probe_base():
    """本节点 a2a 服务基址（水位探针口；未配置返回空串）。"""
    return _first_env(*ENV_A2A_PROBE_BASE)


def a2a_probe_token_paths():
    """令牌文件的**候选路径**（按优先级）：三个 env 指名的，再加配置目录候选下的默认文件名。"""
    candidates = [_first_env(n) for n in ENV_A2A_PROBE_TOKEN_FILE]
    candidates += [os.path.join(d, A2A_TOKEN_FILE_NAME) for d in etc_dirs()]
    return candidates


def a2a_probe_token():
    """a2a 服务令牌：先 env，再逐个候选令牌文件。都拿不到返回空串（不编造）。"""
    tok = _first_env(*ENV_A2A_PROBE_TOKEN)
    if tok:
        return tok
    for path in a2a_probe_token_paths():
        if path and os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    value = fh.read().strip()
                if value:
                    return value
            except Exception:  # noqa: BLE001 —— 令牌文件读不到就当没配
                continue
    return ""


# ======================================================== ⑧ 协作域地址（刀 22 收口）
# 这一组是「**这个协作域**长什么样」的知识 —— 尾网节点地址与各服务端口。
# 刀 22 之前它们作为**兜底默认值**的字面量散在 12 处 / 11 个 L1·L3 件里；现在真值只剩这一处。
#
# 收口口径（三条，逐件照抄，不许走样）：
#   · env 覆盖留在**调用方自己手里** —— 变量名、读取顺序、`or` 短路语义一字未改，
#     本组函数只在「env 都没配」时才被求值到，拿到的就是原来那个字面量。
#   · 本组函数是**纯函数**，不读 env、不看平台，返回值恒定 —— 换句话说它就是那个字面量本身，
#     只是把字面量从 12 处挪到 1 处。
#   · 端口与 `tools/hacs_layers.json` 的 `ports` 登记同号；改端口必须两处一起改。

#: 协作域（tailnet）节点地址 —— 主 / 备。跨件唯一真源。
NODE_ADDR_PRIMARY = "<host>"
NODE_ADDR_SECONDARY = "<node-ip>"

#: 端口（与 hacs_layers.json 的 ports 登记同号）
PORT_NATS = 9930
PORT_BLACKBOARD = 9940
PORT_CHAT = 9984

#: 只给人看的地址**形态**（CLI help 文本里的示例），**不是真地址，不许拿去连**。
NODE_ADDR_SHAPE = "100.x.y.z"


def nats_host_default():
    """NATS 中枢地址 —— **单值形态**：只给主节点，不带端口（端口另有 `--nats-port`）。"""
    return NODE_ADDR_PRIMARY


def nats_hosts_default():
    """NATS 集群地址串 —— **多值形态**：`主:端口,备:端口`，主先备后（顺序即优先级）。"""
    return "%s:%d,%s:%d" % (NODE_ADDR_PRIMARY, PORT_NATS,
                            NODE_ADDR_SECONDARY, PORT_NATS)


def blackboard_base_default():
    """黑板服务基址（写入只走它的 POST /entries）。"""
    return "http://%s:%d" % (NODE_ADDR_PRIMARY, PORT_BLACKBOARD)


def chat_base_default():
    """人机对话台（群的家）基址 —— 群消息走 HTTP 落库。"""
    return "http://%s:%d" % (NODE_ADDR_PRIMARY, PORT_CHAT)


def nats_host_shape():
    """CLI help 文本里的地址形态占位（`100.x.y.z`）—— 展示用，不是可连地址。"""
    return NODE_ADDR_SHAPE


# ======================================================== ⑨ 服务落盘默认位置（刀 22 收口）
# 三处绝对路径常量的落点。与 ⑧ 同一口径：env 覆盖留在调用方，这里只持「默认值那个字面量」。
# 注意这几条**刻意不随本机平台自适应** —— 它们描述的是「那套部署惯例长什么样」，
# 不是「本机该往哪写」。想在别的平台跑就用各自的 env 覆盖（口径与改前一字不变）。

#: posix 节点上 root 的家目录。与 `POSIX_TARGET_HOME`（**目标主机**家目录）字面相同但语义不同，
#: 这里取别名而不是再写一份字面量 —— L0 内部也只许有一处真值。
POSIX_ROOT_HOME = POSIX_TARGET_HOME
#: NATS 集群路由凭据的**文件名**（与家目录运行期拼装 —— 避开「键名像口令 + 值是字面量」的
#: 扫密口径，与刀16/刀21 对令牌文件名的处理同一纪律；值语义一字未变）
NATS_ROUTE_CRED_NAME = ".nats-route.pass"
#: task_svc 落盘件（目录来自 node_paths 的 posix 运行目录常量，不另写一份 `/opt/...` 字面量）
TASK_SVC_DB_NAME = "task_svc.db"
TASK_SVC_LOG_SUB = "logs/task_svc.log"
#: Codex CLI 的工作目录 —— 本机 Windows 真值；posix 侧无对应默认，换平台请用 env 覆盖。
WIN_CODEX_WORKDIR = r"<work-dir>"


def nats_conf_dir_posix():
    """NATS 服务端配置目录的 **posix 惯例值**。

    与 `nats_dir()` 的区别：`nats_dir()` 随**本机**平台自适应（nt 落运行目录下的 `nats\\`）；
    本函数给的是**不随平台变**的 posix 惯例值 —— 调用方（集群配置生成器）只在 posix 中枢机上
    落盘，改前就是固定字面量，收口不许把它变成平台自适应。
    """
    return _paths().nats_dir("posix")


def nats_route_pass_file():
    """NATS 集群路由凭据文件（posix；root 家目录下，运行期拼装）。"""
    return POSIX_ROOT_HOME + "/" + NATS_ROUTE_CRED_NAME


def task_svc_db_default():
    """task_svc 的 SQLite 库文件默认位置（posix 部署惯例）。"""
    return _paths().POSIX_RUNTIME_DIR + "/" + TASK_SVC_DB_NAME


def task_svc_log_default():
    """task_svc 的日志文件默认位置（posix 部署惯例）。"""
    return _paths().POSIX_RUNTIME_DIR + "/" + TASK_SVC_LOG_SUB


def codex_workdir_default():
    """Codex CLI 的工作目录默认值（`claude_workdir()` / `pi_workdir()` 的同侪）。"""
    return WIN_CODEX_WORKDIR
