# -*- coding: utf-8 -*-
"""模型富化执行器 + Agent 展示名下沉 L2（agent_model.py）的**函数级等价证据**（可复跑）。

用法：
    python tools/equivalence/todo_enrich_equiv.py [--ref <下沉前的提交>]

      --ref  **源码仓**（本仓，非对外仓）下沉前的提交；该版本 agent-team/chat_svc.py 里的
             `todo_enrich_*` / `display_of` 作「旧实现」，工作树 hub/l2_adapter/agent_model.py
             作「新实现」。默认 4de88cf = 阶段2 刀2 落地后、本刀动手前的那一版。

为什么留着它：本刀把「执行器怎么调」和「展示名」整段搬出 L3，必须证明**零行为改变**。
迁移提交之后，本脚本仍可指向迁移前那个 commit 复跑。

手法（与 a2a_edge_equiv.py 同一套定式，只是粒度是函数不是 HTTP 响应）：
  · `git show <ref>:agent-team/chat_svc.py` 取老版 → `ast` 抽出**老函数原文 + 老模块级常量**
    （不改一个字符）→ `exec` 进自带 globals 的命名空间；
  · 新实现用 importlib **按 env 逐例重新加载**：两侧都有 import 期求值的常量
    （AGENT_TEAM_ETC_DIR / SSH_TARGET / CLAUDE_EXEC / DISPLAY_NAMES 覆盖），
    只有同 env 下双方都重建，比的才是同一件事；
  · `subprocess` 与 `time` 在两侧都换成**同一个**确定性桩：不打桩的话 call() 会真去连
    ssh、elapsed_s 随机器而变，证据就不可复现。

有意差异（全部列出，没有第四条）：
  1. 函数名：`todo_enrich_enabled/timeout/min_interval/argv/parse/call_model`
     → `enabled/timeout_s/min_interval_s/ssh_argv/parse/call`；`display_of` → `display_name`。
     chat_svc 侧留同名薄委托，调用点零改动（脚本最后一步实测）。
  2. `call(prompt, timeout=None)` 比老的 `todo_enrich_call_model(prompt)` 多一个可选形参；
     省略时取 `timeout_s()`，即老口径。本脚本只比省略时的行为。
  3. 常量改名去掉 `ENRICH_` 前缀（模块名已表达归属）；chat_svc 侧保留老名别名。
  **env 名、默认值、ssh 选项顺序、编码口径、降级返回值一个字没动。**
"""
from __future__ import annotations

import argparse
import ast
import contextlib
import importlib.util
import io
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
#: 旧侧基线的**来源仓**：`agent-team/` 时期的提交（改造前的源码）在**源码仓**里，
#: 本仓是整改后的新仓、不含那段历史。env 可覆盖；同级目录兜底；找不到就退回本仓（不静默）。
SRC_REPO = os.environ.get("HACS_SOURCE_REPO") or os.path.join(os.path.dirname(REPO), "多Agent相互协作")
if not os.path.isdir(os.path.join(SRC_REPO, ".git")):
    SRC_REPO = REPO

#: 保留注入：`_old/` 冻结快照与 `_SNIPPET_HEADER` 抽出的老代码段按裁定一字不动，
#: 其中是**裸名** import（`node_paths` / `view_auth` 等）。分层整改后各件归位到包目录，
#: 裸名再也解析不到，故把它们所在目录一并注入**只供旧侧用**；新侧一律走绝对包名。
FLAT_DIRS = [os.path.join(REPO, "node", "l0_host"),        # node_paths
             os.path.join(REPO, "hub", "x")]               # view_auth
NEW_CHAT_SVC = os.path.join(REPO, "hub", "l3_domain", "chat_svc.py")
NEW_AGENT_MODEL = os.path.join(REPO, "hub", "l2_adapter", "agent_model.py")
OLD = os.path.join(HERE, "_old")

#: 老版里要抽的函数（原文照搬）
OLD_FUNCS = ["scrubbed", "todo_enrich_enabled", "todo_enrich_timeout",
             "todo_enrich_min_interval", "todo_enrich_argv", "todo_enrich_parse",
             "todo_enrich_call_model", "display_of"]
#: 老版里要抽的模块级常量（import 期求值的那些，必须跟着 env 一起重建）
OLD_CONSTS = ["AGENT_TEAM_ETC_DIR", "AGENT_TEAM_SSH_TARGET", "AGENT_TEAM_CLAUDE_EXEC",
              "ENRICH_ENABLE_ENV", "ENRICH_SSH_BIN_ENV", "ENRICH_SSH_ENV", "ENRICH_KEY_ENV",
              "ENRICH_KNOWN_ENV", "ENRICH_CMD_ENV", "ENRICH_TIMEOUT_ENV",
              "ENRICH_MIN_INTERVAL_ENV", "ENRICH_SSH_BIN", "ENRICH_SSH_TARGET",
              "ENRICH_KEY_FILE", "ENRICH_KNOWN_HOSTS", "ENRICH_REMOTE_CMD",
              "ENRICH_TIMEOUT_S", "ENRICH_MIN_INTERVAL_S", "ENRICH_NAME_MAX",
              "ENRICH_SUMMARY_MAX", "_DISPLAY_NAMES_FALLBACK", "DISPLAY_NAMES",
              "_dn_override"]

#: 每例开跑前先清空这些 env，再叠加本例的覆盖 —— 否则本机 shell 里的残留会污染证据
ENV_KEYS = ["CHAT_ENRICH_ENABLE", "CHAT_ENRICH_SSH_BIN", "CHAT_ENRICH_SSH",
            "CHAT_ENRICH_KEY", "CHAT_ENRICH_KNOWN_HOSTS", "CHAT_ENRICH_CMD",
            "CHAT_ENRICH_TIMEOUT_S", "CHAT_ENRICH_MIN_INTERVAL_S",
            "AGENT_TEAM_DISPLAY_NAMES", "AGENT_TEAM_ETC_DIR", "AGENT_TEAM_SSH_TARGET",
            "AGENT_TEAM_CLAUDE_EXEC", "AGENT_TEAM_RUNTIME_DIR"]

_SNIPPET_HEADER = """from __future__ import annotations
import json
import os
import re
import subprocess
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from node.l0_host import node_paths as _np
from hub.x import view_auth
"""


# ------------------------------------------------------------------ 取老实现

def materialize_old(ref):
    """`git show <ref>:agent-team/chat_svc.py` → _old/chat_svc_old.py（原文，一字不改）。"""
    if not os.path.isdir(OLD):
        os.makedirs(OLD)
    target = os.path.join(OLD, "chat_svc_old.py")
    raw = subprocess.run(["git", "show", "%s:agent-team/chat_svc.py" % ref],
                         cwd=SRC_REPO, capture_output=True).stdout
    if not raw:
        print("取不到 %s:agent-team/chat_svc.py —— ref 写错了？" % ref)
        sys.exit(2)
    if b"def todo_enrich_argv" not in raw or b"_DISPLAY_NAMES_FALLBACK" not in raw:
        print("警告：%s 的 chat_svc.py 里没有 todo_enrich_argv 或 _DISPLAY_NAMES_FALLBACK，"
              "那它就不是「旧实现」" % ref)
    io.open(target, "wb").write(raw)
    return target


def extract_old(path):
    """ast 抽出老函数原文 + 老模块级常量，按行号拼回一段可 exec 的源码。"""
    src = io.open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    picked = []          # (lineno, 源码段)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in OLD_FUNCS:
            picked.append((node.lineno, ast.get_source_segment(src, node)))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = [t.id for t in targets if isinstance(t, ast.Name)]
            if any(n in OLD_CONSTS for n in names):
                picked.append((node.lineno, ast.get_source_segment(src, node)))
        elif isinstance(node, ast.If):
            seg = ast.get_source_segment(src, node)
            if "DISPLAY_NAMES.update" in seg:      # env 覆盖那个 if 块
                picked.append((node.lineno, seg))
    picked.sort()
    missing = [n for n in OLD_FUNCS if ("def %s(" % n) not in "\n".join(s for _, s in picked)]
    if missing:
        print("✗ 老版里没抽到这些函数：%s" % "、".join(missing))
        sys.exit(2)
    return _SNIPPET_HEADER + "\n\n" + "\n\n".join(seg for _, seg in picked) + "\n"


# ------------------------------------------------------------------ 环境与加载

@contextlib.contextmanager
def env_ctx(overrides):
    """清掉 ENV_KEYS → 叠加本例覆盖 → 退出时原样还原。"""
    saved = {k: os.environ.get(k) for k in ENV_KEYS}
    try:
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        for k, v in (overrides or {}).items():
            os.environ[k] = v
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


_COUNTER = [0]


def load_old(snippet):
    ns = {"__name__": "chat_svc_old_ns"}
    exec(compile(snippet, "<chat_svc@ref>", "exec"), ns)
    return ns


def load_new():
    _COUNTER[0] += 1
    name = "agent_model_fresh_%d" % _COUNTER[0]
    spec = importlib.util.spec_from_file_location(name, NEW_AGENT_MODEL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ------------------------------------------------------------------ 确定性桩

FROZEN_T0 = 1758758400.0
FROZEN_T1 = 1758758401.234        # call() 里 elapsed_s = round(t1 - t0, 2) = 1.23


class FakeTime(object):
    """时钟桩：第一次 time.time() 返回 t0，之后一律 t1（elapsed_s 两侧必相等）。"""

    def __init__(self):
        self.n = 0

    def time(self):
        self.n += 1
        return FROZEN_T0 if self.n == 1 else FROZEN_T1


class _Proc(object):
    def __init__(self, rc, out, err):
        self.returncode, self.stdout, self.stderr = rc, out, err


class FakeSub(object):
    """subprocess 桩：真异常类（让 except 分支照常命中）+ 按剧本返回，并留存入参。"""

    PIPE = subprocess.PIPE
    TimeoutExpired = subprocess.TimeoutExpired

    def __init__(self, script):
        self.script = script
        self.seen = []

    def run(self, argv, input=None, stdout=None, stderr=None, timeout=None):
        self.seen.append((list(argv), input, timeout, stdout, stderr))
        kind = self.script[0]
        if kind == "timeout":
            raise subprocess.TimeoutExpired(argv[0], timeout)
        if kind == "missing":
            raise FileNotFoundError(2, "No such file or directory")
        if kind == "boom":
            raise OSError("spawn failed dsn=host=1.2.3.4 password=hunter2")
        return _Proc(self.script[1], self.script[2], self.script[3])


def patch(ns_or_mod, sub, clk):
    if isinstance(ns_or_mod, dict):
        ns_or_mod["subprocess"] = sub
        ns_or_mod["time"] = clk
    else:
        ns_or_mod.subprocess = sub
        ns_or_mod.time = clk


# ------------------------------------------------------------------ 冻结输入矩阵

_LONG_NAME = "超长任务名" * 20                     # 100 字，必被 NAME_MAX=18 截
_LONG_SUMMARY = "细节描述" * 40                    # 160 字，必被 SUMMARY_MAX=80 截

#: ssh_argv()：默认 / 逐个 env 覆盖 / 空串 / 含空格 / 非法值
SSH_CASES = [
    ("默认（全不设）", {}),
    ("SSH_BIN 覆盖", {"CHAT_ENRICH_SSH_BIN": "/opt/bin/ssh"}),
    ("SSH 目标覆盖", {"CHAT_ENRICH_SSH": "user@other"}),
    ("KEY 覆盖", {"CHAT_ENRICH_KEY": "/tmp/k1"}),
    ("KNOWN_HOSTS 覆盖", {"CHAT_ENRICH_KNOWN_HOSTS": "/tmp/kh"}),
    ("CMD 覆盖", {"CHAT_ENRICH_CMD": "python3 -X utf8 /tmp/x.py -"}),
    ("六项全覆盖", {"CHAT_ENRICH_SSH_BIN": "ssh", "CHAT_ENRICH_SSH": "a@b",
                    "CHAT_ENRICH_KEY": "/k", "CHAT_ENRICH_KNOWN_HOSTS": "/kh",
                    "CHAT_ENRICH_CMD": "cmd -", "CHAT_ENRICH_ENABLE": "1"}),
    ("SSH_BIN 空串 → 回默认", {"CHAT_ENRICH_SSH_BIN": ""}),
    ("KEY 空串 → 回默认", {"CHAT_ENRICH_KEY": ""}),
    ("KEY 纯空白 → 回默认", {"CHAT_ENRICH_KEY": "   "}),
    ("KNOWN 纯空白 → 回默认", {"CHAT_ENRICH_KNOWN_HOSTS": "\t \n"}),
    ("SSH 目标空串 → 回默认", {"CHAT_ENRICH_SSH": ""}),
    ("CMD 空串 → 回默认", {"CHAT_ENRICH_CMD": ""}),
    ("值含空格（两端会被 strip）", {"CHAT_ENRICH_SSH": "  u@h  ",
                                     "CHAT_ENRICH_KEY": "  /a b/k  "}),
    ("值中间含空格（保留）", {"CHAT_ENRICH_SSH_BIN": "/a b/ssh",
                              "CHAT_ENRICH_CMD": "py -X utf8 /a b/x.py -"}),
    ("非法值：换行/引号/分号", {"CHAT_ENRICH_KEY": "/k\n-oProxyCommand=rm -rf /",
                                 "CHAT_ENRICH_SSH": 'a@b";id;"'}),
    ("非法值：纯符号", {"CHAT_ENRICH_SSH_BIN": "!!", "CHAT_ENRICH_KNOWN_HOSTS": "??"}),
    ("宿主 ETC 目录改道（影响 KEY/KNOWN 默认）", {"AGENT_TEAM_ETC_DIR": "/srv/cfg"}),
    ("宿主 SSH 目标改道", {"AGENT_TEAM_SSH_TARGET": "bob@nb"}),
    ("宿主 claude_exec 改道（影响远端命令行）", {"AGENT_TEAM_CLAUDE_EXEC": "/srv/ce.py"}),
]

#: enabled()：0 / 负数 / 大写 OFF / 非数字 / 未设置
ENABLE_CASES = [
    ("未设置", {}), ("=1", {"CHAT_ENRICH_ENABLE": "1"}), ("=0", {"CHAT_ENRICH_ENABLE": "0"}),
    ("=false", {"CHAT_ENRICH_ENABLE": "false"}), ("=FALSE", {"CHAT_ENRICH_ENABLE": "FALSE"}),
    ("=no", {"CHAT_ENRICH_ENABLE": "no"}), ("=OFF", {"CHAT_ENRICH_ENABLE": "OFF"}),
    ("=Off", {"CHAT_ENRICH_ENABLE": "Off"}), ("=空串", {"CHAT_ENRICH_ENABLE": ""}),
    ("=空白", {"CHAT_ENRICH_ENABLE": "   "}), ("=' 0 '", {"CHAT_ENRICH_ENABLE": " 0 "}),
    ("=-1", {"CHAT_ENRICH_ENABLE": "-1"}), ("=2", {"CHAT_ENRICH_ENABLE": "2"}),
    ("=yes", {"CHAT_ENRICH_ENABLE": "yes"}), ("=abc", {"CHAT_ENRICH_ENABLE": "abc"}),
    ("=true", {"CHAT_ENRICH_ENABLE": "true"}),
]

#: timeout_s() / min_interval_s()：0 / 负数 / 非数字 / 未设置 / 越界
NUM_CASES = [
    ("未设置", {}), ("=空串", {"V": ""}), ("=空白", {"V": "  "}), ("=0", {"V": "0"}),
    ("=-5", {"V": "-5"}), ("=-0.5", {"V": "-0.5"}), ("=5", {"V": "5"}),
    ("=4.9", {"V": "4.9"}), ("=120", {"V": " 120 "}), ("=1800", {"V": "1800"}),
    ("=3600（越上限）", {"V": "3600"}), ("=1e9", {"V": "1e9"}), ("=abc", {"V": "abc"}),
    ("=OFF", {"V": "OFF"}), ("=inf", {"V": "inf"}), ("=nan", {"V": "nan"}),
    ("=2.5", {"V": "2.5"}), ("=十", {"V": "十"}),
]

#: parse()：合法 / 围栏 / 缺字段 / 超长截断 / 非 JSON / 空串 / 非 dict
PARSE_CASES = [
    ("合法 JSON", '{"is_task": true, "task_name": "修队列", "summary": "改掉", "confidence": 0.9}'),
    ("``` 围栏包裹", '```json\n{"is_task": true, "task_name": "修队列", "confidence": 0.5}\n```'),
    ("前后带解释文字", '好的，结论如下：{"is_task": false, "confidence": 0.2} 以上。'),
    ("is_task 字符串 true", '{"is_task": "True", "task_name": "甲"}'),
    ("is_task 字符串 是", '{"is_task": "是", "task_name": "乙"}'),
    ("is_task 字符串 y", '{"is_task": "y", "task_name": "丙"}'),
    ("is_task 字符串 maybe", '{"is_task": "maybe", "task_name": "丁"}'),
    ("is_task=1（数字）", '{"is_task": 1, "task_name": "戊"}'),
    ("is_task=0（数字）", '{"is_task": 0}'),
    ("is_task=null", '{"is_task": null}'),
    ("缺 is_task 字段", '{"task_name": "没有开关"}'),
    ("缺 task_name 但 is_task=true", '{"is_task": true, "summary": "只有摘要"}'),
    ("task_name 只有标点", '{"is_task": true, "task_name": "，。：（）"}'),
    ("task_name 带首尾标点", '{"is_task": true, "task_name": "「修队列」。"}'),
    ("task_name 含换行/多空格", '{"is_task": true, "task_name": "修  \\n 队列"}'),
    ("超长 task_name（测 NAME_MAX）", '{"is_task": true, "task_name": "%s"}' % _LONG_NAME),
    ("超长 summary（测 SUMMARY_MAX）",
     '{"is_task": false, "summary": "%s"}' % _LONG_SUMMARY),
    ("超长原文（测 raw 600 截断）", '{"is_task": false} ' + "尾" * 800),
    ("confidence 字符串", '{"is_task": false, "confidence": "0.7"}'),
    ("confidence 非数字", '{"is_task": false, "confidence": "高"}'),
    ("confidence 缺失", '{"is_task": false}'),
    ("confidence 越界 1.7", '{"is_task": false, "confidence": 1.7}'),
    ("confidence 负数", '{"is_task": false, "confidence": -3}'),
    ("非 JSON 文本", "这不是 JSON，只是一句话。"),
    ("坏 JSON（缺引号）", "{is_task: true}"),
    ("空串", ""),
    ("纯空白", "   \n\t "),
    ("None", None),
    ("非 dict：数组", "[1, 2, 3]"),
    ("非 dict：被大括号包的数字", "{123}"),
    ("只有左括号", "{"),
    ("右括号在左括号前", "} 什么 {"),
    ("嵌套对象", '{"is_task": true, "task_name": "甲", "extra": {"a": 1}}'),
    ("数字 0（非字符串入参）", 0),
    ("dict 入参（会被 str()）", {"is_task": True}),
]

#: display_name()：表内 / env 覆盖 / 未知 id / 空串 / None
DISPLAY_CASES = [
    ("表内 lobster", {}, "lobster"),
    ("表内 openclaw-nb", {}, "openclaw-nb"),
    ("表内 <human-id>", {}, "<human-id>"),
    ("未知 id", {}, "nobody"),
    ("空串", {}, ""),
    ("None", {}, None),
    ("0（假值）", {}, 0),
    ("env 覆盖已有键", {"AGENT_TEAM_DISPLAY_NAMES": '{"lobster": "大虾"}'}, "lobster"),
    ("env 覆盖新增键", {"AGENT_TEAM_DISPLAY_NAMES": '{"newbie": "新人"}'}, "newbie"),
    ("env 覆盖成空串 → 回落 id", {"AGENT_TEAM_DISPLAY_NAMES": '{"lobster": ""}'}, "lobster"),
    ("env 覆盖不影响其它键", {"AGENT_TEAM_DISPLAY_NAMES": '{"newbie": "新人"}'}, "codex"),
    ("env 空 JSON 对象", {"AGENT_TEAM_DISPLAY_NAMES": "{}"}, "lobster"),
    ("env 空串（不触发覆盖）", {"AGENT_TEAM_DISPLAY_NAMES": ""}, "lobster"),
]

#: call()：降级路径逐条（全部只许返回 (None, 原因)）
_OK_JSON = b'{"is_task": true, "task_name": "\xe4\xbf\xae\xe9\x98\x9f\xe5\x88\x97", "confidence": 0.8}'
CALL_CASES = [
    ("正常返回 ok", {}, ("ok", 0, _OK_JSON, b"")),
    ("被开关关掉 → disabled", {"CHAT_ENRICH_ENABLE": "0"}, ("ok", 0, _OK_JSON, b"")),
    ("超时（默认 240）", {}, ("timeout",)),
    ("超时（env 改 7s）", {"CHAT_ENRICH_TIMEOUT_S": "7"}, ("timeout",)),
    ("超时（env 非法 → 回默认）", {"CHAT_ENRICH_TIMEOUT_S": "abc"}, ("timeout",)),
    ("ssh 不存在", {}, ("missing",)),
    ("ssh 不存在（换 SSH_BIN）", {"CHAT_ENRICH_SSH_BIN": "/no/such/ssh"}, ("missing",)),
    ("spawn 其它异常（含口令，测脱敏）", {}, ("boom",)),
    ("退出码非 0 + stderr", {}, ("ok", 255, b"", b"Permission denied (publickey).")),
    ("退出码非 0 + 只有 stdout", {}, ("ok", 1, b"boom on remote", b"")),
    ("退出码非 0 + 超长 stderr（测 -200 截断）", {}, ("ok", 2, b"", b"E" * 500)),
    ("输出不是 JSON → unparsable", {}, ("ok", 0, b"I cannot help with that.", b"")),
    ("输出超长非 JSON（测 -200 截断）", {}, ("ok", 0, "不行".encode("utf-8") * 300, b"")),
    ("输出空 → unparsable", {}, ("ok", 0, b"   ", b"")),
    ("输出非 utf8 字节（测 replace 口径）", {}, ("ok", 0, b'{"is_task": true, "task_name": "\xc4\xe3"}', b"")),
    ("输出 stdout=None", {}, ("ok", 0, None, None)),
    ("判是任务却没名字 → unparsable", {}, ("ok", 0, b'{"is_task": true}', b"")),
]


def norm(value):
    """比对口径：一律取 repr —— float('nan') 也能稳定比（repr 两侧都是 'nan'）。"""
    return repr(value)


def run_all(snippet):
    rows = []      # (分组, 用例, 旧, 新)

    def add(group, label, env, old_fn, new_fn):
        with env_ctx(env):
            old = load_old(snippet)
            new = load_new()
            a = old_fn(old)
            b = new_fn(new)
        rows.append((group, label, norm(a), norm(b)))

    for label, env in SSH_CASES:
        add("ssh_argv", label, env,
            lambda o: o["todo_enrich_argv"](), lambda n: n.ssh_argv())

    for label, env in ENABLE_CASES:
        add("enabled", label, env,
            lambda o: o["todo_enrich_enabled"](), lambda n: n.enabled())

    for label, env in NUM_CASES:
        e = {"CHAT_ENRICH_TIMEOUT_S": env["V"]} if "V" in env else {}
        add("timeout_s", label, e,
            lambda o: o["todo_enrich_timeout"](), lambda n: n.timeout_s())

    for label, env in NUM_CASES:
        e = {"CHAT_ENRICH_MIN_INTERVAL_S": env["V"]} if "V" in env else {}
        add("min_interval_s", label, e,
            lambda o: o["todo_enrich_min_interval"](), lambda n: n.min_interval_s())

    for label, raw in PARSE_CASES:
        add("parse", label, {},
            lambda o, r=raw: o["todo_enrich_parse"](r), lambda n, r=raw: n.parse(r))

    for label, env, mid in DISPLAY_CASES:
        add("display_name", label, env,
            lambda o, m=mid: o["display_of"](m), lambda n, m=mid: n.display_name(m))

    for label, env, script in CALL_CASES:
        def old_fn(o, s=script):
            sub, clk = FakeSub(s), FakeTime()
            patch(o, sub, clk)
            return (o["todo_enrich_call_model"]("提示词 prompt"), sub.seen)

        def new_fn(n, s=script):
            sub, clk = FakeSub(s), FakeTime()
            patch(n, sub, clk)
            return (n.call("提示词 prompt"), sub.seen)

        add("call", label, env, old_fn, new_fn)

    return rows


# ------------------------------------------------------------------ 接线后复验

def check_wiring():
    """import **真实生产模块** chat_svc，确认「搬走实现、调用面不变」。"""
    for _p in FLAT_DIRS + [REPO]:
        if _p not in sys.path:
            sys.path.insert(0, _p)
    from hub.l3_domain import chat_svc as cs                                # noqa: E402
    from hub.l2_adapter import agent_model as am                                 # noqa: E402

    checks = []

    def ck(name, ok, detail=""):
        checks.append((name, bool(ok), detail))

    ck("chat_svc 可 import（真实生产模块，无桩）", True)
    ck("todo_enrich_argv() 与 agent_model.ssh_argv() 逐项相同",
       cs.todo_enrich_argv() == am.ssh_argv())
    ck("todo_enrich_enabled/timeout/min_interval 三者委托一致",
       (cs.todo_enrich_enabled(), cs.todo_enrich_timeout(), cs.todo_enrich_min_interval())
       == (am.enabled(), am.timeout_s(), am.min_interval_s()))
    ck("todo_enrich_parse 委托一致",
       cs.todo_enrich_parse('{"is_task": true, "task_name": "甲"}')
       == am.parse('{"is_task": true, "task_name": "甲"}'))
    ck("display_of / display_name 同一实现",
       cs.display_of("lobster") == am.display_name("lobster") == "龙虾"
       and cs.display_name("nobody") == "nobody" and cs.display_of("") == "")
    ck("DISPLAY_NAMES 是同一个 dict 对象（既有下标用法照旧）",
       cs.DISPLAY_NAMES is am.DISPLAY_NAMES)
    ck("8 个 env 名在 chat_svc 侧别名不变",
       [cs.ENRICH_ENABLE_ENV, cs.ENRICH_SSH_BIN_ENV, cs.ENRICH_SSH_ENV, cs.ENRICH_KEY_ENV,
        cs.ENRICH_KNOWN_ENV, cs.ENRICH_CMD_ENV, cs.ENRICH_TIMEOUT_ENV,
        cs.ENRICH_MIN_INTERVAL_ENV]
       == ["CHAT_ENRICH_ENABLE", "CHAT_ENRICH_SSH_BIN", "CHAT_ENRICH_SSH", "CHAT_ENRICH_KEY",
           "CHAT_ENRICH_KNOWN_HOSTS", "CHAT_ENRICH_CMD", "CHAT_ENRICH_TIMEOUT_S",
           "CHAT_ENRICH_MIN_INTERVAL_S"])
    ck("默认值别名不变（240/1.0/18/80/ssh）",
       (cs.ENRICH_TIMEOUT_S, cs.ENRICH_MIN_INTERVAL_S, cs.ENRICH_NAME_MAX,
        cs.ENRICH_SUMMARY_MAX, cs.ENRICH_SSH_BIN) == (240.0, 1.0, 18, 80, "/usr/bin/ssh"))
    ck("L3 仍持有提示词模板（业务口径没被搬走）",
       "TODO_ENRICH_PROMPT" in dir(cs) and "群名：" in cs.TODO_ENRICH_PROMPT
       and not hasattr(am, "TODO_ENRICH_PROMPT"))
    ck("L3 仍持有队列 worker / 提交口（没被搬走）",
       all(hasattr(cs, x) for x in ("TodoEnricher", "todo_enrich_submit",
                                    "todo_enrich_seq_of", "todo_enrich_is_agent",
                                    "todo_enrich_job")))
    src = io.open(NEW_CHAT_SVC, encoding="utf-8").read()
    ck("L3 里已无 subprocess 调用（执行器机制确实搬走了）",
       "subprocess.run" not in src and "import subprocess" not in src)
    ck("L3 里已无反向 SSH 目标字面量", "<user>@<node>" not in src)
    am_src = io.open(NEW_AGENT_MODEL, encoding="utf-8").read()
    ck("L2 不 import 任何上层件（chat_svc / a2a_* / *_svc）",
       not any(("import %s" % m) in am_src
               for m in ("chat_svc", "a2a_edge", "a2a_svc", "web_gateway",
                         "blackboard_svc", "task_svc")))
    return checks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", default="4de88cf", help="下沉前的 commit/ref")
    args = ap.parse_args()

    for _p in FLAT_DIRS + [REPO]:
        if _p not in sys.path:
            sys.path.insert(0, _p)
    path = materialize_old(args.ref)
    snippet = extract_old(path)
    io.open(os.path.join(OLD, "chat_svc_enrich_snippet.py"), "w",
            encoding="utf-8").write(snippet)     # 留档：比的到底是哪段老代码

    rows = run_all(snippet)
    bad = 0
    group = None
    print("旧 = %s:agent-team/chat_svc.py（ast 抽原文）   新 = 工作树 hub/l2_adapter/agent_model.py\n"
          % args.ref)
    for g, label, a, b in rows:
        if g != group:
            group = g
            print("── %s ──" % g)
        ok = a == b
        bad += 0 if ok else 1
        if ok:
            print("  ✓ %-36s %s" % (label, a if len(a) <= 92 else a[:89] + "..."))
        else:
            print("  ✗ %-36s **不一致**\n      旧=%s\n      新=%s" % (label, a, b))
    print("\n对照矩阵：合计 %d 例，一致 %d 例，不一致 %d 例"
          % (len(rows), len(rows) - bad, bad))

    print("\n── 接线后复验（import 真实生产模块 chat_svc）──")
    checks = check_wiring()
    wrong = 0
    for name, ok, _detail in checks:
        wrong += 0 if ok else 1
        print("  %s %s" % ("✓" if ok else "✗", name))
    print("\n接线复验：合计 %d 项，通过 %d 项，失败 %d 项"
          % (len(checks), len(checks) - wrong, wrong))
    return 1 if (bad or wrong) else 0


if __name__ == "__main__":
    sys.exit(main())
