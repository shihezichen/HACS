# -*- coding: utf-8 -*-
"""刀 22 等价装置 —— 「非 L0 件的宿主事实收口到 L0 节点档案」**零行为改变**证据。

用法
----
    python tools/equivalence/host_truth_equiv.py              # 复跑矩阵，全一致才 rc=0
    python tools/equivalence/host_truth_equiv.py --negative   # 负控：把新侧 L0 默认值改一个字符 → 必须判红
    python tools/equivalence/host_truth_equiv.py --gen        # 从改前树重生成 _old/ 基线（逐字节）

它证明什么（以及为什么不是自证）
--------------------------------
刀 22 把 15 处**非环回宿主事实**从 13 个 L1/L3 件收进 L0 `node_profile.py` 的 ⑧⑨ 两组。
本装置比的是：

  旧侧 —— `_old/host_truth_old_k22.py` 里那段**改前树 c529b32 的逐字节切片**，
          它自己带着字面量（`or "http://…"` / `or "/etc/nats"` / `or r"D:\\…"`）；
  新侧 —— **活树**里同一条语句的切片，它走 `node_profile.<fn>()`。

两侧在**同一组 env 场景**下各自 `exec` 求值，比**取到的值**。所以这不是「两边都调了同一个
L0 函数」的自证 —— 旧侧压根不认识 L0，它算的是改前那个字面量。

切片怎么取（不靠人抄，靠锚点）
------------------------------
每项声明一个**锚点**（赋值目标名，或 `add_argument("--flag"`）。两侧都按锚点定位、
按括号配平取完整语句。旧侧额外断言「命中行号 == 契约 §1 登记的改前行号」且
「切片与 `git show c529b32:<件>` 逐字节一致」（git 可用时），保证基线没被人手改过。

覆盖面
------
  · 19 项 × 10 组 env 场景 = 190 格（契约要求 ≥ 30 格）。
  · env 场景含：全无 / 全设 / 空串 / 纯空白 / 多值串 / 非法值 / 只设主名 / 只设别名 /
    posix 路径 / windows 路径 —— 覆盖每条 `or` 兜底链的每个分叉。
  · A2b·A3 的**多值形态**（`主:端口,备:端口`）既在「全无」场景走默认值，也在「多值」场景走 env。
  · B1·B2 的 **posix 路径**、B3 的 **windows 路径**同理，默认值与 env 两侧都量。

A12 是特例（唯一一处有意的可见变化）
------------------------------------
`team_broadcast.py` 的 `--nats-host` **只有 help 文本**里的示例地址从真地址改成形态占位，
参数名/必填性/默认值一律未动。所以它的判据是：
  ① 两侧 `default` 完全相同；
  ② 把旧 help 里的 IPv4 字面量替换成形态占位后，必须与新 help **逐字节相同**
     （即「除了那个示例地址，一个字都没改」）。

本装置源码里**不出现任何宿主字面量**（契约 §3）：要比对旧默认值一律从 `_old/` 取，
env 场景用的是 `.invalid` / `E:\\` 这类明显假值。
"""
from __future__ import annotations

import argparse
import io
import os
import re
import subprocess
import sys
import textwrap

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
AG = os.path.join(REPO, "agent-team")
OLD_DIR = os.path.join(HERE, "_old")
OLD_MOD = os.path.join(OLD_DIR, "host_truth_old_k22.py")

#: 改前树（契约 §1 的行号都是这棵树上的）
BASE_REV = "c529b32"

for p in (AG, OLD_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)


# ============================================================ 项目清单（契约 §1 逐条）
# kind: assign   —— 模块级赋值，锚点 = 赋值目标名，取值 = 该名字
#       argparse —— parser.add_argument(...)，锚点 = "--flag"，取值 = 该参数的 default
# cmp:  value     —— 比「取到的值」（默认）
#       help_shape—— A12 专用：default 相同 + help 只换示例地址（见模块文档串）
ITEMS = [
    dict(key="A1",  grp="A", file="agent-team/blackboard_view.py",           layer="L3", kind="assign",   anchor="AGENT_TEAM_BB_BASE",   old_line=116,  what="黑板基址兜底"),
    dict(key="A2a", grp="A", file="agent-team/group_watch.py",               layer="L3", kind="assign",   anchor="AGENT_TEAM_CHAT_BASE", old_line=41,   what="对话台基址兜底"),
    dict(key="A2b", grp="A", file="agent-team/group_watch.py",               layer="L3", kind="assign",   anchor="AGENT_TEAM_NATS_HOST", old_line=43,   what="NATS 集群串兜底（多值）"),
    dict(key="A3",  grp="A", file="agent-team/team_cli_core.py",             layer="L1", kind="assign",   anchor="AGENT_TEAM_NATS_HOST", old_line=41,   what="NATS 集群串兜底（多值）"),
    dict(key="A4a", grp="A", file="agent-team/roundtable.py",                layer="L3", kind="argparse", anchor="--blackboard",         old_line=2283, what="--blackboard 默认值"),
    dict(key="A4b", grp="A", file="agent-team/roundtable.py",                layer="L3", kind="argparse", anchor="--nats-host",          old_line=2287, what="--nats-host 默认值"),
    dict(key="A5",  grp="A", file="agent-team/session_ctx_archiver.py",      layer="L3", kind="assign",   anchor="CHAT_BASE_DEFAULT",    old_line=67,   what="对话台基址兜底"),
    dict(key="A6",  grp="A", file="agent-team/team_group.py",                layer="L3", kind="assign",   anchor="DEFAULT_CHAT_BASE",    old_line=50,   what="对话台基址兜底（双 env 链）"),
    dict(key="A7",  grp="A", file="agent-team/team_call.py",                 layer="L1", kind="assign",   anchor="DEFAULT_HOST",         old_line=29,   what="NATS 中枢兜底（单值）"),
    dict(key="A8",  grp="A", file="agent-team/team_roster.py",               layer="L1", kind="assign",   anchor="DEFAULT_HOST",         old_line=27,   what="NATS 中枢兜底（单值）"),
    dict(key="A9",  grp="A", file="agent-team/task_svc.py",                  layer="L3", kind="assign",   anchor="CHAT_BASE_DEFAULT",    old_line=68,   what="对话台基址兜底（CHAT_BASE 链）"),
    dict(key="A10", grp="A", file="agent-team/rt_remind.py",                 layer="L3", kind="argparse", anchor="--nats-host",          old_line=552,  what="--nats-host 默认值"),
    dict(key="A11", grp="A", file="agent-team/group_transcript_archiver.py", layer="L3", kind="assign",   anchor="CHAT_BASE_DEFAULT",    old_line=33,   what="对话台基址兜底"),
    dict(key="A12", grp="A", file="agent-team/team_broadcast.py",            layer="L1", kind="argparse", anchor="--nats-host",          old_line=165,  what="help 里的示例地址形态", cmp="help_shape"),
    dict(key="B1a", grp="B", file="agent-team/gen_nats_conf.py",             layer="L1", kind="assign",   anchor="AGENT_TEAM_NATS_DIR",  old_line=21,   what="NATS 配置目录兜底（posix）"),
    dict(key="B1b", grp="B", file="agent-team/gen_nats_conf.py",             layer="L1", kind="assign",   anchor="ROUTE_PASS_FILE",      old_line=23,   what="路由凭据文件（posix 绝对路径）"),
    dict(key="B2a", grp="B", file="agent-team/task_sys/task_svc.py",         layer="L3", kind="assign",   anchor="DB_DEFAULT",           old_line=35,   what="task_svc 库文件（posix 绝对路径）"),
    dict(key="B2b", grp="B", file="agent-team/task_sys/task_svc.py",         layer="L3", kind="assign",   anchor="LOG_DEFAULT",          old_line=36,   what="task_svc 日志（posix 绝对路径）"),
    dict(key="B3",  grp="B", file="agent-team/cli_backend.py",               layer="L2", kind="assign",   anchor="CODEX_WORKDIR",        old_line=102,  what="codex 工作目录（windows 绝对路径）"),
]


# ============================================================ env 场景（≥8 组，契约 §3）
#: 场景里用到的全部 env —— 每格开跑前一律先清干净，不许串场。
WIPE = ["AGENT_TEAM_BB_BASE", "AGENT_TEAM_CHAT_BASE", "TEAM_CHAT_BASE", "CHAT_BASE",
        "AGENT_TEAM_NATS_HOST", "AGENT_TEAM_NATS", "AGENT_TEAM_NATS_DIR",
        "AGENT_TEAM_CODEX_WORKDIR", "CODEX_WORKDIR", "AGENT_TEAM_RUNTIME_DIR"]

#: 明显假的样本值（不是宿主真值，装置源码里不许出现真地址/真路径）
_FAKE_CHAT = "http://chat.example.invalid:1"
_FAKE_BB = "http://bb.example.invalid:2"
_FAKE_ONE = "node-a.example.invalid"
_FAKE_MULTI = "node-a.example.invalid:1,node-b.example.invalid:2,node-c.example.invalid:3"
_FAKE_POSIX_DIR = "/srv/k22/nats"
_FAKE_POSIX_DIR2 = "/srv/k22/runtime"
_FAKE_WIN_DIR = "E:\\k22\\codex_workspace"
_WEIRD = "??? not a url ???"
_BLANK = "   "

SCENARIOS = [
    ("none", {}),
    ("all-set", {"AGENT_TEAM_BB_BASE": _FAKE_BB, "AGENT_TEAM_CHAT_BASE": _FAKE_CHAT,
                 "TEAM_CHAT_BASE": _FAKE_CHAT + "/x", "CHAT_BASE": _FAKE_CHAT + "/y",
                 "AGENT_TEAM_NATS_HOST": _FAKE_ONE, "AGENT_TEAM_NATS": _FAKE_MULTI,
                 "AGENT_TEAM_NATS_DIR": _FAKE_POSIX_DIR,
                 "AGENT_TEAM_CODEX_WORKDIR": _FAKE_WIN_DIR, "CODEX_WORKDIR": _FAKE_WIN_DIR + "2",
                 "AGENT_TEAM_RUNTIME_DIR": _FAKE_POSIX_DIR2}),
    ("empty", {k: "" for k in WIPE}),
    ("blank", {k: _BLANK for k in WIPE}),
    ("multi", {"AGENT_TEAM_NATS_HOST": _FAKE_MULTI, "AGENT_TEAM_NATS": _FAKE_MULTI}),
    ("weird", {k: _WEIRD for k in WIPE}),
    ("primary-only", {"AGENT_TEAM_CHAT_BASE": _FAKE_CHAT, "AGENT_TEAM_NATS_HOST": _FAKE_ONE,
                      "AGENT_TEAM_CODEX_WORKDIR": _FAKE_WIN_DIR}),
    ("alias-only", {"TEAM_CHAT_BASE": _FAKE_CHAT, "CHAT_BASE": _FAKE_CHAT,
                    "AGENT_TEAM_NATS": _FAKE_MULTI, "CODEX_WORKDIR": _FAKE_WIN_DIR}),
    ("posix-paths", {"AGENT_TEAM_NATS_DIR": _FAKE_POSIX_DIR,
                     "AGENT_TEAM_RUNTIME_DIR": _FAKE_POSIX_DIR2}),
    ("win-paths", {"AGENT_TEAM_CODEX_WORKDIR": _FAKE_WIN_DIR,
                   "CODEX_WORKDIR": _FAKE_WIN_DIR + "2"}),
]

IPV4_RX = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")


# ============================================================ 切片提取
def _read(path):
    with io.open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _git_show(rel):
    """改前树的件内容；git 不可用 / 对象不在时返回 None（只影响自检，不影响比对）。"""
    try:
        out = subprocess.run(["git", "show", "%s:%s" % (BASE_REV, rel)], cwd=REPO,
                             capture_output=True, timeout=60)
    except Exception:                                   # noqa: BLE001 —— 没 git 就跳过自检
        return None
    if out.returncode != 0:
        return None
    return out.stdout.decode("utf-8")


def _anchor_rx(item):
    if item["kind"] == "assign":
        return re.compile(r"^%s\s*=" % re.escape(item["anchor"]))
    return re.compile(r"add_argument\(\s*[\"']%s[\"']" % re.escape(item["anchor"]))


def _balanced(lines, start, where):
    """从第 start 行起按括号配平取完整语句，返回 (切片文本, 结束行下标)。"""
    depth, buf = 0, []
    for i in range(start, len(lines)):
        line = lines[i]
        buf.append(line)
        # 粗略但够用：本仓这几条语句里不含带半角括号的字符串字面量（中文括号不计）
        depth += line.count("(") + line.count("[") - line.count(")") - line.count("]")
        if depth <= 0 and buf:
            return "\n".join(buf) + "\n", i
    raise AssertionError("括号没配平：%s 第 %d 行起" % (where, start + 1))


def slice_of(src, item, where):
    """按锚点在 src 里取唯一一条完整语句 → (切片文本, 1 起行号)。"""
    rx = _anchor_rx(item)
    lines = src.splitlines()
    hits = [i for i, ln in enumerate(lines) if rx.search(ln)]
    if len(hits) != 1:
        raise AssertionError("%s 在 %s 里锚点「%s」命中 %d 处（要求恰好 1 处）"
                             % (item["key"], where, item["anchor"], len(hits)))
    text, _end = _balanced(lines, hits[0], where)
    return text, hits[0] + 1


# ============================================================ 基线生成（--gen）
def gen_old():
    """从改前树逐字节切片，写出 `_old/host_truth_old_k22.py`。"""
    missing = []
    body = []
    for item in ITEMS:
        src = _git_show(item["file"])
        if src is None:
            missing.append(item["file"])
            continue
        text, line = slice_of(src, item, "%s:%s" % (BASE_REV, item["file"]))
        assert line == item["old_line"], \
            "%s 改前行号对不上：契约说 L%d，实测 L%d" % (item["key"], item["old_line"], line)
        body.append("    %r: %r," % (item["key"], text))
    if missing:
        raise SystemExit("git show 取不到：%s" % "、".join(sorted(set(missing))))
    head = (
        "# -*- coding: utf-8 -*-\n"
        '"""刀 22 基线 —— 改前树 %s 的**逐字节切片**（由 host_truth_equiv.py --gen 生成，勿手改）。\n'
        "\n"
        "每条 = 契约 §1 清单里那一项在改前树上的完整语句原文（含它自己的宿主字面量）。\n"
        "装置用它当「旧实现」：`exec` 进一个只有 `os` / `argparse` 的干净命名空间里求值，\n"
        "所以它算出来的是**改前那个字面量**，与活树走 L0 的结果相互独立。\n"
        '"""\n'
        "import argparse\n"
        "import os\n"
        "import textwrap\n"
        "\n"
        "#: 改前树的 commit\n"
        "BASE_REV = %r\n"
        "\n"
        "SLICES = {\n" % (BASE_REV, BASE_REV))
    tail = (
        "}\n"
        "\n"
        "\n"
        "def evaluate(key, kind, name):\n"
        '    """在**当前 env** 下求值一条旧切片。命名空间只给 os / argparse —— 旧件不认识 L0。\n'
        "\n"
        "    切片按**原文逐字节**存着（含缩进），求值前只做 `textwrap.dedent`（整体退格，\n"
        "    不改任何一个有效字符），否则 argparse 那几条带缩进的语句 exec 不了。\n"
        '    """\n'
        "    ns = {\"os\": os, \"argparse\": argparse}\n"
        "    code = textwrap.dedent(SLICES[key])\n"
        "    if kind == \"argparse\":\n"
        "        parser = argparse.ArgumentParser(add_help=False)\n"
        "        ns[\"parser\"] = ns[\"ap\"] = parser\n"
        "        exec(code, ns)                            # noqa: S102 —— 跑的是自己仓里的冻结切片\n"
        "        action = parser._actions[-1]\n"
        "        return action.default, action.help\n"
        "    exec(code, ns)                                # noqa: S102\n"
        "    return ns[name], None\n")
    with io.open(OLD_MOD, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(head + "\n".join(body) + "\n" + tail)
    print("已写 %s（%d 条切片，源 = %s）" % (os.path.relpath(OLD_MOD, REPO), len(body), BASE_REV))
    return 0


# ============================================================ 新侧求值
_NEW_SLICE_CACHE = {}


def new_slice(item):
    """活树里同一条语句的切片（按锚点定位，缓存一次）。"""
    key = item["key"]
    if key not in _NEW_SLICE_CACHE:
        src = _read(os.path.join(REPO, item["file"]))
        _NEW_SLICE_CACHE[key] = slice_of(src, item, "活树 " + item["file"])
    return _NEW_SLICE_CACHE[key]


def eval_new(item, npf):
    """在**当前 env** 下求值活树的同一条语句。命名空间给 os / argparse / `_npf`（L0 节点档案）。"""
    text, _line = new_slice(item)
    code = textwrap.dedent(text)
    ns = {"os": os, "argparse": argparse, "_npf": npf}
    if item["kind"] == "argparse":
        parser = argparse.ArgumentParser(add_help=False)
        ns["parser"] = ns["ap"] = parser
        exec(code, ns)                                     # noqa: S102 —— 跑的是自己仓里的件
        action = parser._actions[-1]
        return action.default, action.help
    exec(code, ns)                                         # noqa: S102
    return ns[item["anchor"]], None


# ============================================================ 负控
def poison(npf):
    """把新侧 L0 的**每个**默认值函数返回值改掉一个字符 —— 装置必须因此判红。"""
    names = ["nats_host_default", "nats_hosts_default", "blackboard_base_default",
             "chat_base_default", "nats_host_shape", "nats_conf_dir_posix",
             "nats_route_pass_file", "task_svc_db_default", "task_svc_log_default",
             "codex_workdir_default"]
    for name in names:
        orig = getattr(npf, name)

        def bumped(_orig=orig):
            v = _orig()
            #: 末位字符 +1（ASCII），只动一个字符
            return v[:-1] + chr(ord(v[-1]) + 1) if v else "x"

        setattr(npf, name, bumped)


# ============================================================ 自检：基线没被手改
def check_baseline(old):
    """每条旧切片必须与 `git show <改前树>:<件>` 逐字节一致（git 不可用则跳过并如实说）。"""
    results = []
    for item in ITEMS:
        src = _git_show(item["file"])
        if src is None:
            results.append((item["key"], None, "git 不可用/对象不在，跳过"))
            continue
        text, line = slice_of(src, item, "%s:%s" % (BASE_REV, item["file"]))
        ok = (text == old.SLICES[item["key"]]) and (line == item["old_line"])
        results.append((item["key"], ok,
                        "L%d（契约 L%d）" % (line, item["old_line"])))
    return results


# ============================================================ 主流程
def run(negative=False):
    import node_profile as npf
    import host_truth_old_k22 as old

    if negative:
        poison(npf)

    rows, bad = [], 0
    for scen, env in SCENARIOS:
        for item in ITEMS:
            for k in WIPE:
                os.environ.pop(k, None)
            for k, v in env.items():
                os.environ[k] = v
            o_val, o_help = old.evaluate(item["key"], item["kind"], item["anchor"])
            n_val, n_help = eval_new(item, npf)

            if item.get("cmp") == "help_shape":
                #: A12：default 必须一样；help 只许「示例地址 → 形态占位」这一处差异。
                #: 形态取 L0 的**常量**而非函数 —— 负控注的是函数，常量不动，A12 才会跟着判红。
                same = (o_val == n_val) and \
                    (IPV4_RX.sub(npf.NODE_ADDR_SHAPE, o_help or "") == (n_help or ""))
                shown = "default=%r help=%r" % (n_val, n_help)
            else:
                same = (o_val == n_val)
                shown = repr(n_val)
            if not same:
                bad += 1
            rows.append((scen, item["key"], same, shown, repr(o_val), repr(n_val)))
    for k in WIPE:
        os.environ.pop(k, None)
    return rows, bad, old


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gen", action="store_true", help="从改前树重生成 _old/ 基线")
    ap.add_argument("--negative", action="store_true", help="负控：新侧默认值改一个字符，必须判红")
    ap.add_argument("--quiet", action="store_true", help="只打不一致的格与汇总")
    args = ap.parse_args()

    if args.gen:
        return gen_old()

    rows, bad, old = run(negative=args.negative)

    print("=" * 108)
    print("刀22 · 宿主事实收口 · 等价矩阵（%d 项 × %d 场景 = %d 格）%s"
          % (len(ITEMS), len(SCENARIOS), len(rows), "   【负控注毒中】" if args.negative else ""))
    print("旧侧 = _old/host_truth_old_k22.py（改前树 %s 逐字节切片，自带字面量）" % old.BASE_REV)
    print("新侧 = 活树同一条语句（走 L0 node_profile 的 ⑧⑨ 两组）")
    print("=" * 108)

    cur = None
    for scen, key, same, shown, o_repr, n_repr in rows:
        if args.quiet and same:
            continue
        if scen != cur:
            print("\n[%s]" % scen)
            cur = scen
        print("  %s %-4s %s" % ("✓" if same else "✗", key, shown if len(shown) < 90 else shown[:87] + "…"))
        if not same:
            print("       旧 %s" % o_repr)
            print("       新 %s" % n_repr)

    print("\n[基线自检] 每条旧切片 vs git show %s:<件>" % old.BASE_REV)
    skipped = 0
    for key, ok, note in check_baseline(old):
        if ok is None:
            skipped += 1
            print("  · %-4s %s" % (key, note))
            continue
        if not ok:
            bad += 1
        print("  %s %-4s 逐字节一致 %s" % ("✓" if ok else "✗", key, note))

    print("\n" + "-" * 108)
    print("合计 %d 格%s，不一致 %d 处" % (len(rows), ("（基线自检跳过 %d 条）" % skipped) if skipped else "", bad))
    if args.negative:
        ok = bad > 0
        print("负控判定：%s（注毒后必须判红）" % ("通过 —— 已判红" if ok else "**失败 —— 竟然还是绿的**"))
        return 0 if ok else 1
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
