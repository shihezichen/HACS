#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""team_cli_equiv.py —— 刀15「群聊 / 会议业务从 L1 team.py 摘到 L3 team_group.py」的
**CLI 级字节等价证据**（纯 stdlib，可复跑）。

用法：
    python tools/equivalence/team_cli_equiv.py                 # 严格模式：跑全矩阵
    python tools/equivalence/team_cli_equiv.py --runs 3        # 连跑 3 次自证确定性
    python tools/equivalence/team_cli_equiv.py --preflight     # 搬迁前预跑（放开「两侧不得同文」断言）

比什么（每条用例逐字节比四样，缺一不可）：
    argv → (stdout, stderr, exit code, 出站请求序列[方法/路径/正文/X-Agent-Id/顺序])
另加**落盘产物**（会议留痕 jsonl、去重文件）也一并比。

怎么比（为什么不打桩 main()）：
    只对 main() 打桩会漏掉真实分派（argparse 子命令树、set_defaults(func=…)）与真实出站
    行为。本装置一律**起子进程跑真实 CLI 入口**（`python <script> <argv>`），从 stub 侧
    记录请求 —— 被测件怎么组织模块、谁 import 谁，装置一概不知道，也就骗不过它。

两侧是谁：
    旧 = tools/equivalence/_old/team_old_k15.py（搬迁前 HEAD 的 team.py 原样副本）
    新 = node/c/team.py（+ 它 import 的 team_group.py / team_cli_core.py）
    开跑前硬校验两侧 md5 **不相等**（`--preflight` 除外），自证不是自比自。

不碰机器现状：
    · 群命令的对话台 = 本进程内起的 stub HTTP（127.0.0.1，端口 0 由内核分配），
      绝不打真服务、不碰 9984。
    · NATS 命令的中枢 = 自己造的**死口**：绑 127.0.0.1:0 拿到端口后立刻关闭，
      于是「无人监听」是本装置自己证的，不依赖机器上恰好没起什么。
    · 身份/口令/去重文件/留痕文件全落在每条用例独享的临时目录里，跑完即删。
    · 环境变量里 AGENT_TEAM_* 一律清干净再按需注入，COLUMNS 固定 100
      （不固定的话 argparse 的 --help 折行随终端宽度变，等价证据就不可复现）。

确定性怎么保证（`--runs N`）：
    随机/时钟来源就三处 —— meet 的 session_id（time+4 位随机）、留痕 ts、
    call_one 的 elapsed，外加两个随机端口与临时目录路径。全部在比对前归一
    （见 normalize()）。归一后 N 次输出必须逐字节相同，否则不打 EQUIV_DETERMINISM_OK。

退出码：0 = 全矩阵一致且确定性通过；1 = 有不一致；2 = 装置自身前置条件不满足。
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from _frozen import write_frozen

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
#: 旧侧基线的**来源仓**：`agent-team/` 时期的提交（改造前的源码）在**源码仓**里，
#: 本仓是整改后的新仓、不含那段历史。env 可覆盖；同级目录兜底；找不到就退回本仓（不静默）。
SRC_REPO = os.environ.get("HACS_SOURCE_REPO") or os.path.join(os.path.dirname(REPO), "多Agent相互协作")
if not os.path.isdir(os.path.join(SRC_REPO, ".git")):
    SRC_REPO = REPO

#: 保留注入（经子进程 PYTHONPATH）：旧侧冻结快照 `team_old_k15.py` 按裁定一字不动，
#: 它内部是**裸名** import（`node_paths` / `from nats_client import ...`）。分层整改后
#: 两件各自归位到包目录里，裸名再也解析不到，故把它们所在目录一并放进 PYTHONPATH；
#: REPO 供新侧的绝对包名（`node.c.team` → `node.l1_transport.*` / `hub.l3_domain.*`）解析。
FLAT_DIRS = [os.path.join(REPO, "node", "l0_host"),        # node_paths
             os.path.join(REPO, "node", "l1_transport")]   # nats_client
OLD_DIR = os.path.join(HERE, "_old")
OLD_SCRIPT = os.path.join(OLD_DIR, "team_old_k15.py")
NEW_SCRIPT = os.path.join(REPO, "node", "c", "team.py")
DEFAULT_REF = "049a418"          # 刀15 动手前的 HEAD
MEMBER = "equivmember"


# ------------------------------------------------------------------ 旧侧基线

def materialize_old(ref):
    """取 <ref> 的 agent-team/team.py 作旧侧基线。

    **只新建、不覆盖**：_old/ 下是历史基线，被覆盖就等于把证据改了。已存在时
    校验它与 <ref> 一致，不一致直接判死而不是悄悄改写。
    """
    if not os.path.isdir(OLD_DIR):
        os.makedirs(OLD_DIR)
    raw = subprocess.run(["git", "show", "%s:agent-team/team.py" % ref],
                         cwd=SRC_REPO, capture_output=True).stdout
    if not raw:
        print("取不到 %s:agent-team/team.py —— ref 写错了？" % ref)
        return False
    if os.path.isfile(OLD_SCRIPT):
        if open(OLD_SCRIPT, "rb").read() != raw:
            print("✗ %s 已存在且与 %s 不一致 —— 拒绝覆盖历史基线，请人工确认"
                  % (OLD_SCRIPT, ref))
            return False
        return True
    write_frozen(OLD_SCRIPT, raw, "team.py")
    return True


def md5(path):
    return hashlib.md5(open(path, "rb").read()).hexdigest()


# ------------------------------------------------------------------ 死口 / stub

def dead_port():
    """自己造一个「已证无人监听」的端口：绑上再立刻关掉。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Stub(BaseHTTPRequestHandler):
    """对话台 stub：按 plan 回一个固定应答，并把收到的请求原样记账。"""
    plan = (200, {})
    requests = []
    lock = threading.Lock()

    def log_message(self, *a):
        pass                      # 别往 stderr 刷访问日志，会污染装置自己的输出

    def _record_and_reply(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        with Stub.lock:
            Stub.requests.append({
                "method": self.command,
                "path": self.path,
                "agent_id": self.headers.get("X-Agent-Id"),
                "content_type": self.headers.get("Content-Type"),
                "body": body.decode("utf-8", "replace"),
            })
        status, payload = Stub.plan
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    do_GET = do_POST = do_PUT = do_DELETE = _record_and_reply


def start_stub():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


# ------------------------------------------------------------------ 用例矩阵

NATS = ["--as", MEMBER, "--password-file", "{PASS}",
        "--nats-host", "127.0.0.1:{DEAD}", "--nats-port", "{DEAD}", "--timeout", "1"]
CHAT = ["--as", MEMBER, "--chat-base", "{CHAT}"]

MSG_201 = (201, {"message": {"seq": 7, "message_id": "m-0007",
                             "meta": {"mentions": ["@all"], "needs_response": [MEMBER]}}})
ERR_400 = (400, {"error": {"code": "no_mention", "message": "Agent 发言必须 @ 至少一个目标"}})
ERR_403 = (403, {"error": {"code": "forbidden", "message": "不是群成员"}})
ERR_404 = (404, {"error": {"code": "not_found", "message": "群不存在"}})
ERR_500 = (500, {"raw_only": True})
PULL_200 = (200, {
    "messages": [
        {"seq": 1, "author": {"id": "bob", "display": "Bob"},
         "text": "第一条\n带换行", "meta": {"mentions": ["@all"], "needs_response": [MEMBER]}},
        {"seq": 2, "author": {"id": "alice"},
         "text": "第二条", "meta": {"mentions": ["bob"], "needs_response": []}},
        {"seq": 3, "author": {}, "text": None, "meta": {}},
    ],
    "my_pending": [{"seq": 1, "from": "bob", "text": "第一条"}],
})
PULL_EMPTY = (200, {"messages": [], "my_pending": []})
SHOW_200 = (200, {"session": {"title": "临时群·等价对照", "session_id": "grp-equiv-1",
                              "state": "open", "created_by": "bob",
                              "members": [{"id": "bob"}, {"id": "alice"}, {"id": MEMBER}]}})
SHOW_CLOSED = (200, {"session": {"title": "已结束的群", "session_id": "grp-equiv-2",
                                 "state": "closed", "created_by": "alice", "members": []}})

MEET_BASE = ["meet", "--goal", "对齐刀15 的验收口径",
             "--agenda", "确认搬迁范围", "--agenda", "定等价判据",
             "--members", "bob,alice", "--out", "{OUT}", "--dedup-file", "{DEDUP}"] + NATS

CASES = [
    # -- 参数面：argparse 子命令树 / 帮助文本 / 缺参报错（不触网、不落盘）--------
    dict(name="help·顶层", argvs=[["--help"]]),
    dict(name="help·meet", argvs=[["meet", "--help"]]),
    dict(name="help·history", argvs=[["history", "--help"]]),
    dict(name="help·group", argvs=[["group", "--help"]]),
    dict(name="help·group say", argvs=[["group", "say", "--help"]]),
    dict(name="help·group pull", argvs=[["group", "pull", "--help"]]),
    dict(name="help·group show", argvs=[["group", "show", "--help"]]),
    dict(name="help·who/say/broadcast", argvs=[["who", "--help"], ["say", "--help"],
                                               ["broadcast", "--help"]]),
    dict(name="无子命令", argvs=[[]]),
    dict(name="未知子命令", argvs=[["nosuch"]]),
    dict(name="group 缺二级子命令", argvs=[["group"]]),
    dict(name="group 未知二级子命令", argvs=[["group", "nosuch"]]),
    dict(name="meet 缺必填 --goal", argvs=[["meet", "--members", "a,b"] + NATS]),

    # -- meet：搬迁对象主路径（死口 → 全员缺席）-------------------------------
    dict(name="meet·空 --goal", argvs=[["meet", "--goal", "  ", "--agenda", "A",
                                        "--members", "bob"] + NATS]),
    dict(name="meet·无 --agenda", argvs=[["meet", "--goal", "G",
                                          "--members", "bob"] + NATS]),
    dict(name="meet·--members 只有空白", argvs=[["meet", "--goal", "G", "--agenda", "A",
                                                 "--members", " , , "] + NATS]),
    dict(name="meet·口令文件不存在",
         argvs=[["meet", "--goal", "G", "--agenda", "A", "--members", "bob",
                 "--as", MEMBER, "--password-file", "{NOPASS}",
                 "--nats-host", "127.0.0.1:{DEAD}"]]),
    dict(name="meet·固定 session_id（主路径）",
         argvs=[MEET_BASE + ["--session-id", "meet-fixed-0001"]],
         post=["{OUT}", "{DEDUP}"]),
    dict(name="meet·自动 session_id（归一后比）",
         argvs=[MEET_BASE], post=["{OUT}"]),
    dict(name="meet·--json", argvs=[MEET_BASE + ["--session-id", "meet-fixed-0002", "--json"]],
         post=["{OUT}"]),
    dict(name="meet·去重命中 → 再 --force 重发",
         argvs=[MEET_BASE + ["--session-id", "meet-fixed-0003"],
                MEET_BASE + ["--session-id", "meet-fixed-0003"],
                MEET_BASE + ["--session-id", "meet-fixed-0003", "--force"]],
         post=["{OUT}", "{DEDUP}"]),
    dict(name="meet·并发度 1", argvs=[MEET_BASE + ["--session-id", "meet-fixed-0004",
                                                   "--concurrency", "1"]], post=["{OUT}"]),

    # -- history：搬迁对象（读 meet 刚写的留痕）-------------------------------
    dict(name="history·回放刚写的那场",
         argvs=[MEET_BASE + ["--session-id", "meet-fixed-0005"],
                ["history", "--session-id", "meet-fixed-0005", "--file", "{OUT}"] + NATS],
         post=["{OUT}"]),
    dict(name="history·--json",
         argvs=[MEET_BASE + ["--session-id", "meet-fixed-0006"],
                ["history", "--session-id", "meet-fixed-0006", "--file", "{OUT}",
                 "--json"] + NATS]),
    dict(name="history·同 id 重发取最后一条",
         argvs=[MEET_BASE + ["--session-id", "meet-dup", "--force"],
                MEET_BASE + ["--session-id", "meet-dup", "--force", "--agenda", "追加议程"],
                ["history", "--session-id", "meet-dup", "--file", "{OUT}"] + NATS]),
    dict(name="history·session 不存在",
         argvs=[MEET_BASE + ["--session-id", "meet-fixed-0007"],
                ["history", "--session-id", "nope", "--file", "{OUT}"] + NATS]),
    dict(name="history·留痕文件不存在",
         argvs=[["history", "--session-id", "x", "--file", "{NOFILE}"] + NATS]),
    dict(name="history·文件里有坏行也要跳过",
         argvs=[["history", "--session-id", "x", "--file", "{JUNK}"] + NATS],
         seed_junk=True),

    # -- 群命令：搬迁对象（走 stub 对话台，比出站请求序列）---------------------
    dict(name="group say·默认 @all → 201",
         argvs=[["group", "say", "--group", "grp-1", "你好，群里的各位"] + CHAT], stub=MSG_201),
    dict(name="group say·--to/--reply-to/--client-msg-id → 201",
         argvs=[["group", "say", "--group", "grp-1", "定向发言",
                 "--to", "bob, alice ", "--reply-to", "m-0001",
                 "--client-msg-id", "cid-1"] + CHAT], stub=MSG_201),
    dict(name="group say·--json → 201",
         argvs=[["group", "say", "--group", "grp-1", "带 json", "--json"] + CHAT], stub=MSG_201),
    dict(name="group say·--text-file",
         argvs=[["group", "say", "--group", "grp-1", "--text-file", "{TEXT}"] + CHAT],
         stub=MSG_201, seed_text=True),
    dict(name="group say·正文为空 → TeamCliError",
         argvs=[["group", "say", "--group", "grp-1", "   "] + CHAT], stub=MSG_201),
    dict(name="group say·--to 为空白 → TeamCliError",
         argvs=[["group", "say", "--group", "grp-1", "x", "--to", "  "] + CHAT], stub=MSG_201),
    dict(name="group say·服务端 400",
         argvs=[["group", "say", "--group", "grp-1", "x"] + CHAT], stub=ERR_400),
    dict(name="group say·服务端 403 + --json",
         argvs=[["group", "say", "--group", "grp-1", "x", "--json"] + CHAT], stub=ERR_403),
    dict(name="group say·服务端 500（非标准错误体）",
         argvs=[["group", "say", "--group", "grp-1", "x"] + CHAT], stub=ERR_500),

    dict(name="group pull·默认", argvs=[["group", "pull", "--group", "grp-1"] + CHAT],
         stub=PULL_200),
    dict(name="group pull·--since/--limit",
         argvs=[["group", "pull", "--group", "grp-1", "--since", "5",
                 "--limit", "3"] + CHAT], stub=PULL_200),
    dict(name="group pull·--limit 0 → 下限兜底 1",
         argvs=[["group", "pull", "--group", "grp-1", "--limit", "0"] + CHAT], stub=PULL_200),
    dict(name="group pull·--json", argvs=[["group", "pull", "--group", "grp-1", "--json"] + CHAT],
         stub=PULL_200),
    dict(name="group pull·空群（无欠回应）",
         argvs=[["group", "pull", "--group", "grp-1"] + CHAT], stub=PULL_EMPTY),
    dict(name="group pull·不带 --as（身份靠推断）",
         argvs=[["group", "pull", "--group", "grp-1", "--chat-base", "{CHAT}"]], stub=PULL_200),
    dict(name="group pull·服务端 404", argvs=[["group", "pull", "--group", "grp-1"] + CHAT],
         stub=ERR_404),

    dict(name="group show·open", argvs=[["group", "show", "--group", "grp-1"] + CHAT],
         stub=SHOW_200),
    dict(name="group show·closed（只读提示）",
         argvs=[["group", "show", "--group", "grp-2"] + CHAT], stub=SHOW_CLOSED),
    dict(name="group show·--json", argvs=[["group", "show", "--group", "grp-1", "--json"] + CHAT],
         stub=SHOW_200),
    dict(name="group show·服务端 500", argvs=[["group", "show", "--group", "grp-1"] + CHAT],
         stub=ERR_500),

    # -- 留在 team.py 的命令：回归（证明搬迁没碰它们）--------------------------
    dict(name="who·死口", argvs=[["who", "--seconds", "1"] + NATS]),
    dict(name="say·死口", argvs=[["say", "bob", "正文"] + NATS]),
    dict(name="say·--session-id 与 --new-session 互斥",
         argvs=[["say", "bob", "正文", "--session-id", "s1", "--new-session"] + NATS]),
    dict(name="say·无正文", argvs=[["say", "bob"] + NATS]),
    dict(name="broadcast·--to 死口",
         argvs=[["broadcast", "正文", "--to", "bob,alice", "--concurrency", "2",
                 "--dedup-file", "{DEDUP}"] + NATS], post=["{DEDUP}"]),
    dict(name="broadcast·--all 与 --to 都不给",
         argvs=[["broadcast", "正文", "--dedup-file", "{DEDUP}"] + NATS]),
    dict(name="broadcast·--all 与 --to 都给",
         argvs=[["broadcast", "正文", "--all", "--to", "bob",
                 "--dedup-file", "{DEDUP}"] + NATS]),
    dict(name="broadcast·--to 只有自己 → 名单为空",
         argvs=[["broadcast", "正文", "--to", MEMBER, "--dedup-file", "{DEDUP}"] + NATS]),
]


# ------------------------------------------------------------------ 归一

def normalize(text, roots, ports):
    """把「随机/时钟/路径」这些机器现状归一掉，只留下实现差异。

    归一范围是封闭的：两个端口、两侧工作目录、session_id 的时间+随机段、
    留痕 ts、call_one 的 elapsed、Windows 下 WinError 的中文措辞（同机同码，
    两侧本就相同，这里只做长度无关的稳定化）。**不归一任何业务字段。**
    """
    for r in roots:
        for form in (r, r.replace("\\", "\\\\"), r.replace("\\", "/")):
            text = text.replace(form, "<WD>")
    for p in ports:
        text = re.sub(r"\b%d\b" % p, "<PORT>", text)
    text = re.sub(r"meet-\d{8}-\d{4}-[a-z0-9]{4}", "meet-<AUTOSID>", text)
    text = re.sub(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", "<TS>", text)
    text = re.sub(r"(?<=\s)\d+\.\ds\b", "<EL>s", text)            # 打印里的 %6.1fs
    text = re.sub(r'"elapsed": \d+\.\d+', '"elapsed": <EL>', text)
    text = re.sub(r'"ts": \d{9,}\.\d+', '"ts": <EPOCH>', text)
    text = re.sub(r"^\s+\"[0-9a-f]{40}\": \d+\.\d+", "  <DEDUPKEY>: <EPOCH>", text, flags=re.M)
    text = re.sub(r'"[0-9a-f]{40}": \d+\.\d+', '"<DEDUPKEY>": <EPOCH>', text)
    return text


# ------------------------------------------------------------------ 跑一侧

def base_env(natsdir):
    env = dict(os.environ)
    for k in list(env):
        if k.startswith("AGENT_TEAM_") or k in ("TEAM_CHAT_BASE",):
            del env[k]
    env["PYTHONPATH"] = os.pathsep.join([REPO] + FLAT_DIRS
                                        + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONHASHSEED"] = "0"
    env["COLUMNS"] = "100"                 # 不固定则 --help 折行随终端宽度飘
    env["AGENT_TEAM_NATS_DIR"] = natsdir   # 空目录：身份推断走「默认成员」那条，确定
    env["AGENT_TEAM_RUNTIME_DIR"] = natsdir
    return env


def run_side(script, root, dead, chat_base):
    """跑完整矩阵，回一份纯文本流水（供归一后逐字节比对）。"""
    out = []
    for idx, case in enumerate(CASES):
        wd = os.path.join(root, "c%02d" % idx)
        natsdir = os.path.join(wd, "natsdir")
        os.makedirs(natsdir)
        subs = {
            "{PASS}": os.path.join(wd, "nats.pass"),
            "{NOPASS}": os.path.join(wd, "missing.pass"),
            "{NOFILE}": os.path.join(wd, "missing.jsonl"),
            "{JUNK}": os.path.join(wd, "junk.jsonl"),
            "{TEXT}": os.path.join(wd, "body.txt"),
            "{OUT}": os.path.join(wd, "meetings.jsonl"),
            "{DEDUP}": os.path.join(wd, "dedup.json"),
            "{DEAD}": str(dead),
            "{CHAT}": chat_base,
        }
        open(subs["{PASS}"], "w", encoding="utf-8").write("equiv-secret\n")
        if case.get("seed_text"):
            open(subs["{TEXT}"], "w", encoding="utf-8").write("来自文件的正文\n第二行\n")
        if case.get("seed_junk"):
            open(subs["{JUNK}"], "w", encoding="utf-8").write(
                "\n  \n不是 json\n{\"session_id\": \"x\", \"convener\": \"bob\", "
                "\"ts\": \"2020-01-01 00:00:00\", \"goal\": \"G\", \"agenda\": [\"A\"], "
                "\"members\": [\"bob\"], \"present\": [\"bob\"], "
                "\"results\": [{\"member\": \"bob\", \"ok\": true, \"reply\": \"行\\n换行\"}]}\n")

        Stub.plan = case.get("stub", (200, {}))
        with Stub.lock:
            Stub.requests = []

        out.append("=" * 70)
        out.append("用例 %02d · %s" % (idx, case["name"]))
        for argv in case["argvs"]:
            real = [subs.get(a, a) for a in argv]
            real = [a.replace("{DEAD}", str(dead)).replace("{CHAT}", chat_base) for a in real]
            shown = [a for a in argv]
            proc = subprocess.run([sys.executable, script] + real, cwd=wd,
                                  env=base_env(natsdir), capture_output=True)
            out.append("-- argv: %s" % json.dumps(shown, ensure_ascii=False))
            out.append("   rc = %d" % proc.returncode)
            out.append("   -- stdout --")
            out.append(proc.stdout.decode("utf-8", "replace"))
            out.append("   -- stderr --")
            out.append(proc.stderr.decode("utf-8", "replace"))
        with Stub.lock:
            reqs = list(Stub.requests)
        out.append("   -- 出站请求序列（%d 条）--" % len(reqs))
        out.append(json.dumps(reqs, ensure_ascii=False, indent=1, sort_keys=True))
        for token in case.get("post", []):
            p = subs[token]
            out.append("   -- 落盘产物 %s --" % token)
            out.append(open(p, encoding="utf-8").read() if os.path.isfile(p) else "<不存在>")
    return "\n".join(out)


def one_run(dead, chat_base, tmp, tag):
    """一次完整对照：旧侧、新侧各跑一遍矩阵，回 (旧文本, 新文本, 归一用的根目录)。"""
    old_root = os.path.join(tmp, tag, "old")
    new_root = os.path.join(tmp, tag, "new")
    os.makedirs(old_root)
    os.makedirs(new_root)
    a = run_side(OLD_SCRIPT, old_root, dead, chat_base)
    b = run_side(NEW_SCRIPT, new_root, dead, chat_base)
    return a, b, (old_root, new_root)


def split_cases(text):
    """把流水按用例切开，方便逐例报一致/不一致。"""
    parts = re.split(r"^={70}$", text, flags=re.M)
    return [p for p in parts if p.strip()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", default=DEFAULT_REF, help="搬迁前的 commit/ref（旧侧基线来源）")
    ap.add_argument("--runs", type=int, default=1, help="连跑几次自证确定性（>=2 才判定）")
    ap.add_argument("--preflight", action="store_true",
                    help="搬迁前预跑：放开「两侧不得同文」断言（此时两侧本就同文）")
    args = ap.parse_args()

    if not materialize_old(args.ref):
        return 2
    m_old, m_new = md5(OLD_SCRIPT), md5(NEW_SCRIPT)
    print("旧侧 %s  md5=%s" % (os.path.relpath(OLD_SCRIPT, REPO), m_old))
    print("新侧 %s  md5=%s" % (os.path.relpath(NEW_SCRIPT, REPO), m_new))
    if m_old == m_new:
        if not args.preflight:
            print("✗ 两侧文件逐字节相同 —— 这是自比自，不构成等价证据。"
                  "（搬迁前预跑请显式加 --preflight）")
            return 2
        print("⚠ --preflight：两侧同文，本次只验矩阵可跑与确定性，不构成等价证据")
    else:
        print("✓ 两侧 md5 不相等：不是自比自")

    dead = dead_port()
    srv, stub_port = start_stub()
    chat_base = "http://127.0.0.1:%d" % stub_port
    print("死口 127.0.0.1:%d（已绑后即关，确无人监听）　stub 对话台 %s" % (dead, chat_base))

    tmp = tempfile.mkdtemp(prefix="team_cli_equiv_")
    try:
        runs = []
        for i in range(max(1, args.runs)):
            a, b, roots = one_run(dead, chat_base, tmp, "r%d" % i)
            na = normalize(a, roots, (dead, stub_port))
            nb = normalize(b, roots, (dead, stub_port))
            runs.append((na, nb))
            if i == 0:
                ca, cb = split_cases(na), split_cases(nb)
                bad = 0
                print("\n%-4s %-34s %s" % ("#", "用例", "结论"))
                for j, case in enumerate(CASES):
                    x = ca[j] if j < len(ca) else "<缺>"
                    y = cb[j] if j < len(cb) else "<缺>"
                    ok = x == y
                    bad += 0 if ok else 1
                    print("%-4d %-34s %s" % (j, case["name"], "一致" if ok else "**不一致**"))
                    if not ok:
                        print("     ---- 旧 ----\n%s\n     ---- 新 ----\n%s" % (x, y))
                print("\n矩阵：一致 %d / 共 %d" % (len(CASES) - bad, len(CASES)))
                if bad:
                    print("EQUIV_SENTINEL_FAIL")
                    return 1
                print("EQUIV_SENTINEL_OK")

        if args.runs >= 2:
            same = all(r == runs[0] for r in runs)
            print("确定性：%d 次归一后输出%s" % (args.runs, "逐字节相同" if same else "**有漂移**"))
            if not same:
                for i, (na, nb) in enumerate(runs):
                    if (na, nb) != runs[0]:
                        print("  第 %d 次与第 0 次不同（旧侧差异 %s / 新侧差异 %s）"
                              % (i, na != runs[0][0], nb != runs[0][1]))
                print("EQUIV_DETERMINISM_FAIL")
                return 1
            print("EQUIV_DETERMINISM_OK")
        return 0
    finally:
        srv.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
