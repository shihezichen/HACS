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
          （规范 JSON **与冻结期望的「同 / 不同」相符** 1 + 六项条数命中冻结期望 6 + CLI 退出码 1）
    D 段  `--check` 的**身份口径**（刀28 加；新侧 vs 刀28 前快照）      6 格
          （观测量漂移必须判一致 · 改前必须判漂移 · 裁决消失/新增必须判漂移 ·
            非观测字段漂移必须判漂移 · 两条报告的判据身份必须逐字节相等）
    E 段  **期望侧不再自证**（刀29 加；新侧 vs 刀29 前快照）            20 格
          （5 场景 × 4 格：改前六项条数 · 改后六项条数 · 改后新增的 kind 集合 ·
            两侧规范 JSON 逐字节比较的结果）
    F 段  **口令真值换源到成员本机凭据**（刀31 加；新侧 vs 刀31 前快照）  22 格
          （5 场景 × 4 格，同 E 段口径 + F4 另加 2 格 rc 判据：
            「改后 collect_errors 非空且改前为空」与「新正件 CLI 退出码 == 2」）
    合计 154 格

E 段在证明什么（这是刀29 的价值本身）
------------------------------------
刀29 之前，对账器的**期望成员集 = 被检对象自己**（两台中枢 conf 的用户并集）——
于是「**两台中枢一起被手改**」（加人 / 删人）它**彻底无感**。刀29 把期望侧切到
L0 档案 `agent-team/member_roster.py`。E 段把**同一份 artifacts**（同一夹具 + 同一份
`roster.json`）同时喂给「改前」`_old/membership_audit_old_k29.py`（正件在刀29 前的逐字节快照，
**函数级直调** `analyze()` —— 它的 CLI 不吃档案）与「改后」正件，钉住两件事：
① 现网态（档案 ≡ conf）两侧输出**逐字节相同** —— 切换是**已知等价的替换**；
② 手改入网 / 手改退网，改前全 0、改后必红。

F 段在证明什么（这是刀31 的价值本身）
------------------------------------
刀29 只切了**成员身份**那一半：M3 的期望口令仍取「基准中枢 conf 的 `user → password`」——
口令这一半的期望侧**还是被检对象自己**。刀31 把真值换成**成员本机凭据**
（名册的 `cred_node` / `cred_kind` 指到的那个文件），中枢 conf 与刀30 落的中枢凭据镜像
一起降级为**被核对的产物**。F 段把同一份 `artifacts` 喂给「改前」
`_old/nats_membership_audit_old_k31.py`（逐字节快照，**函数级直调** `analyze()`）与「改后」正件。

**F2 / F5 的「改前」不是全 0 —— 这是如实记，不是没做到**：改前那一侧的 M3 本来就是
「基准中枢 conf 口令 ↔ 各机 `*.pass`」**互比**，所以 conf 被改动时它也会红 ——
只是**判反了方向**：它咬住「文件与 conf 不符」（`cred_mismatch`，等于说文件错了），
改后咬住「conf 与成员本机真值不符」（`conf_password_differs_from_owner`，即 conf 错了）。
真正「改前彻底无感」的是 F3（镜像改前根本不在观测面里）与 F4（声明的凭据文件不存在时，
改前**静默回落 conf 口令**、六项全 0 且 `collect_errors` 为空）。
每个场景的「改后新增的 kind 集合」那一格钉的就是这个方向差。

夹具里的 `roster.json`
----------------------
`materialize()` 每例都落一份 `roster.json`（4 元组数组）。**既有 10 例**的行取自该例
**hub-a 的 conf 用户**（hub-a 解析不出用户时回落 hub-b —— 与正件 `ref_hub` 同口径，
`malformed` 例就是这种形态）。`cred_node` / `cred_kind` 取「该例中拥有该成员凭据文件的那个
节点与该文件的形态」（`nats-` 前缀 ⇒ `runtime`，否则 ⇒ `conf`）—— 刀31 起正件**要消费这两列**，
推错就会去找一个不存在的文件、平白记采集失败（刀31 前这两列是摆设，随便填都不影响）。

用法
----
    python tools/equivalence/membership_audit_equiv.py                # 跑一遍矩阵
    python tools/equivalence/membership_audit_equiv.py --runs 2       # 连跑两轮，自证确定性
    python tools/equivalence/membership_audit_equiv.py --negative     # 强制负控：注毒九处必须报红
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
import nats_membership_audit_old_k31 as OLDK31            # noqa: E402 —— 刀31 前的正件（F 段「改前」锚）
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


#: 冻结期望：`(名字, 夹具, 六项条数, CLI 退出码, **新旧两侧规范 JSON 是否逐字节相同**)`。
#: **这是契约，不是「跑出来是多少就记多少」** —— 改了判定口径就会在这里立刻撞红。
#:
#: 第 5 列是刀31 加的。旧侧（`_old/membership_audit_old_k25.py`）是**刀25 口径**的独立第二实现，
#: 它的口令真值仍是「基准中枢 conf」；刀31 把新侧的真值换成了「成员本机凭据」，于是三例
#: 必然分道扬镳（过账见刀31 报告与契约文档）——把「相同」写死成契约、让它们撞红是不诚实的，
#: 逐例写明「同 / 不同」并各自说清**为什么**，才是如实记账。其余 7 例仍必须逐字节相同。
CASES = (
    ("clean",           _case_clean,           {"M1": 0, "M2": 0, "M3": 0, "M4": 0, "M5": 0, "M6": 0}, 0, True),
    # zombie：delta 在册却全网无凭据 ⇒ 刀31 记 collect_error（rc 0→2；M4 仍 1 条不变）
    ("zombie",          _case_zombie,          {"M1": 0, "M2": 0, "M3": 0, "M4": 1, "M5": 0, "M6": 0}, 2, False),
    # cred_mismatch：真值换成 owner 文件（= 被改过的 "zz"）后，hub-a 那份文件不再「与真值不符」；
    #                改判两台中枢的 conf 口令都与真值不同 —— **换向更严**，1 条 → 2 条
    ("cred_mismatch",   _case_cred_mismatch,   {"M1": 0, "M2": 0, "M3": 2, "M4": 0, "M5": 0, "M6": 0}, 0, False),
    ("cred_orphan",     _case_cred_orphan,     {"M1": 0, "M2": 0, "M3": 1, "M4": 0, "M5": 0, "M6": 0}, 0, True),
    ("perm_drift",      _case_perm_drift,      {"M1": 0, "M2": 1, "M3": 0, "M4": 0, "M5": 0, "M6": 0}, 0, True),
    # hub_divergence：hub-b 的 gamma 口令（"p9"）与真值（p3）不同 ⇒ 新增 kind 命中（**收紧**）
    ("hub_divergence",  _case_hub_divergence,  {"M1": 2, "M2": 0, "M3": 1, "M4": 0, "M5": 0, "M6": 0}, 0, False),
    ("outside_roster",  _case_outside_roster,  {"M1": 0, "M2": 0, "M3": 0, "M4": 0, "M5": 1, "M6": 0}, 0, True),
    ("legacy_hub",      _case_legacy_hub,      {"M1": 0, "M2": 0, "M3": 0, "M4": 0, "M5": 0, "M6": 1}, 0, True),
    ("dup_route",       _case_dup_route,       {"M1": 0, "M2": 0, "M3": 0, "M4": 0, "M5": 0, "M6": 1}, 0, True),
    ("malformed",       _case_malformed,       {"M1": 0, "M2": 0, "M3": 0, "M4": 0, "M5": 0, "M6": 0}, 2, True),
)


# ============================================================ 夹具落盘

def _write(path, text):
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def _cred_owner(fx):
    """成员 → 该例中持有其凭据文件的 `(节点, 文件名)`（先到先得；没有则查不到）。

    **只看 `creds`，不看 `mirror`** —— 镜像是中枢手里的副本，不是该成员的本机凭据。
    """
    owner = {}
    for node, files in sorted(fx["nodes"].items()):
        for filename in sorted((files.get("creds") or {})):
            member = NEW._member_of(filename)
            if member is not None and member not in owner:
                owner[member] = (node, filename)
    return owner


def _cred_spec(owner, member):
    """名册行的 `(cred_node, cred_kind)` —— **kind 必须按实际文件名推**（刀31）。

    `nats-<m>.pass` ⇒ `runtime`；`<m>.pass` ⇒ `conf`。不这么推的话，`edge-n` 上那份
    `nats-gamma.pass` 会被写成 `("edge-n", "conf")`，刀31 的正件就去找 `edge-n/gamma.pass`
    —— 找不到 ⇒ 采集失败 rc=2，装置自己把自己搞红。

    该例里**没有**凭据文件的成员落 `("hub-a", "conf")`：于是「在册却全网无凭据」（`zombie` 例、
    E2、E5）在刀31 下**必然**多一条 `collect_error`。那是口径**收紧**的直接后果，不是 bug ——
    改前会静默拿 conf 顶上，改后拒绝。
    """
    got = owner.get(member)
    if got is None:
        return "hub-a", "conf"
    node, filename = got
    return node, ("runtime" if filename.startswith("nats-") else "conf")


def _mirror_all(names=("alpha", "beta", "gamma")):
    """一份**内容正确**的中枢凭据镜像（刀30 形态：`<conf_dir>/creds/<m>.pass`）。"""
    return {"%s.pass" % n: PW[n] for n in names}


def roster_rows_of(fx):
    """既有 10 例的 `roster.json` 行 —— 取自该例 **hub-a 的 conf 用户**（顺序照 conf）。

    hub-a 解析不出用户时**回落 hub-b**（与正件 `ref_hub = 第一台有 conf 的中枢` 同口径）：
    `malformed` 例的 hub-a conf 是空文件，不回落的话名册会是空的、M5 会平白多两条，
    10 例的冻结条数期望就守不住了。`cred_node` 取「该例中拥有该成员凭据文件的那个节点」，
    没有则 `hub-a`；`cred_kind` **按实际文件名推**（刀31 起正件要消费这一列，见 `_cred_spec`）。

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
    return [[n, "member"] + list(_cred_spec(owner, n)) for n in names]


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
            if name in ("creds", "mirror"):             # `mirror/` 是刀31 新增的夹具形态
                for filename, content in sorted(body.items()):
                    _write(os.path.join(base, name, filename), content)
            elif isinstance(body, str):
                _write(os.path.join(base, name), body)
            else:
                _write(os.path.join(base, name),
                       json.dumps(body, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    return root


def emit_all(dest):
    """`--emit`：把 10 例夹具原样落盘，供独立重跑与人眼核对。"""
    for name, build, _want, _rc, _same in CASES:
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
    """按给定成员名**顺序**造一份 `roster.json` 行（`cred_node` / `cred_kind` 见 `_cred_spec`）。"""
    owner = _cred_owner(fx)
    return [[n, "member"] + list(_cred_spec(owner, n)) for n in names]


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
    #: E3 / E4 的 `M3` 与 kind 集合在**刀31 改了一格**（过账见报告）：`cred_orphan` 的判据从
    #: 「不在**基准中枢 conf** 的用户里」换成「不在**名册档案**里」。这两例的档案都比 conf 少人
    #: （E3 少 gamma、E4 少 delta），而那个人**有凭据文件** —— 按档案口径它就是孤儿凭据。
    #: 改前拿 conf 当名册，自然看不见；**收紧**，不是放宽。
    ("E3_roster_short", _e_fx_clean, ("alpha", "beta"),
     _ZERO, {"M1": 2, "M2": 0, "M3": 1, "M4": 0, "M5": 1, "M6": 0},
     frozenset(("conf_only_member", "live_outside_roster", "cred_orphan")), False),
    ("E4_manual_join", _e_fx_manual_join, ("alpha", "beta", "gamma"),
     _ZERO, {"M1": 2, "M2": 0, "M3": 1, "M4": 0, "M5": 1, "M6": 0},
     frozenset(("conf_only_member", "live_outside_roster", "cred_orphan")), False),
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


# ============================================================ F 段：口令真值换源（刀31）

def _f_fx_equal():
    """F1 现网态：conf 口令 == owner 真值、**无镜像**。"""
    return _clean()


def _f_fx_conf_differs():
    """F2 **某一台**中枢 conf 里的口令被手改（owner 文件与镜像都对）。"""
    fx = _clean()
    users = [(u, ("zz" if u == "alpha" else p), perm)
             for u, p, perm in _roster(("alpha", "beta", "gamma"))]
    fx["nodes"]["hub-b"]["nats.conf"] = hub_conf("hub-b", H_B, users, routes_to=H_A)
    fx["nodes"]["hub-a"]["mirror"] = _mirror_all()
    return fx


def _f_fx_mirror_differs():
    """F3 中枢凭据镜像被改（conf 与 owner 文件都对）—— 改前**根本不看镜像**。"""
    fx = _clean()
    m = _mirror_all()
    m["alpha.pass"] = "zz"
    fx["nodes"]["hub-a"]["mirror"] = m
    return fx


def _f_fx_missing_owner():
    """F4 名册声称 alpha 的真值在 `hub-a/alpha.pass` —— **那个文件不存在**（其它都在）。"""
    fx = _clean()
    del fx["nodes"]["hub-a"]["creds"]["alpha.pass"]
    fx["nodes"]["hub-a"]["mirror"] = _mirror_all()
    return fx


def _f_fx_both_conf():
    """F5 **两台中枢一起**手改同一个成员的 conf 口令（E 段「一起改」同款）。"""
    users = [(u, ("zz" if u == "alpha" else p), perm)
             for u, p, perm in _roster(("alpha", "beta", "gamma"))]
    fx = _clean()
    fx["nodes"]["hub-a"]["nats.conf"] = hub_conf("hub-a", H_A, users)
    fx["nodes"]["hub-b"]["nats.conf"] = hub_conf("hub-b", H_B, users, routes_to=H_A)
    return fx


_ABC = ("alpha", "beta", "gamma")

#: F 段 5 场景（**冻结期望**）：
#: `(名字, 夹具, 档案成员序, 改前六项条数, 改后六项条数, 改后多出的 kind 集合, 两侧 JSON 是否相同)`
#:
#: 「改前」= `_old/nats_membership_audit_old_k31.py`（刀31 前正件的逐字节快照，**函数级直调**
#: `analyze()`）。**两侧喂同一份 `artifacts`**（同一夹具目录 + 同一份 `roster.json`），
#: 差异只在「口令真值从哪来」：改前取基准中枢 conf 的 `user → password`，改后取成员本机凭据。
#:
#: 关于「改前无感」：改前**真正看不见**的只有 F3（它根本不采镜像）与 F4（声明文件缺失时
#: 静默回落 conf）。F2 / F5 改前**看得见但判错方向** —— 它拿 conf 当真值，于是把
#: 「中枢 conf 被手改」说成「成员机上的凭据文件不对」（F5：1 条 `cred_mismatch`），
#: 或者只说得出「两台中枢不一致」而指不出谁对（F2：M1 2 条）。换源换的正是这个方向：
#: 谁是真值、谁是产物。**（这一栏与本刀 spec 表里「F2/F5 改前六项全 0」不符 —— 那是主控
#: 推演有误：只要该成员有 owner 文件，改前的 M3 就会拿它去比 conf；而若没有 owner 文件，
#: 改后又因缺件而跳过、同样不红。两者互斥，「改前全 0 且改后 conf 判据必红」不可能成立。
#: 逐条实测过账写在刀31 报告里。）**
F_CASES = (
    ("F1_equal", _f_fx_equal, _ABC,
     _ZERO, _ZERO, frozenset(), True),
    ("F2_conf_differs", _f_fx_conf_differs, _ABC,
     {"M1": 2, "M2": 0, "M3": 0, "M4": 0, "M5": 0, "M6": 0},
     {"M1": 2, "M2": 0, "M3": 1, "M4": 0, "M5": 0, "M6": 0},
     frozenset(("conf_password_differs_from_owner",)), False),
    ("F3_mirror_differs", _f_fx_mirror_differs, _ABC,
     _ZERO, {"M1": 0, "M2": 0, "M3": 1, "M4": 0, "M5": 0, "M6": 0},
     frozenset(("mirror_password_differs_from_owner",)), False),
    ("F4_missing_owner", _f_fx_missing_owner, _ABC,
     _ZERO, _ZERO, frozenset(), False),
    ("F5_both_conf", _f_fx_both_conf, _ABC,
     {"M1": 0, "M2": 0, "M3": 1, "M4": 0, "M5": 0, "M6": 0},
     {"M1": 0, "M2": 0, "M3": 2, "M4": 0, "M5": 0, "M6": 0},
     frozenset(("conf_password_differs_from_owner",)), False),
)


def run_truth_source_cases():
    """F 段：**口令真值换源到成员本机凭据**（刀31）。22 格。

    5 场景 × 4 格（改前条数 / 改后条数 / 改后多出的 kind / 两侧 JSON 逐字节比较），
    外加 F4 的两格退出码判据：① 改后 `collect_errors` 非空**且**改前为空（= 改前静默回落
    conf、改后拒绝）② 新正件的 CLI 在该夹具上 `rc == 2`。
    """
    cells, bad = [], 0
    tmp = tempfile.mkdtemp(prefix="k31truth-")

    def cell(name, ok, note):
        nonlocal bad
        bad += 0 if ok else 1
        cells.append(("F", name, ok, note))

    try:
        for label, build, names, want_old, want_new, want_kinds, want_same in F_CASES:
            fx = build()
            root = materialize(fx, os.path.join(tmp, label),
                               roster_rows=_rows_for(fx, names))
            art = NEW.collect_fixtures(root)          # **同一份 artifacts** 喂两侧
            old = OLDK31.analyze(art, gen_nats_conf.perm_list)
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

            if label != "F4_missing_owner":
                continue
            refused = bool(new["collect_errors"]) and not old["collect_errors"]
            cell("%s/refuse" % label, refused,
                 "改后 collect_errors %d 条 / 改前 %d 条（要的是「改后非空、改前空」）"
                 % (len(new["collect_errors"]), len(old["collect_errors"])))
            rc = cli_rc(root)
            cell("%s/rc" % label, rc == 2, "新正件 CLI 退出码 %d（期望 2）" % rc)
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
        for name, build, want, want_rc, want_same in CASES:
            root = materialize(build(), os.path.join(tmp, name))
            new, old = new_report(root), old_report(root)
            n_text, o_text = NEW.canonical(new), NEW.canonical(old)
            same = (n_text == o_text)
            hit = (same == want_same)
            bad += 0 if hit else 1
            if want_same:
                note = "规范 JSON 逐字节一致" if same else _first_diff(n_text, o_text)
            else:
                note = ("与旧侧（刀25 口径）不同 —— 冻结期望：不同（刀31 过账）" if not same
                        else "**竟然逐字节相同** —— 冻结期望是不同")
            cells.append(("C", "%s/json" % name, hit, note))
            for key in NEW.CHECK_KEYS:
                got = new["counts"][key]
                #: 两侧口径已分家的三例（`want_same=False`），旧侧条数**不再是契约** ——
                #: 它停在刀25 的「conf 即真值」，如实列出但不参与判定。
                hit = (got == want[key]) and (not want_same or old["counts"][key] == want[key])
                bad += 0 if hit else 1
                cells.append(("C", "%s/%s" % (name, key), hit,
                              "%d 条（冻结期望 %d）%s"
                              % (got, want[key],
                                 "" if want_same else "｜旧侧 %d 条（刀25 口径，不判）"
                                 % old["counts"][key])))
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

    # --- F 段：口令真值换源到成员本机凭据（刀31）
    f_cells, f_bad = run_truth_source_cases()
    cells.extend(f_cells)
    bad += f_bad
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


def _neg_owner_changed():
    """N1 **owner 文件被改**：conf 与镜像都还停在**旧**真值 —— 三个判据都该指向它们。"""
    bad = _clean()
    bad["nodes"]["hub-a"]["mirror"] = _mirror_all()
    bad["nodes"]["hub-a"]["creds"]["alpha.pass"] = "qq"
    good = _clean()
    good["nodes"]["hub-a"]["mirror"] = _mirror_all()
    return bad, good, _ABC


def _neg_conf_changed():
    """N2 **中枢 conf 口令改掉**（两台一起，把 M1 让开，红点纯落在 M3）。"""
    return _f_fx_both_conf(), _clean(), _ABC


def _neg_mirror_changed():
    """N3 **中枢镜像改掉**（conf 与 owner 文件都对）。"""
    good = _clean()
    good["nodes"]["hub-a"]["mirror"] = _mirror_all()
    return _f_fx_mirror_differs(), good, _ABC


def _neg_owner_missing():
    """N4 **owner 文件删掉**，同时两台中枢 conf 的口令都被改 —— 必须 rc=2 拒绝，**不许回落**。

    这一处专钉「缺件回落」：改前会拿 conf 当真值（于是什么都判不出来），改后必须
    ① 记 `collect_error` ② **不**对该成员产出任何 conf / 镜像差异判据（不猜、不顶替）。
    """
    bad = _f_fx_both_conf()
    del bad["nodes"]["hub-a"]["creds"]["alpha.pass"]
    return bad, _clean(), _ABC


#: 刀31 新增的 4 处负控：毒下在「**口令真值**」上，判的是**改后正件**这一侧
#: （既有 8 行一格未动）。每处两判：**注毒必红 + 还原转绿**。
#: `mode="red"`  → M3 条数 > 0 且命中 `kinds` 里至少一个 kind
#: `mode="refuse"` → `collect_errors` 非空，且 `kinds` 里那些「以 conf 当真值才会有」的判据一条都没有
CRED_POISONS = (
    ("owner 文件被改", _neg_owner_changed, "red",
     frozenset(("cred_mismatch", "conf_password_differs_from_owner",
                "mirror_password_differs_from_owner"))),
    ("中枢 conf 口令改掉", _neg_conf_changed, "red",
     frozenset(("conf_password_differs_from_owner",))),
    ("中枢镜像改掉", _neg_mirror_changed, "red",
     frozenset(("mirror_password_differs_from_owner",))),
    ("owner 文件删掉", _neg_owner_missing, "refuse",
     frozenset(("conf_password_differs_from_owner", "cred_mismatch"))),
)


def run_negative():
    """注毒九处，每处都必须报红；还原后必须转绿。返回 `(行列表, 失败数)`。

    前 3 处（刀25）判「新侧 ↔ 旧侧独立第二实现」两侧同红；
    中 2 处（刀29）判「改前快照 ↔ 改后正件」—— 其中「档案里删一个成员」改前**必然全 0**，
    那是刀29 要闭的盲区本身，故该处的判据是「改后必红 **且** 改前如冻结期望」；
    后 4 处（刀31）毒下在**口令真值**上，判**改后正件**这一侧，每处自带「还原转绿」一行 ——
    其中「owner 文件删掉」判的不是红，是**拒绝**：记 `collect_error`（rc=2 面）且**不回落 conf**。
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

        # --- 刀31 新增 4 处：毒下在**口令真值**上，每处「注毒必红 + 还原转绿」两判
        for i, (what, build, mode, kinds) in enumerate(CRED_POISONS):
            bad_fx, good_fx, names = build()

            root = materialize(bad_fx, os.path.join(tmp, "cred-poison-%d" % i),
                               roster_rows=_rows_for(bad_fx, names))
            after = NEW.analyze(NEW.collect_fixtures(root), gen_nats_conf.perm_list)
            got = _kind_set(after) & kinds
            if mode == "red":
                ok = after["counts"]["M3"] > 0 and bool(got)
                note = "改后 M3 %d 条，命中 %s" % (after["counts"]["M3"],
                                                  "、".join(sorted(got)) or "**一个都没命中**")
            else:
                ok = bool(after["collect_errors"]) and not got
                note = ("改后 collect_errors %d 条（rc=2 面）／%s"
                        % (len(after["collect_errors"]),
                           "该成员未产生任何 conf/凭据差异判据 = 没回落"
                           if not got else "**回落了**：" + "、".join(sorted(got))))
            bad += 0 if ok else 1
            rows.append(("注毒 %s" % what, "M3", ok, note))

            root = materialize(good_fx, os.path.join(tmp, "cred-restored-%d" % i),
                               roster_rows=_rows_for(good_fx, names))
            rep = NEW.analyze(NEW.collect_fixtures(root), gen_nats_conf.perm_list)
            green = rep["ok"]
            bad += 0 if green else 1
            rows.append(("还原 %s" % what, "M3", green,
                         "ok=%s（六项 %s，采集失败 %d 条）"
                         % (rep["ok"], _counts_text(_counts(rep)), len(rep["collect_errors"]))))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return rows, bad


# ============================================================ 主流程

def main(argv=None):
    ap = argparse.ArgumentParser(description="刀25 · 成员入网只读对账 · 等价/冻结矩阵")
    ap.add_argument("--runs", type=int, default=1, help="连跑 N 轮，自证确定性（逐字节相同）")
    ap.add_argument("--negative", action="store_true", help="强制负控：注毒九处必须报红/拒绝")
    ap.add_argument("--emit", default="", help="把 10 例冻结夹具落盘到该目录")
    ap.add_argument("--quiet", action="store_true", help="只打不一致的格与汇总")
    args = ap.parse_args(argv)

    if args.emit:
        return emit_all(args.emit)

    if args.negative:
        rows, bad = run_negative()
        print("=" * 78)
        print("刀25 起 · 刀29 / 刀31 各扩 · 强制负控（装置本身有没有判别力）")
        print("=" * 78)
        for what, key, ok, note in rows:
            print("  %s %-24s %-4s %s" % ("✓" if ok else "✗", what, key, note))
        print("\n负控判定：%s" % ("通过 —— 注毒全红、还原转绿"
                                  if not bad else "**失败 —— 有一处该红没红**"))
        return 0 if not bad else 1

    digests, cells, bad = [], None, 0
    for _ in range(max(1, args.runs)):
        cells, bad = run_matrix()
        digests.append(digest_of(cells)[0])

    print("=" * 78)
    print("刀25 起 · 刀28 扩 D 段 · 刀29 扩 E 段 · 刀31 扩 F 段 · 成员入网只读对账 · "
          "等价/冻结矩阵（%d 格 × %d 轮）" % (len(cells), max(1, args.runs)))
    print("新侧 = tools/nats_membership_audit.py（正则 + 括号配平；模板 import L1 perm_list）")
    print("旧侧 = _old/membership_audit_old_k25.py（逐行状态机 + 顶层逗号切词；模板自拼）")
    print("D 段 = 身份 vs 观测量：新侧 vs _old/membership_audit_old_k28.py（刀28 前逐字节快照）")
    print("E 段 = 期望侧不再自证：新侧 vs _old/membership_audit_old_k29.py（刀29 前逐字节快照）")
    print("F 段 = 口令真值换源：新侧 vs _old/nats_membership_audit_old_k31.py（刀31 前逐字节快照）")
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
