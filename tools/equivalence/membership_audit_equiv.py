# -*- coding: utf-8 -*-
"""刀25 等价 / 冻结矩阵装置 —— 「成员入网只读对账」这把尺子**自己准不准**的证据。

它证明什么（以及为什么不是自证）
--------------------------------
正件 `tools/nats_membership_audit.py` 与旧侧 `_old/membership_audit_old_k25.py` 是
**两条互不相识的算法路径**（正则+括号配平 vs 逐行状态机+顶层逗号切词；正则时长解析 vs
手写扫描器；角色模板一个 import L1 `gen_nats_conf.perm_list`、一个自己按契约文档拼）。
本装置把同一批**冻结夹具**同时喂给两路，逐格比**规范化 JSON**（键名排序、缩进固定）。
两边一致 → 这把尺子的刻度可信；任何一边写歪 → 立刻不一致。

夹具从哪来
----------
**内嵌在本件里**（常量 + 构造函数），跑的时候现落到临时目录，跑完即删 ——
所以装置**完全离线**、不碰任何真机、仓里也不多出一堆数据文件。
`--emit <DIR>` 可以把这批夹具原样落盘，供主控独立重跑与人眼核对。

夹具里的 `permissions` 串**由 `gen_nats_conf.perm_list` 现生成**（它是「现网观测到的样子」，
不是判据）；判据那一侧仍是两份独立模板各算各的 —— 装置开跑先把这两份模板逐字符对一遍（A 段）。

覆盖面
------
    A 段  角色模板交叉核对（L1 `perm_list` vs 旧侧自拼模板）          12 格
    B 段  时长解析交叉核对（正则一路 vs 手写扫描器一路）              14 格
    C 段  10 例冻结夹具 × 8 格 = 80 格
          （规范 JSON 逐字节一致 1 + 六项条数命中冻结期望 6 + CLI 退出码 1）
    D 段  `--check` 的**身份口径**（刀28 加；新侧 vs 刀28 前快照）      6 格
          （观测量漂移必须判一致 · 改前必须判漂移 · 裁决消失/新增必须判漂移 ·
            非观测字段漂移必须判漂移 · 两条报告的判据身份必须逐字节相等）
    E 段  **期望侧不再自证**（刀29 加；新侧 vs 刀29 前快照）            20 格
          （5 场景 × 4 格：改前六项条数 · 改后六项条数 · 改后新增的 kind 集合 ·
            两侧规范 JSON 逐字节比较的结果）
    合计 132 格

E 段在证明什么（这是刀29 的价值本身）
------------------------------------
刀29 之前，对账器的**期望成员集 = 被检对象自己**（两台中枢 conf 的用户并集）——
于是「**两台中枢一起被手改**」（加人 / 删人）它**彻底无感**。刀29 把期望侧切到
L0 档案 `agent-team/member_roster.py`。E 段把**同一份 artifacts**（同一夹具 + 同一份
`roster.json`）同时喂给「改前」`_old/membership_audit_old_k29.py`（正件在刀29 前的逐字节快照，
**函数级直调** `analyze()` —— 它的 CLI 不吃档案）与「改后」正件，钉住两件事：
① 现网态（档案 ≡ conf）两侧输出**逐字节相同** —— 切换是**已知等价的替换**；
② 手改入网 / 手改退网，改前全 0、改后必红。

夹具里的 `roster.json`
----------------------
`materialize()` 每例都落一份 `roster.json`（4 元组数组）。**既有 10 例**的行取自该例
**hub-a 的 conf 用户**（hub-a 解析不出用户时回落 hub-b —— 与正件 `ref_hub` 同口径，
`malformed` 例就是这种形态），于是 10 例的六项条数期望**逐格不变**。

用法
----
    python tools/equivalence/membership_audit_equiv.py                # 跑一遍矩阵
    python tools/equivalence/membership_audit_equiv.py --runs 2       # 连跑两轮，自证确定性
    python tools/equivalence/membership_audit_equiv.py --negative     # 强制负控：注毒五处必须报红
    python tools/equivalence/membership_audit_equiv.py --emit <DIR>   # 把冻结夹具落盘

退出码：0 = 全部一致 / 1 = 有不一致（或负控没能判红）。
装置源码内**不含**任何真实宿主地址、任何真实口令：夹具主机名一律 `*.example.invalid`，
夹具口令一律 2 字符合成串（`p1` / `p2` …）。
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
REPO = os.path.dirname(TOOLS)
OLD_DIR = os.path.join(HERE, "_old")
AGENT_TEAM = os.path.join(REPO, "agent-team")
AUDIT_PY = os.path.join(TOOLS, "nats_membership_audit.py")

for p in (TOOLS, OLD_DIR, AGENT_TEAM):
    if p not in sys.path:
        sys.path.insert(0, p)

import nats_membership_audit as NEW                      # noqa: E402 —— 新侧（正件）
import membership_audit_old_k25 as OLD                   # noqa: E402 —— 旧侧（独立第二实现）
import membership_audit_old_k28 as OLDK28                 # noqa: E402 —— 刀28 前的正件（D 段「改前」锚）
import membership_audit_old_k29 as OLDK29                 # noqa: E402 —— 刀29 前的正件（E 段「改前」锚）
import gen_nats_conf                                     # noqa: E402 —— L1 角色模板唯一出处

#: 夹具里的合成主机名（`.invalid` 是 RFC2606 保留后缀，永远不可能解析到真机）
H_A, H_B = "hub-a.example.invalid", "hub-b.example.invalid"
#: 夹具口令：一律 2 字符合成串（够短，既撞不上扫密门禁，也一眼看得出是假的）
PW = {"alpha": "p1", "beta": "p2", "gamma": "p3", "delta": "p4"}


# ============================================================ 夹具构件

def hub_conf(node, host, users, perms=True, cluster="agent-team",
             client_port=9930, cluster_port=9931, http_port=9932, routes_to=None):
    """合成一份 v2 中枢 `nats.conf`（形状照着 `gen_nats_conf.py` 的产物来）。

    `users` = `[(成员, 口令, permissions 串 | None)]`。`permissions` 串由调用方给 ——
    默认是 `gen_nats_conf.perm_list(成员)`，即「现网观测到的规范形」。
    """
    lines = ["# 合成夹具（非真机），仅供 tools/equivalence/membership_audit_equiv.py 使用",
             'server_name: "%s-nats"' % node,
             "listen: %s:%d" % (host, client_port),
             "http: 127.0.0.1:%d" % http_port,
             "",
             "cluster {",
             '  name: "%s"' % cluster,
             "  listen: %s:%d" % (host, cluster_port),
             '  authorization { user: "route", password: "rp", timeout: 3 }']
    if routes_to:
        lines.append('  routes = [ "nats-route://route:rp@%s:%d" ]' % (routes_to, cluster_port))
    lines += ["}", "", "authorization {", "  timeout: 3", "  users = ["]
    for i, (user, password, perm) in enumerate(users):
        tail = "," if i < len(users) - 1 else ""
        if perms and perm is not None:
            lines.append('    { user: "%s", password: "%s", permissions: { %s } }%s'
                         % (user, password, perm, tail))
        else:
            lines.append('    { user: "%s", password: "%s" }%s' % (user, password, tail))
    lines += ["  ]", "}", ""]
    return "\n".join(lines)


def legacy_conf(users):
    """合成一份 **v1 残留形状**的 conf：4222 / TLS / JetStream / 没有 `permissions` 块。"""
    lines = ["# 合成夹具（非真机）：v1 残留形状",
             "port: 4222",
             'http: "127.0.0.1:8222"',
             "tls {",
             '  cert_file: "/srv/k25-fixture/server.pem"',
             "  verify: true",
             "  timeout: 2",
             "}",
             "jetstream {",
             "  max_memory_store: 64MB",
             "}",
             "authorization {",
             "  users = ["]
    for user, password in users:
        lines.append('    { user: "%s", password: "%s" },' % (user, password))
    lines += ["  ]", "}", ""]
    return "\n".join(lines)


def connz(members, with_auth=False, extra=()):
    """合成 `/connz` 应答。`with_auth=False` = NATS 给不出 `authorized_user`（真机实测形态）。"""
    conns = []
    for i, member in enumerate(list(members) + list(extra)):
        conn = {"cid": 100 + i, "kind": "Client", "type": "nats",
                "port": 40000 + i, "start": "2026-01-01T00:00:00Z",
                "last_activity": "2026-01-01T00:00:01Z", "rtt": "1ms",
                "uptime": "1h0m0s", "idle": "0s", "pending_bytes": 0,
                "in_msgs": 1, "out_msgs": 0, "in_bytes": 10, "out_bytes": 0,
                "subscriptions": 1, "name": "teamd-" + member,
                "lang": "py", "version": "nats_client-1.0"}
        if with_auth:
            conn["authorized_user"] = member
        conns.append(conn)
    return {"server_id": "FIXTURE", "now": "2026-01-01T00:00:02Z",
            "num_connections": len(conns), "total": len(conns),
            "offset": 0, "limit": 1024, "connections": conns}


def routez(idles, remote):
    """合成 `/routez` 应答；`idles` 是人读时长串列表（条目数即 `num_routes`）。"""
    routes = []
    for i, idle in enumerate(idles):
        routes.append({"rid": 10 + i, "remote_id": "FIXTURE",
                       "remote_name": remote + "-nats", "did_solicit": True,
                       "is_configured": True, "port": 9931,
                       "start": "2026-01-01T00:00:00Z",
                       "last_activity": "2026-01-01T00:00:01Z", "rtt": "1ms",
                       "uptime": "9d0m0s", "idle": idle, "pending_size": 0,
                       "in_msgs": 0, "out_msgs": 0, "in_bytes": 0, "out_bytes": 0,
                       "subscriptions": 0, "compression": "off"})
    return {"server_id": "FIXTURE", "server_name": remote + "-nats",
            "now": "2026-01-01T00:00:02Z", "num_routes": len(routes), "routes": routes}


def _roster(names):
    """`[(成员, 口令, 规范 permissions 串)]` —— permissions 由 L1 唯一出处现生成。"""
    return [(n, PW[n], gen_nats_conf.perm_list(n)) for n in names]


# ============================================================ 10 例冻结夹具

def _clean(names=("alpha", "beta", "gamma")):
    """零差异例的骨架：两中枢一致、权限规范形、凭据齐、无僵尸、无遗留、单路由。"""
    users = _roster(names)
    return {
        "case": {"hubs": ["hub-a", "hub-b"], "nodes": ["edge-n", "hub-a", "hub-b"]},
        "nodes": {
            "hub-a": {"nats.conf": hub_conf("hub-a", H_A, users),
                      "connz.json": connz(["alpha", "beta"]),
                      "routez.json": routez(["0s"], "hub-b"),
                      "creds": {"alpha.pass": PW["alpha"]}},
            "hub-b": {"nats.conf": hub_conf("hub-b", H_B, users, routes_to=H_A),
                      "connz.json": connz(["gamma"]),
                      "routez.json": routez(["3s"], "hub-a"),
                      "creds": {"beta.pass": PW["beta"]}},
            "edge-n": {"creds": {"nats-gamma.pass": PW["gamma"]}},
        },
    }


def _case_clean():
    return _clean()


def _case_zombie():
    """名册多一个无 pass / 无连接的成员 → M4 1 条。"""
    fx = _clean(("alpha", "beta", "gamma", "delta"))
    return fx


def _case_cred_mismatch():
    """某机 pass 文件内容与 conf 口令不同 → M3 1 条 cred_mismatch。"""
    fx = _clean()
    fx["nodes"]["hub-a"]["creds"]["alpha.pass"] = "zz"
    return fx


def _case_cred_orphan():
    """某机有 `nats-<x>.pass` 而名册无 `x` → M3 1 条 cred_orphan。"""
    fx = _clean()
    fx["nodes"]["edge-n"]["creds"]["nats-omega.pass"] = "p9"
    return fx


def _case_perm_drift():
    """某 user 的 permissions 被改（心跳主题放成通配）→ M2 1 条 perm_drift。"""
    users = _roster(("alpha", "beta", "gamma"))
    drifted = []
    for user, password, perm in users:
        if user == "beta":
            perm = perm.replace('"team.hb.beta"', '"team.hb.>"')
        drifted.append((user, password, perm))
    fx = _clean()
    fx["nodes"]["hub-a"]["nats.conf"] = hub_conf("hub-a", H_A, drifted)
    return fx


def _case_hub_divergence():
    """两中枢同一成员口令不同 → M1 差集各 1 条。"""
    fx = _clean()
    users = [(u, ("p9" if u == "gamma" else p), perm)
             for u, p, perm in _roster(("alpha", "beta", "gamma"))]
    fx["nodes"]["hub-b"]["nats.conf"] = hub_conf("hub-b", H_B, users, routes_to=H_A)
    return fx


def _case_outside_roster():
    """活连接里出现名册外的 user → M5 1 条（硬违规）。"""
    fx = _clean()
    fx["nodes"]["hub-a"]["connz.json"] = connz(["alpha", "beta"], extra=["intruder"])
    return fx


def _case_legacy_hub():
    """非中枢节点上跑着 v1 形状的 nats-server（4222 / 无 permissions 块）→ M6 legacy_hub。"""
    fx = _clean()
    fx["case"]["nodes"] = ["edge-n", "hub-a", "hub-b", "legacy-n"]
    fx["nodes"]["legacy-n"] = {"nats.conf": legacy_conf([("alpha", PW["alpha"]),
                                                         ("zeta", "pz")])}
    return fx


def _case_dup_route():
    """两侧各 4 条路由、含 3 条陈旧（>1h）→ M6 duplicate_route（按集群聚合成一条）。"""
    fx = _clean()
    fx["nodes"]["hub-a"]["routez.json"] = routez(
        ["6d18h2m9s", "1d9h47m38s", "5h55m10s", "5s"], "hub-b")
    fx["nodes"]["hub-b"]["routez.json"] = routez(
        ["6d18h3m9s", "1d9h48m38s", "5h56m10s", "8s"], "hub-a")
    return fx


def _case_malformed():
    """空 conf + 截断 JSON → 一律记 collect_error，不许抛未捕获异常；CLI 退出码 2。"""
    fx = _clean()
    fx["nodes"]["hub-a"]["nats.conf"] = "\n"               # 文件在、内容空
    fx["nodes"]["hub-b"]["connz.json"] = "{\"connections\": [ {\"cid\": 1,"   # 截断
    return fx


#: 冻结期望：每例的六项条数。**这是契约，不是「跑出来是多少就记多少」** ——
#: 改了判定口径就会在这里立刻撞红。
CASES = (
    ("clean",           _case_clean,           {"M1": 0, "M2": 0, "M3": 0, "M4": 0, "M5": 0, "M6": 0}, 0),
    ("zombie",          _case_zombie,          {"M1": 0, "M2": 0, "M3": 0, "M4": 1, "M5": 0, "M6": 0}, 0),
    ("cred_mismatch",   _case_cred_mismatch,   {"M1": 0, "M2": 0, "M3": 1, "M4": 0, "M5": 0, "M6": 0}, 0),
    ("cred_orphan",     _case_cred_orphan,     {"M1": 0, "M2": 0, "M3": 1, "M4": 0, "M5": 0, "M6": 0}, 0),
    ("perm_drift",      _case_perm_drift,      {"M1": 0, "M2": 1, "M3": 0, "M4": 0, "M5": 0, "M6": 0}, 0),
    ("hub_divergence",  _case_hub_divergence,  {"M1": 2, "M2": 0, "M3": 0, "M4": 0, "M5": 0, "M6": 0}, 0),
    ("outside_roster",  _case_outside_roster,  {"M1": 0, "M2": 0, "M3": 0, "M4": 0, "M5": 1, "M6": 0}, 0),
    ("legacy_hub",      _case_legacy_hub,      {"M1": 0, "M2": 0, "M3": 0, "M4": 0, "M5": 0, "M6": 1}, 0),
    ("dup_route",       _case_dup_route,       {"M1": 0, "M2": 0, "M3": 0, "M4": 0, "M5": 0, "M6": 1}, 0),
    ("malformed",       _case_malformed,       {"M1": 0, "M2": 0, "M3": 0, "M4": 0, "M5": 0, "M6": 0}, 2),
)


# ============================================================ 夹具落盘

def _write(path, text):
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def _cred_owner(fx):
    """成员 → 该例中持有其凭据文件的节点（先到先得；没有则 `None`）。"""
    owner = {}
    for node, files in sorted(fx["nodes"].items()):
        for filename in sorted((files.get("creds") or {})):
            member = NEW._member_of(filename)
            if member is not None and member not in owner:
                owner[member] = node
    return owner


def roster_rows_of(fx):
    """既有 10 例的 `roster.json` 行 —— 取自该例 **hub-a 的 conf 用户**（顺序照 conf）。

    hub-a 解析不出用户时**回落 hub-b**（与正件 `ref_hub = 第一台有 conf 的中枢` 同口径）：
    `malformed` 例的 hub-a conf 是空文件，不回落的话名册会是空的、M5 会平白多两条，
    10 例的冻结条数期望就守不住了。`cred_node` 取「该例中拥有该成员凭据文件的那个节点」，
    没有则 `hub-a`；`role` / `cred_kind` 固定 —— 本刀的正件**不消费**这两列。

    **顺序取字典序**，不是照抄 conf 行序：L0 档案契约写死「顺序即规范序（字典序）」。
    10 例里只有 `zombie` 的 conf 行序不是字典序（… gamma, delta），照抄会让 C 段
    `zombie/json` 格撞红（旧侧第二实现的 `roster` 是 sorted 的）—— 那是**夹具没按契约造**，
    不是判据不一致。六项条数期望不受顺序影响，`CASES` 那张表逐格不变。
    """
    owner = _cred_owner(fx)
    users = []
    for hub in ("hub-a", "hub-b"):
        conf = (fx["nodes"].get(hub) or {}).get("nats.conf") or ""
        users = NEW.parse_conf(conf)["users"]
        if users:
            break
    names = sorted(user for user, _pw, _perm in users)
    return [[n, "member", owner.get(n, "hub-a"), "conf"] for n in names]


def materialize(fx, root, roster_rows=None):
    """把一例夹具落到 `root/`。目录结构见模块文档串与刀25 报告。

    `roster_rows` 不给时按 `roster_rows_of(fx)` 推（= 该例 conf 的成员），即「档案 ≡ conf」的
    现网态；E 段与新增负控会显式传一份**与 conf 不同**的档案，那才是本刀要抓的场面。
    """
    rows = roster_rows_of(fx) if roster_rows is None else roster_rows
    _write(os.path.join(root, "roster.json"),
           json.dumps(rows, ensure_ascii=False, indent=2) + "\n")
    _write(os.path.join(root, "case.json"),
           json.dumps(fx["case"], ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    for node, files in sorted(fx["nodes"].items()):
        base = os.path.join(root, node)
        if not os.path.isdir(base):
            os.makedirs(base)
        for name, body in sorted(files.items()):
            if name == "creds":
                for filename, content in sorted(body.items()):
                    _write(os.path.join(base, "creds", filename), content)
            elif isinstance(body, str):
                _write(os.path.join(base, name), body)
            else:
                _write(os.path.join(base, name),
                       json.dumps(body, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    return root


def emit_all(dest):
    """`--emit`：把 10 例夹具原样落盘，供独立重跑与人眼核对。"""
    for name, build, _want, _rc in CASES:
        target = os.path.join(dest, name)
        if os.path.isdir(target):
            shutil.rmtree(target)
        materialize(build(), target)
    print("已落盘 %d 例冻结夹具 → %s" % (len(CASES), dest))
    return 0


# ============================================================ 两侧求值

def new_report(root):
    """新侧：正件的采集器 + 正件的 `analyze()`，模板注入 L1 `gen_nats_conf.perm_list`。"""
    return NEW.analyze(NEW.collect_fixtures(root), gen_nats_conf.perm_list)


def old_report(root):
    """旧侧：独立第二实现的采集器 + 它自己的 `analyze()`（用它自己那份模板）。"""
    return OLD.report_of(root)


def cli_rc(root):
    """真起一次 CLI 子进程，只看退出码（`--fixtures` 必须离线可跑）。"""
    proc = subprocess.run([sys.executable, AUDIT_PY, "--fixtures", root, "--json"],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
    return proc.returncode


# ============================================================ A / B / C 三段

TEMPLATE_NAMES = ("alpha", "beta", "gamma", "delta", "bbgw", "claudecode", "codex",
                  "hermes", "lobster", "openclaw-nb", "piagent", "top001")

DURATIONS = ("0s", "5s", "8s", "22s", "322µs", "1.278863724s", "17.72075ms",
             "6h0m14s", "5h55m10s", "1d9h47m38s", "6d18h7m50s", "2d8h58m21s",
             "", "not-a-duration")


# ============================================================ D 段：身份 vs 观测量（刀28）

def _dup_route_report(prefix, stale, num_routes=(4, 4)):
    """造一份**真来的**报告（冻结夹具 `dup_route` → `analyze()`），再把该条裁决的**观测数字**改成给定值。

    只在观测字段上动手 —— 这正是真机上发生的事：NATS 重启后路由 idle 归零，
    约 1 小时后计数自己由 0 变正（刀28 实测）。
    """
    root = materialize(_case_dup_route(), os.path.join(prefix, "dup"))
    rep = new_report(root)
    hits = [it for it in rep["findings"]["M6"] if it.get("kind") == "duplicate_route"]
    if len(hits) != 1:
        raise AssertionError("夹具 dup_route 应恰好产出 1 条 duplicate_route，实测 %d" % len(hits))
    for item in hits:
        for j, hub in enumerate(item.get("hubs") or []):
            hub["stale_routes"] = stale[j]
            hub["num_routes"] = num_routes[j]
    return rep


def run_identity_cases():
    """D 段：`--check` 的**身份口径**。

    判据不是「两侧算法一致」，而是两件事：① **同一裁决**在观测数字漂移时还敢不敢假红
    （刀28 要修的就是它）② **裁决本身**变了时还报不报红（**不许被修坏**）。
    改前一侧由 `_old/membership_audit_old_k28.py`（正件在刀28 前逐字节快照）扮演。
    """
    cells, bad = [], 0
    tmp = tempfile.mkdtemp(prefix="k28id-")

    def cell(name, ok, note):
        nonlocal bad
        bad += 0 if ok else 1
        cells.append(("D", name, ok, note))

    try:
        base = _dup_route_report(tmp, stale=(0, 0))            # 基线：路由刚重启过 → 陈旧计数 0
        clock = _dup_route_report(tmp, stale=(2, 2))           # 本班实测：同一裁决，计数 2

        _, same = NEW.diff_baseline(clock, base)
        cell("obs_drift_green", same,
             "同一裁决、仅 stale_routes 0→2 → 判**一致**（刀28 修的时间脆性）"
             if same else "**仍判漂移** —— 时间脆性没修掉")

        _, same_old = OLDK28.diff_baseline(clock, base)
        cell("obs_drift_red_before", not same_old,
             "刀28 前（快照）同一输入 → 判**漂移**（证明本刀真改了行为，不是空转）"
             if not same_old else "**改前也判一致** —— 那本刀无从谈起")

        gone = copy.deepcopy(clock)
        gone["findings"]["M6"] = [x for x in gone["findings"]["M6"]
                                  if x.get("kind") != "duplicate_route"]
        gone["counts"]["M6"] = len(gone["findings"]["M6"])
        _, same_gone = NEW.diff_baseline(gone, base)
        cell("verdict_gone_red", not same_gone,
             "裁决**消失**（不再有重复路由）→ 仍判漂移（「消失也算漂移」没被削）"
             if not same_gone else "**消失了也不报** —— 口径被放宽了")

        extra = copy.deepcopy(base)
        extra["findings"]["M6"].append({"kind": "legacy_hub", "node": "zz-n", "bad_ports": [4222],
                                        "has_permissions": False, "user_count": 1, "why": "合成"})
        extra["counts"]["M6"] = len(extra["findings"]["M6"])
        _, same_extra = NEW.diff_baseline(extra, base)
        cell("verdict_new_red", not same_extra,
             "冒出一条**新裁决** → 判漂移" if not same_extra else "**新裁决不报** —— 漏判")

        grown = _dup_route_report(tmp, stale=(0, 0), num_routes=(8, 4))
        _, same_grown = NEW.diff_baseline(grown, base)
        cell("nonobs_field_red", not same_grown,
             "**非观测字段**漂移（num_routes 4→8）→ 判漂移（只摘了钟那一项）"
             if not same_grown else "**非观测字段也不报** —— 摘过头了")

        same_id = (NEW.canonical(NEW._identity(clock)) == NEW.canonical(NEW._identity(base)))
        cell("identity_equal", same_id,
             "两条报告的**判据身份**逐字节相等（基线重冻结只动了观测量，没动裁决）"
             if same_id else "**身份不等** —— 基线重冻结可能掩盖了真漂移")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return cells, bad


# ============================================================ E 段：期望侧不再自证（刀29）

def _rows_for(fx, names):
    """按给定成员名**顺序**造一份 `roster.json` 行（`cred_node` 沿用该例凭据文件的归属）。"""
    owner = _cred_owner(fx)
    return [[n, "member", owner.get(n, "hub-a"), "conf"] for n in names]


def _kind_set(report):
    """一份报告里出现过的全部 kind 名（跨六项）。"""
    return {it.get("kind") for key in NEW.CHECK_KEYS for it in report["findings"][key]}


def _counts(report):
    return {k: report["counts"][k] for k in NEW.CHECK_KEYS}


def _counts_text(got):
    return " ".join("%s=%d" % (k, got[k]) for k in NEW.CHECK_KEYS)


def _e_fx_clean():
    """现网态骨架：两中枢 conf 用户 == 档案。"""
    return _clean()


def _e_fx_manual_join():
    """**两中枢一起手改加人**：conf 两边都多 `delta`，还给了它凭据文件 + 活连接（完整入网）。"""
    fx = _clean(("alpha", "beta", "gamma", "delta"))
    fx["nodes"]["edge-n"]["creds"]["nats-delta.pass"] = PW["delta"]
    fx["nodes"]["hub-a"]["connz.json"] = connz(["alpha", "beta", "delta"])
    return fx


def _e_fx_manual_leave():
    """**两中枢一起手改删人**：conf 两边都少 `gamma`，凭据文件与活连接也一并撤掉。"""
    fx = _clean(("alpha", "beta"))
    del fx["nodes"]["edge-n"]["creds"]["nats-gamma.pass"]
    fx["nodes"]["hub-b"]["connz.json"] = connz(["beta"])
    return fx


_ZERO = {"M1": 0, "M2": 0, "M3": 0, "M4": 0, "M5": 0, "M6": 0}

#: E 段 5 场景（**冻结期望**）：
#: `(名字, 夹具, 档案成员序, 改前六项条数, 改后六项条数, 改后新增的 kind 集合, 两侧 JSON 是否相同)`
E_CASES = (
    ("E1_equal", _e_fx_clean, ("alpha", "beta", "gamma"),
     _ZERO, _ZERO, frozenset(), True),
    ("E2_roster_extra", _e_fx_clean, ("alpha", "beta", "delta", "gamma"),
     _ZERO, {"M1": 2, "M2": 0, "M3": 0, "M4": 1, "M5": 0, "M6": 0},
     frozenset(("conf_missing_member", "zombie_credential")), False),
    ("E3_roster_short", _e_fx_clean, ("alpha", "beta"),
     _ZERO, {"M1": 2, "M2": 0, "M3": 0, "M4": 0, "M5": 1, "M6": 0},
     frozenset(("conf_only_member", "live_outside_roster")), False),
    ("E4_manual_join", _e_fx_manual_join, ("alpha", "beta", "gamma"),
     _ZERO, {"M1": 2, "M2": 0, "M3": 0, "M4": 0, "M5": 1, "M6": 0},
     frozenset(("conf_only_member", "live_outside_roster")), False),
    ("E5_manual_leave", _e_fx_manual_leave, ("alpha", "beta", "gamma"),
     _ZERO, {"M1": 2, "M2": 0, "M3": 0, "M4": 1, "M5": 0, "M6": 0},
     frozenset(("conf_missing_member", "zombie_credential")), False),
)


def run_expectation_cases():
    """E 段：**期望侧不再自证**。

    两侧喂**同一份** `artifacts`（同一夹具目录 + 同一份 `roster.json`），差异只在「期望成员集
    从哪来」：改前（`_old/membership_audit_old_k29.py`，刀29 前正件的逐字节快照）从 conf 并集推，
    改后（正件）从 L0 档案取。改前那一侧走**函数级直调** `analyze()` —— 它的 CLI 根本不吃档案。
    """
    cells, bad = [], 0
    tmp = tempfile.mkdtemp(prefix="k29exp-")

    def cell(name, ok, note):
        nonlocal bad
        bad += 0 if ok else 1
        cells.append(("E", name, ok, note))

    try:
        for label, build, names, want_old, want_new, want_kinds, want_same in E_CASES:
            fx = build()
            root = materialize(fx, os.path.join(tmp, label),
                               roster_rows=_rows_for(fx, names))
            art = NEW.collect_fixtures(root)          # **同一份 artifacts** 喂两侧
            old = OLDK29.analyze(art, gen_nats_conf.perm_list)
            new = NEW.analyze(art, gen_nats_conf.perm_list)

            got_old, got_new = _counts(old), _counts(new)
            cell("%s/before" % label, got_old == want_old,
                 "改前 %s（冻结期望 %s）" % (_counts_text(got_old), _counts_text(want_old)))
            cell("%s/after" % label, got_new == want_new,
                 "改后 %s（冻结期望 %s）" % (_counts_text(got_new), _counts_text(want_new)))

            got_kinds = _kind_set(new) - _kind_set(old)
            cell("%s/kinds" % label, got_kinds == set(want_kinds),
                 "改后多出的 kind %s（冻结期望 %s）"
                 % ("、".join(sorted(got_kinds)) or "—", "、".join(sorted(want_kinds)) or "—"))

            same = (NEW.canonical(old) == NEW.canonical(new))
            cell("%s/json" % label, same == want_same,
                 "两侧规范 JSON %s（冻结期望 %s）"
                 % ("逐字节相同" if same else "不同", "相同" if want_same else "不同"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return cells, bad


def run_matrix():
    """跑一遍完整矩阵 → `(格列表, 不一致数)`。格 = `(段, 名字, 是否一致, 说明)`。"""
    cells, bad = [], 0

    # --- A 段：两份角色模板逐字符对照（一份 import L1，一份自己拼）
    for name in TEMPLATE_NAMES:
        left, right = gen_nats_conf.perm_list(name), OLD.perm_template(name)
        same = (left == right)
        bad += 0 if same else 1
        cells.append(("A", "template:%s" % name, same,
                      "逐字符相同" if same else "新 %r / 旧 %r" % (left, right)))

    # --- B 段：两条时长解析路径交叉核对
    for text in DURATIONS:
        left, right = NEW.parse_duration(text), OLD.parse_duration(text)
        same = (left == right)
        bad += 0 if same else 1
        cells.append(("B", "duration:%r" % text, same,
                      "%r" % left if same else "新 %r / 旧 %r" % (left, right)))

    # --- C 段：10 例冻结夹具
    tmp = tempfile.mkdtemp(prefix="k25fx-")
    try:
        for name, build, want, want_rc in CASES:
            root = materialize(build(), os.path.join(tmp, name))
            new, old = new_report(root), old_report(root)
            n_text, o_text = NEW.canonical(new), NEW.canonical(old)
            same = (n_text == o_text)
            bad += 0 if same else 1
            cells.append(("C", "%s/json" % name, same,
                          "规范 JSON 逐字节一致" if same else _first_diff(n_text, o_text)))
            for key in NEW.CHECK_KEYS:
                got = new["counts"][key]
                hit = (got == want[key]) and (old["counts"][key] == want[key])
                bad += 0 if hit else 1
                cells.append(("C", "%s/%s" % (name, key), hit,
                              "%d 条（冻结期望 %d）" % (got, want[key])))
            rc = cli_rc(root)
            hit = (rc == want_rc)
            bad += 0 if hit else 1
            cells.append(("C", "%s/rc" % name, hit, "退出码 %d（期望 %d）" % (rc, want_rc)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # --- D 段：`--check` 的身份口径（刀28）
    d_cells, d_bad = run_identity_cases()
    cells.extend(d_cells)
    bad += d_bad

    # --- E 段：期望侧不再自证（刀29）
    e_cells, e_bad = run_expectation_cases()
    cells.extend(e_cells)
    bad += e_bad
    return cells, bad


def _first_diff(a, b):
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return "首处不同 @%d：新 %r / 旧 %r" % (i, a[i:i + 60], b[i:i + 60])
    return "长度不同：新 %d / 旧 %d" % (len(a), len(b))


def digest_of(cells):
    """一轮矩阵的指纹（格名 + 判定 + 说明全进摘要）——`--runs` 靠它比逐字节相同。"""
    blob = "\n".join("%s|%s|%s|%s" % (seg, name, ok, note) for seg, name, ok, note in cells)
    return hashlib.md5(blob.encode("utf-8")).hexdigest(), blob


# ============================================================ 强制负控

def _poison_cred(fx):
    fx["nodes"]["hub-a"]["creds"]["alpha.pass"] = "qq"
    return fx, "M3", "改一个口令"


def _poison_live(fx):
    fx["nodes"]["hub-a"]["connz.json"] = connz(["alpha", "beta"], extra=["ghost"])
    return fx, "M5", "加一个名册外活连接"


def _poison_perms(fx):
    users = [(u, p, None) for u, p, _perm in _roster(("alpha", "beta", "gamma"))]
    fx["nodes"]["hub-a"]["nats.conf"] = hub_conf("hub-a", H_A, users, perms=False)
    return fx, "M2", "删 permissions 块"


POISONS = (_poison_cred, _poison_live, _poison_perms)


def _poison_roster_drop(_fx):
    """**档案里删一个成员**（conf 一字未动）——「谁被悄悄从名册抹掉了」。

    `want_old_red=False` 是**如实写**，不是放宽：改前那一侧的期望成员集就是 conf 自己，
    档案怎么改它都看不见，全 0 是**必然**。这一格钉的正是本刀要闭的盲区
    —— 要求它「也报红」等于自欺。
    """
    return _clean(), ("alpha", "beta"), "M1", "conf_only_member", False, "档案里删一个成员"


def _poison_conf_join(_fx):
    """**conf 两中枢一起加一个人**（无凭据、无连接）——「谁手改把人塞进了两台中枢」。

    改前以**既有** kind `zombie_credential` 报红（并集名册里多一个没落地的人），
    改后以**新增** kind `conf_only_member` 报红（档案里根本没这个人）——
    同一件事，两侧从两个方向都判得出，故 `want_old_red=True`。
    """
    fx = _clean(("alpha", "beta", "gamma", "delta"))
    return (fx, ("alpha", "beta", "gamma"), "M1", "conf_only_member", True,
            "conf 两中枢一起加一个人")


#: 刀29 新增的两处负控：毒下在「**成员身份**」上，判的是「改前 ↔ 改后」这一对
#: （既有 3 处判的是「新侧 ↔ 旧侧第二实现」，两对都保留）。
ROSTER_POISONS = (_poison_roster_drop, _poison_conf_join)


def run_negative():
    """注毒五处，每处都必须报红；还原后必须转绿。返回 `(行列表, 失败数)`。

    前 3 处（刀25）判「新侧 ↔ 旧侧独立第二实现」两侧同红；
    后 2 处（刀29）判「改前快照 ↔ 改后正件」—— 其中「档案里删一个成员」改前**必然全 0**，
    那是本刀要闭的盲区本身，故该处的判据是「改后必红 **且** 改前如冻结期望」。
    """
    rows, bad = [], 0
    tmp = tempfile.mkdtemp(prefix="k25neg-")
    try:
        for i, poison in enumerate(POISONS):
            fx, key, what = poison(_clean())
            root = materialize(fx, os.path.join(tmp, "poison-%d" % i))
            new, old = new_report(root), old_report(root)
            red = new["counts"][key] > 0 and old["counts"][key] > 0
            bad += 0 if red else 1
            rows.append(("注毒 %s" % what, key, red,
                         "新 %d 条 / 旧 %d 条" % (new["counts"][key], old["counts"][key])))

        for i, poison in enumerate(ROSTER_POISONS):
            fx, names, key, want_kind, want_old_red, what = poison(None)
            root = materialize(fx, os.path.join(tmp, "roster-poison-%d" % i),
                               roster_rows=_rows_for(fx, names))
            art = NEW.collect_fixtures(root)
            before = OLDK29.analyze(art, gen_nats_conf.perm_list)
            after = NEW.analyze(art, gen_nats_conf.perm_list)
            after_red = after["counts"][key] > 0 and want_kind in _kind_set(after)
            before_red = any(before["counts"][k] for k in NEW.CHECK_KEYS)
            ok = after_red and (before_red == want_old_red)
            bad += 0 if ok else 1
            rows.append(("注毒 %s" % what, key, ok,
                         "改后 %s %d 条（%s）／改前 %s（冻结期望 %s）"
                         % (want_kind, after["counts"][key], "红" if after_red else "**没红**",
                            "红：" + "、".join(sorted(_kind_set(before))) if before_red else "全 0（盲区）",
                            "红" if want_old_red else "全 0")))

        root = materialize(_clean(), os.path.join(tmp, "restored"))
        new, old = new_report(root), old_report(root)
        green = new["ok"] and old["ok"]
        bad += 0 if green else 1
        rows.append(("还原（无毒）", "all", green,
                     "新 ok=%s / 旧 ok=%s" % (new["ok"], old["ok"])))

        root = materialize(_clean(), os.path.join(tmp, "restored-roster"),
                           roster_rows=_rows_for(_clean(), ("alpha", "beta", "gamma")))
        art = NEW.collect_fixtures(root)
        before = OLDK29.analyze(art, gen_nats_conf.perm_list)
        after = NEW.analyze(art, gen_nats_conf.perm_list)
        green = before["ok"] and after["ok"]
        bad += 0 if green else 1
        rows.append(("还原（档案 ≡ conf）", "all", green,
                     "改前 ok=%s / 改后 ok=%s" % (before["ok"], after["ok"])))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return rows, bad


# ============================================================ 主流程

def main(argv=None):
    ap = argparse.ArgumentParser(description="刀25 · 成员入网只读对账 · 等价/冻结矩阵")
    ap.add_argument("--runs", type=int, default=1, help="连跑 N 轮，自证确定性（逐字节相同）")
    ap.add_argument("--negative", action="store_true", help="强制负控：注毒五处必须报红")
    ap.add_argument("--emit", default="", help="把 10 例冻结夹具落盘到该目录")
    ap.add_argument("--quiet", action="store_true", help="只打不一致的格与汇总")
    args = ap.parse_args(argv)

    if args.emit:
        return emit_all(args.emit)

    if args.negative:
        rows, bad = run_negative()
        print("=" * 78)
        print("刀25 · 强制负控（装置本身有没有判别力）")
        print("=" * 78)
        for what, key, ok, note in rows:
            print("  %s %-16s %-4s %s" % ("✓" if ok else "✗", what, key, note))
        print("\n负控判定：%s" % ("通过 —— 注毒全红、还原转绿"
                                  if not bad else "**失败 —— 有一处该红没红**"))
        return 0 if not bad else 1

    digests, cells, bad = [], None, 0
    for _ in range(max(1, args.runs)):
        cells, bad = run_matrix()
        digests.append(digest_of(cells)[0])

    print("=" * 78)
    print("刀25 起 · 刀28 扩 D 段 · 刀29 扩 E 段 · 成员入网只读对账 · 等价/冻结矩阵（%d 格 × %d 轮）"
          % (len(cells), max(1, args.runs)))
    print("新侧 = tools/nats_membership_audit.py（正则 + 括号配平；模板 import L1 perm_list）")
    print("旧侧 = _old/membership_audit_old_k25.py（逐行状态机 + 顶层逗号切词；模板自拼）")
    print("D 段 = 身份 vs 观测量：新侧 vs _old/membership_audit_old_k28.py（刀28 前逐字节快照）")
    print("E 段 = 期望侧不再自证：新侧 vs _old/membership_audit_old_k29.py（刀29 前逐字节快照）")
    print("=" * 78)
    seg = None
    for segment, name, ok, note in cells:
        if args.quiet and ok:
            continue
        if segment != seg:
            print("\n[%s 段]" % segment)
            seg = segment
        print("  %s %-24s %s" % ("✓" if ok else "✗", name, note))

    determinism = len(set(digests)) == 1
    print("\n" + "-" * 78)
    print("合计 %d 格，不一致 %d 处" % (len(cells), bad))
    print("%d 轮矩阵指纹：%s" % (len(digests), "、".join(digests)))
    if not bad:
        print("EQUIV_SENTINEL_OK 格数=%d" % len(cells))
    if determinism:
        print("EQUIV_DETERMINISM_OK 轮数=%d md5=%s" % (len(digests), digests[0]))
    else:
        print("**确定性失败** —— 多轮指纹不一致")
    return 0 if (not bad and determinism) else 1


if __name__ == "__main__":
    sys.exit(main())
