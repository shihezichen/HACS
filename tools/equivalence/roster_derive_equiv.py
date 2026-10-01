# -*- coding: utf-8 -*-
"""刀27 等价 / 冻结矩阵装置 —— 「档案 + 凭据 ⇒ 用户区」这把尺子**自己准不准**的证据。

它证明什么（以及为什么不是自证）
--------------------------------
正件 `tools/roster_derive_conf.py` 与旧侧 `_old/roster_derive_old_k27.py` 是
**两条互不相识的算法路径**：

    抽用户区   正件＝正则定位 `users = [` 行后**向上回溯** + 逐字符配平
               旧侧＝先把 conf 切成**顶层区块**，再挑「头是 authorization 且体内有 users」的那个
    归一化     正件＝两条正则；旧侧＝**零正则**，手写扫描器逐字符找 `password:` 后那对引号
    渲染       正件＝按索引判「是不是最后一条」加逗号；旧侧＝先全渲染成无尾，最后统一补前 n-1 条
    角色模板   正件＝import L1 `gen_nats_conf.perm_list`；旧侧＝按契约文档口径**自己拼**

本装置把同一批**冻结夹具**同时喂给两路，逐格比**规范 JSON**（键名排序、缩进固定）。
两边一致 → 这把尺子的刻度可信；任何一边写歪 → 立刻不一致。

夹具从哪来
----------
**内嵌在本件里**（常量 + 构造函数），跑的时候现落到临时目录，跑完即删 ——
装置**完全离线**、不碰任何真机、仓里也不多出一堆数据文件。
`--emit <DIR>` 可以把这批夹具原样落盘，供主控独立重跑与人眼核对。

覆盖面
------
    A 段  角色模板交叉核对（L1 `perm_list` vs 旧侧自拼模板）           12 格
    B 段  用户区抽块交叉核对（回溯法 vs 顶层切块法，含 4 例畸形）       7 格
          归一化交叉核对（正则 vs 零正则手写扫描器）                   7 格
    C 段  6 例冻结夹具 × 4 格 = 24 格
          （规范 JSON 逐字节一致 1 + 每 hub 差异行数命中冻结期望 2 + CLI 退出码 1）
    合计 50 格

用法
----
    python tools/equivalence/roster_derive_equiv.py               # 跑一遍矩阵
    python tools/equivalence/roster_derive_equiv.py --runs 2      # 连跑两轮，自证确定性
    python tools/equivalence/roster_derive_equiv.py --negative    # 强制负控：注毒三处必须报红
    python tools/equivalence/roster_derive_equiv.py --emit <DIR>  # 把冻结夹具落盘

退出码：0 = 全部一致且确定性成立 / 1 = 有不一致（或负控没能判红）。
装置源码内**不含**任何真实宿主地址、任何真实口令：夹具主机名一律 `*.example.invalid`
（RFC2606 保留后缀，永远解析不到真机），夹具口令一律 2 字符合成串（`p1` / `p2` …）。
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
#: 保留注入：OLD_DIR / TOOLS 不是包目录 —— 冻结快照 `roster_derive_old_k27` 与正件
#: `roster_derive_conf` 只能按裸名装入；快照内部还是裸名 `import member_roster`，
#: 故把该件今天所在的 L0 目录一并注入**只供旧侧用**。REPO 供绝对包名解析。
L0_HOST = os.path.join(REPO, "node", "l0_host")
DERIVE_PY = os.path.join(TOOLS, "roster_derive_conf.py")

for p in (REPO, TOOLS, OLD_DIR, L0_HOST):
    if p not in sys.path:
        sys.path.insert(0, p)

import roster_derive_conf as NEW                        # noqa: E402 —— 新侧（正件）
import roster_derive_old_k27 as OLD                     # noqa: E402 —— 旧侧（独立第二实现）
import hub.l1_relay.gen_nats_conf as gen_nats_conf                                    # noqa: E402 —— L1 角色模板唯一出处

#: 夹具里的合成主机名（`.invalid` 是 RFC2606 保留后缀，永远不可能解析到真机）
H_A, H_B = "hub-a.example.invalid", "hub-b.example.invalid"
#: 夹具口令：一律 2 字符合成串（够短，既撞不上扫密门禁，也一眼看得出是假的）
PW = {"alpha": "p1", "beta": "p2", "gamma": "p3", "delta": "p4"}

#: 合成名册（`(member, role, cred_node, cred_kind)`，**顺序即规范序**）
BASE_ROSTER = (
    ("alpha", "member", "edge-n", "runtime"),
    ("beta",  "member", "hub-a",  "conf"),
    ("gamma", "member", "hub-b",  "conf"),
)
HUBS = ("hub-a", "hub-b")


# ============================================================ 夹具构件

def hub_conf(node, host, users, stamp="2026-01-01 00:00:00", routes_to=None):
    """合成一份 v2 中枢 `nats.conf`（形状照着 `gen_nats_conf.py` 的产物来）。

    `users` = `[(成员, 口令, permissions 串)]`。permissions 串由调用方给 ——
    默认是 `gen_nats_conf.perm_list(成员)`，即「现网观测到的规范形」，**不是判据**；
    判据那一侧是两份互不相识的模板各算各的（A 段已先把它们对过一遍）。
    """
    lines = ["# agent-team NATS 中枢（合成夹具，非真机）",
             "# 由 gen_nats_conf.py 生成于 %s（改配置请改脚本再重跑，别手改本文件）" % stamp,
             'server_name: "%s-nats"' % node,
             "listen: %s:9930" % host,
             "http: 127.0.0.1:9932",
             "",
             "cluster {",
             '  name: "agent-team"',
             "  listen: %s:9931" % host,
             '  authorization { user: "route", password: "rp", timeout: 3 }']
    if routes_to:
        lines.append('  routes = [ "nats-route://route:rp@%s:9931" ]' % routes_to)
    lines += ["}", "", "authorization {", "  timeout: 3", "  users = ["]
    for i, (user, password, perm) in enumerate(users):
        tail = "," if i < len(users) - 1 else ""
        lines.append('    { user: "%s", password: "%s", permissions: { %s } }%s'
                     % (user, password, perm, tail))
    lines += ["  ]", "}", ""]
    return "\n".join(lines)


def _users(names):
    """`[(成员, 口令, 规范 permissions 串)]` —— permissions 由 L1 唯一出处现生成。"""
    return [(n, PW[n], gen_nats_conf.perm_list(n)) for n in names]


def _clean():
    """零差异例的骨架：档案 3 人、两中枢用户区同形、每人一份内容相符的凭据文件。"""
    users = _users(("alpha", "beta", "gamma"))
    return {
        "roster": {"hub_list": list(HUBS),
                   "roster": [list(r) for r in BASE_ROSTER]},
        "nodes": {
            "hub-a": {"nats.conf": hub_conf("hub-a", H_A, users),
                      "creds": {"beta.pass": PW["beta"]}},
            "hub-b": {"nats.conf": hub_conf("hub-b", H_B, users, routes_to=H_A),
                      "creds": {"gamma.pass": PW["gamma"]}},
            "edge-n": {"creds": {"nats-alpha.pass": PW["alpha"]}},
        },
    }


# ============================================================ 6 例冻结夹具

def _case_clean():
    """档案 / 凭据 / 两中枢用户区**完全一致** → 差异 0、退出码 0。"""
    return _clean()


def _case_order_swap():
    """档案顺序与 conf 不一致（把前两条换个个儿）→ 第 4、5 行各不同。"""
    fx = _clean()
    rows = [list(r) for r in BASE_ROSTER]
    rows[0], rows[1] = rows[1], rows[0]
    fx["roster"]["roster"] = rows
    return fx


def _case_cred_mismatch():
    """某成员的凭据文件内容变了（`p1` → `zz`）→ 该成员那一行不同。"""
    fx = _clean()
    fx["nodes"]["edge-n"]["creds"]["nats-alpha.pass"] = "zz"
    return fx


def _case_missing_cred():
    """某成员的凭据文件缺失 → 推导侧算不出来 → 退出码 2 + `collect_errors` 非空。"""
    fx = _clean()
    del fx["nodes"]["hub-b"]["creds"]["gamma.pass"]
    return fx


def _case_extra_in_conf():
    """现网 conf 多一个名册外用户（`delta`）→ 末条逗号 + 多出的行，共 4 行不同。"""
    fx = _clean()
    users = _users(("alpha", "beta", "gamma", "delta"))
    fx["nodes"]["hub-a"]["nats.conf"] = hub_conf("hub-a", H_A, users)
    fx["nodes"]["hub-b"]["nats.conf"] = hub_conf("hub-b", H_B, users, routes_to=H_A)
    return fx


def _case_perm_drift():
    """只在 hub-a 上把某 user 的权限串改一个字（心跳主题放成通配）→ hub-a 1 行不同、hub-b 仍 0。"""
    fx = _clean()
    drifted = [(u, p, (perm.replace('"team.hb.beta"', '"team.hb.>"') if u == "beta" else perm))
               for u, p, perm in _users(("alpha", "beta", "gamma"))]
    fx["nodes"]["hub-a"]["nats.conf"] = hub_conf("hub-a", H_A, drifted)
    return fx


#: 冻结期望：`(每 hub 的差异行数, CLI 退出码, 是否应有采集错误)`。
#: **这是契约，不是「跑出来是多少就记多少」** —— 改了判定口径就会在这里立刻撞红。
CASES = (
    ("clean",         _case_clean,         {"hub-a": 0, "hub-b": 0}, 0, False),
    ("order_swap",    _case_order_swap,    {"hub-a": 2, "hub-b": 2}, 1, False),
    ("cred_mismatch", _case_cred_mismatch, {"hub-a": 1, "hub-b": 1}, 1, False),
    ("missing_cred",  _case_missing_cred,  {"hub-a": 0, "hub-b": 0}, 2, True),
    ("extra_in_conf", _case_extra_in_conf, {"hub-a": 4, "hub-b": 4}, 1, False),
    ("perm_drift",    _case_perm_drift,    {"hub-a": 1, "hub-b": 0}, 1, False),
)


# ============================================================ 夹具落盘

def _write(path, text):
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def materialize(fx, root):
    """把一例夹具落到 `root/`：`roster.json` + `<node>/nats.conf` + `<node>/creds/<文件名>`。"""
    _write(os.path.join(root, "roster.json"),
           json.dumps(fx["roster"], ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    for node, files in sorted(fx["nodes"].items()):
        base = os.path.join(root, node)
        if not os.path.isdir(base):
            os.makedirs(base)
        for name, body in sorted(files.items()):
            if name == "creds":
                for filename, content in sorted(body.items()):
                    _write(os.path.join(base, "creds", filename), content)
            else:
                _write(os.path.join(base, name), body)
    return root


def emit_all(dest):
    """`--emit`：把 6 例夹具原样落盘，供独立重跑与人眼核对。"""
    for name, build, _want, _rc, _err in CASES:
        target = os.path.join(dest, name)
        if os.path.isdir(target):
            shutil.rmtree(target)
        materialize(build(), target)
    print("已落盘 %d 例冻结夹具 → %s" % (len(CASES), dest))
    return 0


# ============================================================ 两侧求值

def new_report(root):
    """新侧：正件的夹具采集器 + 正件的 `analyze()`，模板注入 L1 `gen_nats_conf.perm_list`。"""
    return NEW.analyze(NEW.collect_fixtures(root), gen_nats_conf.perm_list)


def old_report(root):
    """旧侧：独立第二实现的采集器 + 它自己的 `analyze()`（用它自己那份模板）。"""
    return OLD.report_of(root)


def cli_rc(root):
    """真起一次 CLI 子进程，只看退出码（`--fixtures` 必须离线可跑）。"""
    proc = subprocess.run([sys.executable, DERIVE_PY, "--fixtures", root, "--json"],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
    return proc.returncode


# ============================================================ A / B / C 三段

TEMPLATE_NAMES = ("alpha", "beta", "gamma", "delta", "claudecode", "codex", "hermes",
                  "lobster", "openclaw-nb", "piagent", "quantum", "top001")

#: B 段：抽块器的交叉核对样本（含 4 例畸形）。`None` 那侧比的是「说明文案」是否也一致。
BLOCK_SAMPLES = (
    ("真形状", hub_conf("hub-a", H_A, _users(("alpha", "beta", "gamma")), routes_to=H_B)),
    ("口令含花括号",
     hub_conf("hub-a", H_A, [("alpha", "p{}", gen_nats_conf.perm_list("alpha"))])),
    ("单用户", hub_conf("hub-b", H_B, _users(("beta",)))),
    ("空文本", ""),
    ("没有 users 区块",
     'server_name: "x"\ncluster {\n  name: "agent-team"\n}\n'),
    ("users 在非 authorization 块里",
     'accounts {\n  users = [\n    { user: "alpha" }\n  ]\n}\n'),
    ("花括号未配平",
     'authorization {\n  timeout: 3\n  users = [\n    { user: "alpha" }\n  ]\n'),
)

#: B 段（后半）：**归一化器**的交叉核对样本。
#: 为什么单列：归一化第 2 条（生成时间戳行）在「用户区」上**永远不会命中** —— 那行在文件第 4 行、
#: 不在区内。若只靠 C 段夹具，这条冻结口径就是**零覆盖**（实现了但从没被验过）。
#: 这里直接对文本比两侧的 `normalize()`，把缺口补上。
NORMALIZE_SAMPLES = (
    ("时间戳行",
     "# 由 gen_nats_conf.py 生成于 2026-01-02 03:04:05（改配置请改脚本再重跑，别手改本文件）"),
    ("时间戳行·另一时刻",
     "# 由 gen_nats_conf.py 生成于 2099-12-31 23:59:59（改配置请改脚本再重跑，别手改本文件）"),
    ("形似时间戳但不是", "# 由 gen_nats_conf.py 生成于 2026-01-02 03:04:05"),
    ("一行两个口令",
     '{ user: "a", password: "p1" } { user: "b", password: "p2" }'),
    ("password 后多空格", '    password:    "p3",'),
    ("空口令", '  password: "",'),
    ("没有口令的普通行", '  timeout: 3'),
)


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

    # --- B 段：两条抽块路径交叉核对（块文本 + 说明文案都要一致）
    for label, text in BLOCK_SAMPLES:
        lb, lw = NEW.extract_users_block(text)
        rb, rw = OLD.extract_users_block(text)
        same = (lb == rb) and (lw == rw)
        bad += 0 if same else 1
        if same:
            note = ("抽出 %d 行" % len(lb.split("\n"))) if lb is not None else "同判空：%s" % lw
        else:
            note = "新 %r/%r ≠ 旧 %r/%r" % (
                (lb or "")[:40], lw, (rb or "")[:40], rw)
        cells.append(("B", "block:%s" % label, same, note))

    # --- B 段（后半）：两条归一化路径交叉核对（正则 vs 零正则手写扫描器）
    for label, text in NORMALIZE_SAMPLES:
        left, right = NEW.normalize(text), OLD.normalize(text)
        same = (left == right)
        bad += 0 if same else 1
        cells.append(("B", "norm:%s" % label, same,
                      left if same else "新 %r / 旧 %r" % (left, right)))

    # --- C 段：6 例冻结夹具
    tmp = tempfile.mkdtemp(prefix="k27fx-")
    try:
        for name, build, want, want_rc, want_err in CASES:
            root = materialize(build(), os.path.join(tmp, name))
            new, old = new_report(root), old_report(root)
            n_text, o_text = NEW.canonical(new), NEW.canonical(old)
            same = (n_text == o_text)
            bad += 0 if same else 1
            cells.append(("C", "%s/json" % name, same,
                          "规范 JSON 逐字节一致" if same else _first_diff(n_text, o_text)))
            got_err = bool(new["collect_errors"]) and bool(old["collect_errors"])
            for row_new in new["per_hub"]:
                hub = row_new["hub"]
                row_old = next(r for r in old["per_hub"] if r["hub"] == hub)
                expect = want[hub]
                hit = (row_new["diff_count"] == expect and row_old["diff_count"] == expect)
                bad += 0 if hit else 1
                cells.append(("C", "%s/%s" % (name, hub), hit,
                              "差异 %d 行（冻结期望 %d）" % (row_new["diff_count"], expect)))
            rc = cli_rc(root)
            hit = (rc == want_rc) and (got_err == want_err)
            bad += 0 if hit else 1
            cells.append(("C", "%s/rc" % name, hit,
                          "退出码 %d（期望 %d）· 采集错误 %s（期望 %s）"
                          % (rc, want_rc, got_err, want_err)))
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
    """改一个凭据内容。"""
    fx["nodes"]["hub-a"]["creds"]["beta.pass"] = "qq"
    return fx, "改一个凭据内容"


def _poison_order(fx):
    """换档案顺序（后两条对调）。"""
    rows = [list(r) for r in BASE_ROSTER]
    rows[1], rows[2] = rows[2], rows[1]
    fx["roster"]["roster"] = rows
    return fx, "换档案顺序"


def _poison_drop_user(fx):
    """删 conf 里一个用户。"""
    users = _users(("alpha", "gamma"))
    fx["nodes"]["hub-a"]["nats.conf"] = hub_conf("hub-a", H_A, users)
    fx["nodes"]["hub-b"]["nats.conf"] = hub_conf("hub-b", H_B, users, routes_to=H_A)
    return fx, "删 conf 里一个用户"


POISONS = (_poison_cred, _poison_order, _poison_drop_user)


def run_negative():
    """注毒三处，每处**两侧都必须报红**；还原后必须转绿。返回 `(行列表, 失败数)`。"""
    rows, bad = [], 0
    tmp = tempfile.mkdtemp(prefix="k27neg-")
    try:
        for i, poison in enumerate(POISONS):
            fx, what = poison(_clean())
            root = materialize(fx, os.path.join(tmp, "poison-%d" % i))
            new, old = new_report(root), old_report(root)
            n_diff = sum(r["diff_count"] for r in new["per_hub"])
            o_diff = sum(r["diff_count"] for r in old["per_hub"])
            red = (not new["ok"]) and (not old["ok"]) and n_diff > 0 and o_diff > 0
            bad += 0 if red else 1
            rows.append(("注毒 %s" % what, red, "新 %d 行 / 旧 %d 行差异" % (n_diff, o_diff)))
        root = materialize(_clean(), os.path.join(tmp, "restored"))
        new, old = new_report(root), old_report(root)
        green = new["ok"] and old["ok"]
        bad += 0 if green else 1
        rows.append(("还原（无毒）", green, "新 ok=%s / 旧 ok=%s" % (new["ok"], old["ok"])))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return rows, bad


# ============================================================ 主流程

def main(argv=None):
    ap = argparse.ArgumentParser(description="刀27 · 档案+凭据⇒用户区 · 等价/冻结矩阵")
    ap.add_argument("--runs", type=int, default=1, help="连跑 N 轮，自证确定性（逐字节相同）")
    ap.add_argument("--negative", action="store_true", help="强制负控：注毒三处必须报红")
    ap.add_argument("--emit", default="", help="把 6 例冻结夹具落盘到该目录")
    ap.add_argument("--quiet", action="store_true", help="只打不一致的格与汇总")
    args = ap.parse_args(argv)

    if args.emit:
        return emit_all(args.emit)

    if args.negative:
        rows, bad = run_negative()
        print("=" * 78)
        print("刀27 · 强制负控（装置本身有没有判别力）")
        print("=" * 78)
        for what, ok, note in rows:
            print("  %s %-24s %s" % ("✓" if ok else "✗", what, note))
        print("\n负控判定：%s" % ("通过 —— 注毒全红、还原转绿"
                                  if not bad else "**失败 —— 有一处该红没红**"))
        return 0 if not bad else 1

    digests, cells, bad = [], None, 0
    for _ in range(max(1, args.runs)):
        cells, bad = run_matrix()
        digests.append(digest_of(cells)[0])

    print("=" * 78)
    print("刀27 · 档案+凭据 ⇒ 中枢用户区 · 等价/冻结矩阵（%d 格 × %d 轮）"
          % (len(cells), max(1, args.runs)))
    print("新侧 = tools/roster_derive_conf.py（回溯定位 + 正则归一；模板 import L1 perm_list）")
    print("旧侧 = _old/roster_derive_old_k27.py（顶层切块 + 零正则手写扫描器；模板自拼）")
    print("=" * 78)
    seg = None
    for segment, name, ok, note in cells:
        if args.quiet and ok:
            continue
        if segment != seg:
            print("\n[%s 段]" % segment)
            seg = segment
        print("  %s %-28s %s" % ("✓" if ok else "✗", name, note))

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
