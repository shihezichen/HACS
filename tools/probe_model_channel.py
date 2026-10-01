#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T 工具面：**模型通道自检探针** —— 真打一次最小 prompt，回答「这条通道现在通不通」。

为什么要它
----------
模型富化通道是「本机 → ssh 目标机 → claude_exec.py」三段式，中间任何一段断了，
上层表现都一样：enrich worker `running=true`，`submitted/done` 恒为 0 —— **静默**。
2026-09-25 就是这么埋的雷：chat_svc 从 thinkbook 迁到 lobster 后，远端执行器路径取成了
本机路径，远端 `can't open file`，从 healthz 上完全看不出来。本件把这段哑巴链路变成
一条能主动跑、退出码能判死的探针。

用法
----
    python tools/probe_model_channel.py                 # 本机直跑
    python tools/probe_model_channel.py --node lobster  # 经 ssh 在该节点上跑
    python tools/probe_model_channel.py --json          # 机器可读
    python tools/probe_model_channel.py --timeout 90    # 换单次超时（秒）

`--node` 的节点名、ssh 目标、私钥、运行目录全部读 `tools/deploy_targets.json`（内部真值表，
本件零硬编码）；`transport=local` 的节点等同本机直跑。

退出码：0 = 通道打通且输出可解析；1 = 没打通（原因见 `why`）；2 = 用法/配置错误。
**只读**：不写库、不改状态、不重启任何服务。
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
PROJECT_ROOT = os.path.dirname(HERE)
TARGETS_PATH = os.path.join(HERE, "deploy_targets.json")

SSH_TIMEOUT_PAD = 60          # 远端跑 probe 时，ssh 本身比 probe 多给这么多秒
EXIT_OK, EXIT_FAIL, EXIT_USAGE = 0, 1, 2

#: 在远端执行的自检片段：从该节点运行目录里 import agent_model，打一次 probe，吐 JSON。
#: 刻意只用 stdlib + 绝对路径，不依赖远端 cwd，也不依赖远端装了本仓其它东西。
REMOTE_SNIPPET = (
    "import json,sys;"
    "sys.path.insert(0, %(rt)r);"
    "import agent_model;"
    "r = agent_model.probe(timeout_s=%(to)s);"
    "print(json.dumps(r, ensure_ascii=False))"
)

_ENV_RX = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::([^}]*))?\}")


def _expand(value):
    """展开 `${VAR:默认值}`（与 deploy.py 同一口径）。取不到值就原样留着。"""
    if not isinstance(value, str):
        return value

    def sub(m):
        return os.environ.get(m.group(1)) or (m.group(2) or "")
    return _ENV_RX.sub(sub, value)


def load_targets():
    if not os.path.isfile(TARGETS_PATH):
        return []
    with io.open(TARGETS_PATH, encoding="utf-8") as fh:
        return (json.load(fh) or {}).get("targets") or []


def probe_local(timeout_s):
    """本机直跑：直接 import L2 agent_model 调 probe()。"""
    # 被搬件按绝对包名 import（hub.l2_adapter.agent_model），故仓根入路径即可。
    if PROJECT_ROOT not in sys.path:
        sys.path.insert(0, PROJECT_ROOT)
    try:
        from hub.l2_adapter import agent_model                                  # noqa: E402
    except Exception as exc:                                # 缺依赖也要给结构化结果
        return {"ok": False, "target": "", "argv": [], "elapsed_s": 0.0,
                "why": "import_failed:%s" % exc, "parsed": None}
    return agent_model.probe(timeout_s=timeout_s)


def probe_remote(target, timeout_s, python_bin):
    """经 ssh 在目标节点上跑同一段 probe，把远端吐的 JSON 原样带回来。"""
    argv = ["ssh"]
    key = _expand(target.get("ssh_key") or "")
    if key:
        argv += ["-i", key]
    argv += ["-o", "ConnectTimeout=10", "-o", "BatchMode=yes",
             "-o", "StrictHostKeyChecking=accept-new", _expand(target["ssh_target"])]
    snippet = REMOTE_SNIPPET % {"rt": _expand(target["runtime_dir"]), "to": float(timeout_s)}
    argv += ["%s -c %s" % (python_bin, _shq(snippet))]
    try:
        proc = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=float(timeout_s) + SSH_TIMEOUT_PAD)
    except subprocess.TimeoutExpired:
        return {"ok": False, "target": target["name"], "argv": argv, "elapsed_s": 0.0,
                "why": "ssh_timeout_%ds" % int(float(timeout_s) + SSH_TIMEOUT_PAD), "parsed": None}
    except OSError as exc:
        return {"ok": False, "target": target["name"], "argv": argv, "elapsed_s": 0.0,
                "why": "ssh_spawn_error:%s" % exc, "parsed": None}
    out = (proc.stdout or b"").decode("utf-8", "replace").strip()
    err = (proc.stderr or b"").decode("utf-8", "replace").strip()
    start, end = out.find("{"), out.rfind("}")
    if proc.returncode != 0 or start < 0 or end <= start:
        return {"ok": False, "target": target["name"], "argv": argv, "elapsed_s": 0.0,
                "why": "remote_exit_%d:%s" % (proc.returncode, (err or out)[-200:]),
                "parsed": None}
    try:
        return json.loads(out[start:end + 1])
    except ValueError:
        return {"ok": False, "target": target["name"], "argv": argv, "elapsed_s": 0.0,
                "why": "remote_unparsable:%s" % out[-200:], "parsed": None}


def _shq(text):
    """posix 单引号包裹（远端一律是 posix shell）。"""
    return "'" + str(text).replace("'", "'\"'\"'") + "'"


def render(result, node_label):
    lines = ["=" * 72,
             "模型通道自检 · %s" % node_label,
             "=" * 72,
             "  结果      %s" % ("✓ 打通" if result.get("ok") else "✗ 没打通"),
             "  ssh 目标  %s" % (result.get("target") or "（未知）"),
             "  耗时      %ss" % result.get("elapsed_s"),
             "  说明      %s" % (result.get("why") or "")]
    argv = result.get("argv") or []
    if argv:
        lines.append("  命令行    %s" % " ".join(str(a) for a in argv))
    parsed = result.get("parsed")
    if parsed:
        lines.append("  解析判定  %s" % json.dumps(parsed, ensure_ascii=False)[:300])
    if "target_unknown" in (result.get("why") or ""):
        lines.append("  ⚠ 该 ssh 目标没登记在 L0 目标主机档案里，远端路径用的是 posix 兜底值，"
                     "不是真值 —— 请在 node_paths.py 的档案表里补上，或用 "
                     "AGENT_TEAM_TARGET_PROFILE_JSON 覆盖。")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="模型通道自检探针（真打一次最小 prompt）")
    ap.add_argument("--node", default="", help="节点名（读 tools/deploy_targets.json）；省略=本机直跑")
    ap.add_argument("--timeout", type=float, default=60.0, help="单次调用超时秒数（默认 60）")
    ap.add_argument("--python", default="python3", help="远端 python 可执行名（默认 python3）")
    ap.add_argument("--json", action="store_true", help="只吐 JSON")
    args = ap.parse_args()

    if not args.node:
        result, label = probe_local(args.timeout), "本机直跑"
    else:
        targets = {t.get("name"): t for t in load_targets()}
        if args.node not in targets:
            print("✗ 没有这个节点：%s（可选：%s）" % (args.node, "、".join(sorted(targets)) or "无"))
            return EXIT_USAGE
        target = targets[args.node]
        if (target.get("transport") or "") == "local":
            result, label = probe_local(args.timeout), "%s（transport=local，即本机）" % args.node
        else:
            result, label = probe_remote(target, args.timeout, args.python), "节点 %s（经 ssh）" % args.node

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    else:
        print(render(result, label))
    return EXIT_OK if result.get("ok") else EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
