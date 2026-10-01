#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_frozen.py —— 等价装置的「冻结快照只读」共用件（**不是装置**，不出判据）

问题
----
`tools/equivalence/_old/` 里躺的是**入库的基线证据**（旧侧实现的冻结快照），
而各装置运行时取的旧侧字节来自**源码仓**（`git show <ref>:agent-team/xxx.py`，
见各装置里的 `SRC_REPO`）。原先是直接

    open(os.path.join(HERE, "_old", "xxx_old.py"), "wb").write(raw)

于是有两个后果：

  ① **工作区被装置弄脏**：跑一次装置 `git status` 就出现改动，让人分不清
     「是装置动了仓」还是「真改了仓」；
  ② 更要命 —— 旧侧字节来自源码仓，就地覆盖等于**把源码仓的内容写进对外发布仓**。
     对外仓是脱敏发布面，源码仓含真值；这条路径把两者的边界偷偷抹掉了。

规矩
----
  · 与冻结快照**逐字节相同** ⇒ 直接用快照，**一个字节都不写**（正常路径）；
  · **不同** ⇒ **不改写快照**，写临时目录，并把差异**如实报出来**
    （不静默改写、也不假装一致）；
  · 快照**不存在** ⇒ 才写（首次建立基线时的正常落盘）。

调用方拿到的永远是「可以直接用的路径」—— 装置不必关心走的是哪条分支。
"""
from __future__ import annotations

import os
import tempfile

__all__ = ["write_frozen"]


def write_frozen(path, raw, what="旧侧"):
    """交付旧侧字节；遵守「冻结快照只读」，返回可直接使用的文件路径。

    path : 仓内冻结快照的落点（`<HERE>/_old/xxx_old.py`）
    raw  : 从源码仓取来的字节（**不要**先做任何改写，改写由调用方在取时就做完）
    what : 报告里用的名字，比如「a2a_svc.py」
    """
    # 快照不存在 ⇒ 首次落盘，这是唯一该写仓内快照的情形
    if not os.path.isfile(path):
        d = os.path.dirname(path)
        if d and not os.path.isdir(d):
            os.makedirs(d)
        with open(path, "wb") as fh:
            fh.write(raw)
        return path

    with open(path, "rb") as fh:
        frozen = fh.read()

    if frozen == raw:
        return path                      # 与冻结快照逐字节相同：一个字节都不写

    # 不同：绝不改写仓内快照，改落临时副本，并如实报告
    tmp_dir = os.path.join(
        os.environ.get("TMPDIR") or os.environ.get("TEMP") or tempfile.gettempdir(),
        "equiv_old_overlay")
    if not os.path.isdir(tmp_dir):
        os.makedirs(tmp_dir)
    alt = os.path.join(tmp_dir, os.path.basename(path))
    with open(alt, "wb") as fh:
        fh.write(raw)

    print("注意：源码仓取到的 %s 与冻结快照**不同**。" % what)
    print("      按「冻结快照只读」的规矩不改写它，本次改用临时副本：%s" % alt)
    print("      若确认源码仓该版才是新基线，请单独走一次收基线流程，别让装置顺手改。")
    return alt
