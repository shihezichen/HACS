#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
a2a_svc.py —— A2A 服务的**组合根**（层 C：进程入口装配）

为什么有这个文件：A2A 边缘端点属 L1 通信层，而「上下文用量」属 L2 适配层。
原先 L1 的 a2a_svc 直接 import L2 的 agent_usage，是一条**反向依赖**（下层引用上层），
在 tools/hacs_layers.json 里登记为例外。本件把这条边翻转过来：

  · 实现下沉到 L1 `a2a_edge.py`（只声明能力端口，不认识任何上层件）；
  · 适配能力留在 L2 `agent_usage.py`（水位/窗口/后端判定/会话头的单一口径）；
  · **本件**（层 C）在引导期把 L2 的实现注入 L1 的端口，并把 `/ctx-usage/<agent_id>`
    这个「适配面的传输薄壳」作为一条路由登记进去。

层 C 的语义：C 可 import 任意层；任何层不得 import C。本件因此**只做装配与注入**，
不含业务逻辑、不含传输实现 —— 唯一的例外是 `/ctx-usage` 的响应构造，它本就是
L2 能力的对外表述，放在装配侧而不是通信层，才不会把适配语义漏回 L1。

文件名与命令行参数保持不变（systemd 单元与 Windows 计划任务都指向本文件）：
    python a2a_svc.py --host <tailnet-ip> --port 9920 \
        --config <agents.json 路径> --peers <peers.json 路径>
argv 原样透传给 a2a_edge.main()。
"""

import os
import sys
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

# 批⑤c：a2a_edge 已归位 node/l1_transport/a2a/（L1 通信层子包），本件按绝对包名 import，故把仓根放进搜索路径。
for _hacs_p in (os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                os.path.dirname(os.path.abspath(__file__))):
    sys.path.insert(0, _hacs_p)

from node.l1_transport.a2a import a2a_edge  # L1 通信层：A2A 边缘端点实现（传输，不含 Agent 差异）
# 批⑤d 过渡：agent_usage 仍在 backend/（⑤e 搬走后此处再改一次）。
from backend import agent_usage     # L2 适配层：水位/窗口/后端判定/会话头单一实现

# 对外可见的版本号仍由 L1 实现件持有（Server 头 / healthz / Agent Card 同一份）。
VERSION = a2a_edge.VERSION
PROTOCOL_VERSION = a2a_edge.PROTOCOL_VERSION

CTX_USAGE_PREFIX = "ctx-usage"


# ---------------------------------------------------------------- 路由：/ctx-usage

def ctx_usage_route(handler_self: Any, parts: List[str]) -> bool:
    """/ctx-usage/<agent_id>?session_id=... —— 上下文水位真源（2026-09-24）

    供群里「📚 上下文」那行由服务端渲染用（chat_svc 在别的机器上，读不到本机
    会话真源，只能调这里）。返回该 session 的 (占用, 窗口, pct, text)。
    鉴权与 /agents 同口径；读不到就回 ok=false + reason，**绝不编造数字**。

    返回 True = 已应答；False = 本条不归我管，交回 L1 继续往下走（最终 404）。
    只认「恰好两段」的路径，与迁移前 `len(parts) == 2` 的判据逐字一致。
    """
    if len(parts) != 2:
        return False
    peer = handler_self._auth_peer()
    if peer is None:
        handler_self._send_json(401, {"ok": False, "error": "unauthorized"})
        return True
    agent_id = parts[1]
    cfg = a2a_edge.Runtime.agents.get(agent_id)
    if cfg is None:
        handler_self._send_json(404, {"ok": False, "error": "unknown_agent"})
        return True
    qs = urllib.parse.parse_qs(urllib.parse.urlsplit(handler_self.path).query)
    session_id = (qs.get("session_id") or [""])[0].strip()
    if not session_id:
        handler_self._send_json(400, {"ok": False, "error": "missing_session_id"})
        return True
    usage = agent_usage.read_usage(agent_id, session_id, cfg)
    if usage is None:
        handler_self._send_json(200, {"ok": False, "reason": "no_usage",
                                      "agent": agent_id, "session_id": session_id}, peer)
        return True
    used, window = int(usage[0]), int(usage[1])
    if window <= 0:
        handler_self._send_json(200, {"ok": False, "reason": "no_window"}, peer)
        return True
    pct = int(round(100.0 * used / window))
    handler_self._send_json(200, {
        "ok": True, "agent": agent_id, "session_id": session_id,
        "used": used, "window": window, "pct": pct,
        "text": "上下文：约 %dk/%dk (%d%%)" % (used // 1000, window // 1000, pct),
    }, peer)
    return True


# ---------------------------------------------------------------- 装配

def _read_usage(caller: str, session_id: str,
                agent: Optional[Dict[str, Any]] = None) -> Optional[Tuple[int, int]]:
    """能力 read_usage：按后端分派读水位（读不到 → None，绝不编造）。"""
    return agent_usage.read_usage(caller, session_id, agent)


def _pressure(caller: str, session_id: str,
              agent: Optional[Dict[str, Any]] = None,
              min_tokens_floor: int = 0) -> Optional[float]:
    """能力 pressure：上下文水位 0.0~1.0+；读不到 None。"""
    return agent_usage.pressure(caller, session_id, agent,
                                min_tokens_floor=min_tokens_floor)


def build_capabilities() -> Dict[str, Any]:
    """L1 声明的能力端口 → L2 实现的绑定表（键名与签名见 a2a_edge 顶部注释）。"""
    return {
        "backend_of": agent_usage.backend_kind,
        "session_headers": agent_usage.hermes_session_headers,
        "read_usage": _read_usage,
        "pressure": _pressure,
        "get_routes": lambda: [(CTX_USAGE_PREFIX, ctx_usage_route)],
    }


def install() -> None:
    """引导期装配：把 L2 能力注入 L1 端口。幂等，可在测试里重复调用。"""
    a2a_edge.install_capabilities(build_capabilities())


def main(argv: Optional[List[str]] = None) -> int:
    install()
    return a2a_edge.main(argv)


if __name__ == "__main__":
    sys.exit(main())
