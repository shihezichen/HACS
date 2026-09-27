# -*- coding: utf-8 -*-
"""刀 23 基线 —— 「没有守护时，人工算同一份探针 JSON」的**独立第二实现**。

它从哪来、为什么不是 git 切片
------------------------------
刀 23 的 `agent-team/nodelet.py` 是**新增件**：改前树上不存在，`git show <改前 HEAD>:…`
取不到任何字节。契约 §2 对这种情况的规定是「用**独立第二实现**放 `_old/`，并在报告里
说明它从哪来、为什么」。本文件就是那一份。

它是照着 **`docs/l0-nodelet.md` 的契约文字**、而不是照着 `nodelet.py` 的代码写的 ——
等于「刀 23 之前，运维想知道这四问，自己动手会怎么算」：ssh 上去跑 `systemctl is-active`、
`ls` 一下几个路径、`md5sum` 一下那个件，然后按契约把结果拼成 JSON。

为什么这不是自证（三条）
------------------------
  · **不 import nodelet**。本文件对 `agent-team/nodelet.py` 一无所知，
    `import nodelet` 在这里会被装置判死（装置 C 段有源码守卫）。
  · **常量自带一份**。规范单元名、必需档案面清单、顶层键序、状态词表，本文件全部
    **自己写一遍字面量**。于是 `nodelet.py` 里那些「冻结」的东西一旦漂了，两侧立刻对不上 ——
    这正是我们要的敏感度。
  · **算法刻意走另一条路**。`os.stat`+`stat.S_ISDIR` 而非 `os.path.isdir`；
    `datetime.fromtimestamp(tz=utc)` 而非 `time.gmtime`；手写字符串切分而非 `csv` 模块；
    `OrderedDict` 显式建序而非靠 dict 插入序。同样的答案，两条不同的路走出来。

两侧**共享**的只有一样东西：L0 节点档案 `node_profile`（档案本来就是唯一真源，
「两侧各读一份档案」才是这个装置要证的东西 —— 证的是**推导**没走样，不是档案本身）。
"""

import collections
import datetime
import hashlib
import json
import os
import stat

#: —— 以下常量全部**本文件自己持有一份**，故意不从 nodelet 取（见模块文档串第二条）。

#: 探针 JSON 的顶层键与顺序（照 docs/l0-nodelet.md 抄）
TOP_KEYS = ("node", "profile", "facts", "version", "uptime_s", "ts")
#: facts 的键与顺序
FACT_KEYS = ("runtime_dir", "log_dir", "hermes_state_db", "openclaw_sessions",
             "a2a_probe_token", "services")
#: 必需档案面（顺序即 profile.missing 的顺序）
FACES = ("node_name", "flavour", "home", "runtime_dir", "etc_dirs", "log_dir",
         "target_known", "target_flavour", "target_home", "target_claude_exec",
         "hermes_state_db", "openclaw_sessions_json", "a2a_probe_token")
#: 本机查不到档案时补的那条
FACE_TARGET = "target_profile"

#: 规范单元名
UNIT_POSIX = "agent-team-nodelet"
UNIT_NT = "AgentTeamNodelet"

#: env 名
ENV_SERVICES = "AGENT_TEAM_NODELET_SERVICES"

#: systemd 认得的状态词
SYSTEMD_WORDS = ["active", "activating", "deactivating", "inactive", "failed", "reloading"]
#: 计划任务的状态词
NT_RUNNING = ["running", "正在运行"]
NT_KNOWN = NT_RUNNING + ["ready", "disabled", "queued", "就绪", "已禁用", "已排队"]

#: 时间格式
TS_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _is_dir(path):
    """目录？走 `os.stat` + `S_ISDIR`（刻意不用 `os.path.isdir`）。"""
    if not path:
        return False
    try:
        return stat.S_ISDIR(os.stat(path).st_mode)
    except OSError:
        return False


def _is_file(path):
    """普通文件？同上，走 `S_ISREG`。"""
    if not path:
        return False
    try:
        return stat.S_ISREG(os.stat(path).st_mode)
    except OSError:
        return False


def _dir_fact(path):
    text = str(path or "")
    here = _is_dir(text)
    writable = False
    if here:
        writable = os.access(text, os.W_OK)
    return collections.OrderedDict([("exists", here), ("writable", bool(writable))])


def _file_fact(path):
    text = str(path or "")
    return collections.OrderedDict([("configured", len(text) > 0),
                                    ("exists", _is_file(text))])


def _cred_fact(value):
    """凭据面：只有「有没有」与「多长」。值一个字符都不出门。"""
    text = "" if value is None else "%s" % value
    return collections.OrderedDict([("present", len(text) > 0), ("len", len(text))])


def _services(flavour, env_value):
    """声明单元名列表（去重保序）—— 手写扫一遍，不用 nodelet 的实现。"""
    raw = env_value if env_value is not None else os.environ.get(ENV_SERVICES, "")
    out = []
    for piece in ("%s" % raw).split(","):
        name = piece.strip()
        if len(name) > 0 and name not in out:
            out.append(name)
    if len(out) == 0:
        out = [UNIT_NT if flavour == "nt" else UNIT_POSIX]
    return out


def _argv_for(name, flavour):
    """查一个单元状态的命令行（照契约写，与 nodelet 各写各的）。"""
    if flavour == "nt":
        return ["schtasks", "/Query", "/TN", "%s" % name, "/FO", "CSV", "/NH"]
    return ["systemctl", "is-active", "%s" % name]


def _last_csv_field(text):
    """CSV 最后一行最后一列 —— 手写切分，刻意不用 `csv` 模块。

    够用即可：`schtasks /FO CSV /NH` 的状态列里不会出现逗号（任务名与时间列也不会）。
    """
    rows = [ln for ln in ("%s" % (text or "")).splitlines() if ln.strip()]
    if not rows:
        return ""
    field = rows[-1].strip().rsplit(",", 1)[-1].strip()
    if len(field) >= 2 and field[0] == '"' and field[-1] == '"':
        field = field[1:-1]
    return field.strip()


def _unit_state(name, flavour, run):
    """→ (known, active)。查不出来一律 (False, False)，不编造。"""
    rc, out = run(_argv_for(name, flavour))
    if rc is None:
        return False, False
    if flavour == "nt":
        if rc != 0:
            return False, False
        word = _last_csv_field(out).lower()
        return (word in NT_KNOWN), (word in NT_RUNNING)
    word = ("%s" % (out or "")).strip().lower()
    return (word in SYSTEMD_WORDS), (word == "active")


def _fingerprint(path):
    """磁盘上那一份件的 md5 前 8 位。读不到 → 空串。"""
    if not path:
        return ""
    try:
        fh = open(path, "rb")
    except OSError:
        return ""
    try:
        return hashlib.md5(fh.read()).hexdigest()[0:8]
    finally:
        fh.close()


def _faces(archive):
    """按 FACES 顺序做「面在不在」的体检，返回 (missing 列表, 取值函数)。"""
    missing = []
    for name in FACES:
        fn = getattr(archive, name, None)
        if not callable(fn):
            missing.append(name)

    def value_of(name, *args):
        if name in missing:
            return None
        try:
            return getattr(archive, name)(*args)
        except Exception:                      # noqa: BLE001 —— 面炸了也算「档案不可用」
            if name not in missing:
                missing.append(name)
            return None
    return missing, value_of


def probe_payload(archive, started_at=None, self_path=None, now=None, run=None,
                  flavour_value=None, services_env=None):
    """按契约算一份探针载荷（`OrderedDict`，键序显式写死）。"""
    missing, value_of = _faces(archive)

    node = value_of("node_name") or ""
    flavour = value_of("flavour", flavour_value)
    if len(node) > 0 and FACE_TARGET not in missing:
        if not value_of("target_known", node):
            missing.append(FACE_TARGET)

    svc_rows = []
    for unit in _services(flavour, services_env):
        known, active = _unit_state(unit, flavour, run)
        svc_rows.append(collections.OrderedDict([("name", unit), ("known", known),
                                                 ("active", active)]))

    facts = collections.OrderedDict([
        ("runtime_dir", _dir_fact(value_of("runtime_dir", flavour_value))),
        ("log_dir", _dir_fact(value_of("log_dir", flavour_value))),
        ("hermes_state_db", _file_fact(value_of("hermes_state_db"))),
        ("openclaw_sessions", _file_fact(value_of("openclaw_sessions_json"))),
        ("a2a_probe_token", _cred_fact(value_of("a2a_probe_token"))),
        ("services", svc_rows),
    ])
    assert tuple(facts.keys()) == FACT_KEYS

    moment = (datetime.datetime.now(datetime.timezone.utc).timestamp()
              if now is None else float(now))
    when = datetime.datetime.fromtimestamp(moment, datetime.timezone.utc)
    profile = collections.OrderedDict([
        ("file", os.path.basename(getattr(archive, "__file__", "") or "")),
        ("ok", len(missing) == 0),
        ("missing", list(missing)),
    ])
    payload = collections.OrderedDict([
        ("node", node),
        ("profile", profile),
        ("facts", facts),
        ("version", _fingerprint(self_path)),
        ("uptime_s", 0.0 if started_at is None else round(moment - float(started_at), 3)),
        ("ts", when.strftime(TS_FMT)),
    ])
    assert tuple(payload.keys()) == TOP_KEYS
    return payload


def probe_bytes(**kwargs):
    """人工算法这一侧「该发出去的那串字节」。"""
    return json.dumps(probe_payload(**kwargs), ensure_ascii=False).encode("utf-8")
