#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""L0 `node_paths` 回归断言 + 「刀 3」改造成果守卫。

两件事：
  ① 平台语义：`nt` / `posix` 各拿到**自己的惯例位置**（不许串台），env 覆盖优先；
  ② 改造守卫：已改造的模块里**不许再残留宿主路径字面量**（字面量只许待在 `node_paths.py`）。
"""

import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import node_paths as np  # noqa: E402

ENVS = [np.ENV_RUNTIME_DIR, np.ENV_ETC_DIR, np.ENV_LOG_DIR, np.ENV_NODE_EXE]

# 已按「刀 3」改造的模块（相对 agent-team/）。残留字面量 = 回归。
REFACTORED = ["cli_backend.py", "teamd.py", "team.py", "group_watch.py",
              "group_transcript_archiver.py", "chat_svc.py", "blackboard_svc.py",
              "blackboard_view.py", "roundtable.py", "rt_remind.py", "view_auth.py",
              "shared_content.py", "task_svc.py", "snap_portal.py",
              "verify_portal_public.py", "agent_usage.py"]

LITERALS = [r"<home>\agent-team", "<home>/agent-team", "<deploy-dir>", "<deploy-dir>",
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
        assert np.runtime_dir("nt") == r"<home>\agent-team", np.runtime_dir("nt")
        assert np.runtime_dir("posix") == "<deploy-dir>", np.runtime_dir("posix")
        assert np.etc_dirs("nt") == [r"<home>\agent-team"], np.etc_dirs("nt")
        assert np.etc_dirs("posix") == ["<deploy-dir>"], np.etc_dirs("posix")
        assert np.node_exe("nt").endswith("node.exe"), np.node_exe("nt")
        assert np.node_exe("posix") == "node", np.node_exe("posix")
        assert np.log_dir("posix") == os.path.join("<deploy-dir>", "logs"), np.log_dir("posix")
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
            assert np.runtime_dir() == r"<home>\agent-team", np.runtime_dir()
            assert np.etc_dirs("nt") == [r"<home>\agent-team"], np.etc_dirs()
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


TESTS = [t_platform_semantics, t_env_wins, t_local_equivalence, t_no_leftover_literals]

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
