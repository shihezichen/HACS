#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_layers_reconcile.py —— 门禁边表 ↔ git grep 实测 import 关系的**独立对账**（批① 门禁修复批，T 面）

为什么需要它：批① 搬迁后门禁的边数从 97 掉到 84（`local_imports()` 只抓 import 的第一个标识符，
`from node.l1_transport.nats_client import …` 被解析成前缀 `node`、整条边静默蒸发）。修完之后
**只看一个总数是不够的** —— 本脚本把门禁输出的边长逐条与「实测的 import 关系」对账。

两路互相独立：
  · 门禁侧：跑 `tools/check_layers.py --json`，取 `edge_list`（ast 解析 + 包路径 → 台账登记件映射）。
  · grep 侧：**不 import 门禁的任何解析函数**。① `git grep -n` 实测全仓 import 行（行级证据），
    ② 行级正则（非 ast）抽出模块名与 from-import 名，③ 按**台账件名**在自己的小表里解析成登记路径
    （裸件名精确匹配 / 全点分路径整段等于登记路径的点分后缀）。
两路各得一组 (源件, 依赖件)，diff 出「只在门禁侧 / 只在 grep 侧」；**两边都空 = 对得上（PASS）**，
任一边非空 = FAIL 并逐条打印，附解释。

对账口径（与门禁同）：只算**台账登记件之间**的 import 关系；未登记源件、标准库、第三方不进表。

用法：
  python tools/check_layers_reconcile.py                 # 默认仓库 = 本脚本上一级，默认产物写 ../（见 --out）
  python tools/check_layers_reconcile.py --out <目录>     # Checker 可指定目录复跑复看
  python tools/check_layers_reconcile.py --repo D:/Work/HACS
产物（供独立复核）：
  <out>/edge_list_gate.json   门禁侧边表（含源件/行号/依赖件/层）
  <out>/edge_list_grep.json   grep 侧边长（含源件/行号/原文/解析出的依赖件）
  <out>/reconcile.txt         本脚本完整输出
退出码：0 = 两栏皆空（对得上）；1 = 有差异；2 = 跑不起来（git / 门禁 / 台账不可用）。
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

#: 实测 import 行的**行级**捕获（注意：刻意不用 ast，免得与门禁同错）
#: git grep 用的是 POSIX ERE，**不认 `\s`**（会当成字面 s）—— 这里必须写 `[[:space:]]`；
#: 之前写成 `\s` 时 grep 侧只扫到 65 行、漏 32 条边，正是「两路都哑」的翻版。
GREP_PAT = r'^[[:space:]]*(from|import)[[:space:]]+[A-Za-z_][A-Za-z0-9_.]*'
M_FROM = re.compile(r'^\s*from\s+([\w.]+)\s+import\b(.*)$')
M_IMP = re.compile(r'^\s*import\s+(.*)$')
M_IDENT = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')


def dotted_of(rel: str) -> str:
    parts = rel.replace(os.sep, '/').split('/')
    if not parts[-1].endswith('.py'):
        return '.'.join(parts)
    stem = parts[-1][:-3]
    return '.'.join(parts[:-1]) if stem == '__init__' else '.'.join(parts[:-1] + [stem])


def load_ledger(repo: str):
    p = os.path.join(repo, 'tools', 'hacs_layers.json')
    with io.open(p, encoding='utf-8') as fh:
        d = json.load(fh)
    files = d['files']
    bare = {}
    dotted = {}
    for rel in files:
        r = rel.replace(os.sep, '/')
        bare[os.path.splitext(r.split('/')[-1])[0]] = r
        dotted[dotted_of(r)] = r
    return files, bare, dotted


def gate_edges(repo: str):
    """门禁侧：跑 check_layers.py --json，取 edge_list（含件数/边数表头同一口径）。"""
    script = os.path.join(repo, 'tools', 'check_layers.py')
    proc = subprocess.run([sys.executable, script, '--json'], cwd=repo,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    txt = proc.stdout.decode('utf-8', 'replace')
    i = txt.find('{')
    if i < 0:
        raise RuntimeError('门禁 --json 没有输出 JSON（rc=%d）：%s' % (proc.returncode, txt[:300]))
    rep = json.loads(txt[i:])
    return rep, {(e['src'], e['dep']) for e in rep['edge_list']}


def grep_lines(repo: str):
    proc = subprocess.run(['git', 'grep', '-nE', GREP_PAT], cwd=repo,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode not in (0, 1):
        raise RuntimeError('git grep 失败（rc=%d）：%s'
                           % (proc.returncode, proc.stderr.decode('utf-8', 'replace')[:300]))
    out = []
    for ln in proc.stdout.decode('utf-8', 'replace').splitlines():
        # 形态：<path>:<行号>:<原文>
        m = re.match(r'^(.+?):(\d+):(.*)$', ln)
        if m:
            out.append((m.group(1), int(m.group(2)), m.group(3)))
    return out


def resolve_strict(cand: str, bare: dict, dotted: dict):
    """grep 侧自己的解析（语义与门禁一致，实现独立）：裸名精确匹配；点分路径整段等于登记路径后缀。"""
    if not cand:
        return None
    if '.' not in cand:
        return bare.get(cand)
    hit = dotted.get(cand)
    if hit:
        return hit
    for d, rel in dotted.items():
        if d.endswith('.' + cand):
            return rel
    return None


def resolve_permissive(cand: str, bare: dict):
    """宽松口径（仅用于旁证，不计入 PASS）：只认末段件名 —— 用来把「可能被漏掉的引用」引出来看。"""
    return bare.get(cand.rsplit('.', 1)[-1]) if cand else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--repo', default=ROOT)
    ap.add_argument('--out', default=os.path.join(os.path.dirname(ROOT), 'hacs_gate_reconcile'))
    args = ap.parse_args()
    repo = os.path.abspath(args.repo)
    out = os.path.abspath(args.out)
    lines = []

    def say(s=''):
        lines.append(s)
        print(s)

    files, bare, dotted = load_ledger(repo)
    try:
        rep, gate = gate_edges(repo)
    except RuntimeError as e:
        say('✗ ' + str(e))
        return 2

    # ---- grep 侧 ----
    grep_pairs = {}          # (src, dep) -> (line, text)
    permissive_pairs = {}
    n_probe = 0
    registered = set(r.replace(os.sep, '/') for r in files)
    for path, lineno, text in grep_lines(repo):
        src = path.replace(os.sep, '/')
        if src not in registered:
            continue                       # 与门禁同口径：源件必须是登记件
        n_probe += 1
        body = text.split('#')[0].rstrip()   # 行尾注释要切掉：否则注释里的词会被当成 import 名单
        cands = []
        mf = M_FROM.match(body)
        if mf:
            base, rest = mf.group(1), mf.group(2)
            cands.append(base)
            for n in M_IDENT.findall(rest):
                if n not in ('import', 'as'):
                    cands.append(base + '.' + n)
        else:
            mi = M_IMP.match(body)
            if mi:
                for piece in mi.group(1).split(','):
                    tok = re.match(r'\s*([\w.]+)', piece)     # `import a.b.c as x` → a.b.c
                    if tok:
                        cands.append(tok.group(1))
        for c in cands:
            for tag, res in (('strict', resolve_strict(c, bare, dotted)),
                             ('loose', resolve_permissive(c, bare))):
                if not res or res == src:
                    continue
                if tag == 'strict':
                    grep_pairs.setdefault((src, res), (lineno, text))
                else:
                    permissive_pairs.setdefault((src, res), (lineno, text))

    grep_side = set(grep_pairs)

    # ---- 对账 ----
    only_gate = sorted(gate - grep_side)
    only_grep = sorted(grep_side - gate)
    loose_only = sorted(set(permissive_pairs) - gate - grep_side)

    say('=' * 78)
    say('门禁边表 ↔ git grep 独立对账（批① 门禁修复批）')
    say('=' * 78)
    say('仓库            : %s' % repo)
    say('登记件 / 门禁边  : %d 件 / %d 条（check_layers.py 实时打印同一口径）'
        % (len(files), len(gate)))
    say('grep 实测扫描行  : %d 行（登记件源文件里的 import 行，git grep -nE 行级证据）' % n_probe)
    say('grep 侧解析出边长: %d 条' % len(grep_side))
    say('')
    say('① 只在门禁侧（%d 条）—— 门禁认了、grep 实测没认出来的：' % len(only_gate))
    for s, d in only_gate:
        say('   · %s -> %s' % (s, d))
    if not only_gate:
        say('   （空）')
    say('② 只在 grep 侧（%d 条）—— grep 实测有、门禁没认出来的（**旧 bug 的形态就在这里显形**）：'
        % len(only_grep))
    for s, d in only_grep:
        ln, tx = grep_pairs[(s, d)]
        say('   · %s -> %s   [%s:%d] %s' % (s, d, s, ln, tx.strip()[:96]))
    if not only_grep:
        say('   （空）')

    verdict = 'PASS（两边皆空，对得上）' if not (only_gate or only_grep) else 'FAIL（有差异，见上）'
    say('')
    say('判定：%s' % verdict)
    say('')
    say('旁证 · 宽松口径增量（%d 条；**不计入 PASS**）：只按「末段件名」匹配时多出来的对子，'
        '说明它们是 `from pkg import <属性名>` 里恰好与某个件同名的**属性**，不是 import 关系 ——'
        ' 门禁与本脚本的严格口径都正确地不算边。' % len(loose_only))
    for s, d in loose_only[:20]:
        ln, tx = permissive_pairs[(s, d)]
        say('   🛈 %s -> %s   [%s:%d] %s' % (s, d, s, ln, tx.strip()[:96]))
    if len(loose_only) > 20:
        say('   … 余 %d 条见 edge_list_grep.json' % (len(loose_only) - 20))

    # ---- 产物 ----
    if not os.path.isdir(out):
        os.makedirs(out)
    with io.open(os.path.join(out, 'edge_list_gate.json'), 'w', encoding='utf-8') as fh:
        json.dump(dict(files=rep['files'], edges=rep['edges'], rc=rep['rc'],
                       edge_list=rep['edge_list']), fh, ensure_ascii=False, indent=1)
    with io.open(os.path.join(out, 'edge_list_grep.json'), 'w', encoding='utf-8') as fh:
        json.dump(dict(probe_lines=n_probe, edges=len(grep_side),
                       edge_list=[dict(src=s, line=grep_pairs[(s, d)][0], dep=d,
                                       text=grep_pairs[(s, d)][1].strip())
                                  for (s, d) in sorted(grep_side)],
                       loose_only=[dict(src=s, line=permissive_pairs[(s, d)][0], dep=d,
                                        text=permissive_pairs[(s, d)][1].strip())
                                   for (s, d) in loose_only]),
                  fh, ensure_ascii=False, indent=1)
    with io.open(os.path.join(out, 'reconcile.txt'), 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(lines) + '\n')
    say('')
    say('产物：%s' % out)
    say('   · edge_list_gate.json（门禁侧 %d 条）· edge_list_grep.json（grep 侧 %d 条 + 宽松增量）'
        ' · reconcile.txt（本输出）' % (len(gate), len(grep_side)))
    return 0 if not (only_gate or only_grep) else 1


if __name__ == '__main__':
    sys.exit(main())
