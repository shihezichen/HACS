# -*- coding: utf-8 -*-
"""刀27 的「旧侧」**独立第二实现** —— 「档案 + 凭据 ⇒ 用户区」这件事的另一条算法路径。

它为什么存在
------------
正件 `tools/roster_derive_conf.py` 说「推导侧与现网侧逐字节相等」。**光它自己说不算数** ——
一个抽块器可以既抽错又自洽（抽错的那一段两边一样抽，照样「相等」）。所以本件从零再写一遍
同一套判定，走**另一条路**：

  · **抽用户区**：正件是「先正则定位 `users = [` 行，再**向上回溯**到最近的 `authorization {`
    行，然后逐字符配平括号」。本件是**先把整份 conf 切成顶层区块**（逐行走 depth，depth 由
    0 升起的那一行是区块头、回到 0 的那一行是区块尾），收齐所有 `authorization` 开头的顶层
    区块后，**挑体内含 `users` 的那一个**。一个自底向上一个自顶向下，抽歪了立刻分岔。
  · **归一化**：正件用两条正则（`password:\\s*"…"` / 时间戳行整行匹配）。本件**一个正则都不用** ——
    手写扫描器逐字符找 `password:` 后面那对引号，时间戳行按前缀/后缀切。
  · **角色模板**：本件**不 import `gen_nats_conf`**，按 L5 契约文档的口径自己拼那串期望权限。
    于是「现网权限串 == 期望权限串」这件事被两份互不相识的模板同时确认。
  · **渲染用户区**：正件按索引判「是不是最后一条」加逗号；本件先把每条渲染成无尾形态，
    最后**统一给前 n-1 条补逗号**。

本件**不 import 正件**、不 import 任何运行期件，只用标准库 + L0 数据档案 `member_roster`
（那是**被判定的输入数据**，不是被判定的实现 —— 两侧当然要读同一张名册，就像两侧要读同一份 conf）。

输出形状（键名、顺序、排序规则、`why` 文案）与正件**逐字节相同** —— 那是**冻结的契约**，
不是实现：装置逐格比的就是这两份规范 JSON。哪一侧把判定写歪，JSON 立刻不一样。

口令纪律与正件同款：明文只在内存里走一趟，进报告之前一律先过 `normalize()`。
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
AGENT_TEAM = os.path.join(REPO, "agent-team")
if AGENT_TEAM not in sys.path:
    sys.path.insert(0, AGENT_TEAM)

import member_roster                                    # noqa: E402 —— L0 名册档案（被判定的**输入数据**）

#: 与正件同一份冻结契约（形状常量，不是实现）
SCHEMA = "l5-roster-archive/1"
HUBS_DEFAULT = ("lobster", "top001")
STAMP_PREFIX = "# 由 gen_nats_conf.py 生成于 "
STAMP_SUFFIX = "（改配置请改脚本再重跑，别手改本文件）"
STAMP_NORMALIZED = "# 由 gen_nats_conf.py 生成于 <ts>"


# ============================================================ 角色模板（自己拼，不 import L1）

def perm_template(user):
    """期望权限串 —— 按 L5 契约文档 §「角色模板」的口径**自己拼**。

    形状：`publish` = 通用四条 + 「只许发自己的」心跳/探测两条；`subscribe` = 通用四条。
    """
    publish = ['"a2a.>"',
               '"team.hb.' + user + '"',
               '"team.probe.' + user + '"',
               '"bb.>"',
               '"$JS.API.>"',
               '"_INBOX.>"']
    subscribe = ['"a2a.>"', '"team.>"', '"bb.>"', '"_INBOX.>"']
    return "publish: [" + ", ".join(publish) + "], subscribe: [" + ", ".join(subscribe) + "]"


# ============================================================ 抽用户区（自顶向下切顶层区块）

def _line_depth_walk(line, depth, quote):
    """逐字符走一行 → `(行尾 depth, 行尾引号状态, 本行是否开过花括号)`。引号内的括号不计。"""
    i, opened = 0, False
    while i < len(line):
        ch = line[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = ""
        elif ch == '"' or ch == "'":
            quote = ch
        elif ch == "{":
            depth += 1
            opened = True
        elif ch == "}":
            depth -= 1
        i += 1
    return depth, quote, opened


def top_level_blocks(text):
    """把 conf 切成**顶层区块** → `[(首行下标, 末行下标, 区块文本)]`。

    逐行走 depth：depth 从 0 变正的那一行是区块头，回到 0 的那一行是区块尾；
    在**同一行**开合的（`authorization { … }` 那种单行形态）单独收一条。
    `cluster { }` 体内那条单行 `authorization { user: "route" … }` 的 depth 从 1 起步，
    压根不是顶层区块 —— 集群路由凭据天然落在候选集之外，一个字节都进不来。
    """
    lines = text.split("\n") if text else []
    blocks, depth, quote, head = [], 0, "", None
    for i, line in enumerate(lines):
        if head is None:
            depth, quote, opened = _line_depth_walk(line, depth, quote)
            if depth > 0:
                head = i
            elif opened:
                blocks.append((i, i, line))               # 同一行开合的顶层区块
            continue
        depth, quote, _opened = _line_depth_walk(line, depth, quote)
        if depth <= 0:
            blocks.append((head, i, "\n".join(lines[head:i + 1])))
            head, depth, quote = None, 0, ""
    return blocks


def _head_word(line):
    """区块头那一行的第一个词（`authorization {` → `authorization`）。"""
    s = line.strip()
    out = []
    for ch in s:
        if ch.isalnum() or ch == "_":
            out.append(ch)
        else:
            break
    return "".join(out)


def _body_has_users(block_text):
    """区块体内是否有 `users` 赋值行（不用正则：逐行 strip 后看前缀与等号）。"""
    for line in block_text.split("\n")[1:]:
        s = line.strip()
        if not s.startswith("users"):
            continue
        rest = s[len("users"):].lstrip()
        if rest.startswith("="):
            return True
    return False


def extract_users_block(text):
    """conf → `(用户区文本, 说明)`；抽不出时 `(None, 为什么)`。

    口径与正件一致（首尾两行含在内、行内空白原样），路径不同：先切顶层区块，
    再挑「头是 `authorization` 且体内有 `users =`」的那一个。
    """
    if not (text or "").strip():
        return None, "配置里找不到 users = [ 区块"
    blocks = top_level_blocks(text)
    hit = None
    for _h, _t, body in blocks:
        if _head_word(body.split("\n")[0]) == "authorization" and _body_has_users(body):
            hit = body
            break
    if hit is not None:
        return hit, ""
    # 没抽到 —— 把「为什么」分清楚：是压根没有 users，还是有 users 但括号没配平 / 没有头。
    lines = text.split("\n")
    users_at = None
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("users") and s[len("users"):].lstrip().startswith("="):
            users_at = i
            break
    if users_at is None:
        return None, "配置里找不到 users = [ 区块"
    for i in range(users_at, -1, -1):
        s = lines[i].strip()
        if _head_word(s) == "authorization" and "{" in s:
            return None, "authorization { 的花括号在文件末尾仍未配平"
    return None, "users = [ 之上找不到 authorization { 行"


# ============================================================ 渲染用户区（先无尾、最后统一补逗号）

HEAD_LINES = ["authorization {", "  timeout: 3", "  users = ["]
TAIL_LINES = ["  ]", "}"]


def derive_users_block(roster_rows, passwords, perm_fn):
    """档案有序表 + 口令 + 注入模板 → 期望用户区文本。

    与正件的差别只在**逗号怎么给**：这里先把每条渲染成无尾形态，最后统一给前 n-1 条补上。
    """
    body = []
    for row in roster_rows:
        member = row[0]
        body.append("".join(['    { user: "', member,
                             '", password: "', passwords.get(member, ""),
                             '", permissions: { ', perm_fn(member), " } }"]))
    for i in range(len(body) - 1):
        body[i] = body[i] + ","
    return "\n".join(HEAD_LINES + body + TAIL_LINES)


# ============================================================ 归一化（手写扫描器，零正则）

def sha256_12(text):
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:12]


def md5_8(text):
    return hashlib.md5((text or "").encode("utf-8")).hexdigest()[:8]


def _mask_passwords(line):
    """把本行所有 `password: "<明文>"` 换成 `password: "sha256:<前12>"` —— 逐字符扫，不用正则。"""
    key = "password:"
    out, i, n = [], 0, len(line)
    while i < n:
        if not line.startswith(key, i):
            out.append(line[i])
            i += 1
            continue
        j = i + len(key)
        while j < n and line[j] in " \t":
            j += 1
        if j >= n or line[j] != '"':
            out.append(line[i])
            i += 1
            continue
        k = j + 1
        while k < n and line[k] != '"':
            k += 1
        if k >= n:
            out.append(line[i])
            i += 1
            continue
        out.append(line[i:j])                      # `password:` 与其后的空白，原样
        out.append('"sha256:' + sha256_12(line[j + 1:k]) + '"')
        i = k + 1
    return "".join(out)


def normalize(text):
    """归一化 —— 只有两处：口令换摘要、生成时间戳行换固定形态。"""
    out = []
    for line in (text or "").split("\n"):
        if line.startswith(STAMP_PREFIX) and line.endswith(STAMP_SUFFIX):
            out.append(STAMP_NORMALIZED)
        else:
            out.append(_mask_passwords(line))
    return "\n".join(out)


def diff_lines(expected, actual):
    """两段**已归一化**文本的逐行差异（短的一侧补 `None`）。"""
    a, b = expected.split("\n"), actual.split("\n")
    out = []
    for i in range(max(len(a), len(b))):
        left = a[i] if i < len(a) else None
        right = b[i] if i < len(b) else None
        if left != right:
            out.append({"line": i + 1, "expected": left, "actual": right})
    return out


# ============================================================ 夹具采集（装置离线跑；不实现真机面）

def fixture_cred_name(member, kind):
    return ("nats-%s.pass" % member) if kind == "runtime" else ("%s.pass" % member)


def collect_fixtures(root):
    """从夹具目录读 `collected`（形状与正件同款；真机面不属于判定逻辑，旧侧不重写）。"""
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
            spec = json.loads(io.open(spec_path, encoding="utf-8").read()) or {}
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

    for row in roster_rows:
        member, node, kind = row[0], row[2], row[3]
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


# ============================================================ analyze —— 纯函数

def analyze(collected, perm_fn):
    """与正件同形状的报告（键名/顺序/排序/`why` 文案都是冻结契约）。"""
    hubs = list(collected.get("hub_list") or [])
    roster_rows = [tuple(r) for r in (collected.get("roster") or [])]
    conf = collected.get("conf") or {}
    creds = collected.get("creds") or {}
    errors = list(collected.get("errors") or [])

    roster_out = []
    for row in roster_rows:
        roster_out.append({"member": row[0], "role": row[1],
                           "cred_node": row[2], "cred_kind": row[3]})

    missing = [row[0] for row in roster_rows if row[0] not in creds]
    expected_norm = None
    if roster_rows and not missing:
        expected_norm = normalize(derive_users_block(roster_rows, creds, perm_fn))

    per_hub = []
    for hub in hubs:
        row = {"hub": hub,
               "expected_md5_8": None if expected_norm is None else md5_8(expected_norm),
               "expected_lines": 0 if expected_norm is None else len(expected_norm.split("\n")),
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
            row["why"] = ("推导侧不完整：缺 %d 个成员的凭据" % len(missing)
                          if missing else "名册为空，推导不出用户区")
            per_hub.append(row)
            continue
        d = diff_lines(expected_norm, actual_norm)
        row["diffs"] = d
        row["diff_count"] = len(d)
        row["ok"] = (len(d) == 0)
        row["why"] = "" if not d else "与推导侧有 %d 行不同" % len(d)
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


def report_of(root):
    """装置的入口：夹具目录 → 报告（模板走本件自己那份 `perm_template`）。"""
    return analyze(collect_fixtures(root), perm_template)
