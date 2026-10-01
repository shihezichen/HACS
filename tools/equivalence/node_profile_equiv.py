#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""node_profile_equiv.py —— 刀21 等价证据：L0「节点档案」正式化 + 宿主会话真源读取下沉

旧侧（冻结档，逐字节取自改前树 `git show HEAD:...`）
    tools/equivalence/_old/node_paths_old_k21.py     改前的 L0 node_paths（自带目标主机档案实现）
    tools/equivalence/_old/agent_usage_old_k21.py    改前的 L2 agent_usage（自带 sqlite3 句柄与宿主路径）

新侧（工作区活件）
    node/l0_host/node_profile.py   L0 节点档案（档案表与只读探针的唯一真值）
    node/l0_host/node_paths.py     L0 本机默认值（target_* 已改委托/别名）
    node/l2_adapter/agent_usage.py L2 适配层（不再 import sqlite3 / os）

五段，全部**真 import 两侧模块、真调函数、真开临时 sqlite 库**，没有一处打桩：

  A 段 · 目标主机档案冻结矩阵（13 主机 × 4 env 场景 = 52 格 × 3 路）
        每格比 `known | flavour | home | claude_exec`。三路 = 旧 node_paths / 新 node_paths /
        新 node_profile —— 既证「行为没变」，也证「新旧调用面拿到的是同一份真值」。
        覆盖：user@host 同解 / 大小写与空白 / 未登记不编造 / 空串与 None 兜底 /
              AGENT_TEAM_TARGET_PROFILE_JSON 整表覆盖 / 坏 JSON 退回内置表 / 空串 env。

  B 段 · 本机面冻结矩阵（13 函数 × 3 flavour × 3 env 场景 = 117 格 × 3 路）
        节点身份 / 运行与配置目录 / 执行器路径。第三路是 node_profile 的薄委托 ——
        证明「节点档案这个门」与 node_paths 给的是同一个值，不是第二份真值。

  C 段 · 公共面与对象身份守卫
        node_paths 的 __all__ 与全部目标档案名逐一还在，且**指向 node_profile 里的同一个对象**；
        node_paths 源码里不再有第二份档案表；agent_usage 源码里不再有 sqlite3 / os。

  D 段 · agent_usage 行为面（临时副本 + 造样本，**绝不读生产 state.db**）
        在固定沙箱里造 22 份样本（正常 db / 空库 / 缺表 / 坏文件 / 不存在 / 目录 / 无权限 /
        未配置 / 坏 JSON / 字段缺失 …），同一批样本上把旧实现与新实现的 16 条调用面逐值比对，
        含全部降级路径。

  E 段 · 下沉净效果（只读性与句柄归属的事实记账）

用法：
    python tools/equivalence/node_profile_equiv.py                # 跑一轮
    python tools/equivalence/node_profile_equiv.py --runs 2       # 连跑 2 轮自证确定性
    python tools/equivalence/node_profile_equiv.py --neg profile  # 负控：往新侧档案表注私货，必须判红
    python tools/equivalence/node_profile_equiv.py --neg usage    # 负控：把新侧降级路径改成吞异常返 0，必须判红

退出码：0 全一致；1 有不一致；2 装置自身前置条件不满足（旧侧冻结档缺失等）。
哨兵：全一致打 EQUIV_SENTINEL_OK；--runs N 全轮逐字节相同再打 EQUIV_DETERMINISM_OK。

确定性
------
沙箱路径固定（`%TEMP%/k21_equiv_sandbox`，开跑前先清空），输出里所有沙箱路径与本机家目录
一律归一成 `<SANDBOX>` / `<HOME>`；令牌只打长度与 md5 前缀，绝不回显原文。
"""

import argparse
import ast
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
#: 保留注入：OLD_DIR 不是包目录（冻结快照按裸名装入），且 `agent_usage_old_k21` 内部是
#: 裸名 `import node_paths` —— 快照按裁定一字不动，只能把 L0 目录一并注入供其解析。
L0_HOST = os.path.join(REPO, "node", "l0_host")
OLD_DIR = os.path.join(HERE, "_old")

for _p in (REPO, L0_HOST, OLD_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

#: 固定沙箱（不用 mkdtemp：随机名会让 --runs 的逐字节比对天然失败）
SANDBOX = os.path.join(tempfile.gettempdir(), "k21_equiv_sandbox")

#: 旧侧冻结档 —— 缺一个就判 2（宁可判不清，也不给假绿灯）
REQUIRED_OLD = ["node_paths_old_k21.py", "agent_usage_old_k21.py"]

#: 两侧都会读的 env，每格开跑前一律清干净（不清就串场）
WIPE = [
    "AGENT_TEAM_TARGET_PROFILE_JSON",
    "AGENT_TEAM_RUNTIME_DIR", "AGENT_TEAM_ETC_DIR", "AGENT_TEAM_LOG_DIR",
    "AGENT_TEAM_NODE_EXE", "AGENT_TEAM_NATS_DIR",
    "AGENT_TEAM_CLAUDE_EXEC", "AGENT_TEAM_CLAUDE_WORKDIR",
    "AGENT_TEAM_CODEX_EXE", "AGENT_TEAM_CODEX_CMD",
    "AGENT_TEAM_PI_CLI_JS", "AGENT_TEAM_PI_WORKDIR",
    "A2A_SESSIONS_JSON", "CTX_SESSIONS_JSON",
    "A2A_HERMES_STATE_DB", "CTX_HERMES_STATE_DB",
    "A2A_HERMES_HOME", "HERMES_HOME", "A2A_HERMES_AGENT_DIR",
    "CTX_A2A_BASE", "A2A_BASE", "CTX_A2A_TOKEN", "A2A_TOKEN",
    "CTX_A2A_TOKEN_FILE", "A2A_TOKEN_FILE", "AGENT_TEAM_TOKEN_FILE",
]


# ============================================================ 取值 / 取异常的统一口径

def probe(fn, *args):
    """调一次，把结果压成可逐字节比对的三元组。异常也算结果（类型名 + 消息都进比对）。"""
    try:
        value = fn(*args)
    except BaseException as exc:            # noqa: BLE001 —— 故意抓全，异常本身是判据
        return ("RAISE", type(exc).__name__, norm(str(exc)))
    return ("VALUE", type(value).__name__, norm(repr(value)))


def norm(text):
    """归一：沙箱路径 → <SANDBOX>，本机家目录 → <HOME>。

    三种写法都要收：原样、正斜杠、反斜杠，**外加 repr() 里的双反斜杠转义形态** ——
    少收最后一种，Windows 上 repr 出来的路径就漏网（这正是第一版的疏漏）。
    """
    home = os.path.expanduser("~")
    for raw, tag in ((SANDBOX, "<SANDBOX>"), (home, "<HOME>")):
        if not raw:
            continue
        back = raw.replace("/", "\\")
        for variant in (back.replace("\\", "\\\\"), raw, raw.replace("\\", "/"), back):
            if variant:
                text = text.replace(variant, tag)
    return text


def cell_str(cell):
    kind, tname, text = cell
    return "%s/%s/%s" % (kind, tname, text)


def mask(value):
    """令牌之类不回显原文，只留长度与 md5 前缀（装置日志不许自己变成泄漏源）。"""
    s = str(value)
    if not s:
        return "<空>"
    return "len=%d md5=%s" % (len(s), hashlib.md5(s.encode("utf-8")).hexdigest()[:10])


def wipe(extra=None):
    for k in WIPE:
        os.environ.pop(k, None)
    for k, v in (extra or {}).items():
        os.environ[k] = v


# ============================================================ 装载两侧

def load_sides():
    missing = [n for n in REQUIRED_OLD if not os.path.isfile(os.path.join(OLD_DIR, n))]
    if missing:
        sys.stderr.write("旧侧冻结档缺失：%s\n" % "、".join(missing))
        raise SystemExit(2)
    import node_paths_old_k21 as old_np
    import agent_usage_old_k21 as old_au
    from node.l0_host import node_paths as new_np
    from node.l0_host import node_profile as npf
    from node.l2_adapter import agent_usage as new_au
    return old_np, old_au, new_np, npf, new_au


# ============================================================ A 段 · 目标主机档案冻结矩阵

#: user@host / 裸 host / 大小写 / 前后空白 / 未登记 / 空串 / 纯空白 / None
HOSTS = ["<user>@<node>", "thinkbook", "THINKBOOK", " thinkbook ",
         "lobster", "root@lobster", "quantum", "top001",
         "nosuchhost", "user@nosuchhost", "", "   ", None]

#: 整表覆盖：故意把 thinkbook 改 posix、给 lobster 换家目录、加一台只给 flavour 的新机 ——
#: 一次性证明「flavour / home / 缺键补齐 / 整表覆盖后原有主机变未登记」四条支路都吃这个 env。
PROFILE_JSON = json.dumps({
    "thinkbook": {"flavour": "posix", "home": "/srv/tb",
                  "claude_exec": "/srv/tb/bin/claude_exec.py"},
    "lobster": {"flavour": "posix", "home": "/data/nbhome"},    # 缺 claude_exec → 按 home 补齐
    "newbox": {"flavour": "nt"},                                # 缺 home/exec → 按 flavour 补齐
}, ensure_ascii=False)

A_SCENARIOS = [
    ("no-env", {}),
    ("profile-json", {"AGENT_TEAM_TARGET_PROFILE_JSON": PROFILE_JSON}),
    ("bad-json", {"AGENT_TEAM_TARGET_PROFILE_JSON": "{不是 JSON"}),
    ("empty-json", {"AGENT_TEAM_TARGET_PROFILE_JSON": "   "}),
]


def _target_cell(mod, host):
    parts = []
    for fn in ("target_known", "target_flavour", "target_home", "target_claude_exec"):
        parts.append(cell_str(probe(getattr(mod, fn), host)))
    return " | ".join(parts)


def section_a(out, old_np, new_np, npf):
    lanes = [("旧 node_paths", old_np), ("新 node_paths", new_np), ("新 node_profile", npf)]
    total = len(A_SCENARIOS) * len(HOSTS)
    out("A 段 · 目标主机档案冻结矩阵（%d 格 × %d 路）　列：known | flavour | home | claude_exec"
        % (total, len(lanes)))
    out("  注：输出里的 <HOME> 是归一占位符。本机是 thinkbook，其**目标**家目录与**本机**家目录"
        "恰好同值，所以 thinkbook 那几行也显示成 <HOME>；三路比对是在归一**之前**同样处理的，不影响判定。")
    bad = 0
    for name, env in A_SCENARIOS:
        out("  [%s]" % name)
        for host in HOSTS:
            wipe(env)
            cells = [(lab, _target_cell(m, host)) for lab, m in lanes]
            base = cells[0][1]
            diff = [lab for lab, c in cells if c != base]
            out("    %s %-20s %s" % ("✓" if not diff else "✗", repr(host), base))
            if diff:
                bad += 1
                for lab, c in cells:
                    out("        %-16s %s" % (lab, c))
    wipe()
    out("  → 不一致 %d / %d 格" % (bad, total))
    return bad


# ============================================================ B 段 · 本机面冻结矩阵

B_FUNCS = ["flavour", "runtime_dir", "etc_dirs", "etc_dir", "log_dir", "nats_dir",
           "node_exe", "claude_exec", "claude_workdir", "codex_exe", "codex_cmd",
           "pi_cli_js", "pi_workdir"]

B_SCENARIOS = [
    ("no-env", {}),
    ("env-override", {"AGENT_TEAM_RUNTIME_DIR": "/tmp/rt", "AGENT_TEAM_ETC_DIR": "/tmp/etc",
                      "AGENT_TEAM_LOG_DIR": "/tmp/log", "AGENT_TEAM_NODE_EXE": "/usr/bin/node20",
                      "AGENT_TEAM_NATS_DIR": "/tmp/nats",
                      "AGENT_TEAM_CLAUDE_EXEC": "/tmp/claude.py",
                      "AGENT_TEAM_CLAUDE_WORKDIR": "/tmp/cwd",
                      "AGENT_TEAM_CODEX_EXE": "/tmp/codex", "AGENT_TEAM_CODEX_CMD": "/tmp/codex.cmd",
                      "AGENT_TEAM_PI_CLI_JS": "/tmp/cli.js", "AGENT_TEAM_PI_WORKDIR": "/tmp/pi"}),
    # 空串 env 必须当「没设」（strip 后为空 → 回落平台默认值），不许把空串当真值返回
    ("empty-env", {"AGENT_TEAM_RUNTIME_DIR": "   ", "AGENT_TEAM_ETC_DIR": "",
                   "AGENT_TEAM_LOG_DIR": " ", "AGENT_TEAM_NODE_EXE": "",
                   "AGENT_TEAM_NATS_DIR": "  ", "AGENT_TEAM_CLAUDE_EXEC": "",
                   "AGENT_TEAM_CLAUDE_WORKDIR": " ", "AGENT_TEAM_CODEX_EXE": "",
                   "AGENT_TEAM_CODEX_CMD": "  ", "AGENT_TEAM_PI_CLI_JS": "",
                   "AGENT_TEAM_PI_WORKDIR": " "}),
]

B_FLAVOURS = [None, "nt", "posix"]



#: 批⑦「方案 a」占位符的形态：`<deploy-dir>` 一类（**必须**是这种带尖括号的小写短标识，
#: 否则负控注毒值 `'<poisoned for negative control>'` 也会被误免判 ⇒ 负控失效）。
_PLACEHOLDER_RE = re.compile(r"^<[a-z][a-z0-9_-]*>")
MAX_GRANTED = 20          #: 免判上限：防「已授权面」被悄悄扩大，超限即判红


def _cell_value(text):
    """`cell_str` 形态（`KIND/tname/text`）取第三段；已是纯值则原样返回。"""
    s = str(text)
    head, sep, rest = s.partition("/")
    if sep and head in ("VALUE", "RAISE"):
        return rest.partition("/")[2]
    return s


def _scalars(text):
    """把值拆成「标量片段」表：先剥 `cell_str` 壳，再剥 list/tuple 壳，**取全部非空片段**。

    只取标量会漏掉 `['<deploy-dir>']` 这类 list 形态的格（谓词会静默失效）；只取首个元素
    又会漏掉 `['', '', '', '<deploy-dir>\\client-token']` 这类前导空元素的格。
    标量输入返回单元素表。
    """
    s = _cell_value(text).strip()
    if s[:1] in "[(" and s[-1:] in ")]":
        parts = [p.strip() for p in s[1:-1].split(",")]
    else:
        parts = [s]
    return [p for p in parts if p.strip().strip("'\"")]


def plan_a_granted(old_v, new_v) -> bool:
    """批⑦「方案 a」已授权变更的**谓词式**登记（不写死本机路径、不放宽其它格）。

    占位符语义 = 未配置 ⇒ 退本地开发默认值（线上走真值分支、行为零变化）。因此改造前的
    冻结侧给占位符（`'<deploy-dir>'`），改造后的新侧给 `tempfile.gettempdir()` 派生的绝对
    路径 —— 两侧**同时**满足这两个形态才算「已授权」，其余一律照常判红。免判格**必须打印/计数**
    并计入 `MAX_GRANTED`。
    """
    olds, news = _scalars(old_v), _scalars(new_v)
    if not olds or len(olds) != len(news):
        return False
    tmp = re.sub(r"[\\/]+", "/", tempfile.gettempdir()).rstrip("/")
    home = re.sub(r"[\\/]+", "/", os.path.expanduser("~"))
    for o, n in zip(olds, news):
        o, n = o.strip().strip("'\""), n.strip().strip("'\"")
        if not _PLACEHOLDER_RE.match(o):
            return False                       # 旧侧不是占位符 ⇒ 不适用本登记
        cand = re.sub(r"[\\/]+", "/", n).replace("<HOME>", home)
        if not (cand == tmp or cand.startswith(tmp + "/")):
            return False                       # 新侧不是本机开发兜底 ⇒ 照常判红
    return True


def section_b(out, old_np, new_np, npf):
    lanes = [("旧 node_paths", old_np), ("新 node_paths", new_np), ("新 node_profile", npf)]
    total = len(B_SCENARIOS) * len(B_FLAVOURS) * len(B_FUNCS)
    out("B 段 · 本机面冻结矩阵（%d 格 × %d 路）　节点身份 / 运行与配置目录 / 执行器路径"
        % (total, len(lanes)))
    bad = 0
    granted_cells = set()      # 按唯一 (函数, flavour) 计数，防「已授权面」扩张
    for name, env in B_SCENARIOS:
        out("  [%s]" % name)
        for fl in B_FLAVOURS:
            for fn in B_FUNCS:
                wipe(env)
                cells = [(lab, cell_str(probe(getattr(m, fn), fl))) for lab, m in lanes]
                base = cells[0][1]
                raw = [c for lab, c in cells if c != base]
                # 方案 a 已授权格：旧侧未配置态占位符 × 新侧本机开发兜底 ⇒ 免判，但**必须计数**、计入上限
                waived = [c for c in raw if plan_a_granted(base, c)]
                diff = [c for c in raw if not plan_a_granted(base, c)]
                if waived:
                    granted_cells.add((fn, fl))
                if diff:
                    bad += 1
                    out("    ✗ %-16s flavour=%-7s %s" % (fn, fl, base))
                    for lab, c in cells:
                        out("        %-16s %s" % (lab, c))
                elif waived:
                    out("    ○ %-16s flavour=%-7s %s [方案 a 已授权 ×%d]" % (fn, fl, base, len(waived)))
                elif fl is None:
                    out("    ✓ %-16s flavour=%-7s %s" % (fn, fl, base))
        # 节点身份 home()：node_profile 独有的档案面，与 os.path.expanduser("~") 同解
        wipe(env)
        ok = npf.home() == os.path.expanduser("~")
        out("    %s %-16s %s" % ("✓" if ok else "✗", "home()", norm(repr(npf.home()))))
        if not ok:
            bad += 1
    wipe()
    out("  → 不一致 %d / %d 格（逐格都跑，只打 flavour=None 那一行与全部不一致行）" % (bad, total))
    out("  → 方案 a 已授权 %d 格（唯一「函数 × flavour」计数 · 上限 %d，超限即判红）"
        % (len(granted_cells), MAX_GRANTED))
    return bad + (1 if len(granted_cells) > MAX_GRANTED else 0)


# ============================================================ C 段 · 公共面与对象身份

#: 改前 node_paths 里与目标主机档案有关的全部名字（公开 + 私有），一个都不许丢
TARGET_NAMES = ["ENV_TARGET_PROFILE_JSON", "NT_TARGET_HOME", "POSIX_TARGET_HOME",
                "TARGET_FALLBACK_FLAVOUR", "_TARGET_CLAUDE_SUB", "_TARGET_PROFILES",
                "_target_host", "_target_table", "_target_profile",
                "target_known", "target_flavour", "target_home", "target_claude_exec"]

#: 新旧同名对象必须**是同一个对象**（不是拷贝）：node_paths.<旧名> is node_profile.<新名>
IDENTITY_PAIRS = [("ENV_TARGET_PROFILE_JSON", "ENV_TARGET_PROFILE_JSON"),
                  ("NT_TARGET_HOME", "NT_TARGET_HOME"),
                  ("POSIX_TARGET_HOME", "POSIX_TARGET_HOME"),
                  ("TARGET_FALLBACK_FLAVOUR", "TARGET_FALLBACK_FLAVOUR"),
                  ("_TARGET_CLAUDE_SUB", "TARGET_CLAUDE_SUB"),
                  ("_TARGET_PROFILES", "TARGET_PROFILES"),
                  ("_target_host", "target_host"),
                  ("_target_table", "target_table"),
                  ("_target_profile", "target_profile"),
                  ("target_known", "target_known"),
                  ("target_flavour", "target_flavour"),
                  ("target_home", "target_home"),
                  ("target_claude_exec", "target_claude_exec")]


#: `agent_usage` 允许的 `os` 用途 —— **只允许读环境变量 + 纯字符串路径运算**。
#:
#: 刀 21 把「宿主上长什么样」（路径 / 句柄 / 文件格式）整套下沉到 L0 节点档案，本件只留适配
#: 口径。之后因 `CTX_A2A_TIMEOUT_S` 需可配而把 `import os` 恢复了，另有一行
#: `from os.path import abspath, dirname` —— 但用途严格限定在 `os.environ`（只读 env）与
#: `os.path` 的**纯字符串**成员（不落盘、不读盘）。
#:
#: **原判据是「源码里没有 `import os` 语句」—— 那是拿「有没有 import」代理「碰不碰文件系统」；
#: import 一恢复，代理就失效（判据红而事实无碍）。** 现改为**直判用途**：不禁止 import，
#: 但 `os` 的任何一个成员只要不在下面两张名单里，一律判红 —— 强度不降反升，因为
#: 「不 import」只挡住了整包导入，而这里连 `from os import path` / `os.path.join` 这类
#: 绕法也一并挡住。
#:
#: 名单是**精确列名**而非「碰不碰 fs 的模糊判断」：`os.path.exists` / `os.path.getsize` /
#: `os.listdir` / `os.open` 这些真碰盘的成员一个都不在里面，写进去即红。
AU_OS_ALLOWED_PREFIXES = ("environ",)
AU_OS_ALLOWED_EXACT = ("sep", "linesep", "pathsep", "curdir", "pardir", "altsep",
                       "name", "devnull", "fspath", "PathLike",
                       "path.abspath", "path.dirname", "path.basename", "path.join",
                       "path.normpath", "path.splitext", "path.split", "path.isabs",
                       "path.relpath", "path.commonprefix", "path.sep", "path.curdir",
                       "path.pardir", "path.linesep", "path.defpath", "path.devnull")


def _au_os_allowed(member):
    return (member.startswith(AU_OS_ALLOWED_PREFIXES)
            or member in AU_OS_ALLOWED_EXACT)


def _au_os_and_sqlite_uses(src):
    """AST 扫出本件对 `os` / `sqlite3` 的**实际用途**，返回 (os 成员全名集合, sqlite3 引用数)。

    用 AST 而不是文本匹配：`import os` 只是一行声明，说明不了「拿它干了什么」；
    而 `os.path.join(...)`（成员链）与 `from os.path import abspath`（绕开 `os.` 前缀）
    都会骗过朴素的正则/子串比对。
    """
    os_members, sqlite_refs = set(), 0
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Attribute):
            parts, cur = [], node
            while isinstance(cur, ast.Attribute):
                parts.append(cur.attr)
                cur = cur.value
            if isinstance(cur, ast.Name) and cur.id == "os":
                os_members.add(".".join(reversed(parts)))
        elif isinstance(node, ast.ImportFrom) and node.module in ("os", "os.path"):
            # `from os import X` / `from os.path import X` —— 绕开 `os.` 前缀的写法，
            # 必须同样纳入台账，否则 `from os import remove` 就能大摇大摆进来。
            for a in node.names:
                os_members.add(a.name if node.module == "os" else "path." + a.name)
        if isinstance(node, ast.Name) and node.id == "sqlite3":
            sqlite_refs += 1
        if isinstance(node, ast.Import):
            sqlite_refs += sum(1 for a in node.names if a.name == "sqlite3")
        if isinstance(node, ast.ImportFrom) and node.module == "sqlite3":
            sqlite_refs += 1
    return os_members, sqlite_refs


def _src(rel):
    return io.open(os.path.join(REPO, rel), encoding="utf-8").read()


def section_c(out, old_np, new_np, npf, old_au, new_au):
    np_src = _src("node/l0_host/node_paths.py")
    au_src = _src("node/l2_adapter/agent_usage.py")
    checks = []

    checks.append(("node_paths.__all__ 与改前逐项相同", new_np.__all__ == old_np.__all__))
    for n in TARGET_NAMES:
        checks.append(("node_paths.%s 仍在" % n, hasattr(new_np, n)))
    for old_n, new_n in IDENTITY_PAIRS:
        same = getattr(new_np, old_n, None) is getattr(npf, new_n, object())
        checks.append(("node_paths.%s **is** node_profile.%s（同一对象，非拷贝）" % (old_n, new_n), same))

    checks.append(("node_paths 源码里不再有第二份档案表（`_TARGET_PROFILES = {`）",
                   "_TARGET_PROFILES = {" not in np_src))
    # 批⑦ 方案 a 后：该字面量在未配置态退成空串/占位符 ⇒ `"" in src` 恒真、判据自陷。
    # 因此仅在它仍是**真值**时检查；真值一旦在源码里出现就照常判红。
    _nth = npf.NT_TARGET_HOME
    checks.append(("node_paths 源码里不再有目标家目录字面量（Windows 家目录形态；未配置态不适用）",
                   (not _nth) or _nth.startswith("<") or _nth not in np_src))
    checks.append(("node_paths 仍是本机字面量的去处（WIN_RUNTIME_DIR 还在）",
                   hasattr(new_np, "WIN_RUNTIME_DIR")))

    au_public = [n for n in dir(old_au) if not n.startswith("__")]
    lost = [n for n in au_public if not hasattr(new_au, n)]
    # `_first_env` / `_np` / `os` / `sqlite3` 是随下沉一起消失的实现细节名，登记在案
    expected_gone = {"_first_env", "_np", "os", "sqlite3", "annotations"}
    unexpected = [n for n in lost if n not in expected_gone]
    checks.append(("agent_usage 对外名字面只少了登记在案的实现细节名（少：%s）"
                   % ("、".join(sorted(lost)) or "无"), not unexpected))

    # —— 原两条判据：`agent_usage` 源码里没有 os 的 import 语句 · 运行期没有 os/sqlite3 属性。
    #    它们是拿「有没有 import os」**代理**「碰不碰文件系统与存储」。import 因
    #    `CTX_A2A_TIMEOUT_S` 恢复后代理失效（见 AU_OS_ALLOWED_PREFIXES 处注释）。
    #    现改为**直判用途**：不禁止 import，但用途越界即红；强度不降。
    au_os_uses, au_sqlite_refs = _au_os_and_sqlite_uses(au_src)
    au_os_bad = sorted(u for u in au_os_uses if not _au_os_allowed(u))
    checks.append(("agent_usage 对 os 的用途只限读环境变量 / 纯路径运算（实见：%s；越界：%s）"
                   % ("、".join(sorted(au_os_uses)) or "无",
                      "、".join(au_os_bad) or "无"), not au_os_bad))
    checks.append(("agent_usage 源码里没有任何 sqlite3 引用（不碰存储；实见 %d 处）"
                   % au_sqlite_refs, au_sqlite_refs == 0))
    checks.append(("agent_usage 运行期不持 sqlite3 属性（不碰存储）",
                   not hasattr(new_au, "sqlite3")))
    # 负控：判据必须**可见地会红**，否则「全绿」什么都说明不了。往拷贝文本里注入两种越界
    # 用法（文件系统 / 存储），断言各自命中；注入的是**内存里的字符串**，不落盘、不动真件。
    #
    # 锚点必须按**整行**取，不能拿 `src.find("import os")` —— 文档字符串里就有
    # 「当时连 import os 一起去掉了」这句说明文字，裸 find 会命中它，注入进字符串字面量
    # 内部（AST 看不见），负控变成静默假绿。（本行注释即为该坑的现场记录。）
    au_lines = au_src.splitlines(True)
    au_os_line = next((i for i, ln in enumerate(au_lines) if ln.strip() == "import os"), None)
    checks.append(("负控锚点：真件里确实有一行独立的 `import os`（行号 %s）"
                   % (au_os_line + 1 if au_os_line is not None else "未找到"),
                   au_os_line is not None))
    if au_os_line is not None:
        _poison = "".join(au_lines[:au_os_line + 1] + ["_ALSO = os.listdir('.')\n"]
                          + au_lines[au_os_line + 1:])
        _p_os_bad = sorted(u for u in _au_os_and_sqlite_uses(_poison)[0]
                           if not _au_os_allowed(u))
        checks.append(("负控：注入 `os.listdir` 后本判据判红（实见越界成员：%s）"
                       % ("、".join(_p_os_bad) or "无"), bool(_p_os_bad)))
        # 第二种越界：绕开 `os.` 前缀的 `from os import X` 形式，必须同样被抓住。
        _poison2 = "".join(au_lines[:au_os_line + 1]
                           + ["from os import listdir  # noqa\n"] + au_lines[au_os_line + 1:])
        _p2_bad = sorted(u for u in _au_os_and_sqlite_uses(_poison2)[0]
                         if not _au_os_allowed(u))
        checks.append(("负控：`from os import listdir` 这种绕法也判红（实见越界成员：%s）"
                       % ("、".join(_p2_bad) or "无"), bool(_p2_bad)))
        # 第三种越界：存储面。锚点同样不能落在文件头 —— **第 1 行是模块 docstring 的起始行**
        # （`"""L2 · ...`），插在它前面会让新语句并进 docstring，AST 看不见，负控又成静默假绿。
        # 与上一条同坑同因，所以这里复用「必在 docstring 之后」的 `import os` 行做锚点。
        _p_refs = _au_os_and_sqlite_uses(
            "".join(au_lines[:au_os_line + 1] + ["import sqlite3\n"]
                    + au_lines[au_os_line + 1:]))[1]
        checks.append(("负控：注入 `import sqlite3` 后引用数 > 0（实见 %d 处）" % _p_refs,
                       _p_refs > 0))
    checks.append(("agent_usage 拿到的是 L0 节点档案（_npf is node_profile）",
                   getattr(new_au, "_npf", None) is npf))
    checks.append(("改前 agent_usage 确实持 sqlite3（负向对照：旧侧有该属性）",
                   hasattr(old_au, "sqlite3")))

    out("C 段 · 公共面与对象身份守卫（%d 项）" % len(checks))
    bad = 0
    for label, ok in checks:
        out("  %s %s" % ("✓" if ok else "✗", label))
        if not ok:
            bad += 1
    out("  → 不合格 %d / %d 项" % (bad, len(checks)))
    return bad


# ============================================================ D 段 · agent_usage 行为面

CALLER, SESSION = "probe", "s1"
SESSION_KEY = "a2a:%s:%s" % (CALLER, SESSION)
OPENCLAW_KEY = "agent:main:openai-user:a2a:%s:%s" % (CALLER, SESSION)

CFG_HERMES = {"backend": "http://127.0.0.1:8642/v1", "model": "hermes-agent"}
CFG_OPENCLAW = {"backend": "http://127.0.0.1:18789/v1", "model": "gpt-x"}
CFG_OTHER = {"backend": "http://127.0.0.1:9999/v1", "model": "mystery"}

CONFIG_YAML = (
    "# 造的宿主 Hermes 配置（不是生产件）\n"
    "server:\n"
    "  port: 8642\n"
    "model:\n"
    "  default: deepseek-v4-flash\n"
    "  context_length: 200000\n"
    "logging:\n"
    "  level: info\n"
)
CONFIG_YAML_BAD_LEN = CONFIG_YAML.replace("context_length: 200000", "context_length: 不是数字")
CONFIG_YAML_NO_MODEL = "server:\n  port: 8642\n"


def _mkdb(path, *, sessions_table=True, messages_table=True, session_row=True,
          active_rows=2, calls=5, model="deepseek-v4-flash", key=SESSION_KEY):
    """造一份**临时** Hermes state.db 副本（结构对齐真源，数据全是编的）。"""
    conn = sqlite3.connect(path)
    try:
        if sessions_table:
            conn.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY, session_key TEXT, "
                         "model TEXT, input_tokens INTEGER, cache_read_tokens INTEGER, "
                         "api_call_count INTEGER, started_at TEXT, last_activity_at TEXT)")
        if messages_table:
            conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id INTEGER, "
                         "active INTEGER, content TEXT, tool_calls TEXT, reasoning_content TEXT)")
        if sessions_table and session_row:
            conn.execute("INSERT INTO sessions VALUES (7, ?, ?, 120000, 30000, ?, "
                         "'2026-09-26T00:00:00Z', '2026-09-27T00:00:00Z')", (key, model, calls))
            # 第二行同 key、更早 —— 证明 ORDER BY last_activity DESC LIMIT 1 的选行口径没变
            conn.execute("INSERT INTO sessions VALUES (8, ?, ?, 1, 1, 1, "
                         "'2026-09-20T00:00:00Z', '2026-09-21T00:00:00Z')", (key, model))
        if messages_table and sessions_table and session_row:
            samples = [("纯中文内容，用来压 CJK 密集分支" * 40, None, None),
                       ("ascii only content " * 200, '{"name":"tool_a"}', "reasoning text " * 30),
                       (None, None, None)]
            for i, (c, t, r) in enumerate(samples[:max(active_rows, 0)]):
                conn.execute("INSERT INTO messages VALUES (?, 7, 1, ?, ?, ?)", (100 + i, c, t, r))
            conn.execute("INSERT INTO messages VALUES (200, 7, 0, '非活跃消息不该被算进去', NULL, NULL)")
        conn.commit()
    finally:
        conn.close()


def build_samples(out):
    """在固定沙箱里造样本。返回 [(名字, env dict, 备注)]。"""
    if os.path.isdir(SANDBOX):
        shutil.rmtree(SANDBOX, ignore_errors=True)
    os.makedirs(SANDBOX)
    etc = os.path.join(SANDBOX, "etc")
    os.makedirs(etc)

    hhome = os.path.join(SANDBOX, "hermes_home")
    os.makedirs(hhome)
    io.open(os.path.join(hhome, "config.yaml"), "w", encoding="utf-8", newline="\n").write(CONFIG_YAML)

    hhome_bad = os.path.join(SANDBOX, "hermes_home_badlen")
    os.makedirs(hhome_bad)
    io.open(os.path.join(hhome_bad, "config.yaml"), "w", encoding="utf-8",
            newline="\n").write(CONFIG_YAML_BAD_LEN)

    hhome_nomodel = os.path.join(SANDBOX, "hermes_home_nomodel")
    os.makedirs(hhome_nomodel)
    io.open(os.path.join(hhome_nomodel, "config.yaml"), "w", encoding="utf-8",
            newline="\n").write(CONFIG_YAML_NO_MODEL)

    hhome_empty = os.path.join(SANDBOX, "hermes_home_empty")   # 有目录无 config.yaml
    os.makedirs(hhome_empty)

    hhome_cfgdir = os.path.join(SANDBOX, "hermes_home_cfgdir")  # config.yaml 是个目录
    os.makedirs(os.path.join(hhome_cfgdir, "config.yaml"))

    db_ok = os.path.join(SANDBOX, "ok.db")
    _mkdb(db_ok)
    db_noactive = os.path.join(SANDBOX, "noactive.db")
    _mkdb(db_noactive, active_rows=0)
    db_zerocalls = os.path.join(SANDBOX, "zerocalls.db")
    _mkdb(db_zerocalls, active_rows=0, calls=0)
    db_empty = os.path.join(SANDBOX, "empty.db")
    _mkdb(db_empty, session_row=False)
    db_nomsg = os.path.join(SANDBOX, "nomsgtable.db")
    _mkdb(db_nomsg, messages_table=False)
    db_nosess = os.path.join(SANDBOX, "nosesstable.db")
    _mkdb(db_nosess, sessions_table=False)
    db_nullmodel = os.path.join(SANDBOX, "nullmodel.db")
    _mkdb(db_nullmodel, model=None, active_rows=0)
    db_otherkey = os.path.join(SANDBOX, "otherkey.db")
    _mkdb(db_otherkey, key="a2a:someone:else")
    db_broken = os.path.join(SANDBOX, "broken.db")
    io.open(db_broken, "wb").write(b"\x00\x01NOT-A-SQLITE-FILE" * 64)
    db_dir = os.path.join(SANDBOX, "dir.db")
    os.makedirs(db_dir)
    db_missing = os.path.join(SANDBOX, "no_such.db")

    # 无权限样本：Windows 走 icacls /deny，posix 走 chmod 000。造不出来就如实降级记账。
    db_denied = os.path.join(SANDBOX, "denied.db")
    _mkdb(db_denied)
    denied_ok = _deny_read(db_denied)

    sj_ok = os.path.join(SANDBOX, "sessions_ok.json")
    io.open(sj_ok, "w", encoding="utf-8", newline="\n").write(json.dumps(
        {OPENCLAW_KEY: {"totalTokens": 45678, "contextTokens": 200000, "other": 1},
         "agent:main:openai-user:a2a:other:x": {"totalTokens": 1, "contextTokens": 2}},
        ensure_ascii=False))
    sj_bad = os.path.join(SANDBOX, "sessions_bad.json")
    io.open(sj_bad, "w", encoding="utf-8", newline="\n").write("{ 这不是 JSON")
    sj_list = os.path.join(SANDBOX, "sessions_list.json")
    io.open(sj_list, "w", encoding="utf-8", newline="\n").write("[1, 2, 3]")
    sj_nokey = os.path.join(SANDBOX, "sessions_nokey.json")
    io.open(sj_nokey, "w", encoding="utf-8", newline="\n").write(json.dumps({"whatever": {}}))
    sj_notdict = os.path.join(SANDBOX, "sessions_notdict.json")
    io.open(sj_notdict, "w", encoding="utf-8", newline="\n").write(json.dumps({OPENCLAW_KEY: 42}))
    sj_missfield = os.path.join(SANDBOX, "sessions_missfield.json")
    io.open(sj_missfield, "w", encoding="utf-8", newline="\n").write(
        json.dumps({OPENCLAW_KEY: {"totalTokens": 100}}))
    sj_zerowin = os.path.join(SANDBOX, "sessions_zerowin.json")
    io.open(sj_zerowin, "w", encoding="utf-8", newline="\n").write(
        json.dumps({OPENCLAW_KEY: {"totalTokens": 100, "contextTokens": 0}}))
    sj_missing = os.path.join(SANDBOX, "sessions_no_such.json")

    tokfile = os.path.join(etc, "client-token")
    io.open(tokfile, "w", encoding="utf-8", newline="\n").write("fake-token-for-equiv-only\n")

    base = {"AGENT_TEAM_ETC_DIR": etc, "A2A_HERMES_HOME": hhome}

    def s(name, note, **over):
        env = dict(base)
        env.update(over)
        return (name, env, note)

    samples = [
        s("01 正常 db", "sessions 两行同 key + 3 条 active=1 消息（含 CJK/ASCII/NULL）",
          A2A_HERMES_STATE_DB=db_ok),
        s("02 无 active 消息", "口径B 无行 → 必须回退口径A", A2A_HERMES_STATE_DB=db_noactive),
        s("03 calls=0", "口径A 除零保护：必须 None 而不是崩", A2A_HERMES_STATE_DB=db_zerocalls),
        s("04 空库（有表无行）", "两口径都 None", A2A_HERMES_STATE_DB=db_empty),
        s("05 缺 messages 表", "口径B 抛 → 降级；口径A 照常", A2A_HERMES_STATE_DB=db_nomsg),
        s("06 缺 sessions 表", "两口径都抛 → 都降级", A2A_HERMES_STATE_DB=db_nosess),
        s("07 model 为 NULL", "拿不到分母 → None，不编造", A2A_HERMES_STATE_DB=db_nullmodel),
        s("08 session_key 不匹配", "查无此会话 → None", A2A_HERMES_STATE_DB=db_otherkey),
        s("09 坏文件（非 sqlite）", "connect 成功、execute 抛 → 降级", A2A_HERMES_STATE_DB=db_broken),
        s("10 路径不存在", "isfile 假 → None（不抛、不记日志）", A2A_HERMES_STATE_DB=db_missing),
        s("11 路径是目录", "isfile 假 → None", A2A_HERMES_STATE_DB=db_dir),
        s("12 无权限%s" % ("" if denied_ok else "（本机造不出，退化为正常 db）"),
          "打不开 → 降级为不自愈", A2A_HERMES_STATE_DB=db_denied),
        s("13 state.db 未配置", "env 全空 → None", ),
        s("14 sessions.json 正常", "openclaw 口径取 totalTokens/contextTokens",
          A2A_SESSIONS_JSON=sj_ok),
        s("15 sessions.json 坏 JSON", "解析抛 → 降级", A2A_SESSIONS_JSON=sj_bad),
        s("16 sessions.json 是 list", "store.get 抛 → 降级", A2A_SESSIONS_JSON=sj_list),
        s("17 sessions.json key 不在", "→ None", A2A_SESSIONS_JSON=sj_nokey),
        s("18 条目不是 dict", "→ None", A2A_SESSIONS_JSON=sj_notdict),
        s("19 缺 contextTokens", "字段缺失 → None", A2A_SESSIONS_JSON=sj_missfield),
        s("20 contextTokens=0", "分母非正 → None", A2A_SESSIONS_JSON=sj_zerowin),
        s("21 sessions.json 不存在", "→ None，且 backend_kind 兜底不判 openclaw",
          A2A_SESSIONS_JSON=sj_missing),
        s("22 db + sessions.json 齐备", "两条真源同时在，按 agent_cfg 分派",
          A2A_HERMES_STATE_DB=db_ok, A2A_SESSIONS_JSON=sj_ok),
        s("23 Hermes 配置：context_length 非数字", "解析成 0 → 走下一环",
          A2A_HERMES_STATE_DB=db_ok, A2A_HERMES_HOME=hhome_bad),
        s("24 Hermes 配置：没有 model 段", "→ ('', 0)", A2A_HERMES_STATE_DB=db_ok,
          A2A_HERMES_HOME=hhome_nomodel),
        s("25 Hermes 主目录无 config.yaml", "→ ('', 0)", A2A_HERMES_STATE_DB=db_ok,
          A2A_HERMES_HOME=hhome_empty),
        s("26 config.yaml 是目录", "isfile 假 → ('', 0)", A2A_HERMES_STATE_DB=db_ok,
          A2A_HERMES_HOME=hhome_cfgdir),
        s("27 Hermes 主目录 env 指向不存在的路径", "显式 env 原样返回（不探测、不兜底）",
          A2A_HERMES_HOME=os.path.join(SANDBOX, "no_such_home")),
        s("28 令牌走文件", "etc 目录里放了 client-token", CTX_A2A_TOKEN_FILE=""),
        s("29 令牌走 env", "env 优先于文件", CTX_A2A_TOKEN="env-token-xyz"),
    ]
    out("  样本沙箱：%s（开跑前已清空重建）" % norm(SANDBOX))
    out("  无权限样本造出来了吗：%s" % ("是" if denied_ok else "否 —— 本机权限模型不配合，已如实降级"))
    return samples


def _deny_read(path):
    """把文件设成当前用户不可读。造得出来返 True，造不出来返 False（不假装）。"""
    try:
        if os.name == "nt":
            user = os.environ.get("USERNAME") or ""
            if not user:
                return False
            subprocess.run(["icacls", path, "/inheritance:r"],
                           capture_output=True, check=False)
            subprocess.run(["icacls", path, "/deny", "%s:(R)" % user],
                           capture_output=True, check=False)
        else:
            os.chmod(path, 0)
        with io.open(path, "rb"):
            pass
        return False        # 还能读 → 没造成功
    except OSError:
        return True
    except Exception:       # noqa: BLE001
        return False


def _battery(au):
    """一批样本下把 agent_usage 的调用面全过一遍，返回 [(标签, cell)]。"""
    rows = [
        ("sessions_json_path()", probe(au.sessions_json_path)),
        ("hermes_state_db()", probe(au.hermes_state_db)),
        ("hermes_home()", probe(au.hermes_home)),
        ("effective_window_from_config()", probe(au.effective_window_from_config)),
        ("a2a_remote_base()", probe(au.a2a_remote_base)),
        ("window_for('deepseek-v4-flash','hermes')",
         probe(au.window_for, "deepseek-v4-flash", "hermes")),
        ("window_for('hermes-agent','hermes')", probe(au.window_for, "hermes-agent", "hermes")),
        ("window_for('hermes-agent','openclaw')", probe(au.window_for, "hermes-agent", "openclaw")),
        ("window_for('','hermes')", probe(au.window_for, "", "hermes")),
        ("backend_kind(hermes)", probe(au.backend_kind, CFG_HERMES)),
        ("backend_kind(openclaw)", probe(au.backend_kind, CFG_OPENCLAW)),
        ("backend_kind(other)", probe(au.backend_kind, CFG_OTHER)),
        ("backend_kind(None)", probe(au.backend_kind, None)),
        ("hermes_session_headers", probe(au.hermes_session_headers, CFG_HERMES, CALLER, SESSION)),
        ("_read_hermes_ctx_b（口径B）", probe(au._read_hermes_ctx_b, CALLER, SESSION, "")),
        ("_read_hermes_usage_a（口径A）", probe(au._read_hermes_usage_a, CALLER, SESSION, "")),
        ("read_hermes", probe(au.read_hermes, CALLER, SESSION, "")),
        ("read_openclaw", probe(au.read_openclaw, CALLER, SESSION)),
        ("read_usage(cfg=hermes)", probe(au.read_usage, CALLER, SESSION, CFG_HERMES)),
        ("read_usage(cfg=openclaw)", probe(au.read_usage, CALLER, SESSION, CFG_OPENCLAW)),
        ("read_usage(cfg=None)", probe(au.read_usage, CALLER, SESSION, None)),
        ("pressure(cfg=hermes)", probe(au.pressure, CALLER, SESSION, CFG_HERMES)),
        ("read_watermark(cfg=hermes)", probe(au.read_watermark, CALLER, SESSION, CFG_HERMES)),
        ("read_watermark(cfg=openclaw)", probe(au.read_watermark, CALLER, SESSION, CFG_OPENCLAW)),
        ("read_watermark(空 caller)", probe(au.read_watermark, "", SESSION, CFG_HERMES)),
    ]
    tok = probe(au.a2a_remote_token)
    rows.append(("a2a_remote_token()【掩码】", (tok[0], tok[1], mask(tok[2]))))
    return rows


def section_d(out, old_au, new_au, samples):
    out("D 段 · agent_usage 行为面（%d 份样本 × %d 条调用面；临时副本，绝不读生产 state.db）"
        % (len(samples), len(_battery(new_au))))
    bad = 0
    for name, env, note in samples:
        wipe(env)
        old_rows = _battery(old_au)
        wipe(env)
        new_rows = _battery(new_au)
        diffs = [(lab, o, n) for (lab, o), (_, n) in zip(old_rows, new_rows) if o != n]
        out("  %s %-46s %s" % ("✓" if not diffs else "✗", name, note))
        if diffs:
            bad += len(diffs)
            for lab, o, n in diffs:
                out("      · %s" % lab)
                out("        旧 | %s" % cell_str(o))
                out("        新 | %s" % cell_str(n))
        elif name.startswith(("01", "02", "05", "09", "14")):
            # 抽几份关键样本把实测值打出来，免得「全绿」看不出到底测到了什么
            for lab, cell in new_rows:
                if lab in ("_read_hermes_ctx_b（口径B）", "_read_hermes_usage_a（口径A）",
                           "read_openclaw", "read_usage(cfg=hermes)", "read_watermark(cfg=hermes)"):
                    out("        %-30s %s" % (lab, cell_str(cell)))
    wipe()
    out("  → 不一致 %d 处" % bad)
    return bad


# ============================================================ E 段 · 下沉净效果

def section_e(out, new_au, npf):
    """事实记账：句柄归属与只读性。"""
    checks = []
    conn = None
    dbp = os.path.join(SANDBOX, "ro_probe.db")
    _mkdb(dbp)
    try:
        conn = npf.open_hermes_state_ro(dbp)
        checks.append(("open_hermes_state_ro 真开出连接", conn is not None))
        wrote = "写不进去"
        try:
            conn.execute("INSERT INTO sessions VALUES (999,'x','y',1,1,1,'a','b')")
            wrote = "**写进去了**"
        except sqlite3.OperationalError:
            pass
        checks.append(("只读打开：写操作被 sqlite 直接拒（%s）" % wrote, wrote == "写不进去"))
    finally:
        if conn is not None:
            conn.close()
    checks.append(("未配置时 open_hermes_state_ro() 返回 None（不抛、不猜路径）",
                   npf.open_hermes_state_ro("") is None))
    checks.append(("路径不存在时返回 None",
                   npf.open_hermes_state_ro(os.path.join(SANDBOX, "nope.db")) is None))
    bads = probe(npf.hermes_session_usage_row, "k", os.path.join(SANDBOX, "broken.db"))
    checks.append(("坏文件 → L0 **抛**而不是静悄悄返 None（%s）" % cell_str(bads), bads[0] == "RAISE"))
    # 同一个坏文件，L2 必须把它吞成 None（降级口径留在 L2；L0 只抛不吞）
    wipe({"A2A_HERMES_STATE_DB": os.path.join(SANDBOX, "broken.db")})
    l2 = probe(new_au._read_hermes_usage_a, CALLER, SESSION, "")
    wipe()
    checks.append(("同一个坏文件，L2 agent_usage 吞成 None（%s）—— 降级口径留在 L2"
                   % cell_str(l2), l2 == ("VALUE", "NoneType", "None")))

    out("E 段 · 下沉净效果（%d 项）" % len(checks))
    bad = 0
    for label, ok in checks:
        out("  %s %s" % ("✓" if ok else "✗", label))
        if not ok:
            bad += 1
    return bad


# ============================================================ 负控

def apply_negative(kind, new_np, npf, new_au):
    """往**新侧**注一处私货，证明这套矩阵真有敏感度（不是恒绿）。"""
    if kind == "profile":
        # 把未登记主机的兜底 flavour 从 posix 改成 nt —— 正是「不编造真值」那条的反面
        npf.TARGET_FALLBACK_FLAVOUR = "nt"
        return "负控 profile：node_profile.TARGET_FALLBACK_FLAVOUR posix → nt（未登记主机改口径）"
    if kind == "usage":
        # 把口径A 的降级从「返回 None」改成「返回 (0, 1)」—— 正是「不编造数字」那条的反面
        orig = new_au._read_hermes_usage_a

        def fake(caller, session_id, model=""):
            got = orig(caller, session_id, model)
            return got if got is not None else (0, 1)
        new_au._read_hermes_usage_a = fake
        return "负控 usage：agent_usage._read_hermes_usage_a 读不到时返回 (0,1) 而非 None（编造数字）"
    raise SystemExit("未知负控种类：%s" % kind)


# ============================================================ 主流程

def run_once(neg=None):
    buf = []

    def out(line):
        buf.append(line)

    old_np, old_au, new_np, npf, new_au = load_sides()

    out("=" * 100)
    out("刀21 等价装置 · L0 节点档案正式化 + 宿主会话真源读取下沉")
    out("旧侧：_old/node_paths_old_k21.py（%s） + _old/agent_usage_old_k21.py（%s）"
        % (_md5(os.path.join(OLD_DIR, "node_paths_old_k21.py")),
           _md5(os.path.join(OLD_DIR, "agent_usage_old_k21.py"))))
    out("新侧：node/l0_host/node_profile.py + node_paths.py + node/l2_adapter/agent_usage.py（工作区活件）")
    if neg:
        out("！！负控模式：%s" % apply_negative(neg, new_np, npf, new_au))
    out("=" * 100)
    out("")

    total = 0
    total += section_a(out, old_np, new_np, npf)
    out("")
    total += section_b(out, old_np, new_np, npf)
    out("")
    total += section_c(out, old_np, new_np, npf, old_au, new_au)
    out("")
    samples = build_samples(out)
    total += section_d(out, old_au, new_au, samples)
    out("")
    total += section_e(out, new_au, npf)
    out("")
    out("=" * 100)
    out("合计不一致：%d" % total)
    out("EQUIV_SENTINEL_OK" if total == 0 else "EQUIV_SENTINEL_FAIL")
    return total, "\n".join(buf) + "\n"


def _md5(path):
    return "%d B md5 %s" % (os.path.getsize(path),
                            hashlib.md5(io.open(path, "rb").read()).hexdigest()[:12])


def main():
    ap = argparse.ArgumentParser(description="刀21 等价装置")
    ap.add_argument("--runs", type=int, default=1, help="连跑 N 轮自证确定性（默认 1）")
    ap.add_argument("--neg", choices=["profile", "usage"], default=None,
                    help="负控：往新侧注私货，本装置必须判红")
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
    return 0 if total == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
