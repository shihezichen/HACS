#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
teamd.py —— 团队 NATS 客户端守护（每个成员节点跑一份）

它是「NATS 中枢」与「本机 A2A 服务（a2a_svc.py:9920）」之间的翻译官：

    别的成员                NATS 中枢 <NODE_IP>:9930          本机
    a2a.v1.req.<member>  ───────────────────────────────────▶  teamd
                                                                  │ POST /a2a/<member>/messages
                                                                  ▼
                                                              a2a_svc.py
    ◀─────────── publish(reply, A2A 的响应 JSON 原样) ─────────────┘

同时：
  * 每 30s 往 `team.hb.<member>` 发心跳；
  * 断线按 1s→2s→5s→10s→30s 退避重连，重连后重新订阅；
  * 每 60s 用 PING/PONG 探活（flush 失败即判定断线）；
  * 127.0.0.1:9960 起管理口，`GET /healthz`、`GET /varz`（只绑回环，不含口令）。

一成员一凭据（2026-09-20）：
  同机多个成员时，**每个成员各自一条独立 NATS 连接**，用该成员自己的 user + 口令文件；
  互不共享连接，一个成员的凭据失效/断线不影响其他成员。中枢白名单因此能对所有成员
  收成同一形状（只允许 `team.hb.<自己>`），不再需要按成员开洞的委托特例表。

设计约束：纯标准库、零 pip 依赖，语法兼容 Python 3.9（不用 X|Y 注解 / match / list[str]）；
Windows 与 Linux 都能跑（signal 注册一律 try/except 兜住）。

用法（单成员，向后兼容旧写法）：
    python teamd.py --member <MEMBER_ID> \\
        --nats-host <NODE_IP> --nats-port 9930 \\
        --user <MEMBER_ID> --password-file <PASS_FILE> \\
        --a2a-base http://127.0.0.1:9920 --token-file <TOKEN_FILE> \\
        --admin-port 9960

用法（同机多成员，每人一套凭据）：
    python teamd.py --member <MEMBER_ID_1>,<MEMBER_ID_2>,<MEMBER_ID_3> \\
        --nats-host <NODE_IP> --nats-port 9930 \\
        --user <MEMBER_ID_1>=<USER_ID_1> --user <MEMBER_ID_2>=<USER_ID_2> --user <MEMBER_ID_3>=<USER_ID_3> \\
        --password-file <MEMBER_ID_1>=<RUNTIME_DIR>\\nats-<MEMBER_ID_1>.pass \\
        --password-file <MEMBER_ID_2>=<RUNTIME_DIR>\\nats-<MEMBER_ID_2>.pass \\
        --password-file <MEMBER_ID_3>=<RUNTIME_DIR>\\nats-<MEMBER_ID_3>.pass \\
        --a2a-base http://<NODE_IP>:9920 \\
        --token-file <MEMBER_ID_1>=<RUNTIME_DIR>\\<MEMBER_ID_1>-token \\
        --token-file <MEMBER_ID_2>=<RUNTIME_DIR>\\<MEMBER_ID_2>-token \\
        --token-file <MEMBER_ID_3>=<RUNTIME_DIR>\\<MEMBER_ID_3>-token \\
        --admin-port 9960

--user / --password-file 都缺省时按平台自动推断路径：
    Linux   <PASS_DIR>/<member>.pass
    Windows <PASS_DIR>\\nats-<member>.pass
推断不到就在启动时报清楚哪个成员缺凭据并退出（非 0 退出码），不静默降级。
"""

import argparse
import inspect
import json
import os
import queue
import re
import socket
import ssl  # noqa: F401  (urllib 在部分环境下需要先加载 ssl，显式 import 以免打包后缺模块)
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple  # noqa: F401

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nats_client import NatsClient, NatsError, read_secret_file  # noqa: E402

# PHASE2_TODO: 待节点注入 env 后删除
AGENT_TEAM_RUNTIME_DIR = os.environ.get("AGENT_TEAM_RUNTIME_DIR") or "<home>\\agent-team"

VERSION = "1.2"
REQ_SUBJECT_FMT = "a2a.v1.req.%s"
HB_SUBJECT_FMT = "team.hb.%s"
REQ_SID = "teamd-req"
_MEMBER_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")  # 成员名：字母数字._-

DEFAULT_TIMEOUT_S = 120.0
MAX_TIMEOUT_S = 600.0   # 调用方**声明**的上限，不再等于一个回合实际能用多久。
                        # 2026-09-22: 300→600. 值守点名 <MEMBER_ID> 的回合常>300s(带工具调用),
                        # 300 上限会把整轮砍成 timeout, 群里表现为"它没回应"。
                        # 2026-09-22 二改: 实际回合时长由 HARD_ROUND_CAP_S 兜底(取二者小值)。
MIN_TIMEOUT_S = 0.1          # 下限只防 0/负数，方便自测跑短超时
HARD_ROUND_CAP_S = 240.0     # 回合硬上限（秒）：单条 A2A 请求从进总线到**必须**回包。
                             # 独立于 MAX_TIMEOUT_S(那是调用方声明的上限)，实际取 min(声明, 本值)。
                             # 到点还没结果就回 error=timeout 并立刻放掉 worker，不许占坑干等。
HARD_WALL_GRACE_S = 8.0      # 硬墙护栏：worker 最多多等这么久（a2a_svc 自己的 504 走 5s margin 先到，
                             # 这条只兜「HTTP 层卡住/不返回」的底）
DETAIL_MAX = 200             # 错误 detail 截断长度
MAX_PAYLOAD_BYTES = 262144   # 单条 NATS 请求体上限 256 KiB

BACKOFF_STEPS = (1.0, 2.0, 5.0, 10.0, 30.0)

COUNTER_KEYS = ("rx_requests", "ok", "errors", "timeouts", "hb_sent", "reconnects",
                "bad_requests", "hb_failed", "busy_rejected", "inbox_replayed", "inbox_errors")

# 拒收 busy 时建议的重试间隔（毫秒）：让发送方知道「等会儿再来」，
# 而不是盲等 135 秒超时、还以为消息丢了。
BUSY_RETRY_AFTER_MS = 2000

# 队列与并发闸门默认值（评审硬伤 1：原先「来一条开一个线程」无上限）
DEFAULT_QUEUE_SIZE = 64         # 有界队列深度
DEFAULT_INBOX_REPLAY_LIMIT = 20   # 一次离线补拉最多领几条
INBOX_HTTP_TIMEOUT_S = 10.0       # 补拉的 HTTP 超时：短一点，别干等
DEFAULT_WORKERS = 8             # worker 池大小。2026-09-22: 4→8（原来 4 个 worker 被
                                # 卡死回合占满后，别的成员的请求只能排队/被 busy 拒收）。
                                # 真实并发上限仍由 per_member_limit 按成员控制。
DEFAULT_PER_MEMBER_LIMIT = 2    # 单成员在途上限（一个 agent 忙不该堵住别人，也不该自我雪崩）


# ---------------------------------------------------------------- 日志

_LOG_LOCK = threading.Lock()


def log(event, member=None, **fields):
    # type: (str, Optional[str], Any) -> None
    """stdout 一行一事件，带时间戳与成员前缀。不走 logging，免得被上游配置影响。"""
    parts = [time.strftime("%Y-%m-%d %H:%M:%S")]
    if member:
        parts.append("[%s]" % member)
    parts.append(event)
    for key in sorted(fields.keys()):
        value = fields[key]
        if isinstance(value, float):
            value = round(value, 3)
        parts.append("%s=%s" % (key, value))
    line = " ".join(str(p) for p in parts)
    with _LOG_LOCK:
        try:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()
        except Exception:
            pass


def truncate(text, limit=DETAIL_MAX):
    # type: (Any, int) -> str
    s = text if isinstance(text, str) else str(text)
    return s if len(s) <= limit else (s[:limit] + "…")


# ---------------------------------------------------------------- 计数器

class Counters(object):
    """线程安全计数器。errors 含 timeouts / bad_requests（它们是 errors 的子类）。"""

    def __init__(self):
        # type: () -> None
        self._lock = threading.Lock()
        self._data = {}  # type: Dict[str, int]
        for key in COUNTER_KEYS:
            self._data[key] = 0

    def inc(self, key, delta=1):
        # type: (str, int) -> None
        with self._lock:
            self._data[key] = self._data.get(key, 0) + delta

    def snapshot(self):
        # type: () -> Dict[str, int]
        with self._lock:
            return dict(self._data)


# ---------------------------------------------------------------- 凭据解析（不连网，可单元测试）

def _parse_member_kv(entries):
    # type: (List[str]) -> Tuple[Dict[str, str], List[str]]
    """把 `--user`/`--password-file` 的原始值列表拆成「成员=值」与「裸值」两类。"""
    keyed = {}   # type: Dict[str, str]
    unkeyed = []  # type: List[str]
    for entry in entries:
        key, sep, value = str(entry).partition("=")
        if sep and _MEMBER_RE.match(key):
            keyed[key] = value
        else:
            unkeyed.append(entry)
    return keyed, unkeyed


def default_password_path(member):
    # type: (str) -> str
    """两者都缺省时的平台默认口令文件路径。"""
    if os.name == "nt":
        return "%s\\nats-%s.pass" % (os.environ.get("AGENT_TEAM_NATS_DIR") or AGENT_TEAM_RUNTIME_DIR, member)
    return "%s/%s.pass" % (os.environ.get("AGENT_TEAM_NATS_DIR") or "/etc/nats", member)  # PHASE2_TODO: 待节点注入 env 后删除


def resolve_member_creds(members, user_entries, password_file_entries):
    # type: (List[str], List[str], List[str]) -> Dict[str, Dict[str, str]]
    """解析每个成员的 NATS 用户名 + 口令文件路径（只定位路径，不读文件内容）。

    规则：
      * `成员=值` 形式可重复给出，各归各；
      * 裸值（不带 `=成员`前缀）只允许在 --member 单成员时给出一个，
        用于兼容旧写法；--member 有多个成员时给裸值直接报错（避免像今天
        这样一份凭据被误用于多个成员）；
      * 缺省（既没有 keyed 也没有裸值）时按平台推断默认路径；
      * user 缺省时取成员名本身。
    推断/指定的口令文件如果不存在，汇总成一条清晰的 FATAL 报错，非 0 退出。
    """
    user_keyed, user_unkeyed = _parse_member_kv(user_entries or [])
    pass_keyed, pass_unkeyed = _parse_member_kv(password_file_entries or [])

    if user_unkeyed:
        if len(user_unkeyed) > 1:
            raise SystemExit("[FATAL] --user 不带「成员=」前缀时只能给一个值，收到多个: %s"
                             % user_unkeyed)
        if len(members) > 1:
            raise SystemExit(
                "[FATAL] --member 有多个成员时，--user 必须用「成员=用户名」形式给出"
                "（如 --user %s=%s）；收到未加成员前缀的单值: %r"
                % (members[0], user_unkeyed[0], user_unkeyed[0]))
        user_keyed.setdefault(members[0], user_unkeyed[0])

    if pass_unkeyed:
        if len(pass_unkeyed) > 1:
            raise SystemExit("[FATAL] --password-file 不带「成员=」前缀时只能给一个值，收到多个: %s"
                             % pass_unkeyed)
        if len(members) > 1:
            raise SystemExit(
                "[FATAL] --member 有多个成员时，--password-file 必须用「成员=路径」形式给出"
                "（如 --password-file %s=<PASS_DIR>/%s.pass）；收到未加成员前缀的单值: %r"
                % (members[0], members[0], pass_unkeyed[0]))
        pass_keyed.setdefault(members[0], pass_unkeyed[0])

    result = {}   # type: Dict[str, Dict[str, str]]
    missing = []  # type: List[str]
    for m in members:
        user = user_keyed.get(m) or m
        pf = pass_keyed.get(m) or default_password_path(m)
        if not os.path.isfile(os.path.expanduser(pf)):
            missing.append("%s（口令文件不存在: %s）" % (m, pf))
            continue
        result[m] = {"user": user, "password_file": pf}
    if missing:
        raise SystemExit(
            "[FATAL] 以下成员缺 NATS 凭据，请用 --password-file 成员=路径 显式指定，"
            "或确认默认路径存在：\n  " + "\n  ".join(missing))
    return result


# ---------------------------------------------------------------- 配置

class Config(object):
    """teamd 的全部运行参数（口令/令牌单独放，绝不进 /varz；每个成员一套 NATS 凭据）。"""

    def __init__(self, member, nats_host, nats_port, user, password,
                 a2a_base, token, admin_host="127.0.0.1", admin_port=9960,
                 enable_admin=True, hb_interval=30.0, ping_interval=60.0,
                 a2a_timeout_margin=5.0, nats_name=None, watch_stdin=False,
                 inbound=True, members=None, tokens=None, creds=None,
                 queue_size=DEFAULT_QUEUE_SIZE, workers=DEFAULT_WORKERS,
                 per_member_limit=DEFAULT_PER_MEMBER_LIMIT,
                 blackboard_base="", inbox_replay_limit=DEFAULT_INBOX_REPLAY_LIMIT):
        # type: (...) -> None
        self.member = member
        self.nats_host = nats_host
        self.nats_port = int(nats_port)
        self.user = user
        self.password = password
        self.a2a_base = (a2a_base or "").rstrip("/")
        # 中枢（blackboard）基址：离线补拉要从这里领消息。**默认为空 = 不补拉** ——
        # 免得老部署一升级就冒出个没配好地址的后台动作。
        self.blackboard_base = (blackboard_base or "").rstrip("/")
        self.inbox_replay_limit = max(1, int(inbox_replay_limit))
        self.token = token
        self.admin_host = admin_host
        self.admin_port = int(admin_port)
        self.enable_admin = bool(enable_admin)
        self.hb_interval = float(hb_interval)
        self.ping_interval = float(ping_interval)
        self.a2a_timeout_margin = float(a2a_timeout_margin)
        self.nats_name = nats_name or ("teamd-" + member)
        # 多成员：同一台机多个成员共用一个 teamd 进程（否则会抢 9960 端口），
        # 但每个成员各自一条独立 NATS 连接、各自一套凭据（见 self.creds）。
        self.members = [str(m).strip() for m in (members or [member]) if str(m).strip()]
        if member not in self.members:
            self.members.insert(0, member)
        self.tokens = dict(tokens or {})
        if member not in self.tokens and token:
            self.tokens[member] = token
        # 每个成员一套 NATS 凭据：{member: {"user":.., "password":.., "password_file":..}}
        self.creds = dict(creds or {})
        for m in self.members:
            if m not in self.creds:
                # 兜底：直接构造 Config（不经 CLI）且未显式给 creds 时，退化为用顶层
                # user/password（单成员场景下这就是它自己的凭据；生产 CLI 路径不会走到这里）。
                self.creds[m] = {"user": user, "password": password, "password_file": None}
        # 并发闸门：有界队列 + worker 池 + 每成员在途上限。
        # 原先 on_nats_message 每条消息开一个线程、无上限；一旦改成异步 ACK 或
        # 离线补拉（一次投一批），后端就会被瞬间打爆。满了**明确拒收**（busy）。
        self.queue_size = max(1, int(queue_size))
        self.workers = max(1, int(workers))
        self.per_member_limit = max(1, int(per_member_limit))
        self.watch_stdin = bool(watch_stdin)
        # inbound=False：纯出站成员（如 <MEMBER_ID>，按规划零监听）——不订阅自己的点名 subject
        self.inbound = bool(inbound)

    def user_for(self, member):
        # type: (str) -> str
        return self.creds.get(member, {}).get("user") or self.user

    def password_for(self, member):
        # type: (str) -> str
        return self.creds.get(member, {}).get("password") or self.password

    def password_file_for(self, member):
        # type: (str) -> Optional[str]
        return self.creds.get(member, {}).get("password_file")

    @property
    def req_subject(self):
        # type: () -> str
        return REQ_SUBJECT_FMT % self.member

    @property
    def req_subjects(self):
        # type: () -> List[str]
        return [REQ_SUBJECT_FMT % m for m in self.members]

    @property
    def hb_subjects(self):
        # type: () -> List[str]
        return [HB_SUBJECT_FMT % m for m in self.members]

    def a2a_url_for(self, member):
        # type: (str) -> str
        return "%s/a2a/%s/messages" % (self.a2a_base, member)

    def token_for(self, member):
        # type: (str) -> str
        return self.tokens.get(member) or self.tokens.get(self.member) or self.token or ""

    @property
    def hb_subject(self):
        # type: () -> str
        return HB_SUBJECT_FMT % self.member

    @property
    def a2a_url(self):
        # type: () -> str
        return "%s/a2a/%s/messages" % (self.a2a_base, self.member)

    def public_summary(self):
        # type: () -> Dict[str, Any]
        """给 /varz 用的配置摘要 —— 口令与令牌一律不出现，只暴露凭据文件路径。"""
        return {
            "member": self.member,
            "members": list(self.members),
            "nats": {"host": self.nats_host, "port": self.nats_port, "user": self.user},
            "nats_by_member": dict((m, {"user": self.user_for(m),
                                        "password_file": self.password_file_for(m)})
                                   for m in self.members),
            "a2a_base": self.a2a_base,
            "a2a_url": self.a2a_url,
            "subjects": {"request": self.req_subject, "heartbeat": self.hb_subject},
            "admin": {"host": self.admin_host, "port": self.admin_port,
                      "enabled": self.enable_admin},
            "intervals": {"heartbeat_s": self.hb_interval, "ping_s": self.ping_interval},
            "timeout": {"default_s": DEFAULT_TIMEOUT_S, "max_s": MAX_TIMEOUT_S,
                        "hard_cap_s": HARD_ROUND_CAP_S, "wall_grace_s": HARD_WALL_GRACE_S,
                        "a2a_margin_s": self.a2a_timeout_margin},
        }


# ---------------------------------------------------------------- 管理口

class AdminHandler(BaseHTTPRequestHandler):
    """只读管理口。daemon 由 make_admin_server 注入到类属性上。"""

    server_version = "teamd/" + VERSION
    daemon_ref = None  # type: Any

    def log_message(self, fmt, *args):
        return  # 管理口不刷日志，健康检查会很频繁

    def _reply(self, status, obj):
        # type: (int, Dict[str, Any]) -> None
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def do_GET(self):
        path = (self.path or "/").split("?", 1)[0].rstrip("/") or "/"
        daemon = self.__class__.daemon_ref
        if daemon is None:
            self._reply(503, {"ok": False, "error": "not_ready"})
            return
        if path == "/healthz":
            self._reply(200, daemon.healthz())
        elif path == "/varz":
            self._reply(200, daemon.varz())
        else:
            self._reply(404, {"ok": False, "error": "not_found", "path": path})


def make_admin_server(daemon, host, port):
    # type: (TeamDaemon, str, int) -> ThreadingHTTPServer
    handler = type("BoundAdminHandler", (AdminHandler,), {"daemon_ref": daemon})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


# ---------------------------------------------------------------- 单成员连接状态

class MemberConn(object):
    """一个成员的连接状态：各自的 NatsClient + 各自的重连/心跳记录，互不影响。"""

    def __init__(self, member):
        # type: (str) -> None
        self.member = member
        self.client = None            # type: Any
        self.last_err = None          # type: Optional[str]
        self.was_connected = False
        self.reconnects = 0
        self.last_hb_ts = None        # type: Optional[int]
        self.last_req_ts = None       # type: Optional[int]


# ---------------------------------------------------------------- 守护主体

class TeamDaemon(object):
    """每个成员一条独立 NATS 连接 + 一个共用 admin HTTP + 主线程心跳循环。"""

    def __init__(self, config, nats_factory=None, a2a_caller=None, http_caller=None):
        # type: (Config, Optional[Callable], Optional[Callable], Optional[Callable]) -> None
        self.cfg = config
        self.counters = Counters()
        self.started_at = time.time()
        self.host = socket.gethostname()
        self.last_hb_ts = None      # type: Optional[int]
        self.last_req_ts = None     # type: Optional[int]
        self.nats_last_err = None   # type: Optional[str]
        self._stop = threading.Event()
        self._nats_lock = threading.Lock()
        self._admin_server = None   # type: Any
        self._threads = []          # type: List[threading.Thread]
        self._all_tag = ",".join(self.cfg.members)
        self.conns = dict((m, MemberConn(m)) for m in self.cfg.members)
        # 有界任务队列 + 每成员在途计数（收包回调只入队，处理交给固定 worker 池）
        self._task_queue = queue.Queue(maxsize=self.cfg.queue_size)   # type: queue.Queue
        self._inflight = {}                 # type: Dict[str, int]
        self._inflight_lock = threading.Lock()
        self._workers_started = False
        # 可注入：自测用假传输 / 假 A2A，免得依赖真中枢
        self.nats_factory = nats_factory or self._default_nats_factory
        self.a2a_caller = a2a_caller or self._default_a2a_caller
        # 中枢 HTTP 可注入：自测用假中枢，不碰真 9940
        self.http_caller = http_caller or self._default_http_caller
        self.last_inbox_ts = None    # type: Optional[int]
        # 注入的 nats_factory 可能是老的 0 参签名（既有自测直接 `lambda: fake`），
        # 探测参数个数决定要不要把 member 传进去。
        try:
            self._factory_arity = len(inspect.signature(self.nats_factory).parameters)
        except (TypeError, ValueError):
            self._factory_arity = 1
        # 注入的 A2A caller 可能是老 4 参签名（既有自测），探测参数个数决定怎么调
        try:
            self._caller_arity = len(inspect.signature(self.a2a_caller).parameters)
        except (TypeError, ValueError):
            self._caller_arity = 4

    # ------------------------------------------------------------ 默认实现
    def _default_nats_factory(self, member):
        # type: (str) -> NatsClient
        return NatsClient(self.cfg.nats_host, self.cfg.nats_port,
                          self.cfg.user_for(member), self.cfg.password_for(member),
                          name="teamd-%s" % member,
                          on_error=lambda line, _m=member: self._on_nats_err(_m, line))

    def _make_client(self, member):
        # type: (str) -> Any
        if self._factory_arity <= 0:
            return self.nats_factory()
        return self.nats_factory(member)

    def _on_nats_err(self, member, err_line):
        # type: (str, str) -> None
        conn = self.conns.get(member)
        if conn is not None:
            conn.last_err = err_line
        self.nats_last_err = err_line
        log("nats.err", member=member, detail=truncate(err_line, 160))

    # ------------------------------------------------------------ 状态输出
    @property
    def uptime_s(self):
        # type: () -> float
        return time.time() - self.started_at

    def nats_status(self, member=None):
        # type: (Optional[str]) -> Dict[str, Any]
        """聚合/主成员的连接状态（向后兼容旧的单连接 /healthz 形状）；
        每个成员的独立明细见 healthz() 里的 members_detail。"""
        member = member or self.cfg.member
        conn = self.conns.get(member)
        client = conn.client if conn is not None else None
        connected = bool(client is not None and getattr(client, "is_connected", False))
        last_err = getattr(client, "last_err", None) if client is not None else None
        return {"connected": connected,
                "last_err": last_err or (conn.last_err if conn is not None else None) or self.nats_last_err,
                "host": self.cfg.nats_host, "port": self.cfg.nats_port}

    def healthz(self):
        # type: () -> Dict[str, Any]
        # NATS 断线时依然返 200，只把 connected 打成 false —— 健康检查不该因中枢抖动而拔掉本机。
        members_detail = {}
        for m in self.cfg.members:
            conn = self.conns[m]
            client = conn.client
            connected = bool(client is not None and getattr(client, "is_connected", False))
            last_err = getattr(client, "last_err", None) if client is not None else None
            members_detail[m] = {
                "connected": connected,
                "reconnects": conn.reconnects,
                "last_hb_ts": conn.last_hb_ts,
                "last_req_ts": conn.last_req_ts,
                "subject": (REQ_SUBJECT_FMT % m) if self.cfg.inbound else None,
                "user": self.cfg.user_for(m),
                "password_file": self.cfg.password_file_for(m),
                "last_err": last_err or conn.last_err,
            }
        return {"ok": True, "member": self.cfg.member, "members": list(self.cfg.members),
                "members_detail": members_detail,
                "inbound": bool(self.cfg.inbound), "nats": self.nats_status(),
                "last_hb_ts": self.last_hb_ts, "counters": self.counters.snapshot(),
                "queue": self.queue_stats(),
                "inbox_replayed": self.counters.snapshot().get("inbox_replayed"),
                "inbox_errors": self.counters.snapshot().get("inbox_errors"),
                "last_inbox_ts": self.last_inbox_ts,
                "uptime_s": int(self.uptime_s), "version": VERSION,
                "ts": int(time.time())}

    def varz(self):
        # type: () -> Dict[str, Any]
        out = {"ok": True, "version": VERSION, "member": self.cfg.member,
               "host": self.host, "pid": os.getpid(),
               "started_at": int(self.started_at), "uptime_s": int(self.uptime_s),
               "counters": self.counters.snapshot(),
               "counters_note": "errors 含 timeouts 与 bad_requests",
               "nats": self.nats_status(),
               "last_hb_ts": self.last_hb_ts, "last_req_ts": self.last_req_ts,
               "config": self.cfg.public_summary(),
               "python": sys.version.split()[0],
               "ts": int(time.time())}
        return out

    # ------------------------------------------------------------ A2A 调用
    def _call_a2a(self, member, text, session_id, timeout_s, caller):
        # type: (str, str, Optional[str], float, str) -> Tuple[int, bytes]
        """按 arity 兼容两种注入签名：老测试的 4 参 (text, session, timeout, caller) 与新的 5 参。"""
        if self._caller_arity >= 5:
            return self.a2a_caller(text, session_id, timeout_s, caller, member)
        return self.a2a_caller(text, session_id, timeout_s, caller)

    def _default_a2a_caller(self, text, session_id, timeout_s, caller, member=None):
        # type: (str, Optional[str], float, str) -> Tuple[int, bytes]
        """POST 到本机 a2a_svc。返回 (http_status, 原始响应体)。网络层异常直接抛。"""
        payload = {"text": text, "session_id": session_id, "timeout_s": timeout_s}
        if caller:
            payload["from"] = caller
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        target = self.cfg.a2a_url_for(member) if member else self.cfg.a2a_url
        req = urllib.request.Request(target, data=body, method="POST")
        req.add_header("Content-Type", "application/json; charset=utf-8")
        req.add_header("Authorization", "Bearer " + self.cfg.token_for(member or self.cfg.member))
        # HTTP 读超时比业务超时多留 margin：让 a2a_svc 有机会自己返 504（错误更具体）
        http_timeout = timeout_s + self.cfg.a2a_timeout_margin
        try:
            resp = urllib.request.urlopen(req, timeout=http_timeout)
            try:
                return resp.getcode(), resp.read()
            finally:
                resp.close()
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read()
            except Exception:
                raw = b""
            return exc.code, raw

    def _call_a2a_hard_walled(self, member, text, session_id, timeout_s, caller):
        # type: (str, str, Optional[str], float, str) -> Optional[Tuple[int, bytes]]
        """给 _call_a2a 套一道「回合硬墙」：worker 最多等 timeout_s + HARD_WALL_GRACE_S。

        到点还拿不到结果就返回 None —— 调用方立刻回 error=timeout、马上放掉 worker，
        不占坑。被丢下的那个线程仍会卡在 socket 读上（读超时 = timeout_s + a2a_margin，
        最多多活几秒），它不回包、不写日志，所以不会产生第二条回包。
        """
        box = {}  # type: Dict[str, Any]

        def _run():
            # type: () -> None
            try:
                box["res"] = self._call_a2a(member, text, session_id, timeout_s, caller)
            except BaseException as exc:   # 交回主线程走原有异常分支
                box["exc"] = exc

        worker = threading.Thread(target=_run, name="teamd-a2a-%s" % member)
        worker.daemon = True
        worker.start()
        worker.join(timeout_s + HARD_WALL_GRACE_S)
        if worker.is_alive():
            log("req.hard_wall", member=member, frm=(caller or "-"),
                waited_s=round(timeout_s + HARD_WALL_GRACE_S, 3))
            return None
        if "exc" in box:
            raise box["exc"]
        return box.get("res")

    # ------------------------------------------------------------ 请求处理
    @staticmethod
    def _clamp_timeout(raw):
        # type: (Any) -> float
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return DEFAULT_TIMEOUT_S
        if value <= 0:
            return DEFAULT_TIMEOUT_S
        if value > MAX_TIMEOUT_S:
            return MAX_TIMEOUT_S
        if value < MIN_TIMEOUT_S:
            return MIN_TIMEOUT_S
        return value

    def handle_request(self, payload, reply, member=None):
        # type: (bytes, str, Optional[str]) -> bytes
        """NATS 请求 → A2A → 回包（回包用**这个成员自己**的连接发出）。返回实际回给
        reply 的字节（自测直接调它）。

        member：点名的是哪个成员（多成员共用一个 teamd 进程时按 subject 分流）。
        """
        member = member or self.cfg.member
        started = time.time()
        self.counters.inc("rx_requests")
        self.last_req_ts = int(started)
        conn = self.conns.get(member)
        if conn is not None:
            conn.last_req_ts = int(started)
        caller = ""
        try:
            if len(payload) > MAX_PAYLOAD_BYTES:
                return self._respond(member, reply, self._err("bad_request", "payload 超过 %d 字节"
                                                              % MAX_PAYLOAD_BYTES), started, caller)
            try:
                obj = json.loads(payload.decode("utf-8"))
            except Exception as exc:
                return self._respond(member, reply, self._err("bad_request", "payload 不是合法 JSON: %s" % exc),
                                     started, caller)
            if not isinstance(obj, dict):
                return self._respond(member, reply, self._err("bad_request", "payload 顶层必须是对象"),
                                     started, caller)

            text = obj.get("text")
            caller = str(obj.get("from") or "")
            if not isinstance(text, str) or not text.strip():
                return self._respond(member, reply, self._err("bad_request", "缺少必填字段 text"),
                                     started, caller)

            session_id = obj.get("session_id")
            if session_id is not None:
                session_id = str(session_id)
            declared_s = self._clamp_timeout(obj.get("timeout_s"))
            # 回合硬上限：调用方声明只是"最多", 真正可用时长还要过 240s 这道墙。
            timeout_s = min(declared_s, HARD_ROUND_CAP_S)

            log("req.recv", member=member, frm=(caller or "-"),
                chars=len(text), timeout_s=timeout_s, declared_s=declared_s,
                session=(session_id or "-"))

            try:
                result = self._call_a2a_hard_walled(member, text, session_id, timeout_s, caller)
                if result is None:
                    # 硬墙到期：立刻回包 + 放 worker（不必等 socket 自己超时）
                    return self._respond(member, reply,
                                         self._err("timeout", "回合硬上限到期：等了 %ds 无回包（声明 %ds，硬上限 %ds）"
                                                   % (int(timeout_s + HARD_WALL_GRACE_S), int(declared_s),
                                                      int(HARD_ROUND_CAP_S))),
                                         started, caller)
                status, raw = result
            except (socket.timeout, TimeoutError) as exc:
                return self._respond(member, reply, self._err("timeout", "A2A 超时: %s" % (exc or "read timed out")),
                                     started, caller)
            except urllib.error.URLError as exc:
                if isinstance(getattr(exc, "reason", None), (socket.timeout, TimeoutError)):
                    return self._respond(member, reply, self._err("timeout", "A2A 超时: %s" % exc.reason),
                                         started, caller)
                return self._respond(member, reply, self._err("network_error", "连不上本机 A2A: %s" % exc),
                                     started, caller)
            except Exception as exc:
                return self._respond(member, reply, self._err("internal", "%s: %s" % (exc.__class__.__name__, exc)),
                                     started, caller)

            if status == 200:
                self.counters.inc("ok")
                return self._send_reply(member, reply, raw, started, caller, status)
            if status == 504:
                # a2a_svc 自己判超时（backend_timeout）→ 对调用方统一成 timeout
                return self._respond(member, reply, self._err("timeout", self._body_text(raw)), started, caller)
            return self._respond(member, reply,
                                 self._err("http_error", "HTTP %d: %s" % (status, self._body_text(raw)),
                                           status=status),
                                 started, caller)
        except Exception as exc:  # 兜底：任何意外都要给调用方一个回包，绝不让它死等
            return self._respond(member, reply, self._err("internal", "%s: %s" % (exc.__class__.__name__, exc)),
                                 started, caller)

    @staticmethod
    def _body_text(raw):
        # type: (bytes) -> str
        try:
            return truncate(raw.decode("utf-8", "replace").strip())
        except Exception:
            return ""

    @staticmethod
    def _err(kind, detail="", status=None):
        # type: (str, str, Optional[int]) -> Dict[str, Any]
        out = {"ok": False, "error": kind, "detail": truncate(detail)}
        if status is not None:
            out["status"] = status
        return out

    def _respond(self, member, reply, obj, started, caller):
        # type: (str, str, Dict[str, Any], float, str) -> bytes
        kind = obj.get("error") or "error"
        self.counters.inc("errors")
        if kind == "timeout":
            self.counters.inc("timeouts")
        elif kind == "bad_request":
            self.counters.inc("bad_requests")
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        return self._send_reply(member, reply, raw, started, caller, None, error=kind)

    def _send_reply(self, member, reply, raw, started, caller, status=None, error=None):
        # type: (str, str, bytes, float, str, Optional[int], Optional[str]) -> bytes
        elapsed_ms = int((time.time() - started) * 1000)
        if reply:
            try:
                self._publish(member, reply, raw)
            except Exception as exc:
                log("reply.fail", member=member, to=reply, detail=truncate(str(exc), 160))
        log("req.done", member=member, frm=(caller or "-"), elapsed_ms=elapsed_ms, bytes=len(raw),
            status=(status if status is not None else "-"), error=(error or "-"),
            reply=(reply or "-"))
        return raw

    # ------------------------------------------------------------ NATS 收发
    def _publish(self, member, subject, raw, reply=""):
        # type: (str, str, bytes, str) -> None
        with self._nats_lock:
            conn = self.conns.get(member)
            client = conn.client if conn is not None else None
        if client is None:
            raise NatsError("成员 %s 的 NATS 未连接" % member)
        client.publish(subject, raw, reply=reply)

    # ------------------------------------------------------------ 并发闸门（有界队列 + worker 池）
    def start_workers(self):
        # type: () -> None
        """起固定 worker 池消费任务队列。必须早于 start_nats()：先有消费者再收包。"""
        with self._inflight_lock:       # 兼作启动锁：多 reader 线程并发懒启动时只起一份
            if self._workers_started:
                return
            self._workers_started = True
        for i in range(self.cfg.workers):
            t = threading.Thread(target=self._worker_loop, name="teamd-worker-%d" % i)
            t.daemon = True
            t.start()
            self._threads.append(t)
        log("teamd.workers", count=self.cfg.workers, queue_size=self.cfg.queue_size,
            per_member_limit=self.cfg.per_member_limit)

    def _worker_loop(self):
        # type: () -> None
        """worker：从有界队列逐个取任务处理。单条任务崩了也不能带走整个 worker。"""
        while not self._stop.is_set():
            try:
                item = self._task_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            payload, reply, member = item
            try:
                self.handle_request(payload, reply, member)
            except Exception as exc:        # 兜底：worker 绝不能因单条任务死掉
                self.counters.inc("errors")
                log("worker.error", member=member, detail=truncate(str(exc), 160))
            finally:
                self._release(member)
                self._task_queue.task_done()

    def _admit(self, member):
        # type: (str) -> Optional[str]
        """尝试占用一个并发额度。放行返回 None，否则返回拒收原因（人类可读）。"""
        if self._task_queue.full():
            return "队列已满(%d/%d)" % (self._task_queue.qsize(), self.cfg.queue_size)
        with self._inflight_lock:
            cur = self._inflight.get(member, 0)
            if cur >= self.cfg.per_member_limit:
                return "成员 %s 在途已达上限(%d)" % (member, self.cfg.per_member_limit)
            self._inflight[member] = cur + 1
        return None

    def _release(self, member):
        # type: (str) -> None
        with self._inflight_lock:
            cur = self._inflight.get(member, 0)
            if cur > 1:
                self._inflight[member] = cur - 1
            else:
                self._inflight.pop(member, None)

    def queue_stats(self):
        # type: () -> Dict[str, Any]
        """队列/闸门可观测性快照（healthz、varz 用）。"""
        with self._inflight_lock:
            inflight = dict(self._inflight)
        return {"depth": self._task_queue.qsize(), "capacity": self.cfg.queue_size,
                "workers": self.cfg.workers, "per_member_limit": self.cfg.per_member_limit,
                "inflight": inflight}

    # ------------------------------------------------------------ 离线补拉（P2-b）
    def http_headers(self):
        # type: () -> Dict[str, str]
        """调中枢用的头。token 取**本成员自己**的（多成员共用进程时按成员取）。"""
        headers = {"Accept": "application/json"}   # type: Dict[str, str]
        token = self.cfg.token_for(self.cfg.member)
        if token:
            headers["Authorization"] = "Bearer " + token
        return headers

    def _default_http_caller(self, method, url, body=None):
        # type: (str, str, Optional[bytes]) -> Tuple[int, Dict[str, Any]]
        req = urllib.request.Request(url, data=body, method=method)
        for key, value in self.http_headers().items():
            req.add_header(key, value)
        if body is not None:
            req.add_header("Content-Type", "application/json; charset=utf-8")
        resp = None
        try:
            resp = urllib.request.urlopen(req, timeout=INBOX_HTTP_TIMEOUT_S)
            raw, code = resp.read(), resp.getcode()
        except urllib.error.HTTPError as exc:
            raw, code = exc.read(), exc.code
        finally:
            if resp is not None:
                try:
                    resp.close()
                except Exception:
                    pass
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            data = {}
        return code, (data if isinstance(data, dict) else {})

    def _claim(self, msg_id):
        # type: (str) -> bool
        """占一个并发额度并 ack 认领。**顺序不能反**：先认领再排队，一旦队列满就会
        出现"已经认领、却进不了队列"——消息卡死在 processing 上，谁都捞不着。"""
        reason = self._admit(self.cfg.member)
        if reason:
            log("inbox.busy", member=self.cfg.member, detail=reason)
            return False
        try:
            code, payload = self.http_caller(
                "POST", "%s/msg/%s/ack" % (self.cfg.blackboard_base, msg_id))
        except Exception as exc:
            self._release(self.cfg.member)
            self.counters.inc("inbox_errors")
            log("inbox.error", member=self.cfg.member,
                detail=truncate("ack 失败: %s" % exc, 160))
            return False
        if code != 200 or payload.get("repeat"):
            # repeat=True：别人已经认领过 → 绝不能重复喂一遍 LLM
            self._release(self.cfg.member)
            return False
        return True

    def _process_replayed(self, msg):
        # type: (Dict[str, Any]) -> bool
        """把库里那条消息拼成正常请求，**走同一条处理路径**（handle_request）。

        复用而不是另写一条：补拉的消息和刚到的消息必须同一套逻辑（配额、会话、
        回投），否则两条路迟早长歪。额度在这一步 finally 里放掉。
        """
        text = str(msg.get("body") or msg.get("text") or "")
        envelope = {"text": text,
                    "from": str(msg.get("from_member") or msg.get("from") or ""),
                    "session_id": msg.get("session_id") or None,
                    "timeout_s": self._clamp_timeout(msg.get("timeout_s"))}
        if not text.strip():
            self._release(self.cfg.member)
            return False
        raw = json.dumps(envelope, ensure_ascii=False).encode("utf-8")
        try:
            reply_raw = self.handle_request(raw, "", self.cfg.member)
            payload = json.loads(reply_raw.decode("utf-8"))
        except Exception as exc:
            self.counters.inc("errors")
            log("inbox.error", member=self.cfg.member,
                detail=truncate("处理失败: %s" % exc, 160))
            return False
        finally:
            self._release(self.cfg.member)
        # 只有"后端真给了答复"才算处理成功。错误回包要标 failed ——
        # 否则补拉会把一路失败的消息标成 done，问题就悄悄没了。
        ok = (isinstance(payload, dict) and payload.get("ok") is not False
              and bool(payload.get("reply")))
        if ok:
            self._send_back(envelope, payload)
        return ok

    def _send_back(self, envelope, payload):
        # type: (Dict[str, Any], Dict[str, Any]) -> bool
        """把处理结果回投给原发送方（它可能也离线 → 落库即送达）。"""
        to = envelope.get("from")
        if not to:
            return True      # 不知道回给谁（老接口没带 from）：不猜
        body = {"to": to, "text": str(payload.get("reply")),
                "session_id": envelope.get("session_id"),
                "from_member": self.cfg.member}
        try:
            code, _ = self.http_caller("POST", self.cfg.blackboard_base + "/msg",
                                       json.dumps(body, ensure_ascii=False).encode("utf-8"))
        except Exception as exc:
            self.counters.inc("inbox_errors")
            log("inbox.error", member=self.cfg.member,
                detail=truncate("回投失败: %s" % exc, 160))
            return False
        return code == 200

    def _finish(self, msg_id, ok=True):
        # type: (str, bool) -> None
        """标完成/失败。标不上不算大事（sweep 会把卡住的捞出来告警），不抛。"""
        try:
            self.http_caller("POST", "%s/msg/%s/done" % (self.cfg.blackboard_base, msg_id),
                             json.dumps({"ok": bool(ok)}).encode("utf-8"))
        except Exception as exc:
            log("inbox.error", member=self.cfg.member,
                detail=truncate("done 失败: %s" % exc, 160))

    def replay_inbox(self, limit=None):
        # type: (Optional[int]) -> int
        """上线补拉：把「已落库但还没被处理」的消息领回来，重新喂给 worker。

        为什么非有不可：msg_log 让「落库 = 送达」成立（对方离线也不丢），
        但**消息躺在库里不会自己跑出来** —— 得有人在它上线时去领。这就是那个动作。

        出任何错都不带崩 teamd：只计数 + 记日志。消息还在库里，下次上线再拉。
        """
        base = self.cfg.blackboard_base
        if not base:
            return 0
        limit = max(1, int(limit or self.cfg.inbox_replay_limit))
        url = "%s/msg?to=%s&state=queued&limit=%d" % (
            base, urllib.parse.quote(self.cfg.member), limit)
        try:
            code, payload = self.http_caller("GET", url)
        except Exception as exc:
            self.counters.inc("inbox_errors")
            log("inbox.error", member=self.cfg.member,
                detail=truncate("拉取失败: %s" % exc, 160))
            return 0
        if code != 200:
            self.counters.inc("inbox_errors")
            log("inbox.error", member=self.cfg.member, detail="拉取返回 HTTP %s" % code)
            return 0
        msgs = payload.get("messages") or []
        if not msgs:
            return 0
        done = 0
        for msg in msgs:
            if not isinstance(msg, dict):
                continue
            msg_id = str(msg.get("msg_id") or "")
            if not msg_id or not self._claim(msg_id):
                continue
            ok = self._process_replayed(msg)
            self._finish(msg_id, ok=ok)
            if ok:
                done += 1
        self.counters.inc("inbox_replayed", done)
        self.last_inbox_ts = int(time.time())
        log("inbox.replayed", member=self.cfg.member, count=done, offered=len(msgs))
        return done

    def replay_inbox_async(self):
        # type: () -> None
        """后台补拉：绝不能卡住启动路径（中枢慢/挂时 teamd 照样要起来收消息）。"""
        def _run():
            try:
                self.replay_inbox()
            except Exception as exc:    # 兜底：补拉绝不让线程炸出未捕获栈
                self.counters.inc("inbox_errors")
                log("inbox.error", member=self.cfg.member, detail=truncate(str(exc), 160))

        thread = threading.Thread(target=_run, name="teamd-inbox-replay")
        thread.daemon = True
        thread.start()
        self._threads.append(thread)

    def _reject_busy(self, payload, reply, member, reason):
        # type: (bytes, str, str, str) -> bytes
        """明确拒收：发送方**立刻**知道「对方忙」，而不是干等 135 秒超时还以为丢了。

        拒收 ≠ 丢弃：此刻消息已在中枢落库（msg_log，落库=送达），对方有空时
        可由补投再送一次；这里拒掉的只是「这一刻的并发额度」。
        """
        started = time.time()
        caller = ""
        try:
            obj = json.loads(payload.decode("utf-8"))
            if isinstance(obj, dict):
                caller = str(obj.get("from") or "")
        except Exception:
            pass
        self.counters.inc("busy_rejected")
        body = self._err("busy", "%s，请稍后重试" % reason)
        body["retry_after_ms"] = BUSY_RETRY_AFTER_MS
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        if reply:
            return self._send_reply(member, reply, raw, started, caller, None, error="busy")
        return raw

    def on_nats_message(self, payload, reply="", member=None):
        # type: (bytes, str, Optional[str]) -> None
        """reader 线程回调：必须**立刻**返回。

        为什么「立刻返回」是硬要求：这个回调跑在 NATS 的 reader 线程上，一旦阻塞，
        连心跳都收不到。但「立刻返回」不等于「开一条新线程」—— 原先正是那么写的，
        于是每条消息一个新线程、并发无上限。现在改成投进**有界队列**，由固定
        worker 池消费：队列满或该成员在途超限 → 立刻回 busy 拒收（不排队、不静默丢）。
        """
        m = member or self.cfg.member
        if not self._workers_started:   # 懒启动兜底：任何调用路径都必须有消费者，
            self.start_workers()        # 否则任务进队列没人取 —— 静默不处理是最可怕的失败
        reason = self._admit(m)
        if reason is not None:
            self._reject_busy(payload, reply, m, reason)
            return
        try:
            self._task_queue.put_nowait((payload, reply, m))
        except queue.Full:      # _admit 与 put 之间可能被其它线程塞满
            self._release(m)
            self._reject_busy(payload, reply, m, "队列已满")

    def _subscribe_member(self, member):
        # type: (str) -> None
        conn = self.conns[member]
        subject = REQ_SUBJECT_FMT % member

        def _cb(payload, reply="", _m=member):
            # 闭包把「哪个成员」带进回调——每个成员只订阅自己的点名 subject
            self.on_nats_message(payload, reply, _m)

        conn.client.subscribe(subject, _cb, sid="%s-%s" % (REQ_SID, member))
        log("nats.subscribe", member=member, subject=subject)

    # ------------------------------------------------------------ 连接监督（每成员独立）
    def _connect_member(self, member):
        # type: (str) -> bool
        conn = self.conns[member]
        try:
            client = self._make_client(member)
            connect = getattr(client, "connect", None)
            if callable(connect) and not getattr(client, "is_connected", False):
                connect()
            with self._nats_lock:
                conn.client = client
            if self.cfg.inbound:
                self._subscribe_member(member)
            conn.last_err = None
            return True
        except Exception as exc:
            conn.last_err = truncate("%s: %s" % (exc.__class__.__name__, exc), 200)
            with self._nats_lock:
                conn.client = None
            return False

    def _connect_once(self):
        # type: () -> bool
        """向后兼容：一次性把所有成员都连一遍（单成员场景下等价于旧的 _connect_once）。"""
        ok = True
        for member in self.cfg.members:
            if not self._connect_member(member):
                ok = False
        return ok

    def _member_loop(self, member):
        # type: (str) -> None
        """单个成员：建连 → 订阅 → 每 ping_interval 探活；断线按退避重连并重新订阅。
        一个成员的凭据失效/断线，只影响它自己这一条循环，不牵连其他成员。"""
        conn = self.conns[member]
        attempt = 0
        while not self._stop.is_set():
            client = conn.client
            if client is None or not getattr(client, "is_connected", False):
                if client is not None:
                    log("nats.disconnected", member=member, last_err=truncate(conn.last_err or "-", 120))
                    try:
                        client.close()
                    except Exception:
                        pass
                if self._connect_member(member):
                    attempt = 0
                    # reconnects 只统计「掉线后重新连上」，首次建连不算
                    if conn.was_connected:
                        self.counters.inc("reconnects")
                        conn.reconnects += 1
                    conn.was_connected = True
                    log("nats.connected", member=member, host=self.cfg.nats_host,
                        port=self.cfg.nats_port, user=self.cfg.user_for(member),
                        subject=REQ_SUBJECT_FMT % member)
                else:
                    delay = BACKOFF_STEPS[min(attempt, len(BACKOFF_STEPS) - 1)]
                    attempt += 1
                    log("nats.connect_failed", member=member, detail=truncate(conn.last_err or "-", 160),
                        retry_in_s=delay)
                    self._stop.wait(delay)
                continue
            # 已连上：等一个探活周期，再 PING/PONG 一次
            if self._stop.wait(self.cfg.ping_interval):
                break
            client = conn.client
            if client is None:
                continue
            try:
                alive = client.flush(timeout=5.0)
            except Exception as exc:
                alive = False
                conn.last_err = truncate(str(exc), 160)
            if not alive and not self._stop.is_set():
                log("nats.ping_timeout", member=member, interval_s=self.cfg.ping_interval,
                    detail="探活未收到 PONG，判定断线")
                try:
                    client.close()
                except Exception:
                    pass
                with self._nats_lock:
                    conn.client = None

    # ------------------------------------------------------------ 心跳
    def send_heartbeat(self):
        # type: () -> bool
        """每个成员各发一条 team.hb.<成员>，用它自己的连接（各自凭据）发出。"""
        all_ok = True
        for member in self.cfg.members:
            subject = HB_SUBJECT_FMT % member
            obj = {"member": member, "members": list(self.cfg.members), "host": self.host,
                   "ts": int(time.time()), "uptime_s": int(self.uptime_s),
                   "version": VERSION, "pid": os.getpid()}
            raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            try:
                self._publish(member, subject, raw)
            except Exception as exc:
                self.counters.inc("hb_failed")
                conn = self.conns[member]
                conn.last_err = truncate(str(exc), 160)
                self.nats_last_err = conn.last_err
                # 成功不打日志（30s 一条会淹没）；失败必须留痕，但每 10 次才打一条防刷屏
                n_failed = self.counters.snapshot().get("hb_failed", 0)
                if n_failed == 1 or n_failed % 10 == 0:
                    log("hb.failed", member=member, subject=subject, err=conn.last_err)
                all_ok = False
                continue
            self.counters.inc("hb_sent")
            self.conns[member].last_hb_ts = obj["ts"]
            self.conns[member].last_err = None   # 成功即清该成员的历史错误（否则 healthz 长期挂旧告警）
            self.last_hb_ts = obj["ts"]
        if all_ok:
            # 全员都发成功 → 聚合错误也清掉；不然 healthz 会一直显示启动瞬间的残留错误，
            # 让人误判"某成员未连接"（实际 connected=true）。
            self.nats_last_err = None
        return all_ok

    def wait_nats_ready(self, timeout=20.0):
        # type: (float) -> bool
        """等**任一**成员建连（启动瞬间尚未连上时，首跳心跳必失败，会白记 hb_failed）。"""
        def _any_connected():
            return any(getattr(c.client, "is_connected", False)
                      for c in self.conns.values() if c.client is not None)

        deadline = time.time() + float(timeout)
        while time.time() < deadline:
            if self._stop.is_set():
                return False
            if _any_connected():
                return True
            if self._stop.wait(0.2):
                return False
        return _any_connected()

    def heartbeat_loop(self):
        # type: () -> None
        self.wait_nats_ready(timeout=20.0)
        while not self._stop.is_set():
            self.send_heartbeat()
            if self._stop.wait(self.cfg.hb_interval):
                break

    # ------------------------------------------------------------ 生命周期
    def start_admin(self):
        # type: () -> Optional[int]
        if not self.cfg.enable_admin:
            log("admin.disabled", member=self._all_tag)
            return None
        self._admin_server = make_admin_server(self, self.cfg.admin_host, self.cfg.admin_port)
        port = self._admin_server.server_address[1]
        thread = threading.Thread(target=self._admin_server.serve_forever,
                                  name="teamd-admin")
        thread.daemon = True
        thread.start()
        self._threads.append(thread)
        log("admin.listen", member=self._all_tag, host=self.cfg.admin_host, port=port)
        return port

    def start_nats(self):
        # type: () -> None
        """每个成员各起一条独立的连接监督线程。"""
        for member in self.cfg.members:
            thread = threading.Thread(target=self._member_loop, args=(member,),
                                      name="teamd-nats-%s" % member)
            thread.daemon = True
            thread.start()
            self._threads.append(thread)

    def watch_stdin(self):
        # type: () -> None
        """stdin 关闭（父进程收工）也算退出信号。"""
        stream = sys.stdin
        if stream is None:
            return
        try:
            while not self._stop.is_set():
                line = stream.readline()
                if not line:
                    log("stdin.closed", member=self._all_tag, detail="stdin EOF，准备退出")
                    self.stop()
                    return
        except Exception:
            return

    def stop(self):
        # type: () -> None
        self._stop.set()

    def shutdown(self):
        # type: () -> None
        self._stop.set()
        if self._admin_server is not None:
            try:
                self._admin_server.shutdown()
                self._admin_server.server_close()
            except Exception:
                pass
            self._admin_server = None
        with self._nats_lock:
            clients = []
            for conn in self.conns.values():
                clients.append(conn.client)
                conn.client = None
        for client in clients:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass

    def run(self):
        # type: () -> int
        tag = self._all_tag
        log("teamd.start", member=tag, version=VERSION, members=",".join(self.cfg.members),
            nats="%s:%d" % (self.cfg.nats_host, self.cfg.nats_port),
            python=sys.version.split()[0])
        for m in self.cfg.members:
            log("member.config", member=m, user=self.cfg.user_for(m),
                password_file=(self.cfg.password_file_for(m) or "-"),
                a2a=self.cfg.a2a_url_for(m), inbound=self.cfg.inbound)
        self.start_admin()
        self.start_workers()    # 先有消费者再收包：否则第一波请求全被 busy 拒掉
        self.start_nats()
        # 上线领回欠我的消息（补拉）。放在 NATS 之后：先能收，再谈补。
        self.replay_inbox_async()
        if getattr(self.cfg, "watch_stdin", False):
            stdin_thread = threading.Thread(target=self.watch_stdin, name="teamd-stdin")
            stdin_thread.daemon = True
            stdin_thread.start()
        try:
            self.heartbeat_loop()
        except KeyboardInterrupt:
            self.stop()
        finally:
            counters = self.counters.snapshot()
            self.shutdown()
            log("teamd.stop", member=tag, uptime_s=int(self.uptime_s), **counters)
        return 0


# ---------------------------------------------------------------- 入口

def build_parser():
    # type: () -> argparse.ArgumentParser
    ap = argparse.ArgumentParser(
        prog="teamd.py",
        description="团队 NATS 客户端守护：把 a2a.v1.req.<member> 的点名转成本机 A2A 调用；"
                    "每个成员各自一条独立 NATS 连接、各自一套凭据")
    ap.add_argument("--member", required=True,
                    help="本机成员 id；同机多成员用逗号分隔（如 <MEMBER_ID_1>,<MEMBER_ID_2>,<MEMBER_ID_3>），"
                         "共用一个 teamd 进程/9960 管理口，但每个成员各自一条独立 NATS 连接")
    ap.add_argument("--nats-host", required=True,
                    help="NATS 中枢地址，如 <NODE_IP>；支持逗号分隔多地址做故障转移，"
                         "如 <NODE_IP_1>:9930,<NODE_IP_2>:9930")
    ap.add_argument("--nats-port", type=int, default=9930, help="NATS 端口（默认 9930）")
    ap.add_argument("--user", default=None, action="append",
                    metavar="[成员=]用户名",
                    help="NATS 用户名；多成员可重复给出「成员=用户名」（如 --user <MEMBER_ID>=<USER_ID>）；"
                         "--member 只有一个成员时可给单值（向后兼容旧写法）；"
                         "缺省时默认取成员名本身")
    ap.add_argument("--password-file", default=None, action="append",
                    metavar="[成员=]路径",
                    help="NATS 口令文件路径；多成员可重复给出「成员=路径」"
                         "（如 --password-file <MEMBER_ID>=<PASS_FILE>）；"
                         "--member 只有一个成员时可给单值（向后兼容旧写法）；"
                         "缺省时自动探测 Linux <PASS_DIR>/<member>.pass 或 "
                         "Windows <PASS_DIR>\\nats-<member>.pass，探测不到会在启动时报错退出")
    ap.add_argument("--a2a-base",
                    help="本机 A2A 基地址，如 http://<NODE_IP>:9920")
    ap.add_argument("--token-file", default=None, action="append",
                    metavar="[成员=]路径",
                    help="A2A token 文件；多成员可重复给出（如 --token-file <MEMBER_ID>=<RUNTIME_DIR>/<MEMBER_ID>-token）；"
                         "单成员可不带成员前缀；--no-inbound 时可不给")
    ap.add_argument("--no-inbound", action="store_true",
                    help="纯出站模式：不订阅自己的点名 subject（零对外服务成员用，如 <MEMBER_ID>）")
    ap.add_argument("--admin-port", type=int, default=9960,
                    help="管理口端口（默认 9960，只绑 127.0.0.1）")
    ap.add_argument("--no-admin", action="store_true", help="不起管理口")
    ap.add_argument("--blackboard-base",
                    default=os.environ.get("AGENT_TEAM_BB_BASE") or os.environ.get("TEAMD_BLACKBOARD_BASE", ""),
                    help="中枢（blackboard）基址，如 http://<NODE_IP>:9940；"
                         "给了才会做离线补拉")
    ap.add_argument("--inbox-replay-limit", type=int, default=DEFAULT_INBOX_REPLAY_LIMIT,
                    help="一次离线补拉最多领几条")
    ap.add_argument("--queue-size", type=int, default=DEFAULT_QUEUE_SIZE,
                    help="有界任务队列深度（默认 %d）；满了立即回 busy 拒收，不排队" % DEFAULT_QUEUE_SIZE)
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                    help="worker 池线程数（默认 %d），即同时处理的最大请求数" % DEFAULT_WORKERS)
    ap.add_argument("--per-member-limit", type=int, default=DEFAULT_PER_MEMBER_LIMIT,
                    help="单个成员在途请求上限（默认 %d）" % DEFAULT_PER_MEMBER_LIMIT)
    ap.add_argument("--log-file", default=None,
                    help="把日志同时写入该文件（计划任务/pythonw 无控制台时排障用；超 5MB 自动清空）")
    ap.add_argument("--watch-stdin", action="store_true",
                    help="把 stdin 关闭当作退出信号（仅交互式调试用；默认关闭——"
                         "计划任务/systemd 下 stdin 是 /dev/null，看 stdin 会让服务秒退）")
    ap.add_argument("--hb-interval", type=float, default=30.0, help="心跳间隔秒（默认 30）")
    ap.add_argument("--ping-interval", type=float, default=60.0, help="探活间隔秒（默认 60）")
    ap.add_argument("--a2a-timeout-margin", type=float, default=5.0,
                    help="A2A HTTP 读超时相对业务超时的余量秒（默认 5）")
    return ap


def config_from_args(args):
    # type: (argparse.Namespace) -> Config
    members = [m.strip() for m in str(args.member).split(",") if m.strip()]
    if not members:
        raise SystemExit("[FATAL] --member 不能为空")
    for m in members:
        if not _MEMBER_RE.match(m):
            raise SystemExit("[FATAL] 成员名不合法（只能字母数字._-）: %r" % m)

    user_entries = args.user or []
    if isinstance(user_entries, str):
        user_entries = [user_entries]
    pass_entries = args.password_file or []
    if isinstance(pass_entries, str):
        pass_entries = [pass_entries]

    resolved = resolve_member_creds(members, user_entries, pass_entries)
    creds = {}
    for m in members:
        info = resolved[m]
        pf = info["password_file"]
        try:
            password = read_secret_file(pf)
        except Exception as exc:
            raise SystemExit("[FATAL] 读不到成员 %s 的 NATS 口令文件 %s: %s" % (m, pf, exc))
        if not password:
            raise SystemExit("[FATAL] 成员 %s 的 NATS 口令文件是空的: %s" % (m, pf))
        creds[m] = {"user": info["user"], "password": password, "password_file": pf}

    # 多成员：每个成员一套 token（--token-file 可重复，支持 成员=路径）
    tokens = {}
    raw_token_files = args.token_file or []
    if isinstance(raw_token_files, str):
        raw_token_files = [raw_token_files]

    def _load_token(path, label):
        try:
            value = read_secret_file(path)
        except Exception as exc:
            raise SystemExit("[FATAL] 读不到 A2A token 文件（%s）: %s" % (label, exc))
        if not value:
            raise SystemExit("[FATAL] A2A token 文件是空的（%s）: %s" % (label, path))
        return value

    for entry in raw_token_files:
        key, sep, value = str(entry).partition("=")
        if sep and _MEMBER_RE.match(key):
            tokens[key] = _load_token(value, key)
        else:
            tokens[members[0]] = _load_token(entry, members[0])
    token = tokens.get(members[0], "")
    if args.no_inbound:
        # 纯出站成员：不订阅点名 subject，也就不需要自己的 A2A token
        pass  # 纯出站：不订阅点名 subject，也就用不到自己的 A2A token
    else:
        if not tokens:
            raise SystemExit("[FATAL] 需要 --token-file（被点名时要拿它调本机 A2A）；"
                             "纯出站成员请显式加 --no-inbound")
        missing = [m for m in members if not tokens.get(m)]
        if missing:
            raise SystemExit("[FATAL] 这些成员缺 A2A token（多成员须逐个给 --token-file 成员=路径）：%s"
                             % ", ".join(missing))
    if args.no_inbound and not args.a2a_base:
        args.a2a_base = "http://127.0.0.1:1"  # 纯出站模式用不到；给个占位以免被必填卡住
    base = str(args.a2a_base or "")
    if not base.startswith("http://") and not base.startswith("https://"):
        raise SystemExit("[FATAL] --a2a-base 必须以 http:// 或 https:// 开头，收到: %r" % base)

    primary = members[0]
    primary_creds = creds[primary]
    return Config(member=primary, nats_host=args.nats_host, nats_port=args.nats_port,
                  user=primary_creds["user"], password=primary_creds["password"],
                  a2a_base=base, token=token,
                  members=members, tokens=tokens, creds=creds,
                  admin_host="127.0.0.1", admin_port=args.admin_port,
                  enable_admin=not args.no_admin, hb_interval=args.hb_interval,
                  ping_interval=args.ping_interval,
                  a2a_timeout_margin=args.a2a_timeout_margin,
                  watch_stdin=args.watch_stdin, inbound=not args.no_inbound,
                  queue_size=args.queue_size, blackboard_base=args.blackboard_base, inbox_replay_limit=args.inbox_replay_limit, workers=args.workers,
                  per_member_limit=args.per_member_limit)


def install_signals(daemon):
    # type: (TeamDaemon) -> None
    """SIGTERM/SIGINT（Windows 上还有 SIGBREAK）触发优雅退出；不支持的平台静默跳过。"""
    import signal

    def handler(signum, frame):
        log("signal.recv", member=getattr(daemon, "_all_tag", None), signum=signum)
        daemon.stop()

    for name in ("SIGTERM", "SIGINT", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, handler)
        except Exception:
            pass


def main(argv=None):
    # type: (Optional[List[str]]) -> int
    _argv = list(sys.argv[1:] if argv is None else argv)
    if "--log-file" in _argv:
        try:
            _lp = _argv[_argv.index("--log-file") + 1]
            import os as _os
            if _os.path.exists(_lp) and _os.path.getsize(_lp) > 5 * 1024 * 1024:
                _os.remove(_lp)
            _fh = open(_lp, "a", encoding="utf-8", buffering=1)
            sys.stdout = _fh
            sys.stderr = _fh
        except Exception:
            pass
    args = build_parser().parse_args(argv)
    cfg = config_from_args(args)
    daemon = TeamDaemon(cfg)
    install_signals(daemon)
    return daemon.run()


if __name__ == "__main__":
    sys.exit(main())
