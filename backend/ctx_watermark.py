"""ctx_watermark.py —— 上下文水位（**薄壳体**，L2 适配层对外入口）。

2026-09-25 阶段 1（刀 1+2）：本文件原先自带一整套 state.db / sessions.json 读取实现，
与 `a2a_svc.py`（L1 通行层）里那套是邻居 —— 同一件事两份代码，且窗口分母已漂移成
两个值（1000000 / 200000）。现全部下沉到同层 `agent_usage.py`（单一权威口径），
本文件只保留对外入口，签名与返回结构一字不变。

契约（对外不变）
----------------
    read_watermark(caller, session_id, agent_cfg=None) -> dict | None
        {"used": int, "window": int, "pct": int,
         "text": "上下文：约 <used>k/<window>k (<pct>%)"}
    读不到返回 None —— 不渲染、**绝不编造数字**。
    两级读取：① 本机直读（本机就是该 agent 所在节点）；② 远端 a2a `/ctx-usage` 回退。

自测
----
    python ctx_watermark.py <caller> <session_id>
"""
from __future__ import annotations

import logging
import sys
from typing import Any, Dict, Optional

import agent_usage

# 旧内部名保留为别名（迁移期兼容，勿新增引用）——实现全部在 agent_usage。
_fmt = agent_usage.fmt
_window_for = agent_usage.window_for
_backend_kind = agent_usage.backend_kind
_estimate_tokens_rough = agent_usage.estimate_tokens_rough
_read_hermes = agent_usage.read_hermes
_read_openclaw = agent_usage.read_openclaw
_read_remote = agent_usage.read_remote
CTX_WINDOW_FALLBACK = agent_usage.CTX_WINDOW_FALLBACK

LOG = logging.getLogger("ctx_watermark")


def read_watermark(caller: str, session_id: str,
                   agent_cfg: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """读真源水位 → dict；读不到返回 None。实现与契约见 agent_usage.read_watermark。"""
    return agent_usage.read_watermark(caller, session_id, agent_cfg)


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 2:
        print(__doc__)
        return 2
    wm = read_watermark(caller=args[0], session_id=args[1])
    print("None（读不到，未渲染）" if wm is None else wm)
    return 0 if wm is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
