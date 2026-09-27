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
    合计 106 格

用法
----
    python tools/equivalence/membership_audit_equiv.py                # 跑一遍矩阵
    python tools/equivalence/membership_audit_equiv.py --runs 2       # 连跑两轮，自证确定性
    python tools/equivalence/membership_audit_equiv.py --negative     # 强制负控：注毒三处必须报红
    python tools/equivalence/membership_audit_equiv.py --emit <DIR>   # 把冻结夹具落盘

退出码：0 = 全部一致 / 1 = 有不一致（或负控没能判红）。
装置源码内**不含**任何真实宿主地址、任何真实口令：夹具主机名一律 `*.example.invalid`，
夹具口令一律 2 字符合成串（`p1` / `p2` …）。
"""
from __future__ import annotations

import argparse
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


def materialize(fx, root):
    """把一例夹具落到 `root/`。目录结构见模块文档串与刀25 报告。"""
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


def run_negative():
    """注毒三处，每处**两侧都必须报红**；还原后必须转绿。返回 `(行列表, 失败数)`。"""
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
        root = materialize(_clean(), os.path.join(tmp, "restored"))
        new, old = new_report(root), old_report(root)
        green = new["ok"] and old["ok"]
        bad += 0 if green else 1
        rows.append(("还原（无毒）", "all", green,
                     "新 ok=%s / 旧 ok=%s" % (new["ok"], old["ok"])))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return rows, bad


# ============================================================ 主流程

def main(argv=None):
    ap = argparse.ArgumentParser(description="刀25 · 成员入网只读对账 · 等价/冻结矩阵")
    ap.add_argument("--runs", type=int, default=1, help="连跑 N 轮，自证确定性（逐字节相同）")
    ap.add_argument("--negative", action="store_true", help="强制负控：注毒三处必须报红")
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
    print("刀25 · 成员入网只读对账 · 等价/冻结矩阵（%d 格 × %d 轮）"
          % (len(cells), max(1, args.runs)))
    print("新侧 = tools/nats_membership_audit.py（正则 + 括号配平；模板 import L1 perm_list）")
    print("旧侧 = _old/membership_audit_old_k25.py（逐行状态机 + 顶层逗号切词；模板自拼）")
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
