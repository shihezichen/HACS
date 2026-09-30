#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T 工具面：**「档案 + 凭据探针 ⇒ 中枢用户区」的逐字节自证**（阶段5 第三刀 · 刀27）。

它证明什么
----------
阶段5 的收口刀要让生成器**按档案自动写 NATS 配置**。在那之前必须先回答一个问题：

    L0 名册档案 `agent-team/member_roster.py` 里那张表，加上各成员**自己那台机**上的
    凭据文件，**够不够**唯一决定现网中枢 `nats.conf` 的用户区？

「够」不是靠读代码相信的，是靠**逐字节比**证出来的。本件就是那把尺子::

    推导侧 = 档案的有序成员表 + 各成员凭据文件的内容（探针读）
            → 按 `gen_nats_conf.py` 的行格式渲染出「用户区」文本
    现网侧 = 两台中枢 <conf_dir>/nats.conf 里的「用户区」区块（**原样抽出**，不重排不 strip）
    判据   = 两侧归一化后 **逐字节相等**（两个中枢都必须一致，差异 0 行）

证成立 → 收口刀的切换是「已知等价的替换」；证不成立 → 后面两刀就不该动手。

本刀**不改生成器、不改对账器、不动 NATS**。把生成器切成档案驱动是下一刀的事，
而且它还卡在一个**已量出的缺口**上：中枢机上的生成器拿不到全体成员的凭据 ——
现有凭据是**分布在各成员自己那台机**上的。详见 `agent-team/docs/l5-roster-archive.md` §缺口。

「用户区」的定义（**冻结**）
----------------------------
从**含 `users = [` 的那个 `authorization {` 行**起，到与之配对的 `}` 行止（含首尾两行）。
抽出时**原样保留** —— 不重排、不 strip 行内空白。
`cluster { ... authorization { user: "route" ... } }` 那一行里没有 `users = [`，
所以集群路由凭据天然落在区外，**一个字节都不会进本件的输入**。

归一化（**只许两处，其余一个字都不许归一**）
--------------------------------------------
  1. ``password: "<明文>"``  →  ``password: "sha256:<前 12 位十六进制>"``
  2. ``# 由 gen_nats_conf.py 生成于 <时间戳>（改配置请改脚本再重跑，别手改本文件）``
     →  ``# 由 gen_nats_conf.py 生成于 <ts>``（时间戳是天然漂移）

第 2 条在「用户区」上**永远不会命中** —— 那行时间戳在文件第 4 行，不在用户区内。
它仍然写进归一化器并冻结在契约里：归一化器是**对文本**的，不是对某个区块的；
将来若把对照面扩到整份 conf，口径不必再议一次。

口令一个字节都不出门
--------------------
明文**只存在于进程内存**：采集回来的凭据内容与现网 conf 文本都不进报告。
出口（stdout / `--json` / `--out` / 人读输出 / 异常信息）**只允许** `sha256:<前12>` 形态 ——
`analyze()` 在把任何文本放进报告之前先过归一化器，所以报告里不可能出现明文。

用法
----
    python tools/roster_derive_conf.py --live [--json] [--out FILE]
    python tools/roster_derive_conf.py --fixtures <DIR> [--json]

退出码
------
    0  两个中枢的用户区与推导侧**逐字节一致**（归一化后）
    1  **有差异**（逐 hub 打印差异行数与逐行差异）
    2  **采集失败**（ssh 不通 / conf 读不到 / 凭据文件缺失或读不到 / 夹具缺件）

采集失败**绝不退化成 0 或 1** —— 「没量到」和「量到了、一致/不一致」是三件事。
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)
AGENT_TEAM = os.path.join(PROJECT_ROOT, "agent-team")
TARGETS_PATH = os.path.join(HERE, "deploy_targets.json")

# 批① 起被搬件按绝对包名 import，故仓根一并入路径。
for _hacs_p in (PROJECT_ROOT, AGENT_TEAM):
    if _hacs_p not in sys.path:
        sys.path.insert(0, _hacs_p)

import node_profile                                    # noqa: E402 —— L0 节点档案（宿主事实唯一真源）
import member_roster                                   # noqa: E402 —— L0 名册档案（成员真源）

EXIT_OK, EXIT_DIFF, EXIT_COLLECT = 0, 1, 2

#: 输出契约版本（**顶层键名冻结**；改形状必须进版本号）
SCHEMA = "l5-roster-archive/1"

#: 中枢清单（**节点名**，不是地址）。ssh 目标经 `tools/deploy_targets.json` 解析 → 本件零宿主字面量。
HUBS_DEFAULT = ("lobster", "top001")

#: 远端命令的 PATH —— 自己钉死（刀23 实测：本机 MSYS ssh 起的远端 shell 未必含 /usr/bin）
REMOTE_PATH = "export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$PATH; "
#: 远端命令的退出码回传标记（ssh 自己的 rc 区分不了「文件不在」与「连不上」）
RC_MARK = "K27RC="

_ENV_RX = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::([^}]*))?\}")


# ============================================================ 小工具

def _expand(value):
    """展开 `${VAR:默认值}`（与 deploy.py / nats_membership_audit.py 同一口径）。"""
    if not isinstance(value, str):
        return value

    def sub(m):
        return os.environ.get(m.group(1)) or (m.group(2) or "")
    return _ENV_RX.sub(sub, value)


def sha256_12(text):
    """字符串的 sha256 **前 12 位十六进制** —— 口令唯一被允许的出门形态。"""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:12]


def md5_8(text):
    """文本的 md5 前 8 位（供人眼对数；比对判定用的是逐字节相等，不是这个摘要）。"""
    return hashlib.md5((text or "").encode("utf-8")).hexdigest()[:8]


def canonical(obj):
    """规范 JSON：键名排序、缩进固定、不转义非 ASCII —— 供逐字节比对。"""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, indent=2) + "\n"


# ============================================================ 用户区：抽出 / 推导 / 归一

_USERS_LINE_RX = re.compile(r"^\s*users\s*=\s*\[")
_AUTH_LINE_RX = re.compile(r"^\s*authorization\b[^{]*\{")


def _scan_braces(line, depth, in_quote):
    """逐字符走一行，返回 `(新 depth, 新 in_quote)`。引号内的括号不计。"""
    i, n = 0, len(line)
    while i < n:
        ch = line[i]
        if in_quote:
            if ch == "\\":
                i += 2
                continue
            if ch == in_quote:
                in_quote = ""
        elif ch in "\"'":
            in_quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        i += 1
    return depth, in_quote


def extract_users_block(text):
    """`nats.conf` 文本 → `(用户区文本, 说明)`；抽不出时 `(None, 为什么)`。

    口径（**冻结**）：先定位第一条 `users = [` 所在行，再**向上回溯**到最近的
    `authorization ... {` 行作为首行，然后从该行起按括号配平找到配对的 `}` 行作为末行。
    首尾两行都**含在**结果里，行内空白**原样保留**。

    为什么是「先找 users 再回溯」而不是「找第一个 authorization」：`cluster {}` 里那条
    单行 `authorization { user: "route" … }` 出现得更早，正向找会抓到路由凭据。
    """
    lines = (text or "").split("\n")
    start = None
    for i, line in enumerate(lines):
        if _USERS_LINE_RX.match(line):
            start = i
            break
    if start is None:
        return None, "配置里找不到 users = [ 区块"
    head = None
    for i in range(start, -1, -1):
        if _AUTH_LINE_RX.match(lines[i]):
            head = i
            break
    if head is None:
        return None, "users = [ 之上找不到 authorization { 行"
    depth, in_quote = 0, ""
    for i in range(head, len(lines)):
        depth, in_quote = _scan_braces(lines[i], depth, in_quote)
        if depth == 0:
            return "\n".join(lines[head:i + 1]), ""
    return None, "authorization { 的花括号在文件末尾仍未配平"


#: 用户区的固定首尾（与 `agent-team/gen_nats_conf.py:188-190,195-196` 逐字符同形）
BLOCK_HEAD = ("authorization {", "  timeout: 3", "  users = [")
BLOCK_TAIL = ("  ]", "}")


def derive_users_block(roster_rows, passwords, perm_fn):
    """档案的有序成员表 + 各成员口令 + 注入的角色模板 → **期望用户区文本**。

    `roster_rows` = `[(member, role, cred_node, cred_kind)]`，**顺序即输出顺序**（档案负责规范序）。
    `passwords`   = `{member: 明文}`（探针读回来的凭据内容）。
    `perm_fn`     = 角色模板的唯一出处；正件在 `main()` 里传 L1 `gen_nats_conf.perm_list`，
                    装置传它自己那份独立实现 —— 所以本函数**不许**自己 import 它（那就成自证了）。

    行格式与 `gen_nats_conf.py:191-195` 逐字符同形：4 空格缩进、`password:` 后一个空格、
    末条无逗号其余带逗号。
    """
    lines = list(BLOCK_HEAD)
    total = len(roster_rows)
    for idx, row in enumerate(roster_rows):
        member = row[0]
        comma = "," if idx < total - 1 else ""
        lines.append('    { user: "%s", password: "%s", permissions: { %s } }%s'
                     % (member, passwords.get(member, ""), perm_fn(member), comma))
    lines.extend(BLOCK_TAIL)
    return "\n".join(lines)


_PW_RX = re.compile(r'(password:\s*")([^"]*)(")')
_STAMP_RX = re.compile(
    r"^# 由 gen_nats_conf\.py 生成于 .*（改配置请改脚本再重跑，别手改本文件）$")
#: 归一化后时间戳行的固定形态
STAMP_NORMALIZED = "# 由 gen_nats_conf.py 生成于 <ts>"


def normalize(text):
    """归一化 —— **只有两处**（见模块文档串）。其余一个字都不动。

    这是口令的**唯一出口闸**：任何文本在进报告之前都要先过它，
    所以报告里不可能出现 `password:` 的明文。
    """
    out = []
    for line in (text or "").split("\n"):
        if _STAMP_RX.match(line):
            out.append(STAMP_NORMALIZED)
            continue
        out.append(_PW_RX.sub(lambda m: m.group(1) + "sha256:" + sha256_12(m.group(2)) + m.group(3),
                              line))
    return "\n".join(out)


def diff_lines(expected, actual):
    """两段**已归一化**文本的逐行差异 → `[{"line": 1 起, "expected":…, "actual":…}]`。

    一侧短了就补 `None`（JSON null）—— 「这一侧根本没有这一行」与「这一行是空串」是两回事。
    """
    a = expected.split("\n")
    b = actual.split("\n")
    out = []
    for i in range(max(len(a), len(b))):
        left = a[i] if i < len(a) else None
        right = b[i] if i < len(b) else None
        if left != right:
            out.append({"line": i + 1, "expected": left, "actual": right})
    return out


# ============================================================ analyze —— 纯函数

def analyze(collected, perm_fn):
    """`collected` + **注入的角色模板函数** → 报告（纯函数：无 IO、无 time、无随机）。

    `collected` 形状::

        {"hub_list": [中枢节点名],
         "roster":   [(member, role, cred_node, cred_kind)],   # 有序
         "conf":     {中枢名: nats.conf 全文 | None},
         "creds":    {成员名: 口令明文},                        # 只在内存里走一趟
         "errors":   [{"kind","node","what","why"}]}

    **口令出口闸**：本函数把推导侧与现网侧都先 `normalize()` 再放进报告，
    所以返回值里不可能有明文。
    """
    hubs = list(collected.get("hub_list") or [])
    roster_rows = [tuple(r) for r in (collected.get("roster") or [])]
    conf = collected.get("conf") or {}
    creds = collected.get("creds") or {}
    errors = list(collected.get("errors") or [])

    roster_out = [{"member": m, "role": role, "cred_node": node, "cred_kind": kind}
                  for m, role, node, kind in roster_rows]

    missing_cred = [m for m, _r, _n, _k in roster_rows if m not in creds]
    expected_norm = None
    if roster_rows and not missing_cred:
        expected_norm = normalize(derive_users_block(roster_rows, creds, perm_fn))

    per_hub = []
    for hub in hubs:
        row = {"hub": hub,
               "expected_md5_8": md5_8(expected_norm) if expected_norm is not None else None,
               "expected_lines": len(expected_norm.split("\n")) if expected_norm is not None else 0,
               "actual_md5_8": None, "actual_lines": 0,
               "diff_count": 0, "diffs": [], "ok": False, "why": ""}
        text = conf.get(hub)
        if text is None:
            row["why"] = "没有采到这台中枢的 nats.conf"
            per_hub.append(row)
            continue
        block, why = extract_users_block(text)
        if block is None:
            row["why"] = why
            per_hub.append(row)
            continue
        actual_norm = normalize(block)
        row["actual_md5_8"] = md5_8(actual_norm)
        row["actual_lines"] = len(actual_norm.split("\n"))
        if expected_norm is None:
            row["why"] = ("推导侧不完整：缺 %d 个成员的凭据" % len(missing_cred)
                          if missing_cred else "名册为空，推导不出用户区")
            per_hub.append(row)
            continue
        diffs = diff_lines(expected_norm, actual_norm)
        row["diffs"] = diffs
        row["diff_count"] = len(diffs)
        row["ok"] = not diffs
        row["why"] = "" if not diffs else "与推导侧有 %d 行不同" % len(diffs)
        per_hub.append(row)

    collect_errors = sorted(errors,
                            key=lambda it: json.dumps(it, sort_keys=True, ensure_ascii=False))
    return {
        "schema": SCHEMA,
        "hub_list": sorted(hubs),
        "roster": roster_out,
        "per_hub": sorted(per_hub, key=lambda r: r["hub"]),
        "collect_errors": collect_errors,
        "ok": bool(hubs) and (not collect_errors) and all(r["ok"] for r in per_hub),
    }


# ============================================================ 路径（真值全部来自 L0 档案 / 部署表）

def load_targets():
    """内部站点真值表（节点名 → transport / ssh_target / runtime_dir）。"""
    if not os.path.isfile(TARGETS_PATH):
        return {}
    with io.open(TARGETS_PATH, encoding="utf-8") as fh:
        data = json.load(fh) or {}
    return {t["name"]: t for t in (data.get("targets") or []) if t.get("name")}


def conf_dir_of(node, target):
    """该节点上 NATS 配置目录的绝对路径（nt 节点落运行目录下的 `nats/`；posix 走 L0 惯例值）。"""
    runtime = _expand(target.get("runtime_dir") or "")
    if node_profile.target_flavour(node) == "nt":
        return (runtime.rstrip("/\\") + "/nats") if runtime else ""
    return node_profile.nats_conf_dir_posix()


def cred_path(node, target, member, kind):
    """档案里的 `(cred_node, cred_kind)` → 该机上凭据文件的**绝对路径**。

    档案只说「哪台机 + 哪一类位置」，拼路径是这里的事 —— 位置面来自 L0 `node_profile`
    与部署表，所以**档案里一个路径字面量都没有**，本件里也没有。
    """
    if kind == member_roster.CRED_RUNTIME:
        runtime = _expand(target.get("runtime_dir") or "")
        return (runtime.rstrip("/\\") + "/nats-%s.pass" % member) if runtime else ""
    if kind == member_roster.CRED_CONF:
        base = conf_dir_of(node, target)
        return (base.rstrip("/\\") + "/%s.pass" % member) if base else ""
    return ""


def fixture_cred_name(member, kind):
    """夹具里凭据文件的文件名（与真机的文件名惯例同形）。"""
    if kind == member_roster.CRED_RUNTIME:
        return "nats-%s.pass" % member
    return "%s.pass" % member


# ============================================================ 采集面 A：真机（**只读**）

def _guarded(cmd):
    """把远端命令包一层：**内层 rc 回传** + 末尾垫空行（防无尾换行的文件把标记粘上去）。"""
    return REMOTE_PATH + "RC=0; " + cmd + " 2>/dev/null || RC=$?; echo; echo " + RC_MARK + "$RC"


def _split_rc(out):
    """`(内层 rc, 内容)`；取不到标记说明命令根本没跑起来 → `(None, 原文)`。"""
    lines = (out or "").split("\n")
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].startswith(RC_MARK):
            try:
                rc = int(lines[i][len(RC_MARK):].strip())
            except ValueError:
                return None, out
            body = "\n".join(lines[:i])
            return rc, body[:-1] if body.endswith("\n") else body
    return None, out


def run_remote(target, cmd, timeout=40.0):
    """经 ssh 在该节点上跑一条**只读**命令 → `(内层 rc, 内容, 说明)`。

    远端命令**原样传、不加引号**（刀23 实测：本机 ssh 是 MSYS 版，加的引号会一并留在命令串里
    → 远端 bash 把整串当文件名去 execve）。用 argv 列表交给 ssh，由远端 shell 解释。
    """
    key = _expand(target.get("ssh_key") or "")
    argv = ["ssh"]
    if key:
        argv += ["-i", key]
    argv += ["-o", "ConnectTimeout=10", "-o", "BatchMode=yes",
             "-o", "StrictHostKeyChecking=accept-new",
             _expand(target["ssh_target"]), _guarded(cmd)]
    try:
        proc = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, "", "超时 %ds" % int(timeout)
    except OSError as exc:                              # noqa: BLE001 —— ssh 起不来也是采集失败
        return None, "", "起不来：%s" % exc
    rc, body = _split_rc((proc.stdout or b"").decode("utf-8", "replace"))
    if rc is None:
        err = (proc.stderr or b"").decode("utf-8", "replace")
        return None, "", (err.strip() or "远端没有回传退出码标记")[-200:]
    return rc, body, ""


def read_file(target, path, timeout=40.0):
    """读一个文件 → `(rc, 内容, 说明)`。`rc=0` 有；`rc!=0` 文件不在；`rc=None` 采集失败。

    `transport=local` 的节点走本机 Python（**不起 shell** —— 本机是 Windows，posix 的 `cat`
    在这里根本不存在）；其余节点走 ssh，远端只发 `cat` 这一条形态。
    """
    if (target.get("transport") or "") == "local":
        if not os.path.isfile(path):
            return 1, "", ""
        try:
            with io.open(path, encoding="utf-8", errors="replace") as fh:
                return 0, fh.read(), ""
        except OSError as exc:
            return None, "", "读不了：%s" % exc
    return run_remote(target, "cat %s" % path, timeout)


def collect_live(hubs, roster_rows, targets, timeout=40.0):
    """只读采真机 → `collected`。远端只发 `cat` 一条形态，一个写操作都没有。"""
    out = {"hub_list": sorted(hubs), "roster": list(roster_rows),
           "conf": {}, "creds": {}, "errors": []}

    def err(node, what, why):
        out["errors"].append({"kind": "collect_error", "node": node,
                              "what": what, "why": str(why)[:200]})

    for hub in sorted(hubs):
        target = targets.get(hub)
        out["conf"][hub] = None
        if not target:
            err(hub, "部署表", "内部真值表里没有这个节点，拿不到 ssh 目标")
            continue
        base = conf_dir_of(hub, target)
        if not base:
            err(hub, "nats.conf", "算不出该机的 NATS 配置目录")
            continue
        rc, body, why = read_file(target, base + "/nats.conf", timeout)
        if rc is None:
            err(hub, "nats.conf", why or "采集失败")
        elif rc != 0:
            err(hub, "nats.conf", "中枢上没有 nats.conf")
        elif not body.strip():
            err(hub, "nats.conf", "配置文件存在但内容为空")
        else:
            out["conf"][hub] = body

    for member, _role, node, kind in roster_rows:
        target = targets.get(node)
        if not target:
            err(node, "部署表", "成员 %s 的凭据机不在内部真值表里" % member)
            continue
        path = cred_path(node, target, member, kind)
        if not path:
            err(node, "凭据 %s" % member, "算不出凭据路径（cred_kind=%s）" % kind)
            continue
        rc, body, why = read_file(target, path, timeout)
        if rc is None:
            err(node, "凭据 %s" % member, why or "采集失败")
        elif rc != 0:
            err(node, "凭据 %s" % member, "凭据文件不在该机上")
        elif not body.strip():
            err(node, "凭据 %s" % member, "凭据文件存在但内容为空")
        else:
            #: 明文到此为止 —— 只在内存里走一趟，`analyze()` 归一化后才允许出门
            out["creds"][member] = body.strip()
    return out


# ============================================================ 采集面 B：冻结夹具（离线）

def collect_fixtures(root):
    """从夹具目录读**同一形状**的 `collected`（装置只走这条，全程离线）。

    目录结构::

        <DIR>/roster.json               可选 {"hub_list": [...], "roster": [[m, role, node, kind], …]}
                                        缺省 = L0 档案本体 + 默认中枢清单
        <DIR>/<node>/nats.conf          该中枢的配置
        <DIR>/<node>/creds/<文件名>      该机上落地的凭据（`nats-<m>.pass` / `<m>.pass`）
    """
    out = {"hub_list": [], "roster": [], "conf": {}, "creds": {}, "errors": []}

    def err(node, what, why):
        out["errors"].append({"kind": "collect_error", "node": node,
                              "what": what, "why": str(why)[:200]})

    if not os.path.isdir(root):
        err("", "夹具目录", "夹具目录不存在：%s" % os.path.basename(root.rstrip("/\\")))
        return out

    hubs = list(HUBS_DEFAULT)
    roster_rows = [tuple(r) for r in member_roster.ROSTER]
    spec_path = os.path.join(root, "roster.json")
    if os.path.isfile(spec_path):
        try:
            with io.open(spec_path, encoding="utf-8") as fh:
                spec = json.load(fh) or {}
            hubs = list(spec.get("hub_list") or hubs)
            rows = spec.get("roster")
            if rows:
                roster_rows = [tuple(r) for r in rows]
        except ValueError as exc:
            err("", "roster.json", "夹具名册不是合法 JSON：%s" % exc)

    out["hub_list"] = sorted(hubs)
    out["roster"] = roster_rows

    for hub in out["hub_list"]:
        out["conf"][hub] = None
        path = os.path.join(root, hub, "nats.conf")
        if not os.path.isfile(path):
            err(hub, "nats.conf", "夹具里这台中枢没有 nats.conf")
            continue
        text = io.open(path, encoding="utf-8", errors="replace").read()
        if not text.strip():
            err(hub, "nats.conf", "配置文件存在但内容为空")
        else:
            out["conf"][hub] = text

    for member, _role, node, kind in roster_rows:
        path = os.path.join(root, node, "creds", fixture_cred_name(member, kind))
        if not os.path.isfile(path):
            err(node, "凭据 %s" % member, "凭据文件不在该机上")
            continue
        body = io.open(path, encoding="utf-8", errors="replace").read()
        if not body.strip():
            err(node, "凭据 %s" % member, "凭据文件存在但内容为空")
        else:
            out["creds"][member] = body.strip()
    return out


# ============================================================ 人读输出

def render(report):
    """每 hub 一行结论；有差异就逐行摊开（两侧内容**已归一化**，不可能带明文）。"""
    out = ["=" * 78,
           "档案 + 凭据探针 ⇒ 中枢用户区 · 逐字节自证 · %s" % report["schema"],
           "中枢 %s ｜ 档案成员 %d 人（顺序即规范序）"
           % ("、".join(report["hub_list"]) or "—", len(report["roster"])),
           "=" * 78]
    for row in report["roster"]:
        out.append("    %-12s role=%-8s 凭据 %s:%s"
                   % (row["member"], row["role"], row["cred_node"], row["cred_kind"]))
    out.append("")
    for row in report["per_hub"]:
        if row["ok"]:
            out.append("%s 用户区 ✓ 一致（归一化 md5 %s）" % (row["hub"], row["actual_md5_8"]))
            continue
        out.append("%s 用户区 ✗ %s（推导侧 md5 %s / 现网侧 md5 %s）"
                   % (row["hub"], row["why"] or "不一致",
                      row["expected_md5_8"] or "—", row["actual_md5_8"] or "—"))
        for d in row["diffs"]:
            out.append("    第 %d 行" % d["line"])
            out.append("      推导 %s" % ("<无此行>" if d["expected"] is None else d["expected"]))
            out.append("      现网 %s" % ("<无此行>" if d["actual"] is None else d["actual"]))
    if report["collect_errors"]:
        out.append("")
        out.append("采集失败 %d 条（退出码 2）：" % len(report["collect_errors"]))
        for item in report["collect_errors"]:
            out.append("    ✗ %s / %s：%s" % (item.get("node") or "—",
                                              item.get("what"), item.get("why")))
    out.append("")
    total = sum(r["diff_count"] for r in report["per_hub"])
    out.append("合计差异 %d 行%s" % (total, "，采集完整" if not report["collect_errors"] else ""))
    return "\n".join(out)


# ============================================================ main

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="「档案 + 凭据探针 ⇒ 中枢用户区」逐字节自证（阶段5 第三刀）")
    ap.add_argument("--live", action="store_true", help="只读采真机（ssh / 本地）")
    ap.add_argument("--fixtures", default="", help="从冻结夹具目录采（离线）")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    ap.add_argument("--out", default="", help="把 JSON 另存到文件")
    ap.add_argument("--timeout", type=float, default=40.0, help="单条远端命令超时（秒）")
    args = ap.parse_args(argv)

    if not (args.live or args.fixtures):
        ap.error("要么 --live，要么 --fixtures <DIR>")

    if args.fixtures:
        collected = collect_fixtures(args.fixtures)
    else:
        collected = collect_live(list(HUBS_DEFAULT),
                                 [tuple(r) for r in member_roster.ROSTER],
                                 load_targets(), timeout=args.timeout)

    import hub.l1_relay.gen_nats_conf as gen_nats_conf                                # noqa: E402 —— L1 角色模板的唯一出处
    report = analyze(collected, gen_nats_conf.perm_list)
    text = canonical(report)

    if args.out:
        with io.open(args.out, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
    if args.json:
        sys.stdout.write(text)
    else:
        print(render(report))

    if report["collect_errors"]:
        if not args.json:
            print("\n采集失败 —— 本次结果不作数（退出码 2）")
        return EXIT_COLLECT
    return EXIT_OK if report["ok"] else EXIT_DIFF


if __name__ == "__main__":
    sys.exit(main())
