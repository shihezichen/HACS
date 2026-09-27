#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T 工具面：**成员入网只读对账**（阶段5 第一刀）——「现网权限矩阵 vs 期望矩阵」的机械答案。

为什么要它
----------
阶段5 的目标是把「成员入网」从**人工改 NATS 配置**变成**按档案 + 探针自动生成**。
但在动 NATS 之前必须先有一把尺子：**现在到底是什么样**。没有这把尺子，任何自动化
都只是把「不知道对不对」换成「不知道对不对，而且是自动的」。

本件就是那把尺子 —— **只读**地把六件事量出来，冻结成基线，之后每次跑都与基线比。
它**不改任何配置、不重启任何单元、不写远端任何文件**；远端只发 `cat` 与 `curl -s` 两种命令。

名册真源（契约，**刀29 修订**）
------------------------------
  · **名册 = L0 档案 `agent-team/member_roster.py` 的 `ROSTER`**（顺序即规范序）。
    刀25 曾把名册真源定义为「两台中枢 `nats.conf` 里 `authorization.users` 的规范形」——
    那让**期望侧就是被检对象自己**：谁登上两台中枢把 conf 一起手改（加人 / 删人），
    本件**完全看不见**。刀29 把期望侧切到档案，这个盲区才闭合（刀27 已真机证明
    「档案 + 凭据探针 ⇒ 中枢用户区」逐字节成立，所以这是**已知等价的替换**）。
  · 档案经 `artifacts["roster_rows"]` 注入（`[(member, role, cred_node, cred_kind), …]`，
    **顺序即规范序，不许再排一遍**）。**注不进来就是采集失败** —— 记一条 `collect_error`、
    退出码 2，**绝不回退到「conf 并集」**（回退就是把自证请回来）。
  · **本刀只切「成员身份」这一半**：M3 的期望口令仍取**基准中枢 conf** 的 `user → password`
    （口令改动的漏点由既有 M3 覆盖，故不阻塞）；`cred_node` / `cred_kind` 本刀**未消费**。
  · **角色模板的唯一出处 = `gen_nats_conf.perm_list(user)`**（L1）。期望权限串一律**调它算**，
    本件源码里**没有**第二份模板字符串。它由 `main()` 注入进 `analyze()`，所以
    `analyze()` 本身是**纯函数**（无 IO / 无 time / 无随机），装置可以塞自己那份独立实现进来互证。
  · **中枢清单**是节点名 `("lobster", "top001")`（节点名不是宿主地址，可入仓）；
    ssh 目标与运行目录**一律经 `tools/deploy_targets.json` 解析**，本件零宿主字面量。

六项检查（键名与顺序冻结）
--------------------------
  M1 `hub_user_matrix`   两中枢的 `(user → password)` 集合完全相同，**且成员集 == L0 名册档案**
  M2 `perm_template`     每个 user 的 `permissions` 逐字符 == `perm_list(user)`
  M3 `cred_landing`      各机 `*.pass` 内容 == 名册中同名成员的口令
  M4 `roster_coverage`   名册每个成员至少有一处落地（pass 文件 ∪ 活连接）
  M5 `live_subset`       活连接的成员身份必须 ⊆ 名册（**硬违规**）
  M6 `legacy_and_routes` 非 v2 形状的中枢；集群路由条目 >2 或存在陈旧路由

口令一个字节都不出门
--------------------
采集器**当场把 `*.pass` 的内容换成 sha256**，明文连 `artifacts` 都进不去；名册口令只在
`analyze()` 内部用来算 sha256 做比对，输出里只出现 `sha256_8`（前 8 位十六进制）。
stdout / `--out` / 基线文件 / 报告里**不可能**出现任何口令明文。

用法
----
    python tools/nats_membership_audit.py --live [--json] [--out FILE]
    python tools/nats_membership_audit.py --live --write-baseline tools/nats_membership_audit_baseline.json
    python tools/nats_membership_audit.py --fixtures <DIR> [--json]   # 装置用，离线可跑
    python tools/nats_membership_audit.py --check [--json]            # 采一次 + 比冻结基线

退出码
------
    0  `--check` 与冻结基线逐项一致；或 `--live` / `--fixtures` 采集成功
    1  `--check` 发现漂移（逐条打印「新增 / 消失」，**不因漂移而静默**）
    2  采集失败（ssh / 网络 / 夹具缺失 / 中枢配置空或解析不出用户 / 监控口回的不是 JSON）

**采集面的退出码只表达采集成败，不表达 findings 是否为空** —— 判定请用 `--check`。
本刀冻结的基线**本来就非零**（僵尸凭据 / 遗留中枢 / 重复路由），那是**如实记账的已知项**，
由阶段5 后续刀去消。**绝不许为了把基线变绿而放宽口径或删检查项。**

发现项的「身份」与「观测量」（刀28 修订）
----------------------------------------
`--check` 比的是**发现项的判据身份** —— 「哪一项检查 / 哪一类违规 / 落在哪个对象上」，
不是整条 JSON。**判据的输入量**（例：`num_routes > 2` 才成判据）必须留在身份里；
只有「同一裁决下自己会随时间漂移的输出数字」才登记进 `VOLATILE_KEYS`，比对时摘出。

为什么必须分开：M6 的 `duplicate_route` 条目里带 `stale_routes` 计数，而该计数由**墙钟**决定
（NATS 每次重启后路由 idle 归零、约 1 小时后计数必由 0 变正）→ 基线的「瞬时值」每次必假红。
**修的是「身份里混进了钟」，不是放宽判据**：裁决消失（例：重复路由不再存在）照旧报「消失」，
双向严格一点没削（`docs/l5-membership-audit.md`《身份与观测量》节 + 装置 D 段有机械证据）。
"""
from __future__ import annotations

import argparse
import fnmatch
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
BASELINE_PATH = os.path.join(HERE, "nats_membership_audit_baseline.json")

if AGENT_TEAM not in sys.path:
    sys.path.insert(0, AGENT_TEAM)

import node_profile                                     # noqa: E402 —— L0 节点档案（宿主事实唯一真源）

EXIT_OK, EXIT_DRIFT, EXIT_COLLECT = 0, 1, 2

#: 输出契约版本（键名冻结；改形状必须进版本号）
SCHEMA = "l5-membership-audit/1"

#: 默认中枢（节点名，不是地址）。`--hub` 只为测试保留，真机跑时别传。
HUBS_DEFAULT = ("lobster", "top001")

#: v2 中枢的端口体系：9930 客户端 / 9931 集群路由 / 9932 监控（仅环回）。
#: 监听端口落在集合外 = 不是 v2 形状（与 `agent-team/gen_nats_conf.py` 生成的那份对齐）。
V2_PORTS = (9930, 9931, 9932)
#: 监控口只绑 127.0.0.1 —— 所以 connz/routez 一律**经 ssh 在该机上打环回**，本件不需要任何地址字面量。
MONITOR_PORT = 9932

#: 陈旧路由阈值：`idle` 超过它就是「早该被回收却还挂着的重复路由」。
STALE_IDLE_SECONDS = 3600

#: 六项检查（**顺序与键名冻结**）
CHECKS = (
    ("M1", "hub_user_matrix"),
    ("M2", "perm_template"),
    ("M3", "cred_landing"),
    ("M4", "roster_coverage"),
    ("M5", "live_subset"),
    ("M6", "legacy_and_routes"),
)
CHECK_KEYS = tuple(k for k, _ in CHECKS)

#: `--check` 参与比对的字段（其余是说明性的，不进判定）
COMPARE_KEYS = ("node_list", "hub_list", "roster", "live_sources", "findings")

#: **观测量**（键名 → 为什么它不是判据身份的一部分）。比对时递归摘出，见 `_identity()`。
#: 登记口径：**判据的输入量**（如 `num_routes`：> 2 才成判据）**不许登记**，它必须留在身份里；
#: 只有「同一裁决下自己会随时间漂移的输出数字」才准登记。登记 = 声明它不进身份，**必须留痕**
#: （契约文档《身份与观测量》节 + 台账版本行），且**只准摘观测量，判据一条都不许放宽**。
VOLATILE_KEYS = {
    "stale_routes": "该计数由**墙钟**决定（idle 是否越过 STALE_IDLE_SECONDS）：NATS 每次重启后"
                    "路由 idle 归零、约 1 小时后计数必由 0 变正，同一裁决的身份被钟推着漂移"
                    "→ 基线每次假红（刀28 实测）。判据本身是布尔的「是否存在陈旧路由」，"
                    "计数只是给人看的观测值。",
}

#: 凭据文件的两种形态：posix 中枢落在配置目录下的 `<member>.pass`；
#: nt 节点落在运行目录下的 `nats-<member>.pass`。成员名从**文件名**推，两种都认。
CRED_PATTERNS = (("nats-*.pass", re.compile(r"^nats-(.+)\.pass$")),
                 ("*.pass", re.compile(r"^(.+)\.pass$")))

#: 远端命令的 PATH —— 自己钉死（刀23 实测：MSYS ssh 起的远端 shell 未必含 /usr/bin）
REMOTE_PATH = "export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$PATH; "
#: 远端命令的退出码回传标记（ssh 自己的 rc 只反映最后一条命令，区分不了「文件不在」与「连不上」）
RC_MARK = "K25RC="

_ENV_RX = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::([^}]*))?\}")


# ============================================================ 小工具

def _expand(value):
    """展开 `${VAR:默认值}`（与 deploy.py / nodelet_gen.py 同一口径）。"""
    if not isinstance(value, str):
        return value

    def sub(m):
        return os.environ.get(m.group(1)) or (m.group(2) or "")
    return _ENV_RX.sub(sub, value)


def sha256_hex(text):
    """字符串的 sha256（十六进制）。口令只以它的形态离开本进程。"""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def canonical(obj):
    """规范 JSON：键名排序、缩进固定、不转义非 ASCII —— 供逐字节比对。"""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, indent=2) + "\n"


def _sort_findings(items):
    """findings 列表的确定性排序（按规范化后的条目文本）。"""
    return sorted(items, key=lambda it: json.dumps(it, sort_keys=True, ensure_ascii=False))


def _identity(item):
    """发现项的**判据身份**：递归摘掉登记在 `VOLATILE_KEYS` 里的观测数字。

    `--check` 拿身份跟冻结基线比 —— 否则「随时间自己会变」的观测数字会让**同一裁决**反复报
    「新增 + 消失」（刀28 修的就是这个时间脆性）。摘掉的只是观测值：裁决若**消失**（例：重复
    路由不再存在），照旧报「消失」；非观测字段（例：`num_routes` 4 → 8）照旧报漂移。
    """
    if isinstance(item, dict):
        return {k: _identity(v) for k, v in item.items() if k not in VOLATILE_KEYS}
    if isinstance(item, list):
        return [_identity(v) for v in item]
    return item


# ============================================================ conf 解析（正件这一路：括号配平 + 正则取字段）

_DUR_RX = re.compile(r"(\d+(?:\.\d+)?)\s*(ns|µs|us|ms|s|m|h|d)")
_DUR_UNITS = {"ns": 1e-9, "µs": 1e-6, "us": 1e-6, "ms": 1e-3,
              "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}


def parse_duration(text):
    """NATS 的人读时长串 → **秒**（`"6d18h7m50s"` / `"5s"` / `"322µs"`）。

    解析不出来返回 `None` —— **不猜 0**（把「读不懂」说成「刚活动过」会把陈旧路由洗白）。
    """
    s = str(text or "").strip()
    if not s:
        return None
    total, pos, seen = 0.0, 0, False
    for m in _DUR_RX.finditer(s):
        if m.start() != pos:
            return None
        total += float(m.group(1)) * _DUR_UNITS[m.group(2)]
        pos, seen = m.end(), True
    if not seen or pos != len(s):
        return None
    return total


def _skip_string(text, i):
    """`text[i]` 是引号时，跳到该字符串字面量之后。"""
    quote, i = text[i], i + 1
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == quote:
            return i + 1
        i += 1
    return i


def _balanced_slice(text, start, opener, closer):
    """从 `text[start]`（必须是 `opener`）起取配平的一段 → (内部文本, 结束下标+1)。

    引号内的括号不计（口令与主题串里理论上可以出现括号）。配不平返回 `(None, len)`。
    """
    depth, i = 0, start
    while i < len(text):
        ch = text[i]
        if ch in "\"'":
            i = _skip_string(text, i)
            continue
        if ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return text[start + 1:i], i + 1
        i += 1
    return None, len(text)


_USERS_RX = re.compile(r"\busers\s*=\s*\[")
_USER_RX = re.compile(r'\buser\s*:\s*"([^"]*)"')
_PASS_RX = re.compile(r'\bpassword\s*:\s*"([^"]*)"')
_PERM_RX = re.compile(r"\bpermissions\s*:\s*\{")
_LISTEN_RX = re.compile(r"^\s*(listen|port|https?)\s*:\s*(.+?)\s*,?\s*$")
_CLUSTER_RX = re.compile(r"\bcluster\s*\{")
_NAME_RX = re.compile(r'\bname\s*:\s*"([^"]*)"')


def parse_conf(text):
    """`nats.conf` → 结构化事实（**只取本刀要判的那几样**，不做通用 conf 解析器）。

    返回 `{"users": [(user, password, perm_body|None)], "listen_ports": [int],
           "cluster_name": str, "has_permissions": bool, "has_users_block": bool}`。

    为什么不取 `cluster { authorization { user: "route" … } }` 里那条：它是**路由凭据**不是成员，
    定位靠 `users = [ … ]` 这个数组本身（集群授权块里没有它），天然把路由那条挡在外面。
    """
    src = text or ""
    users, perm_seen = [], False
    m = _USERS_RX.search(src)
    if m:
        inner, _end = _balanced_slice(src, m.end() - 1, "[", "]")
        i, inner = 0, inner or ""
        while i < len(inner):
            ch = inner[i]
            if ch in "\"'":
                i = _skip_string(inner, i)
                continue
            if ch != "{":
                i += 1
                continue
            entry, i = _balanced_slice(inner, i, "{", "}")
            if entry is None:
                break
            um, pm = _USER_RX.search(entry), _PASS_RX.search(entry)
            if not um:
                continue
            pm2 = _PERM_RX.search(entry)
            perm = None
            if pm2:
                body, _e = _balanced_slice(entry, pm2.end() - 1, "{", "}")
                perm = (body or "").strip()
                perm_seen = True
            users.append((um.group(1), pm.group(1) if pm else "", perm))

    ports = []
    for line in src.splitlines():
        lm = _LISTEN_RX.match(line)
        if not lm:
            continue
        raw = lm.group(2).strip().strip('"').strip("'")
        tail = raw.rsplit(":", 1)[-1].strip()
        if tail.isdigit():
            ports.append(int(tail))

    cluster_name = ""
    cm = _CLUSTER_RX.search(src)
    if cm:
        body, _e = _balanced_slice(src, cm.end() - 1, "{", "}")
        nm = _NAME_RX.search(body or "")
        if nm:
            cluster_name = nm.group(1)

    return {"users": users,
            "listen_ports": sorted(set(ports)),
            "cluster_name": cluster_name,
            "has_permissions": perm_seen or bool(re.search(r"\bpermissions\s*:", src)),
            "has_users_block": bool(m)}


_TEAMD_RX = re.compile(r"^teamd-(.+)$")


def live_members(connz):
    """活连接 → `{"authorized_user": [...], "name_prefix": [...]}`（**两个来源都留**）。

    NATS 给得出 `authorized_user` 就用它；给不出时回落到客户端 `name` 的 `teamd-<member>` 前缀。
    **两个来源都要记进输出** —— 少留一个就等于给自己留了个「消红」的后门。
    """
    out = {"authorized_user": set(), "name_prefix": set()}
    for conn in ((connz or {}).get("connections") or []):
        if not isinstance(conn, dict):
            continue
        who = str(conn.get("authorized_user") or "").strip()
        if who:
            out["authorized_user"].add(who)
        nm = _TEAMD_RX.match(str(conn.get("name") or "").strip())
        if nm:
            out["name_prefix"].add(nm.group(1))
    return {k: sorted(v) for k, v in out.items()}


# ============================================================ analyze —— 纯函数，六项检查

def analyze(artifacts, perm_fn):
    """`artifacts` + **注入的角色模板函数** → 报告（纯函数：无 IO、无 time、无随机）。

    `perm_fn(user) -> str` 是期望权限串的唯一出处。正件在 `main()` 里传
    `gen_nats_conf.perm_list`；等价装置传它自己那份独立实现 —— 这样两路才是互证而非自证。
    """
    hubs = list(artifacts.get("hubs") or [])
    nodes = list(artifacts.get("nodes") or [])
    conf_text = artifacts.get("conf") or {}
    connz = artifacts.get("connz") or {}
    routez = artifacts.get("routez") or {}
    creds = artifacts.get("creds") or {}
    errors = list(artifacts.get("errors") or [])

    parsed = {n: parse_conf(conf_text.get(n)) for n in nodes if conf_text.get(n) is not None}

    # ---- 期望侧的成员身份 = **L0 名册档案**（刀29）。注不进来就是采集失败 —— 不回退到 conf 并集，
    #      回退等于把「拿被检对象当期望」的自证请回来，本刀要闭的正是这个盲区。
    roster_rows = artifacts.get("roster_rows")
    if roster_rows is None:
        errors.append({"kind": "collect_error", "node": "-", "what": "名册档案",
                       "why": "期望侧名册未注入（既不是 L0 member_roster 档案、"
                              "也不是夹具里的 roster.json）"})
        roster_rows = []
    roster = [row[0] for row in roster_rows]               # **档案顺序即规范序**，不许再 sort

    # ---- 口令真值侧**本刀未切**：仍以第一台有 conf 的中枢为基准（见模块文档串「名册真源」）
    hub_users = {}
    for h in hubs:
        if h in parsed:
            hub_users[h] = {u: p for u, p, _perm in parsed[h]["users"]}
    ref_hub = next((h for h in hubs if h in hub_users), "")
    roster_pw = dict(hub_users.get(ref_hub) or {})

    findings = {k: [] for k in CHECK_KEYS}

    # ---- M1 两中枢的 (user → password) 集合完全相同
    if ref_hub:
        ref_pairs = set(hub_users[ref_hub].items())
        for h in hubs:
            if h == ref_hub or h not in hub_users:
                continue
            other = hub_users[h]
            other_pairs = set(other.items())
            for u, _p in sorted(ref_pairs - other_pairs):
                findings["M1"].append({
                    "kind": "hub_matrix_missing", "member": u, "hub": h, "ref_hub": ref_hub,
                    "why": "口令与基准中枢不同" if u in other else "该成员不在此中枢名册里"})
            for u, _p in sorted(other_pairs - ref_pairs):
                findings["M1"].append({
                    "kind": "hub_matrix_extra", "member": u, "hub": h, "ref_hub": ref_hub,
                    "why": "口令与基准中枢不同" if u in hub_users[ref_hub]
                           else "该成员不在基准中枢名册里"})

    # ---- M1（刀29 增量）每个中枢的**成员集** vs L0 名册档案。
    #      上面那两类只能发现「两台中枢互相不一致」；**两台一起被手改**时它们是全绿的 ——
    #      下面这两类才是抓「手改入网 / 手改退网」的那把钳子。
    arch_members = set(roster)
    for h in hubs:
        if h not in hub_users:
            continue
        conf_members = set(hub_users[h])
        for u in sorted(conf_members - arch_members):
            findings["M1"].append({
                "kind": "conf_only_member", "member": u, "hub": h,
                "why": "该中枢配置里有此成员，但名册档案里没有（谁手改加了人？）"})
        for u in sorted(arch_members - conf_members):
            findings["M1"].append({
                "kind": "conf_missing_member", "member": u, "hub": h,
                "why": "名册档案里有此成员，但该中枢配置里没有"})

    # ---- M2 每个 user 的 permissions 逐字符 == perm_fn(user)
    for h in hubs:
        if h not in parsed:
            continue
        for user, _pw, perm in parsed[h]["users"]:
            expected = perm_fn(user)
            if perm == expected:
                continue
            findings["M2"].append({"kind": "perm_drift", "hub": h, "member": user,
                                   "expected": expected, "actual": perm})

    # ---- M3 pass 文件内容 == 名册中同名成员的口令
    for node in nodes:
        for filename, digest in sorted((creds.get(node) or {}).items()):
            member = _member_of(filename)
            if member is None:
                continue
            item = {"member": member, "node": node,
                    "location": "%s:%s" % (node, filename), "sha256_8": digest[:8]}
            if member not in roster_pw:
                findings["M3"].append(dict(item, kind="cred_orphan",
                                           why="该机上有凭据文件，名册里没有这个成员"))
            elif sha256_hex(roster_pw[member]) != digest:
                findings["M3"].append(dict(item, kind="cred_mismatch",
                                           why="凭据文件内容与名册口令不一致"))

    # ---- M4 名册每个成员至少有一处落地（pass 文件 ∪ 活连接）
    landed_files = set()
    for node in nodes:
        for filename in (creds.get(node) or {}):
            member = _member_of(filename)
            if member is not None:
                landed_files.add(member)
    live_sources = {"authorized_user": set(), "name_prefix": set()}
    for h in hubs:
        got = live_members(connz.get(h))
        for src_name, members in got.items():
            live_sources[src_name].update(members)
    landed_live = live_sources["authorized_user"] | live_sources["name_prefix"]
    for member in roster:
        if member in landed_files or member in landed_live:
            continue
        findings["M4"].append({"kind": "zombie_credential", "member": member,
                               "why": "名册里有，但全网既无凭据文件也无活连接"})

    # ---- M5 活连接的成员身份必须 ⊆ 名册（硬违规）
    roster_set = set(roster)
    for h in hubs:
        got = live_members(connz.get(h))
        for src_name in sorted(got):
            for member in got[src_name]:
                if member in roster_set:
                    continue
                findings["M5"].append({"kind": "live_outside_roster", "hub": h,
                                       "member": member, "source": src_name})

    # ---- M6a 非 v2 形状的中枢（按 conf 判：无 permissions 块，或监听端口出了 v2 端口体系）
    for node in nodes:
        info = parsed.get(node)
        if info is None or not (info["users"] or info["listen_ports"]):
            continue                              # 该机没有 nats 配置 —— 不是「遗留」，是「没有」
        bad_ports = [p for p in info["listen_ports"] if p not in V2_PORTS]
        if info["has_permissions"] and not bad_ports:
            continue
        why = []
        if not info["has_permissions"]:
            why.append("conf 里没有 permissions 块（= 全权）")
        if bad_ports:
            why.append("监听端口 %s 不属于 v2 端口体系 %s"
                       % (",".join(str(p) for p in bad_ports),
                          "/".join(str(p) for p in V2_PORTS)))
        findings["M6"].append({"kind": "legacy_hub", "node": node,
                               "bad_ports": bad_ports,
                               "has_permissions": info["has_permissions"],
                               "user_count": len(info["users"]),
                               "why": "；".join(why)})

    # ---- M6b 集群路由重复 / 陈旧（**按集群聚合成一条** —— 同一份重复路由从两端各看见一次，
    #          它是一个缺陷不是两个；每端的数字仍逐条列在 `hubs` 里，不丢信息）
    clusters = {}
    for h in hubs:
        rz = routez.get(h)
        if not isinstance(rz, dict):
            continue
        routes = rz.get("routes") or []
        stale = 0
        for route in routes:
            if not isinstance(route, dict):
                continue
            secs = parse_duration(route.get("idle"))
            if secs is not None and secs > STALE_IDLE_SECONDS:
                stale += 1
        num = rz.get("num_routes")
        num = int(num) if isinstance(num, int) else len(routes)
        name = (parsed.get(h) or {}).get("cluster_name") or ""
        clusters.setdefault(name, []).append(
            {"hub": h, "num_routes": num, "stale_routes": stale})
    for name in sorted(clusters):
        rows = sorted(clusters[name], key=lambda r: r["hub"])
        if not any(r["num_routes"] > 2 or r["stale_routes"] for r in rows):
            continue
        findings["M6"].append({
            "kind": "duplicate_route", "cluster": name, "hubs": rows,
            "stale_threshold_seconds": STALE_IDLE_SECONDS,
            "why": "两台中枢之间应当只有 1 条活路由；条目数 >2 或存在 idle 超阈值的陈旧条目"})

    findings = {k: _sort_findings(v) for k, v in findings.items()}
    counts = {k: len(findings[k]) for k in CHECK_KEYS}
    collect_errors = _sort_findings(errors)
    return {
        "schema": SCHEMA,
        "node_list": sorted(nodes),
        "hub_list": sorted(hubs),
        "roster": roster,
        "check_names": {k: name for k, name in CHECKS},
        "live_sources": {k: sorted(v) for k, v in live_sources.items()},
        "collect_errors": collect_errors,
        "findings": findings,
        "counts": counts,
        "ok": (not collect_errors) and not any(counts.values()),
    }


def _member_of(filename):
    """凭据文件名 → 成员名（`nats-<m>.pass` 优先于 `<m>.pass`）。都不匹配返回 `None`。"""
    for _glob, rx in CRED_PATTERNS:
        m = rx.match(filename)
        if m:
            return m.group(1)
    return None


# ============================================================ 采集面 A：真机（只读）

def load_targets():
    """内部站点真值表（节点名 → transport / ssh_target / runtime_dir）。"""
    if not os.path.isfile(TARGETS_PATH):
        return {}
    with io.open(TARGETS_PATH, encoding="utf-8") as fh:
        data = json.load(fh) or {}
    return {t["name"]: t for t in (data.get("targets") or []) if t.get("name")}


def _guarded(cmd):
    """把远端命令包一层：**内层 rc 回传** + 末尾垫一个空行（防止无尾换行的文件把标记粘上去）。"""
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
    → 远端 bash 把整串当文件名去 execve）。用 argv 列表交给 ssh，由它拼命令行、远端 shell 解释。
    """
    wrapped = _guarded(cmd)
    key = _expand(target.get("ssh_key") or "")
    argv = ["ssh"]
    if key:
        argv += ["-i", key]
    argv += ["-o", "ConnectTimeout=10", "-o", "BatchMode=yes",
             "-o", "StrictHostKeyChecking=accept-new",
             _expand(target["ssh_target"]), wrapped]
    try:
        proc = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, "", "超时 %ds" % int(timeout)
    except OSError as exc:                              # noqa: BLE001 —— ssh 起不来也是采集失败
        return None, "", "起不来：%s" % exc
    out = (proc.stdout or b"").decode("utf-8", "replace")
    err = (proc.stderr or b"").decode("utf-8", "replace")
    rc, body = _split_rc(out)
    if rc is None:
        return None, "", (err.strip() or "远端没有回传退出码标记")[-200:]
    return rc, body, ""


# --- 三个语义采集口。`transport=local` 的节点走本机 Python（**不起 shell** ——
#     本机是 Windows，posix 的 `cat`/`ls` 在这里根本不存在）；其余节点走 ssh。

def _is_local(target):
    return (target.get("transport") or "") == "local"


def read_file(target, path, timeout=40.0):
    """读一个文件 → `(rc, 内容, 说明)`。`rc=0` 有；`rc!=0` 文件不在（不是错）；`rc=None` 采集失败。"""
    if _is_local(target):
        if not os.path.isfile(path):
            return 1, "", ""
        try:
            with io.open(path, encoding="utf-8", errors="replace") as fh:
                return 0, fh.read(), ""
        except OSError as exc:
            return None, "", "读不了：%s" % exc
    return run_remote(target, "cat %s" % path, timeout)


def list_dir(target, path, timeout=40.0):
    """列一个目录 → `(rc, 名字列表, 说明)`。语义同 `read_file`。"""
    if _is_local(target):
        if not os.path.isdir(path):
            return 1, [], ""
        try:
            return 0, sorted(os.listdir(path)), ""
        except OSError as exc:
            return None, [], "列不了：%s" % exc
    rc, body, why = run_remote(target, "ls -1 %s" % path, timeout)
    if rc is None or rc != 0:
        return rc, [], why
    return 0, [ln.strip() for ln in body.splitlines() if ln.strip()], why


def http_get(target, url, timeout=40.0):
    """GET 一个**环回**地址 → `(rc, 正文, 说明)`。远端经 ssh 在该机上打，本机直接打。"""
    if _is_local(target):
        import urllib.request
        try:
            with urllib.request.urlopen(url, timeout=min(timeout, 10.0)) as resp:
                return 0, resp.read().decode("utf-8", "replace"), ""
        except Exception as exc:                        # noqa: BLE001 —— 打不通就是采集失败
            return None, "", "打不通：%s" % exc
    return run_remote(target, "curl -s --max-time 8 %s" % url, timeout)


def node_dirs(name, target):
    """该节点上「NATS 配置目录」与「运行目录」的绝对路径（**真值全部来自 L0 档案 / 部署表**）。"""
    runtime = _expand(target.get("runtime_dir") or "")
    if node_profile.target_flavour(name) == "nt":
        conf_dir = (runtime.rstrip("/\\") + "/nats") if runtime else ""
    else:
        conf_dir = node_profile.nats_conf_dir_posix()
    out = []
    for path in (conf_dir, runtime):
        if path and path not in out:
            out.append(path)
    return conf_dir, out


def collect_live(hubs, targets, timeout=40.0):
    """只读采真机 → `artifacts`。远端只发 `cat` / `ls` / `curl -s`，一个写操作都没有。"""
    import member_roster                                 # noqa: E402 —— L0 名册档案（期望侧唯一真源）
    nodes = sorted(targets)
    art = {"hubs": sorted(hubs), "nodes": nodes,
           "conf": {}, "connz": {}, "routez": {}, "creds": {}, "errors": [],
           #: 第 4 类采集对象：**档案**（刀29）。它不是「实现」而是输入数据，所以跟
           #: conf / creds 一样放进 artifacts —— `analyze()` 因此仍是纯函数、签名不变。
           "roster_rows": [tuple(r) for r in member_roster.ROSTER]}

    def err(node, what, why):
        art["errors"].append({"kind": "collect_error", "node": node,
                              "what": what, "why": str(why)[:200]})

    for name in nodes:
        target = targets[name]
        conf_dir, dirs = node_dirs(name, target)
        art["conf"][name] = None
        art["creds"][name] = {}

        # --- nats.conf（不在 = 该机没有 nats 配置，不是错）
        if conf_dir:
            rc, body, why = read_file(target, conf_dir + "/nats.conf", timeout)
            if rc is None:
                err(name, "nats.conf", why or "采集失败")
            elif rc == 0:
                if not body.strip():
                    err(name, "nats.conf", "配置文件存在但内容为空")
                else:
                    art["conf"][name] = body

        # --- 凭据文件：两个候选目录都查，成员名从文件名推；**当场换成 sha256**
        for d in dirs:
            rc, names, why = list_dir(target, d, timeout)
            if rc is None:
                err(name, "列目录 %s" % os.path.basename(d.rstrip("/\\")), why or "采集失败")
                continue
            if rc != 0:
                continue                                # 目录不在 —— 不是错
            for filename in names:
                if filename in art["creds"][name]:
                    continue                            # 同名先到先得（两个候选目录撞名）
                if not any(fnmatch.fnmatch(filename, g) for g, _rx in CRED_PATTERNS):
                    continue
                crc, content, cwhy = read_file(target, d + "/" + filename, timeout)
                if crc is None:
                    err(name, "读凭据 %s" % filename, cwhy or "采集失败")
                    continue
                if crc != 0:
                    continue
                #: 口令明文到此为止 —— 只留摘要，明文连 artifacts 都进不去
                art["creds"][name][filename] = sha256_hex(content.strip())

        # --- 中枢才有监控口（只绑环回，故经 ssh 在该机上打）
        if name not in art["hubs"]:
            continue
        for what in ("connz", "routez"):
            url = "http://127.0.0.1:%d/%s" % (MONITOR_PORT, what)
            rc, body, why = http_get(target, url, timeout)
            art[what][name] = None
            if rc is None or rc != 0:
                err(name, what, why or "curl rc=%s" % rc)
                continue
            try:
                art[what][name] = json.loads(body)
            except ValueError as exc:
                err(name, what, "监控口回的不是 JSON：%s" % exc)

    for hub in art["hubs"]:
        if hub in targets and art["conf"].get(hub) is None \
                and not any(e["node"] == hub and e["what"] == "nats.conf" for e in art["errors"]):
            err(hub, "nats.conf", "中枢上没有 nats.conf")
        if art["conf"].get(hub) and not parse_conf(art["conf"][hub])["users"]:
            err(hub, "nats.conf", "中枢配置里解析不出任何用户")
    return art


# ============================================================ 采集面 B：冻结夹具（离线）

def collect_fixtures(root, hubs_override=None):
    """从夹具目录读**同一形状**的 artifacts（装置只走这条，不联网）。

    目录结构::

        <DIR>/case.json                 可选 {"hubs": [...], "nodes": [...]}
        <DIR>/roster.json               名册档案（JSON 数组的数组，4 元组全字符串；
                                        **缺 = 期望侧没注入 ⇒ 采集失败**，不回退 conf 并集）
        <DIR>/<node>/nats.conf          该节点的配置（缺 = 该机没有 nats 配置）
        <DIR>/<node>/connz.json         中枢才有
        <DIR>/<node>/routez.json        中枢才有
        <DIR>/<node>/creds/<文件名>      落地的凭据文件（`<m>.pass` / `nats-<m>.pass`）
    """
    art = {"hubs": [], "nodes": [], "conf": {}, "connz": {}, "routez": {},
           "creds": {}, "errors": [], "roster_rows": None}

    def err(node, what, why):
        art["errors"].append({"kind": "collect_error", "node": node,
                              "what": what, "why": str(why)[:200]})

    if not os.path.isdir(root):
        err("", "夹具目录", "夹具目录不存在：%s" % os.path.basename(root.rstrip("/\\")))
        return art

    # --- 名册档案（刀29）：夹具里的 `roster.json` 顶替真机上的 L0 `member_roster`。
    #     **缺文件不回退** —— 留 `None` 交给 `analyze()` 记采集失败（退出码 2）。
    roster_path = os.path.join(root, "roster.json")
    if os.path.isfile(roster_path):
        try:
            with io.open(roster_path, encoding="utf-8") as fh:
                art["roster_rows"] = [tuple(row) for row in (json.load(fh) or [])]
        except (ValueError, TypeError) as exc:
            err("-", "roster.json", "名册档案不是合法 JSON 数组的数组：%s" % exc)

    nodes, hubs = [], []
    case_path = os.path.join(root, "case.json")
    if os.path.isfile(case_path):
        try:
            with io.open(case_path, encoding="utf-8") as fh:
                case = json.load(fh) or {}
            nodes = list(case.get("nodes") or [])
            hubs = list(case.get("hubs") or [])
        except ValueError as exc:
            err("", "case.json", "夹具清单不是合法 JSON：%s" % exc)
    if not nodes:
        nodes = sorted(d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d)))
    if not hubs:
        hubs = [n for n in nodes if os.path.isfile(os.path.join(root, n, "connz.json"))]
    if hubs_override:
        hubs = list(hubs_override)

    art["nodes"], art["hubs"] = sorted(nodes), sorted(hubs)
    for name in art["nodes"]:
        base = os.path.join(root, name)
        art["conf"][name] = None
        art["creds"][name] = {}
        conf_path = os.path.join(base, "nats.conf")
        if os.path.isfile(conf_path):
            text = io.open(conf_path, encoding="utf-8", errors="replace").read()
            if not text.strip():
                err(name, "nats.conf", "配置文件存在但内容为空")
            else:
                art["conf"][name] = text
        cred_dir = os.path.join(base, "creds")
        if os.path.isdir(cred_dir):
            for filename in sorted(os.listdir(cred_dir)):
                if not any(fnmatch.fnmatch(filename, g) for g, _rx in CRED_PATTERNS):
                    continue
                content = io.open(os.path.join(cred_dir, filename),
                                  encoding="utf-8", errors="replace").read()
                art["creds"][name][filename] = sha256_hex(content.strip())
        for what in ("connz", "routez"):
            art[what][name] = None
            if name not in art["hubs"]:
                continue
            path = os.path.join(base, what + ".json")
            if not os.path.isfile(path):
                err(name, what, "中枢夹具缺 %s.json" % what)
                continue
            try:
                art[what][name] = json.loads(
                    io.open(path, encoding="utf-8", errors="replace").read())
            except ValueError as exc:
                err(name, what, "监控口夹具不是 JSON：%s" % exc)

    for hub in art["hubs"]:
        if art["conf"].get(hub) is None \
                and not any(e["node"] == hub and e["what"] == "nats.conf" for e in art["errors"]):
            err(hub, "nats.conf", "中枢上没有 nats.conf")
        if art["conf"].get(hub) and not parse_conf(art["conf"][hub])["users"]:
            err(hub, "nats.conf", "中枢配置里解析不出任何用户")
    return art


# ============================================================ 人读输出 / 基线比对

def render(report):
    """六项各一行，差异逐条缩进打印。"""
    out = ["=" * 78,
           "成员入网只读对账 · %s" % report["schema"],
           "中枢 %s ｜ 节点 %s ｜ 名册 %d 人"
           % ("、".join(report["hub_list"]) or "—",
              "、".join(report["node_list"]) or "—", len(report["roster"])),
           "名册来源  L0 档案 agent-team/member_roster.py（顺序即规范序）",
           "=" * 78]
    for key, name in CHECKS:
        items = report["findings"][key]
        out.append("%s %-18s %s 差异 %d 条" % (key, name, "✓" if not items else "✗", len(items)))
        for item in items:
            out.append("    · " + _one_line(item))
    src = report.get("live_sources") or {}
    out.append("")
    out.append("活连接成员身份来源（两个来源都记，不许只留一个）：")
    for name in sorted(src):
        out.append("    %-16s %s" % (name, "、".join(src[name]) or "—"))
    if report["collect_errors"]:
        out.append("")
        out.append("采集失败 %d 条（退出码 2）：" % len(report["collect_errors"]))
        for item in report["collect_errors"]:
            out.append("    ✗ %s / %s：%s" % (item.get("node") or "—",
                                              item.get("what"), item.get("why")))
    out.append("")
    out.append("合计差异 %d 条%s" % (sum(report["counts"].values()),
                                     "，采集完整" if not report["collect_errors"] else ""))
    return "\n".join(out)


def _one_line(item):
    """一条 finding 压成一行（键名排序；口令永远只以 sha256_8 出现）。"""
    kind = item.get("kind", "?")
    rest = ", ".join("%s=%s" % (k, item[k]) for k in sorted(item) if k != "kind")
    return "%s  %s" % (kind, rest)


def diff_baseline(report, baseline):
    """本次 vs 冻结基线 → 逐条「新增 / 消失」。返回 `(行列表, 是否一致)`。

    比的是**判据身份**（`_identity()`：摘掉 `VOLATILE_KEYS` 里的观测数字），打印的也是身份 ——
    于是「同一裁决、观测数字随钟变」不再被误报成「新增 + 消失」（刀28）。
    「差异消失也算漂移」的双向严格**原样保留**：裁决不见了照样 `same=False`。
    """
    lines, same = [], True
    for key in COMPARE_KEYS:
        cur, old = report.get(key), baseline.get(key)
        if key == "findings":
            for ck in CHECK_KEYS:
                a = {json.dumps(_identity(x), sort_keys=True, ensure_ascii=False) for x in (cur or {}).get(ck, [])}
                b = {json.dumps(_identity(x), sort_keys=True, ensure_ascii=False) for x in (old or {}).get(ck, [])}
                for text in sorted(a - b):
                    same = False
                    lines.append("  ✗ %s 新增  %s" % (ck, _one_line(json.loads(text))))
                for text in sorted(b - a):
                    same = False
                    lines.append("  ✗ %s 消失  %s" % (ck, _one_line(json.loads(text))))
            continue
        if cur != old:
            same = False
            lines.append("  ✗ %s 变了" % key)
            lines.append("      基线 %s" % json.dumps(old, sort_keys=True, ensure_ascii=False))
            lines.append("      本次 %s" % json.dumps(cur, sort_keys=True, ensure_ascii=False))
    return lines, same


# ============================================================ main

def build_report(args):
    """按 `--live` / `--fixtures` 采一次 + 算一次 → 报告。"""
    hubs = [h.strip() for h in (args.hub or "").split(",") if h.strip()] or list(HUBS_DEFAULT)
    if args.fixtures:
        artifacts = collect_fixtures(args.fixtures, hubs_override=hubs if args.hub else None)
    else:
        targets = load_targets()
        missing = [h for h in hubs if h not in targets]
        artifacts = collect_live(hubs, targets, timeout=args.timeout)
        for h in missing:
            artifacts["errors"].append({"kind": "collect_error", "node": h, "what": "部署表",
                                        "why": "内部真值表里没有这个节点，拿不到 ssh 目标"})
    import gen_nats_conf                                 # noqa: E402 —— L1 角色模板的唯一出处
    return analyze(artifacts, gen_nats_conf.perm_list)


def main(argv=None):
    ap = argparse.ArgumentParser(description="成员入网只读对账（阶段5 第一刀）")
    ap.add_argument("--live", action="store_true", help="只读采真机")
    ap.add_argument("--fixtures", default="", help="从冻结夹具目录采（离线）")
    ap.add_argument("--check", action="store_true", help="采一次 + 与冻结基线逐项比对")
    ap.add_argument("--write-baseline", dest="write_baseline", default="",
                    help="把本次结果冻结成基线文件")
    ap.add_argument("--baseline", default=BASELINE_PATH, help="基线文件路径")
    ap.add_argument("--hub", default="", help="覆盖中枢清单（逗号分隔；**测试用**）")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    ap.add_argument("--out", default="", help="把 JSON 另存到文件")
    ap.add_argument("--timeout", type=float, default=40.0, help="单条远端命令超时（秒）")
    args = ap.parse_args(argv)

    if not (args.live or args.fixtures or args.check or args.write_baseline):
        ap.error("要么 --live，要么 --fixtures <DIR>，要么 --check")

    report = build_report(args)
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

    if args.write_baseline:
        with io.open(args.write_baseline, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        if not args.json:
            print("\n已冻结基线 → %s" % os.path.basename(args.write_baseline))
        return EXIT_OK

    if args.check:
        if not os.path.isfile(args.baseline):
            print("\n找不到冻结基线 %s —— 先跑 --live --write-baseline"
                  % os.path.basename(args.baseline))
            return EXIT_COLLECT
        with io.open(args.baseline, encoding="utf-8") as fh:
            baseline = json.load(fh)
        lines, same = diff_baseline(report, baseline)
        if not args.json:
            print("\n[与冻结基线比对] %s" % os.path.basename(args.baseline))
            if VOLATILE_KEYS:
                print("  身份口径：已摘出观测量 %s —— 判据一条未放宽（裁决消失/非观测字段变化照旧报漂移）"
                      % "、".join(sorted(VOLATILE_KEYS)))
            print("\n".join(lines) if lines else "  ✓ 逐项一致")
        return EXIT_OK if same else EXIT_DRIFT

    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
