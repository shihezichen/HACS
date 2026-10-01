#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build_deploy_tree.py —— 按 deploy_manifest.json 生成**运行面镜像目录**（T 面工具）

为什么需要镜像
--------------
机群分发走既有机制 tools/deploy.py + tools/deploy_targets.json：它对每一件只记一个
**相对路径**，然后用同一个相对路径去拼「源」和「目标」—— 也就是说

    部署源目录的结构  必须 ==  运行面目录的结构

而本仓是**分层结构**（hub/ node/ 各自 l0…l4 分层包），运行面要的却是另一种布局：
分层包要原样在（被搬件早已改成绝对包名 import，运行面没有同样的包结构就 import 不到），
前端静态件要收在一个 `ui/` 静态根下，启动壳要躺在运行面根（计划任务直接以
`<RUN_DIR>\\_boot.py` 调起，不认子目录）。两种结构对不上，部署源就不能直接指向本仓。

所以：本脚本从本仓**生成**一份结构 == 运行面的镜像目录，再把部署源指向它。
本仓仍是唯一真源，镜像是派生物 —— 它可以随时重建，不该被手改，也不该入库。

镜像的布局
----------
  1. **分层包原样镜像**：`hub/…` `node/…` `tools/…` 的件，rel ≡ 仓内相对路径。
     这是默认规则，因为这些件按绝对包名互相 import，包路径必须逐级保留。
  2. **`ui/` 静态根**：前端件从 `hub/l4_experience/` 映射到镜像的 `ui/`
     （对话台服务的静态根靠环境变量指到它，故可以换目录名）。
  3. **根下启动壳**：Windows 启动壳从 `deploy/windows/` 映射到镜像**根**，
     好让计划任务以 `<RUN_DIR>\\<壳件名>` 直接调起。

与 deploy_manifest.json 的关系
------------------------------
清单是**表**，本脚本是**搬运工**：表里每条 `{"rel": …, "src": …}` 说的是
「仓内 `src` 这一件，在运行面叫 `rel`」；上面三条布局规则全都已经写死在表里
（默认规则 = rel 与 src 相同；两条例外 = rel 与 src 不同）。

本脚本因此**只认表、不认真值**：它不知道有哪些机器、机器在哪、用什么凭据 ——
那些在源码仓的 tools/deploy_targets.json 里，禁止进本仓。新增件时先改表，
不改本脚本。

用法
----
    python deploy/build_deploy_tree.py --out <目录>            # 生成
    python deploy/build_deploy_tree.py --out <目录> --clean    # 先清空目标再生成
    python deploy/build_deploy_tree.py --check                 # 只校验不写（源件是否齐全）

退出码：0 = 全部搬到且逐件 md5 复验一致；1 = 有缺源件 / 有 md5 不一致；
        2 = 跑不起来（清单不可读、参数不合法、目标目录不可用）。
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys

#: 仓根 = 本件所在 deploy/ 的上一级。镜像清单与所有源件都按仓根解析。
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST_DEFAULT = os.path.join(REPO_ROOT, "deploy", "deploy_manifest.json")

CHUNK = 1 << 20

#: 「跑不起来」统一走退出码 2（见头部 docstring）：清单不可读 / 参数不合法 / 落点不可用。
EXIT_UNUSABLE = 2


def die(msg):
    """跑不起来：把原因说清楚，按约定退 2（裸 `SystemExit(str)` 会退 1，对不上契约）。"""
    sys.stderr.write(msg + "\n")
    return SystemExit(EXIT_UNUSABLE)


def md5_of(path):
    """文件 md5（分块读，别把大件整个吃进内存）。"""
    h = hashlib.md5()
    with open(path, "rb") as fh:
        while True:
            blk = fh.read(CHUNK)
            if not blk:
                break
            h.update(blk)
    return h.hexdigest()


def load_manifest(path):
    """读清单；只认 files[] 里的 rel / src 两个键，其余（_readme 等）一概不碰。"""
    import json
    try:
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except OSError as exc:
        raise die("清单读不到：%s（%s）" % (path, exc))
    except ValueError as exc:
        raise die("清单不是合法 JSON：%s（%s）" % (path, exc))

    files = doc.get("files")
    if not isinstance(files, list) or not files:
        raise die("清单里没有 files[]：%s" % path)

    items, seen = [], {}
    for i, ent in enumerate(files):
        rel, src = ent.get("rel"), ent.get("src")
        if not rel or not src:
            raise die("清单第 %d 条缺 rel 或 src：%r" % (i + 1, ent))
        rel = rel.replace("\\", "/").lstrip("/")
        src = src.replace("\\", "/").lstrip("/")
        if rel in seen:
            raise die("清单里 rel 重复：%s（第 %d 条与第 %d 条）"
                      % (rel, seen[rel] + 1, i + 1))
        seen[rel] = i
        items.append((rel, src))
    return items


def guard_out_dir(out):
    """--clean 要删目录，先挡住明显不该删的落点：仓根 / 仓内 / 盘符根。"""
    out_abs = os.path.abspath(out)
    if os.path.dirname(out_abs) == out_abs:          # 盘符根 / 文件系统根
        raise die("--out 不能是盘符根：%s" % out_abs)
    if out_abs == REPO_ROOT or out_abs.startswith(REPO_ROOT + os.sep):
        raise die("--out 不能落在仓内（镜像是派生物，不入库）：%s" % out_abs)
    return out_abs


def check_only(items):
    """--check：只读源件存在性，一个字节都不写。"""
    missing = [(rel, src) for rel, src in items
               if not os.path.isfile(os.path.join(REPO_ROOT, src))]
    total = sum(os.path.getsize(os.path.join(REPO_ROOT, src))
                for rel, src in items
                if os.path.isfile(os.path.join(REPO_ROOT, src)))
    print("模式          : --check（只校验，不写任何文件）")
    print("清单件数      : %d" % len(items))
    print("源件齐全      : %d / %d" % (len(items) - len(missing), len(items)))
    print("源件总字节    : %d" % total)
    report_list("缺失清单", ["%s  ←  %s" % (rel, src) for rel, src in missing])
    print("不一致清单    : （--check 不复制，不适用）")
    return 1 if missing else 0


def build(items, out_abs, clean):
    if clean and os.path.isdir(out_abs):
        shutil.rmtree(out_abs)
    os.makedirs(out_abs, exist_ok=True)

    missing, mismatch = [], []
    done, total_bytes = 0, 0

    for rel, src in items:
        src_abs = os.path.join(REPO_ROOT, src)
        if not os.path.isfile(src_abs):
            missing.append("%s  ←  %s" % (rel, src))
            continue

        dst_abs = os.path.join(out_abs, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(dst_abs), exist_ok=True)
        shutil.copy2(src_abs, dst_abs)

        # 拷完逐件复验：源与目标 md5 必须一致，不一致就是失败，不许写成成功。
        want, got = md5_of(src_abs), md5_of(dst_abs)
        if want != got:
            mismatch.append("%s  src=%s  dst=%s" % (rel, want, got))
            continue
        done += 1
        total_bytes += os.path.getsize(dst_abs)

    print("模式          : --out%s" % ("  --clean" if clean else ""))
    print("镜像目录      : %s" % out_abs)
    print("清单件数      : %d" % len(items))
    print("生成件数      : %d" % done)
    print("总字节        : %d" % total_bytes)
    report_list("缺失清单", missing)
    report_list("不一致清单", mismatch)
    return 1 if (missing or mismatch) else 0


def report_list(title, rows):
    # 上面几行的标签都是全角字符，按「一个全角占两列」补到 14 列才对得齐。
    label = title + " " * max(1, 14 - 2 * len(title))
    if not rows:
        print("%s: （空）" % label)
        return
    print("%s: %d 条" % (label, len(rows)))
    for r in rows:
        print("    · %s" % r)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="按 deploy_manifest.json 生成运行面镜像目录（结构 == 运行面）")
    ap.add_argument("--out", default=None, help="镜像目录（生成模式必填）")
    ap.add_argument("--clean", action="store_true", help="先清空 --out 再生成")
    ap.add_argument("--check", action="store_true", help="只校验源件是否齐全，不写文件")
    ap.add_argument("--manifest", default=MANIFEST_DEFAULT,
                    help="部署视图清单（默认 deploy/deploy_manifest.json）")
    args = ap.parse_args(argv)

    if args.check and args.out:
        ap.error("--check 与 --out 互斥：--check 不写文件")
    if not args.check and not args.out:
        ap.error("要么给 --out <目录> 生成，要么给 --check 只校验")
    if args.clean and args.check:
        ap.error("--clean 与 --check 互斥：--check 不写文件")

    items = load_manifest(args.manifest)
    print("清单          : %s" % os.path.abspath(args.manifest))
    print("仓根          : %s" % REPO_ROOT)

    if args.check:
        return check_only(items)
    return build(items, guard_out_dir(args.out), args.clean)


if __name__ == "__main__":
    sys.exit(main())
