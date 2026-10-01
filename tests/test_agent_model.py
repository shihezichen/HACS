#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""L2 `agent_model` 的通道自检与「远端路径跟着目标主机走」回归（刀 4）。

跑法：`python tests/test_agent_model.py`（在 agent-team/ 下）。

两件事：
  ① `probe()` —— 用 **subprocess 桩**替掉真调用，断言返回结构、ok 判定、降级路径、
     未登记目标的 `target_unknown` 提示，以及「不写库、不改状态」（桩记录调用次数）；
  ② 远端执行器路径 —— thinkbook 目标拿 Windows 路径、lobster 目标拿 posix 路径。
     这是 2026-09-25 那个「本机真值被当远端真值用」缺陷的回归钉子。
"""

import importlib
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)  # 仓根：node.l0_host 绝对包名 import 需要

from hub.l2_adapter import agent_model as am      # noqa: E402
from node.l0_host import node_paths as np  # noqa: E402

WIPE = ["CHAT_ENRICH_ENABLE", "CHAT_ENRICH_SSH", "CHAT_ENRICH_SSH_BIN", "CHAT_ENRICH_CMD",
        "CHAT_ENRICH_KEY", "CHAT_ENRICH_KNOWN_HOSTS", "CHAT_ENRICH_TIMEOUT_S",
        "AGENT_TEAM_ETC_DIR", "AGENT_TEAM_SSH_TARGET", "AGENT_TEAM_TARGET_PROFILE_JSON"]

#: 运行面基线（方案 a 口径，见 9af4f05「脱敏占位符收口」）：源码树里部署面真值是**脱敏占位符**，
#: 占位符语义 = 「未配置」⇒ 件自身退到开发兜底。本件量的是**配好了**的那条路，所以夹具得自己把
#: 基线注进去，否则量到的是「没配置」的样子而不是实现错：
#:   · 不注 `AGENT_TEAM_ETC_DIR` ⇒ `etc_dir()` 退到 `%TEMP%`，`-i` / `UserKnownHostsFile`
#:     跟着机器变（argv 不可复现）；
#:   · 不注 `AGENT_TEAM_SSH_TARGET` ⇒ 目标是**空串**，`target_known("")` 必为 False，
#:     于是 `probe()` 一律给 `ok|target_unknown:` / `disabled|target_unknown:` —— 那是
#:     「目标没配」的正确行为，不是 `why` 口径变了。
#: 基线目标取**登记在档案表里**的 `root@lobster`（chat_svc 的实跑节点），这样默认路径下
#: `target_known()` 为真、`why` 不挂尾巴；要量未登记目标的用例自己用 `CHAT_ENRICH_SSH` 覆盖。
BASE_ENV = {"AGENT_TEAM_ETC_DIR": "/hacs-test-etc",
            "AGENT_TEAM_SSH_TARGET": "root@lobster"}

GOOD = b'{"is_task": false, "task_name": "", "summary": "", "confidence": 0.9}'


class _Proc(object):
    def __init__(self, rc, out, err=b""):
        self.returncode, self.stdout, self.stderr = rc, out, err


class _Stub(object):
    """subprocess.run 的桩：记下每次 argv / stdin，按脚本给回结果。绝不起真进程。"""

    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append({"argv": list(argv), "input": kw.get("input"),
                           "timeout": kw.get("timeout")})
        got = self.results.pop(0) if self.results else _Proc(0, GOOD)
        if isinstance(got, Exception):
            raise got
        return got


def _reload(env=None):
    """清掉这条链路的全部 env，铺上运行面基线，再叠本次用例的覆盖，最后重载两件。

    `am` 的 `AGENT_TEAM_ETC_DIR` / `SSH_TARGET` / `REMOTE_CMD` 是**模块级**求值的，
    所以换 env 必须 reload 才算数（`np` 要先 reload：`am` 的模块级常量读它）。
    用例给的 `env` 叠在 `BASE_ENV` 之上 —— 同名键以用例为准。
    """
    for k in WIPE:
        os.environ.pop(k, None)
    merged = dict(BASE_ENV)
    merged.update(env or {})
    for k, v in merged.items():
        os.environ[k] = v
    importlib.reload(np)
    importlib.reload(am)


def _with_stub(stub, fn):
    real = subprocess.run
    am.subprocess.run = stub
    try:
        return fn()
    finally:
        am.subprocess.run = real


# ------------------------------------------------------------------ probe()

def t_probe_ok():
    """打通：ok=True，why=ok，parsed 是校验过的判定，argv 与 ssh_argv() 一致。"""
    _reload()
    stub = _Stub(_Proc(0, GOOD))
    r = _with_stub(stub, lambda: am.probe(timeout_s=7))
    assert r["ok"] is True, r
    assert r["why"] == "ok", r
    assert r["parsed"]["is_task"] is False, r
    assert r["parsed"]["confidence"] == 0.9, r
    assert r["target"] == am.SSH_TARGET, r
    assert r["argv"] == am.ssh_argv(), r
    assert isinstance(r["elapsed_s"], float), r
    assert len(stub.calls) == 1, "probe 只该打一次"
    assert stub.calls[0]["timeout"] == 7, stub.calls[0]
    assert am.PROBE_PROMPT.encode("utf-8") == stub.calls[0]["input"], "提示词要原样送进 stdin"
    return "ok=True / 结构齐全 / 只打一次 / timeout 透传"


def t_probe_exit_nonzero():
    """远端非 0 退出（就是线上那条 can't open file）：ok=False，why 带原文尾巴。"""
    _reload()
    stub = _Stub(_Proc(2, b"", b"python.exe: can't open file 'X'"))
    r = _with_stub(stub, lambda: am.probe(timeout_s=5))
    assert r["ok"] is False, r
    assert r["why"].startswith("exit_2:"), r
    assert "can't open file" in r["why"], r
    assert r["parsed"] is None, r
    return "退出码非 0 → ok=False，原文进 why"


def t_probe_unparsable_and_disabled():
    """输出不是 JSON → unparsable；总开关关掉 → disabled 且**一次子进程都不起**。"""
    _reload()
    stub = _Stub(_Proc(0, "抱歉，我不能".encode("utf-8")))
    r = _with_stub(stub, lambda: am.probe(timeout_s=5))
    assert r["ok"] is False and r["why"].startswith("unparsable:"), r

    _reload({"CHAT_ENRICH_ENABLE": "0"})
    stub2 = _Stub(_Proc(0, GOOD))
    r2 = _with_stub(stub2, lambda: am.probe(timeout_s=5))
    assert r2["ok"] is False and r2["why"] == "disabled", r2
    assert stub2.calls == [], "关掉总开关还起子进程 = 回归"
    _reload()
    return "unparsable / disabled 两条降级路径都对"


def t_probe_timeout_and_unknown_target():
    """超时降级；目标没登记在档案表里时 why 追加 target_unknown。"""
    _reload({"CHAT_ENRICH_SSH": "root@nosuchbox"})
    stub = _Stub(subprocess.TimeoutExpired(cmd="ssh", timeout=5))
    r = _with_stub(stub, lambda: am.probe(timeout_s=5))
    assert r["ok"] is False, r
    assert r["why"].startswith("timeout_5s"), r
    assert "target_unknown:root@nosuchbox" in r["why"], r
    # 登记过的目标不许挂这个尾巴
    _reload({"CHAT_ENRICH_SSH": "root@lobster"})
    stub2 = _Stub(_Proc(0, GOOD))
    r2 = _with_stub(stub2, lambda: am.probe(timeout_s=5))
    assert "target_unknown" not in r2["why"], r2
    _reload()
    return "超时降级 + 未登记目标挂 target_unknown"


# ------------------------------------------- 远端路径跟着目标主机走（缺陷回归钉子）

def t_remote_path_follows_target():
    """thinkbook 目标 → Windows 路径；lobster 目标 → posix 路径。"""
    # 主机名用档案表里**登记的那个名字**（`thinkbook`）：`target_host()` 只取 `@` 右边去查表，
    # 用户名不参与，所以左边留占位符无妨。此前写的 `<node>` 是脱敏占位符，查表必然落空 ⇒
    # 退 posix 兜底，这条 nt 用例就永远量不到自己要量的东西。
    _reload({"AGENT_TEAM_SSH_TARGET": "<user>@thinkbook"})
    win = am.ssh_argv()[-1]
    assert "<home>/AppData/Local/hermes/scripts/claude_exec.py" in win, win
    assert am.REMOTE_CMD == win, (am.REMOTE_CMD, win)

    _reload({"AGENT_TEAM_SSH_TARGET": "root@lobster"})
    posix = am.ssh_argv()[-1]
    # 期望值必须是 `/root` 而不是 `<home>`：posix 目标的家目录默认值就是字面量 `/root`
    # （`node_profile.POSIX_TARGET_HOME`，刀 21 之前的快照里也是 `/root`，从未变过）；
    # `<home>` 只是 **nt 侧** 的占位形态（`NT_TARGET_HOME`，批⑦ 收口时就它会是占位符）。
    # 同文件的 `test_node_paths.py` 里那两条断言本是一对：`target_home == "/root"` 与
    # `target_claude_exec == "<home>/.local/..."` —— 后一条拿 posix 的 host 去比 nt 的占位符，
    # 与它前一条自相矛盾、恒不可满足，所以这条用例从入仓首版（`2b8a90d`）起就没绿过。
    assert "/root/.local/share/hermes/scripts/claude_exec.py" in posix, posix

    # 缺陷本体：远端路径不许等于「本机」路径（在 Windows 上跑时这两者恰好同形，
    # 所以用「目标是 lobster 时不得出现盘符」来钉住跨主机语义）
    assert ":" not in posix.split("python -X utf8 ")[1].split()[0][:2], posix
    _reload()
    return "路径按目标主机取（Windows↔posix 不串台）"


def t_cmd_override_is_outermost():
    """`CHAT_ENRICH_CMD` 仍是最外层覆盖：设了就一字不变，与目标主机无关。"""
    # 三个目标各占一类：登记的 nt / 登记的 posix / 未登记（同上，`<node>` 查表落空，
    # 会把前两类塌成「两个未登记」，这条「与目标主机无关」就少量了一类）
    for target in ("<user>@thinkbook", "root@lobster", "root@nosuchbox"):
        _reload({"CHAT_ENRICH_CMD": "python3 /x/y.py -", "CHAT_ENRICH_SSH": target})
        assert am.ssh_argv()[-1] == "python3 /x/y.py -", am.ssh_argv()
    _reload()
    return "CHAT_ENRICH_CMD 三个目标下都一字不变"


def t_public_surface_unchanged():
    """对外名字面：刀 4 只加 probe，刀 14 只加 DISPLAY_NAMES_FALLBACK，别的一个不许少。

    名单变更只跟着**已登记的实现演进**走，每加一条都要能指到台账里的那一刀：
      · `probe`（刀 4）—— 通道自检；
      · `DISPLAY_NAMES_FALLBACK`（阶段3 刀 14，`049a418`）—— 兜底表的公开别名
        （与私有原名 `_DISPLAY_NAMES_FALLBACK` **同一个对象**），用来消掉 chat_svc 那处
        跨件私有面访问；登记在 `docs/architecture.md:210` 与 `tools/hacs_layers.json`
        的 L2 note 里。删除面：一个都没有。
    """
    want = ["enabled", "timeout_s", "min_interval_s", "ssh_argv", "parse", "call",
            "probe", "display_name", "DISPLAY_NAMES", "DISPLAY_NAMES_FALLBACK"]
    _reload()
    assert am.__all__ == want, am.__all__
    for name in want:
        assert hasattr(am, name), name
    assert am.display_name("lobster") == "龙虾", am.display_name("lobster")
    assert am.timeout_s() == 240.0, am.timeout_s()
    assert am.min_interval_s() == 1.0, am.min_interval_s()
    assert am.enabled() is True
    return "对外面 %d 个名字齐全，默认值未变" % len(want)


TESTS = [t_probe_ok, t_probe_exit_nonzero, t_probe_unparsable_and_disabled,
         t_probe_timeout_and_unknown_target, t_remote_path_follows_target,
         t_cmd_override_is_outermost, t_public_surface_unchanged]

if __name__ == "__main__":
    bad = 0
    for fn in TESTS:
        try:
            print("  PASS  %-34s %s" % (fn.__name__, fn()))
        except AssertionError as exc:
            bad += 1
            print("  FAIL  %-34s %s" % (fn.__name__, exc))
    print("-" * 72)
    print("合计 %d 项，通过 %d 项，失败 %d 项" % (len(TESTS), len(TESTS) - bad, bad))
    sys.exit(1 if bad else 0)
