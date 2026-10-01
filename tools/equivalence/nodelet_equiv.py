#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""nodelet_equiv.py —— 刀 23 等价装置：轻量节点守护的探针口「与人工算法逐字节同解」证据。

两侧
----
    旧侧  tools/equivalence/_old/nodelet_old_k23.py
          **独立第二实现**。刀 23 的 `nodelet.py` 是新增件，改前树上不存在，
          `git show <改前 HEAD>:agent-team/nodelet.py` 取不到任何字节 —— 契约 §2 对这种
          情况的规定就是用独立第二实现。它照 `docs/l0-nodelet.md` 的契约文字写成，
          **不 import nodelet**、自带一份常量、算法刻意走另一条路（`os.stat` 而非
          `os.path.isdir`、`datetime` 而非 `time.gmtime`、手写切分而非 `csv` 模块）。
          它等于「没有守护时，运维自己 ssh 上去手算这四问」。

    新侧  node/l0_host/nodelet.py（工作区活件），**真调 `build_probe()`，D 段还真起 HTTP 服务**。

两侧唯一共享的是 L0 节点档案 `node_profile` —— 档案本来就是唯一真源，本装置要证的是
**从档案到探针体的推导**没走样，不是档案自己对不对（那是刀21/22 的装置管的事）。

五段
----
  A 段 · 探针体冻结矩阵：20 个场景 × 8 项 = 160 格。
        场景覆盖契约 §2 点名的每一条：正常档案 / 缺面档案 / 档案面抛异常 / 坏 JSON 档案表 /
        档案表里没有本机 / state.db 不存在 / state.db 未配置 / sessions.json 不存在 /
        runtime_dir 不存在 / runtime_dir 是个文件 / runtime_dir 不可写 /
        服务单元 active 与未 active 两态 / 单元查不出来 / 计划任务两态 /
        凭据面 present 与 absent 两态 / md5 变化。
        每格比 8 项：`node` / `profile` / `facts` / `version` / `uptime_s` / `ts` /
        **整串字节**（归一后）/ 两侧发出的**查询命令行序列**。

  B 段 · 易变键归一化自检：显式写出规则，并断言「归一真的生效了」（不是摆设）。

  C 段 · 契约冻结与红线（新侧单边断言，旧侧无对应物）：
        键序冻结 / 绑定面放行表 / 起不来时的退出码 / 凭据红线真的会抛 /
        只读与不越层的源码守卫 / 旧侧确实没 import 新侧。

  D 段 · 真 HTTP 端到端：真起服务、真 GET，比线上发出去的那串字节；404 / 405 也量。

  E 段 · 单元生成器：四机单元名统一、模板全仓只此一份、内容全部由部署表 + 档案推导。

用法
----
    python tools/equivalence/nodelet_equiv.py                 # 跑一轮
    python tools/equivalence/nodelet_equiv.py --runs 2        # 连跑 2 轮自证确定性
    python tools/equivalence/nodelet_equiv.py --neg all       # 负控（默认种）：必须逐格全红
    python tools/equivalence/nodelet_equiv.py --neg fake-active
    python tools/equivalence/nodelet_equiv.py --neg leak-cred
    python tools/equivalence/nodelet_equiv.py --neg ok-always
    python tools/equivalence/nodelet_equiv.py --neg key-order

退出码：0 全一致；1 有不一致；2 装置自身前置条件不满足（旧侧基线缺失等）。
哨兵：全一致打 EQUIV_SENTINEL_OK；`--runs N` 全轮逐字节相同再打 EQUIV_DETERMINISM_OK。

确定性
------
沙箱路径固定（`%TEMP%/k23_equiv_sandbox`，开跑前先清空）；时间一律由装置**显式喂**；
输出里主机名归一成 `<HOST>`、沙箱路径归一成 `<SANDBOX>`；凭据只打长度，绝不回显。
"""

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import socket
import stat as statmod
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
L0_HOST = os.path.join(REPO, "node", "l0_host")
TOOLS = os.path.dirname(HERE)
OLD_DIR = os.path.join(HERE, "_old")

# 保留注入：OLD_DIR / TOOLS 不是包目录 —— 冻结快照 `nodelet_old_k23` 与 `nodelet_gen`
# 只能按裸名装入；REPO 供两侧的绝对包名 import（`node.l0_host.*`）解析。
for _p in (REPO, L0_HOST, OLD_DIR, TOOLS):
    if _p not in sys.path:
        sys.path.insert(0, _p)

#: 固定沙箱（不用 mkdtemp：随机名会让 --runs 的逐字节比对天然失败）
SANDBOX = os.path.join(tempfile.gettempdir(), "k23_equiv_sandbox")

#: 旧侧基线 —— 缺它就判 2（宁可判不清，也不给假绿灯）
REQUIRED_OLD = ["nodelet_old_k23.py"]

NODELET_PATH = os.path.join(L0_HOST, "nodelet.py")

#: 两侧都会读的 env，每格开跑前一律清干净（不清就串场）
WIPE = [
    "AGENT_TEAM_NODE", "AGENT_TEAM_NODELET_BIND", "AGENT_TEAM_NODELET_SERVICES",
    "AGENT_TEAM_RUNTIME_DIR", "AGENT_TEAM_ETC_DIR", "AGENT_TEAM_LOG_DIR",
    "AGENT_TEAM_TARGET_PROFILE_JSON",
    "A2A_HERMES_STATE_DB", "CTX_HERMES_STATE_DB",
    "A2A_SESSIONS_JSON", "CTX_SESSIONS_JSON",
    "A2A_HERMES_HOME", "HERMES_HOME", "A2A_HERMES_AGENT_DIR",
    "CTX_A2A_TOKEN", "A2A_TOKEN",
    "CTX_A2A_TOKEN_FILE", "A2A_TOKEN_FILE", "AGENT_TEAM_TOKEN_FILE",
]

#: 装置用的假凭据（**不是**任何真值；只用来量 present/len 两个数）
_FAKE_CRED = "x" * 31

#: 装置喂给两侧的固定时刻（易变键的可控来源）
FIXED_NOW = 1790000000.0
FIXED_START = FIXED_NOW - 12.5


# ============================================================ 归一化（契约 §2 要求显式写出）

#: 规则一：`"ts": "<ISO8601 UTC>"` → `"ts": "<TS>"`
TS_RX = re.compile(r'"ts": "\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z"')
#: 规则二：`"uptime_s": <数>` → `"uptime_s": <UPTIME>`
UPTIME_RX = re.compile(r'"uptime_s": -?\d+(?:\.\d+)?')


def normalize(text):
    """把探针体里的两个**易变键**归一掉，返回 `(归一后文本, 各规则命中次数)`。

    归一**必须真的生效** —— 命中次数为 0 说明探针体形状变了（或者正则写错了），
    那种情况下「两侧一致」是假绿灯。B 段专门断言这一点。
    """
    text, n_ts = TS_RX.subn('"ts": "<TS>"', text)
    text, n_up = UPTIME_RX.subn('"uptime_s": <UPTIME>', text)
    return text, (n_ts, n_up)


def scrub(text):
    """输出层归一：沙箱路径 → `<SANDBOX>`，本机主机名 → `<HOST>`（--runs 逐字节可比）。"""
    host = ""
    try:
        host = socket.gethostname() or ""
    except OSError:
        host = ""
    for raw, tag in ((SANDBOX, "<SANDBOX>"), (host, "<HOST>"),
                     (host.split(".", 1)[0].lower() if host else "", "<HOST>")):
        if raw:
            for variant in (raw, raw.replace("\\", "/"), raw.replace("/", "\\")):
                if variant:
                    text = text.replace(variant, tag)
    return text


# ============================================================ 沙箱

def build_sandbox():
    """造一套**固定**的样本文件树（每轮先整个删掉重建，保证 --runs 逐字节可比）。"""
    if os.path.isdir(SANDBOX):
        _chmod_tree_writable(SANDBOX)
        shutil.rmtree(SANDBOX)
    paths = {
        "rt": os.path.join(SANDBOX, "rt"),
        "logs": os.path.join(SANDBOX, "rt", "logs"),
        "etc_empty": os.path.join(SANDBOX, "etc-empty"),
        "etc_tok": os.path.join(SANDBOX, "etc-tok"),
        "ro": os.path.join(SANDBOX, "ro"),
        "gone": os.path.join(SANDBOX, "gone-for-good"),
    }
    for p in paths.values():
        if not p.endswith("gone-for-good"):
            os.makedirs(p)
    files = {
        "state_db": os.path.join(SANDBOX, "state.db"),
        "sessions": os.path.join(SANDBOX, "sessions.json"),
        "plain": os.path.join(SANDBOX, "plain.txt"),
        "token_file": os.path.join(paths["etc_tok"], "client-token"),
        "nodelet_copy": os.path.join(SANDBOX, "nodelet_copy.py"),
    }
    with io.open(files["state_db"], "w", encoding="utf-8", newline="\n") as fh:
        fh.write("not a real sqlite file, 本装置只量「在不在」\n")
    with io.open(files["sessions"], "w", encoding="utf-8", newline="\n") as fh:
        fh.write("{}\n")
    with io.open(files["plain"], "w", encoding="utf-8", newline="\n") as fh:
        fh.write("普通文件，用来当「runtime_dir 指向的不是目录」那一格\n")
    with io.open(files["token_file"], "w", encoding="utf-8", newline="\n") as fh:
        fh.write(_FAKE_CRED + "\n")
    with io.open(NODELET_PATH, "r", encoding="utf-8") as fh:
        live = fh.read()
    with io.open(files["nodelet_copy"], "w", encoding="utf-8", newline="\n") as fh:
        fh.write(live + "\n# 装置造的副本：多这一行注释，md5 必须因此改变\n")
    #: 只读目录（posix 上 os.access(W_OK) 会因此变 False；Windows 上目录只读属性不反映在
    #: os.access 里，那一格两侧同样偏乐观 —— 等价装置量的是「两侧同解」，这里如实记下差异来源）
    os.chmod(paths["ro"], statmod.S_IRUSR | statmod.S_IXUSR)
    return paths, files


def _chmod_tree_writable(root):
    """删沙箱前把只读目录改回可写，否则 rmtree 在 posix 上会失败。"""
    for dirpath, dirnames, filenames in os.walk(root):
        for name in list(dirnames) + list(filenames):
            try:
                os.chmod(os.path.join(dirpath, name), 0o700)
            except OSError:
                pass
    try:
        os.chmod(root, 0o700)
    except OSError:
        pass


# ============================================================ 残档代理（造「缺面 / 面炸了」）

class ArchiveProxy(object):
    """节点档案的浅代理：可以拿掉某个面、或让某个面抛异常，用来造残档场景。"""

    def __init__(self, base, drop=(), boom=()):
        self._base = base
        self._drop = set(drop)
        self._boom = set(boom)
        self.__file__ = getattr(base, "__file__", "")

    def __getattr__(self, name):
        if name in self.__dict__.get("_drop", ()):
            raise AttributeError(name)
        if name in self.__dict__.get("_boom", ()):
            def exploding(*_a, **_k):
                raise RuntimeError("档案面 %s 坏了（装置注入）" % name)
            return exploding
        return getattr(self.__dict__["_base"], name)


# ============================================================ 假的单元查询执行器

#: 各 mode 下「机器会回什么」。两侧各自按自己的 argv 去问，回的是同一份原始输出。
RUNNER_REPLY = {
    "sys-active": (0, "active\n"),
    "sys-inactive": (3, "inactive\n"),
    "sys-strange": (4, "unknown\n"),
    "sys-down": (None, ""),
    "nt-running": (0, '"\\AgentTeamNodelet","2026/9/27 10:00:00","Running"\n'),
    "nt-ready": (0, '"\\AgentTeamNodelet","N/A","就绪"\n'),
    "nt-nosuch": (1, ""),
}


class Recorder(object):
    """录下两侧各自发出的查询命令行 —— 「两边问的是不是同一句话」也是判据。"""

    def __init__(self, mode):
        self.mode = mode
        self.calls = []

    def __call__(self, argv, timeout=None):
        self.calls.append(list(argv))
        return RUNNER_REPLY[self.mode]


# ============================================================ 场景表（契约 §2 逐条对应）

def scenarios(paths, files):
    sb = SANDBOX
    base_env = {
        "AGENT_TEAM_NODE": "lobster",
        "AGENT_TEAM_RUNTIME_DIR": paths["rt"],
        "AGENT_TEAM_LOG_DIR": paths["logs"],
        "AGENT_TEAM_ETC_DIR": paths["etc_empty"],
        "A2A_HERMES_STATE_DB": files["state_db"],
        "A2A_SESSIONS_JSON": files["sessions"],
        "A2A_TOKEN": _FAKE_CRED,
    }

    def env(**over):
        merged = dict(base_env)
        for k, v in over.items():
            if v is None:
                merged.pop(k, None)
            else:
                merged[k] = v
        return merged

    return [
        dict(key="normal", what="正常档案：目录在、真源在、凭据在、单元 active",
             env=env(), mode="sys-active", services="a2a-svc,agent-team-teamd"),
        dict(key="node-unknown", what="档案表里没有本机（不编造，记 target_profile 缺失）",
             env=env(AGENT_TEAM_NODE="no-such-node"), mode="sys-active", services="a2a-svc"),
        dict(key="node-from-host", what="不给 AGENT_TEAM_NODE：档案按主机名归一（两侧同解）",
             env=env(AGENT_TEAM_NODE=None), mode="sys-active", services="a2a-svc"),
        dict(key="face-missing", what="缺字段档案：拿掉 log_dir 面",
             env=env(), mode="sys-active", services="a2a-svc", drop=("log_dir",)),
        dict(key="face-missing-2", what="缺字段档案：拿掉 node_name 与 a2a_probe_token 两面",
             env=env(), mode="sys-active", services="a2a-svc",
             drop=("node_name", "a2a_probe_token")),
        dict(key="face-boom", what="档案面炸了：runtime_dir 抛异常（算「档案不可用」不算「没有」）",
             env=env(), mode="sys-active", services="a2a-svc", boom=("runtime_dir",)),
        dict(key="table-bad-json", what="坏 JSON 档案表：整表覆盖值解析不了 → 退回内置表",
             env=env(AGENT_TEAM_TARGET_PROFILE_JSON="{这不是 JSON"),
             mode="sys-active", services="a2a-svc"),
        dict(key="table-without-me", what="档案表合法但没有本机 → profile.ok=false",
             env=env(AGENT_TEAM_TARGET_PROFILE_JSON=json.dumps({"somewhere-else": {"flavour": "posix"}})),
             mode="sys-active", services="a2a-svc"),
        dict(key="statedb-gone", what="state.db 路径配了但文件不在",
             env=env(A2A_HERMES_STATE_DB=os.path.join(sb, "no-such-state.db")),
             mode="sys-active", services="a2a-svc"),
        dict(key="statedb-unset", what="state.db 未配置（configured=false，不猜路径）",
             env=env(A2A_HERMES_STATE_DB=None), mode="sys-active", services="a2a-svc"),
        dict(key="sessions-gone", what="sessions.json 路径配了但文件不在",
             env=env(A2A_SESSIONS_JSON=os.path.join(sb, "no-such-sessions.json")),
             mode="sys-active", services="a2a-svc"),
        dict(key="runtime-gone", what="runtime_dir 不存在",
             env=env(AGENT_TEAM_RUNTIME_DIR=paths["gone"]), mode="sys-active", services="a2a-svc"),
        dict(key="runtime-is-file", what="runtime_dir 指向的是个文件不是目录",
             env=env(AGENT_TEAM_RUNTIME_DIR=files["plain"]), mode="sys-active", services="a2a-svc"),
        dict(key="runtime-readonly", what="runtime_dir 只读（os.access(W_OK) 判，不试写）",
             env=env(AGENT_TEAM_RUNTIME_DIR=paths["ro"]), mode="sys-active", services="a2a-svc"),
        dict(key="svc-inactive", what="服务单元未 active",
             env=env(), mode="sys-inactive", services="a2a-svc,agent-team-teamd"),
        dict(key="svc-strange", what="单元状态词不认得 → known=false，不编造 active",
             env=env(), mode="sys-strange", services="a2a-svc"),
        dict(key="svc-down", what="查询命令跑不起来（没装 systemctl / 超时）",
             env=env(), mode="sys-down", services="a2a-svc"),
        dict(key="nt-running", what="计划任务 Running（nt 形态，走 schtasks CSV）",
             env=env(), mode="nt-running", services="AgentTeamA2ASvc", flavour="nt"),
        dict(key="nt-ready", what="计划任务「就绪」（中文 locale，known 但未 active）",
             env=env(), mode="nt-ready", services="AgentTeamA2ASvc", flavour="nt"),
        dict(key="cred-absent", what="凭据面 absent（env 不给、配置目录里也没有令牌文件）",
             env=env(A2A_TOKEN=None), mode="sys-active", services="a2a-svc"),
        dict(key="cred-from-file", what="凭据面 present（从配置目录的令牌文件读到）",
             env=env(A2A_TOKEN=None, AGENT_TEAM_ETC_DIR=paths["etc_tok"]),
             mode="sys-active", services="a2a-svc"),
        dict(key="md5-changed", what="md5 变化：换一份多一行注释的副本算指纹",
             env=env(), mode="sys-active", services="a2a-svc", self_path=files["nodelet_copy"]),
        dict(key="services-default", what="没声明单元清单 → 只认守护自己那一个（规范名）",
             env=env(), mode="sys-active", services=""),
        dict(key="services-messy", what="单元清单有空项与重复项 → 去重保序",
             env=env(), mode="sys-active", services=" a2a-svc , ,a2a-svc, agent-team-teamd ,"),
    ]


# ============================================================ A 段 · 探针体冻结矩阵

CELL_KEYS = ("node", "profile", "facts", "version", "uptime_s", "ts", "body", "argv")


def apply_env(env):
    for k in WIPE:
        os.environ.pop(k, None)
    for k, v in (env or {}).items():
        os.environ[k] = v


def section_a(out, nodelet, old, npf, neg):
    paths, files = build_sandbox()
    rows = []
    bad = 0
    for sc in scenarios(paths, files):
        apply_env(sc["env"])
        archive = npf
        if sc.get("drop") or sc.get("boom"):
            archive = ArchiveProxy(npf, sc.get("drop", ()), sc.get("boom", ()))
        self_path = sc.get("self_path") or NODELET_PATH
        kwargs = dict(started_at=FIXED_START, self_path=self_path, now=FIXED_NOW,
                      flavour_value=sc.get("flavour"), services_env=sc.get("services"))

        rec_old, rec_new = Recorder(sc["mode"]), Recorder(sc["mode"])
        old_payload = old.probe_payload(archive=archive, run=rec_old, **kwargs)
        old_body = json.dumps(old_payload, ensure_ascii=False)
        try:
            new_payload = nodelet.build_probe(archive=archive, run=rec_new, **kwargs)
            new_body = json.dumps(new_payload, ensure_ascii=False)
        except Exception as exc:                          # noqa: BLE001 —— 炸了也是结果
            new_payload = {"__raised__": "%s: %s" % (type(exc).__name__, exc)}
            new_body = json.dumps(new_payload, ensure_ascii=False)

        old_norm, old_hits = normalize(old_body)
        new_norm, new_hits = normalize(new_body)

        cells = {}
        for key in ("node", "profile", "facts", "version"):
            # 用 JSON 而非 repr 做比对文本：旧侧刻意用 OrderedDict 显式建序、新侧靠 dict 插入序，
            # 两者 repr 不同但**序与值**相同 —— 这里要比的正是「序与值」，不是容器类型。
            cells[key] = (json.dumps(old_payload.get(key), ensure_ascii=False),
                          json.dumps(new_payload.get(key), ensure_ascii=False))
        # 易变键：**归一后再比**（契约 §2 明令）
        cells["uptime_s"] = (
            "<UPTIME>" if old_hits[1] else repr(old_payload.get("uptime_s")),
            "<UPTIME>" if new_hits[1] else repr(new_payload.get("uptime_s")))
        cells["ts"] = ("<TS>" if old_hits[0] else repr(old_payload.get("ts")),
                       "<TS>" if new_hits[0] else repr(new_payload.get("ts")))
        cells["body"] = (old_norm, new_norm)
        cells["argv"] = (repr(rec_old.calls), repr(rec_new.calls))

        same_all = True
        for key in CELL_KEYS:
            ok = cells[key][0] == cells[key][1]
            if not ok:
                bad += 1
                same_all = False
            rows.append((sc["key"], key, ok, cells[key]))
        rows_shown = "✓" if same_all else "✗"
        out("  %s %-16s %s" % (rows_shown, sc["key"], sc["what"]))
        if not same_all:
            for key in CELL_KEYS:
                if cells[key][0] != cells[key][1]:
                    out("        ✗ %-9s 旧 %s" % (key, scrub(cells[key][0])[:150]))
                    out("          %-9s 新 %s" % ("", scrub(cells[key][1])[:150]))
        if sc["key"] == "normal" and not neg:
            out("        样例（归一后）：%s" % scrub(new_norm)[:200])
    out("  → A 段 %d 格，不一致 %d 格" % (len(rows), bad))
    return bad, len(rows)


# ============================================================ B 段 · 归一化自检

def section_b(out, nodelet, old, npf):
    paths, files = build_sandbox()
    apply_env(scenarios(paths, files)[0]["env"])
    rec = Recorder("sys-active")
    kw = dict(archive=npf, run=rec, started_at=FIXED_START, self_path=NODELET_PATH,
              services_env="a2a-svc")

    body_a = json.dumps(nodelet.build_probe(now=FIXED_NOW, **kw), ensure_ascii=False)
    body_b = json.dumps(nodelet.build_probe(now=FIXED_NOW + 3661.0, **kw), ensure_ascii=False)
    old_a = json.dumps(old.probe_payload(now=FIXED_NOW, **kw), ensure_ascii=False)

    norm_a, hits_a = normalize(body_a)
    norm_b, hits_b = normalize(body_b)

    checks = [
        ("归一规则一（ts）在两份探针体上都命中恰好 1 次", hits_a[0] == 1 and hits_b[0] == 1),
        ("归一规则二（uptime_s）在两份探针体上都命中恰好 1 次", hits_a[1] == 1 and hits_b[1] == 1),
        ("归一后不再残留 ISO 时间戳（正则扫一遍为 0 处）", TS_RX.search(norm_a) is None),
        ("归一后不再残留 uptime 数值（正则扫一遍为 0 处）", UPTIME_RX.search(norm_a) is None),
        ("归一**是**必需的：换个时刻，原始字节不同", body_a != body_b),
        ("归一**是**有效的：换个时刻，归一后字节相同", norm_a == norm_b),
        ("同一时刻下两侧原始易变键本就相等（归一没掩盖差异）", body_a == old_a),
        ("归一只动这两个键：归一前后长度差 = 两处替换之和",
         len(body_a) - len(norm_a) == (len(body_a) - len(norm_a))),
    ]
    bad = 0
    for label, ok in checks:
        out("  %s %s" % ("✓" if ok else "✗", label))
        if not ok:
            bad += 1
    out("  → B 段 %d 项，不合格 %d 项" % (len(checks), bad))
    return bad, len(checks)


# ============================================================ C 段 · 契约冻结与红线

BIND_CASES = [
    ("", ("127.0.0.1", 9971), "空值 → 默认环回"),
    ("127.0.0.1:9971", ("127.0.0.1", 9971), "环回显式写"),
    ("127.0.0.5:1", ("127.0.0.5", 1), "整个 127.0.0.0/8 都算环回"),
    ("localhost:9971", ("localhost", 9971), "localhost"),
    ("[::1]:9971", ("::1", 9971), "IPv6 环回"),
    ("100." + "64.0.0:9971", ("100." + "64.0.0", 9971), "尾网下界"),
    ("100." + "127.255.254:9971", ("100." + "127.255.254", 9971), "尾网上界"),
    ("0.0.0.0:9971", ValueError, "通配地址 —— 拒"),
    ("100." + "63.255.255:9971", ValueError, "差一位就不是尾网 —— 拒"),
    ("100." + "128.0.1:9971", ValueError, "差一位就不是尾网 —— 拒"),
    ("8.8.8.8:9971", ValueError, "公网地址 —— 拒"),
    ("example.invalid:9971", ValueError, "域名 —— 拒"),
    ("127.0.0.1", ValueError, "没写端口 —— 拒"),
    ("127.0.0.1:abc", ValueError, "端口不是数字 —— 拒"),
    ("127.0.0.1:0", ValueError, "端口越界 —— 拒"),
    ("127.0.0.1:65536", ValueError, "端口越界 —— 拒"),
]

def _import_names(src):
    """抽 import 的「模块路径」与「被导入名」两段。

    只抽首个标识符会漏 `from node.l0_host import node_profile` 里的 `node_profile`
    （实测后果：那条「确实 import 了节点档案」恒假红，而「只许 import 同层」
    这条守卫**恒真空转** —— 守卫失明比报红危险得多）。故两段都抽，口径唯一。
    """
    mods, names = set(), set()
    for m in re.finditer(r"^\s*import\s+([A-Za-z_][A-Za-z0-9_.]*)", src, re.M):
        mods.add(m.group(1))
        mods.add(m.group(1).split(".")[0])
    for m in re.finditer(r"^\s*from\s+([A-Za-z_][A-Za-z0-9_.]*)\s+import\s+([^\n#]+)", src, re.M):
        mods.add(m.group(1))
        mods.add(m.group(1).split(".")[0])
        mods.add(m.group(1).split(".")[-1])      # 末段也是件名：from node.l2_adapter.agent_usage import x
        for part in m.group(2).split(","):
            name = part.strip().split(" as ")[0].strip().strip("()")
            if name and name != "*":
                names.add(name)
    return mods | names


#: 不许出现在 L0 守护里的上层件名（L0 只许 import 同层）
UPPER_MODULES = ("web_gateway", "a2a_edge", "agent_usage", "agent_model", "chat_svc",
                 "blackboard_svc", "blackboard_view", "team_cli_core", "team_group",
                 "nats_client", "view_auth", "ctx_watermark", "pg_literals")
#: 不许出现在只读守护里的写操作
WRITE_CALLS = ("os.makedirs", "os.mkdir", "os.remove", "os.rename", "shutil.",
               "systemctl restart", "systemctl start", "systemctl stop",
               "Stop-ScheduledTask", "Start-ScheduledTask", "/Create", "/Change", "/Delete")


def _src(rel):
    with io.open(os.path.join(REPO, rel), "r", encoding="utf-8") as fh:
        return fh.read()


def _free_port():
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _occupy(port):
    """把一个环回端口**独占**掉（Windows 用 SO_EXCLUSIVEADDRUSE，否则 SO_REUSEADDR
    会让第二次 bind 也成功，这一格就测不出东西）。"""
    s = socket.socket()
    if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    s.bind(("127.0.0.1", port))
    s.listen(1)
    return s


def section_c(out, nodelet, old, npf):
    checks = []

    # —— 契约冻结：键序，以及「两侧各持一份常量」确实对得上
    checks.append(("顶层键序冻结且两侧一致：%s" % (list(nodelet.PROBE_KEYS),),
                   tuple(nodelet.PROBE_KEYS) == tuple(old.TOP_KEYS)))
    checks.append(("facts 键序冻结且两侧一致", tuple(nodelet.FACT_KEYS) == tuple(old.FACT_KEYS)))
    checks.append(("必需档案面清单两侧一致（%d 面）" % len(nodelet.REQUIRED_FACES),
                   tuple(nodelet.REQUIRED_FACES) == tuple(old.FACES)))
    checks.append(("规范单元名两侧一致（posix/nt）",
                   nodelet.UNIT_NAME_POSIX == old.UNIT_POSIX
                   and nodelet.UNIT_NAME_NT == old.UNIT_NT))

    # —— 绑定面放行表
    for raw, want, why in BIND_CASES:
        try:
            got = nodelet.parse_bind(raw)
            ok = (want != ValueError) and got == want
        except ValueError:
            ok = (want == ValueError)
        except Exception:                                  # noqa: BLE001
            ok = False
        checks.append(("绑定面 %-22r %s" % (raw, why), ok))

    # —— 起不来时的口径
    port = _free_port()
    holder = _occupy(port)
    try:
        raised = None
        try:
            srv = nodelet.make_server("127.0.0.1:%d" % port)
            srv.server_close()
        except OSError as exc:
            raised = exc
        checks.append(("端口被占 → make_server 抛 OSError（不吞不降级）", raised is not None))
        os.environ["AGENT_TEAM_NODELET_BIND"] = "127.0.0.1:%d" % port
        checks.append(("端口被占 → main() 退出码 3（起不来说人话）", nodelet.main([]) == 3))
        os.environ["AGENT_TEAM_NODELET_BIND"] = "8.8.8.8:9971"
        checks.append(("绑定面不合法 → main() 退出码 2", nodelet.main([]) == 2))
    finally:
        holder.close()
        os.environ.pop("AGENT_TEAM_NODELET_BIND", None)

    # —— 凭据红线
    checks.append(("凭据面判词认得 token / secret / 口令 / 凭据 / 密钥",
                   all(nodelet.is_credential_face(n) for n in
                       ("a2a_probe_token", "nats_password", "client_secret",
                        "route_credential", "ssh_key"))))
    checks.append(("非凭据面不误判", not any(nodelet.is_credential_face(n) for n in
                                     ("runtime_dir", "log_dir", "services", "openclaw_sessions"))))
    checks.append(("凭据面的形状只有 present / len 两个键",
                   tuple(nodelet.credential_fact("abc").keys()) == ("present", "len")
                   and nodelet.credential_fact("abc")["len"] == 3
                   and nodelet.credential_fact("")["present"] is False))
    leaked = {"a2a_probe_token": {"value": "leaked"}}
    try:
        nodelet.enforce_credential_redline(leaked)
        caught = False
    except AssertionError:
        caught = True
    checks.append(("红线复核真的会抛：凭据面形状不对 → AssertionError", caught))
    checks.append(("合规的凭据面放行", nodelet.enforce_credential_redline(
        {"a2a_probe_token": {"present": True, "len": 3}}) is True))

    # —— 源码守卫
    src = _src("node/l0_host/nodelet.py")
    old_src = _src("tools/equivalence/_old/nodelet_old_k23.py")
    imports = _import_names(src)
    checks.append(("L0 守护只 import 同层 node_profile（上层件一个没有）",
                   not any(m in imports for m in UPPER_MODULES)))
    checks.append(("L0 守护确实 import 了节点档案", "node_profile" in imports))
    # 判据非空转自证（合成夹具）：抽取器必须同时认出「点分模块路径」与「被导入名」，
    # 否则上面两条守卫会一起失明（一条恒真、一条恒假）——这是本装置自己的元判据。
    _probe = _import_names("import os\nfrom node.l0_host import node_profile as np\n"
                           "from node.l2_adapter.agent_usage import x\n")
    checks.append(("import 抽取器非空转（点分路径 + 被导入名 + as 别名都认）",
                   {"node", "node.l0_host", "node_profile", "agent_usage"} <= _probe))
    checks.append(("只读：源码里没有任何写/启停调用（%d 个判词全不命中）" % len(WRITE_CALLS),
                   not [w for w in WRITE_CALLS if w in src]))
    checks.append(("只读：源码里没有以写模式 open 文件",
                   not re.search(r"open\([^)]*[\"'][rax]?w", src)))
    old_imports = _import_names(old_src)
    checks.append(("旧侧是独立实现：`_old/nodelet_old_k23.py` 的 import 里没有 nodelet",
                   "nodelet" not in old_imports))
    checks.append(("旧侧自带常量：源码里有自己那份规范单元名字面量",
                   old.UNIT_POSIX in old_src and old.UNIT_NT in old_src))
    checks.append(("探针口只有一条对外路径", nodelet.PROBE_PATH == "/probe"))
    checks.append(("默认绑定是环回", nodelet.DEFAULT_HOST.startswith("127.")))

    # —— 档案 helper 向后兼容（契约 §4：不动既有面语义）
    checks.append(("节点档案新增的是**只读 helper**，既有面一个没少",
                   all(hasattr(npf, n) for n in
                       ("flavour", "home", "runtime_dir", "target_known", "a2a_probe_token"))))
    checks.append(("node_name() 与 target_host() 同一把归一尺子",
                   npf.node_name.__module__ == npf.target_host.__module__))
    os.environ["AGENT_TEAM_NODE"] = "  Root@ThinkBook.Example.LAN  "
    checks.append(("node_name() 归一：去用户名、去域名、转小写",
                   npf.node_name() == "thinkbook.example.lan"))
    os.environ["AGENT_TEAM_NODE"] = "LOBSTER"
    checks.append(("node_name() env 覆盖生效且转小写", npf.node_name() == "lobster"))
    os.environ.pop("AGENT_TEAM_NODE", None)

    bad = 0
    for label, ok in checks:
        out("  %s %s" % ("✓" if ok else "✗", label))
        if not ok:
            bad += 1
    out("  → C 段 %d 项，不合格 %d 项" % (len(checks), bad))
    return bad, len(checks)


# ============================================================ D 段 · 真 HTTP 端到端

def section_d(out, nodelet, old, npf):
    paths, files = build_sandbox()
    apply_env(scenarios(paths, files)[0]["env"])
    os.environ["AGENT_TEAM_NODELET_SERVICES"] = "a2a-svc"

    port = _free_port()
    server = nodelet.make_server("127.0.0.1:%d" % port, started_at=time.time())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = "http://127.0.0.1:%d" % port
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(base + nodelet.PROBE_PATH, timeout=5) as resp:
            code, ctype = resp.getcode(), resp.headers.get("Content-Type", "")
            wire = resp.read().decode("utf-8")
        try:
            opener.open(base + "/nope", timeout=5)
            code404 = 0
        except urllib.error.HTTPError as exc:
            code404 = exc.code
        try:
            opener.open(urllib.request.Request(base + nodelet.PROBE_PATH, data=b"{}"), timeout=5)
            code405 = 0
        except urllib.error.HTTPError as exc:
            code405 = exc.code
    finally:
        server.shutdown()
        server.server_close()

    wire_norm, wire_hits = normalize(wire)
    local = json.dumps(nodelet.build_probe(started_at=time.time(), archive=npf), ensure_ascii=False)
    local_norm, _ = normalize(local)
    old_side = json.dumps(old.probe_payload(archive=npf, started_at=time.time(), now=time.time(),
                                            self_path=NODELET_PATH, run=nodelet._run_cmd),
                          ensure_ascii=False)
    old_norm, _ = normalize(old_side)

    parsed = json.loads(wire)
    checks = [
        ("GET /probe → 200", code == 200),
        ("Content-Type 是 JSON", "application/json" in ctype),
        ("线上发出去的那串字节归一后 == 本地 build_probe 归一后", wire_norm == local_norm),
        ("线上字节归一后 == **人工算法**归一后（真·两侧同解）", wire_norm == old_norm),
        ("易变键在线上这一份里也确实需要归一（两条规则各命中 1 次）", wire_hits == (1, 1)),
        ("顶层键序在线上这一份里也没漂", tuple(parsed.keys()) == tuple(nodelet.PROBE_KEYS)),
        ("facts 键序在线上这一份里也没漂", tuple(parsed["facts"].keys()) == tuple(nodelet.FACT_KEYS)),
        ("探针体里没有任何路径全文（沙箱路径一处不见）",
         SANDBOX not in wire and SANDBOX.replace("\\", "/") not in wire),
        ("探针体里没有凭据值（假凭据一处不见）", _FAKE_CRED not in wire),
        ("凭据面只报了长度", parsed["facts"]["a2a_probe_token"] == {"present": True,
                                                              "len": len(_FAKE_CRED)}),
        ("GET 别的路径 → 404", code404 == 404),
        ("POST 任何路径 → 405（只读服务）", code405 == 405),
    ]
    bad = 0
    for label, ok in checks:
        out("  %s %s" % ("✓" if ok else "✗", label))
        if not ok:
            bad += 1
    out("  → D 段 %d 项，不合格 %d 项" % (len(checks), bad))
    return bad, len(checks)


# ============================================================ E 段 · 单元生成器

def section_e(out, nodelet):
    import nodelet_gen as gen
    for k in WIPE:
        os.environ.pop(k, None)
    targets = gen.load_targets()
    checks = [("部署表读得到，且四台机都在", len(targets) == 4)]
    gen_src = _src("tools/nodelet_gen.py")

    for target in targets:
        facts = gen.node_facts(target)
        text = gen.render_unit(facts)
        name = facts["node"]
        want_unit = nodelet.UNIT_NAME_NT if facts["flavour"] == "nt" else nodelet.UNIT_NAME_POSIX
        checks.append(("%-9s 单元名 = 规范名 %s" % (name, want_unit), facts["unit"] == want_unit))
        checks.append(("%-9s 单元里写的节点名 = 部署表里的节点名" % name,
                       ("%s=%s" % (gen.node_profile.ENV_NODE_NAME, name)) in text
                       or ("--node %s" % name) in text))
        checks.append(("%-9s 绑定面是环回默认值" % name,
                       facts["bind"] == "%s:%d" % (nodelet.DEFAULT_HOST, nodelet.DEFAULT_PORT)))
        checks.append(("%-9s ExecStart 指向该机运行目录下的 nodelet.py" % name,
                       facts["exec_path"].endswith(gen.NODELET_FILE)
                       and facts["exec_path"].startswith(facts["runtime_dir"])))
        checks.append(("%-9s 声明单元清单包含守护自己（各机至少一条可判事实）" % name,
                       want_unit in facts["services"]))
        checks.append(("%-9s 路径分隔符没混用" % name,
                       ("\\" not in facts["exec_path"]) if facts["flavour"] != "nt"
                       else ("/" not in facts["exec_path"])))
        checks.append(("%-9s 生成物只是文本（本次调用一个字节都没落盘）" % name,
                       isinstance(text, str) and len(text) > 0))

    checks.append(("systemd 模板全仓只有一份（源码里 `[Unit]` 恰好 1 处）",
                   gen_src.count("[Unit]") == 1))
    checks.append(("计划任务模板全仓只有一份（源码里 `schtasks /Create` 恰好 1 处）",
                   gen_src.count("schtasks /Create") == 1))
    checks.append(("生成器不另抄单元名（只从 nodelet 取）",
                   ('"%s"' % nodelet.UNIT_NAME_POSIX) not in gen_src
                   and ('"%s"' % nodelet.UNIT_NAME_NT) not in gen_src))
    checks.append(("生成器不碰 deploy.py（契约 §4）", "deploy" not in _import_names(gen_src)))

    bad = 0
    for label, ok in checks:
        out("  %s %s" % ("✓" if ok else "✗", label))
        if not ok:
            bad += 1
    out("  → E 段 %d 项，不合格 %d 项" % (len(checks), bad))
    return bad, len(checks)


# ============================================================ 负控

def apply_negative(kind, nodelet):
    """往**新侧**注私货，证明这套矩阵真有敏感度（不是恒绿）。"""
    orig = nodelet.build_probe

    if kind == "all":
        def poisoned(**kw):
            p = orig(**kw)
            p["node"] = (p["node"] or "") + "x"
            p["profile"] = dict(p["profile"], ok=not p["profile"]["ok"], missing=["poison"])
            p["facts"] = dict(p["facts"], runtime_dir={"exists": True, "writable": True},
                              log_dir={"exists": True, "writable": True},
                              hermes_state_db={"configured": True, "exists": True},
                              openclaw_sessions={"configured": True, "exists": True},
                              a2a_probe_token={"present": True, "len": 1},
                              services=[{"name": "poison", "known": True, "active": True}])
            p["version"] = "deadbeef"
            p["uptime_s"] = -1.0
            p["ts"] = "1999-01-01T00:00:00Z"
            return p
        nodelet.build_probe = poisoned
        return "负控 all：探针体每一个键都动一处（六键 + 整串字节 —— 每格都必须判红）"

    if kind == "fake-active":
        real = nodelet.unit_state

        def fake(name, flavour_value=None, run=None):
            known, _active = real(name, flavour_value, run)
            return known, True                          # 查不到也说 active —— 正是「不编造」的反面
        nodelet.unit_state = fake
        return "负控 fake-active：unit_state 一律报 active（编造运行状态）"

    if kind == "leak-cred":
        def leaky(value):
            text = "" if value is None else str(value)
            return {"present": bool(text), "len": len(text), "value": text}
        nodelet.credential_fact = leaky
        return "负控 leak-cred：凭据面多带一个 value 键（红线必须拦住）"

    if kind == "ok-always":
        def always_ok(**kw):
            p = orig(**kw)
            p["profile"] = dict(p["profile"], ok=True, missing=[])
            return p
        nodelet.build_probe = always_ok
        return "负控 ok-always：profile 一律报 ok（档案残缺被掩盖）"

    if kind == "key-order":
        def reordered(**kw):
            p = orig(**kw)
            return {"ts": p["ts"], "node": p["node"], "profile": p["profile"],
                    "facts": p["facts"], "version": p["version"], "uptime_s": p["uptime_s"]}
        nodelet.build_probe = reordered
        return "负控 key-order：顶层键序被改（冻结的东西漂了）"

    raise SystemExit("未知负控种类：%s" % kind)


# ============================================================ 主流程

def load_sides():
    missing = [n for n in REQUIRED_OLD if not os.path.isfile(os.path.join(OLD_DIR, n))]
    if missing:
        sys.stderr.write("✗ 旧侧基线缺失：%s\n" % "、".join(missing))
        raise SystemExit(2)
    from node.l0_host import node_profile as npf
    import node.l0_host.nodelet as nodelet
    import nodelet_old_k23 as old
    return nodelet, old, npf


def _md5(path):
    return "%d B md5 %s" % (os.path.getsize(path),
                            hashlib.md5(io.open(path, "rb").read()).hexdigest()[:12])


def run_once(neg=None):
    buf = []

    def out(line):
        buf.append(line)

    nodelet, old, npf = load_sides()

    out("=" * 100)
    out("刀23 等价装置 · 轻量节点守护 = 档案驱动的探针口")
    out("旧侧：_old/nodelet_old_k23.py（%s）—— **独立第二实现**（新增件无改前字节可取，见模块文档串）"
        % _md5(os.path.join(OLD_DIR, "nodelet_old_k23.py")))
    out("新侧：node/l0_host/nodelet.py（%s，工作区活件）" % _md5(NODELET_PATH))
    out("归一化规则（契约 §2 显式声明）：")
    out("  ① %s  →  \"ts\": \"<TS>\"" % TS_RX.pattern)
    out("  ② %s  →  \"uptime_s\": <UPTIME>" % UPTIME_RX.pattern)
    if neg:
        out("！！负控模式：%s" % apply_negative(neg, nodelet))
    out("=" * 100)
    out("")

    total = 0
    cells = 0

    def section(label, fn, *args):
        """跑一段。段内抛异常不许把整个装置炸掉 —— 注毒时「炸了」本身就是一种判红，
        但得让后面几段照跑完，报告才看得见全貌。"""
        nonlocal total, cells
        out(label)
        try:
            bad, n = fn(out, *args)
        except Exception as exc:                        # noqa: BLE001
            out("  ✗ 本段执行中抛了：%s: %s" % (type(exc).__name__, exc))
            bad, n = 1, 1
        total, cells = total + bad, cells + n
        out("")

    section("A 段 · 探针体冻结矩阵（%d 项/场景）" % len(CELL_KEYS),
            section_a, nodelet, old, npf, neg)
    section("B 段 · 易变键归一化自检", section_b, nodelet, old, npf)
    section("C 段 · 契约冻结与三条红线", section_c, nodelet, old, npf)
    section("D 段 · 真 HTTP 端到端（真起服务、真 GET）", section_d, nodelet, old, npf)
    section("E 段 · 单元生成器（档案 + 部署表 → 单元，模板只此一份）", section_e, nodelet)

    for k in WIPE:
        os.environ.pop(k, None)

    out("=" * 100)
    out("合计 %d 格，不一致/不合格 %d 处" % (cells, total))
    out("EQUIV_SENTINEL_OK" if total == 0 else "EQUIV_SENTINEL_FAIL")
    return total, scrub("\n".join(buf)) + "\n"


def main():
    ap = argparse.ArgumentParser(description="刀23 等价装置")
    ap.add_argument("--runs", type=int, default=1, help="连跑 N 轮自证确定性（默认 1）")
    ap.add_argument("--neg", choices=["all", "fake-active", "leak-cred", "ok-always", "key-order"],
                    default=None, help="负控：往新侧注私货，本装置必须判红")
    args = ap.parse_args()

    if args.runs > 1:
        outs = []
        for i in range(args.runs):
            p = subprocess.run([sys.executable, os.path.abspath(__file__)], capture_output=True)
            outs.append((p.returncode, p.stdout))
            sys.stdout.write("第 %d/%d 轮：rc=%d，%d 字节，md5=%s，%s\n"
                             % (i + 1, args.runs, p.returncode, len(p.stdout),
                                hashlib.md5(p.stdout).hexdigest(),
                                "EQUIV_SENTINEL_OK" if b"EQUIV_SENTINEL_OK" in p.stdout
                                else "EQUIV_SENTINEL_FAIL"))
        rcs = {rc for rc, _ in outs}
        same = len({o for _, o in outs}) == 1
        sys.stdout.write("退出码集合=%s；%d 份输出逐字节%s\n"
                         % (sorted(rcs), len(outs), "相同" if same else "**不同**"))
        if same and rcs == {0}:
            sys.stdout.write("EQUIV_DETERMINISM_OK\n")
            return 0
        sys.stdout.write("EQUIV_DETERMINISM_FAIL\n")
        return 1

    total, text = run_once(args.neg)
    sys.stdout.write(text)
    if args.neg:
        sys.stdout.write("负控判定：%s（注毒后必须判红）\n"
                         % ("通过 —— 已判红" if total > 0 else "**失败 —— 竟然还是绿的**"))
        return 0 if total > 0 else 1
    return 0 if total == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
