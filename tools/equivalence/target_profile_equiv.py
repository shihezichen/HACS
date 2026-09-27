# -*- coding: utf-8 -*-
"""「目标主机档案」的**冻结矩阵证据**（可复跑）—— 刀 4。

用法：
    python tools/equivalence/target_profile_equiv.py            # 复跑并与冻结矩阵逐格比对
    python tools/equivalence/target_profile_equiv.py --print    # 只打印当前矩阵（生成/更新冻结值时用）

和前几刀的等价脚本不一样的地方
------------------------------
前几刀证明的是「零行为改变」，所以拿**改前实现**当基线。本刀有**一处有意变更**：
远端执行器路径从「本机真值」改成「目标主机真值」—— 改前的值就是错的，拿它当基线毫无意义。
所以本刀的基线**就是这张矩阵本身**：把 `target_claude_exec` 在
「主机 × env 覆盖」全组合下的返回值逐格冻结在 `FROZEN` 里，以后谁改动档案逻辑，
这张表一格对不上就红。

覆盖的组合（7 × 3 = 21 格）
---------------------------
  主机 host ∈ user@thinkbook / thinkbook / lobster / quantum / top001 / 未知host / 空串
  env  ∈ 无 env / AGENT_TEAM_TARGET_PROFILE_JSON 整表覆盖 / CHAT_ENRICH_CMD 覆盖

每格记四个值：
  known  —— `node_paths.target_known(host)`：False = 拿到的是兜底值不是真值
  fl     —— `node_paths.target_flavour(host)`
  exec   —— `node_paths.target_claude_exec(host)`（本刀的主角）
  remote —— `agent_model.ssh_argv()` 里那条远端命令（证明 `CHAT_ENRICH_CMD` 仍是最外层覆盖，
            以及远端路径确实跟着**目标主机**走）

另外两条断言（契约第 6 条）：
  · thinkbook 目标下 `ssh_argv()` 必须含 **Windows 路径**；
  · lobster  目标下 `ssh_argv()` 必须含 **posix 路径**。
"""
from __future__ import annotations

import argparse
import importlib
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
AG = os.path.join(REPO, "agent-team")
if AG not in sys.path:
    sys.path.insert(0, AG)

#: 参与组合的主机值（含 user@host / 裸 host / 未登记 / 空串）
HOSTS = ["<user>@<node>", "thinkbook", "lobster", "quantum", "top001",
         "nosuchhost", ""]

#: 整表覆盖用的 JSON：故意把 thinkbook 改成 posix、给 lobster 换家目录、加一台新机，
#: 一次性证明「flavour / home / 缺键补齐 / 未登记」四条支路都吃这个 env。
PROFILE_JSON = json.dumps({
    "thinkbook": {"flavour": "posix", "home": "/srv/tb",
                  "claude_exec": "/srv/tb/bin/claude_exec.py"},
    "lobster": {"flavour": "posix", "home": "/data/nbhome"},      # 缺 claude_exec → 按 home 补齐
    "newbox": {"flavour": "nt"},                                # 缺 home/exec → 按 flavour 补齐
}, ensure_ascii=False)

#: 最外层覆盖：这条一旦设了，远端命令必须**一字不变**地用它（与目标主机无关）
CMD_OVERRIDE = "python -X utf8 /custom/exec.py --timeout 150 -"

#: env 场景：名字 → 要设的 env（其余相关 env 一律清空）
SCENARIOS = [
    ("no-env", {}),
    ("profile-json", {"AGENT_TEAM_TARGET_PROFILE_JSON": PROFILE_JSON}),
    ("enrich-cmd", {"CHAT_ENRICH_CMD": CMD_OVERRIDE}),
]

#: 每次重建模块前都要清干净的 env（不清就会串场）
WIPE = ["AGENT_TEAM_TARGET_PROFILE_JSON", "CHAT_ENRICH_CMD", "CHAT_ENRICH_SSH",
        "CHAT_ENRICH_SSH_BIN", "CHAT_ENRICH_KEY", "CHAT_ENRICH_KNOWN_HOSTS",
        "AGENT_TEAM_SSH_TARGET", "AGENT_TEAM_CLAUDE_EXEC"]

# --------------------------------------------------------------------- 冻结矩阵
#: 键 = "场景|主机"，值 = "known|flavour|exec|remote"。改档案逻辑后对不上就是回归。
FROZEN = {
 "no-env|<user>@<node>": "True|nt|<home>/AppData/Local/hermes/scripts/claude_exec.py|python -X utf8 <home>/AppData/Local/hermes/scripts/claude_exec.py --timeout 150 -",
 "no-env|thinkbook": "True|nt|<home>/AppData/Local/hermes/scripts/claude_exec.py|python -X utf8 <home>/AppData/Local/hermes/scripts/claude_exec.py --timeout 150 -",
 "no-env|lobster": "True|posix|<home>/.local/share/hermes/scripts/claude_exec.py|python -X utf8 <home>/.local/share/hermes/scripts/claude_exec.py --timeout 150 -",
 "no-env|quantum": "True|posix|<home>/.local/share/hermes/scripts/claude_exec.py|python -X utf8 <home>/.local/share/hermes/scripts/claude_exec.py --timeout 150 -",
 "no-env|top001": "True|posix|<home>/.local/share/hermes/scripts/claude_exec.py|python -X utf8 <home>/.local/share/hermes/scripts/claude_exec.py --timeout 150 -",
 "no-env|nosuchhost": "False|posix|<home>/.local/share/hermes/scripts/claude_exec.py|python -X utf8 <home>/.local/share/hermes/scripts/claude_exec.py --timeout 150 -",
 "no-env|": "False|posix|<home>/.local/share/hermes/scripts/claude_exec.py|python -X utf8 <home>/.local/share/hermes/scripts/claude_exec.py --timeout 150 -",
 "profile-json|<user>@<node>": "True|posix|/srv/tb/bin/claude_exec.py|python -X utf8 /srv/tb/bin/claude_exec.py --timeout 150 -",
 "profile-json|thinkbook": "True|posix|/srv/tb/bin/claude_exec.py|python -X utf8 /srv/tb/bin/claude_exec.py --timeout 150 -",
 "profile-json|lobster": "True|posix|/data/nbhome/.local/share/hermes/scripts/claude_exec.py|python -X utf8 /data/nbhome/.local/share/hermes/scripts/claude_exec.py --timeout 150 -",
 "profile-json|quantum": "False|posix|<home>/.local/share/hermes/scripts/claude_exec.py|python -X utf8 <home>/.local/share/hermes/scripts/claude_exec.py --timeout 150 -",
 "profile-json|top001": "False|posix|<home>/.local/share/hermes/scripts/claude_exec.py|python -X utf8 <home>/.local/share/hermes/scripts/claude_exec.py --timeout 150 -",
 "profile-json|nosuchhost": "False|posix|<home>/.local/share/hermes/scripts/claude_exec.py|python -X utf8 <home>/.local/share/hermes/scripts/claude_exec.py --timeout 150 -",
 "profile-json|": "False|posix|<home>/.local/share/hermes/scripts/claude_exec.py|python -X utf8 <home>/.local/share/hermes/scripts/claude_exec.py --timeout 150 -",
 "enrich-cmd|<user>@<node>": "True|nt|<home>/AppData/Local/hermes/scripts/claude_exec.py|python -X utf8 /custom/exec.py --timeout 150 -",
 "enrich-cmd|thinkbook": "True|nt|<home>/AppData/Local/hermes/scripts/claude_exec.py|python -X utf8 /custom/exec.py --timeout 150 -",
 "enrich-cmd|lobster": "True|posix|<home>/.local/share/hermes/scripts/claude_exec.py|python -X utf8 /custom/exec.py --timeout 150 -",
 "enrich-cmd|quantum": "True|posix|<home>/.local/share/hermes/scripts/claude_exec.py|python -X utf8 /custom/exec.py --timeout 150 -",
 "enrich-cmd|top001": "True|posix|<home>/.local/share/hermes/scripts/claude_exec.py|python -X utf8 /custom/exec.py --timeout 150 -",
 "enrich-cmd|nosuchhost": "False|posix|<home>/.local/share/hermes/scripts/claude_exec.py|python -X utf8 /custom/exec.py --timeout 150 -",
 "enrich-cmd|": "False|posix|<home>/.local/share/hermes/scripts/claude_exec.py|python -X utf8 /custom/exec.py --timeout 150 -",
}


def _wipe(extra=None):
    for k in WIPE:
        os.environ.pop(k, None)
    for k, v in (extra or {}).items():
        os.environ[k] = v


def _cell(host):
    """在当前 env 下量一格。模块级常量随 env 求值，所以两个模块都必须重载。"""
    import node_paths
    importlib.reload(node_paths)
    os.environ["CHAT_ENRICH_SSH"] = host        # 空串时 ssh_argv 会退回 SSH_TARGET，见下
    import agent_model
    importlib.reload(agent_model)
    known = node_paths.target_known(host)
    fl = node_paths.target_flavour(host)
    exe = node_paths.target_claude_exec(host)
    remote = agent_model.ssh_argv()[-1]
    return "%s|%s|%s|%s" % (known, fl, exe, remote)


def build():
    """跑出当前矩阵。空串主机单独处理：`CHAT_ENRICH_SSH=""` 会被 ssh_argv 当「没设」。"""
    out = {}
    for name, env in SCENARIOS:
        for host in HOSTS:
            _wipe(env)
            if host == "":
                # 空串走 AGENT_TEAM_SSH_TARGET 这条路（模块 import 期求值），才量得到空串语义
                os.environ["AGENT_TEAM_SSH_TARGET"] = " "
            out["%s|%s" % (name, host)] = _cell(host)
    _wipe()
    return out


def check_ssh_argv():
    """契约第 6 条的两条硬断言：路径必须跟着**目标主机**的平台走。"""
    import agent_model
    results = []
    for host, must, kind in [("<user>@<node>", "<home>/AppData/Local/hermes/scripts/claude_exec.py", "Windows 路径"),
                             ("root@lobster", "<home>/.local/share/hermes/scripts/claude_exec.py", "posix 路径")]:
        _wipe({"CHAT_ENRICH_SSH": host})
        importlib.reload(importlib.import_module("node_paths"))
        importlib.reload(agent_model)
        argv = agent_model.ssh_argv()
        ok = must in argv[-1]
        results.append((host, kind, ok, argv[-1]))
    _wipe()
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--print", dest="dump", action="store_true")
    args = ap.parse_args()

    got = build()
    if args.dump:
        print("FROZEN = {")
        for k in sorted(got, key=lambda x: (list(dict(SCENARIOS)).index(x.split("|")[0]),
                                            HOSTS.index(x.split("|", 1)[1]))):
            print(' %s: %s,' % (json.dumps(k, ensure_ascii=False), json.dumps(got[k], ensure_ascii=False)))
        print("}")
        return 0

    bad = 0
    print("=" * 110)
    print("目标主机档案 · 冻结矩阵（%d 格）   列：known | flavour | target_claude_exec | ssh_argv 远端命令" % len(got))
    print("=" * 110)
    for name, _env in SCENARIOS:
        print("\n[%s]" % name)
        for host in HOSTS:
            key = "%s|%s" % (name, host)
            want, have = FROZEN.get(key), got[key]
            flag = "✓" if want == have else "✗"
            if want != have:
                bad += 1
            print("  %s %-18s %s" % (flag, repr(host), have))
            if want != have:
                print("      期望  %s" % want)

    print("\n[ssh_argv 平台断言]")
    for host, kind, ok, remote in check_ssh_argv():
        if not ok:
            bad += 1
        print("  %s %-18s 远端命令含 %s：%s" % ("✓" if ok else "✗", host, kind, remote))

    missing = sorted(set(FROZEN) - set(got))
    if missing:
        bad += len(missing)
        print("\n冻结矩阵里有格子没跑到：%s" % "、".join(missing))

    print("\n" + "-" * 110)
    print("合计 %d 格 + 2 条断言，不符 %d 处" % (len(got), bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
