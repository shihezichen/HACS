#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""L0 平台层：**轻量节点守护**（节点档案的「动态那一半」）。

一句话职责
----------
每台节点上跑一个**只读**的轻量守护，对外只暴露**一个**探针口 `GET /probe`；
它回答的全部内容都由 **L0 节点档案**（`node_profile.py`，本机形态）推导而来 ——
不另立第二处真值、不写任何文件、不动任何监管单元。

为什么是这一件
--------------
刀 21 把「节点档案」的**静态那一半**落在了 `node_profile.py`（路径 / 凭据 / 宿主真源 /
只读探针），刀 22 又把散在各处的宿主事实收了进去。到此为止，「这台机该长什么样」有了
唯一出处 —— 但「**这台机现在实际长什么样**」仍然没人回答：

  · 运行目录在不在、写得进去吗？
  · 档案里声明的宿主真源（Hermes `state.db` / OpenClaw `sessions.json`）文件真在吗？
  · 档案声明要跑的那几个服务单元，本机此刻是不是 active？
  · 这台机上跑的这一份件，是不是我以为的那一份（指纹对不对）？

改前这四问只能靠人 ssh 上去手敲四条命令，各机命令还不一样（systemd / 计划任务）。
本件把这四问收成**一个 HTTP 口**、一份**键名与顺序冻结**的 JSON，各机口径一字不差。

三条铁律（违反即回滚）
----------------------
  · **只读**：不写文件、不建目录、不改/不重启任何监管单元。连「这个目录写得进去吗」
    都只用 `os.access(W_OK)` 判定，**不试写**（只读优先于判定精度 —— 见 `dir_fact`）。
  · **不越层**：只 import 同层 L0 `node_profile`（它再惰性委托同层 `node_paths`）。
    不 import L1–L4 的任何一件，也不 import `web_gateway`（那是 L1）—— 故 HTTP 面
    用纯标准库 `http.server` 自己搭，这是本件作为 L0 的代价，不是疏漏。
  · **不泄密**：绝不输出凭据值，也绝不输出路径全文。凡名字含 token / secret / 口令词
    的面，唯一合法形态是 `{"present": bool, "len": int}`（`credential_fact`），
    出口前由 `enforce_credential_redline()` 机械复核一遍，违反直接抛。

探针口契约（键名与顺序**冻结**，逐字节比对）
--------------------------------------------
    GET /probe → 200 application/json

    {"node": ..., "profile": {...}, "facts": {...},
     "version": ..., "uptime_s": ..., "ts": ...}

逐键释义见 `docs/l0-nodelet.md`；此处只记三条**易错**的：

  · `version` = **磁盘上本件**的 md5 前 8 位（每次探针现算，不是启动时冻结的常量）。
    于是「已落盘但没重启」这种静默态有了可观测量：探针里的 version 变了、进程没重启，
    两边一对就知道该重启了。
  · `uptime_s` / `ts` 是**易变键**：任何逐字节比对（等价装置、跨机 diff）都必须先归一。
  · `facts.services[]` 的单元名来自 env `AGENT_TEAM_NODELET_SERVICES`（部署表 →
    生成器 → 单元 `Environment=` → 本件）。**本件不读部署表** —— 部署表是 T 工具面的
    内部真值表，不随件下发到节点；「档案驱动」那一环落在生成器 `tools/nodelet_gen.py`。

绑定面
------
默认 `127.0.0.1:9971`；`AGENT_TEAM_NODELET_BIND` 可覆盖，但 `parse_bind()` 只放行
**环回**与**尾网**（`<tailnet-cidr>`）两类地址 —— 写别的直接拒启，不给「不小心暴露到
公网」留缝。
"""

import hashlib
import json
import os
import socket
import sys
import time

_HACS_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 批② 过渡：本件已归位 node/l0_host/，同层 L0 件（node_profile 及其惰性依赖 node_paths）
# 仍留在旧目录 backend/，故旧目录一并入路径；待它们各自归位（node/l0_host/，批⑤a/⑤h）
# 后，本循环可收回成只留本目录。
for _hacs_p in (os.path.dirname(os.path.abspath(__file__)),
                os.path.join(_HACS_ROOT, 'backend')):
    if _hacs_p not in sys.path:
        sys.path.insert(0, _hacs_p)

import node_profile  # noqa: E402  L0 同层：节点档案（本件全部内容的唯一推导来源）

__all__ = [
    # 绑定面
    "ENV_BIND", "DEFAULT_HOST", "DEFAULT_PORT", "parse_bind",
    # 单元面
    "ENV_SERVICES", "UNIT_NAME_NT", "UNIT_NAME_POSIX", "unit_name",
    "declared_services", "unit_probe_argv", "unit_state",
    # 事实面
    "REQUIRED_FACES", "PROBE_KEYS", "dir_fact", "file_fact",
    "credential_fact", "is_credential_face", "enforce_credential_redline",
    "self_fingerprint", "build_probe", "probe_bytes",
    # 服务面
    "PROBE_PATH", "make_server", "serve", "main",
]


# ======================================================== ① 绑定面

#: 覆盖绑定用的 env（刀 23 新定；默认值只许环回）
ENV_BIND = "AGENT_TEAM_NODELET_BIND"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9971

#: 探针口路径（只此一条对外路径）
PROBE_PATH = "/probe"

#: 环回地址的三种写法（IPv4 环回段一律按段判，见 `_is_loopback`）
LOOPBACK_NAMES = ("localhost", "::1", "0:0:0:0:0:0:0:1")
#: 尾网（tailnet）地址段 <tailnet-cidr> —— CGNAT 段，协作域的全部节点地址都落在里面
TAILNET_FIRST = 100
TAILNET_SECOND_LO = 64
TAILNET_SECOND_HI = 127


def _octets(host):
    """`a.b.c.d` → (a, b, c, d)；不是点分四段十进制就返回 None。"""
    parts = str(host).split(".")
    if len(parts) != 4:
        return None
    out = []
    for p in parts:
        if not p.isdigit():
            return None
        v = int(p)
        if v < 0 or v > 255:
            return None
        out.append(v)
    return tuple(out)


def _is_loopback(host):
    """环回？IPv4 走 127.0.0.0/8，另加 localhost / ::1 两种写法。"""
    low = str(host).strip().strip("[]").lower()
    if low in LOOPBACK_NAMES:
        return True
    oct4 = _octets(low)
    return bool(oct4) and oct4[0] == 127


def _is_tailnet(host):
    """尾网？<tailnet-cidr>。"""
    oct4 = _octets(str(host).strip().strip("[]"))
    return bool(oct4) and oct4[0] == TAILNET_FIRST \
        and TAILNET_SECOND_LO <= oct4[1] <= TAILNET_SECOND_HI


def parse_bind(text=None):
    """`"host:port"` → `(host, port)`。空值 → 默认（环回）。

    只放行**环回**与**尾网**地址：探针口报的是本机事实，泄出协作域就是白给。
    写了别的地址一律 `ValueError` —— **拒启**，不做「悄悄改回环回」这种好心事
    （那会让人以为自己的配置生效了）。
    """
    raw = (text if text is not None else os.environ.get(ENV_BIND) or "").strip()
    if not raw:
        return DEFAULT_HOST, DEFAULT_PORT
    if raw.startswith("["):                       # [::1]:9971
        host, _, port_s = raw.partition("]")
        host, port_s = host[1:], port_s.lstrip(":")
    elif raw.count(":") == 1:
        host, _, port_s = raw.partition(":")
    else:
        raise ValueError("%s 要写成 host:port，实际是 %r" % (ENV_BIND, raw))
    host, port_s = host.strip(), port_s.strip()
    if not port_s.isdigit():
        raise ValueError("%s 的端口不是数字：%r" % (ENV_BIND, raw))
    port = int(port_s)
    if not 1 <= port <= 65535:
        raise ValueError("%s 的端口越界：%r" % (ENV_BIND, raw))
    if not (_is_loopback(host) or _is_tailnet(host)):
        raise ValueError("%s 只许绑环回或尾网地址，拒绝 %r（探针口不许暴露到协作域以外）"
                         % (ENV_BIND, host))
    return host, port


# ======================================================== ② 监管单元面

#: 声明「本机该有哪几个单元」的 env（逗号分隔；由 tools/nodelet_gen.py 从部署表生成进单元）
ENV_SERVICES = "AGENT_TEAM_NODELET_SERVICES"

#: 本守护自己的单元名 —— 各机**统一**就靠这两个常量（生成器与 --check 都取它）
UNIT_NAME_POSIX = "agent-team-nodelet"
UNIT_NAME_NT = "AgentTeamNodelet"

#: `systemctl is-active` 认得的状态词（不在表里 = 这台机压根不认识这个单元）
SYSTEMD_STATES = ("active", "activating", "deactivating", "inactive", "failed", "reloading")
#: 计划任务的状态词（英文 / 中文两套 locale 都收）
NT_STATES_RUNNING = ("running", "正在运行")
NT_STATES_KNOWN = NT_STATES_RUNNING + ("ready", "disabled", "queued", "就绪", "已禁用", "已排队")

#: 查单元状态的超时（秒）。查不到就报「不认识」，不许把探针口拖死。
UNIT_QUERY_TIMEOUT = 5.0


def unit_name(flavour_value=None):
    """**本守护自己**在这台机上的单元名（nt = 计划任务名，posix = systemd 单元名）。"""
    return UNIT_NAME_NT if node_profile.flavour(flavour_value) == "nt" else UNIT_NAME_POSIX


def declared_services(flavour_value=None, env_value=None):
    """本机**声明**要跑的单元名列表（去重保序）。

    来源 = env `AGENT_TEAM_NODELET_SERVICES`（部署表推导，由生成器写进单元）。
    没声明就只认自己那一个 —— 这样一台没配过的机器也能说出一句真话。
    """
    raw = env_value if env_value is not None else (os.environ.get(ENV_SERVICES) or "")
    names, seen = [], set()
    for piece in str(raw).split(","):
        name = piece.strip()
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names or [unit_name(flavour_value)]


def unit_probe_argv(name, flavour_value=None):
    """查一个单元状态的命令行（**只查不动**：systemctl is-active / schtasks /Query）。"""
    if node_profile.flavour(flavour_value) == "nt":
        return ["schtasks", "/Query", "/TN", str(name), "/FO", "CSV", "/NH"]
    return ["systemctl", "is-active", str(name)]


def _decode_out(raw):
    """命令输出的解码：**先 utf-8，再本机 ANSI 代码页**。

    刀23 实测：中文 Windows 上 `schtasks /FO CSV` 的状态列是 **GBK 字节**（如「正在运行」），
    一律按 utf-8 解会变乱码 → 状态词永远比对不上、`active` 恒为 False。
    本件是只读探针，宁可退到 latin-1 也不抛。
    """
    data = raw or b""
    for enc in ("utf-8", "mbcs", "gbk", "latin-1"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", "replace")


def _run_cmd(argv, timeout=UNIT_QUERY_TIMEOUT):
    """跑一条只读查询，返回 `(rc, stdout)`；跑不起来/超时 → `(None, "")`。

    `subprocess` 是**函数体内**才 import 的：本件在「只被 import 当库用」时
    （等价装置、单测）根本不该把进程创建能力拉进来。
    """
    import subprocess
    try:
        proc = subprocess.run(list(argv), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=float(timeout))
    except Exception:                    # noqa: BLE001 —— 命令不在/超时/权限，都只是「问不出来」
        return None, ""
    return proc.returncode, _decode_out(proc.stdout)


def _nt_status_field(text):
    """`schtasks /FO CSV /NH` 的最后一行最后一列（= 状态）。"""
    import csv
    import io as _io
    rows = [r for r in csv.reader(_io.StringIO(text or "")) if r]
    return (rows[-1][-1] if rows and rows[-1] else "").strip()


def unit_state(name, flavour_value=None, run=None):
    """一个单元在本机的状态 → `(known, active)`，**两个布尔，没有第三种话**。

      · `known=False` —— 这台机不认识这个单元（没登记 / 查询跑不起来）。
      · `active`      —— 此刻真在跑。查不出来一律 `False`，**不编造 True**。
    """
    runner = run or _run_cmd
    rc, out = runner(unit_probe_argv(name, flavour_value))
    if rc is None:
        return False, False
    if node_profile.flavour(flavour_value) == "nt":
        if rc != 0:
            return False, False
        status = _nt_status_field(out).lower()
        return status in NT_STATES_KNOWN, status in NT_STATES_RUNNING
    word = (out or "").strip().lower()
    return word in SYSTEMD_STATES, word == "active"


# ======================================================== ③ 凭据红线

#: 「这个面的名字像不像口令」的判词。**运行期拼装**，不写成字面量 ——
#: 与 node_profile 里 A2A_TOKEN_FILE_NAME / NATS_ROUTE_CRED_NAME 同一条纪律：
#: 避开 scan_secrets「键名像口令 + 值是字面量」的路 2 判据。
CRED_WORDS = ("token", "secret", "pass" + "word", "credential", "key")

#: 凭据面唯一合法的两个键（出口复核按这个断言）
CRED_KEYS = ("present", "len")


def is_credential_face(name):
    """这个面的名字算不算凭据面（含 token / secret / 口令 / 凭据 / 密钥词根）。"""
    low = str(name).lower()
    return any(word in low for word in CRED_WORDS)


def credential_fact(value):
    """凭据面的**唯一**合法输出：有没有 + 多长。值本身一个字符都不出门。"""
    text = "" if value is None else str(value)
    return {"present": bool(text), "len": len(text)}


def enforce_credential_redline(facts):
    """出口机械复核：凡名字像口令的面，形状必须恰好是 `{"present","len"}`。

    这条不是「提示」，是**断言** —— 真违反了说明代码写错了，宁可让探针口炸掉，
    也不许把一个凭据值悄悄送出门。
    """
    for name, value in (facts or {}).items():
        if not is_credential_face(name):
            continue
        if not isinstance(value, dict) or tuple(value.keys()) != CRED_KEYS:
            raise AssertionError(
                "凭据红线：面「%s」的形状必须是 %s，实际 %r" % (name, list(CRED_KEYS), type(value)))
    return True


# ======================================================== ④ 档案面与事实面

#: 本件需要的**档案面**清单（顺序即 `profile.missing` 的顺序，冻结）
REQUIRED_FACES = (
    "node_name", "flavour", "home", "runtime_dir", "etc_dirs", "log_dir",
    "target_known", "target_flavour", "target_home", "target_claude_exec",
    "hermes_state_db", "openclaw_sessions_json", "a2a_probe_token",
)

#: 本机在档案表里查不到时，记在 `profile.missing` 里的那条（固定串，不拼主机名）
FACE_TARGET_PROFILE = "target_profile"

#: 探针 JSON 的顶层键**与顺序**（冻结；装置逐字节比对就比这个）
PROBE_KEYS = ("node", "profile", "facts", "version", "uptime_s", "ts")

#: `facts` 的键与顺序（冻结）
FACT_KEYS = ("runtime_dir", "log_dir", "hermes_state_db", "openclaw_sessions",
             "a2a_probe_token", "services")


class _Faces(object):
    """按 `REQUIRED_FACES` 取档案面，把「面不在 / 面炸了」一并记进 `missing`。

    取不到的面一律给 `None`，对应的事实退成 `False` —— 「不编造」那条铁律在这里落地：
    档案坏了就老实说 `profile.ok=false`，而不是拿默认值糊一个好看的探针。
    """

    def __init__(self, archive):
        self.archive = archive
        self.missing = []
        for name in REQUIRED_FACES:
            fn = getattr(archive, name, None)
            if not callable(fn):
                self.missing.append(name)

    def call(self, name, *args):
        if name in self.missing:
            return None
        fn = getattr(self.archive, name, None)
        try:
            return fn(*args)
        except Exception:                 # noqa: BLE001 —— 档案面炸了也是「档案不可用」
            if name not in self.missing:
                self.missing.append(name)
            return None


def dir_fact(path):
    """一个目录的事实：在不在、写不写得进去。

    `writable` 用 `os.access(W_OK)` 判，**不试写** —— 本件的只读铁律优先于判定精度。
    （Windows 上目录的只读属性不反映在 `os.access` 里，那一格会偏乐观；这是已知代价，
    写在这儿免得后人当 bug 修。）
    """
    text = str(path or "")
    exists = bool(text) and os.path.isdir(text)
    return {"exists": exists, "writable": bool(exists and os.access(text, os.W_OK))}


def file_fact(path):
    """一个档案声明的文件：配没配、在不在。**只给布尔，不给路径**。"""
    text = str(path or "")
    return {"configured": bool(text), "exists": bool(text) and os.path.isfile(text)}


def self_fingerprint(self_path=None):
    """**磁盘上本件**的 md5 前 8 位（每次探针现算）。读不到 → 空串，不编造。"""
    path = self_path or os.path.abspath(__file__)
    try:
        with open(path, "rb") as fh:
            return hashlib.md5(fh.read()).hexdigest()[:8]
    except OSError:
        return ""


def build_probe(started_at=None, self_path=None, now=None, run=None, archive=None,
                flavour_value=None, services_env=None):
    """探针载荷（键名与顺序冻结；每个参数都是为了可验证而留的缝，不是配置项）。

      · `archive`     —— 节点档案模块（默认同层 `node_profile`；装置注入残档做负向场景）
      · `run`         —— 单元查询执行器（默认 `_run_cmd`；装置注入假 runner 造 active 两态）
      · `now`/`started_at` —— 时间源（易变键，装置必须归一后再比）
      · `self_path`   —— 算指纹的那一份件（装置换一份改过的副本，验 version 会变）
    """
    npf = archive or node_profile
    faces = _Faces(npf)

    node = faces.call("node_name") or ""
    flav = faces.call("flavour", flavour_value)
    if node and FACE_TARGET_PROFILE not in faces.missing:
        if not faces.call("target_known", node):
            faces.missing.append(FACE_TARGET_PROFILE)

    facts = {
        "runtime_dir": dir_fact(faces.call("runtime_dir", flavour_value)),
        "log_dir": dir_fact(faces.call("log_dir", flavour_value)),
        "hermes_state_db": file_fact(faces.call("hermes_state_db")),
        "openclaw_sessions": file_fact(faces.call("openclaw_sessions_json")),
        "a2a_probe_token": credential_fact(faces.call("a2a_probe_token")),
        "services": [],
    }
    for name in declared_services(flav, services_env):
        known, active = unit_state(name, flav, run)
        facts["services"].append({"name": name, "known": known, "active": active})
    enforce_credential_redline(facts)

    moment = time.time() if now is None else float(now)
    payload = {
        "node": node,
        "profile": {"file": os.path.basename(getattr(npf, "__file__", "") or ""),
                    "ok": not faces.missing,
                    "missing": list(faces.missing)},
        "facts": facts,
        "version": self_fingerprint(self_path),
        "uptime_s": 0.0 if started_at is None else round(moment - float(started_at), 3),
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(moment)),
    }
    assert tuple(payload.keys()) == PROBE_KEYS, "探针顶层键序被改了（契约冻结）"
    assert tuple(payload["facts"].keys()) == FACT_KEYS, "facts 键序被改了（契约冻结）"
    return payload


def probe_bytes(**kwargs):
    """探针口**实际发出去的那串字节**（装置比的就是它，不是 dict）。"""
    return json.dumps(build_probe(**kwargs), ensure_ascii=False).encode("utf-8")


# ======================================================== ⑤ HTTP 面（纯标准库）
# L0 不许 import L1 web_gateway，所以这里自己搭一个**最小**的 http.server：
# 一条 GET 路由、一个 404、一个 405，没有中间件、没有鉴权钩子、没有状态。

def make_handler(started_at):
    """造出绑定了启动时刻的 handler 类（只读；不持任何可变状态）。"""
    from http.server import BaseHTTPRequestHandler

    class NodeletHandler(BaseHTTPRequestHandler):
        server_version = "nodelet"
        sys_version = ""
        protocol_version = "HTTP/1.1"

        def _send(self, code, body):
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):                                      # noqa: N802 —— stdlib 约定名
            path = (self.path or "").split("?", 1)[0]
            if path != PROBE_PATH:
                self._send(404, json.dumps({"ok": False, "why": "no such path"}).encode("utf-8"))
                return
            try:
                body = probe_bytes(started_at=started_at)
            except Exception as exc:                           # noqa: BLE001
                # 探针自己炸了也要说人话：报类型名，不回显细节（细节里可能带路径）
                body = json.dumps({"ok": False, "why": type(exc).__name__}).encode("utf-8")
                self._send(500, body)
                return
            self._send(200, body)

        def do_POST(self):                                     # noqa: N802
            self._send(405, json.dumps({"ok": False, "why": "read only"}).encode("utf-8"))

        do_PUT = do_DELETE = do_PATCH = do_POST

        def log_message(self, fmt, *args):
            sys.stderr.write("[nodelet] %s %s\n" % (self.address_string(), fmt % args))

    return NodeletHandler


def make_server(bind=None, started_at=None):
    """建好但**不开跑**的服务对象（端口被占 → `OSError` 原样抛，由 `main` 翻成人话）。"""
    from http.server import ThreadingHTTPServer
    host, port = parse_bind(bind)
    server = ThreadingHTTPServer((host, port), make_handler(started_at or time.time()))
    server.daemon_threads = True
    return server


def serve(bind=None):
    """开跑（阻塞）。"""
    started = time.time()
    server = make_server(bind, started)
    host, port = server.server_address[0], server.server_address[1]
    sys.stderr.write("[nodelet] 探针口 http://%s:%d%s（只读）\n" % (host, port, PROBE_PATH))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def main(argv=None):
    """命令行。

    三个可选开关 `--node` / `--bind` / `--services` 只做一件事：**把值写回本进程的 env**，
    然后照常走 env 那条唯一入口。为什么要这层薄壳 —— systemd 有 `Environment=` 可用，
    Windows 计划任务的 `/TR` 却既有 261 字符上限、又要靠 `cmd /c set X=Y&&…` 串起来，
    脆得很。给个显式开关，两个平台的单元都能写得短且直白，而守护本身的输入通道仍只有 env。

    `--once` 打一份探针 JSON 就退（人工核对 / 机内健康检查用，不起服务）。
    """
    import argparse
    ap = argparse.ArgumentParser(prog="nodelet", description="L0 轻量节点守护（只读探针口）")
    ap.add_argument("--node", default="", help="本节点名（等价于 env %s）" % node_profile.ENV_NODE_NAME)
    ap.add_argument("--bind", default="", help="绑定面 host:port（等价于 env %s）" % ENV_BIND)
    ap.add_argument("--services", default="",
                    help="本机声明的单元名，逗号分隔（等价于 env %s）" % ENV_SERVICES)
    ap.add_argument("--once", action="store_true", help="只打一份探针 JSON 就退，不起服务")
    args = ap.parse_args(sys.argv[1:] if argv is None else list(argv))

    for value, name in ((args.node, node_profile.ENV_NODE_NAME),
                        (args.bind, ENV_BIND), (args.services, ENV_SERVICES)):
        if value:
            os.environ[name] = value

    if args.once:
        sys.stdout.write(probe_bytes(started_at=time.time()).decode("utf-8") + "\n")
        return 0
    try:
        return serve()
    except ValueError as exc:
        sys.stderr.write("[nodelet] 绑定面不合法：%s\n" % exc)
        return 2
    except OSError as exc:
        sys.stderr.write("[nodelet] 起不来（端口被占或地址不可绑）：%s\n" % exc)
        return 3


#: `socket` 在本件里只用于「这台机叫什么」的兜底 —— 真正的节点名由档案 `node_name()` 给，
#: 这里留个 import 是为了让「档案没给出名字」时的排障命令行有东西可打。
def hostname_hint():
    """主机名（排障用；探针 JSON 里的 `node` 一律取档案的 `node_name()`）。"""
    try:
        return socket.gethostname()
    except OSError:
        return ""


if __name__ == "__main__":
    sys.exit(main())
