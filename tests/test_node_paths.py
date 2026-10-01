#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""L0 `node_paths` 回归断言 + 「刀 3」改造成果守卫。

两件事：
  ① 平台语义：`nt` / `posix` 各拿到**自己的惯例位置**（不许串台），env 覆盖优先；
  ② 改造守卫：已改造的模块里**不许再残留宿主路径字面量**（字面量只许待在 `node_paths.py`）。
"""

import io
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)  # 仓根：node.l0_host 绝对包名 import 需要

from node.l0_host import node_paths as np  # noqa: E402

ENVS = [np.ENV_RUNTIME_DIR, np.ENV_ETC_DIR, np.ENV_LOG_DIR, np.ENV_NODE_EXE]

# 已按「刀 3」改造的模块（相对 agent-team/）。残留字面量 = 回归。
REFACTORED = ["cli_backend.py", "teamd.py", "team.py", "group_watch.py",
              "group_transcript_archiver.py", "chat_svc.py", "blackboard_svc.py",
              "blackboard_view.py", "roundtable.py", "rt_remind.py", "view_auth.py",
              "shared_content.py", "task_svc.py", "snap_portal.py",
              "verify_portal_public.py", "agent_usage.py"]

LITERALS = [r"<deploy-dir>", "<deploy-dir>", "<deploy-dir>", "<deploy-dir>",
            r"<home>"]


def _clear_env():
    saved = {k: os.environ.pop(k, None) for k in ENVS}
    return saved


def _restore(saved):
    for k, v in saved.items():
        if v is not None:
            os.environ[k] = v


def t_platform_semantics():
    """两个平台各拿自己的惯例位置，互不串台。"""
    s = _clear_env()
    try:
        # 方案 a（2026-10-01）：源码树里部署面字面量是**脱敏占位符**（真值在部署面）⇒ 退到开发兜底。
        # 两面都测，守卫价值不降：① 占位符状态 ⇒ 已退（不停在占位符本身）、nt 落点是已存在目录
        #                        ② 注入真值 ⇒ 两平台**逐字透传**（线上口径）
        assert not np.runtime_dir("nt").startswith("<"), np.runtime_dir("nt")
        assert not np.runtime_dir("posix").startswith("<"), np.runtime_dir("posix")
        assert os.path.isdir(np.runtime_dir("nt")), np.runtime_dir("nt")
        assert np.etc_dirs("nt") == [np.runtime_dir("nt")], np.etc_dirs("nt")
        assert np.etc_dirs("posix") == [np.runtime_dir("posix")], np.etc_dirs("posix")
        # log_dir 必须在**注入 env 之前**比：它取 runtime_dir(plat)（env 优先），
        # 注入后两边口径就不一致了（一边走 env、一边是模块级默认）。
        assert np.log_dir("posix") == os.path.join(np.POSIX_RUNTIME_DIR, "logs"), np.log_dir("posix")
        os.environ["AGENT_TEAM_RUNTIME_DIR"] = r"C:\hacs-probe-rt"
        os.environ["AGENT_TEAM_ETC_DIR"] = r"C:\hacs-probe-etc"
        assert np.log_dir("posix") == os.path.join(r"C:\hacs-probe-rt", "logs"), np.log_dir("posix")
        assert np.runtime_dir("nt") == r"C:\hacs-probe-rt", np.runtime_dir("nt")
        assert np.runtime_dir("posix") == r"C:\hacs-probe-rt", np.runtime_dir("posix")
        assert np.etc_dirs("nt") == [r"C:\hacs-probe-etc"], np.etc_dirs("nt")
        assert np.etc_dirs("posix") == [r"C:\hacs-probe-etc"], np.etc_dirs("posix")
        assert np.node_exe("nt").endswith("node.exe"), np.node_exe("nt")
        assert np.node_exe("posix") == "node", np.node_exe("posix")
        assert np.nats_dir("posix") == "/etc/nats", np.nats_dir("posix")
    finally:
        _restore(s)
    return "nt/posix 各归其位"


def t_env_wins():
    """env 覆盖优先于平台默认值（这是配置零硬编码的落点）。"""
    s = _clear_env()
    try:
        os.environ[np.ENV_RUNTIME_DIR] = "/tmp/rt"
        os.environ[np.ENV_ETC_DIR] = "/tmp/etc"
        os.environ[np.ENV_LOG_DIR] = "/tmp/log"
        os.environ[np.ENV_NODE_EXE] = "/usr/bin/node20"
        for fl in ("nt", "posix"):
            assert np.runtime_dir(fl) == "/tmp/rt", np.runtime_dir(fl)
            assert np.etc_dirs(fl) == ["/tmp/etc"], np.etc_dirs(fl)
            assert np.log_dir(fl) == "/tmp/log", np.log_dir(fl)
            assert np.node_exe(fl) == "/usr/bin/node20", np.node_exe(fl)
    finally:
        _restore(s)
    return "env 覆盖四个键，两平台一致生效"


def t_local_equivalence():
    """零行为改变锚点：本机（Windows）默认解析值必须等于改造前的字面量。"""
    s = _clear_env()
    try:
        if os.name == "nt":
            # 方案 a（2026-10-01）：源码树里部署面字面量是**脱敏占位符**（真值在部署面），
            # 占位符语义 = 「未配置」⇒ 源码件自身退到开发兜底目录（必须**已存在**，因本件「不建目录」）。
            # 锚点两面都测，守卫价值不降：
            #   ① 占位符状态 ⇒ 必须已退（不得停在占位符本身），且落点是**已存在**的目录
            #   ② 注入真值 ⇒ **逐字透传**（这条才是「零行为改变」那句的本体：线上走真值分支）
            assert not np.WIN_RUNTIME_DIR.startswith("<"), np.WIN_RUNTIME_DIR
            assert os.path.isdir(np.WIN_RUNTIME_DIR), np.WIN_RUNTIME_DIR
            assert np.runtime_dir() == np.WIN_RUNTIME_DIR, np.runtime_dir()
            assert np.etc_dirs("nt") == [np.WIN_RUNTIME_DIR], np.etc_dirs()
            os.environ["AGENT_TEAM_RUNTIME_DIR"] = r"C:\hacs-probe-rt"
            os.environ["AGENT_TEAM_ETC_DIR"] = r"C:\hacs-probe-etc"
            assert np.runtime_dir() == r"C:\hacs-probe-rt", np.runtime_dir()
            assert np.etc_dirs("nt") == [r"C:\hacs-probe-etc"], np.etc_dirs()
            assert np.node_exe().endswith(r"node-v22.23.2-win-x64\node.exe"), np.node_exe()
            # 下面四条是「每用户工具」的改造锚点：默认值必须与改造前字面量逐字相同
            assert np.claude_exec() == os.path.join(os.path.expanduser("~"), "AppData", "Local",
                                                    "hermes", "scripts", "claude_exec.py"), np.claude_exec()
            assert np.claude_workdir() == os.path.join(os.path.expanduser("~"), "claude-workspace"), np.claude_workdir()
            assert np.codex_exe() == os.path.join(os.path.expanduser("~"), ".codex", "packages",
                                                  "standalone", "current", "bin", "codex.exe"), np.codex_exe()
            assert np.codex_cmd() == os.path.join(os.path.expanduser("~"), ".local", "bin", "codex.cmd"), np.codex_cmd()
    finally:
        _restore(s)
    return "本机默认值与改造前字面量一致"


def t_no_leftover_literals():
    """改造守卫：已改造模块里不许再出现宿主字面量（字面量只许在 node_paths.py）。"""
    bad = []
    trip = re.compile(r'"""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\'')
    for rel in REFACTORED:
        p = os.path.join(ROOT, rel)
        if not os.path.isfile(p):
            continue
        text = trip.sub(lambda m: "\n" * m.group(0).count("\n"), io.open(p, encoding="utf-8").read())
        for i, line in enumerate(text.splitlines(), 1):
            s = line.strip()
            if s.startswith("#") or s.startswith("*") or s.startswith('"') or s.startswith("'"):
                continue                      # 注释/文档串不算代码硬编码
            for lit in LITERALS:
                if lit in line and "node_paths" not in line and "AGENT_TEAM" not in line:
                    bad.append("%s:%d %s" % (rel, i, s[:70]))
    assert not bad, "仍残留宿主字面量：\n    " + "\n    ".join(bad)
    return "%d 个已改造模块零残留" % len(REFACTORED)


def t_target_profile():
    """目标主机档案（刀 4）：user@host / 裸 host 同解；四台各归其平台；未登记不编造。"""
    saved = os.environ.pop(np.ENV_TARGET_PROFILE_JSON, None)
    try:
        # 主机名必须是**档案表里登记的那个**（`thinkbook`）：`target_host()` 只取 `@` 右边查表，
        # 用户名不参与 —— 所以左边留 `<user>` 占位无妨，右边写成 `<node>` 就会查表落空、
        # 退 posix 兜底，这条 nt 用例便永远量不到自己要量的东西。（首版起就这么写着。）
        for host in ("<user>@thinkbook", "thinkbook", "THINKBOOK", " thinkbook "):
            assert np.target_known(host) is True, host
            assert np.target_flavour(host) == "nt", host
            assert np.target_claude_exec(host) == \
                "<home>/AppData/Local/hermes/scripts/claude_exec.py", host
        for host in ("lobster", "root@lobster", "quantum", "top001"):
            assert np.target_known(host) is True, host
            assert np.target_flavour(host) == "posix", host
            assert np.target_home(host) == "/root", host
            # posix 目标的执行器路径由**它自己的**家目录拼出，所以是 `/root/.local/...`。
            # 这里原先写 `<home>/.local/...` —— 那是 nt 侧占位形态，与上一条
            # `target_home == "/root"` 自相矛盾（同一循环、同一 host），恒不可满足。
            assert np.target_claude_exec(host) == \
                "/root/.local/share/hermes/scripts/claude_exec.py", host
        # 未登记 / 空串：按 posix 兜底，但 target_known 必须说实话
        for host in ("nosuchhost", "user@nosuchhost", "", None, "   "):
            assert np.target_known(host) is False, host
            assert np.target_flavour(host) == "posix", host
            # 未登记 → 按 posix 兜底，兜底家目录就是 `/root`（同上），所以是 `/root/` 起头。
            # 原先写 `<home>/` —— 又一次把 nt 占位形态用在了 posix 分支上。
            assert np.target_claude_exec(host).startswith("/root/"), host
    finally:
        if saved is not None:
            os.environ[np.ENV_TARGET_PROFILE_JSON] = saved
    return "四台归位 + user@host 同解 + 未登记不编造"


def t_target_profile_env_override():
    """`AGENT_TEAM_TARGET_PROFILE_JSON` 整表覆盖；缺键按 flavour 惯例补齐；坏 JSON 退回内置表。"""
    saved = os.environ.pop(np.ENV_TARGET_PROFILE_JSON, None)
    try:
        os.environ[np.ENV_TARGET_PROFILE_JSON] = json.dumps({
            "thinkbook": {"flavour": "posix", "home": "/srv/tb",
                          "claude_exec": "/srv/tb/bin/claude_exec.py"},
            "lobster": {"flavour": "posix", "home": "/data/nbhome"},
            "newbox": {"flavour": "nt"},
        })
        assert np.target_flavour("thinkbook") == "posix", np.target_flavour("thinkbook")
        assert np.target_claude_exec("thinkbook") == "/srv/tb/bin/claude_exec.py"
        # 缺 claude_exec → 按覆盖后的 home 补齐
        assert np.target_claude_exec("lobster") == \
            "/data/nbhome/.local/share/hermes/scripts/claude_exec.py", np.target_claude_exec("lobster")
        # 缺 home/claude_exec → 按 flavour 惯例补齐
        assert np.target_flavour("newbox") == "nt"
        assert np.target_claude_exec("newbox") == \
            "<home>/AppData/Local/hermes/scripts/claude_exec.py", np.target_claude_exec("newbox")
        # **整表**覆盖：内置表里有、覆盖表里没有的，变成未登记
        assert np.target_known("quantum") is False, "整表覆盖没生效"
        # 坏 JSON 不许抛，退回内置表
        os.environ[np.ENV_TARGET_PROFILE_JSON] = "{不是 JSON"
        assert np.target_known("quantum") is True, "坏 JSON 该退回内置表"
        assert np.target_flavour("thinkbook") == "nt"
    finally:
        os.environ.pop(np.ENV_TARGET_PROFILE_JSON, None)
        if saved is not None:
            os.environ[np.ENV_TARGET_PROFILE_JSON] = saved
    return "整表覆盖 / 缺键补齐 / 坏 JSON 退回内置表"


def t_local_vs_target_are_separate():
    """本机真值与目标主机真值是两套：既有 `claude_exec()` 的签名与返回值一字未改。"""
    s = _clear_env()
    saved = os.environ.pop(np.ENV_CLAUDE_EXEC, None)
    try:
        if os.name == "nt":
            assert np.claude_exec() == os.path.join(os.path.expanduser("~"), "AppData", "Local",
                                                    "hermes", "scripts", "claude_exec.py"), np.claude_exec()
        # posix 语义下本机值与「目标是 thinkbook」的值必须不同 —— 这正是缺陷的形状
        assert np.claude_exec("posix") != np.target_claude_exec("thinkbook")
        assert np.claude_exec("posix").endswith(".local/share/hermes/scripts/claude_exec.py".replace("/", os.sep)) \
            or np.claude_exec("posix").endswith(".local/share/hermes/scripts/claude_exec.py"), np.claude_exec("posix")
    finally:
        if saved is not None:
            os.environ[np.ENV_CLAUDE_EXEC] = saved
        _restore(s)
    return "claude_exec() 未改；本机值 ≠ 目标值"


TESTS = [t_platform_semantics, t_env_wins, t_local_equivalence, t_no_leftover_literals,
         t_target_profile, t_target_profile_env_override, t_local_vs_target_are_separate]

if __name__ == "__main__":
    bad = 0
    for fn in TESTS:
        try:
            print("  PASS  %-24s %s" % (fn.__name__, fn()))
        except AssertionError as exc:
            bad += 1
            print("  FAIL  %-24s %s" % (fn.__name__, exc))
    print("-" * 60)
    print("合计 %d 项，通过 %d 项，失败 %d 项" % (len(TESTS), len(TESTS) - bad, bad))
    sys.exit(1 if bad else 0)
