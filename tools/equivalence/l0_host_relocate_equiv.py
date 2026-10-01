#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批⑤a 等价装置：L0 两件归位 `node/l0_host/` 之后的**值对账 + 单实例 + 引用面可解析**。

本刀把 `backend/node_paths.py`、`backend/node_profile.py` 搬进 `node/l0_host/`，并把全仓引用从
裸名改成绝对包名。判定三件事：

① **值对账**：改前（`git show <rev>:backend/...`，在临时目录按原名复刻）与改后
   （`node.l0_host.*`）逐项比对两件 `__all__` 里**全部无参可调用成员**与公开常量的 repr。
   判据：逐项一致（`<需参>` / `<err>` 项两侧同名同态即跳过，单独计数并打印）。
② **单实例**（本刀新裁定的不变量）：`node.l0_host.node_paths is node.l0_host.node_profile._paths()`，
   且**加载旧侧之前** `sys.modules` 里没有任何顶层 `node_paths` / `node_profile` 副本 ——
   裸名 import 会另起一个值相同却不同一的副本，破坏 node_paths 文档串声明的「同一对象不是拷贝」。
③ **引用面可解析**：全仓（除 `_old/` 冻结档）凡 `from node.l0_host import ...` 的件，
   其源码里必须同时有把**仓根**放进 `sys.path` 的注入段（AST 查 `sys.path.insert` 实参树含
   `dirname(__file__)` 两跳以上），否则该件运行期解析不了绝对包名 —— 逐件列出，可疑件单列。

用法：
    PYTHONUTF8=1 python tools/equivalence/l0_host_relocate_equiv.py            # rc=0 全绿
    PYTHONUTF8=1 python tools/equivalence/l0_host_relocate_equiv.py --negative # 旧侧注毒 1 字符 → 必判红
    PYTHONUTF8=1 python tools/equivalence/l0_host_relocate_equiv.py --verbose
"""
from __future__ import annotations

import argparse
import ast
import inspect
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REV = "863ffd3"                     # 本刀基线（两件尚在 backend/ 的最后一个 commit）
OLD = [("backend/node_paths.py", "node_paths.py"),
       ("backend/node_profile.py", "node_profile.py")]
TMP = os.path.join(os.environ.get("TMPDIR") or tempfile.gettempdir(), "l0_old_batch5a")
SKIP_DIR_PARTS = ("_old", "__pycache__", ".git")
POISON_FIRST = "runtime_dir"        # node_paths.__all__[0]：负控注毒时在旧侧源码末尾覆盖它


def base_src(rel: str, poison: bool = False) -> str:
    out = subprocess.run(["git", "show", "%s:%s" % (REV, rel)], cwd=ROOT,
                         capture_output=True, check=True).stdout.decode("utf-8")
    if poison and rel.endswith("node_paths.py"):
        # 注毒 = 末尾追加一个覆盖定义（改值不改语义），不依赖源码里恰好有某个字面量
        out += ("\n\ndef %s(*_a, **_k):\n"
                "    return '<poisoned for negative control>'\n" % POISON_FIRST)
    return out


def materialize_old(poison: bool) -> str:
    os.makedirs(TMP, exist_ok=True)
    for rel, name in OLD:
        with open(os.path.join(TMP, name), "w", encoding="utf-8") as f:
            f.write(base_src(rel, poison))
    return TMP



#: 批⑦「方案 a」占位符的形态：`<deploy-dir>` 一类（**必须**是这种带尖括号的小写短标识，
#: 否则负控注毒值 `'<poisoned for negative control>'` 也会被误免判 ⇒ 负控失效）。
_PLACEHOLDER_RE = re.compile(r"^<[a-z][a-z0-9_-]*>")
MAX_GRANTED = 16         #: 免判上限：防「已授权面」被悄悄扩大，超限即判红


def _cell_value(text):
    """`cell_str` 形态（`KIND/tname/text`）取第三段；已是纯值则原样返回。"""
    s = str(text)
    head, sep, rest = s.partition("/")
    if sep and head in ("VALUE", "RAISE"):
        return rest.partition("/")[2]
    return s


def _scalars(text):
    """把值拆成「标量片段」表：先剥 `cell_str` 壳，再剥 list/tuple 壳，**取全部非空片段**。

    只取标量会漏掉 `['<deploy-dir>']` 这类 list 形态的格（谓词会静默失效）；只取首个元素
    又会漏掉 `['', '', '', '<deploy-dir>\\client-token']` 这类前导空元素的格。
    标量输入返回单元素表。
    """
    s = _cell_value(text).strip()
    if s[:1] in "[(" and s[-1:] in ")]":
        parts = [p.strip() for p in s[1:-1].split(",")]
    else:
        parts = [s]
    return [p for p in parts if p.strip().strip("'\"")]


def plan_a_granted(old_v, new_v) -> bool:
    """批⑦「方案 a」已授权变更的**谓词式**登记（不写死本机路径、不放宽其它格）。

    占位符语义 = 未配置 ⇒ 退本地开发默认值（线上走真值分支、行为零变化）。因此改造前的
    冻结侧给占位符（`'<deploy-dir>'`），改造后的新侧给 `tempfile.gettempdir()` 派生的绝对
    路径 —— 两侧**同时**满足这两个形态才算「已授权」，其余一律照常判红。免判格**必须打印/计数**
    并计入 `MAX_GRANTED`。
    """
    olds, news = _scalars(old_v), _scalars(new_v)
    if not olds or len(olds) != len(news):
        return False
    tmp = re.sub(r"[\\/]+", "/", tempfile.gettempdir()).rstrip("/")
    home = re.sub(r"[\\/]+", "/", os.path.expanduser("~"))
    for o, n in zip(olds, news):
        o, n = o.strip().strip("'\""), n.strip().strip("'\"")
        if not _PLACEHOLDER_RE.match(o):
            return False                       # 旧侧不是占位符 ⇒ 不适用本登记
        cand = re.sub(r"[\\/]+", "/", n).replace("<HOME>", home)
        if not (cand == tmp or cand.startswith(tmp + "/")):
            return False                       # 新侧不是本机开发兜底 ⇒ 照常判红
    return True


def collect(mod) -> dict:
    """把一件的公开面无参取值全抓出来（常量取 repr，函数调一次取 repr）。"""
    out = {}
    for name in getattr(mod, "__all__", []):
        try:
            obj = getattr(mod, name)
        except AttributeError:
            out[name] = "<缺>"
            continue
        if callable(obj):
            try:
                ps = list(inspect.signature(obj).parameters.values())
                if any(p.default is inspect.Parameter.empty
                       and p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD) for p in ps):
                    out[name] = "<需参>"
                else:
                    out[name] = repr(obj())
            except Exception as exc:          # noqa: BLE001
                out[name] = "<err:%s>" % type(exc).__name__
        else:
            out[name] = repr(obj)
    return out


def check_reference_sites() -> tuple:
    """③ 引用面：谁的源码里用了绝对包名、但有/没有把仓根放进 sys.path。"""
    files = subprocess.run(["git", "ls-files", "*.py"], cwd=ROOT,
                           capture_output=True, check=True).stdout.decode().split()
    ok, suspect = [], []
    for rel in files:
        parts = rel.replace("\\", "/").split("/")
        if any(p in SKIP_DIR_PARTS for p in parts):
            continue
        with open(os.path.join(ROOT, rel), encoding="utf-8", errors="replace") as f:
            src = f.read()
        if "from node.l0_host" not in src and "from node.l0_host." not in src:
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:
            suspect.append((rel, "语法错"))
            continue
        # 判据：文件里既要把路径入 sys.path，又要有从 __file__ 派生的目录（即真的在算仓根）。
        # 写法多样（dirname 嵌套 / PROJECT_ROOT=dirname(HERE) / Path.parents[N]），不做链深要求。
        has_inject = "sys.path.insert" in src or "sys.path.append" in src
        derived = "dirname" in src or "parents[" in src
        rooted = has_inject and derived and "__file__" in src
        (ok if rooted else suspect).append((rel, "" if rooted else "无「仓根入 sys.path」段"))
    return ok, suspect


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--negative", action="store_true", help="旧侧注毒 1 字符 → 装置必须判红")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()

    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)

    # ---- ② 单实例（必须在加载旧侧之前查）
    from node.l0_host import node_paths as np_new
    from node.l0_host import node_profile as npf_new
    dup = [m for m in ("node_paths", "node_profile") if m in sys.modules]
    same_obj = np_new is npf_new._paths()

    # ---- ① 值对账
    old_dir = materialize_old(a.negative)
    sys.path.insert(0, old_dir)
    for m in ("node_paths", "node_profile"):
        sys.modules.pop(m, None)
    import node_paths as np_old            # noqa: E402  顶层旧版（另一实例，专供对账）
    import node_profile as npf_old         # noqa: E402

    diffs, skipped, granted = [], [], []
    for label, om, nm in (("node_paths", np_old, np_new), ("node_profile", npf_old, npf_new)):
        co, cn = collect(om), collect(nm)
        for k in sorted(set(co) | set(cn)):
            vo, vn = co.get(k, "<无>"), cn.get(k, "<无>")
            if vo in ("<需参>", "<err:TypeError>") or vn in ("<需参>", "<err:TypeError>"):
                skipped.append("%s.%s" % (label, k))
                continue
            if vo != vn:
                line = "%s.%s: 旧=%s 新=%s" % (label, k, vo[:70], vn[:70])
                if plan_a_granted(vo, vn):
                    granted.append(line)
                else:
                    diffs.append(line)
        if a.verbose:
            print("  %s：公开项 %d（无参取值 %d）" % (label, len(co), len(co) - sum(1 for v in co.values() if v.startswith("<"))))

    ok, suspect = check_reference_sites()

    print("基线 %s · 旧侧复刻于 %s · 新侧 node/l0_host/" % (REV, old_dir))
    print("① 值对账：不一致 %d 处（跳过「需参/不支持无参调用」%d 项 · 方案 a 已授权 %d 处，上限 %d）"
          % (len(diffs), len(skipped), len(granted), MAX_GRANTED))
    for d in diffs[:12]:
        print("     × %s" % d)
    for d in granted[:12]:
        print("     ○ [方案 a 已授权] %s" % d)
    print("② 单实例：node_paths is node_profile._paths() → %s · 加载旧侧前顶层副本 → %s"
          % ("是" if same_obj else "**否**", "无" if not dup else "**有 %s**" % dup))
    print("③ 引用面：合规 %d 件 · 可疑 %d 件（凡用绝对包名者须自注入仓根）" % (len(ok), len(suspect)))
    for rel, why in suspect[:12]:
        print("     ? %s —— %s" % (rel, why))

    bad = (bool(diffs) or (not same_obj) or bool(dup) or bool(suspect)
           or len(granted) > MAX_GRANTED)
    print("")
    if a.negative:
        if bad:
            print("负控 OK：旧侧注毒后装置判红（值对账 %d 处不一致）—— 判红能力已证" % len(diffs))
            return 1
        print("负控 FAIL：注毒后装置仍判绿 —— 装置失效")
        return 2
    print("rc=%d" % (1 if bad else 0))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
