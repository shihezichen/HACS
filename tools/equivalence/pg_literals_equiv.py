#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pg_literals_equiv.py —— 刀16 等价证据：三份重复实现收口到 L2 `pg_literals.py` 后的逐例对照

旧侧 = tools/equivalence/_old/pg_literals_old_k16.py（搬迁前三份实现的**逐字节**快照，
       由改前树 `git archive HEAD` 独立解包后切片生成，非工作区自比）
新侧 = agent-team/pg_literals.py 的两个函数，以及三个 L3 调用件保留的**同名别名**
       （blackboard_svc._vector_literal / _to_utc、shared_content.vector_literal / to_utc、
        backfill_embeddings._vector_literal）

做四件事（都**真 import 两侧模块、真调函数**，一处 main() 打桩都没有）：

  A. 单元级 · vector_literal —— 每个用例喂给 6 个可调用对象（旧 3 份 / 新 3 条别名 + 出处），
     逐字节比对「返回值 + 返回值类型 + 异常类型 + 异常消息」。
  B. 单元级 · to_utc —— 同上，旧 2 份 / 新 3 条。
  C. 别名面 · 对象身份 —— 断言三个 L3 件的同名别名**就是** pg_literals 里那一个函数对象
     （不是拷贝、不是包装），且模块属性面仍可访问（blackboard_svc._to_utc 等）。
  D. 黑盒对照 —— 走 blackboard_svc / shared_content 的**公开函数**（不碰库、不起服务）：
     对同一批输入，把模块里的私有别名临时换回**旧侧冻结实现**再跑一遍，两次输出必须
     逐字节相同 —— 证明收口没有改变对外口径。

用法：
    python tools/equivalence/pg_literals_equiv.py              # 跑一轮
    python tools/equivalence/pg_literals_equiv.py --runs 5     # 连跑 5 轮自证确定性
    python tools/equivalence/pg_literals_equiv.py --old-tree D:/tmp/before  # 另起独立旧树对照

退出码：0 全一致；1 有不一致；2 装置自身前置条件不满足（旧侧快照缺失等）。
哨兵：全一致打 EQUIV_SENTINEL_OK；--runs N 全轮逐字节相同再打 EQUIV_DETERMINISM_OK。
"""

import argparse
import datetime
import decimal
import hashlib
import importlib
import io
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
AGENT_TEAM = os.path.join(REPO, "agent-team")
OLD_DIR = os.path.join(HERE, "_old")

for p in (AGENT_TEAM, OLD_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)


# ============================================================ 取值 / 取异常的统一口径

def probe(fn, make_arg):
    """调一次，把结果压成可逐字节比对的三元组。

    异常也算结果：类型名与消息都进比对 —— 「认不出来返回 None」和「抛 ValueError」
    不是同一件事，不能因为都"没返回值"就算等价。
    """
    try:
        value = fn(make_arg())
    except BaseException as exc:                     # noqa: BLE001 —— 故意抓全，异常本身是判据
        return ("RAISE", type(exc).__name__, str(exc))
    return ("VALUE", type(value).__name__, repr(value))


def digest(cell):
    """大向量的 repr 有上万字符，打印时压成「长度 + md5 + 首尾」，比对仍用全量。"""
    kind, tname, text = cell
    if len(text) <= 96:
        return "%s/%s/%s" % (kind, tname, text)
    md5 = hashlib.md5(text.encode("utf-8")).hexdigest()[:12]
    return "%s/%s/len=%d md5=%s %s…%s" % (kind, tname, len(text), md5, text[:28], text[-16:])


# ============================================================ 用例表

UTC8 = datetime.timezone(datetime.timedelta(hours=8))


def gen_five():
    return (x / 3.0 for x in range(5))


VECTOR_CASES = [
    ("None（不写向量）",              lambda: None),
    ("空列表",                        lambda: []),
    ("单元素",                        lambda: [0.5]),
    ("三个整数",                      lambda: [1, 2, 3]),
    ("整数与浮点混排",                lambda: [1, 2.5, 3]),
    ("科学计数字面量",                lambda: [1e10, 1e-10, 2.5e-3]),
    ("小量 1e-7",                     lambda: [1e-7, -1e-7, 1.0000001e-7]),
    ("负零与正零",                    lambda: [-0.0, 0.0]),
    ("inf / -inf / nan",              lambda: [float("inf"), float("-inf"), float("nan")]),
    ("浮点边界 1e308 / 1e-308",       lambda: [1e308, 1e-308, 5e-324]),
    ("repr 位数敏感值",               lambda: [0.1, 0.2, 0.1 + 0.2, 1 / 3.0]),
    ("元组输入",                      lambda: (1.5, 2.5)),
    ("生成器输入（只能消费一次）",    gen_five),
    ("可转浮点的字符串",              lambda: ["1.5", "2", "3e4"]),
    ("Decimal 元素",                  lambda: [decimal.Decimal("1.5"), decimal.Decimal("2")]),
    ("bool 元素",                     lambda: [True, False]),
    ("1536 维（生产维度）",           lambda: [i * 0.001 for i in range(1536)]),
    ("4097 维（超长 > 4096）",        lambda: [i * 1e-5 for i in range(4097)]),
    ("不可转浮点的字符串 → 抛",       lambda: ["abc"]),
    ("元素是 None → 抛",              lambda: [None]),
    ("整数当参数（不可迭代）→ 抛",    lambda: 5),
    ("字符串当参数（逐字符迭代）",    lambda: "12"),
    ("元素是 dict → 抛",              lambda: [{"a": 1}]),
]

TO_UTC_CASES = [
    ("None",                          lambda: None),
    ("空串",                          lambda: ""),
    ("纯空白串",                      lambda: "   "),
    ("非法串",                        lambda: "not a date"),
    ("纯日期串",                      lambda: "2026-09-27"),
    ("带 Z",                          lambda: "2026-09-27T12:00:00Z"),
    ("带小写 z",                      lambda: "2026-09-27T12:00:00z"),
    ("带 +08:00",                     lambda: "2026-09-27T12:00:00+08:00"),
    ("带 -05:30",                     lambda: "2026-09-27T12:00:00-05:30"),
    ("带小数秒 + Z",                  lambda: "2026-09-27T12:00:00.123456Z"),
    ("带 1 位小数秒",                 lambda: "2026-09-27T12:00:00.5Z"),
    ("空格分隔（PG 风格）",           lambda: "2026-09-27 12:00:00+00"),
    ("首尾空白 + Z",                  lambda: "  2026-09-27T12:00:00Z  "),
    ("双 Z（非法）",                  lambda: "2026-09-27T12:00:00ZZ"),
    ("裸时间（无时区）",              lambda: "2026-09-27T12:00:00"),
    ("naive datetime",                lambda: datetime.datetime(2026, 9, 27, 12, 0, 0)),
    ("aware datetime +08:00",         lambda: datetime.datetime(2026, 9, 27, 12, 0, 0, tzinfo=UTC8)),
    ("aware datetime UTC",            lambda: datetime.datetime(2026, 9, 27, 12, 0, 0,
                                                                tzinfo=datetime.timezone.utc)),
    ("date 对象（不是 datetime）",     lambda: datetime.date(2026, 9, 27)),
    ("int",                           lambda: 1758888888),
    ("float",                         lambda: 1758888888.5),
    ("dict",                          lambda: {"ts": "2026-09-27T12:00:00Z"}),
    ("bytes",                         lambda: b"2026-09-27T12:00:00Z"),
    ("float('nan') 的字符串",         lambda: str(float("nan"))),
    ("float('nan') 本体",             lambda: float("nan")),
    ("datetime.min + 东八区（溢出）", lambda: datetime.datetime(1, 1, 1, 0, 0, tzinfo=UTC8)),
    ("datetime.max naive",            lambda: datetime.datetime.max),
    ("年份下界串",                    lambda: "0001-01-01T00:00:00+00:00"),
    ("年份上界串",                    lambda: "9999-12-31T23:59:59+00:00"),
    ("闰秒（非法）",                  lambda: "2026-06-30T23:59:60Z"),
]


# ============================================================ 装载两侧

def load_sides(old_tree):
    """返回 (old, new_mods)。old_tree 非空时，旧侧改从那棵独立解包的改前树现取。"""
    import pg_literals_old_k16 as old

    new = {}
    for name in ("pg_literals", "blackboard_svc", "shared_content", "backfill_embeddings"):
        new[name] = importlib.import_module(name)

    old_tree_mods = None
    if old_tree:
        old_at = os.path.join(old_tree, "agent-team")
        if not os.path.isdir(old_at):
            sys.stderr.write("--old-tree 里没有 agent-team/：%s\n" % old_tree)
            raise SystemExit(2)
        old_tree_mods = _load_from_tree(old_at)
    return old, new, old_tree_mods


def _load_from_tree(old_at):
    """把改前树里的 L3 三件当**独立模块**装进来（自带 sys.path 前置，不污染新侧）。"""
    import importlib.util

    mods = {}
    saved = list(sys.path)
    sys.path.insert(0, old_at)
    try:
        for name in ("blackboard_svc", "shared_content", "backfill_embeddings"):
            spec = importlib.util.spec_from_file_location(
                "oldtree_" + name, os.path.join(old_at, name + ".py"))
            mod = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = mod
            spec.loader.exec_module(mod)
            mods[name] = mod
    finally:
        sys.path[:] = saved
    return mods


# ============================================================ 各段检查

def section_vector(out, old, new, old_tree_mods):
    """A 段：vector_literal 的 6 路（旧 3 / 新 3）逐例比对。"""
    pgl, bb, sc, bf = (new["pg_literals"], new["blackboard_svc"],
                       new["shared_content"], new["backfill_embeddings"])
    # 旧 bf 那份**没有 None 分支**（它的 SQL 不传 None），是登记在案的唯一一处形态差异。
    lanes_old = [("旧·blackboard_svc._vector_literal", old.bb_vector_literal),
                 ("旧·shared_content.vector_literal", old.sc_vector_literal)]
    lanes_new = [("新·pg_literals.vector_literal", pgl.vector_literal),
                 ("新·blackboard_svc._vector_literal", bb._vector_literal),
                 ("新·shared_content.vector_literal", sc.vector_literal),
                 ("新·backfill_embeddings._vector_literal", bf._vector_literal)]
    if old_tree_mods:
        lanes_old += [("改前树·blackboard_svc._vector_literal",
                       old_tree_mods["blackboard_svc"]._vector_literal),
                      ("改前树·shared_content.vector_literal",
                       old_tree_mods["shared_content"].vector_literal)]

    diffs = 0
    out("A 段 · vector_literal（%d 例 × %d 路）" % (len(VECTOR_CASES),
                                                    len(lanes_old) + len(lanes_new)))
    for label, make_arg in VECTOR_CASES:
        cells = [(n, probe(f, make_arg)) for n, f in lanes_old + lanes_new]
        base = cells[0][1]
        bad = [n for n, c in cells if c != base]
        flag = "OK " if not bad else "DIFF"
        out("  [%s] %-30s %s" % (flag, label, digest(base)))
        if bad:
            diffs += 1
            for n, c in cells:
                out("        %-42s %s" % (n, digest(c)))
    return diffs


def section_bf_none(out, old, new):
    """A' 段：backfill 旧实现缺 None 分支这一处形态差异，如实记账 + 证不可达。"""
    bf = new["backfill_embeddings"]
    old_cell = probe(old.bf_vector_literal, lambda: None)
    new_cell = probe(bf._vector_literal, lambda: None)
    out("A' 段 · backfill_embeddings 旧实现无 None 分支（登记差异，不是回归）")
    out("  旧：%s" % digest(old_cell))
    out("  新：%s" % digest(new_cell))
    src = io.open(os.path.join(AGENT_TEAM, "backfill_embeddings.py"),
                  "r", encoding="utf-8").read()
    kw = src.count("_vector_literal(values=") + src.count("_vector_literal(embedding=")
    calls = src.count("_vector_literal(vec)")
    out("  调用点：`_vector_literal(vec)` × %d（位置参数），关键字调用 × %d" % (calls, kw))
    out("  vec 来自 embedder.embed_texts 的元素，恒非 None → 该分支在生产路径上不可达")
    bad = 0
    if kw:
        out("  [DIFF] 出现关键字调用，形参改名（values→embedding）会穿帮")
        bad += 1
    if calls < 1:
        out("  [DIFF] 没找到预期的位置参数调用点")
        bad += 1
    if not bad:
        out("  [OK ] 差异不可达，且无关键字调用")
    return bad


def section_to_utc(out, old, new, old_tree_mods):
    """B 段：to_utc 的 5 路（旧 2 / 新 3）逐例比对。"""
    pgl, bb, sc = new["pg_literals"], new["blackboard_svc"], new["shared_content"]
    lanes_old = [("旧·blackboard_svc._to_utc", old.bb_to_utc),
                 ("旧·shared_content.to_utc", old.sc_to_utc)]
    lanes_new = [("新·pg_literals.to_utc", pgl.to_utc),
                 ("新·blackboard_svc._to_utc", bb._to_utc),
                 ("新·shared_content.to_utc", sc.to_utc)]
    if old_tree_mods:
        lanes_old += [("改前树·blackboard_svc._to_utc",
                       old_tree_mods["blackboard_svc"]._to_utc),
                      ("改前树·shared_content.to_utc",
                       old_tree_mods["shared_content"].to_utc)]

    diffs = 0
    out("B 段 · to_utc（%d 例 × %d 路）" % (len(TO_UTC_CASES),
                                            len(lanes_old) + len(lanes_new)))
    for label, make_arg in TO_UTC_CASES:
        cells = [(n, probe(f, make_arg)) for n, f in lanes_old + lanes_new]
        base = cells[0][1]
        bad = [n for n, c in cells if c != base]
        flag = "OK " if not bad else "DIFF"
        out("  [%s] %-30s %s" % (flag, label, digest(base)))
        if bad:
            diffs += 1
            for n, c in cells:
                out("        %-42s %s" % (n, digest(c)))
    return diffs


def _imported_modules(path):
    """AST 取顶层被 import 的模块名（只看名字，不执行）。"""
    import ast

    tree = ast.parse(io.open(path, "r", encoding="utf-8").read())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def section_surface(out, new):
    """C 段：别名面 —— 必须是同一个函数对象，模块属性面一个名都不能少。"""
    pgl, bb, sc, bf = (new["pg_literals"], new["blackboard_svc"],
                       new["shared_content"], new["backfill_embeddings"])
    checks = [
        ("blackboard_svc._vector_literal is pg_literals.vector_literal",
         bb._vector_literal is pgl.vector_literal),
        ("blackboard_svc._to_utc is pg_literals.to_utc",
         bb._to_utc is pgl.to_utc),
        ("shared_content.vector_literal is pg_literals.vector_literal",
         sc.vector_literal is pgl.vector_literal),
        ("shared_content.to_utc is pg_literals.to_utc",
         sc.to_utc is pgl.to_utc),
        ("backfill_embeddings._vector_literal is pg_literals.vector_literal",
         bf._vector_literal is pgl.vector_literal),
        ("blackboard_svc 仍自带 UTC 常量（没被 import 顶掉）",
         bb.UTC is datetime.timezone.utc and "UTC" in vars(bb)),
        ("shared_content 仍自带 UTC / now_utc",
         sc.UTC is datetime.timezone.utc and callable(sc.now_utc)),
        ("pg_literals.UTC 就是 datetime.timezone.utc",
         pgl.UTC is datetime.timezone.utc),
        ("pg_literals 只 import 标准库（datetime / typing），不碰 psycopg2 与任何上层件",
         _imported_modules(os.path.join(AGENT_TEAM, "pg_literals.py")) == {"datetime", "typing"}),
    ]
    out("C 段 · 别名面与对象身份（%d 项）" % len(checks))
    bad = 0
    for label, ok in checks:
        out("  [%s] %s" % ("OK " if ok else "DIFF", label))
        if not ok:
            bad += 1
    return bad


# ---------------------------------------------------------------- D 段 · 黑盒

FIXED_NOW = datetime.datetime(2026, 9, 27, 12, 0, 0, tzinfo=datetime.timezone.utc)
OPEN_STATES = ("open", "doing")

TMP_ROW = ("t-ABCD2345", "hermes", '["alice","bob"]', "标题", "正文", 6, "sha256:dead",
           3, 1, '["alice"]', "2026-09-27T11:00:00Z", "2026-09-27T10:00:00Z", None, None)

BLACKBOX_INPUTS = [
    "2026-09-27T11:59:59Z",          # 早于 now → 逾期 / 过期
    "2026-09-27T12:00:00Z",          # 正好等于 now → 边界，不逾期
    "2026-09-27T12:00:01Z",          # 晚于 now
    "2026-09-27T20:00:00+08:00",     # 换算后正好等于 now
    "2026-09-27T11:59:59",           # 裸时间按 UTC 解释
    "2026-09-27",                    # 纯日期
    None,
    "",
    "garbage",
    {"x": 1},
    1758888888,
    datetime.datetime(2026, 9, 27, 11, 0, 0),
    datetime.datetime(2026, 9, 27, 20, 0, 0, tzinfo=UTC8),
]


def _blackbox_run(bb, sc):
    """跑一遍公开函数，返回一串可逐字节比对的文本行。"""
    lines = []
    for v in BLACKBOX_INPUTS:
        for st in ("open", "done"):
            lines.append("is_overdue(%r, %r) = %r"
                         % (v, st, bb.is_overdue(v, st, OPEN_STATES, FIXED_NOW)))
        lines.append("shared_is_expired(%r) = %r" % (v, bb.shared_is_expired(v, FIXED_NOW)))
        row = list(TMP_ROW)
        row[10] = v
        item = bb.row_to_tmp(row, FIXED_NOW, with_body=False)
        lines.append("row_to_tmp(expires_at=%r).expired = %r / expires_at=%r"
                     % (v, item["expired"], item["expires_at"]))
        lines.append("shared_content.iso(%r) = %r" % (v, sc.iso(v)))
        lines.append("shared_content.fmt_time(%r) = %r" % (v, sc.fmt_time(v)))
    return lines


def section_blackbox(out, old, new, old_tree_mods):
    """D 段：公开函数黑盒 —— 把私有别名临时换回旧侧冻结实现，两趟输出必须逐字节相同。"""
    bb, sc = new["blackboard_svc"], new["shared_content"]
    new_lines = _blackbox_run(bb, sc)

    saved = (bb._to_utc, sc.to_utc)
    try:
        bb._to_utc = old.bb_to_utc
        sc.to_utc = old.sc_to_utc
        old_lines = _blackbox_run(bb, sc)
    finally:
        bb._to_utc, sc.to_utc = saved

    lanes = [("旧侧冻结实现回插", old_lines)]
    if old_tree_mods:
        lanes.append(("改前树原件", _blackbox_run(old_tree_mods["blackboard_svc"],
                                                  old_tree_mods["shared_content"])))

    out("D 段 · 黑盒对照 · 公开函数（%d 例 × %d 路；is_overdue / shared_is_expired / "
        "row_to_tmp / shared_content.iso / shared_content.fmt_time）"
        % (len(new_lines), len(lanes) + 1))
    diffs = 0
    for name, other in lanes:
        n = sum(1 for a, b in zip(new_lines, other) if a != b) + abs(len(new_lines) - len(other))
        out("  [%s] 新侧 vs %s：逐行 diff %d 行（md5 新=%s 旧=%s）"
            % ("OK " if not n else "DIFF", name, n,
               hashlib.md5("\n".join(new_lines).encode("utf-8")).hexdigest()[:12],
               hashlib.md5("\n".join(other).encode("utf-8")).hexdigest()[:12]))
        if n:
            diffs += n
            for a, b in zip(new_lines, other):
                if a != b:
                    out("        新 | %s" % a)
                    out("        旧 | %s" % b)
    out("  样本（前 6 行）：")
    for line in new_lines[:6]:
        out("        %s" % line)
    return diffs


# ============================================================ 主流程

def run_once(old_tree):
    buf = []

    def out(line):
        buf.append(line)

    try:
        old, new, old_tree_mods = load_sides(old_tree)
    except ImportError as exc:
        sys.stderr.write("旧侧快照装不进来：%s\n" % exc)
        raise SystemExit(2)

    out("=" * 78)
    out("刀16 等价装置 · pgvector 字面量 / 时间归一 收口到 L2 pg_literals")
    out("旧侧：tools/equivalence/_old/pg_literals_old_k16.py"
        + ("（外加 --old-tree 改前树原件）" if old_tree_mods else ""))
    out("新侧：agent-team/pg_literals.py + 三个 L3 件的同名别名")
    out("=" * 78)
    out("")

    total = 0
    total += section_vector(out, old, new, old_tree_mods)
    out("")
    total += section_bf_none(out, old, new)
    out("")
    total += section_to_utc(out, old, new, old_tree_mods)
    out("")
    total += section_surface(out, new)
    out("")
    total += section_blackbox(out, old, new, old_tree_mods)
    out("")
    out("=" * 78)
    out("合计不一致：%d" % total)
    out("EQUIV_SENTINEL_OK" if total == 0 else "EQUIV_SENTINEL_FAIL")
    return total, "\n".join(buf) + "\n"


def main():
    ap = argparse.ArgumentParser(description="刀16 等价装置")
    ap.add_argument("--runs", type=int, default=1, help="连跑 N 轮自证确定性（默认 1）")
    ap.add_argument("--old-tree", default=None,
                    help="改前树根目录（含 agent-team/），给则额外起改前树原件做独立对照")
    args = ap.parse_args()

    if args.runs > 1:
        outs = []
        for i in range(args.runs):
            cmd = [sys.executable, os.path.abspath(__file__)]
            if args.old_tree:
                cmd += ["--old-tree", args.old_tree]
            p = subprocess.run(cmd, capture_output=True)
            outs.append((p.returncode, p.stdout))
            sys.stdout.write("第 %d/%d 轮：rc=%d，%d 字节，md5=%s\n"
                             % (i + 1, args.runs, p.returncode, len(p.stdout),
                                hashlib.md5(p.stdout).hexdigest()))
        rcs = {rc for rc, _ in outs}
        same = len({o for _, o in outs}) == 1
        sys.stdout.write("退出码集合=%s；%d 份输出逐字节%s\n"
                         % (sorted(rcs), len(outs), "相同" if same else "**不同**"))
        if same and rcs == {0}:
            sys.stdout.write("EQUIV_DETERMINISM_OK\n")
            return 0
        sys.stdout.write("EQUIV_DETERMINISM_FAIL\n")
        return 1

    total, text = run_once(args.old_tree)
    sys.stdout.write(text)
    return 0 if total == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
