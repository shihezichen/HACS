#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T 工具面：**节点守护单元的生成与核对** —— 「各机单元名不统一」这件事的机械答案。

为什么要它
----------
同一个守护，lobster 叫 `a2a-svc`、thinkbook 叫 `AgentTeamA2ASvc`、quantum 又叫
`agent-team-a2a` —— 这不是谁手滑，是**没有任何一处声明过「它该叫什么」**，于是每台机
各自按当时顺手的写法建了一遍。结果就是：想知道「四台机上这个东西都活着吗」，得先想起来
它在每台机上分别叫什么。

刀 23 不迁存量（那些名字一个不动），但**新增的轻量节点守护从第一天起只有一个名字**：

    posix  systemd 单元    agent-team-nodelet
    nt     计划任务        AgentTeamNodelet

这两个名字的唯一出处是 L0 `nodelet.py` 的 `UNIT_NAME_POSIX` / `UNIT_NAME_NT`；本件
**不另抄一份**，`--gen` 与 `--check` 都从那里取。单元内容的模板在本件里**只有一份**，
四台机的差异（运行目录 / python / 该机声明的服务单元清单）全部由**部署表 + L0 档案**推导。

用法
----
    python tools/nodelet_gen.py --gen                  # 打印四台机将生成的单元（只打印，不落盘）
    python tools/nodelet_gen.py --gen --node lobster   # 只看一台
    python tools/nodelet_gen.py --check                # 核对各机单元是否存在、名字是否统一
    python tools/nodelet_gen.py --check --node lobster
    python tools/nodelet_gen.py --gen --json           # 机器可读

**只读**：`--gen` 只往 stdout 打印，一个字节都不落盘、不发到任何机器；`--check` 只跑
`systemctl list-unit-files` / `schtasks /Query` 这类**查询**命令，不启停任何单元。

退出码：0 = 一切如约；1 = `--check` 发现缺单元或名字不统一；2 = 用法/配置错误。
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

# 批② 起被搬件按绝对包名 import（node.l0_host.nodelet / node.l0_host.node_profile），故仓根一并入路径。
for _hacs_p in (PROJECT_ROOT,):
    if _hacs_p not in sys.path:
        sys.path.insert(0, _hacs_p)

from node.l0_host import node_profile                  # noqa: E402 —— L0 节点档案
import node.l0_host.nodelet as nodelet                 # noqa: E402 —— L0 轻量守护（取单元名与绑定默认值）

EXIT_OK, EXIT_FAIL, EXIT_USAGE = 0, 1, 2

#: 本件在节点上的落盘文件名（部署表里那条 path，模板里要写进 ExecStart）
NODELET_FILE = "nodelet.py"

#: 单元里写的 python 可执行名，按 flavour 取（`--python` 一把覆盖两边）。
#: systemd 的 ExecStart **必须是绝对路径**，所以 posix 侧不能只写 `python3`。
#: nt 侧同理 **必须给绝对路径**（刀23 实测）：计划任务以 SYSTEM 起，SYSTEM 的 PATH 里
#: 没有 `python`，写裸名会 0x1 退出；另外**不能用 `pythonw.exe`** —— 无控制台时
#: nodelet 启动横幅那行 `print()` 会拿到 `sys.stdout is None` 直接崩。
#: 解析顺序：`--python` → env `AGENT_TEAM_NT_PYTHON` → 兜底裸名（并打警告）。
DEFAULT_PYTHON = {"posix": "/usr/bin/python3", "nt": "python"}
ENV_NT_PYTHON = "AGENT_TEAM_NT_PYTHON"

#: 「像 nodelet 的单元」的判词 —— `--check` 用它找出**名字不统一**的同类单元
NODELET_HINT = re.compile(r"nodelet", re.I)

_ENV_RX = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::([^}]*))?\}")


def _expand(value):
    """展开 `${VAR:默认值}`（与 deploy.py / probe_model_channel.py 同一口径）。"""
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


# ============================================================ 档案 + 部署表 → 单元事实

def node_services(target):
    """该节点**部署表里声明过监管单元**的全部单元名（去重保序）。

    这就是探针口 `facts.services[]` 的来源：守护自己不读部署表（部署表是内部真值表，
    不下发到节点），而是由本件把它**烘进单元的 `Environment=`**，一次生成、长期有效。
    """
    names, seen = [], []
    for spec in target.get("files") or []:
        for svc in spec.get("services") or []:
            if svc and svc not in seen:
                seen.append(svc)
                names.append(svc)
    return names


def node_facts(target):
    """一台机生成单元所需的全部事实（**全部**来自部署表 + L0 档案，本件零硬编码）。"""
    name = target.get("name") or ""
    flavour = node_profile.target_flavour(name)
    known = node_profile.target_known(name)
    services = node_services(target)
    unit = nodelet.UNIT_NAME_NT if flavour == "nt" else nodelet.UNIT_NAME_POSIX
    runtime = _expand(target.get("runtime_dir") or "").rstrip("/\\")
    # 部署表里的 Windows 运行目录写成 `<deploy-dir>`（正斜杠），单元里要写成本平台惯例写法，
    # 否则会拼出 `<deploy-dir>\nodelet.py` 这种两种分隔符混用的路径。
    if flavour == "nt":
        runtime = runtime.replace("/", "\\")
    return {
        "node": name,
        "flavour": flavour,
        "profile_known": known,
        "supervisor": target.get("supervisor") or "",
        "transport": target.get("transport") or "",
        "runtime_dir": runtime,
        "unit": unit,
        "exec_path": runtime + ("\\" if flavour == "nt" else "/") + NODELET_FILE,
        "bind": "%s:%d" % (nodelet.DEFAULT_HOST, nodelet.DEFAULT_PORT),
        "services": [u for u in dict.fromkeys(services + [unit])],  # 守护也把自己算进去；去重保序（部署表里 nodelet 自己那条服务也在 services 里）
    }


# ============================================================ 单元模板（**全仓只此一份**）

SYSTEMD_TEMPLATE = """\
[Unit]
Description=Agent-Team nodelet (read-only node probe %(probe)s)
Documentation=file:%(exec_path)s
# 只绑环回，不依赖 tailscaled；network-online 够了
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=%(runtime)s
ExecStart=%(python)s %(exec_path)s
Environment=PYTHONUNBUFFERED=1
Environment=%(env_node)s=%(node)s
Environment=%(env_bind)s=%(bind)s
Environment=%(env_services)s=%(services)s
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

# 只读守护：它一个字节都不写，权限按只读收。
# 比同仓其它只读服务松一档（它们用 ProtectSystem=strict）：本件要向 systemd 查单元状态，
# 先用 full 保证查得到；待实测确认 strict 也不挡，再收紧。
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=full
ProtectHome=read-only
ProtectKernelTunables=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
LockPersonality=yes

[Install]
WantedBy=multi-user.target
"""

#: Windows 侧：`/TR` 有 261 字符上限，且 `cmd /c set X=Y&&…` 串 env 很脆 ——
#: 所以这里走 nodelet 的三个薄开关（它们只把值写回本进程 env，入口仍只有 env）。
SCHTASKS_TEMPLATE = """\
schtasks /Create /TN %(unit)s /SC ONSTART /RL HIGHEST /RU SYSTEM /F ^
 /TR "%(python)s \\"%(exec_path)s\\" --node %(node)s --bind %(bind)s --services %(services)s"
"""


def python_for(facts, python_bin=""):
    """本 flavour 该用的 python 可执行名/路径：`--python` → env → 兜底（刀23 实测口径）。"""
    if python_bin:
        return python_bin
    if facts.get("flavour") == "nt":
        env_py = (os.environ.get(ENV_NT_PYTHON) or "").strip()
        if env_py:
            return env_py
    return DEFAULT_PYTHON[facts["flavour"]]


def python_warning(facts, python_bin=""):
    """nt 侧给不出绝对路径时的**响亮提醒**（不是失败）：SYSTEM 账户 PATH 里没有裸名 python。"""
    chosen = python_for(facts, python_bin)
    if facts.get("flavour") != "nt":
        return ""
    if os.path.isabs(chosen) or ":" in chosen:
        return ""
    return ("⚠ %s 的单元里 python 还是裸名 %r —— 计划任务以 SYSTEM 起时 PATH 不可靠（实测 0x1 退出）。"
            "请用 --python <绝对路径> 或 env %s 再生成一次；并用 python.exe，不要用 pythonw.exe"
            "（无控制台时启动横幅的 print() 会崩）。" % (facts.get("node") or "?", chosen, ENV_NT_PYTHON))


def render_unit(facts, python_bin=""):
    """一台机的单元内容（systemd 单元文件 / schtasks 命令行）。**只返回文本，不落盘**。"""
    fields = {
        "probe": nodelet.PROBE_PATH,
        "runtime": facts["runtime_dir"],
        "python": python_for(facts, python_bin),
        "exec_path": facts["exec_path"],
        "node": facts["node"],
        "unit": facts["unit"],
        "bind": facts["bind"],
        "env_node": node_profile.ENV_NODE_NAME,
        "env_bind": nodelet.ENV_BIND,
        "env_services": nodelet.ENV_SERVICES,
        "services": ",".join(facts["services"]),
    }
    if facts["flavour"] == "nt":
        return SCHTASKS_TEMPLATE % fields
    return SYSTEMD_TEMPLATE % fields


def unit_filename(facts):
    """生成物该叫什么（posix = 单元文件名；nt = 计划任务名，没有文件）。"""
    if facts["flavour"] == "nt":
        return facts["unit"] + "（计划任务，无单元文件）"
    return "/etc/systemd/system/%s.service" % facts["unit"]


# ============================================================ --check：各机单元核对

#: 查「这台机上有哪些像 nodelet 的单元」的命令（**只查不动**）
#: posix 侧**自己把 PATH 钉死**（刀23 实测）：native python 起的 ssh 与 MSYS bash 起的 ssh
#: 走的不是同一个客户端，远端 shell 的 PATH 未必含 /usr/bin → `systemctl: command not found`。
CHECK_CMD_POSIX = ("PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin "
                   "systemctl list-unit-files --type=service --no-legend --no-pager")
CHECK_CMD_NT = "schtasks /Query /FO CSV /NH"


def _remote_cmd(text):
    """远端命令**原样**传，不加引号。

    刀23 实测：本机 `ssh` 解析到的是 MSYS 版（Git 安装目录下 usr/bin/ssh.exe），它会把我们
    加的引号**一并留在命令串里** → 远端 bash 把整串当成一个文件名去 execve，报
    `bash: line 1: ...: No such file or directory` / `command not found`。
    不加引号时 ssh 把 argv 用空格拼成命令行交给远端 shell 解释 —— 正是我们要的。
    """
    return str(text)


def run_on(target, cmd, timeout=30.0):
    """在该节点上跑一条**只读**命令 → `(rc, stdout, 说明)`。

    `transport=local` 就在本机跑；其余经 ssh（参数与 probe_model_channel.py 同一套，
    本件不 import deploy.py —— 契约明令不碰它）。
    """
    if (target.get("transport") or "") == "local":
        argv, shell = cmd, True
    else:
        key = _expand(target.get("ssh_key") or "")
        argv = ["ssh"]
        if key:
            argv += ["-i", key]
        argv += ["-o", "ConnectTimeout=10", "-o", "BatchMode=yes",
                 "-o", "StrictHostKeyChecking=accept-new",
                 _expand(target["ssh_target"]), _remote_cmd(cmd)]
        shell = False
    try:
        proc = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              shell=shell, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, "", "超时 %ds" % int(timeout)
    except OSError as exc:
        return None, "", "起不来：%s" % exc
    out = (proc.stdout or b"").decode("utf-8", "replace")
    err = (proc.stderr or b"").decode("utf-8", "replace")
    return proc.returncode, out, (err.strip()[-200:] if proc.returncode != 0 else "")


def parse_units_posix(text):
    """`systemctl list-unit-files` 输出 → 单元名列表（只取**像 nodelet 的**那些）。"""
    names = []
    for line in (text or "").splitlines():
        first = line.strip().split()
        if not first:
            continue
        unit = first[0]
        if unit.endswith(".service"):
            unit = unit[: -len(".service")]
        if NODELET_HINT.search(unit):
            names.append(unit)
    return names


def parse_units_nt(text):
    """`schtasks /Query /FO CSV /NH` 输出 → 任务名列表（只取像 nodelet 的那些）。"""
    import csv
    names = []
    for row in csv.reader(io.StringIO(text or "")):
        if not row:
            continue
        task = (row[0] or "").strip().lstrip("\\")
        if NODELET_HINT.search(task):
            names.append(task)
    return names


def check_node(target):
    """核对一台机：规范单元在不在、有没有**名字不统一**的同类单元。"""
    facts = node_facts(target)
    cmd = CHECK_CMD_NT if facts["flavour"] == "nt" else CHECK_CMD_POSIX
    rc, out, why = run_on(target, cmd)
    result = dict(facts, queried=cmd, rc=rc, why=why, found=[], ok=False, verdict="")
    if rc is None or rc != 0:
        result["verdict"] = "查不出来（%s）" % (why or "rc=%s" % rc)
        return result
    found = parse_units_nt(out) if facts["flavour"] == "nt" else parse_units_posix(out)
    result["found"] = found
    canonical = facts["unit"]
    strays = [n for n in found if n != canonical]
    if canonical in found and not strays:
        result["ok"], result["verdict"] = True, "规范单元在，且无异名同类单元"
    elif canonical not in found and not found:
        result["verdict"] = "缺单元：这台机上没有 %s（也没有任何同类单元）" % canonical
    elif canonical not in found:
        result["verdict"] = "名字不统一：只有 %s，规范名 %s 不在" % ("、".join(found), canonical)
    else:
        result["verdict"] = "名字不统一：规范单元在，但还有异名同类单元 %s" % "、".join(strays)
    return result


# ============================================================ 渲染

def render_gen(rows, python_bin):
    lines = []
    for facts in rows:
        lines.append("=" * 88)
        lines.append("节点 %s ｜ flavour=%s（档案%s） ｜ supervisor=%s"
                     % (facts["node"], facts["flavour"],
                        "登记在案" if facts["profile_known"] else "**未登记，按 posix 兜底**",
                        facts["supervisor"]))
        lines.append("生成物：%s" % unit_filename(facts))
        lines.append("声明单元（写进 %s）：%s"
                     % (nodelet.ENV_SERVICES, "、".join(facts["services"])))
        lines.append("-" * 88)
        lines.append(render_unit(facts, python_bin).rstrip("\n"))
        lines.append("")
    lines.append("以上**只打印未落盘**：要装请把内容贴到目标机上，或经部署管线下发。")
    return "\n".join(lines)


def render_check(rows):
    lines = ["=" * 88,
             "节点守护单元核对 · 规范名 posix=%s / nt=%s（唯一出处 node/l0_host/nodelet.py）"
             % (nodelet.UNIT_NAME_POSIX, nodelet.UNIT_NAME_NT),
             "=" * 88]
    bad = 0
    for r in rows:
        mark = "✓" if r["ok"] else "✗"
        if not r["ok"]:
            bad += 1
        lines.append("  %s %-10s 规范名 %-18s %s" % (mark, r["node"], r["unit"], r["verdict"]))
        lines.append("        查询：%s" % r["queried"])
        if r["found"]:
            lines.append("        实测同类单元：%s" % "、".join(r["found"]))
    lines.append("-" * 88)
    lines.append("不合格 %d / %d 台" % (bad, len(rows)))
    return "\n".join(lines), bad


# ============================================================ 主流程

def pick(targets, node):
    if not node:
        return targets, ""
    chosen = [t for t in targets if t.get("name") == node]
    if not chosen:
        return [], "没有这个节点：%s（可选：%s）" % (
            node, "、".join(sorted(t.get("name") or "" for t in targets)) or "无")
    return chosen, ""


def main():
    ap = argparse.ArgumentParser(description="节点守护单元的生成与核对（只读）")
    ap.add_argument("--gen", action="store_true", help="打印将生成的单元内容（只打印，不落盘）")
    ap.add_argument("--check", action="store_true", help="核对各机单元是否存在、名字是否统一")
    ap.add_argument("--node", default="", help="只看一台（省略 = 部署表里全部）")
    ap.add_argument("--python", default="",
                    help="单元里写的 python 可执行名（省略 = 按 flavour 取 %s）" % DEFAULT_PYTHON)
    ap.add_argument("--json", action="store_true", help="只吐 JSON")
    args = ap.parse_args()

    if args.gen == args.check:
        ap.error("--gen 与 --check 必须给且只给一个")

    targets, why = pick(load_targets(), args.node)
    if why:
        print("✗ " + why)
        return EXIT_USAGE
    if not targets:
        print("✗ 部署表里一台机都没有：%s" % TARGETS_PATH)
        return EXIT_USAGE

    if args.gen:
        rows = [node_facts(t) for t in targets]
        if args.json:
            print(json.dumps([dict(f, unit_text=render_unit(f, args.python)) for f in rows],
                             ensure_ascii=False, indent=1))
        else:
            print(render_gen(rows, args.python))
        return EXIT_OK

    rows = [check_node(t) for t in targets]
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return EXIT_OK if all(r["ok"] for r in rows) else EXIT_FAIL
    text, bad = render_check(rows)
    print(text)
    return EXIT_OK if bad == 0 else EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
