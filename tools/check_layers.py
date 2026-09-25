#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_layers.py —— 分层门禁（阶段 0 产出）

四条检查，对应 docs/HACS-分层规划-handoff.html 的七条解耦判据：

  检查 1  依赖方向（判据 2）：只允许上层依赖下层；同层可互依；横切 X 可被任意层引用。
  检查 2  传输不含语义（判据 5）：L1 通行层的代码里不该出现业务词。
  检查 3  配置零硬编码（判据 4）：运行件里的 IP 字面量 / 绝对路径常量。
  检查 4  存储句柄（判据 1 的前置）：哪些件直接持数据库 / 文件句柄（持句柄的件必须在自己层内）。

用法：
  python tools/check_layers.py                # 全量报告
  python tools/check_layers.py --only 1,2     # 只看某几条
  python tools/check_layers.py --json         # 机器可读

退出码：0 = 无「硬违规」；1 = 有硬违规（检查 1 的方向违规）。
检查 2/3/4 默认是「提示级」——它们的命中需要人判断，不直接判死（见台账第八节）。
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)
MAP_PATH = os.path.join(HERE, 'hacs_layers.json')
MISSING = set()       # 读不到的源文件（用于「源码树守卫」；read_src 会被多次调用，故用集合去重）


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


def check_direction(layers, files, index):
    """检查 1：依赖方向。返回 (violations, edges)"""
    order = layers['rules']['layer_order']           # ['L4','L3','L2','L1','L0']
    rank = {L: i for i, L in enumerate(order)}       # 越小越靠上
    cross_ok = set(layers['rules']['allowed_import']['cross_allowed'])
    exempt = set(layers['rules']['allowed_import']['exempt_src_layers'])
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
PATH_RX = re.compile(r'(?:[A-Za-z]:[\\/][^\s"\')]{3,}|/(?:opt|etc|var|home|root)/[^\s"\')]{3,})')


def check_hardcode(layers, files):
    run_layers = {'L1', 'L2', 'L3', 'L4'}
    hits = []
    for rel, meta in files.items():
        if meta['layer'] not in run_layers:
            continue
        src = read_src(rel)
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
        if ips or paths:
            hits.append(dict(file=rel, layer=meta['layer'], ips=ips[:6], ip_count=len(ips),
                             paths=paths[:4], path_count=len(paths)))
    return hits


def check_storage(layers, files):
    forbidden = set(layers['rules']['storage_handle_forbidden_layers'])
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
            hits.append(dict(file=rel, layer=meta['layer'],
                             kinds=kinds,
                             flag=meta['layer'] in forbidden))
    return hits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--only', default='')
    ap.add_argument('--json', action='store_true')
    args = ap.parse_args()
    only = {x.strip() for x in args.only.split(',') if x.strip()}

    layers = load_map()
    files = layers['files']
    index = {os.path.splitext(k.split('/')[-1])[0]: v['layer'] for k, v in files.items()}

    v1, edges = check_direction(layers, files, index) if (not only or '1' in only) else ([], [])
    w2 = check_transport_words(layers, files) if (not only or '2' in only) else []
    w3 = check_hardcode(layers, files) if (not only or '3' in only) else []
    w4 = check_storage(layers, files) if (not only or '4' in only) else []

    # 源码树守卫：在对外仓（HACS）里跑本脚本时，层归属表在、源文件不在 ——
    # 那样「件数 53 / 边 0」看着像通过，其实是没料可查。宁可判不清也不给假绿灯。
    if len(MISSING) > len(files) // 10:
        print('✗ 只找到 %d/%d 个源文件（缺 %d 个），当前目录不是项目源码树。'
              % (len(files) - len(MISSING), len(files), len(MISSING)))
        print('  本门禁只对项目源码树（含 agent-team/）有效；对外仓内这两件是只读参考件。')
        print('  例如缺：%s' % '、'.join(sorted(MISSING)[:3]))
        return 2

    report = dict(direction_violations=v1, transport_business_hits=w2,
                  hardcode=w3, storage=w4, edges=len(edges), files=len(files))
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

    print('\n判据对照：检查1↔判据2 · 检查2↔判据5 · 检查3↔判据4 · 检查4↔判据1')
    return 1 if v1 else 0


if __name__ == '__main__':
    sys.exit(main())
