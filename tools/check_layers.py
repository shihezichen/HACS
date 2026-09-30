#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_layers.py —— 分层门禁（阶段 0 产出）

五条检查，对应 docs/HACS-分层规划-handoff.html 的七条解耦判据：

  检查 1  依赖方向（判据 2）：只允许上层依赖下层；同层可互依；横切 X 可被任意层引用。
  检查 2  传输不含语义（判据 5）：L1 通信层的代码里不该出现业务词。
  检查 3  配置零硬编码（判据 4）：运行件里的 IP 字面量 / 绝对路径常量。
  检查 4  存储句柄（判据 1 的前置）：哪些件直接持数据库 / 文件句柄（持句柄的件必须在自己层内）。
  检查 5  跨件私有面访问（判据 1）：契约先于实现 —— 跨层只走公开契约，禁跨件摸 `_` 私有面。
  检查 6  相方向（裁定① / v3 E3，**硬门禁**）：顶层相 hub/ · node/ · shared/；相内件可向
          shared/ 单向 import，**shared/ 不得 import 任何相内件**。

另有「自检 · 门禁自身完整性」7 项（批① 门禁修复新增，任一项 ✗ ⇒ 退出码 1）：包路径回环 /
已搬迁件（nats_client · gen_nats_conf）的边可识别（批① 回归锚）/ 包路径前缀与 os.path·json.loads
这类「同名非件」负控 / 档位接线（shared 入 layer_order + phases 明文）/ shared 相判据合成夹具
非空转 / 解析无歧义无兜底 / 边无重复。

用法：
  python tools/check_layers.py                # 全量报告
  python tools/check_layers.py --only 1,2     # 只看某几条
  python tools/check_layers.py --json         # 机器可读（含 edge_list 边表）
  python tools/check_layers.py --dump-edges   # 纯文本逐行打印边表（源件:行 -> 依赖件）

退出码：0 = 无「硬违规」；1 = 有硬违规或自检失败（检查 1 方向违规 / 检查 6 相方向违规 /
自检 ✗）；2 = 当前目录不是项目源码树。
检查 2/3/4/5 默认是「提示级」——它们的命中需要人判断，不直接判死（见台账第八节）。
"""
from __future__ import annotations

import argparse
import ast
import io
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)
MAP_PATH = os.path.join(HERE, 'hacs_layers.json')
MISSING = set()
DECLARED = []      # 已声明的例外（带理由+到期条件；门禁照常打印，只是不判违规）       # 读不到的源文件（用于「源码树守卫」；read_src 会被多次调用，故用集合去重）
AMBIGUOUS = {}     # 点分包路径 → 命中的多条登记路径（歧义；由自检项报出，不静默取一条）
PARSED_BAD = []    # ast 解析不通、退回过行正则的件（由自检项报出，不静默降级）


def load_map():
    with io.open(MAP_PATH, encoding='utf-8') as fh:
        return json.load(fh)


def read_src(rel: str) -> str:
    p = os.path.join(PROJECT_ROOT, rel.replace('/', os.sep))
    if not os.path.isfile(p):
        MISSING.add(rel)
        return ''
    return io.open(p, encoding='utf-8', errors='replace').read()


# ---------------------------------------------------------------------------
# import 解析（批① 门禁修复）：把 import 语句**规范化到台账登记件**
# ---------------------------------------------------------------------------
# 旧口径（已修的 bug）：正则 `^\s*(?:import|from)\s+([A-Za-z_][A-Za-z0-9_]*)` 只抓**第一个
# 标识符** —— `from node.l1_transport.nats_client import NatsClient` 被解析成 `node`，既不是
# 件名也不在台账里，于是**整条边被静默丢弃**（批① 搬迁后边数 97 → 84，丢的 13 条正是这两件
# 的引用面：迁出件自身的 import 边 + 全仓对它们的引用边）。
#
# 新口径：解析**完整点分模块路径**（ast 为主、行正则为语法兜底），按「包路径 → 台账登记路径」
# 映射回登记件；同目录相对 import（`from .x import y`）按本件所在包折算层数。
# 防两类新误判（由「自检 ③ 前缀/同名非件负控」机械断言）：
#   ① 包路径前缀（node / hub / shared / backend / …）不得被当成件名 —— 映射表只登记**登记件
#      路径的点分后缀**，前缀本身不在表里，`import node` 解析为空；
#   ② os.path / json.loads / os.environ 这类「同名非件」不得被当成件 —— 多段点分路径必须
#      **整段等于**某条登记路径的点分后缀（= Python 绝对 import 语义），不做末段模糊匹配。
# 边数**不再只看总数**：`--edges` 逐条打印（件:行 → 登记路径），可与 `git grep` 实测对账。


def _dotted_of(rel: str) -> str:
    """台账登记路径 → 完整点分包路径（`a/b/c.py` → `a.b.c`；`a/b/__init__.py` → `a.b`）。"""
    parts = rel.replace(os.sep, '/').split('/')
    if not parts[-1].endswith('.py'):
        return '.'.join(parts)
    stem = parts[-1][:-3]
    if stem == '__init__':
        return '.'.join(parts[:-1])
    return '.'.join(parts[:-1] + [stem])


def build_pkg_index(files: dict) -> dict:
    """点分包路径 → 台账登记路径（值 = 命中集合，用于歧义自检）。

    键含**各级后缀**（`node.l1_transport.nats_client` / `l1_transport.nats_client` /
    `nats_client`），对应「从仓根绝对 import」「从相根 import」「裸名 import」三种实际书写形态；
    后缀只到登记路径的**点分边界**为止，故前缀（`node`）与「同名非件」（`os.path`）都进不了表。
    """
    idx = {}
    for rel in files:
        rel = rel.replace(os.sep, '/')
        parts = _dotted_of(rel).split('.')
        for i in range(len(parts)):
            idx.setdefault('.'.join(parts[i:]), set()).add(rel)
    return idx


def _lookup_pkg(pkg_idx: dict, dotted: str):
    hits = pkg_idx.get(dotted)
    if not hits:
        return None
    if len(hits) > 1:
        AMBIGUOUS[dotted] = sorted(hits)
    return sorted(hits)[0]


def _import_candidates(rel: str, src: str) -> list:
    """产出 [(行号, [候选点分模块路径])]。ast 为主（含括号 / 多行 / 相对 import）；语法不通时
    退回行正则（只抓完整点分路径），并在 PARSED_BAD 里记名，由自检项报出 —— 不静默降级。"""
    own_pkg = _dotted_of(rel).split('.')[:-1]
    out = []
    try:
        tree = ast.parse(src)
    except SyntaxError:
        if rel.replace(os.sep, '/').endswith('.py'):
            PARSED_BAD.append(rel)          # 只对 .py 记名：html/js/css 登记件本就不是 Python，退回正则是正常的
        rx = re.compile(r'^\s*(?:import|from)\s+([A-Za-z_][A-Za-z0-9_.]*)', re.M)
        for m in rx.finditer(src):
            out.append((src.count('\n', 0, m.start()) + 1, [m.group(1)]))
        return out
    for node in ast.walk(tree):
        cands = []
        if isinstance(node, ast.Import):
            cands = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            lvl = node.level or 0
            if lvl:
                if lvl - 1 > len(own_pkg):
                    continue                          # 越过仓根的相对 import：非法，不入表
                up = own_pkg[:len(own_pkg) - (lvl - 1)]
                base = '.'.join(up + ([node.module] if node.module else []))
            else:
                base = node.module or ''
            if base:
                cands.append(base)
                for a in node.names:
                    if a.name != '*':
                        cands.append(base + '.' + a.name)   # `from pkg import mod` 形态
            else:
                for a in node.names:
                    if a.name != '*':
                        cands.append(a.name)
        if cands:
            out.append((node.lineno, cands))
    return out


def local_imports(rel: str, src: str, pkg_idx: dict) -> list:
    """返回 [(行号, 依赖件登记路径)] —— 本件 import 到的**台账登记件**，按 dep 去重保留最小行号。

    自件、台账外模块（标准库 / 第三方 / 尚未登记的件）一律不入边。
    """
    me = rel.replace(os.sep, '/')
    best = {}
    for line, cands in _import_candidates(rel, src):
        for c in cands:
            hit = _lookup_pkg(pkg_idx, c)
            if not hit or hit == me:
                continue
            if hit not in best or line < best[hit]:
                best[hit] = line
    return sorted((ln, r) for r, ln in best.items())


def phase_of_rel(rel: str, phases: dict):
    """件所属**顶层相**：hub/node 为相内、shared 为相中立；不在相目录下的件返回 None。"""
    top = rel.replace(os.sep, '/').split('/')[0]
    return (phases.get('top_level') or {}).get(top)


def phase_code(rel: str, layer: str, phases: dict) -> str:
    """件在**方向判据**里的有效档位：相中立件（shared/）一律按 `layer_order` 末档 `shared` 计。

    这样单条 rank 判据（`rank[src] <= rank[dst]`）就天然实现了 v3 E3：
      · 相内件（hub/、node/）→ shared/：rank ≤ 末档 ⇒ 放行（单向可引用）；
      · shared/ → 任何相内件：末档 rank 最大 ⇒ 反向违规；
      · shared/ → shared/：同档 ⇒ 放行。
    相内件之间（hub↔node）的档位仍取各自登记层 —— 那一条由层判据管，不在本修复范围。
    """
    ph = phase_of_rel(rel, phases)
    if ph and ph == phases.get('neutral'):
        return ph
    return layer


def phase_violation(src_phase, dep_phase, phases) -> bool:
    """E3 判据的本体（单点判据，check_phases 与自检夹具共用）：**shared 不得 import 相内件**。"""
    return (src_phase == phases.get('neutral')
            and dep_phase in set(phases.get('in_phase_dirs') or []))


def check_phases(layers, files, edges):
    """检查 6 · 相方向（硬门禁）：相内件（hub/、node/）可向 shared/ 单向 import；
    shared/ 不得 import 任何相内件（v3 E3 定案）。返回 (violations, stats)。"""
    phases = phases_cfg(layers)
    stats = dict(neutral_to_in_phase=0, in_phase_to_neutral=0, in_phase_pairs=0, neutral_internal=0)
    v = []
    for e in edges:
        sp = phase_of_rel(e['src'], phases)
        dp = phase_of_rel(e['dep'], phases)
        if sp != phases.get('neutral') and dp != phases.get('neutral'):
            if sp and dp:
                stats['in_phase_pairs'] += 1
            continue
        if phase_violation(sp, dp, phases):
            stats['neutral_to_in_phase'] += 1
            v.append(dict(src=e['src'], src_phase=sp, dep=e['dep'], dep_phase=dp,
                          why='相中立件反向依赖了相内件（shared → %s）：shared/ 只许被相内件引用' % dp))
        elif sp == dp and sp == phases.get('neutral'):
            stats['neutral_internal'] += 1
        elif phase_of_rel(e['dep'], phases) == phases.get('neutral'):
            stats['in_phase_to_neutral'] += 1
    return v, stats


def check_direction(layers, files, pkg_idx, phases):
    """检查 1：依赖方向。返回 (violations, edges)

    edges 每项 = dict(src, src_layer, dep, dep_layer, line) —— **边明细保留在报告里**（`--json`
    的 `edge_list` / `--dump-edges` 的纯文本），供逐条与 `git grep` 实测 import 关系对账。
    """
    order = layers['rules']['layer_order']           # ['C','L4','L3','L2','L1','L0','shared']
    rank = {L: i for i, L in enumerate(order)}       # 越小越靠上；末档 = 相中立 shared
    cross_ok = set(layers['rules']['allowed_import']['cross_allowed'])
    exempt = set(layers['rules']['allowed_import']['exempt_src_layers'])
    ex_imp = {(e['src'], e['dst']): e
              for e in layers['rules']['allowed_import'].get('exceptions', [])}
    v, edges = [], []
    for rel, meta in files.items():
        src = read_src(rel)
        if not src:
            continue
        sl = meta['layer']
        scode = phase_code(rel, sl, phases)
        for line, dep_rel in local_imports(rel, src, pkg_idx):
            dl = files[dep_rel]['layer']
            dcode = phase_code(dep_rel, dl, phases)
            edges.append(dict(src=rel, src_layer=sl, dep=dep_rel, dep_layer=dl, line=line))
            if sl in exempt or dl in cross_ok or sl in cross_ok:
                continue
            # 相中立件（shared/）作为源的边交给检查 6 判（E3 专门判据），此处不重复判、不重复计
            if scode == phases.get('neutral'):
                continue
            if scode == dcode:
                continue
            # 允许：上层依赖下层
            if scode in rank and dcode in rank:
                ok = rank[scode] <= rank[dcode]
            else:
                ok = True
            if not ok:
                ex = ex_imp.get((rel, dep_rel)) or ex_imp.get((rel, dep_rel.split('/')[-1][:-3]))
                if ex:
                    DECLARED.append(dict(kind='依赖方向', item='%s [%s] → %s [%s]' % (rel, sl, dep_rel, dl),
                                         reason=ex.get('reason', ''), expires=ex.get('expires_when', '')))
                    continue
                v.append(dict(src=rel, src_layer=sl, dep=dep_rel, dep_layer=dl,
                              why='下层件反向依赖了上层（%s → %s）' % (scode, dcode)))
    return v, edges


def check_transport_words(layers, files):
    words = layers['rules']['transport_business_words']
    hits = []
    for rel, meta in files.items():
        if meta['layer'] != 'L1':
            continue
        src = read_src(rel)
        for i, line in enumerate(src.splitlines(), 1):
            s = line.strip()
            if s.startswith('#'):
                continue
            for w in words:
                if w in line:
                    hits.append(dict(file=rel, line=i, word=w, text=s[:88]))
                    break
    return hits


IP_RX = re.compile(r'\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b')
PATH_RX = re.compile(r'(?:[A-Za-z]:(?!//)[\\/][^\s"\')]{3,}'   # 盘符路径；
                       r'|(?<![:/\w])/(?:opt|etc|var|home|root)/[^\s"\')]{3,})')  # posix 惯例目录；
                                                                                       # 负向断言排除 URL（http:// 里的 'p:/'、'//opt/'）


TRIPLE_RX = re.compile(r'"""(?:.|\n)*?"""|\'\'\'(?:.|\n)*?\'\'\'')


def _strip_docstrings(src: str) -> str:
    """文档串里的示例路径不算硬编码（保留行数以维持行号）。"""
    return TRIPLE_RX.sub(lambda m: '\n' * m.group(0).count('\n'), src)


def host_path_owners(layers) -> list:
    """`host_path_owner` —— **兼容 str 与 list**（刀33）。

    历史形态是单值字符串（只能写一个所有者）；刀33 起是列表，因为
    `agent-team/node_paths.py` 与 `agent-team/node_profile.py` 两件同属 L0、
    语义上共同构成「宿主字面量的唯一合法去处」。

    · `str` → 视为单元素列表（旧表照常能跑）。
    · `list` → 视为「允许多个所有者」，逐项 `endswith` 匹配。

    放宽**只到这张表为止**：不在表里的件持宿主路径字面量，仍一律报「要收」。
    """
    raw = (layers.get('rules') or {}).get('host_path_owner')
    if not raw:
        return []
    if isinstance(raw, str):
        return [raw]
    return [x for x in raw if x]


def path_fp_index(layers) -> dict:
    """已登记的 `PATH_RX` 误判（刀33）：键 =（件, 命中原串），值 = 理由。

    键要求**原串全等**（不是按件豁免、不是按行号豁免）——源码一改，命中串就变，
    本条自动失效、该处立刻重新计进「要收」，不会悄悄替新硬编码背书。
    """
    out = {}
    for e in (layers.get('rules') or {}).get('path_known_false_positives') or []:
        out[(e.get('file', ''), e.get('match', ''))] = e.get('reason', '')
    return out


def check_hardcode(layers, files):
    """宿主字面量检查，**分三类**（只有一类是真该收的）：

      · `host_paths` —— 宿主路径字面量（真硬编码）。唯一合法去处 = 层归属表里声明的
        `host_path_owner`（刀33 起是**列表**：L0 `agent-team/node_paths.py` +
        `agent-team/node_profile.py`）；其余件出现即「要收」，这类数字应当单调趋 0，
        是「配置零硬编码 / 跨 OS 解耦」的可机械校验进度指标。
      · `known-FP` —— `PATH_RX` 把源码里的**正则串**（`id:\\s*`、`:\\n`、字符类
        `[...Z:\\-+.]`）误判成盘符路径。逐条实名登记在
        `rules.path_known_false_positives` 里，**检查本身保留**、命中仍逐条打印
        （文件:行号 + 原文 + 理由），只是不计进「要收」的路径指标。
      · `net_literals` —— IP / 端口默认值（提示级）。大量是**合法的**默认（loopback、
        兜底基址），强行收走反而降低可读性，故只提示不设门禁。

    文档字符串与注释里的示例地址一律不计（`_strip_docstrings`）。
    """
    run_layers = {'L1', 'L2', 'L3', 'L4'}
    owners = host_path_owners(layers)
    fp_idx = path_fp_index(layers)
    hits = []
    for rel, meta in files.items():
        if meta['layer'] not in run_layers:
            continue
        src = _strip_docstrings(read_src(rel))
        ips, paths, fps = [], [], []
        for i, line in enumerate(src.splitlines(), 1):
            s = line.strip()
            if s.startswith('#'):
                continue
            for m in IP_RX.finditer(line):
                if m.group(0).startswith('0.') or m.group(0) in ('0.0.0.0',):
                    continue
                ips.append((i, m.group(0), s[:70]))
            for m in PATH_RX.finditer(line):
                why = fp_idx.get((rel.replace('\\', '/'), m.group(0)))
                if why is None:
                    paths.append((i, m.group(0), s[:70]))
                else:
                    fps.append((i, m.group(0), s[:70], why))
        if owners and any(rel.replace('\\', '/').endswith(o) for o in owners):
            paths, fps = [], []            # 合法去处：宿主默认值的唯一出处，不计数
        if ips or paths or fps:
            hits.append(dict(file=rel, layer=meta['layer'], ips=ips[:6], ip_count=len(ips),
                             paths=paths, path_count=len(paths),
                             fps=fps, fp_count=len(fps)))
    return hits


def check_storage(layers, files):
    forbidden = set(layers['rules']['storage_handle_forbidden_layers'])
    ex_store = {e['file']: e for e in layers['rules'].get('storage_handle_exceptions', [])}
    hits = []
    for rel, meta in files.items():
        src = read_src(rel)
        if not src:
            continue
        kinds = []
        if re.search(r'^\s*import\s+psycopg2|from\s+psycopg2', src, re.M):
            kinds.append('psycopg2(PostgreSQL)')
        if re.search(r'^\s*import\s+sqlite3|from\s+sqlite3', src, re.M):
            kinds.append('sqlite3')
        if kinds:
            flag = meta['layer'] in forbidden
            if flag and rel in ex_store:
                flag = False
                ex = ex_store[rel]
                DECLARED.append(dict(kind='存储句柄', item='%s [%s] %s' % (rel, meta['layer'], ','.join(kinds)),
                                     reason=ex.get('reason', ''), expires=ex.get('expires_when', '')))
            hits.append(dict(file=rel, layer=meta['layer'], kinds=kinds, flag=flag))
    return hits


def check_private_surface(layers, files):
    """检查 5：跨件私有面访问（判据 1 · 契约先于实现）。

    判据 1（契约先于实现）说的是「跨层只走契约」。可机械校验的那一半 = **别跨件摸 `_` 名字**：
    某件 import 了另一件，却直接访问对方以 `_` 开头的属性，说明契约没定全、实现漏了出去。

    口径（与旧的正则扫描的关键区别：**只认真实属性访问**）：
      · 只扫层归属表里 `layer != "T"` 的 `.py` 件（HTML/JS 不解析；测试件在 T 层，自然豁免）。
      · 用 ast 建「导入别名 → 模块名」映射，只在 `ast.Attribute` 且 `value` 是 `ast.Name` 时判定 ——
        注释、文档串、字符串里的 `mod._xxx` 提及**一律不算**。
      · `__x__` 形态的 dunder 不算私有面。

    **提示级**：不参与退出码（rc 仍由检查 1 的硬违规决定）。
    阶段3 收官后若噪声仍为 0，即把本检查提为硬门禁。
    """
    reg = {os.path.basename(f) for f in files}
    mod_names = {k[:-3] for k in reg if k.endswith('.py')}
    hits = []
    for rel, meta in files.items():
        if meta.get('layer') == 'T' or not rel.endswith('.py'):
            continue
        src = read_src(rel)
        if not src:
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        alias = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    alias[a.asname or a.name.split('.')[0]] = a.name.split('.')[0]
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    base = node.module.split('.')[0]
                    for a in node.names:
                        alias[a.asname or a.name] = base
        own = os.path.splitext(os.path.basename(rel))[0]
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)):
                continue
            mod = alias.get(node.value.id)
            if not mod or mod not in mod_names or mod == own:
                continue
            if not node.attr.startswith('_'):
                continue
            if node.attr.startswith('__') and node.attr.endswith('__'):
                continue
            hits.append(dict(file=rel, line=node.lineno, alias=node.value.id, attr=node.attr))
    return sorted(hits, key=lambda h: (h['file'], h['line']))


def phases_cfg(layers: dict) -> dict:
    """顶层相配置（v3 E3/裁定①）：hub/node 为**相内**、shared 为**相中立**。

    台账里少写这一节时返回 {}，判据不会静默放行 —— 自检项 ④ 硬断言表里**明文声明**它。
    """
    return (layers.get('rules') or {}).get('phases') or {}


def self_checks(files, pkg_idx, edges, layers, phases):
    """门禁**自身完整性**自检（批① 门禁修复：把「静默失明」变成会红的自检项）。

    任何一项 ✗ ⇒ 退出码 1。宁可判不清，也不给假绿灯。
    """
    res = []

    # ① 包路径回环：每个登记 .py 件都能被自己的完整包路径解析回自己
    bad, n_py = [], 0
    for rel in sorted(files):
        r = rel.replace(os.sep, '/')
        if not r.endswith('.py'):
            continue
        n_py += 1
        if _lookup_pkg(pkg_idx, _dotted_of(r)) != r:
            bad.append(r)
    res.append(dict(ok=not bad, name='① 包路径回环（%d 个 .py 登记件）' % n_py,
                    detail='全部可解析回自己' if not bad else '解析不回自己：%s' % '、'.join(bad[:5])))

    # ② 已搬迁两件（批① 回归锚）：绝对包路径可解析 **且** 入边被统计到。
    #    「边数恢复」不再靠总数拍胸脯：这里逐件断言，并给出行正则（不经 ast）的独立口径交叉核对。
    pins = [('node.l1_transport.nats_client', 'node/l1_transport/nats_client.py'),
            ('hub.l1_relay.gen_nats_conf', 'hub/l1_relay/gen_nats_conf.py'),
            ('node.l0_host.node_paths', 'node/l0_host/node_paths.py'),
            ('node.l0_host.node_profile', 'node/l0_host/node_profile.py')]
    ok2, det2 = True, []
    for dotted, want in pins:
        hit = _lookup_pkg(pkg_idx, dotted)
        inbound = [e for e in edges if e['dep'] == want]
        n_src = len({e['src'] for e in inbound})
        rx_dotted = re.compile(r'^\s*(?:from|import)\s+' + re.escape(dotted) + r'(?=\s|\.|$)', re.M)
        rx_bare = re.compile(r'^\s*(?:from|import)\s+' + re.escape(want.split('/')[-1][:-3]) + r'(?=\s|\.|,|$)', re.M)
        n_dotted = sum(len(rx_dotted.findall(read_src(r))) for r in files)
        n_bare = sum(len(rx_bare.findall(read_src(r))) for r in files)
        det2.append('%s ⇒ %s（%d 条入边 / %d 个源件；独立行正则口径：全点分路径 %d 行 + 裸件名 %d 行，二者可含同一件）'
                    % (dotted, hit, len(inbound), n_src, n_dotted, n_bare))
        if hit != want or not inbound:
            ok2 = False
    res.append(dict(ok=ok2, name='② 已搬迁两件的边可识别（批① 回归锚）', detail='；'.join(det2)))

    # ③ 前缀 / 同名非件负控：包路径前缀与「同名非件」都不得成为件、不得进边
    prefixes = sorted({r.replace(os.sep, '/').split('/')[0] for r in files})
    probes = prefixes + ['node', 'hub', 'shared', 'os.path', 'json.loads', 'os.environ',
                         'datetime.datetime', 'sys.exit', 'typing.Any', 'sqlite3.Row']
    bad3 = [p for p in probes if _lookup_pkg(pkg_idx, p)]
    pref_set = set(prefixes) | {'node', 'hub', 'shared'}
    bad3b = ['%s→%s' % (e['src'], e['dep']) for e in edges
             if os.path.splitext(os.path.basename(e['dep']))[0] in pref_set]
    bad3c = ['%s→%s' % (e['src'], e['dep']) for e in edges if e['dep'] not in files]
    bad3all = bad3 + bad3b + bad3c
    res.append(dict(ok=not bad3all, name='③ 前缀 / 同名非件负控（%d 个探针）' % len(probes),
                    detail='全部解析为空；边内无前缀件、无台账外件' if not bad3all
                    else '误判：%s' % '、'.join(bad3all[:6])))

    # ④ 档位接线：'shared' 在 layer_order 里；phases 明文；登记件的层都已在 layers 里声明
    order = layers['rules']['layer_order']
    bad4 = []
    if 'shared' not in order:
        bad4.append("layer_order 里没有相中立档 'shared'")
    if phases.get('neutral') not in order:
        bad4.append('phases.neutral=%r 不在 layer_order 里（相中立档没接线）' % phases.get('neutral'))
    for k in ('top_level', 'in_phase_dirs', 'neutral'):
        if not phases.get(k):
            bad4.append('台账 rules.phases 缺键 %s' % k)
    unknown = sorted({m['layer'] for m in files.values()} - set(layers.get('layers') or {}))
    if unknown:
        bad4.append('登记件出现 layers 未声明的层：%s' % '、'.join(unknown))
    res.append(dict(ok=not bad4, name='④ 档位接线（shared 入 layer_order + phases 明文）',
                    detail=('layer_order=%s；phases.top_level=%s' % ('/'.join(order), phases.get('top_level')))
                    if not bad4 else '；'.join(bad4)))

    # ⑤ 相判据非空转：合成夹具四例逐一断言（不依赖仓内现在有没有 shared/ 件）+ 档位折算断言
    fixtures = [(('shared', 'hub'), True), (('shared', 'node'), True),
                (('hub', 'shared'), False), (('node', 'shared'), False)]
    bad5 = ['%s→%s 期望 %s 实得 %s' % (sp, dp, want, phase_violation(sp, dp, phases))
            for (sp, dp), want in fixtures if phase_violation(sp, dp, phases) != want]
    if phase_code('shared/l1_transport/x.py', 'L1', phases) != 'shared':
        bad5.append('phase_code 没把 shared/ 件折算成相中立档')
    res.append(dict(ok=not bad5, name='⑤ shared 相判据非空转（合成夹具 4 例 + 档位折算）',
                    detail='4/4 期望命中、shared/ 件折算为相中立档' if not bad5 else '；'.join(bad5)))

    # ⑥ 解析无歧义、无 ast 兜底（有就是静默降级的苗头，必须红）
    res.append(dict(ok=not AMBIGUOUS and not PARSED_BAD, name='⑥ 解析无歧义、无 ast 兜底',
                    detail=('0 条歧义 / 0 件退回正则' if not (AMBIGUOUS or PARSED_BAD)
                            else '歧义 %d 条、退回正则 %d 件：%s'
                                 % (len(AMBIGUOUS), len(PARSED_BAD),
                                    '、'.join(list(AMBIGUOUS)[:3] + PARSED_BAD[:3])))))

    # ⑦ 边内无重复（同一 src→dep 只算一条 —— 保证边数与「唯一 import 关系」同口径）
    seen, dup = set(), []
    for e in edges:
        k = (e['src'], e['dep'])
        if k in seen:
            dup.append('%s→%s' % k)
        seen.add(k)
    res.append(dict(ok=not dup, name='⑦ 边无重复（同 src→dep 一条）',
                    detail=('%d 条边，无重复' % len(edges)) if not dup else '重复：%s' % '、'.join(dup[:5])))
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--only', default='')
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--dump-edges', action='store_true',
                    help='纯文本逐行打印边表（源件:行 -> 依赖件[层→层]），供与 git grep 实测逐条对账')
    args = ap.parse_args()
    only = {x.strip() for x in args.only.split(',') if x.strip()}

    layers = load_map()
    files = layers['files']
    pkg_idx = build_pkg_index(files)      # 点分包路径 → 台账登记件（含各级后缀；见 build_pkg_index）
    phases = phases_cfg(layers)           # 顶层相（hub/node 相内、shared 相中立；v3 E3）

    v1, edges = check_direction(layers, files, pkg_idx, phases) if (not only or '1' in only) else ([], [])
    v6, pstats = check_phases(layers, files, edges) if edges else ([], {})
    selfs = self_checks(files, pkg_idx, edges, layers, phases) if edges else []
    w2 = check_transport_words(layers, files) if (not only or '2' in only) else []
    w3 = check_hardcode(layers, files) if (not only or '3' in only) else []
    w4 = check_storage(layers, files) if (not only or '4' in only) else []
    w5 = check_private_surface(layers, files) if (not only or '5' in only) else []

    # 源码树守卫：在对外仓（HACS）里跑本脚本时，层归属表在、源文件不在 ——
    # 那样「件数 53 / 边 0」看着像通过，其实是没料可查。宁可判不清也不给假绿灯。
    if len(MISSING) > len(files) // 10:
        print('✗ 只找到 %d/%d 个源文件（缺 %d 个），当前目录不是项目源码树。'
              % (len(files) - len(MISSING), len(files), len(MISSING)))
        print('  本门禁只对项目源码树（含 agent-team/）有效；对外仓内这两件是只读参考件。')
        print('  例如缺：%s' % '、'.join(sorted(MISSING)[:3]))
        return 2

    host_paths = [dict(file=h['file'], layer=h['layer'], count=h['path_count'],
                       samples=[p[2] for p in h['paths'][:2]]) for h in w3 if h['path_count']]
    net_literals = [dict(file=h['file'], layer=h['layer'], count=h['ip_count']) for h in w3 if h['ip_count']]
    owners = host_path_owners(layers)
    # 「要收」= 非所有者件里的宿主路径字面量（known-FP 已剔除，但已逐条登记在案并照常打印）
    to_fix = [dict(file=h['file'], layer=h['layer'], line=i, match=v, text=s)
              for h in w3 for (i, v, s) in h['paths']]
    known_fp = [dict(file=h['file'], layer=h['layer'], line=i, match=v, text=s, reason=why)
                for h in w3 for (i, v, s, why) in h['fps']]
    rc = 1 if (v1 or v6 or any(not s['ok'] for s in selfs)) else 0
    report = dict(direction_violations=v1, phase_violations=v6, phase_stats=pstats,
                  self_checks=selfs,
                  transport_business_hits=w2,
                  hardcode=w3, host_paths=host_paths, net_literals=net_literals,
                  host_path_owner=owners,
                  host_paths_to_fix=to_fix, path_known_false_positives=known_fp,
                  storage=w4, private_surface=w5,
                  edges=len(edges), edge_list=edges, files=len(files),
                  declared_exceptions=DECLARED, rc=rc)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return rc

    print('=' * 78)
    print('分层门禁 · 台账（件数 %d，层间 import 边 %d）' % (len(files), len(edges)))
    print('=' * 78)

    print('\n检查 1 · 依赖方向（硬门禁）')
    if not v1:
        print('  ✓ 无反向依赖：现有 import 图与五层方向一致')
    for d in v1:
        print('  ✗ %s [%s] → %s [%s]：%s' % (d['src'], d['src_layer'], d['dep'], d['dep_layer'], d['why']))

    print('\n检查 2 · L1 通信层里的业务词（提示级）')
    if not w2:
        print('  ✓ 无命中')
    for h in w2:
        print('  · %s:%d 命中「%s」  %s' % (h['file'], h['line'], h['word'], h['text']))

    print('\n检查 3 · 宿主硬编码（提示级）')
    if not w3:
        print('  ✓ 无命中')
    for h in sorted(w3, key=lambda x: -(x['ip_count'] + x['path_count'] + x['fp_count'])):
        print('  · %-34s [%s] IP×%-3d 路径×%-3d known-FP×%-3d'
              % (h['file'], h['layer'], h['ip_count'], h['path_count'], h['fp_count']))
        for i, v, s in h['ips'][:3]:
            print('        L%-5d IP    %s   | %s' % (i, v, s))
        for i, v, s in h['paths'][:3]:
            print('        L%-5d 路径  %s   | %s' % (i, v, s))
        for i, v, s, _why in h['fps'][:3]:
            print('        L%-5d 路径  %s   | %s   [known-FP]' % (i, v, s))

    print('\n  宿主路径字面量的唯一合法去处 host_path_owner（%d 件，其余件出现即「要收」）：' % len(owners))
    for o in owners:
        print('      · %s' % o)
    if to_fix:
        print('  ✗ 要收：%d 处宿主路径字面量落在**非所有者**件里' % len(to_fix))
        for d in to_fix:
            print('      ✗ %s:%d [%s]  命中 %s' % (d['file'], d['line'], d['layer'], d['match']))
            print('           原文：%s' % d['text'])
    else:
        print('  ✓ 要收 0 处：L1–L4 里未登记的宿主路径字面量为 0')
    if known_fp:
        print('  🛈 known-FP %d 处（PATH_RX 把正则串误判成盘符路径；**检查保留**，只是不计进「要收」）：'
              % len(known_fp))
        for d in known_fp:
            print('      🛈 [known-FP] %s:%d [%s]  命中 %s' % (d['file'], d['line'], d['layer'], d['match']))
            print('           原文：%s' % d['text'])
            print('           理由：%s' % d['reason'])

    print('\n检查 4 · 直接持存储句柄的件（提示级；打 ⚠ 的是「不该持」的层）')
    for h in w4:
        print('  %s %-34s [%s] %s' % ('⚠' if h['flag'] else '·', h['file'], h['layer'], ','.join(h['kinds'])))

    print('\n检查 5 · 跨件私有面访问（提示级）')
    if not w5:
        print('  ✓ 无跨件私有面访问（0 处）')
    for h in w5:
        print('  · %s:%d  %s.%s' % (h['file'], h['line'], h['alias'], h['attr']))

    print('\n检查 6 · 相方向（硬门禁；v3 E3）—— 相内件（hub/、node/）可向 shared/ 单向 import；shared/ 不得 import 相内件')
    if not v6:
        print('  ✓ 无相方向违规（相内件之间 %d 条、相内→相中立 %d 条、相中立内部 %d 条、相中立→相内 %d 条）'
              % (pstats.get('in_phase_pairs', 0), pstats.get('in_phase_to_neutral', 0),
                 pstats.get('neutral_internal', 0), pstats.get('neutral_to_in_phase', 0)))
        if not pstats.get('in_phase_to_neutral', 0) and not pstats.get('neutral_internal', 0):
            print('      （当前仓内还没有 shared/ 件 —— 判据本体会由下面「自检 ⑤」的合成夹具证明**非空转**）')
    for d in v6:
        print('  ✗ %s [%s] → %s [%s]：%s' % (d['src'], d['src_phase'], d['dep'], d['dep_phase'], d['why']))

    print('\n自检 · 门禁自身完整性（批① 门禁修复；任一项 ✗ ⇒ 退出码 1）')
    for s in selfs:
        print('  %s %s' % ('✓' if s['ok'] else '✗', s['name']))
        print('      %s' % s['detail'])
    if not selfs:
        print('  （子集模式：未计算边，自检跳过 —— 全量跑才有意义）')

    if DECLARED:
        print('\n已声明的例外（不判违规；每条都带理由与到期条件，登记在 tools/hacs_layers.json）')
        for dd in DECLARED:
            print('  🛈 [%s] %s' % (dd['kind'], dd['item']))
            print('      理由：%s' % dd['reason'])
            print('      到期：%s' % dd['expires'])

    if args.dump_edges:
        print('\n边表（%d 条；`源件:行 -> 依赖件[源层→目标层]`，供与 git grep 实测逐条对账）' % len(edges))
        for e in sorted(edges, key=lambda x: (x['src'], x['line'], x['dep'])):
            print('  %s:%d -> %s [%s→%s]'
                  % (e['src'], e['line'], e['dep'], e['src_layer'], e['dep_layer']))

    print('\n判据对照：检查1↔判据2 · 检查2↔判据5 · 检查3↔判据4 · 检查4↔判据1 · 检查5↔判据1 · 检查6↔裁定①/v3 E3')
    print('退出码：%d（检查1 方向违规 %d · 检查6 相方向违规 %d · 自检失败 %d）'
          % (rc, len(v1), len(v6), sum(1 for s in selfs if not s['ok'])))
    return rc


if __name__ == '__main__':
    sys.exit(main())
