#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_layers.py —— 分层门禁（阶段 0 产出）

五条检查，对应 docs/HACS-分层规划-handoff.html 的七条解耦判据：

  检查 1  依赖方向（判据 2）：只允许上层依赖下层；同层可互依；横切 X 可被任意层引用。
  检查 2  传输不含语义（判据 5）：L1 通行层的代码里不该出现业务词。
  检查 3  配置零硬编码（判据 4）：运行件里的 IP 字面量 / 绝对路径常量。
  检查 4  存储句柄（判据 1 的前置）：哪些件直接持数据库 / 文件句柄（持句柄的件必须在自己层内）。
  检查 5  跨件私有面访问（判据 1）：契约先于实现 —— 跨层只走公开契约，禁跨件摸 `_` 私有面。

用法：
  python tools/check_layers.py                # 全量报告
  python tools/check_layers.py --only 1,2     # 只看某几条
  python tools/check_layers.py --json         # 机器可读

退出码：0 = 无「硬违规」；1 = 有硬违规（检查 1 的方向违规）。
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


def load_map():
    with io.open(MAP_PATH, encoding='utf-8') as fh:
        return json.load(fh)


def read_src(rel: str) -> str:
    p = os.path.join(PROJECT_ROOT, rel.replace('/', os.sep))
    if not os.path.isfile(p):
        MISSING.add(rel)
        return ''
    return io.open(p, encoding='utf-8', errors='replace').read()


def local_imports(rel: str, src: str, all_names: set) -> list:
    me = os.path.splitext(os.path.basename(rel))[0]
    found = re.findall(r'^\s*(?:import|from)\s+([A-Za-z_][A-Za-z0-9_]*)', src, re.M)
    return sorted({x for x in found if x in all_names and x != me})


def check_direction(layers, files, index, name2rel=None):
    """检查 1：依赖方向。返回 (violations, edges)"""
    order = layers['rules']['layer_order']           # ['L4','L3','L2','L1','L0']
    rank = {L: i for i, L in enumerate(order)}       # 越小越靠上
    cross_ok = set(layers['rules']['allowed_import']['cross_allowed'])
    exempt = set(layers['rules']['allowed_import']['exempt_src_layers'])
    ex_imp = {(e['src'], e['dst']): e
              for e in layers['rules']['allowed_import'].get('exceptions', [])}
    name2rel = name2rel or {}
    v, edges = [], []
    for rel, meta in files.items():
        src = read_src(rel)
        if not src:
            continue
        sl = meta['layer']
        for dep in local_imports(rel, src, set(index)):
            dl = index[dep]
            edges.append((rel, sl, dep, dl))
            if sl in exempt or dl in cross_ok or sl in cross_ok:
                continue
            if sl == dl:
                continue
            # 允许：上层依赖下层（rank 大的依赖 rank 小的？注意 rank 定义）
            if sl in rank and dl in rank:
                ok = rank[sl] <= rank[dl]
            else:
                ok = True
            if not ok:
                dst_rel = dep if dep in files else name2rel.get(dep, dep)
                ex = ex_imp.get((rel, dst_rel)) or ex_imp.get((rel, dep))
                if ex:
                    DECLARED.append(dict(kind='依赖方向', item='%s [%s] → %s [%s]' % (rel, sl, dep, dl),
                                         reason=ex.get('reason', ''), expires=ex.get('expires_when', '')))
                    continue
                v.append(dict(src=rel, src_layer=sl, dep=dep, dep_layer=dl,
                              why='下层件反向依赖了上层（%s → %s）' % (sl, dl)))
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


def check_hardcode(layers, files):
    """宿主字面量检查，**分两类**（只有一类是真该收的）：

      · `host_paths` —— 宿主路径字面量（真硬编码）。唯一合法去处 = 层归属表里声明的
        `host_path_owner`（当前 = L0 `agent-team/node_paths.py`）；其余件出现即「要收」，
        这类数字应当单调趋 0，是「配置零硬编码 / 跨 OS 解耦」的可机械校验进度指标。
      · `net_literals` —— IP / 端口默认值（提示级）。大量是**合法的**默认（loopback、
        兜底基址），强行收走反而降低可读性，故只提示不设门禁。

    文档字符串与注释里的示例地址一律不计（`_strip_docstrings`）。
    """
    run_layers = {'L1', 'L2', 'L3', 'L4'}
    owner = (layers.get('rules') or {}).get('host_path_owner') or ''
    hits = []
    for rel, meta in files.items():
        if meta['layer'] not in run_layers:
            continue
        src = _strip_docstrings(read_src(rel))
        ips, paths = [], []
        for i, line in enumerate(src.splitlines(), 1):
            s = line.strip()
            if s.startswith('#'):
                continue
            for m in IP_RX.finditer(line):
                if m.group(0).startswith('0.') or m.group(0) in ('0.0.0.0',):
                    continue
                ips.append((i, m.group(0), s[:70]))
            for m in PATH_RX.finditer(line):
                paths.append((i, m.group(0), s[:70]))
        if owner and rel.replace('\\', '/').endswith(owner):
            paths = []                     # 合法去处：宿主默认值的唯一出处，不计数
        if ips or paths:
            hits.append(dict(file=rel, layer=meta['layer'], ips=ips[:6], ip_count=len(ips),
                             paths=paths[:4], path_count=len(paths)))
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--only', default='')
    ap.add_argument('--json', action='store_true')
    args = ap.parse_args()
    only = {x.strip() for x in args.only.split(',') if x.strip()}

    layers = load_map()
    files = layers['files']
    index = {os.path.splitext(k.split('/')[-1])[0]: v['layer'] for k, v in files.items()}
    name2rel = {os.path.splitext(k.split('/')[-1])[0]: k for k in files}

    v1, edges = check_direction(layers, files, index, name2rel) if (not only or '1' in only) else ([], [])
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
    report = dict(direction_violations=v1, transport_business_hits=w2,
                  hardcode=w3, host_paths=host_paths, net_literals=net_literals,
                  host_path_owner=(layers.get('rules') or {}).get('host_path_owner') or '',
                  storage=w4, private_surface=w5,
                  edges=len(edges), files=len(files), declared_exceptions=DECLARED)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 1 if v1 else 0

    print('=' * 78)
    print('分层门禁 · 台账（件数 %d，层间 import 边 %d）' % (len(files), len(edges)))
    print('=' * 78)

    print('\n检查 1 · 依赖方向（硬门禁）')
    if not v1:
        print('  ✓ 无反向依赖：现有 import 图与五层方向一致')
    for d in v1:
        print('  ✗ %s [%s] → %s [%s]：%s' % (d['src'], d['src_layer'], d['dep'], d['dep_layer'], d['why']))

    print('\n检查 2 · L1 通行层里的业务词（提示级）')
    if not w2:
        print('  ✓ 无命中')
    for h in w2:
        print('  · %s:%d 命中「%s」  %s' % (h['file'], h['line'], h['word'], h['text']))

    print('\n检查 3 · 宿主硬编码（提示级）')
    if not w3:
        print('  ✓ 无命中')
    for h in sorted(w3, key=lambda x: -(x['ip_count'] + x['path_count'])):
        print('  · %-34s [%s] IP×%-3d 路径×%-3d' % (h['file'], h['layer'], h['ip_count'], h['path_count']))
        for i, v, s in h['ips'][:3]:
            print('        L%-5d IP    %s   | %s' % (i, v, s))
        for i, v, s in h['paths'][:3]:
            print('        L%-5d 路径  %s   | %s' % (i, v, s))

    print('\n检查 4 · 直接持存储句柄的件（提示级；打 ⚠ 的是「不该持」的层）')
    for h in w4:
        print('  %s %-34s [%s] %s' % ('⚠' if h['flag'] else '·', h['file'], h['layer'], ','.join(h['kinds'])))

    print('\n检查 5 · 跨件私有面访问（提示级）')
    if not w5:
        print('  ✓ 无跨件私有面访问（0 处）')
    for h in w5:
        print('  · %s:%d  %s.%s' % (h['file'], h['line'], h['alias'], h['attr']))

    if DECLARED:
        print('\n已声明的例外（不判违规；每条都带理由与到期条件，登记在 tools/hacs_layers.json）')
        for dd in DECLARED:
            print('  🛈 [%s] %s' % (dd['kind'], dd['item']))
            print('      理由：%s' % dd['reason'])
            print('      到期：%s' % dd['expires'])

    print('\n判据对照：检查1↔判据2 · 检查2↔判据5 · 检查3↔判据4 · 检查4↔判据1 · 检查5↔判据1')
    return 1 if v1 else 0


if __name__ == '__main__':
    sys.exit(main())
