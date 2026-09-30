#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""chat_svc.py —— 人机对话台（team_chat）后端服务

契约：API契约-人机对话台-v1.md（已冻结）/ v1.1-前端增强增补.md
本文件只做**后端**；前端产物 agent-team/ui/ 归 <MEMBER_ID>，本服务只负责同源托管。

依赖纪律（与团队其它服务一致）：**纯标准库 + psycopg2**（唯一第三方），
语法兼容 Python 3.9；口令只从环境变量 / 受保护文件读，绝不写进代码 / 日志 / 响应。

实现进度（契约 §12.4 的顺序）：
  ① 只读端点：/healthz、/api/whoami、/api/agents、/api/sessions、
     /api/sessions/{sid}、/api/sessions/{sid}/messages          ← 本版
  ② POST messages + SSE        ③ POST/PATCH sessions
  ④ POST meetings + 会议推流    ⑤ 旁听
  未实现的端点一律 501 not_implemented（**不是 404**），前端据此开关按钮。

起法（<HOST>）：
  CHAT_PG_DSN='host=127.0.0.1 port=9950 dbname=blackboard user=bb password=...' \
  CHAT_SESSION_KEY_FILE=<PASS_FILE> \
  python3 chat_svc.py --host <NODE_IP> --port 9980
"""
from __future__ import annotations

import argparse
import base64
import datetime
import hashlib
import json
import logging
import mimetypes
import os
import queue
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
import node_paths as _np  # L0 平台层：宿主默认值唯一出处（env 优先）

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import view_auth  # noqa: E402  —— 账号 / 会话 / 角色 / 密级，与看板同一套口径
#: L2 适配层：模型执行器「怎么调」+ Agent 展示名。本件只留薄委托，调用点不变。
import agent_model  # noqa: E402
#: L1 通信层：HTTP 应答出口（写 socket 那一段）与**非 GET 方法**的路由分发。
import web_gateway  # noqa: E402

# 上下文水位（服务端渲染，2026-09-24）：让「上下文：约 Xk/Yk (Z%)」由服务端按
# 真源（<MEMBER_ID_1> sessions.json / <MEMBER_ID_2> state.db）渲染，绝不采信 Agent 手打的数字。
try:
    import ctx_watermark as _ctx_wm  # noqa: E402
except Exception:  # noqa: BLE001 —— 缺这个模块只是不挂水位行，不影响主流程
    _ctx_wm = None

VERSION = "2.0"                          # 2.0 = 临时群（契约 §I）

LOG = logging.getLogger("chat_svc")

# ------------------------------------------------------------------ 常量

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9980

#: 默认文档：根路径 / 与 SPA fallback 都落这里（原 index.html 已停用）
DEFAULT_DOC = "group.html"

# 兜底默认值集中到 L0 node_paths（env 优先）
AGENT_TEAM_RUNTIME_DIR = os.environ.get("AGENT_TEAM_RUNTIME_DIR") or _np.runtime_dir()
# 兜底默认值集中到 L0 node_paths（env 优先）
AGENT_TEAM_ETC_DIR = os.environ.get("AGENT_TEAM_ETC_DIR") or _np.etc_dir()
# 兜底默认值集中到 L0 node_paths（env 优先）
AGENT_TEAM_DOMAIN = os.environ.get("AGENT_TEAM_DOMAIN") or "example.com"
#: 反向 SSH 目标与本机 claude_exec.py 路径已下沉 L2 agent_model（模型「怎么调」归适配层）。

#: 门户登录页（本服务**不自己签发 cookie**，与「只有看板 POST /login」的既有口径一致）。
#: 未登录的浏览器访客一律转交这里，并带「登录后回本页」的 next。
LOGIN_BASE_URL = (os.environ.get("CHAT_LOGIN_BASE_URL")
                  or "https://%s" % AGENT_TEAM_DOMAIN).rstrip("/")
#: 本服务的对外地址（Host 不在自有域名内时的兜底，用来拼 next）。
PUBLIC_URL = (os.environ.get("CHAT_PUBLIC_URL")
              or "https://chat.%s" % AGENT_TEAM_DOMAIN).rstrip("/")

#: 数据库连接串：优先 CHAT_PG_DSN，其次与黑板同一个 BB_PG_DSN
DSN_ENV_PRIMARY = "CHAT_PG_DSN"
DSN_ENV_FALLBACK = "BB_PG_DSN"
DSN_FILE = os.environ.get("AGENT_TEAM_PG_ENV_FILE") or AGENT_TEAM_ETC_DIR + "/pg.env"

#: 成员名册：多份 agents.json 合并（后面读到的同名成员不覆盖先读到的）
ROSTER_ENV = "CHAT_AGENTS_JSON"          # 冒号/分号分隔的路径列表
ROSTER_DEFAULTS = (
    os.environ.get("AGENT_TEAM_AGENTS_JSON") or AGENT_TEAM_ETC_DIR + "/agents.json",
)

#: 成员显示名补充表（兜底表 + `AGENT_TEAM_DISPLAY_NAMES` 覆盖）已下沉 L2 agent_model。
#: 这里是**同一个 dict 对象**的别名：既有 `DISPLAY_NAMES[...]` 用法照旧可用。
#: 兜底表以 L2 的**公开名** `DISPLAY_NAMES_FALLBACK` 引用（判据 6：跨件只走公开契约）。
_DISPLAY_NAMES_FALLBACK = agent_model.DISPLAY_NAMES_FALLBACK
DISPLAY_NAMES = agent_model.DISPLAY_NAMES

BODY_MAX = 1 << 20                       # 请求体上限 1MB
#: tailnet 内免登录时把对端认作的人（与看板"内网免登录"同口径）。
#: 有人用会话登录时以登录身份为准；要区分多人就实现 §H 的 /login（复用 view_auth）。
# 兜底默认值集中到 L0 node_paths（env 优先）
DEFAULT_HUMAN_ID = (os.environ.get("AGENT_TEAM_HUMAN_ID") or os.environ.get("CHAT_HUMAN_ID") or "<human-id>").strip() or "<human-id>"
DB_CONNECT_TIMEOUT_S = 5
DB_RETRY_S = 30.0                        # 连接坏了多久才重连
PAGE_LIMIT_DEFAULT = 20
PAGE_LIMIT_MAX = 200
MSG_LIMIT_DEFAULT = 50
MSG_LIMIT_MAX = 200
TEXT_MAX = 4000                          # 单条正文上限（字）
EXCERPT_LEN = 80

# ---- 群名 / 备注可编辑（#90：PATCH /api/sessions/{sid} 支持 title + note）----
#: 群名上限 80 字、备注上限 2000 字：超限一律 **4xx 拒收**（明确错误码），绝不静默截断。
SESSION_TITLE_MAX = 80
SESSION_NOTE_MAX = 2000

#: 幂等建列 / 建留痕表（老库补 note 列；每次改群名/备注落一条事件，可回溯）
SESSION_NOTE_DDL = """
ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS note text NOT NULL DEFAULT ''
"""

META_DDL_EVENTS = """
CREATE TABLE IF NOT EXISTS chat_session_meta_events (
  event_id    bigserial PRIMARY KEY,
  session_id  text NOT NULL,
  field       text NOT NULL,
  old_value   text NOT NULL DEFAULT '',
  new_value   text NOT NULL DEFAULT '',
  actor       text NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now()
)
"""

# ---- 群记忆锚 RoomMemo（契约：GET/PUT/PATCH /api/sessions/{sid}/memo）----
#: 结构化字段 + 只追加的历史：不写自然语言摘要（摘要丢细节，字段可机器核未完成项）。
MEMO_LIST_KEYS = ("decisions", "open_tasks", "conventions")
MEMO_STR_KEYS = ("goal", "resume")
MEMO_MAX_BYTES = 256 * 1024              # 单份记忆上限（防有人把整个日志塞进来）

#: 幂等建表：记忆本体 + 变更留痕（每次写必落一条，可回溯任一版本）。
MEMO_DDL_MAIN = """
CREATE TABLE IF NOT EXISTS chat_session_memo (
  session_id  text PRIMARY KEY,
  doc         jsonb NOT NULL DEFAULT '{}'::jsonb,
  version     integer NOT NULL DEFAULT 0,
  etag        text NOT NULL DEFAULT '',
  updated_at  timestamptz NOT NULL DEFAULT now(),
  updated_by  text NOT NULL DEFAULT ''
)
"""

MEMO_DDL_EVENTS = """
CREATE TABLE IF NOT EXISTS chat_session_memo_events (
  event_id    bigserial PRIMARY KEY,
  session_id  text NOT NULL,
  version     integer NOT NULL,
  action      text NOT NULL,
  actor       text NOT NULL,
  etag        text NOT NULL,
  summary     jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at  timestamptz NOT NULL DEFAULT now()
)
"""

# ---- 待办台账 Todo（契约：GET/POST /api/sessions/{sid}/todos + PATCH /api/todos/{id}）----
#: 真源**一套 items[]**（群 #108~#114 冻结，不搞 asks[] 第二套）：
#:   {id, text, owner, kind: confirm|review|decide, status: open|done, created_at, cleared_at,
#:    landing: [字符串落点, ...]}
#: 写口口径（#131 §2 收紧）：**agent 不能自盖完成章** —— `status=done` 必须带落点
#: （`landing` 字符串数组 / `evidence(s)`），落点经后端校验通过才翻 done；
#: **不带落点写 status=done 一律 422 `no_landing`**，条目继续挂 open。
#: 责任田（#114 冻结）：只能改 owner=自己的条目，新增也只能给自己加。
#: 群关闭 = 只读归档（#112/#124）：closed 后 todos 仍可 GET，写口全关。
TODO_KINDS = ("confirm", "review", "decide")
#: #448：状态从两段（open/done）扩成**三段式**：open=待办 / doing=进行中 / done=已完成。
#: `doing` 是「认领开工」——只证明 agent 开工了，不证明干完了，所以**不要求落点**；
#: `done` 仍是完成章，口径不变（#131 §2：必须带机器可验落点，落点空一律 422 no_landing）。
TODO_STATUS = ("open", "doing", "done")
#: 状态中文口径（前端/巡检同一套：队列面板直接按这个显示）
TODO_STATUS_LABEL = {"open": "待办", "doing": "进行中", "done": "已完成"}
TODO_SORTS = ("time", "owner")
TODO_TEXT_MAX = 500
TODO_EVIDENCE_MAX = 8
#: 落点四类（#133 冻结，四类都机器可验、缺一不算落点）
TODO_EVIDENCE_TYPES = ("file", "curl", "commit", "restart")
#: 落点字段 `landing`（#131 §2）：**字符串数组**，元素本身必须是上面四类之一的
#: 机器可验写法。跟 `evidences`（{type,value} 对象数组）并存：`landing` 是给人和巡检器
#: 直接读的落点原文，`evidences` 是同一批落点的结构化副本，两列同一次写入。
TODO_LANDING_MAX = 8
TODO_LANDING_MAXLEN = 500

TODO_DDL_MAIN = """
CREATE TABLE IF NOT EXISTS chat_session_todos (
  todo_id     text PRIMARY KEY,
  session_id  text NOT NULL,
  text        text NOT NULL,
  owner       text NOT NULL,
  kind        text NOT NULL DEFAULT 'confirm',
  status      text NOT NULL DEFAULT 'open',
  deadline    timestamptz,
  created_by  text NOT NULL DEFAULT '',
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now(),
  cleared_at  timestamptz,
  cleared_by  text NOT NULL DEFAULT '',
  evidences   jsonb NOT NULL DEFAULT '[]'::jsonb,
  landing     jsonb NOT NULL DEFAULT '[]'::jsonb
)
"""

#: 老库补列（#131 §2）：`landing` 落点字段对已存在的表做幂等补列，不重建表。
TODO_DDL_ALTER = (
    "ALTER TABLE chat_session_todos"
    " ADD COLUMN IF NOT EXISTS landing jsonb NOT NULL DEFAULT '[]'::jsonb"
)

TODO_DDL_INDEX = """
CREATE INDEX IF NOT EXISTS chat_session_todos_sid_idx
  ON chat_session_todos (session_id, owner, status)
"""

TODO_COLUMNS = (
    "todo_id, session_id, text, owner, kind, status, deadline, created_by,"
    " created_at, updated_at, cleared_at, cleared_by, evidences, landing,"
    " intent, followup_count, task_name, task_summary, enrich_state"
)

#: #478：意图分类的落库列（幂等补列，老库不重建表）。
#: `intent` 记这条**为什么入队**（本版入队的都是 task）；`followup_count` 记同一任务
#: 被追问了几次 —— 追问只累加这个计数，**绝不新建条目**。
TODO_DDL_ALTER_478 = (
    "ALTER TABLE chat_session_todos"
    " ADD COLUMN IF NOT EXISTS intent text NOT NULL DEFAULT 'task'",
    "ALTER TABLE chat_session_todos"
    " ADD COLUMN IF NOT EXISTS followup_count integer NOT NULL DEFAULT 0",
)

#: #534：队列条目的**抽象短名**（列表只显示名字，鼠标悬停再看全文）。
#: 入队时由 `todo_task_name()` 从原消息正文抽取、与 text 同一次写入落库；
#: 老行（本列还是 '') 由 `task_name IS NULL OR task_name = ''` 的 backfill 补齐，
#: 前端再留一层兜底（本列为空时自己截）。列追加在最后，既有列序号不变。
TODO_DDL_ALTER_534 = (
    "ALTER TABLE chat_session_todos"
    " ADD COLUMN IF NOT EXISTS task_name text NOT NULL DEFAULT ''",
)

#: #541：任务**判断**与任务名**抽象**改由模型做（#534 的「截原文前几字符」被否掉）。
#: 两条新列（写法与 #478/#534 的补列一致，幂等，老库不重建表）：
#:   · `task_summary` —— 模型给的一句话详情（做什么 / 验收标准 / 涉及对象），
#:     前端只在 hover title 里用，列表还是只显示短名；
#:   · `enrich_state` —— 富化状态机，可回查「这条现在是什么情况」：
#:     `pending` 已交模型待回 / `done` 模型名已落库 / `fallback` 模型没回来（保留规则名）
#:     / `skipped` backfill 时模型判不是任务（历史条目只标记、不删）。
TODO_DDL_ALTER_541 = (
    "ALTER TABLE chat_session_todos"
    " ADD COLUMN IF NOT EXISTS task_summary text NOT NULL DEFAULT ''",
    "ALTER TABLE chat_session_todos"
    " ADD COLUMN IF NOT EXISTS enrich_state text NOT NULL DEFAULT ''",
)

#: #478：被判为「进度追问 → 没入队」的消息台账。专为事后核对
#: 「为什么这条没入队」而留：`reason` 是命中的规则、`hits` 是命中的词。
#: `UNIQUE (session_id, dedupe_key)` + `ON CONFLICT DO NOTHING` 保证重发/重放幂等
#: （同一句话只留一行，追问次数也只加一次）。
TODO_DDL_INTENT_SKIPS = """
CREATE TABLE IF NOT EXISTS chat_todo_intent_skips (
  skip_id     text PRIMARY KEY,
  session_id  text NOT NULL,
  seq         bigint,
  message_id  text NOT NULL DEFAULT '',
  author_id   text NOT NULL DEFAULT '',
  owner       text NOT NULL DEFAULT '',
  intent      text NOT NULL DEFAULT 'followup',
  reason      text NOT NULL DEFAULT '',
  hits        jsonb NOT NULL DEFAULT '[]'::jsonb,
  text        text NOT NULL DEFAULT '',
  dedupe_key  text NOT NULL DEFAULT '',
  created_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (session_id, dedupe_key)
)
"""

TODO_DDL_INTENT_SKIPS_INDEX = (
    "CREATE INDEX IF NOT EXISTS chat_todo_intent_skips_sid_idx"
    " ON chat_todo_intent_skips (session_id, created_at)"
)

TODO_SKIP_COLUMNS = (
    "skip_id, session_id, seq, message_id, author_id, owner, intent,"
    " reason, hits, text, created_at"
)

#: 落点里的时间戳 / pid / commit 形状（机器可验，收的是值不是自述）
TODO_EVIDENCE_TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?")
TODO_EVIDENCE_HTTP_RE = re.compile(r"\b[1-5]\d{2}\b")
TODO_EVIDENCE_PID_RE = re.compile(r"(?i)\bpid\b\s*[:=]?\s*\d+")
TODO_EVIDENCE_COMMIT_RE = re.compile(r"^[0-9a-f]{7,40}$")

# ---- 同源托管（会议 §H · <MEMBER_ID> 8 条清单）-------------------------
#: 静态根：优先 CHAT_UI_DIR，默认本文件同级的 ui/
UI_DIR_ENV = "CHAT_UI_DIR"
UI_DIR_NAME = "ui"

#: 对外**不可见**的开发辅助文件（清单第 1 条：过滤 serve.py / mock.js）
STATIC_EXCLUDE = frozenset(["serve.py", "mock.js"])

#: 刀34：备份 / 临时件不属于可 serve 的静态面。
#: `tools/deploy.py` 每次部署第 1 步会把被覆盖的件留一份 `<path>.bak-<ts>` **就地**备份，
#: 静态根 `ui/` 里的件也不例外 —— 于是 `ui/group.html.bak-20260926-155258`、
#: `ui/group.css.bak-*`、`ui/codeblock.js.bak-inline-`、`ui/calc.html.bak-v1-*`
#: 这些历史版本会**原样躺在静态根里被当静态件 serve 出去**（实测 lobster:<deploy-dir>/ui/
#: 48 件里就有 4 类这种备份件）。`STATIC_EXCLUDE` 是逐件名单、隐藏件判定只认 `.` 开头，
#: 两条都拦不住它们 —— 所以这里按形状拦：任一路径段命中即 404。
#: 判据只收紧、不放宽：命中的件由「能取到」变成「取不到」，其余一格不动。
STATIC_BACKUP_RE = re.compile(r"(\.bak($|[-._])|~$|\.orig$|\.rej$|\.tmp$|\.swp$|\.pre-)")

#: 命中这些扩展名的未命中请求回 404，**不走 SPA fallback** ——
#: 否则浏览器会把 index.html 当 JS/CSS 解析，报 MIME 错，排查起来很绕。
STATIC_EXTS = frozenset([
    ".js", ".mjs", ".css", ".map", ".json", ".txt", ".xml", ".wasm",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp", ".avif",
    ".woff", ".woff2", ".ttf", ".otf", ".eot", ".mp4", ".webm", ".mp3", ".pdf", ".zip",
])

#: MIME 钉死（清单第 7 条）：**不能只靠 mimetypes.guess_type** ——
#: Windows 注册表会把 .js 猜成 text/plain，前端会直接报 "Unexpected token '<'"。
MIME_OVERRIDE = {
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".svg": "image/svg+xml; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
}

#: 静态一律 no-cache（清单第 5 条：v1 阶段 js/css 也不缓存，便于前端迭代）
STATIC_CACHE = "no-cache"

#: 心跳/在线态（契约 v1.1 §B.1）—— v1 只报静态成员，online 一律 None（"未知"）
HB_FRESH_S = 60.0

UTC = datetime.timezone.utc
BJ = datetime.timezone(datetime.timedelta(hours=8))


def memo_etag(session_id: str, version: int, doc: Any) -> str:
    """乐观锁 etag：内容 + 版本号的指纹。同内容同版本 -> 同 etag。"""
    raw = json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(("%s|%d|%s" % (session_id, version, raw)).encode("utf-8"))
    return "W/\"%s\"" % digest.hexdigest()[:32]


def memo_etag_ok(current: Dict[str, Any], if_match: Optional[str]) -> bool:
    """If-Match 判定：缺省/`*` 只在\"还没有过记忆\"时放行，避免盲写覆盖别人。"""
    want = (if_match or "").strip()
    if want in ("", "*"):
        return int(current.get("version") or 0) == 0
    return want == (current.get("etag") or "")


def memo_event_summary(action: str, doc: Dict[str, Any]) -> Dict[str, Any]:
    counts = {}
    for key in MEMO_LIST_KEYS:
        value = doc.get(key)
        counts[key] = len(value) if isinstance(value, list) else 0
    return {"action": action, "counts": counts, "goal": (doc.get("goal") or "")[:80]}


class DBUnavailable(Exception):
    """数据库不可用（连不上 / 驱动缺失）—— 对外统一 503。"""


class RequestError(Exception):
    """带 HTTP 状态码与契约错误码的请求级错误。"""

    def __init__(self, status: int, code: str, message: str = "",
                 extra: Optional[Dict[str, Any]] = None) -> None:
        Exception.__init__(self, code)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra or {}


# ------------------------------------------------------------------ 小工具

def scrubbed(exc: Any) -> str:
    """错误文本脱敏：口令类字段一律抹掉，再截断。"""
    return view_auth.safe_err(exc)


def iso_bj(value: Any) -> Optional[str]:
    """timestamptz -> ISO8601 带 +08:00（契约里所有时间都是这个形状）。"""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(BJ).isoformat(timespec="seconds")


#: 「构建时间戳」——由 _init_build_stamp() 在 **import（进程启动）时算一次**填好。
#: 语义：这份代码被构建/部署覆盖到机器上的那一刻（取 chat_svc.py 的 mtime），
#: 外加本进程启动时刻。同一进程内所有 /healthz 响应里这几个值**恒等**，
#: 因此可以据此判断「跑的是哪一版、什么时候起起来的」，而不是每次请求现算的当前时间。
BUILD_STAMP: Dict[str, Any] = {}


def _init_build_stamp() -> None:
    src = os.path.abspath(__file__)
    try:
        built = datetime.datetime.fromtimestamp(os.stat(src).st_mtime, BJ)
    except OSError as exc:                       # 极端情况：读不到 mtime 也不许挡启动
        LOG.warning("构建时间戳取文件 mtime 失败：%s", scrubbed(exc))
        built = datetime.datetime.now(BJ)
    started = datetime.datetime.now(BJ)
    BUILD_STAMP.update({
        # 形如 2026-09-23 19:30:00 UTC+08:00（人读用）
        "build_time": built.strftime("%Y-%m-%d %H:%M:%S UTC+08:00"),
        # 机器读：2026-09-23T19:30:00+08:00
        "build_time_iso": built.isoformat(timespec="seconds"),
        "build_epoch": int(built.timestamp()),
        "build_source": "chat_svc.py mtime",
        "process_start": started.strftime("%Y-%m-%d %H:%M:%S UTC+08:00"),
    })


_init_build_stamp()


def _b32(n: int) -> str:
    """小写 base32（无填充），契约 §2 的 id 尾巴用它。"""
    raw = b""
    while n > 0:
        raw += bytes([n & 0xFF])
        n >>= 8
    if not raw:
        raw = b"\x00"
    return base64.b32encode(raw).decode("ascii").rstrip("=").lower()


def gen_session_id(kind: str) -> str:
    """<kind>_<yyyyMMdd>_<6位小写base32>（契约 §2）。"""
    now = datetime.datetime.now(BJ)
    tail = _b32(secrets.randbits(30))[:6]
    return "%s_%s_%s" % (kind, now.strftime("%Y%m%d"), tail)


def gen_message_id() -> str:
    """msg_<yyyyMMddHHmmss>_<6位小写base32>（契约 §2）。"""
    now = datetime.datetime.now(BJ)
    tail = _b32(secrets.randbits(30))[:6]
    return "msg_%s_%s" % (now.strftime("%Y%m%d%H%M%S"), tail)


def gen_todo_id() -> str:
    """td_<yyyyMMddHHmmss>_<6位十六进制>（契约 §2：不透明，客户端不得解析）。"""
    now = datetime.datetime.now(BJ)
    return "td_%s_%s" % (now.strftime("%Y%m%d%H%M%S"), secrets.token_hex(3))


def gen_skip_id() -> str:
    """sk_<yyyyMMddHHmmss>_<6位十六进制>（#478 追问跳过台账的行 id）。"""
    now = datetime.datetime.now(BJ)
    return "sk_%s_%s" % (now.strftime("%Y%m%d%H%M%S"), secrets.token_hex(3))


def parse_time(value: Any) -> Optional[datetime.datetime]:
    """宽松解析时间（落点/截止时间用）：ISO8601 / 'YYYY-MM-DD HH:MM' / 'YYYY-MM-DD'。

    解析不出来回 None —— 调用方据此**拒收**，不猜、不默认。
    """
    if isinstance(value, datetime.datetime):
        return value if value.tzinfo else value.replace(tzinfo=BJ)
    raw = str(value or "").strip()
    if not raw:
        return None
    parsed = None  # type: Optional[datetime.datetime]
    try:
        parsed = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
    if parsed is None:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
                    "%Y/%m/%d %H:%M", "%Y/%m/%d"):
            try:
                parsed = datetime.datetime.strptime(raw, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=BJ)


def validate_todo_evidence(raw: Any) -> Dict[str, str]:
    """落点校验（群 #131/#133 冻结的四类，四类都机器可验、缺一不算落点）。

    校验不过一律 422：**「没落点的一直挂 open」**，不许拿一句自述盖完成章。
    `file` 型若给的是本机绝对 POSIX 路径，真去查存在性——写在这儿就是给后人看：
    这条不是格式检查，是「落点必须真的落下来了」。
    """
    if not isinstance(raw, dict):
        raise RequestError(422, "bad_evidence", "落点必须是对象 {type, value}")
    kind = str(raw.get("type") or "").strip()
    value = str(raw.get("value") or "").strip()
    if kind not in TODO_EVIDENCE_TYPES:
        raise RequestError(422, "bad_evidence",
                           "落点类型只能是 %s" % "、".join(TODO_EVIDENCE_TYPES))
    if not value:
        raise RequestError(422, "bad_evidence", "落点 value 不能为空")
    if kind == "commit":
        if not TODO_EVIDENCE_COMMIT_RE.match(value):
            raise RequestError(422, "bad_evidence", "commit 落点需 7~40 位小写十六进制 commit 号")
    elif kind == "curl":
        if not TODO_EVIDENCE_HTTP_RE.search(value):
            raise RequestError(422, "bad_evidence", "curl 落点需带 HTTP 状态码（如 200）")
    elif kind == "restart":
        if not (TODO_EVIDENCE_PID_RE.search(value) and TODO_EVIDENCE_TS_RE.search(value)):
            raise RequestError(422, "bad_evidence", "重启落点需带 PID 与时间戳")
    else:
        path = value.split("@")[0] if "@" in value else value.split("|")[0]
        path = path.strip()
        if not path:
            raise RequestError(422, "bad_evidence", "file 落点需给路径")
        if path.startswith("/") and not os.path.exists(path):
            raise RequestError(422, "evidence_not_found", "落点文件不存在：%s" % path)
        if not TODO_EVIDENCE_TS_RE.search(value):
            raise RequestError(422, "bad_evidence",
                               "file 落点需带时间戳（形如 /path/file @ 2026-09-22 12:00）")
    return {"type": kind, "value": value[:500]}


#: 落点字符串里的「提交号」写法：允许 `commit: abc1234` 这种带前缀的写法
TODO_LANDING_COMMIT_RE = re.compile(r"(?i)^commit\s*[:：=#]?\s*([0-9a-f]{7,40})$")
#: 落点字符串里的「真跑接口」写法：curl 或 方法+路径
TODO_LANDING_CURL_RE = re.compile(r"(?i)curl|\b(?:GET|POST|PUT|PATCH|DELETE)\s+/")
#: 落点字符串里的「文件」写法：POSIX/Windows 路径（带扩展名或目录分隔）
TODO_LANDING_PATH_RE = re.compile(
    r"([A-Za-z]:\\[^\s，,。；;）)】\]]+|/[\w./-]{2,})")


def landing_kind_of(value: Any) -> Optional[Tuple[str, str]]:
    """把一条落点字符串自动归到四类之一，返回 (type, 规范化 value)；不达标返回 None。

    判据与 `validate_todo_evidence` 同一套正则（不另造口径）：
    commit = 7~40 位小写十六进制；restart = PID + 时间戳；curl = 真跑接口 + HTTP 码；
    file = 路径 + 时间戳。**一句自述（「已上线」「搞定了」）四类都不沾，一律 None**。
    """
    text = str(value or "").strip()
    if not text:
        return None
    if TODO_EVIDENCE_COMMIT_RE.match(text):
        return "commit", text
    hit = TODO_LANDING_COMMIT_RE.match(text)
    if hit:
        return "commit", hit.group(1)
    if TODO_EVIDENCE_PID_RE.search(text) and TODO_EVIDENCE_TS_RE.search(text):
        return "restart", text
    if TODO_LANDING_CURL_RE.search(text) and TODO_EVIDENCE_HTTP_RE.search(text):
        return "curl", text
    if TODO_LANDING_PATH_RE.search(text) and TODO_EVIDENCE_TS_RE.search(text):
        return "file", text
    return None


def normalize_landing(raw: Any) -> Tuple[List[str], List[Dict[str, str]]]:
    """归一化落点入参 → (landing 字符串数组, evidences 结构化数组)。

    收**字符串数组**（#131 §2 的新口径，本轮加的就是这个字段），同时兼容
    单个字符串、以及老的 `{type, value}` 对象；空数组、非机器可验写法一律 422。
    """
    if raw is None:
        return [], []
    if isinstance(raw, (str, dict)):
        raw = [raw]
    if not isinstance(raw, list):
        raise RequestError(422, "bad_landing",
                           "landing 需是字符串数组（或单个字符串）："
                           "commit 哈希 / 字面 curl+HTTP 码 / 文件路径+时间戳 / 重启 PID+时间戳")
    raw = [entry for entry in raw if entry not in (None, "", [], {})]
    if not raw:
        return [], []
    if len(raw) > TODO_LANDING_MAX:
        raise RequestError(422, "bad_landing", "landing 一次最多 %d 条" % TODO_LANDING_MAX)
    strings, evidences = [], []      # type: List[str], List[Dict[str, str]]
    for entry in raw:
        if isinstance(entry, dict):
            checked = validate_todo_evidence(entry)
            strings.append(("%s: %s" % (checked["type"], checked["value"]))[:TODO_LANDING_MAXLEN])
            evidences.append(checked)
            continue
        text = str(entry or "").strip()
        if not text:
            raise RequestError(422, "bad_landing", "landing 元素不能为空")
        hit = landing_kind_of(text)
        if hit is None:
            raise RequestError(422, "bad_landing",
                               "landing 不是机器可验写法：%s（收 commit 哈希 / 字面 curl+HTTP 码 / "
                               "文件路径+时间戳 / 重启 PID+时间戳；「已上线」这类自述不算落点）"
                               % text[:80])
        checked = validate_todo_evidence({"type": hit[0], "value": hit[1]})
        strings.append(text[:TODO_LANDING_MAXLEN])
        evidences.append(checked)
    return strings, evidences


# ---- #448：群聊下发的任务 → 任务队列（todos）自动入队 ----
#: 队列条目正文摘要长度（左栏一条看得下）
TODO_TASK_SUMMARY_LEN = 160
#: 入队时剥掉正文开头的「点名」（`@<MEMBER_ID>`/`@<MEMBER_ID>：` 这类；id 允许汉字后缀）
TODO_TASK_MENTION_STRIP_RE = re.compile(
    r"^\s*(?:@[\u4e00-\u9fffA-Za-z0-9_\-]+[\s:：,，]*)+")


#: #448-2：agent 回群报进度 → 队列状态跟着翻（待办 → 进行中 → 已完成）。
#: 完成词表：命中即认为这条 agent 回复在报完成。**先查否定词**——「还没完成 / 未修好」
#: 这类正文里同样含「完成 / 修好」，不设否定闸门就会把没干完的活盖章成已完成。
TODO_REPLY_DONE_WORDS = (
    "已完成", "已经完成", "均已改完", "完成了", "已做完", "修好了", "修好", "已修复", "修复完成",
    "搞定了", "搞定", "已上线", "上线了", "改好了", "已改好", "已落库", "已生效",
    "done", "finished", "fixed", "resolved", "completed",
)
TODO_REPLY_DONE_NEG = (
    "未完成", "没完成", "还没完成", "没有完成", "完不成", "未做完", "没做完",
    "未修好", "没修好", "还没修好", "未修复", "没修复", "未搞定", "没搞定",
    "未上线", "还没上线", "没上线", "未生效", "还没生效",
    "not done", "not fixed", "unfinished", "incomplete",
)
#: 开工词表：翻「进行中」（process 态，不写落点、不算完成章）。
TODO_REPLY_DOING_WORDS = (
    "进行中", "已开工", "开工", "正在做", "开始做", "已开始", "在做了", "处理中",
    "doing", "in progress", "wip",
)
#: 回复里对条目的引用：`#448`（条目标题以 `#<seq>` 开头）或 `td_xxx`（条目 id）。
TODO_REF_SEQ_RE = re.compile(r"#\s?(\d{1,9})")
TODO_REF_ID_RE = re.compile(r"td_[A-Za-z0-9_\-]{1,64}")
#: 正文里抠不到机器可验落点时，用「这条群消息本身」当落点（可回查，不空手盖完成章）。
TODO_MSG_EVIDENCE_TYPE = "msg"
#: 队列去重指纹长度：去掉 `#seq` 前缀 + 压平空白后取前 N 字。
#: 不这么比就会**同一句话入队两条**——回填时实踩：先落库的条目正文截断到 159 字、
#: 后面这条 160 字，逐字比不相等，同一条 #448 就进了两次队列。
TODO_DEDUPE_PREFIX = 120


def todo_dedupe_key(text: Any) -> str:
    """「是不是同一句话」指纹：剥 `#seq` 前缀、压平所有空白、取前 120 字。"""
    raw = re.sub(r"^#\d+\s+", "", str(text or ""))
    return re.sub(r"\s+", "", raw)[:TODO_DEDUPE_PREFIX]


def todo_message_key(text: Any) -> str:
    """**群消息正文**的去重指纹（与 `todo_text_from_message` 同一套清洗）。

    必须先剥开头的 `@点名` 再算指纹：队列条目的正文是剥过点名的
    （`#476 如下任务： …`），而群里那条消息原文是带点名的
    （`@<MEMBER_ID> 如下任务：\\n1. …`）—— 两边直接比指纹永远不相等，
    会得出「队列里没有这条」的错误结论（回填去重复核实测踩到）。
    """
    body = re.sub(r"\s+", " ", str(text or "")).strip()
    body = TODO_TASK_MENTION_STRIP_RE.sub("", body).strip()
    return todo_dedupe_key(body)


def roster_agent_ids(roster: Any) -> set:
    """名册里的 id = agent；名册外的成员（<USER_DISPLAY> <USER_ID> / 默认人）= 人。

    「人给 agent 派活才入队、agent 之间互相 @ 不入队」这条分界就靠它。
    """
    try:
        return set(roster.ids()) if roster else set()
    except Exception:
        return set()


#: #534：列表里只显示「抽象后的任务名」——短标题，从原消息正文抽。
#: 规则是**确定性**的（与 #478 的 classif 同口径：可重放、可解释、不调远程模型）：
#:   1. 剥开头 @点名 → 剥 `#seq` 前缀；
#:   2. 剥样板抬头（「如下任务：」「你负责」「需求：」…）与序号（「1. 」「(2)、」）；
#:   3. 取第一个硬停顿（。！？；;!?换行）之前的那一段；
#:   4. 超长截断加省略号。
#: 抽不出内容 → 返回 ''（调用方据此**不入队**，空文本不落账）。
TODO_TASK_NAME_MAX = 36
TODO_TASK_NAME_SEQ_RE = re.compile(r"^(?:#\d+\s*)+")
TODO_TASK_NAME_SERIAL_RE = re.compile(r"^[\(\[（【]?\d+[\.、\)）】]\s*")
TODO_TASK_NAME_PREFIXES = (
    "如下任务", "以下任务", "任务如下", "任务", "需求", "要求", "待办",
    "你负责", "负责如下", "安排如下", "分配如下",
)
TODO_TASK_NAME_HARD_STOP_RE = re.compile(r"[。！？；;!?\n\r]")


def todo_task_name(text: Any, limit: int = TODO_TASK_NAME_MAX) -> str:
    """原消息正文 → 队列列表里显示的**抽象短名**（#534）。抽不出 → ''。"""
    body = re.sub(r"\s+", " ", str(text or "")).strip()
    body = TODO_TASK_MENTION_STRIP_RE.sub("", body).strip()
    body = TODO_TASK_NAME_SEQ_RE.sub("", body).strip()
    changed = True
    while changed and body:                 # 抬头/序号可能叠好几层，循环剥到底
        changed = False
        for pre in TODO_TASK_NAME_PREFIXES:
            if body.startswith(pre):
                body = body[len(pre):].lstrip(" :：,，、\t").strip()
                changed = True
                break
        m = TODO_TASK_NAME_SERIAL_RE.match(body)
        if m and m.end() > 0:
            body = body[m.end():].strip()
            changed = True
    body = TODO_TASK_NAME_HARD_STOP_RE.split(body)[0].strip()
    body = body.strip("，,、:：;； ")
    if not body:
        return ""
    return body[:limit] + ("…" if len(body) > limit else "")


def todo_text_from_message(message: Any, limit: int = TODO_TASK_SUMMARY_LEN) -> str:
    """消息 → 队列条目正文：`#<seq> <摘要>`（剥掉开头 @点名、压平换行）。

    带 `#seq` 是为了队列里一眼对回群里的原话（<USER_ID> #448 就是第 448 条）。
    """
    raw = re.sub(r"\s+", " ", str((message or {}).get("text") or "")).strip()
    raw = TODO_TASK_MENTION_STRIP_RE.sub("", raw).strip()
    seq = (message or {}).get("seq")
    head = ("#%s " % seq) if seq else ""
    return (head + raw).strip()[:min(limit, TODO_TEXT_MAX)]


def todo_landing_from_text(text: Any) -> List[str]:
    """从 agent 回复正文里抠出**机器可验落点**（#448-2）。

    按行/分句扫，命中 `landing_kind_of` 的四类写法（commit / curl+HTTP 码 /
    文件路径+时间戳 / 重启 PID+时间戳）就收；一段都没命中 → 返回空数组，
    调用方退回「这条群消息本身」当落点。**一句自述（「已完成」）永远抠不出落点**，
    这是这套台账"不许自盖完成章"的底线。

    两个必须做的清洗（都是实踩出来的）：
      · 先剥掉段首的 `@点名`；
      · file 类**只取 `<路径> @ <时间戳>`**，不是把整句当落点 ——
        `validate_todo_evidence` 是按**第一个** `@` 切「路径 | 时间戳」的，
        句子以 `@<USER_ID>` 开头时切出来是空串，直接 422 `file 落点需给路径`
        （第一次跑就踩到：明明写了落点，队列里却只留下 msg 兜底）。
    """
    out = []  # type: List[str]
    for segment in re.split(r"[\n\r；;]+", str(text or "")):
        segment = TODO_TASK_MENTION_STRIP_RE.sub("", segment.strip()).strip()
        segment = segment.strip("·-—*#> ")
        if not segment or len(segment) > TODO_LANDING_MAXLEN:
            continue
        hit = landing_kind_of(segment)
        if hit is None:
            continue
        value = segment
        if hit[0] == "file":
            path = TODO_LANDING_PATH_RE.search(segment)
            stamp = TODO_EVIDENCE_TS_RE.search(segment)
            if not (path and stamp):
                continue
            value = "%s @ %s" % (path.group(1), stamp.group(0))
        if value not in out:
            out.append(value)
    return out


# ---- #478：人发言的意图分类（新任务 vs 进度追问）----------------------
#: 只有 `intent=task` 才入任务队列；`intent=followup`（进度追问/催问）**不入队**，
#: 只把追问次数累加到该 owner 已有条目的 `followup_count` 上。
TODO_INTENTS = ("task", "followup")

#: 追问 / 催问的**显式短语**：命中即认定「问进度」。前几条就是 #478 <USER_ID>的原话形状，
#: 其余是同一类句式的常用说法。
TODO_FOLLOWUP_PHRASES = (
    "完成了吗", "完成了没", "完成了没有", "做完了吗", "做完了没", "改完了吗", "改完了没",
    "好了吗", "好了没", "好了没有", "搞定了吗", "搞定了没", "写好了吗", "写完了吗",
    "弄好了吗", "弄好了没", "上线了吗", "上线了没", "修好了吗", "修好了没", "完了吗",
    "有没有完成", "是否完成", "有没有做好", "有没有做", "有没有改",
    "进度", "进展", "到哪一步", "到哪了", "到哪儿了", "做到哪", "进行到",
    "怎么样了", "怎么样啦", "多久能", "还要多久", "要多久", "什么时候能", "什么时候好",
    "啥时候能", "何时能", "催一下", "催你", "没动静", "有结果了吗", "有结果没",
    "通报", "汇报", "报进度", "报一下", "同步一下", "同步下", "反馈一下",
    "在做了吗", "开始了吗", "开始没", "开工了吗", "动工了吗", "可以了吗", "干完了吗",
)

#: **强催问 / 警告**句式：命中的是「最后通牒 / 训人」而不是派活，优先级**高于**动作动词。
#: 实测踩到：<USER_ID> #494「我等你的完成结果通报…这是最后一次机会…我就会把你从机器上删除了」
#: —— 里面有「删除」这个动作词，只按动词表会把它当新任务排队（它其实是催问+警告）。
#: 已知取舍：这类句子若同时又给了真活儿（罕见），会被判 followup，但会落台账可回查。
TODO_FOLLOWUP_STRONG = (
    "最后一次机会", "最后机会", "最后通牒", "把你删", "把你踢", "把你移出", "不需要你",
    "糊弄", "别糊弄", "在糊弄", "我等你的", "等你通报", "警告你",
)

#: 新任务的**动作指令动词**：指向具体产出（改什么 / 做什么）。
#: 有意**不含**「完成 / 通报 / 汇报 / 告诉 / 反馈 / 同步 / 确认」—— 那些是问结果
#: 或要人回话，不是派活；#478 的「完成了要主动给我通报」正是靠这条不被误判成新任务。
TODO_TASK_ACTION_WORDS = (
    "改", "修改", "修", "修复", "加", "增加", "新增", "补", "补齐", "补充",
    "做", "实现", "开发", "写", "编写", "重构", "优化", "完善", "调整", "替换",
    "删除", "去掉", "迁移", "接入", "支持", "升级", "部署", "上线", "发布",
    "创建", "搭", "生成", "导出", "导入", "排查", "定位", "检查", "核对",
    "测试", "验证", "复现", "整理", "梳理", "盘点", "清理", "归档",
    # ---- #539：实测漏判的常用派活动词（人点名 agent 却判成「没动作」→ 不入队）----
    # 重启 / 停止 / 删掉 / 收尾 这些是运维与验收里最高频的指令，白名单里原来一个都没有，
    # 于是「@<MEMBER_ID> 重启 9980 两个服务」「把队列里的空条目删掉」被判 followup 静默不入队。
    "重启", "启动", "停止", "停掉", "关掉", "删掉", "清掉", "收尾", "收口",
    "回滚", "回退", "改回", "补上", "接上", "跑通", "执行", "安装", "装上",
    "落盘", "出图", "出件", "打包", "对齐", "拉取", "推送", "提交",
    "重跑", "复跑", "复查", "跑一遍", "跑一下",
    "add", "fix", "implement", "refactor", "deploy", "create", "build", "update",
)

#: 明确「请你做」（祈使）的显式标志：带这些 + 动作词，**即使正文带问号也算新任务**
#: （「帮我加个导出功能好吗？」= 派活；「你的代码写了吗？」= 问进度）。
TODO_TASK_DIRECTIVE_MARKERS = (
    "请", "帮我", "帮忙", "麻烦", "给我", "把", "你去", "需要你", "要你",
    "安排", "记得", "别忘", "顺手", "新任务",
)

#: 动作动词前面出现这些否定/排除词，说明这句在**描述「没做 / 别做」**，不是派活。
#: 实测踩到：<USER_ID> #494「如果这次你还是什么都不做在这里糊弄我」里的「做」，
#: 会把一条纯催问判成新任务（只做子串匹配就是这个下场）。
TODO_ACTION_NEGATIONS = ("不", "没", "未", "别", "无", "非")
#: 看动作动词前面几个字内有否定词（够覆盖「都不做 / 没有做 / 别搞 / 未修复」）
TODO_ACTION_NEG_WINDOW = 3


def todo_action_hits(text: str) -> List[str]:
    """正文里的**真·动作指令**动作词：命中即收，但**被否定的那次命中不算**
    （「什么都不做」的「做」、「没有修复」的「修复」都不是派活）。

    同义词按**第一次未被否定的命中**算一次；全被否定就不收。
    """
    out = []  # type: List[str]
    low = text.lower()
    for word in TODO_TASK_ACTION_WORDS:
        idx = low.find(word)
        while idx >= 0:
            near = text[max(0, idx - TODO_ACTION_NEG_WINDOW):idx]
            if not any(neg in near for neg in TODO_ACTION_NEGATIONS):
                if word not in out:
                    out.append(word)
                break
            idx = low.find(word, idx + 1)
    return out


#: 明确「这是派活」的**任务引导词**：它们本身就是任务书的抬头，命中即 task
#: （不再要求句子里出现动作动词）。实测踩出来的：<USER_ID>的真实派活 #452/#476 写的是
#: 「你负责如下任务：1. …」「如下任务：1. …」—— 句子是**效果描述**（"我希望它能够点击"），
#: 一个动作动词都抠不到，只靠动词表会被误判成追问（首次回填实测：这两条被判 followup）。
TODO_TASK_INTRODUCERS = (
    "如下任务", "以下任务", "任务如下", "任务：", "任务:",
    "需求：", "需求:", "要求：", "要求:", "待办：", "待办:",
    "你负责", "负责如下", "安排如下", "分配如下",
)

#: 疑问句形状：正文出现问号 / 句尾语气助词 / 疑问代词结构（任一命中即算在提问）。
#: 句尾收「了吧 / 了么」而不是光收「吧」——「做完了吧」是催问，而「加个导出吧」
#: 是派活，只收「吧」会把后者错判成追问。
#: #534：整句以冒号收尾 = 只发了「如下任务：」「我给你一个任务：」这种**抬头**，
#: 正文还没到 —— 这条本身没有任务内容，不入队（落 intents 台账可回查）。
TODO_TASK_PREAMBLE_RE = re.compile(r"[：:]\s*$")

TODO_QUESTION_MARK_RE = re.compile(r"[?？]")
TODO_QUESTION_TAIL_WORDS = ("吗", "呢", "了没", "没有", "了吧", "了么", "没")
TODO_QUESTION_INNER_RE = re.compile(
    r"(有没有|是不是|能不能|行不行|可不可以|会不会|算不算|多久|什么时候|啥时候|何时|"
    r"哪一步|到哪|怎么样|如何)")


def _intent_out(intent: str, reason: str, hits: Sequence[str]) -> Dict[str, Any]:
    return {"intent": intent, "reason": reason, "hits": list(hits)[:12]}


def classify_human_intent(text: Any) -> Dict[str, Any]:
    """人发的**一句话** → `{"intent","reason","hits"}`（#478，纯确定性规则）。

    证据按优先级判：
      1. 有**动作指令**（动作动词 + 不是疑问句；或疑问句里带「请/帮我/把…」祈使标志）
         → `task`：新任务，入队；
      2. 命中**追问短语**（「完成了吗 / 进度 / 还要多久 / 通报 …」）
         → `followup`：进度追问，不入队，只记追问次数；
      3. **疑问句**形状（问号 / 句尾「吗、呢、了没」/ 疑问代词）
         → `followup`；
      4. 三条都不命中 → **默认 `followup`**（队列宁缺毋滥：判不准就不入队），
         但会落 `chat_todo_intent_skips` 台账（带 reason + hits），事后可回查、可补。

    纯函数：不调远程模型、不碰库、只看正文 —— 所以同一句话重放结果恒等（幂等），
    reason/hits 直接就是「为什么这条没入队」的答案。
    """
    raw = re.sub(r"\s+", " ", str(text or "")).strip()
    body = TODO_TASK_MENTION_STRIP_RE.sub("", raw).strip()
    if not body:
        return _intent_out("followup", "empty_text", [])
    low = body.lower()
    hits = []  # type: List[str]
    for phrase in TODO_FOLLOWUP_PHRASES:
        if phrase in low and phrase not in hits:
            hits.append(phrase)
    actions = todo_action_hits(body)
    directives = [w for w in TODO_TASK_DIRECTIVE_MARKERS if w in body]
    # #534：抬头词紧跟「的」= 定语（「你负责的任务队列」），不是派活抬头
    introducers = [w for w in TODO_TASK_INTRODUCERS
                   if w in body and not body[body.find(w) + len(w):].startswith("的")]
    is_q = bool(TODO_QUESTION_MARK_RE.search(body)) or \
        any(body.endswith(w) for w in TODO_QUESTION_TAIL_WORDS) or \
        bool(TODO_QUESTION_INNER_RE.search(body))
    # 判定顺序（改这个顺序前先看这段注释，顺序本身就是口径）：
    #   任务书抬头 > 强催问/警告 > 动作指令 > 追问短语 > 疑问句 > 默认不入队
    # 强催问必须压在动作指令**前面**：#494 里「把你删除」有动作词，
    # 但整句是催问+警告，不能当派活。
    # ---- #534：两类误入队要在这里挡住（都是<USER_ID> #509 实测踩到的形状）----
    #   a) 定语用法：「你负责的<东西>」是在指称已有的东西，不是派活抬头
    #      （#509「我们继续测试你负责的任务队列」里的「你负责」正是这种）；
    #   b) 只发了抬头没发正文：整句以冒号收尾（「我给你一个任务：」）——
    #      正文还在下一条消息里，这条本身**没有任务内容**，不该占队列。
    #      以冒号结尾的句子按定义后面就没内容了，所以这里不看是否命中关键词。
    if TODO_TASK_PREAMBLE_RE.search(body):
        return _intent_out("followup", "task_preamble_no_payload", introducers + directives)
    if introducers and not (is_q and not directives):
        return _intent_out("task", "task_introducer", introducers + directives)
    strong = [w for w in TODO_FOLLOWUP_STRONG if w in body]
    if strong:
        return _intent_out("followup", "followup_strong", strong + hits)
    if actions and (not is_q or directives):
        return _intent_out("task", "action_directive",
                           actions + [d for d in directives if d not in actions])
    if hits:
        return _intent_out("followup", "followup_phrase", hits)
    if is_q:
        return _intent_out("followup", "question_no_directive", directives or actions)
    # ---- #539：处置式祈使句兜底 ----
    # 「把 <东西> <动词>」是最典型的中文派活句形，动词却是开放集合（删掉 / 收尾 / 干掉 …），
    # 白名单永远追不上；实测「@<MEMBER_ID> 把队列里的空条目删掉」被判 followup、条目不入队。
    # 前置闸门全在：不是疑问句、不是催问/警告（都在上面 return 了）、不是否定句
    # （「别把队列清空」不算派活），且调用方只在「人 + @点名了名册 agent」时才走到这里。
    if "把" in directives:
        at = body.find("把")
        near = body[max(0, at - TODO_ACTION_NEG_WINDOW):at]
        if not any(neg in near for neg in TODO_ACTION_NEGATIONS):
            return _intent_out("task", "imperative_ba", directives)
    return _intent_out("followup", "no_action_directive", [])


def display_of(member_id: str) -> str:
    """成员显示名：补充表 > 名册里自带的 display > id 原样。（实现见 L2 agent_model）"""
    return agent_model.display_name(member_id)


#: L2 的正名；本件内外都可直接用 `display_name(...)`，与 `display_of(...)` 同一实现。
display_name = agent_model.display_name


def excerpt(text: Any, limit: int = EXCERPT_LEN) -> str:
    if not text:
        return ""
    flat = re.sub(r"\s+", " ", str(text)).strip()
    return flat if len(flat) <= limit else flat[:limit] + "…"


def clamp(value: Any, low: int, high: int, default: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, n))


#: 正文里手打 @xxx 的识别。三条硬口径（都不折叠大小写/繁简，与成员表原文精确比对）：
#: 1) **在标点处截断**，不是只 rstrip 结尾标点——中文不写空格，"@<MEMBER_ID>，你好"
#:    用 '@([^\s@]+)' 取会整段黏成 "<MEMBER_ID>，你好" 而漏解析（2026-09-21 实踩）。
#: 2) 前缀 (?<![A-Za-z0-9._%+@-]) 排除邮箱/普通 @ 文本（"a@b.com" 不算点名）；
#:    注意不能用 \w：\w 含汉字，"我@<MEMBER_ID>" 会被误排除。
#: 3) 汉字直接黏在 id 后（"@<MEMBER_ID>你好"）由 parse_text_mentions 的汉字前缀回退兜住。
MENTION_TEXT_RE = re.compile(
    r"(?<![A-Za-z0-9._%+@-])@([^\s@，。！？；：、,.!?;:\"'“”‘’（）()\[\]【】{}《》<>·…]+)")
MENTION_TRAILING = "，。！？；：、,.!?;:\"'“”‘’）)】]}>》…"
#: 汉字判据（回退用）：只有截断处是汉字才认前缀命中，避免 "@<MEMBER_ID>abc" 误点名
CJK_CHAR_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
#: 与前端 group.html textMentions() 同口径的别名（手点 @ 栏写进正文的就是 "@所有人"）
MENTION_ALL_ALIASES = frozenset(["所有人", "全员", "all"])


def parse_text_mentions(text: str, member_ids: Sequence[str]) -> List[str]:
    """正文里手打的 @xxx → 结构化 mentions（与手点 @ 栏同一套取值）。

    只补**命中的会话成员**；同一关键词只补一次；返回要补进 mentions 的值，
    正文文本不改写。

    命中规则（#539 起，按这个顺序）：
      1. 成员表原文**精确**命中（真有个成员叫 all 也不吃亏）；
      2. 别名（所有人 / 全员 / all）→ `@all`；
      3. **大小写不敏感**命中成员表原文（"@<MEMBER_ID_CAP>" → 成员 `<MEMBER_ID>`）——
         前端 group.html 的 @ 栏已下线、群成员名就是 "<MEMBER_ID>" 这种首字母大写写法，
         人照显示名打 `@<MEMBER_ID>` 是本能的；此前只做精确匹配 → 点名被丢弃 →
         mentions 退成 `@all` → 「人点名 agent 派活自动入队」这条钩子**静默不触发**（#539 实测）。
         只有当 casefold 后**唯一**命中一个成员时才认，有歧义（两个成员仅大小写不同）就不认。
      4. 汉字直接黏在 id 后（"@<MEMBER_ID>你好"）：按前缀回退，只在截断处是汉字时认
         （"@<MEMBER_ID>abc" 这类英文黏连不算点名），前缀同样走大小写不敏感。
    """
    known = set(member_ids or [])
    fold = {}                              # casefold -> id；只留**无歧义**的
    for mid in known:
        key = str(mid).lower()
        fold[key] = None if (key in fold and fold[key] != mid) else mid

    def resolve(token: str) -> Optional[str]:
        if token in known:
            return token
        if token in MENTION_ALL_ALIASES:
            return "@all"
        hit = fold.get(str(token).lower())
        return hit if hit else None        # 有歧义(=None) 或没命中 → 不认

    found = []  # type: List[str]
    for raw in MENTION_TEXT_RE.findall(str(text or "")):
        token = raw.strip().rstrip(MENTION_TRAILING)
        if not token:
            continue
        target = resolve(token)
        if target is None:
            for cut in range(len(token) - 1, 0, -1):
                if not CJK_CHAR_RE.match(token[cut]):
                    continue
                target = resolve(token[:cut])
                if target is not None:
                    break
        if target is None:
            continue                       # 不是群成员：不补，避免误伤普通 @ 文本
        if target not in found:
            found.append(target)
    return found


# ------------------------------------------------------------------ 成员名册

class Roster(object):
    """成员名单：多份 agents.json 合并后的只读快照。

    契约 §2 规定"成员 id = agents.json 的键"。名册文件在各成员机上是分散的，
    所以这里支持读多份并合并；读不到的路径不算错（不同机器目录不一样）。
    """

    def __init__(self, paths: Sequence[str]) -> None:
        self._paths = [p for p in paths if p]
        self._lock = threading.Lock()
        self._members = {}       # type: Dict[str, Dict[str, Any]]
        self.reload()

    def reload(self) -> None:
        found = {}                # type: Dict[str, Dict[str, Any]]
        for path in self._paths:
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
            except Exception as exc:
                LOG.debug("名册 %s 读不到：%s", path, scrubbed(exc))
                continue
            agents = data.get("agents") if isinstance(data, dict) else None
            if not isinstance(agents, dict):
                continue
            node_id = str(data.get("node_id") or "")
            host = str(data.get("host") or "")
            for member_id, spec in agents.items():
                if member_id in found:
                    continue          # 先读到的为准
                spec = spec if isinstance(spec, dict) else {}
                found[member_id] = {
                    "id": member_id,
                    "display": str(spec.get("display") or display_of(member_id)),
                    "node_id": node_id,
                    "host": host,
                    "model": str(spec.get("model") or ""),
                    "description": str(spec.get("description") or ""),
                    "skills": spec.get("skills") or [],
                }
        with self._lock:
            self._members = found

    def get(self, member_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._members.get(member_id)

    def all(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self._members.values()]

    def ids(self) -> List[str]:
        with self._lock:
            return list(self._members.keys())


# ------------------------------------------------------------------ 数据库

SESSION_COLUMNS = (
    "session_id, kind, title, sensitivity, members, created_by, created_at,"
    " updated_at, state, ref_table, ref_id, note"
)
MESSAGE_COLUMNS = (
    "message_id, session_id, seq, author_type, author_id, kind, text,"
    " ref_table, ref_id, status, reply_to, run, meta, created_at"
)


class ChatDB(object):
    """PG 访问层（惰性连接 + 断了自动重连）。

    与 blackboard_svc 同一个手法：只从环境变量 / 受保护文件读 DSN，
    连接坏了下次调用自己重建，避免把服务带崩。
    """

    def __init__(self, dsn: str, importer: Optional[Callable[[str], Any]] = None) -> None:
        if not dsn:
            raise DBUnavailable("未配置 DSN（CHAT_PG_DSN / BB_PG_DSN）")
        self._dsn = dsn
        self._importer = importer or (lambda name: __import__(name))
        self._driver = None
        self._conn = None
        self._lock = threading.Lock()
        self._failed_at = 0.0

    # -- 连接管理 ---------------------------------------------------
    def _driver_or_raise(self) -> Any:
        if self._driver is None:
            try:
                self._driver = self._importer("psycopg2")
            except Exception as exc:
                raise DBUnavailable(
                    "psycopg2 未安装（%s）。装法：pip install 'psycopg2-binary>=2.9'" % scrubbed(exc)
                )
        return self._driver

    def _connect(self) -> Any:
        return self._driver_or_raise().connect(self._dsn, connect_timeout=DB_CONNECT_TIMEOUT_S)

    def _handle(self) -> Any:
        now = time.time()
        if self._conn is not None:
            try:
                if self._conn.closed:
                    raise DBUnavailable("连接已关闭")
                return self._conn
            except DBUnavailable:
                self._conn = None
        if self._conn is None and (now - self._failed_at) < DB_RETRY_S:
            raise DBUnavailable("数据库最近连接失败，%ds 内不重试" % int(DB_RETRY_S))
        try:
            self._conn = self._connect()
        except DBUnavailable:
            self._failed_at = now
            raise
        except Exception as exc:
            self._failed_at = now
            self._conn = None
            raise DBUnavailable("连接数据库失败：%s" % scrubbed(exc))
        return self._conn

    def query(self, sql: str, params: Sequence[Any] = ()) -> List[Tuple[Any, ...]]:
        """执行只读 SQL，返回所有行。

        **读完必须结束事务**（rollback）。psycopg2 在执行第一条语句时就隐式 BEGIN，
        而 `query()` 既不 commit 也不 rollback —— 连接会停成 `idle in transaction`，
        把刚才读过的表上的 AccessShareLock 一直攥着。后果实测过：启动路径
        `ensure_todo_schema()` 的 `ALTER TABLE chat_session_todos ADD COLUMN ...`
        要 AccessExclusiveLock，被这个悬空读事务挡住 → **新实例永远起不来**
        （进程活着、不监听、不打日志，systemd 还显示 active）。
        只读路径 rollback 语义等价于 commit，且不会把任何写带进去。
        """
        with self._lock:
            conn = self._handle()
            try:
                cur = conn.cursor()
                try:
                    cur.execute(sql, list(params))
                    rows = cur.fetchall() if cur.description else []
                finally:
                    cur.close()
                try:
                    conn.rollback()          # 结束隐式事务，释放读锁（见 docstring）
                except Exception:
                    pass
                return list(rows or [])
            except DBUnavailable:
                raise
            except Exception as exc:
                try:
                    conn.rollback()
                except Exception:
                    pass
                # 连接级故障 -> 丢弃，下次重建
                self._conn = None
                self._failed_at = time.time()
                raise DBUnavailable("查询失败：%s" % scrubbed(exc))

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None

    # -- 写入（契约 §I）---------------------------------------------
    def execute_tx(self, statements: Sequence[Tuple[str, Sequence[Any]]]
                   ) -> List[Tuple[Any, ...]]:
        """在一个事务里按序执行并提交，返回**最后一条**语句的行。

        写操作必须走这里：`query()` 是只读封装（不提交），而
        `pg_advisory_xact_lock` 要靠事务存活，不提交锁就悬着。
        """
        with self._lock:
            conn = self._handle()
            rows = []  # type: List[Tuple[Any, ...]]
            try:
                cur = conn.cursor()
                try:
                    for sql, params in statements:
                        cur.execute(sql, list(params))
                        rows = list(cur.fetchall()) if cur.description else []
                finally:
                    cur.close()
                conn.commit()
                return rows
            except DBUnavailable:
                raise
            except Exception as exc:
                try:
                    conn.rollback()
                except Exception:
                    pass
                self._conn = None
                self._failed_at = time.time()
                raise DBUnavailable("写入失败：%s" % scrubbed(exc))

    def create_session(self, session_id: str, kind: str, title: str,
                       members: Sequence[str], created_by: str,
                       sensitivity: str = "internal",
                       client_ref: Optional[str] = None
                       ) -> Optional[Dict[str, Any]]:
        """建会话。带 client_ref 时幂等：同 created_by + client_ref 返回同一条。"""
        rows = self.execute_tx([(
            "INSERT INTO chat_sessions (session_id, kind, title, sensitivity,"
            " members, created_by, state, client_ref)"
            " VALUES (%s, %s, %s, %s, %s::jsonb, %s, 'open', %s)"
            " ON CONFLICT (created_by, client_ref) DO NOTHING"
            " RETURNING " + SESSION_COLUMNS,
            [session_id, kind, title, sensitivity,
             json.dumps(list(members), ensure_ascii=False), created_by, client_ref])])
        if not rows and client_ref:
            rows = self.query(
                "SELECT %s FROM chat_sessions WHERE created_by = %%s AND client_ref = %%s"
                % SESSION_COLUMNS, [created_by, client_ref])
        return self._row_to_session(rows[0]) if rows else None

    def set_session_state(self, session_id: str, state: str
                          ) -> Optional[Dict[str, Any]]:
        """改会话状态（关群 = archived）。chat_sessions 无 append-only 触发器。"""
        rows = self.execute_tx([(
            "UPDATE chat_sessions SET state = %s, updated_at = now()"
            " WHERE session_id = %s RETURNING " + SESSION_COLUMNS,
            [state, session_id])])
        return self._row_to_session(rows[0]) if rows else None

    # -- 群名 / 备注可编辑（#90）------------------------------------
    def ensure_meta_schema(self) -> None:
        """幂等建列 / 建留痕表。与 memo 同口径：失败只告警不阻断。"""
        self.execute_tx([(SESSION_NOTE_DDL, []), (META_DDL_EVENTS, [])])

    def set_session_meta(self, session_id: str, fields: Dict[str, str],
                         actor: str, state: Optional[str] = None
                         ) -> Optional[Dict[str, Any]]:
        """改群名（title）/ 备注（note），可顺带改 state —— 一次事务里「改行 + 逐字段留痕」。

        留痕口径：**只有真变了的字段**才落 chat_session_meta_events（同值重复 PATCH 不刷记录）；
        事件 INSERT 排在 UPDATE **前面**（execute_tx 只回最后一条语句的行），两者同事务同生共死。
        返回的 session 里多一个 `changed`（本次真改动的字段名列表），调用方取走后对外单列。
        """
        rows = self.query(
            "SELECT %s FROM chat_sessions WHERE session_id = %%s" % SESSION_COLUMNS,
            [session_id])
        if not rows:
            return None
        old = self._row_to_session(rows[0])
        sets = []        # type: List[str]
        params = []      # type: List[Any]
        events = []      # type: List[Tuple[str, str, str]]
        for col in ("title", "note"):
            if col not in fields:
                continue
            value = fields[col]
            sets.append(col + " = %s")
            params.append(value)
            old_value = str(old.get(col) or "")
            if old_value != value:
                events.append((col, old_value, value))
        if state is not None:
            sets.append("state = %s")
            params.append(state)
        sets.append("updated_at = now()")

        statements = [(
            "INSERT INTO chat_session_meta_events"
            " (session_id, field, old_value, new_value, actor)"
            " VALUES (%s, %s, %s, %s, %s) RETURNING event_id",
            [session_id, col, old_value, new_value, actor])
            for col, old_value, new_value in events]
        statements.append((
            "UPDATE chat_sessions SET " + ", ".join(sets)
            + " WHERE session_id = %s RETURNING " + SESSION_COLUMNS,
            params + [session_id]))
        rows = self.execute_tx(statements)
        if not rows:
            return None
        updated = self._row_to_session(rows[0])
        updated["changed"] = [col for col, _old, _new in events]
        return updated

    # -- 群记忆锚 RoomMemo（写路径全部带乐观锁版本判定）--------------
    def ensure_memo_schema(self) -> None:
        """幂等建表。服务启动时跑一次；失败只告警不阻断（记忆是附加能力）。"""
        self.execute_tx([(MEMO_DDL_MAIN, []), (MEMO_DDL_EVENTS, [])])

    def memo_get(self, session_id: str) -> Dict[str, Any]:
        rows = self.query(
            "SELECT doc, version, etag, updated_at, updated_by"
            " FROM chat_session_memo WHERE session_id = %s", [session_id])
        if not rows:
            # 还没写过：version=0 / etag=""，前端据此显示"本群尚无记忆"。
            return {"memo": {}, "version": 0, "etag": "",
                    "updated_at": None, "updated_by": ""}
        doc, version, etag, updated_at, updated_by = rows[0]
        return {
            "memo": doc if isinstance(doc, dict) else {},
            "version": int(version or 0),
            "etag": etag or "",
            "updated_at": iso_bj(updated_at),
            "updated_by": updated_by or "",
        }

    def memo_write(self, session_id: str, doc: Dict[str, Any], actor: str,
                   if_match: Optional[str], action: str
                   ) -> Tuple[bool, Dict[str, Any]]:
        """整份写 / append 后的整份写，都走这条。

        乐观锁：UPSERT 的 DO UPDATE 带 `WHERE version = <读到的版本>`，
        撞车时返回 0 行 -> 判定冲突，把**当前**版本连同 etag 交回前端刷新。
        「留痕」用一条 SELECT-EXISTS 条件插入挂在**同一个事务**里：
        写没成，事件也不会落空 —— 两者同生共死。
        """
        current = self.memo_get(session_id)
        if not memo_etag_ok(current, if_match):
            return False, current
        new_version = int(current["version"]) + 1
        etag = memo_etag(session_id, new_version, doc)
        summary = json.dumps(memo_event_summary(action, doc), ensure_ascii=False)
        rows = self.execute_tx([
            ("INSERT INTO chat_session_memo"
             " (session_id, doc, version, etag, updated_by, updated_at)"
             " VALUES (%s, %s::jsonb, %s, %s, %s, now())"
             " ON CONFLICT (session_id) DO UPDATE SET"
             " doc = EXCLUDED.doc, version = EXCLUDED.version,"
             " etag = EXCLUDED.etag, updated_by = EXCLUDED.updated_by,"
             " updated_at = now()"
             " WHERE chat_session_memo.version = %s"
             " RETURNING version",
             [session_id, json.dumps(doc, ensure_ascii=False), new_version,
              etag, actor, int(current["version"])]),
            ("INSERT INTO chat_session_memo_events"
             " (session_id, version, action, actor, etag, summary)"
             " SELECT %s, %s, %s, %s, %s, %s::jsonb"
             " WHERE EXISTS (SELECT 1 FROM chat_session_memo"
             "  WHERE session_id = %s AND version = %s AND etag = %s)"
             " RETURNING version, etag, created_at",
             [session_id, new_version, action, actor, etag, summary,
              session_id, new_version, etag]),
        ])
        if not rows:
            return False, self.memo_get(session_id)      # 撞车：回最新状态
        return True, {
            "memo": doc, "version": new_version, "etag": etag,
            "updated_at": iso_bj(rows[0][2]),
            "updated_by": actor,
        }

    # -- 待办台账 Todo（群 #108~#114 冻结：真源一套 items[]）----------
    def ensure_todo_schema(self) -> None:
        """幂等建表 + 索引 + 补列。与 memo 同口径：失败只告警不阻断。"""
        stmts = [(TODO_DDL_MAIN, []), (TODO_DDL_ALTER, []), (TODO_DDL_INDEX, [])]
        # #478：意图列（幂等补列）+ 追问跳过台账
        stmts += [(alter, []) for alter in TODO_DDL_ALTER_478]
        stmts += [(alter, []) for alter in TODO_DDL_ALTER_534]   # #534 短名列
        stmts += [(alter, []) for alter in TODO_DDL_ALTER_541]   # #541 详情/富化状态列
        stmts += [(TODO_DDL_INTENT_SKIPS, []), (TODO_DDL_INTENT_SKIPS_INDEX, [])]
        self.execute_tx(stmts)

    def todos_list(self, session_id: str, owner: Optional[str] = None,
                   status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = ["SELECT %s FROM chat_session_todos WHERE session_id = %%s" % TODO_COLUMNS]
        params = [session_id]  # type: List[Any]
        if owner:
            sql.append(" AND owner = %s")
            params.append(owner)
        if status:
            sql.append(" AND status = %s")
            params.append(status)
        sql.append(" ORDER BY created_at ASC, todo_id ASC")
        return [self._row_to_todo(row) for row in self.query("".join(sql), params)]

    def todo_get(self, todo_id: str) -> Optional[Dict[str, Any]]:
        rows = self.query(
            "SELECT %s FROM chat_session_todos WHERE todo_id = %%s" % TODO_COLUMNS,
            [todo_id])
        return self._row_to_todo(rows[0]) if rows else None

    def todo_create(self, session_id: str, text: str, owner: str, kind: str,
                    deadline: Optional[datetime.datetime], actor: str,
                    landing: Sequence[str] = (), intent: str = "task",
                    task_name: str = "", enrich_state: str = ""
                    ) -> Optional[Dict[str, Any]]:
        # #534：短名与正文同一次写入（调用方没给就现抽，保证新条目不缺列）。
        # #541：这里给的只是**规则兜底名**，入队后异步 worker 会用模型名覆盖它
        #（enrich_state='pending' 就是「已交给模型待回」的标记）。
        if not task_name:
            task_name = todo_task_name(text)
        rows = self.execute_tx([(
            "INSERT INTO chat_session_todos"
            " (todo_id, session_id, text, owner, kind, status, deadline, created_by,"
            "  landing, intent, task_name, enrich_state)"
            " VALUES (%s, %s, %s, %s, %s, 'open', %s, %s, %s::jsonb, %s, %s, %s)"
            " RETURNING " + TODO_COLUMNS,
            [gen_todo_id(), session_id, text, owner, kind, deadline, actor,
             json.dumps(list(landing), ensure_ascii=False),
             intent if intent in TODO_INTENTS else "task",
             str(task_name or "")[:TODO_TEXT_MAX],
             str(enrich_state or "")[:32]])])
        return self._row_to_todo(rows[0]) if rows else None

    def todo_set_task_name(self, todo_id: str, task_name: str
                           ) -> Optional[Dict[str, Any]]:
        """#534：补/修一条条目的短名（backfill 用；只改这一列）。"""
        rows = self.execute_tx([(
            "UPDATE chat_session_todos SET task_name = %s"
            " WHERE todo_id = %s RETURNING " + TODO_COLUMNS,
            [str(task_name or "")[:TODO_TEXT_MAX], todo_id])])
        return self._row_to_todo(rows[0]) if rows else None

    # -- #541：模型富化回写（异步 worker / 回填通道共用）-------------------
    def todo_set_enrichment(self, todo_id: str, task_name: Optional[str] = None,
                            summary: Optional[str] = None, state: str = ""
                            ) -> Optional[Dict[str, Any]]:
        """回写模型结果：`task_name=None` = 不动短名，`summary=None` = 不动详情。

        `state` 是 `enrich_state` 的新值（''=不动）—— 降级时用 `fallback` 把「模型没
        回来、这条还是规则名」这件事留在库里，事后能查、能重跑回填。
        """
        sets = []  # type: List[str]
        params = []  # type: List[Any]
        if task_name is not None:
            sets.append("task_name = %s")
            params.append(str(task_name or "")[:TODO_TEXT_MAX])
        if summary is not None:
            sets.append("task_summary = %s")
            params.append(str(summary or "")[:TODO_TEXT_MAX])
        if state:
            sets.append("enrich_state = %s")
            params.append(str(state)[:32])
        if not sets:
            return None
        sets.append("updated_at = now()")
        params.append(todo_id)
        rows = self.execute_tx([(
            "UPDATE chat_session_todos SET " + ", ".join(sets)
            + " WHERE todo_id = %s RETURNING " + TODO_COLUMNS, params)])
        return self._row_to_todo(rows[0]) if rows else None

    def todo_delete_pristine(self, todo_id: str,
                             expect_task_name: str = "") -> bool:
        """#541：撤掉「模型判为非任务」的**刚落条目** —— 只在它确实没被人动过时撤。

        守卫（任一不满足就**不删**，宁可留一条脏条目也不误删别人的活）：
        仍是 open、没被清过、没证据、没落点、短名还是入队时那个规则名。
        短名对不上 = 有人/别的路径动过这行 → 不删。
        """
        rows = self.execute_tx([(
            "DELETE FROM chat_session_todos WHERE todo_id = %s"
            " AND status = 'open' AND cleared_at IS NULL"
            " AND (evidences IS NULL OR evidences = '[]'::jsonb)"
            " AND (landing IS NULL OR landing = '[]'::jsonb)"
            " AND task_name = %s RETURNING todo_id",
            [todo_id, str(expect_task_name or "")[:TODO_TEXT_MAX]])])
        return bool(rows)

    def todos_unenriched(self, session_id: Optional[str] = None,
                         limit: int = 20) -> List[Dict[str, Any]]:
        """#541：还没被模型富化过的条目（回填通道的待办集合）。

        口径 = #534 `todos_missing_task_name()` 的条件，**并上**「富化状态为空」
        （历史行规则名非空，光看 task_name 会漏掉它们），即：
        没名字 或 没富化状态。

        #541 补：**并上 `enrich_state='pending'`** —— 富化队列在内存里，重启会把
        「已提交、还没被模型回答」的条目原地留成 pending；不认它，这条就永远补不回来
        （实测踩到：并发重启打断了在途模型调用，条目卡 pending，回填看不见它）。
        """
        sql = ["SELECT %s FROM chat_session_todos"
               " WHERE (enrich_state IS NULL OR enrich_state = ''"
               "        OR enrich_state = 'pending'"
               "        OR task_name IS NULL OR task_name = '')" % TODO_COLUMNS]
        params = []  # type: List[Any]
        if session_id:
            sql.append(" AND session_id = %s")
            params.append(session_id)
        sql.append(" ORDER BY created_at ASC, todo_id ASC LIMIT %s")
        params.append(max(1, int(limit)))
        return [self._row_to_todo(row) for row in self.query("".join(sql), params)]

    def todos_stale_pending(self, limit: int = 20) -> List[Dict[str, Any]]:
        """#541 补：卡在 `pending` 的条目（富化任务提交给了**上一个进程**，重启即丢）。

        只取 `enrich_state='pending'`（**不是**空状态）—— 空的是历史老行，启动时
        一起补投会把模型通道刷爆；pending 才是「已认领、没回答」的真遗孤。
        只读，不写；有界（LIMIT）。
        """
        sql = ("SELECT %s FROM chat_session_todos WHERE enrich_state = %%s"
               " ORDER BY created_at ASC, todo_id ASC LIMIT %%s" % TODO_COLUMNS)
        return [self._row_to_todo(row)
                for row in self.query(sql, ["pending", max(1, int(limit))])]

    def todos_missing_task_name(self, session_id: Optional[str] = None
                                ) -> List[Dict[str, Any]]:
        """#534：短名为空的条目（backfill 的待办集合，跨群时可只给 session_id）。"""
        sql = ["SELECT %s FROM chat_session_todos"
               " WHERE (task_name IS NULL OR task_name = '')" % TODO_COLUMNS]
        params = []  # type: List[Any]
        if session_id:
            sql.append(" AND session_id = %s")
            params.append(session_id)
        sql.append(" ORDER BY created_at ASC, todo_id ASC")
        return [self._row_to_todo(row) for row in self.query("".join(sql), params)]

    def todo_delete(self, todo_id: str) -> bool:
        """#534：物理删掉一条**脏条目**（无任务内容的误入队行）。"""
        rows = self.execute_tx([(
            "DELETE FROM chat_session_todos WHERE todo_id = %s RETURNING todo_id",
            [todo_id])])
        return bool(rows)

    # -- #478：意图分类落库（追问跳过台账 / 追问次数）----------------------
    def todo_skip_create(self, session_id: str, message: Optional[Dict[str, Any]],
                         author_id: str, owner: str, cls: Dict[str, Any],
                         text: str) -> Optional[str]:
        """记一条「判为进度追问 → 没入队」的台账，返回 skip_id。

        幂等：`ON CONFLICT (session_id, dedupe_key) DO NOTHING` —— 同一句话重发 /
        重放 / 重跑 backfill 只会留一行；**本句已记过 → 返回 None**，调用方据此
        不重复累加追问次数。
        """
        key = todo_dedupe_key(text)
        if not key:
            return None
        rows = self.execute_tx([(
            "INSERT INTO chat_todo_intent_skips"
            " (skip_id, session_id, seq, message_id, author_id, owner, intent,"
            "  reason, hits, text, dedupe_key)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)"
            " ON CONFLICT (session_id, dedupe_key) DO NOTHING"
            " RETURNING skip_id",
            [gen_skip_id(), session_id, (message or {}).get("seq"),
             str((message or {}).get("message_id") or ""), author_id or "",
             owner or "", str(cls.get("intent") or "followup"),
             str(cls.get("reason") or ""),
             json.dumps(list(cls.get("hits") or [])[:12], ensure_ascii=False),
             str(text or "")[:TODO_TEXT_MAX], key])])
        return rows[0][0] if rows else None

    def todo_bump_followup(self, todo_id: str) -> Optional[Dict[str, Any]]:
        """追问次数 +1（**只改计数，不新建条目、不动 status**）。"""
        rows = self.execute_tx([(
            "UPDATE chat_session_todos"
            " SET followup_count = followup_count + 1, updated_at = now()"
            " WHERE todo_id = %s RETURNING " + TODO_COLUMNS,
            [todo_id])])
        return self._row_to_todo(rows[0]) if rows else None

    def todos_skips_list(self, session_id: str, limit: int = 200
                         ) -> List[Dict[str, Any]]:
        """#478：本群「判为追问、没入队」的消息台账（只读，最新在前）。"""
        rows = self.query(
            "SELECT %s FROM chat_todo_intent_skips WHERE session_id = %%s"
            " ORDER BY created_at DESC, skip_id DESC LIMIT %%s" % TODO_SKIP_COLUMNS,
            [session_id, limit])
        return [{
            "id": r[0], "session_id": r[1], "seq": r[2], "message_id": r[3],
            "author_id": r[4], "owner": r[5], "owner_display": display_of(r[5]),
            "intent": r[6], "reason": r[7],
            "hits": list(r[8]) if isinstance(r[8], list) else [],
            "text": r[9], "created_at": iso_bj(r[10]),
        } for r in rows]

    def todo_mark_done(self, todo_id: str, actor: str,
                       evidences: Sequence[Dict[str, str]],
                       landing: Sequence[str] = ()
                       ) -> Optional[Dict[str, Any]]:
        """落点校验通过后由**后端**翻 done（不是 agent 自己写 status）。"""
        rows = self.execute_tx([(
            "UPDATE chat_session_todos SET status = 'done', cleared_at = now(),"
            " cleared_by = %s, updated_at = now(),"
            " evidences = COALESCE(evidences, '[]'::jsonb) || %s::jsonb,"
            " landing = COALESCE(landing, '[]'::jsonb) || %s::jsonb"
            " WHERE todo_id = %s RETURNING " + TODO_COLUMNS,
            [actor, json.dumps(list(evidences), ensure_ascii=False),
             json.dumps(list(landing), ensure_ascii=False), todo_id])])
        return self._row_to_todo(rows[0]) if rows else None

    def todo_mark_doing(self, todo_id: str, actor: str) -> Optional[Dict[str, Any]]:
        """#448：开工（待办 -> 进行中）。

        只翻状态，不写落点、不置 cleared_at：`doing` 是过程态，不是完成章
        （完成章仍只认 todo_mark_done，且必须已过落点校验）。
        """
        rows = self.execute_tx([(
            "UPDATE chat_session_todos SET status = 'doing', updated_at = now(),"
            " cleared_at = NULL, cleared_by = ''"
            " WHERE todo_id = %s RETURNING " + TODO_COLUMNS,
            [todo_id])])
        return self._row_to_todo(rows[0]) if rows else None

    def todo_reopen(self, todo_id: str) -> Optional[Dict[str, Any]]:
        """撤回（done -> open）：落点清空、清账留痕字段复位。"""
        rows = self.execute_tx([(
            "UPDATE chat_session_todos SET status = 'open', cleared_at = NULL,"
            " cleared_by = '', evidences = '[]'::jsonb, landing = '[]'::jsonb,"
            " updated_at = now()"
            " WHERE todo_id = %s RETURNING " + TODO_COLUMNS,
            [todo_id])])
        return self._row_to_todo(rows[0]) if rows else None

    def todos_purge(self, session_id: str) -> List[str]:
        """#448-3：**队列级清空**（左栏「任务队列」的清空按钮）。

        与 `todos_clear`（勾选递交 = 逐条盖完成章，还要落点）不同：purge 是显式的
        「这个队列不要了」——整批删掉本群队列条目，页面归空且**刷新后仍为空**
        （是落库状态，不是前端 localStorage）。删掉的 id 由调用方写进服务日志留痕。
        只按 session_id 限定，不跨群。
        """
        rows = self.query(
            "SELECT todo_id FROM chat_session_todos WHERE session_id = %s"
            " ORDER BY created_at ASC, todo_id ASC", [session_id])
        ids = [str(row[0]) for row in rows]
        if ids:
            self.execute_tx([(
                "DELETE FROM chat_session_todos"
                " WHERE session_id = %s AND todo_id = ANY(%s::text[])",
                [session_id, ids])])
        return ids

    def todos_rows(self, session_id: str, ids: Sequence[str]) -> List[Dict[str, Any]]:
        """按 id 批量读条目（`/todos/clear` 落点校验用，只读不写）。"""
        if not ids:
            return []
        return [self._row_to_todo(r) for r in self.query(
            "SELECT %s FROM chat_session_todos"
            " WHERE session_id = %%s AND todo_id = ANY(%%s::text[])" % TODO_COLUMNS,
            [session_id, list(ids)])]

    def todos_clear(self, session_id: str, ids: Sequence[str], actor: str,
                    landing: Sequence[str] = (),
                    evidences: Sequence[Dict[str, str]] = ()
                    ) -> Dict[str, Any]:
        """批量勾选递交（#114/#124，**幂等**）：只清 owner=actor 且仍 open 的。

        重复递交（前端重复点）不双清：第二次这些条目已不是 open，直接进 skipped。
        落点（本轮补）：与 PATCH 同口径，`landing`/`evidences` 随本次递交落进被清的条目。
        """
        rows = self.query(
            "SELECT todo_id, owner, status FROM chat_session_todos"
            " WHERE session_id = %s AND todo_id = ANY(%s::text[])",
            [session_id, list(ids)])
        cleared, skipped = [], []  # type: List[str], List[Dict[str, str]]
        for todo_id, owner, status in rows:
            if owner != actor:
                skipped.append({"id": todo_id, "why": "not_yours"})
            elif status != "open":
                skipped.append({"id": todo_id, "why": "already_done"})
            else:
                cleared.append(todo_id)
        if cleared:
            self.execute_tx([(
                "UPDATE chat_session_todos SET status = 'done', cleared_at = now(),"
                " cleared_by = %s, updated_at = now(),"
                " evidences = COALESCE(evidences, '[]'::jsonb) || %s::jsonb,"
                " landing = COALESCE(landing, '[]'::jsonb) || %s::jsonb"
                " WHERE session_id = %s AND todo_id = ANY(%s::text[])"
                " AND owner = %s AND status = 'open'",
                [actor, json.dumps(list(evidences), ensure_ascii=False),
                 json.dumps(list(landing), ensure_ascii=False),
                 session_id, cleared, actor])])
        known = set(row[0] for row in rows)
        for todo_id in ids:
            if todo_id not in known:
                skipped.append({"id": todo_id, "why": "not_found"})
        return {"cleared": cleared, "skipped": skipped}

    def _row_to_todo(self, row: Sequence[Any]) -> Dict[str, Any]:
        return {
            "id": row[0], "session_id": row[1], "text": row[2], "owner": row[3],
            "kind": row[4], "status": row[5], "deadline": iso_bj(row[6]),
            "created_by": row[7] or "", "created_at": iso_bj(row[8]),
            "updated_at": iso_bj(row[9]), "cleared_at": iso_bj(row[10]),
            "cleared_by": row[11] or "",
            "evidences": list(row[12]) if isinstance(row[12], list) else [],
            "landing": list(row[13]) if isinstance(row[13], list) else [],
            "intent": str(row[14] or "task"),
            "followup_count": int(row[15] or 0),
            "task_name": str(row[16] or ""),          # #534 抽象短名（#541 起由模型给）
            "task_summary": str(row[17] or ""),       # #541 模型详情
            "enrich_state": str(row[18] or ""),       # #541 富化状态
        }

    def members_write(self, session_id: str, add: Sequence[str], remove: Sequence[str],
                      actor: str) -> Dict[str, Any]:
        """加人 / 踢人（无人时返 changed=[]）。

        乐观并发：`WHERE members = <我读到的旧值>`，两个人同时改必有一个返 0 行。
        踢人顺带把其名下待回应账作废（摘掉 needs_response 里的该成员），
        否则人走了账还挂着，群里永久挂红。两件事在同一事务里。
        """
        rows = self.query(
            "SELECT %s FROM chat_sessions WHERE session_id = %%s" % SESSION_COLUMNS,
            [session_id])
        if not rows:
            raise RequestError(404, "not_found", "会话不存在")
        old = self._row_to_session(rows[0])
        old_ids = list(old.get("member_ids") or [])
        new_ids = [m for m in old_ids if m not in remove]
        for mid in add:
            if mid not in new_ids:
                new_ids.append(mid)
        changed = [m for m in new_ids if m not in old_ids]
        left = [m for m in old_ids if m not in new_ids]
        if not changed and not left:
            return {"session": old, "added": [], "removed": [], "voided": 0}

        statements = [
            ("SELECT pg_advisory_xact_lock(hashtext(%s))", [session_id]),
            ("UPDATE chat_sessions SET members = %s::jsonb, updated_at = now()"
             " WHERE session_id = %s AND members = %s::jsonb"
             " RETURNING " + SESSION_COLUMNS,
             [json.dumps(new_ids, ensure_ascii=False), session_id,
              json.dumps(old_ids, ensure_ascii=False)]),
        ]
        voided = 0
        if left:
            statements.append((
                "UPDATE chat_messages SET meta = jsonb_set("
                " meta, '{needs_response}',"
                " COALESCE((SELECT jsonb_agg(e) FROM"
                "  jsonb_array_elements(meta->'needs_response') e"
                "  WHERE NOT (e = ANY (SELECT to_jsonb(u) FROM unnest(%s::text[]) u))),"
                " '[]'::jsonb))"
                " WHERE session_id = %s"
                " AND meta->'needs_response' ?| %s::text[]"
                " RETURNING seq",
                [list(left), session_id, list(left)]))
        result = self.execute_tx(statements)
        # execute_tx 只回最后一条语句的行：有踢人时是作废统计，否则是 UPDATE 后的会话行。
        if left:
            voided = len(result or [])
            updated_rows = self.query(
                "SELECT %s FROM chat_sessions WHERE session_id = %%s" % SESSION_COLUMNS,
                [session_id])
        else:
            updated_rows = result
        if not updated_rows:
            raise RequestError(409, "members_conflict",
                               "成员表刚被其他人改过，请刷新后重试")
        return {
            "session": self._row_to_session(updated_rows[0]),
            "added": changed, "removed": left, "voided": voided,
        }

    def add_message(self, session_id: str, author_type: str, author_id: str,
                    text: str, mentions: Sequence[str], audience: Sequence[str],
                    needs_response: Sequence[str], reply_to: Optional[str] = None,
                    client_msg_id: Optional[str] = None
                    ) -> Optional[Dict[str, Any]]:
        """往群里落一条消息，seq 由本事务内 MAX+1 分配（advisory lock 防并发撞号）。"""
        meta = json.dumps({
            "mentions": list(mentions),
            "audience": list(audience),
            "needs_response": list(needs_response),
            "client_msg_id": client_msg_id,
        }, ensure_ascii=False)
        message_id = "msg_%s_%s" % (
            datetime.datetime.now(BJ).strftime("%Y%m%d%H%M%S"), secrets.token_hex(3))
        # 顺序要紧：lock -> 抬 updated_at -> INSERT(RETURNING)，最后一条才有行。
        rows = self.execute_tx([
            ("SELECT pg_advisory_xact_lock(hashtext(%s))", [session_id]),
            ("UPDATE chat_sessions SET updated_at = now() WHERE session_id = %s",
             [session_id]),
            # 聚合 MAX 保证"没有历史消息时也返回一行"，否则 INSERT 会插 0 行。
            ("INSERT INTO chat_messages (message_id, session_id, seq, author_type,"
             " author_id, kind, text, status, reply_to, meta)"
             " SELECT %s, %s, COALESCE(MAX(seq), 0) + 1, %s, %s, 'text', %s, 'done',"
             " %s, %s::jsonb FROM chat_messages WHERE session_id = %s"
             " RETURNING " + MESSAGE_COLUMNS,
             [message_id, session_id, author_type, author_id, text, reply_to, meta,
              session_id]),
        ])
        return self._row_to_message(rows[0]) if rows else None

    # -- 查询 -------------------------------------------------------
    def list_sessions(self, role: str, filters: Dict[str, Any]) -> List[Dict[str, Any]]:
        sql = ["SELECT %s FROM chat_sessions WHERE 1=1" % SESSION_COLUMNS]
        params = []  # type: List[Any]

        kind = filters.get("kind")
        if kind:
            sql.append("AND kind = %s")
            params.append(kind)
        state = filters.get("state")
        if state:
            sql.append("AND state = %s")
            params.append(state)
        member = filters.get("member")
        if member:
            sql.append("AND members @> %s::jsonb")
            params.append(json.dumps([member]))
        cursor = filters.get("cursor")
        if cursor:
            # 游标 = 上一页最后一条的 updated_at（不透明，前端不解析）
            sql.append("AND updated_at < %s")
            params.append(cursor)
        limit = filters.get("limit") or PAGE_LIMIT_DEFAULT
        sql.append("ORDER BY updated_at DESC LIMIT %s")
        params.append(int(limit) + 1)          # 多取一条判断 has_more

        rows = self.query(" ".join(sql), params)
        sessions = [self._row_to_session(row) for row in rows[:int(limit)]]
        # 密级过滤：无权即不可见（契约 §3）
        sessions = [s for s in sessions
                    if view_auth.role_can_see(role, s.get("sensitivity"))]
        return sessions

    def get_session(self, session_id: str, role: str) -> Optional[Dict[str, Any]]:
        rows = self.query(
            "SELECT %s FROM chat_sessions WHERE session_id = %%s" % SESSION_COLUMNS,
            [session_id],
        )
        if not rows:
            return None
        session = self._row_to_session(rows[0])
        if not view_auth.role_can_see(role, session.get("sensitivity")):
            return None                        # 不泄露存在性（契约 §3）
        return session

    def last_message(self, session_id: str) -> Optional[Dict[str, Any]]:
        rows = self.query(
            "SELECT %s FROM chat_messages WHERE session_id = %%s"
            " ORDER BY seq DESC LIMIT 1" % MESSAGE_COLUMNS,
            [session_id],
        )
        return self._row_to_message(rows[0]) if rows else None

    def list_messages(self, session_id: str, limit: int, before_seq: Optional[int],
                      after_seq: Optional[int]) -> Tuple[List[Dict[str, Any]], bool, Optional[int]]:
        """返回 (messages 升序, has_more, next_before_seq)。"""
        if before_seq is not None:
            rows = self.query(
                "SELECT %s FROM (SELECT %s FROM chat_messages WHERE session_id = %%s"
                " AND seq < %%s ORDER BY seq DESC LIMIT %%s) t ORDER BY seq ASC" % (
                    MESSAGE_COLUMNS, MESSAGE_COLUMNS),
                [session_id, before_seq, limit + 1],
            )
        elif after_seq is not None:
            rows = self.query(
                "SELECT %s FROM chat_messages WHERE session_id = %%s AND seq > %%s"
                " ORDER BY seq ASC LIMIT %%s" % MESSAGE_COLUMNS,
                [session_id, after_seq, limit + 1],
            )
        else:
            rows = self.query(
                "SELECT %s FROM (SELECT %s FROM chat_messages WHERE session_id = %%s"
                " ORDER BY seq DESC LIMIT %%s) t ORDER BY seq ASC" % (
                    MESSAGE_COLUMNS, MESSAGE_COLUMNS),
                [session_id, limit + 1],
            )
        has_more = len(rows) > limit
        if has_more:
            # after_seq 方向：多取的那条在尾部（更新的），保留头部；
            # before_seq / 默认方向：SQL 是"倒序取 limit+1 再正序"，多取的在头部（更老的），保留尾部。
            rows = rows[:limit] if after_seq is not None else rows[-limit:]
        messages = [self._row_to_message(r) for r in rows]
        next_before = messages[0]["seq"] if (messages and has_more) else None
        return messages, has_more, next_before

    # -- 行 -> 对象 -------------------------------------------------
    def _row_to_session(self, row: Sequence[Any]) -> Dict[str, Any]:
        (sid, kind, title, sensitivity, members, created_by, created_at,
         updated_at, state, ref_table, ref_id, note) = row
        member_ids = members if isinstance(members, list) else []
        return {
            "session_id": sid,
            "kind": kind,
            "title": title,
            "note": note or "",
            "sensitivity": sensitivity,
            "members": [{"id": m, "display": display_of(str(m))} for m in member_ids],
            "member_ids": [str(m) for m in member_ids],
            "created_by": created_by,
            "created_at": iso_bj(created_at),
            "updated_at": iso_bj(updated_at),
            "state": state,
            "ref": {"table": ref_table, "id": ref_id} if ref_id else None,
        }

    def _row_to_message(self, row: Sequence[Any]) -> Dict[str, Any]:
        (mid, sid, seq, author_type, author_id, kind, text, ref_table, ref_id,
         status, reply_to, run, meta, created_at) = row
        meta = meta if isinstance(meta, dict) else {}
        # ---- 上下文水位（服务端渲染，2026-09-24）----
        # Agent 回复不再手打「📚 上下文：约 Xk/Yk (Z%)」那行；改由服务端
        # 按真源（<MEMBER_ID_1> sessions.json / <MEMBER_ID_2> state.db）现读现挂到 meta.context。
        # 口径：只对 agent 发言挂（人不挂）；读不到就不挂（绝不编造）。
        # **不覆盖已有 meta.context** —— 落库时若不慎带了 error 标记则保留（见 _stamp_context）。
        if author_type == "agent":
            ctx = self._context_watermark(str(author_id), sid)
            if ctx:
                meta = dict(meta)
                meta["context"] = ctx
        return {
            "message_id": mid,
            "session_id": sid,
            "seq": int(seq),
            "author": {
                "type": author_type,
                "id": author_id,
                "display": display_of(str(author_id)) if author_type != "system" else "系统",
            },
            "kind": kind,
            "text": text,
            "ref": {"table": ref_table, "id": ref_id} if ref_id else None,
            "status": status,
            "reply_to": reply_to,
            "run": run,
            "meta": meta,
            "created_at": iso_bj(created_at),
        }

    # ---- 上下文水位（服务端渲染，2026-09-24）----
    def _agent_backend_cfg(self, member_id: str) -> Optional[Dict[str, Any]]:
        """取某成员的 backend/model（用于判定水位数据源）。读 agents.json 原文缓存。"""
        cache = getattr(self, "_backend_cache", None)
        if cache is None:
            cache = {}
            self._backend_cache = cache
        if not cache:
            for path in (os.environ.get(ROSTER_ENV) or "").replace(";", ":").split(":"):
                path = path.strip()
                if not path:
                    continue
                try:
                    with open(path, "r", encoding="utf-8") as handle:
                        data = json.load(handle)
                except Exception:  # noqa: BLE001
                    continue
                for member, spec in ((data.get("agents") or {}) if isinstance(data, dict) else {}).items():
                    if member in cache:
                        continue
                    spec = spec if isinstance(spec, dict) else {}
                    cache[member] = {"backend": str(spec.get("backend") or ""),
                                     "model": str(spec.get("model") or "")}
        return cache.get(member_id)

    def _context_watermark(self, member_id: str, session_id: str) -> Optional[Dict[str, Any]]:
        """本群该成员本轮的上下文水位（真源读，不编造）。读不到返回 None。"""
        if _ctx_wm is None or not member_id or not session_id:
            return None
        try:
            cfg = self._agent_backend_cfg(member_id)
            return _ctx_wm.read_watermark(caller=member_id, session_id="meet:" + session_id,
                                          agent_cfg=cfg)
        except Exception as exc:  # noqa: BLE001 —— 水位是增强，绝不允许拖墤主流程
            LOG.debug("上下文水位渲染失败(%s)", scrubbed(exc))
            return None


# ---------------------------------------------------- #541 模型富化（任务判定 + 任务名抽象）

#: #541：任务**是不是任务**、**叫什么名**这两件事交给模型做 —— 规则只做快速预筛。
#: 模型通道 = <HOST>上**反向 SSH 回本机**跑 claude_exec.py（原生 claude.exe + 本机代理，
#: 包月额度；本机直连 api.anthropic.com 必 403，别试）。整条链路可被环境变量覆盖，
#: 排障/降级实测就靠它们：
#:   CHAT_ENRICH_ENABLE=0        → 关掉富化（worker 不起，条目保留规则名）
#:   CHAT_ENRICH_CMD=...         → 换远端执行器命令行（**指向错路径 = 模型不可用降级实测**）
#:   CHAT_ENRICH_SSH=user@host   → 换反向 SSH 目标
#:   CHAT_ENRICH_MIN_INTERVAL_S  → 两次模型调用之间的最小间隔（默认 1s，限速）
#: 执行器「怎么调」整段已下沉 L2 `agent_model`（ssh 二进制 / 反向目标 / 私钥与
#: known_hosts / 远端命令行 / 编码口径 / 超时与限速默认值 / subprocess 与输出解析）。
#: 这里只保留**别名**，env 名与默认值逐字不变，本件既有引用点零改动。
ENRICH_ENABLE_ENV = agent_model.ENABLE_ENV
ENRICH_SSH_BIN_ENV = agent_model.SSH_BIN_ENV
ENRICH_SSH_ENV = agent_model.SSH_ENV
ENRICH_KEY_ENV = agent_model.KEY_ENV
ENRICH_KNOWN_ENV = agent_model.KNOWN_ENV
ENRICH_CMD_ENV = agent_model.CMD_ENV
ENRICH_TIMEOUT_ENV = agent_model.TIMEOUT_ENV
ENRICH_MIN_INTERVAL_ENV = agent_model.MIN_INTERVAL_ENV

ENRICH_SSH_BIN = agent_model.SSH_BIN
ENRICH_SSH_TARGET = agent_model.SSH_TARGET
ENRICH_KEY_FILE = agent_model.KEY_FILE
ENRICH_KNOWN_HOSTS = agent_model.KNOWN_HOSTS
ENRICH_REMOTE_CMD = agent_model.REMOTE_CMD
ENRICH_TIMEOUT_S = agent_model.TIMEOUT_S
ENRICH_MIN_INTERVAL_S = agent_model.MIN_INTERVAL_S
ENRICH_NAME_MAX = agent_model.NAME_MAX
ENRICH_SUMMARY_MAX = agent_model.SUMMARY_MAX
#: 以下四个是**本层（L3）**的 worker 旋钮，不属执行器适配，留在这里。
ENRICH_QUEUE_MAX = 200
ENRICH_CONTEXT_LINES = 6
ENRICH_BACKFILL_MAX = 20          # 一次回填最多排 20 条
ENRICH_BACKFILL_DEFAULT = 10

#: 提示词：要**严格 JSON**，一行；口径写死在提示里（模型判 is_task=false 的三种典型形状）。
TODO_ENRICH_PROMPT = """你是团队群「任务队列」的判定器。判断下面这条群消息是不是一条「给人派活的任务」，并抽象出任务名。

只输出**一行 JSON**（不要 markdown 代码块、不要解释、不要多余文字）：
{"is_task": true, "task_name": "不超过18个汉字的抽象任务名", "summary": "不超过80字的详情", "confidence": 0.9}

判定口径：
- is_task=false：纯寒暄 / 纯提问 / 进度追问（「完成了吗」「到哪一步了」「还要多久」）/ 催办警告 / **只发了抬头没有正文**（如「我给你一个任务：」「如下任务：」）；这类消息不进队列。
- is_task=true：明确要求某人或某个 agent 对某个**具体对象**做某个**具体动作**（修复/实现/部署/验证/整理/删除/复验…），有可验收的产出。
- task_name：把这件事**抽象**成的动宾短名（例：「修复任务队列短名截断问题」）。必须是概括，**不许照抄原文前缀**，不含 @点名、#序号、时间戳、口水词（「你负责」「如下任务」「请」）。
- summary：做什么 + 验收标准 + 涉及对象，一句话。
- confidence：0 到 1 之间，你对 is_task 判断的把握。

群名：%s
发言者：%s
这条消息（seq=%s）：%s
上下文（同群最近几条，旧→新）：
%s
"""


# ---- 执行器适配的薄委托（实现见 L2 agent_model；签名与返回值逐字不变）----

def todo_enrich_enabled() -> bool:
    """富化总开关（`CHAT_ENRICH_ENABLE=0/false/no/off` 关）。"""
    return agent_model.enabled()


def todo_enrich_timeout() -> float:
    return agent_model.timeout_s()


def todo_enrich_min_interval() -> float:
    return agent_model.min_interval_s()


def todo_enrich_argv() -> List[str]:
    """模型调用的完整命令行（ssh → 本机 claude_exec.py），提示词走 stdin。"""
    return agent_model.ssh_argv()


def todo_enrich_parse(raw: Any) -> Optional[Dict[str, Any]]:
    """模型输出 → 校验过的判定。**不合格一律返回 None**（调用方据此重试 / 降级）。"""
    return agent_model.parse(raw)


def todo_enrich_call_model(prompt: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """把提示词交给模型，返回 (判定, 说明)。判定 None = 这次失败（原因写在说明里）。

    **失败是正常路径**：超时 / ssh 不通 / 执行器不存在 / 输出不是 JSON —— 全部只返回
    None，由调用方重试或降级成规则名。这里绝不抛异常给上层。
    """
    return agent_model.call(prompt)


def todo_enrich_seq_of(text: Any) -> Optional[int]:
    """条目正文形如 `#448 <摘要>`，从里面取回原消息 seq（上下文定位用）。"""
    m = re.match(r"^#\s?(\d{1,9})\b", str(text or ""))
    return int(m.group(1)) if m else None


def todo_enrich_is_agent(member_id: Any) -> bool:
    """名册里的是 agent，名册外的是人（喂提示词时说清发言人是哪一侧）。"""
    try:
        return bool(member_id) and str(member_id) in roster_agent_ids(Runtime.roster)
    except Exception:
        return False


def todo_enrich_job(row: Optional[Dict[str, Any]], source: str = "enqueue",
                    message: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """队列条目 → 富化任务（worker 只认这个结构）。"""
    row = row or {}
    text = str(row.get("text") or "")
    seq = (message or {}).get("seq") or row.get("seq") or todo_enrich_seq_of(text)
    try:
        seq = int(seq) if seq is not None else None
    except (TypeError, ValueError):
        seq = None
    return {"todo_id": row.get("id"), "session_id": row.get("session_id"),
            "text": text, "seq": seq, "owner": row.get("owner") or "",
            "author_id": row.get("created_by") or "",
            "rule_name": str(row.get("task_name") or ""), "source": source,
            "message": message or {"seq": seq}}


class TodoEnricher(object):
    """#541：单线程 + 队列的**异步富化 worker**（模型调用绝不进 HTTP 请求路径）。

    为什么单线程：模型通道是本机 Claude（包月、但**并发就是打爆它**），而且限速要求
    是「每秒 ≤1 次」—— 单线程 + 固定间隔最简单也最不容易失控。

    三条硬底线（改这个类之前先读）：
      1. `submit()` **永不阻塞**：队列满就 `put_nowait` 失败 → 直接返回 False，
         条目保留规则名（宁可不富化，也不让人的请求卡住）；
      2. 任何一步异常都只告警，**不影响已入队的条目**；
      3. 模型判 `is_task=false` 时按 source 分开处理：`enqueue`（刚落的新条目）撤掉它
         + 记 skips 台账；`backfill`（历史条目）**只标记不动数据**（不删别人的台账）。
    """

    def __init__(self, maxsize: int = ENRICH_QUEUE_MAX) -> None:
        self.q = queue.Queue(maxsize=max(1, maxsize))  # type: queue.Queue
        self._thread = None  # type: Optional[threading.Thread]
        self._stop = threading.Event()
        self._last_call = 0.0
        self._lock = threading.Lock()
        self.stats = {"submitted": 0, "done": 0, "fallback": 0, "not_task": 0,
                      "dropped": 0, "last_why": "", "last_elapsed_s": 0.0}

    # -- 生命周期 ---------------------------------------------------
    def start(self) -> bool:
        if self.running:
            return True
        if not todo_enrich_enabled():
            LOG.warning("模型富化被 %s 关掉：条目一律保留规则名（worker 不起）",
                        ENRICH_ENABLE_ENV)
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run,
                                        name="todo-enricher", daemon=True)
        self._thread.start()
        self._redrive_stale_pending()
        LOG.info("模型富化 worker 已起（单线程；限速 %.1fs/次；超时 %.0fs；目标 %s）",
                 todo_enrich_min_interval(), todo_enrich_timeout(),
                 (os.environ.get(ENRICH_SSH_ENV) or ENRICH_SSH_TARGET))
        return True

    def stop(self) -> None:
        self._stop.set()

    @property
    def running(self) -> bool:
        return bool(self._thread is not None and self._thread.is_alive())

    def stats_snapshot(self) -> Dict[str, Any]:
        with self._lock:
            snap = dict(self.stats)
        snap.update({"running": self.running, "queued": self.q.qsize(),
                     "enabled": todo_enrich_enabled(),
                     "min_interval_s": todo_enrich_min_interval()})
        return snap

    # -- 入口 -------------------------------------------------------
    def submit(self, job: Dict[str, Any]) -> bool:
        """把一条富化任务塞进队列。**非阻塞**：满了 / 没起就返回 False。"""
        if not job.get("todo_id") or not self.running:
            return False
        try:
            self.q.put_nowait(job)
        except queue.Full:
            LOG.warning("模型富化队列已满（%d），本条保留规则名 todo=%s",
                        self.q.maxsize, job.get("todo_id"))
            return False
        with self._lock:
            self.stats["submitted"] += 1
        return True

    def _redrive_stale_pending(self) -> int:
        """启动自愈：把**上一个进程**遗留的 `pending` 条目补投一次。

        为什么需要：富化队列只在内存里。重启（含 systemctl restart / 崩溃重启）会把
        「已提交、模型还没回答」的条目原地留成 `pending`，没人再管它 —— 界面永远显示
        「富化中」，而回填通道原来的口径也看不见它。启动时按 created_at 补投是最小的
        自愈动作：有界（ENRICH_BACKFILL_MAX 条）、非阻塞（走 submit，满就放弃）、
        source=backfill（模型判不是任务时**只标记不删**，不替别的 agent 撤台账）。

        任何异常只告警，绝不影响服务起停。
        """
        try:
            db = getattr(Runtime, "db", None)
            if db is None:
                return 0
            rows = db.todos_stale_pending(ENRICH_BACKFILL_MAX)
        except Exception as exc:
            LOG.warning("富化启动自愈：扫 pending 失败（忽略）：%s", scrubbed(exc))
            return 0
        n = 0
        for row in rows:
            try:
                if self.submit(todo_enrich_job(row, "backfill")):
                    n += 1
            except Exception as exc:
                LOG.warning("富化启动自愈：补投失败 todo=%s：%s",
                            row.get("id"), scrubbed(exc))
        if n:
            LOG.info("富化启动自愈：上个进程遗留 %d 条 pending（共扫到 %d 条），已补投",
                     n, len(rows))
        return n

    # -- 内部 -------------------------------------------------------
    def _throttle(self) -> None:
        gap = todo_enrich_min_interval() - (time.time() - self._last_call)
        if gap > 0:
            time.sleep(gap)
        self._last_call = time.time()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                job = self.q.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self._handle(job)
            except Exception as exc:
                LOG.warning("模型富化处理异常（本条保留规则名）：%s", scrubbed(exc))
            finally:
                self.q.task_done()

    def _prompt(self, job, todo) -> str:
        """提示词 = 这条消息 + 上下文（发言人 / 群名 / 同群最近几条）。"""
        sid = str(job.get("session_id") or "")
        title = ""
        try:
            session = Runtime.db.get_session(sid, "owner") if Runtime.db else None
            title = str((session or {}).get("title") or "")
        except Exception as exc:
            LOG.debug("富化取群名失败：%s", scrubbed(exc))
        author = job.get("author_id") or ""
        who = display_of(str(author)) or str(author) or "-"
        lines = []  # type: List[str]
        seq = job.get("seq")
        try:
            msgs, _more, _nb = Runtime.db.list_messages(
                sid, ENRICH_CONTEXT_LINES, (int(seq) + 1) if seq else None, None)
            for m in msgs:
                au = (m.get("author") or {})
                who_m = au.get("display") or display_of(str(au.get("id") or "")) or "-"
                lines.append("%s(#%s): %s" % (who_m, m.get("seq"),
                                              excerpt(m.get("text") or "", 60)))
        except Exception as exc:
            LOG.debug("富化取上下文失败：%s", scrubbed(exc))
        return TODO_ENRICH_PROMPT % (
            title or "-",
            who + ("（agent）" if todo_enrich_is_agent(author) else "（人）"),
            seq if seq is not None else "?",
            excerpt((todo or {}).get("text") or job.get("text") or "", 400),
            "\n".join(lines) or "（无）")

    def _handle(self, job: Dict[str, Any]) -> Dict[str, Any]:
        todo_id = str(job.get("todo_id") or "")
        todo = Runtime.db.todo_get(todo_id) if Runtime.db else None
        if not todo:
            LOG.info("模型富化跳过：条目已不在 todo=%s", todo_id)
            return {"state": "gone"}
        prompt = self._prompt(job, todo)
        verdict, why = None, ""
        for attempt in (1, 2):                     # 解析失败/超时 → 重试 1 次
            self._throttle()
            verdict, why = todo_enrich_call_model(prompt)
            if verdict is not None:
                break
            LOG.warning("模型富化第 %d/2 次没成功（%s）todo=%s", attempt, why, todo_id)
        if verdict is None:
            # 降级：保留规则名，只把「模型没回来」这件事记在库里 + journal
            Runtime.db.todo_set_enrichment(todo_id, None, None, "fallback")
            with self._lock:
                self.stats["fallback"] += 1
                self.stats["last_why"] = why
            LOG.warning("模型富化降级：模型不可用（%s），保留规则名 task_name=%r todo=%s",
                        why, todo.get("task_name"), todo_id)
            return {"state": "fallback", "why": why}
        with self._lock:
            self.stats["last_why"] = why
            self.stats["last_elapsed_s"] = verdict.get("elapsed_s") or 0.0
        if not verdict["is_task"]:
            return self._apply_not_task(job, todo, verdict)
        Runtime.db.todo_set_enrichment(todo_id, verdict["task_name"],
                                       verdict["summary"], "done")
        with self._lock:
            self.stats["done"] += 1
        LOG.info("模型富化回写 session=%s todo=%s 规则名=%r → 模型名=%r conf=%.2f"
                 " summary=%r（%.1fs）",
                 job.get("session_id"), todo_id, job.get("rule_name"),
                 verdict["task_name"], verdict["confidence"], verdict["summary"],
                 verdict.get("elapsed_s") or 0.0)
        return {"state": "done", "task_name": verdict["task_name"]}

    def _apply_not_task(self, job, todo, verdict) -> Dict[str, Any]:
        """模型判「这条不是任务」：记 skips 台账 + 按来源决定要不要撤条目。"""
        sid = str(job.get("session_id") or todo.get("session_id") or "")
        cls = {"intent": "not_task", "reason": "model_not_task",
               "hits": ["model", "conf=%.2f" % verdict["confidence"]]}
        try:
            Runtime.db.todo_skip_create(sid, job.get("message"),
                                        str(job.get("author_id") or ""),
                                        str(todo.get("owner") or ""), cls,
                                        str(todo.get("text") or ""))
        except Exception as exc:
            LOG.warning("模型富化记 skips 台账失败：%s", scrubbed(exc))
        with self._lock:
            self.stats["not_task"] += 1
        if str(job.get("source") or "") == "enqueue":
            removed = False
            try:
                removed = Runtime.db.todo_delete_pristine(
                    todo.get("id") or "", str(job.get("rule_name") or ""))
            except Exception as exc:
                LOG.warning("模型富化撤条目失败：%s", scrubbed(exc))
            LOG.info("模型判**不是任务** → 不入队 session=%s todo=%s removed=%s"
                     " conf=%.2f 正文=%r（台账 reason=model_not_task，"
                     "GET /api/sessions/{sid}/todos/intents 可查）",
                     sid, todo.get("id"), removed, verdict["confidence"],
                     str(todo.get("text") or "")[:60])
            return {"state": "not_task", "removed": removed}
        Runtime.db.todo_set_enrichment(todo.get("id"), None, None, "skipped")
        LOG.info("回填时模型判**不是任务** → 历史条目只标记不删 session=%s todo=%s"
                 " conf=%.2f", sid, todo.get("id"), verdict["confidence"])
        return {"state": "not_task_kept"}


def todo_enrich_submit(item: Optional[Dict[str, Any]],
                       message: Optional[Dict[str, Any]] = None) -> bool:
    """入队后把条目异步交给模型富化。**任何异常都只告警**（队列不是命门）。"""
    try:
        enricher = getattr(Runtime, "enricher", None)
        if enricher is None or not enricher.running:
            LOG.info("模型富化 worker 未运行，条目保留规则名 todo=%s",
                     (item or {}).get("id"))
            return False
        return enricher.submit(todo_enrich_job(item, "enqueue", message))
    except Exception as exc:
        LOG.warning("模型富化排队失败（保留规则名）：%s", scrubbed(exc))
        return False


# ------------------------------------------------------------------ 运行时

class Runtime(object):
    """进程级共享状态：DB、名册、鉴权。测试里直接替换这些字段即可。"""

    db = None                # type: Optional[ChatDB]
    roster = None            # type: Optional[Roster]
    auth = None              # type: Optional[Any]      —— view_auth.Auth
    session_key = None       # type: Optional[bytes]
    ui_dir = None            # type: Optional[str]        —— 同源托管的静态根
    enricher = None          # type: Optional[TodoEnricher] —— #541 模型富化 worker
    started_at = 0.0
    allow_login = False      #: 是否对外提供 /login（v1：只复用看板，不自己发）

    @classmethod
    def reset(cls) -> None:
        cls.db = None
        cls.roster = None
        cls.auth = None
        cls.session_key = None
        cls.ui_dir = None
        cls.enricher = None
        cls.allow_login = False


class Visitor(object):
    """请求背后是谁（与看板同构：tailnet 直连 / 有效会话 / 都不是）。"""

    def __init__(self, username: Optional[str], role: Optional[str], source: str) -> None:
        self.username = username
        self.role = role
        self.source = source

    @property
    def display(self) -> str:
        return display_of(self.username) if self.username else self.source

    def caps(self) -> Dict[str, bool]:
        rank = view_auth.ROLE_RANK.get(self.role or "", -1)
        return {
            "can_write": rank >= view_auth.ROLE_RANK["member"],
            "can_create_meeting": rank >= view_auth.ROLE_RANK["member"],
            "can_observe": rank >= view_auth.ROLE_RANK["viewer"],
            "can_admin": rank >= view_auth.ROLE_RANK["owner"],
        }


# ------------------------------------------------------------------ HTTP
#
# 「应答出口」这层**传输职责**交给 L1 `web_gateway`（阶段2 刀8a）：`send_response` →
# 逐条写头 → `end_headers` → 写体 → 客户端早断时吞异常，全在 L1 的 `_reply()` 里。
# 本件三个出口（`_send_json` / `_redirect` / `_send_bytes`）只剩 L1 给不了的三件事：
#   · **摆哪几个头、按什么顺序摆**（头的顺序就是发出去的字节顺序）；
#   · **HEAD 不写体**（但 `Content-Length` 仍报真实长度）；
#   · `server_version` / HTTP/1.1（L1 默认 HTTP/1.0 且 server_version 取网关名）。
#
# **本件没有流式面**（硬约束 3）：全文件搜不到 `text/event-stream` / `chunked` /
# `Transfer-Encoding`，也没有「连接不关」的写法；三个出口清一色是「先算出完整 body →
# 一次性报 `Content-Length` → 一次写完」，所以整条应答路径都能安全走 L1 的缓冲封装，
# 不需要任何「原样直写 socket」的豁免分支。
#
# **路由分派**刀8a 收的是**非 GET**：POST / PUT / PATCH / DELETE 的 11 条写路径，
# 加上它们同样够得着的三条共享前缀（`/healthz`、`/calc/api/*`、`/calc/admin/*`）
# 与兜底；**刀8b 接着把 GET / HEAD 的整张表收了进来**，于是本件再没有「自己那串
# if 选处理函数」的代码 —— 迁移前的 `_dispatch()` / `_handle()` 两个方法随之删掉
# （它们唯一的调用方就是 do_GET 与 `_gw_not_found()` 里那条 HEAD 转发）。
#
# GET 与 HEAD **一起**登记：迁移前的 `_handle()` 是一张**方法无关的共享表**（同一串
# if 同时服务 GET / HEAD / 写），HEAD 的可达集合**就是 GET 的表**。刀8a 为此只把
# HEAD 的方法分派接进 L1、表原样转回 `_handle("HEAD", ...)`；本刀把表搬进来之后，
# HEAD 就落到各自的 @GW.route 上 —— **口径一个字没改**，只是转发那一跳没了。

GW = web_gateway.Gateway("chat_svc", version=VERSION, quiet=True)

#: 登记的处理函数一律**推式写出**（自己调 `_send_json()` / `_send_bytes()`），回这个
#: 占位信封；`_dispatch_gw()` 看见 `self._sent` 为真就把它丢掉，绝不会发第二份响应。
#: 之所以不改成「返式」：迁移前 `_dispatch()` 本来就是两种出口并存 ——「叶子方法回
#: (status, payload)」与「回 None = 响应已自行发出（静态 / 反代 / 302 走这条）」，
#: 把后者改成返式等于重写四十来个叶子方法，那就不是收路由而是改业务了。
_SENT = (204, None, {})  # type: Tuple[int, Any, Dict[str, Any]]


def _gw_done(req: "web_gateway.Request",
             result: Optional[Tuple[int, Dict[str, Any]]]) -> Any:
    """把「叶子方法回 (status, payload) 或 None」收敛成 L1 的占位信封。

    与迁移前 `_dispatch()` 里那两行逐字同义：

        body = self._handle(method, path, query)
        if body is not None:              # None = 响应已自行发出
            self._send_json(body[0], body[1])
    """
    if result is not None:
        req.svc._send_json(result[0], result[1])
    return _SENT


@GW.pre
def _gw_pre(req: "web_gateway.Request") -> Any:
    """所有非 GET 方法的公共前奏：**先把请求体读完并缓存**。

    迁移前 `_dispatch()` 开头那句 `self._drain_body()` 就是这一步 —— 写端点要用
    （`_json_body()` 读的就是这份缓存），而且哪怕接下来就 405/501 也得把 socket
    排空，否则 keep-alive 连接上的下一个请求会错位。
    """
    req.svc._drain_body()
    return None


def _gw_unauthorized() -> Tuple[int, Dict[str, Any]]:
    """GET / HEAD 在 `/api/` 面上的那道 401。

    迁移前它是 `_handle()` 里**一处** `visitor is None` 判定，位置在「非 /api 一律
    走静态」之后、全部只读路由之前。刀8b 把 GET 表拆成一条条 @GW.route 之后，这一
    处判定变成了几条路由各调一次 —— 所以文案收在这里**只写一遍**，逐字与迁移前同；
    每次新造一份字典，不共享可变对象（同一进程里两个请求会同时拿到它）。
    """
    return 401, {"ok": False, "error": {
        "code": "unauthorized", "message": "需要登录"}}


@GW.not_found
def _gw_not_found(req: "web_gateway.Request") -> Any:
    """没登记的路径。三支，**顺序就是迁移前 `_handle()` 里的顺序**：

    1. 非 `/api/` 开头 —— 迁移前这一判在写端点判定**之前**，一律走同源静态托管。
       写方法在 `_serve_static()` 里落 405「静态资源只支持 GET/HEAD」；GET / HEAD
       则走静态与 **SPA 兜底** —— 未知路径回的是**整页 200**，不是 404（硬约束 4）。
       `/login`、`/logout`、`/calc`、`/calc/admin`、`/` 这些写请求都落在这里。
    2. `/api/` 下的 GET / HEAD —— 先过那道 401。迁移前 `GET /api/zzz` 未登录时是
       **401**（门槛在前、501 兜底在后），不是 501；这一步漏了就是行为变更。
    3. `/api/` 下没对上形状的 —— 照旧 501 `not_implemented`（**不是 404**，
       前端据这个码开关按钮），文案含方法与路径，一字不改。

    第 2 支**故意不做成 `@GW.auth` 全局钩子**（硬约束 1）：钩子在路由之前跑，会把
    `/healthz`、静态资源与 SPA 兜底一起拦成 401 —— 而它们迁移前根本不鉴权，未知
    路径该落的是 404（带扩展名）或 200 整页（兜底），不是 401。
    """
    svc = req.svc
    if not req.path.startswith("/api/"):
        return _gw_done(req, svc._serve_static(req.path, req.method))
    if req.method in ("GET", "HEAD") and svc._resolve_visitor() is None:
        return _gw_done(req, _gw_unauthorized())
    return _gw_done(req, svc._not_implemented(req.method, req.path))


# --- 非 GET 够得着的三条共享前缀（迁移前它们在 `_handle()` 里**不分方法**）--------

@GW.route("/healthz", methods=("POST", "PUT", "PATCH", "DELETE"))
def _gw_write_healthz(req: "web_gateway.Request") -> Any:
    """迁移前 `if path == "/healthz"` 那一判**没有方法门槛**：`POST /healthz`
    照样回 200 健康体。这里原样保留 —— 改成 405 就是行为变更。"""
    return _gw_done(req, (200, req.svc._healthz()))


@GW.route("/calc/api", methods=("POST", "PUT", "PATCH", "DELETE"), prefix=True)
def _gw_write_calc_api(req: "web_gateway.Request") -> Any:
    """`/calc/api/*` → 反代计算器后台（迁移前同样不分方法）。

    **必须再判一次 `startswith("/calc/api/")`**：迁移前写的是「带尾斜杠的前缀」，
    `/calc/api` 本身（`_dispatch` 已 rstrip 掉尾斜杠）落不进这一支、要走静态 405；
    而 L1 的前缀匹配把「路径等于前缀」也算命中，不补这一判就会把 405 悄悄变成反代。
    """
    if not req.path.startswith("/calc/api/"):
        return _gw_not_found(req)
    svc = req.svc
    return _gw_done(req, svc._calc_proxy(req.method, req.path[len("/calc"):],
                                         req.query_dict))


@GW.route("/calc/admin", methods=("POST", "PUT", "PATCH", "DELETE"), prefix=True)
def _gw_write_calc_admin(req: "web_gateway.Request") -> Any:
    """`/calc/admin/*` → 管理后台静态（写方法在 `_serve_admin()` 里落 405）。

    同上补判：`/calc/admin` 本身迁移前只对 GET/HEAD 生效，写方法走静态 405。
    """
    if not req.path.startswith("/calc/admin/"):
        return _gw_not_found(req)
    svc = req.svc
    return _gw_done(req, svc._serve_admin(req.path[len("/calc/admin"):], req.method))


# --- 写端点登记表（契约 §I：临时群）：迁移前 do_POST/PATCH/PUT/DELETE 共用的那串 if --
#
# **不挂 `@GW.auth` 全局鉴权钩子**：`/healthz` 与「未知路径」这两支迁移前根本不鉴权，
# 挂上去会让未知路径先落 401 而不是落它该落的 501/405。所以鉴权仍由每条路由自己
# 在调叶子方法时 `_require_visitor()`（= 迁移前每条路由重复的那一下）。
#
# L1 只有「精确 / 前缀」两种匹配，带 <sid> 的形状只能落前缀，再由处理函数用**原正则**
# 二次判形。正则与顺序原样搬过来，一条不动、一条不合并 —— 顺序变了就可能换一条命中。

@GW.route("/api/sessions", methods=("POST",))
def _gw_post_sessions(req: "web_gateway.Request") -> Any:
    """POST /api/sessions —— 建群（契约 §I.4）。

    精确登记管这一条、前缀登记管带 <sid> 的：L1 先查精确表，前缀只在精确表落空时
    才轮到，与迁移前 `path == "/api/sessions"` / 正则带 id 的分工一一对上。
    """
    svc = req.svc
    return _gw_done(req, svc._create_session(svc._require_visitor(), svc._json_body()))


@GW.route("/api/sessions", methods=("POST", "PUT", "PATCH", "DELETE"), prefix=True)
def _gw_write_sessions(req: "web_gateway.Request") -> Any:
    """`/api/sessions/<sid>/...` 下的全部写端点，顺序与迁移前逐条对齐。

    形状对不上的（`PUT /api/sessions/x/messages`、`POST /api/sessions/x/unknown`）
    转 `_gw_not_found()`，落 501 `not_implemented` —— 与迁移前那条兜底逐字一致。

    注意实参的**求值顺序**：`_require_visitor()` 写在 `_json_body()` 左边，所以
    「没登录 + 请求体不是 JSON」时先抛 401 而不是 400。这一点原样保留。
    """
    svc, path, method = req.svc, req.path, req.method
    m = re.match(r"^/api/sessions/([A-Za-z0-9_\-]{1,64})/messages$", path)
    if m and method == "POST":
        return _gw_done(req, svc._post_message(svc._require_visitor(), m.group(1),
                                               svc._json_body()))
    m = re.match(r"^/api/sessions/([A-Za-z0-9_\-]{1,64})$", path)
    if m and method == "PATCH":
        return _gw_done(req, svc._patch_session(svc._require_visitor(), m.group(1),
                                                svc._json_body()))
    # 加人 / 踢人（契约 §I.5）：加踢都是改成员表，同一条路径 POST/DELETE。
    m = re.match(r"^/api/sessions/([A-Za-z0-9_\-]{1,64})/members$", path)
    if m and method in ("POST", "DELETE"):
        return _gw_done(req, svc._members_write(svc._require_visitor(), m.group(1),
                                                method, svc._json_body()))
    # 群记忆锚 RoomMemo（契约 §I.6）：PUT 整份初始化 / PATCH 只追加。
    m = re.match(r"^/api/sessions/([A-Za-z0-9_\-]{1,64})/memo$", path)
    if m and method in ("PUT", "PATCH"):
        return _gw_done(req, svc._memo_write(svc._require_visitor(), m.group(1),
                                             method, svc._json_body()))
    # 待办台账（群 #108~#114 冻结）：POST 新增条目 / 批量勾选递交。
    m = re.match(r"^/api/sessions/([A-Za-z0-9_\-]{1,64})/todos$", path)
    if m and method == "POST":
        return _gw_done(req, svc._todo_create(svc._require_visitor(), m.group(1),
                                              svc._json_body()))
    m = re.match(r"^/api/sessions/([A-Za-z0-9_\-]{1,64})/todos/clear$", path)
    if m and method == "POST":
        return _gw_done(req, svc._todos_clear(svc._require_visitor(), m.group(1),
                                              svc._json_body()))
    # #448-3：队列级清空（左栏「任务队列」清空按钮）
    m = re.match(r"^/api/sessions/([A-Za-z0-9_\-]{1,64})/todos/purge$", path)
    if m and method == "POST":
        return _gw_done(req, svc._todos_purge(svc._require_visitor(), m.group(1),
                                              svc._json_body()))
    # #448-1：历史几轮「人 @agent 派活」的消息回填进队列
    m = re.match(r"^/api/sessions/([A-Za-z0-9_\-]{1,64})/todos/backfill$", path)
    if m and method == "POST":
        return _gw_done(req, svc._todos_backfill(svc._require_visitor(), m.group(1),
                                                 svc._json_body()))
    # #541：手动触发「模型富化回填」（给历史行补模型名/详情；限速，不自动跑）
    m = re.match(r"^/api/sessions/([A-Za-z0-9_\-]{1,64})/todos/enrich$", path)
    if m and method == "POST":
        return _gw_done(req, svc._todos_enrich(svc._require_visitor(), m.group(1),
                                               svc._json_body()))
    return _gw_not_found(req)


@GW.route("/api/todos", methods=("PATCH",), prefix=True)
def _gw_patch_todo(req: "web_gateway.Request") -> Any:
    """单条状态变更（路径不带 sid，与冻结口径一致：PATCH /api/todos/{id}）。

    迁移前它排在那串 if 的最后一条；拆成独立一条登记不改命中结果 —— 它的正则
    要求 `/api/todos/` 开头，与前面九条 `/api/sessions/` 开头的两两不相交。
    """
    svc = req.svc
    m = re.match(r"^/api/todos/([A-Za-z0-9_\-]{1,64})$", req.path)
    if m:
        return _gw_done(req, svc._todo_patch(svc._require_visitor(), m.group(1),
                                             svc._json_body()))
    return _gw_not_found(req)


# --- GET / HEAD 登记表（阶段2 刀8b）：迁移前 `_handle()` 里那串顺序敏感的 if -----
#
# 逐条对齐，**一条不合并、一条不重排**。迁移前的顺序（括号里是本刀对应的登记）：
#   1  path == "/healthz"                      → 精确 /healthz
#   2  path in ("/login", "/logout")           → 精确 /login + 精确 /logout
#   3  path == "/calc"                         → 精确 /calc
#   4  path.startswith("/calc/api/")           → 前缀 /calc/api（补判尾斜杠）
#   5  path == "/calc/admin"                   → 精确 /calc/admin
#   6  path.startswith("/calc/admin/")         → 前缀 /calc/admin（补判尾斜杠）
#   7  not path.startswith("/api/")            → 精确 /（根页）+ `_gw_not_found()` 兜底
#   8  visitor is None → 401                   → 各 /api 路由各判一次（见 _gw_unauthorized）
#   9  /api/whoami|agents|sessions 与六条带 <sid> 的正则 → 三条精确 + 一条 /api/sessions 前缀
#  10  _not_implemented → 501                  → `_gw_not_found()` 第 3 支
#
# 第 3~6 条的相对顺序靠 L1「**精确表先查、前缀表后查**」自然成立（`/calc/admin` 命中
# 第 5 条、`/calc/admin/x` 才轮到第 6 条），与迁移前「第 5 行在第 6 行之前」同一结果。
#
# **仍然不挂 `@GW.auth` 全局鉴权钩子**（硬约束 1）：本表里鉴权位置各不相同 ——
# `/healthz`、`/login`、`/logout`、`/calc`、`/calc/admin`、`/calc/api/*`、静态与 SPA
# 兜底**都不鉴权**，只有 `/api/*` 有 401 门槛。钩子跑在路由之前，挂上去等于让未知
# 路径先落 401，而它该落的是 404（带扩展名的静态）或 **200 整页**（SPA 兜底）。


@GW.route("/healthz", methods=("GET", "HEAD"))
def _gw_get_healthz(req: "web_gateway.Request") -> Any:
    """表里第 1 条：公开健康口，不鉴权。

    与 `_gw_write_healthz()` **分开登记**而不是往那条上加 methods：那条的来历是
    「迁移前这一判没有方法门槛」的副作用（`POST /healthz` 照样 200），这条才是正经
    只读端点。合起来写，下一个人会以为写方法的 200 是有意设计的。行为完全一致。
    """
    return _gw_done(req, (200, req.svc._healthz()))


@GW.route("/login", methods=("GET", "HEAD"))
@GW.route("/logout", methods=("GET", "HEAD"))
def _gw_get_login_redirect(req: "web_gateway.Request") -> Any:
    """表里第 2 条：`path in ("/login", "/logout")` → 转交门户登录页（302）。

    两条路径**共用一个处理函数**（叠两层装饰器），与迁移前那个 `in (...)` 一一对上。
    `_login_redirect()` 自己写出 302 并返回 None（= 响应已自行发出），所以照旧交给
    `_gw_done()` 把占位信封丢掉；它收到的 path 仍是归一后的那个（`/login/` 已被
    `_dispatch_gw()` rstrip 成 `/login`），入参与迁移前逐字相同。
    """
    svc = req.svc
    return _gw_done(req, svc._login_redirect(req.path, req.query_dict))


@GW.route("/calc", methods=("GET", "HEAD"))
def _gw_get_calc(req: "web_gateway.Request") -> Any:
    """表里第 3 条：计算器前台页 = 同源托管 ui/calc.html。

    传给 `_serve_static()` 的是写死的 `"/calc.html"`，**不是 req.path** —— 这是
    迁移前就有的「路径改写」，照抄，别顺手换成 req.path。
    """
    svc = req.svc
    return _gw_done(req, svc._serve_static("/calc.html", req.method))


@GW.route("/calc/api", methods=("GET", "HEAD"), prefix=True)
def _gw_get_calc_api(req: "web_gateway.Request") -> Any:
    """表里第 4 条：`/calc/api/*` → 反代计算器后台。

    与 `_gw_write_calc_api()` 同一条护栏，理由也同：迁移前写的是**带尾斜杠的前缀**，
    `/calc/api` 本身落不进这一支、要走第 7 条静态；而 L1 的前缀匹配把「路径等于前缀」
    也算命中，不补这一判就会把静态 404 悄悄变成反代。
    """
    if not req.path.startswith("/calc/api/"):
        return _gw_not_found(req)
    svc = req.svc
    return _gw_done(req, svc._calc_proxy(req.method, req.path[len("/calc"):],
                                         req.query_dict))


@GW.route("/calc/admin", methods=("GET", "HEAD"))
def _gw_get_calc_admin(req: "web_gateway.Request") -> Any:
    """表里第 5 条：后台侧 SPA 首页（admin-ui/index.html）。

    `/calc/admin` 与 `/calc/admin/` 归一后是同一条（`_dispatch_gw()` 已 rstrip），
    与迁移前那行注释所述一致。
    """
    svc = req.svc
    return _gw_done(req, svc._serve_admin("/" + svc.ADMIN_DOC, req.method))


@GW.route("/calc/admin", methods=("GET", "HEAD"), prefix=True)
def _gw_get_calc_admin_sub(req: "web_gateway.Request") -> Any:
    """表里第 6 条：`/calc/admin/*` 静态（目录穿越 / SPA 兜底全在 `_serve_admin()` 里）。

    那句 `startswith` 在**当前**登记下够不到反例（`/calc/admin` 本身已被上面的精确
    登记截走，前缀表只在精确表落空时才轮到）—— 留着是为了让「第 6 条只管带尾斜杠的」
    这条不变量写在代码里：哪天上面那条精确登记被挪走，这里不会静静地改了语义。
    """
    if not req.path.startswith("/calc/admin/"):
        return _gw_not_found(req)
    svc = req.svc
    return _gw_done(req, svc._serve_admin(req.path[len("/calc/admin"):], req.method))


@GW.route("/", methods=("GET", "HEAD"))
def _gw_get_root(req: "web_gateway.Request") -> Any:
    """表里第 7 条的**头一例**：群页面（前端页服务的入口）。

    迁移前 `/` 走的是「非 /api 一律静态」那条兜底：`_serve_static("/", method)`
    → 命中 ui/ 目录 → `DEFAULT_DOC`（group.html）→ `_send_file()`。这里把根路径
    **显式登记**，其余静态资源与 SPA 兜底仍留在 `_gw_not_found()` 里 —— L1 只有
    「精确 / 前缀」两种匹配，而静态兜底是**任意路径**的兜底：登记成 `/` 前缀会把
    `/api/*` 一起吃掉（会议 §H 清单第 2 条：静态绝不允许覆盖 /api/ 前缀），那才是
    真正的行为变更。`_send_file()` 本体（MIME 判定、缓存头、302、字节流写法）
    一个字没动，本刀只是把它「接进」L1 分发。
    """
    svc = req.svc
    return _gw_done(req, svc._serve_static(req.path, req.method))


@GW.route("/api/whoami", methods=("GET", "HEAD"))
def _gw_get_whoami(req: "web_gateway.Request") -> Any:
    """表里第 9 条之一。401 门槛（第 8 条）写在每条 /api 路由里，见 `_gw_unauthorized()`。"""
    svc = req.svc
    visitor = svc._resolve_visitor()
    if visitor is None:
        return _gw_done(req, _gw_unauthorized())
    return _gw_done(req, (200, svc._whoami(visitor)))


@GW.route("/api/agents", methods=("GET", "HEAD"))
def _gw_get_agents(req: "web_gateway.Request") -> Any:
    svc = req.svc
    visitor = svc._resolve_visitor()
    if visitor is None:
        return _gw_done(req, _gw_unauthorized())
    return _gw_done(req, (200, svc._agents(visitor)))


@GW.route("/api/sessions", methods=("GET", "HEAD"))
def _gw_get_sessions(req: "web_gateway.Request") -> Any:
    """`GET /api/sessions` —— 群列表。精确登记管这一条、下面的前缀登记管带 <sid> 的，
    与迁移前 `path == "/api/sessions"` / 正则带 id 的分工一一对上。"""
    svc = req.svc
    visitor = svc._resolve_visitor()
    if visitor is None:
        return _gw_done(req, _gw_unauthorized())
    return _gw_done(req, (200, svc._sessions(visitor, req.query_dict)))


@GW.route("/api/sessions", methods=("GET", "HEAD"), prefix=True)
def _gw_get_sessions_sub(req: "web_gateway.Request") -> Any:
    """`/api/sessions/<sid>/...` 下的六条只读端点，**正则与顺序原样搬过来**。

    形状对不上的落 `_not_implemented()` → 501（**不是 404**）。这里**直接调**而不是
    转 `_gw_not_found()`：转过去会再跑一遍 `_resolve_visitor()`（多一次 cookie 校验），
    而到这一行 401 门槛已经过了，兜底那支只剩 501 一种结果 —— 结果相同、少一次校验。
    """
    svc, path = req.svc, req.path
    visitor = svc._resolve_visitor()
    if visitor is None:
        return _gw_done(req, _gw_unauthorized())

    m = re.match(r"^/api/sessions/([A-Za-z0-9_\-]{1,64})$", path)
    if m:
        return _gw_done(req, svc._session_detail(visitor, m.group(1)))

    m = re.match(r"^/api/sessions/([A-Za-z0-9_\-]{1,64})/messages$", path)
    if m:
        return _gw_done(req, svc._messages(visitor, m.group(1), req.query_dict))

    m = re.match(r"^/api/sessions/([A-Za-z0-9_\-]{1,64})/memo$", path)
    if m:
        return _gw_done(req, svc._memo_get(visitor, m.group(1)))

    # #478：意图分类台账（只读）—— 「为什么这条没入队」可回查
    m = re.match(r"^/api/sessions/([A-Za-z0-9_\-]{1,64})/todos/intents$", path)
    if m:
        return _gw_done(req, svc._todos_intents(visitor, m.group(1), req.query_dict))

    # 待办清单（群 #108~#114 冻结）：GET ?owner=&status=&sort=
    m = re.match(r"^/api/sessions/([A-Za-z0-9_\-]{1,64})/todos$", path)
    if m:
        return _gw_done(req, svc._todos(visitor, m.group(1), req.query_dict))

    m = re.match(r"^/api/sessions/([A-Za-z0-9_\-]{1,64})/members$", path)
    if m:
        return _gw_done(req, svc._members(visitor, m.group(1), req.query_dict))

    return _gw_done(req, svc._not_implemented(req.method, path))


# 必须在全部 @GW.route 之后取：`handler_class()` 会按「已登记的方法」删掉多余的
# do_<METHOD>。刀8b 之后本件 GET/POST/PUT/PATCH/DELETE/HEAD 六个方法都有登记，
# 于是 L1 只会删掉 do_OPTIONS —— OPTIONS 照旧落基类的 501，与迁移前逐字一致。
# （do_GET / do_HEAD 虽然留着，仍被下面的 ChatHandler 覆盖成本件自己的入口。）
_GatewayHandler = GW.handler_class()


class ChatHandler(_GatewayHandler):
    """只读阶段的全部路由。写端点一律 501（契约 §12.4）。"""

    server_version = "agent-team-chat/" + VERSION
    protocol_version = "HTTP/1.1"

    #: #478：本次请求里最后一次意图判定结果（给 POST /messages 响应回带，便于核对
    #: 「这条为什么没入队」）。Handler 每请求一个新实例，所以只是请求级暂存。
    _todo_last_intent = None

    # --- 基础 ------------------------------------------------------
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        LOG.info("%s - %s", self._peer_ip(), fmt % args)

    #: 本次请求是否已经写出响应（登记的处理函数一律推式出口）。
    #: keep-alive 上一个实例要服务多个请求，所以每次进 `_dispatch_gw()` 都重置。
    _sent = False

    # **六个方法的路由分派全在 L1**（模块顶部的 @GW.route 登记表）：非 GET 是阶段2
    # 刀8a 收的，GET / HEAD 的整张表是刀8b 收的。下面六个方法只把入口接上去，本件
    # 再没有「自己那串 if 选处理函数」的代码。
    def do_GET(self) -> None:  # noqa: N802
        self._dispatch_gw()

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch_gw()

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch_gw()

    def do_PATCH(self) -> None:  # noqa: N802
        self._dispatch_gw()

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch_gw()

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch_gw()

    def _dispatch_gw(self) -> None:
        """**全部六个方法的统一入口**：路由分派交给 L1（模块顶部的 @GW.route 登记表）。

        与迁移前的 `_dispatch()` 逐行同构，只把「一串 if 选处理函数」换成
        `GW.dispatch()`；计时、三层兜底 except、`_request_ms` 全照旧。`_drain_body()`
        挪进了 `@GW.pre`，那里同样在本 try 之内，抛错照旧落这里的 500。
        兜底 except 里的日志用 `self.command` 取方法名 —— 与迁移前 `_dispatch()`
        用形参 `method` 取到的是同一个值（形参本来就来自 do_<METHOD>）。
        """
        started = time.time()
        self._sent = False
        try:
            parsed = urllib.parse.urlsplit(self.path)
            req = web_gateway.Request(self)
            req.svc = self
            # 路径与查询一律沿用**本件自己的**归一化：`urlsplit()` 会剥掉 #fragment，
            # L1 的 `partition("?")` 不会 —— 不覆盖的话 `/api/x#y` 的归一结果就变了。
            req.path = parsed.path.rstrip("/") or "/"
            req.query_dict = urllib.parse.parse_qs(parsed.query)
            status, body, headers = GW.dispatch(req)
            if not self._sent:            # 兜底：理论上走不到（每条路径都写过）
                self._reply(status, body, headers)
        except RequestError as exc:
            self._send_json(exc.status, {
                "ok": False,
                "error": dict({"code": exc.code, "message": exc.message}, **exc.extra),
            })
        except DBUnavailable as exc:
            LOG.warning("DB 不可用：%s", scrubbed(exc))
            self._send_json(503, {"ok": False, "error": {
                "code": "upstream_unavailable", "message": "数据库暂不可用"}})
        except Exception as exc:                    # 兜底：绝不把栈吐给前端
            LOG.exception("未处理异常 %s %s", self.command, self.path)
            self._send_json(500, {"ok": False, "error": {
                "code": "internal", "message": "服务端异常：%s" % scrubbed(exc)}})
        finally:
            self._request_ms = int((time.time() - started) * 1000)

    # --- 路由 ------------------------------------------------------
    #
    # 迁移前这里有 `_dispatch()`（GET 入口）与 `_handle()`（方法无关的共享路由表）。
    # 刀8b 把整张表拆成模块顶部的 @GW.route 登记后，两个方法都没有调用方了，随之删掉。
    # 表里每一条的去向、顺序怎么保的、鉴权为什么不做成钩子，都写在那份登记表的注释里。
    # 以下留下的全是**叶子方法**（真正干活的那些），一个字没动。

    # --- 临时群（契约 §I）------------------------------------------

    def _require_visitor(self) -> Visitor:
        visitor = self._resolve_visitor()
        if visitor is None:
            raise RequestError(401, "unauthorized", "需要登录")
        return visitor

    def _speaker(self, visitor):
        # type: (Visitor) -> Tuple[Optional[str], Optional[str]]
        """这次发言以谁的名义：(author_type, author_id)。

        Agent 走 tailnet 直连 + `X-Agent-Id`；人走会话 cookie（cookie 优先，
        所以<USER_DISPLAY>在 tailnet 内也是 <USER_ID> 而非匿名 owner）。
        `X-Agent-Id` **只在 tailnet 来源被接受**，防冒名。

        tailnet 内没有会话 cookie 时（= 与看板一样的"内网免登录"），按
        `DEFAULT_HUMAN_ID` 认作这位人 —— 否则"人"这一侧不成立，网页上进来的
        人既建不了群也发不了言，群协议等于对发起人关门。
        """
        agent_id = (self.headers.get("X-Agent-Id") or "").strip()
        if agent_id and visitor.source == "tailnet":
            return "agent", agent_id
        if visitor.source == "session" and visitor.username:
            return "human", visitor.username
        if visitor.source == "tailnet":
            return "human", DEFAULT_HUMAN_ID
        return None, None

    def _create_session(self, visitor: Visitor, body: Dict[str, Any]
                        ) -> Tuple[int, Dict[str, Any]]:
        """建群（契约 §I.4）：由**人**发起，发起人自动进成员表。"""
        author_type, author_id = self._speaker(visitor)
        if author_type != "human":
            raise RequestError(403, "creator_required", "建群必须由人发起（请先登录）")

        kind = str(body.get("kind") or "meet").strip()
        if kind not in ("dm", "meet"):
            raise RequestError(422, "bad_kind", "kind 只能是 dm / meet")

        roster_ids = set(Runtime.roster.ids()) if Runtime.roster else set()
        wanted = body.get("members")
        if wanted is None:
            wanted = []
        if not isinstance(wanted, list):
            raise RequestError(422, "bad_members", "members 必须是数组")

        members = [author_id]                      # 发起人必在群里（规则 1）
        for raw in wanted:
            mid = str(raw or "").strip()
            if not mid or mid in members:
                continue
            if mid not in roster_ids:
                raise RequestError(422, "unknown_member", "成员不存在：%s" % mid)
            members.append(mid)

        title = str(body.get("title") or "").strip()[:120] or (
            "临时群 · %s" % datetime.datetime.now(BJ).strftime("%m月%d日 %H:%M"))
        session_id = "meet_%s_%s" % (
            datetime.datetime.now(BJ).strftime("%Y%m%d"), secrets.token_hex(3))
        created = Runtime.db.create_session(
            session_id, kind, title, members, author_id,
            sensitivity="internal", client_ref=body.get("client_ref"))
        if created is None:
            raise RequestError(500, "create_failed", "建群失败")
        return 201, {"ok": True, "session": created,
                     "server_time": iso_bj(datetime.datetime.now(UTC))}

    def _post_message(self, visitor: Visitor, session_id: str, body: Dict[str, Any]
                      ) -> Tuple[int, Dict[str, Any]]:
        """群内发言（契约 §I.3 门禁 + §I.1 六条规则）。"""
        session = Runtime.db.get_session(session_id, visitor.role)
        if session is None:
            raise RequestError(404, "not_found", "会话不存在")

        author_type, author_id = self._speaker(visitor)
        if author_type is None:
            raise RequestError(400, "agent_id_required",
                               "tailnet 免登录不能以某个成员名义发言：请带 X-Agent-Id 头")
        members = session.get("member_ids") or []
        if author_id not in members:
            raise RequestError(404, "not_found", "会话不存在")   # 不泄露存在性
        if session.get("state") != "open":
            raise RequestError(409, "session_closed", "群已归档，不能再发言（规则 4：由发起人宣布结束）")

        text = str(body.get("text") or "").strip()
        if not text:
            raise RequestError(422, "empty_text", "text 不能为空")
        if len(text) > TEXT_MAX:
            raise RequestError(422, "text_too_long", "text 超过 %d 字" % TEXT_MAX)

        mentions = body.get("mentions")
        if mentions is None:
            # 规则 6：Agent 必须带 @；规则 2：人不带 @ 默认全员
            mentions = [] if author_type == "agent" else ["@all"]
        if not isinstance(mentions, list):
            raise RequestError(422, "bad_mentions", "mentions 必须是数组")
        mentions = [str(m or "").strip() for m in mentions if str(m or "").strip()]

        # 正文里手打的 @某人 也是点名：与手点 @ 栏补进**同一个** mentions 字段，
        # 在写入路径上一次性合成（不是只改正文、也不是新开字段）。只补群内成员，
        # 同一关键词只补一次，正文原文不改写。
        for target in parse_text_mentions(text, members):
            if target not in mentions:
                mentions.append(target)

        if not mentions:
            if author_type == "agent":
                raise RequestError(422, "mention_required",
                                   "Agent 发言必须 @ 至少一个目标（@所有人 写 \"@all\"）")
            mentions = ["@all"]
        for target in mentions:
            if target == "@all":
                continue
            if target not in members:
                raise RequestError(422, "unknown_mention", "不在群里的目标：%s" % target)

        # 规则 5：**可见性恒定是全群**（audience 只是"这条点名了谁"的账）。
        audience = list(members) if "@all" in mentions else [
            m for m in members if m in mentions]
        # 需要处理并回应的 = 被点名的，去掉自己（自己不用回自己）
        needs_response = [m for m in audience if m != author_id]

        # #448：agent 若在回复里带了队列声明（body.todo），**先校验再落消息** ——
        # 校验不过就整条不发（422），不会出现「消息进了群、队列没翻」或反过来的两笔账。
        decl = body.get("todo")
        decl_plan = None
        if decl:
            if author_type != "agent":
                raise RequestError(403, "todo_agent_only",
                                   "todo 声明只能由 agent 带（X-Agent-Id）一起发")
            decl_plan = self._todo_declare_plan(session_id, author_id, decl)

        message = Runtime.db.add_message(
            session_id, author_type, author_id, text, mentions, audience,
            needs_response, reply_to=body.get("reply_to"),
            client_msg_id=body.get("client_msg_id"))
        if message is None:
            raise RequestError(500, "write_failed", "发言落库失败")

        # ---- #448：发言与任务队列（todos）接上（两个方向都在这条写入路径上合成）----
        # 1) 人 @agent 派活 → 自动入队（待办）；
        # 2) agent 声明 → 同一次调用里翻「进行中 / 已完成」，群里说的和队列里的不留两笔账。
        queued = self._todo_autotrack_task(session, session_id, author_id,
                                           mentions, message)
        decl_item = self._todo_declare_apply(decl_plan) if decl_plan else None
        # #448-2：agent 回群说「已完成 / 进行中」→ 同一次调用里把队列状态翻掉
        tracked = self._todo_reply_autotrack(session_id, author_id, text, message,
                                             decl_plan)
        return 201, {
            "ok": True,
            "message": message,
            "queued": self._todo_out(queued) if queued else None,
            "todo": self._todo_out(decl_item) if decl_item else None,
            "tracked": [self._todo_out(item) for item in tracked],
            # #478：这条人的发言被判成 task 还是 followup（followup → 没入队），
            # 带 reason/hits 便于当场核对「为什么这条没入队」。
            "intent": getattr(self, "_todo_last_intent", None),
            "server_time": iso_bj(datetime.datetime.now(UTC)),
        }

    def _meta_title(self, value: Any) -> str:
        """群名入参（#90）：非空字符串、去首尾空白、上限 SESSION_TITLE_MAX 字。"""
        if not isinstance(value, str):
            raise RequestError(422, "bad_title", "title 必须是字符串")
        text = value.strip()
        if not text:
            raise RequestError(422, "bad_title", "title 不能为空（群名至少 1 个字）")
        if len(text) > SESSION_TITLE_MAX:
            raise RequestError(
                422, "title_too_long",
                "title 超过上限 %d 字（收到 %d 字），未写入" % (SESSION_TITLE_MAX, len(text)),
                extra={"field": "title", "max": SESSION_TITLE_MAX, "len": len(text)})
        return text

    def _meta_note(self, value: Any) -> str:
        """备注入参（#90）：字符串（空串 = 清空备注）、上限 SESSION_NOTE_MAX 字。"""
        if not isinstance(value, str):
            raise RequestError(422, "bad_note", "note 必须是字符串")
        text = value.strip()
        if len(text) > SESSION_NOTE_MAX:
            raise RequestError(
                422, "note_too_long",
                "note 超过上限 %d 字（收到 %d 字），未写入" % (SESSION_NOTE_MAX, len(text)),
                extra={"field": "note", "max": SESSION_NOTE_MAX, "len": len(text)})
        return text

    def _patch_session(self, visitor: Visitor, session_id: str, body: Dict[str, Any]
                       ) -> Tuple[int, Dict[str, Any]]:
        """PATCH /api/sessions/{sid} —— #90：改群名（title）/ 备注（note）。

        口径（**原有 state=archived 语义一字未动**，见下面 has_state 分支）：
          * title/note 走与 memo **同一条权限口径**（`_memo_actor`）：必须能落款到本群成员
            （agent 带 `X-Agent-Id`；人在 tailnet 内按 DEFAULT_HUMAN_ID/会话身份），
            非本群成员 403 `not_a_member`。
          * 类型/长度不合规一律 4xx + 明确错误码（`bad_title` / `title_too_long` /
            `bad_note` / `note_too_long`，超限额外带 max/len），**不静默截断**。
          * 每个真改动的字段落一条 `chat_session_meta_events` 留痕（同 memo events 手法）。
          * state=archived 仍是「只有发起人能宣布结束」→ 403 `only_owner_can_close`。
        """
        session = Runtime.db.get_session(session_id, visitor.role)
        if session is None or not self._can_read_session(visitor, session):
            # 与其它写口同口径：无权即不可见（404，不泄露存在性）
            raise RequestError(404, "not_found", "会话不存在")

        has_state = "state" in body
        fields = {}  # type: Dict[str, str]
        if "title" in body:
            fields["title"] = self._meta_title(body.get("title"))
        if "note" in body:
            fields["note"] = self._meta_note(body.get("note"))
        if not has_state and not fields:
            raise RequestError(422, "empty_patch",
                               "PATCH 至少要带一个要改的字段（title / note / state）")

        new_state = None  # type: Optional[str]
        if has_state:
            new_state = str(body.get("state") or "").strip()
            if new_state != "archived":
                raise RequestError(422, "bad_state",
                                   "本版只支持 state=archived（关群 = 归档）")
            if not (visitor.source == "session" and visitor.username
                    and visitor.username == session.get("created_by")):
                raise RequestError(403, "only_owner_can_close",
                                   "只有群的发起人能宣布结束")

        actor = self._memo_actor(visitor, session) if fields else ""
        updated = Runtime.db.set_session_meta(session_id, fields, actor, state=new_state)
        if updated is None:
            raise RequestError(500, "write_failed", "改群名 / 备注失败")
        changed = updated.pop("changed", [])
        return 200, {"ok": True, "session": updated, "changed": changed,
                     "server_time": iso_bj(datetime.datetime.now(UTC))}

    # --- 成员变更 / 群记忆锚 ---------------------------------------

    def _memo_actor(self, visitor: Visitor, session: Dict[str, Any]) -> str:
        """写路径的落款人：必须是**本群成员**（agent 走 X-Agent-Id，人走会话身份）。

        与 _post_message 同口径：不在群里的人既不能发言，也不能改成员表和群记忆。
        """
        author_type, author_id = self._speaker(visitor)
        if author_type is None or not author_id:
            raise RequestError(400, "agent_id_required",
                               "tailnet 免登录不能以某个成员名义写入：请带 X-Agent-Id 头")
        if author_id not in (session.get("member_ids") or []):
            raise RequestError(403, "not_a_member", "只有本群成员能改本群成员表 / 群记忆")
        return str(author_id)

    def _members_write(self, visitor: Visitor, session_id: str, method: str,
                       body: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        """加人（POST）/ 踢人（DELETE）：POST / DELETE /api/sessions/{sid}/members。

        契约 §I.5：请求体 `{"members":["a","b"]}`（或 `{"member":"a"}` 给单个），
        响应一律返**成员全量**，前端直接整体替换。踢人口径（#48 定稿）：
        作废该成员名下全部待回应账 + 落一条「成员变更」系统消息留痕 + 不可恢复。
        """
        session = Runtime.db.get_session(session_id, visitor.role)
        if session is None or not self._can_read_session(visitor, session):
            raise RequestError(404, "not_found", "会话不存在")
        actor = self._memo_actor(visitor, session)

        raw = body.get("members")
        if raw is None:
            raw = body.get("member")
        if raw is None:
            raise RequestError(422, "bad_members", "members 必须是数组（或给 member 单个 id）")
        ids = [raw] if isinstance(raw, str) else raw
        if not isinstance(ids, list):
            raise RequestError(422, "bad_members", "members 必须是数组")
        ids = [str(item or "").strip() for item in ids]
        ids = [item for item in ids if item]
        if not ids:
            raise RequestError(422, "bad_members", "members 不能为空")

        if method == "POST":
            roster = Runtime.roster
            known = set(roster.ids()) if roster else set()
            for mid in ids:
                if mid not in known:
                    raise RequestError(422, "unknown_member", "成员不存在：%s" % mid)
            outcome = Runtime.db.members_write(session_id, ids, [], actor)
            verb, moved = "加入", outcome["added"]
        else:
            # 踢人是破坏性动作：只放行群发起人或有管理权的人（#48 未写死，先按这条）。
            is_creator = bool(visitor.username) and visitor.username == session.get("created_by")
            if not (is_creator or visitor.caps().get("can_admin")):
                raise RequestError(403, "only_owner_can_kick",
                                   "只有群发起人（或有管理权的人）能踢人")
            outcome = Runtime.db.members_write(session_id, [], ids, actor)
            verb, moved = "移出", outcome["removed"]

        updated = outcome["session"]
        if moved:
            # 留痕：一条系统消息，不可恢复、删不掉（群协议：成员变更必须可追溯）
            detail = "、".join(moved)
            if method == "DELETE":
                extra = "；其名下 %d 条待回应已作废（不可恢复）" % outcome["voided"]
            else:
                extra = ""
            Runtime.db.add_message(
                session_id, "system", "system",
                "【成员变更】%s 被 %s %s%s" % (detail, actor, verb, extra),
                [], list(updated.get("member_ids") or []), [])
        return 200, {
            "ok": True,
            "session_id": session_id,
            "members": updated.get("members") or [],
            "added": outcome["added"],
            "removed": outcome["removed"],
            "voided": outcome["voided"],
            "server_time": iso_bj(datetime.datetime.now(UTC)),
        }

    def _memo_get(self, visitor: Visitor, session_id: str) -> Tuple[int, Dict[str, Any]]:
        """GET /api/sessions/{sid}/memo —— 清空上下文/重启后的第一件事就是拉这里。"""
        session = Runtime.db.get_session(session_id, visitor.role)
        if session is None or not self._can_read_session(visitor, session):
            raise RequestError(404, "not_found", "会话不存在")
        state = Runtime.db.memo_get(session_id)
        return 200, {
            "ok": True,
            "session_id": session_id,
            "memo": state["memo"],
            "version": state["version"],
            "etag": state["etag"],
            "updated_at": state["updated_at"],
            "updated_by": state["updated_by"],
            "server_time": iso_bj(datetime.datetime.now(UTC)),
        }

    def _memo_doc(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """把请求体收敛成记忆文档：只认 5 个字段，多一个都不收（不许塞日志）。"""
        doc = {}  # type: Dict[str, Any]
        for key in MEMO_STR_KEYS:
            value = body.get(key)
            doc[key] = str(value or "").strip()[:4000]
        for key in MEMO_LIST_KEYS:
            value = body.get(key)
            if value is None:
                doc[key] = []
                continue
            if not isinstance(value, list):
                raise RequestError(422, "bad_memo", "%s 必须是数组" % key)
            doc[key] = value
        unknown = sorted(set(body.keys()) - set(MEMO_STR_KEYS) - set(MEMO_LIST_KEYS))
        if unknown:
            raise RequestError(422, "bad_memo", "未知字段：%s" % "、".join(unknown))
        # open_tasks 每条必须带 blocked_by[]（字符串数组，空数组 = 没被外部卡）。
        # 恢复后先扫 blocked_by 再扫 owner，一眼分清「卡在外部」还是「卡在自己」。
        tasks = doc["open_tasks"]
        for idx, item in enumerate(tasks):
            if not isinstance(item, dict):
                raise RequestError(422, "bad_memo", "open_tasks[%d] 必须是对象" % idx)
            blocked = item.get("blocked_by")
            if blocked is None:
                item["blocked_by"] = []
            elif not isinstance(blocked, list):
                raise RequestError(422, "bad_memo", "open_tasks[%d].blocked_by 必须是数组" % idx)
            else:
                item["blocked_by"] = [str(x or "").strip() for x in blocked if str(x or "").strip()]
        raw = json.dumps(doc, ensure_ascii=False).encode("utf-8")
        if len(raw) > MEMO_MAX_BYTES:
            raise RequestError(413, "memo_too_large",
                               "记忆超过 %d KB，请只留要点、明细留原消息" % (MEMO_MAX_BYTES // 1024))
        return doc

    def _memo_merge(self, current: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
        """PATCH = 只追加：数组字段追加，标量字段（goal/resume）覆盖。"""
        if not isinstance(patch, dict) or not patch:
            raise RequestError(422, "empty_patch", "PATCH 至少要带一个要改的字段")
        doc = {}  # type: Dict[str, Any]
        for key in MEMO_STR_KEYS:
            doc[key] = current.get(key) or ""
        for key in MEMO_LIST_KEYS:
            value = current.get(key)
            doc[key] = list(value) if isinstance(value, list) else []
        for key, value in patch.items():
            if key in MEMO_LIST_KEYS:
                if not isinstance(value, list):
                    raise RequestError(422, "bad_memo", "%s 必须是数组" % key)
                doc[key] = doc[key] + list(value)
            elif key in MEMO_STR_KEYS:
                doc[key] = str(value or "").strip()[:4000]
            else:
                raise RequestError(422, "bad_memo",
                                   "PATCH 只追加 %s / 覆盖 %s，收到：%s"
                                   % ("、".join(MEMO_LIST_KEYS), "、".join(MEMO_STR_KEYS), key))
        return self._memo_doc(doc)

    def _memo_write(self, visitor: Visitor, session_id: str, method: str,
                    body: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        """PUT 整份初始化 / PATCH 只追加；两者都受乐观锁管（If-Match）。"""
        session = Runtime.db.get_session(session_id, visitor.role)
        if session is None or not self._can_read_session(visitor, session):
            raise RequestError(404, "not_found", "会话不存在")
        actor = self._memo_actor(visitor, session)

        if method == "PUT":
            doc, action = self._memo_doc(body), "put"
        else:
            patch = body.get("patch") if isinstance(body.get("patch"), dict) else body
            doc = self._memo_merge(Runtime.db.memo_get(session_id)["memo"], patch)
            action = "patch"

        ok, state = Runtime.db.memo_write(
            session_id, doc, actor, self.headers.get("If-Match"), action)
        if not ok:
            # 409 + 当前 etag：前端据这句提示「有人刚改过，已刷新」。
            raise RequestError(
                409, "memo_conflict",
                "本群记忆已被他人更新，已为你刷新到最新版本，请确认后重试",
                extra={"etag": state["etag"], "version": state["version"],
                       "updated_by": state["updated_by"], "memo": state["memo"]})
        return 200, {
            "ok": True,
            "session_id": session_id,
            "memo": state["memo"],
            "version": state["version"],
            "etag": state["etag"],
            "updated_at": state["updated_at"],
            "updated_by": state["updated_by"],
            "server_time": iso_bj(datetime.datetime.now(UTC)),
        }

    # --- 待办台账 Todo（群 #108~#114 冻结口径）---------------------

    def _todo_write_guard(self, session: Dict[str, Any]) -> None:
        """群关闭 = 只读归档（#112/#124）：closed 后 todos 仍可 GET，写口全关。"""
        state = str(session.get("state") or "open")
        if state != "open":
            raise RequestError(409, "read_only_archived",
                               "群已归档（state=%s）：待办只读，写口全关" % state)

    def _todo_out(self, item: Dict[str, Any]) -> Dict[str, Any]:
        """对外形状：字段口径按 #109（items[]），不新造字段。"""
        return {
            "id": item["id"],
            "text": item["text"],
            "owner": item["owner"],
            "owner_display": display_of(item["owner"]),
            "kind": item["kind"],
            "status": item["status"],
            "deadline": item["deadline"],
            "created_at": item["created_at"],
            "created_by": item["created_by"],
            "cleared_at": item["cleared_at"],
            "cleared_by": item["cleared_by"],
            "evidences": item["evidences"],
            "landing": item["landing"],
            "intent": item.get("intent") or "task",
            "followup_count": int(item.get("followup_count") or 0),
            # #534：列表只显示这个名字；老条目列里是空串 → 现抽兜底（不写库）
            # #541：新条目这个名先落**规则兜底名**，异步 worker 用模型名覆盖
            "task_name": item.get("task_name") or todo_task_name(item.get("text")),
            # #541：模型给的详情 + 富化状态（pending 待回 / done 已回 / fallback 未回 / skipped）
            "task_summary": item.get("task_summary") or "",
            "enrich_state": item.get("enrich_state") or "",
        }

    def _todos(self, visitor: Visitor, session_id: str,
               query: Dict[str, List[str]]) -> Tuple[int, Dict[str, Any]]:
        """GET /api/sessions/{sid}/todos?owner=&status=&sort= —— 待办清单（只读）。

        默认列法（#109/#110/#113 冻结）：**每个成员都列，0 条也回空组**（前端显「无待办」，
        一眼看出谁没报进度）。排序后端做好，前端直接渲染（sort=time 组按最新条目倒序、
        组内按时间升序；sort=owner 组按 owner 名升序）。
        """
        def first(name: str) -> str:
            values = query.get(name) or []
            return (values[0] or "").strip() if values else ""

        session = Runtime.db.get_session(session_id, visitor.role)
        if session is None or not self._can_read_session(visitor, session):
            raise RequestError(404, "not_found", "会话不存在")

        owner = first("owner")
        status = first("status")
        if status and status not in TODO_STATUS:
            raise RequestError(422, "bad_status", "status 只能是 %s" % "、".join(TODO_STATUS))
        sort = first("sort") or "time"
        if sort not in TODO_SORTS:
            raise RequestError(422, "bad_sort", "sort 只能是 %s" % "、".join(TODO_SORTS))

        items = [self._todo_out(row)
                 for row in Runtime.db.todos_list(session_id, owner, status)]
        members = [m for m in (session.get("member_ids") or []) if m]
        groups = []  # type: List[Dict[str, Any]]
        for member_id in members:
            own = [it for it in items if it["owner"] == member_id]
            groups.append({"owner": member_id, "display": display_of(member_id),
                           "count": len(own), "items": own})
        if owner:
            groups = [g for g in groups if g["owner"] == owner]
            if not groups:            # 非成员 / 名册外的人：照样回一条空组，不当 404
                groups = [{"owner": owner, "display": display_of(owner),
                           "count": 0, "items": []}]
        if sort == "owner":
            groups.sort(key=lambda g: g["owner"])
        else:
            filled = [g for g in groups if g["items"]]
            empty = [g for g in groups if not g["items"]]
            filled.sort(key=lambda g: g["items"][-1]["created_at"] or "", reverse=True)
            groups = filled + empty
        return 200, {
            "ok": True,
            "session_id": session_id,
            "state": session.get("state") or "open",
            "owner": owner,
            "status": status,
            "sort": sort,
            "count": len(items),
            "groups": groups,
            "items": items,
            "server_time": iso_bj(datetime.datetime.now(UTC)),
        }

    # --- #448：群聊下发任务 → 队列（todos）跟踪钩子 -----------------
    def _todo_followup_target(self, session_id: str,
                              owner: str) -> Optional[Dict[str, Any]]:
        """这条追问该记到哪个已有条目上：**同一个 owner** 最近一条未完成条目；
        都没有未完成的就退回他最近一条（任何状态）。**绝不新建条目**。"""
        rows = Runtime.db.todos_list(session_id, owner=owner)
        if not rows:
            return None
        for status in ("open", "doing"):
            cand = [r for r in rows if r.get("status") == status]
            if cand:
                return cand[-1]
        return rows[-1]

    def _todo_record_intent_skip(self, session_id: str, owner: str,
                                 author_id: str, message: Optional[Dict[str, Any]],
                                 text: str, cls: Dict[str, Any],
                                 count_followup: bool = True
                                 ) -> Dict[str, Any]:
        """#478：判定为进度追问 → 落跳过台账 + 给已有条目累加追问次数。

        `count_followup=False` 是给**回填**用的：历史消息早已说完，把它们累计到
        「当前在办的那一条」上是错误归因（实测：一次回填把某条刷到 13 次追问），
        所以回填只留台账、不动计数。

        幂等：台账靠 `(session_id, dedupe_key)` 唯一约束去重；**只有本次真新增了
        台账行**才给条目 +1，所以同一句话重发不会把 followup_count 刷大。

        另一个必须挡的情况（回填实测踩到）：这条正文**本来就已经在队列里**（历史条目
        早于本次改动入队，或同一句话先被当成任务入队过）—— 那就不能记「没入队」，
        否则台账自相矛盾（说没入队、队列里却躺着一条）。这种情况直接返回、不写台账。
        """
        out = {"intent": cls.get("intent"), "reason": cls.get("reason"),
               "hits": cls.get("hits") or [], "owner": owner,
               "counted": False, "todo_id": None, "followup_count": None,
               "duplicate": False, "already_queued": False}
        dkey = todo_message_key(text)
        if dkey:
            for row in Runtime.db.todos_list(session_id, owner=owner):
                if todo_dedupe_key(row.get("text")) == dkey:
                    out["already_queued"] = True
                    out["todo_id"] = row.get("id")
                    out["reason"] = "already_queued"
                    LOG.info("队列意图判定 skip 但**队列里已有同正文条目**，不记跳过台账"
                             " session=%s owner=%s seq=%s todo=%s",
                             session_id, owner, (message or {}).get("seq"),
                             row.get("id"))
                    return out
        key = Runtime.db.todo_skip_create(session_id, message, author_id, owner,
                                          cls, text)
        out["duplicate"] = key is None
        if key is None:
            return out                      # 同一句话已记过：幂等跳过，不重复计数
        if not count_followup:
            return out                      # 回填：只留台账，不把历史追问记到当前条目上
        target = self._todo_followup_target(session_id, owner)
        if target is not None:
            bumped = Runtime.db.todo_bump_followup(target["id"])
            if bumped:
                out["todo_id"] = bumped["id"]
                out["followup_count"] = bumped.get("followup_count")
                out["counted"] = True
        LOG.info("队列意图=followup 不入队 session=%s owner=%s seq=%s reason=%s hits=%s"
                 " → 记到 %s followup_count=%s",
                 session_id, owner, (message or {}).get("seq"), cls.get("reason"),
                 ",".join(cls.get("hits") or []), out["todo_id"],
                 out["followup_count"])
        return out

    def _todo_autotrack_task(self, session: Dict[str, Any], session_id: str,
                             author_id: str, mentions: Sequence[str],
                             message: Optional[Dict[str, Any]]
                             ) -> Optional[Dict[str, Any]]:
        """人在群里 @某个 agent 派活 → 自动入队一条**待办**（status=open）。

        规则（#448 原样保留 + #478 加一层意图闸门）：
          · 触发：作者**不是**名册里的 agent（名册外 = 人：<USER_ID>/默认人），且 mentions 里
            至少有一个名册里的 agent。agent 之间互相 @（协同/催办）不入队，免得刷成噪声。
          · **#478 闸门**：正文先过 `classify_human_intent` —— 只会为 `intent=task`
            （有动作指令、指向具体产出）建条目；判为 `followup`（进度追问/催问/纯提问）
            的**不入队**，改记 `chat_todo_intent_skips` 台账 + 给已有条目累加
            `followup_count`。判据是确定性规则，可解释（reason/hits 入台账）。
          · 落账：owner = 被点名的第一个 agent，kind=confirm，status=open（待办=还没开工），
            created_by = 下发人，正文 = `#<seq> <摘要>`（见 todo_text_from_message），
            intent = "task"。
          · 幂等：同群同 owner 已有**同正文且未完成**的条目 → 不重复入队
            （重发 / 前端重试 / 轮询都不会把队列撑大）。
          · **#541**：这里落的只是 `todo_task_name()` 的**规则兜底名**，立刻返回 201；
            条目随后进 `Runtime.enricher` 的单线程异步队列，由模型重判
            「是不是任务 + 抽象名 + 详情」。模型判不是任务 → 撤条目 + 记 skips 台账。
        队列是台账不是命门：这一步任何异常都只告警，**不阻断发言**。
        """
        try:
            agents = roster_agent_ids(Runtime.roster)
            if not agents or author_id in agents:
                return None                      # agent 之间互相 @：不入队
            targets = [m for m in (mentions or []) if m in agents]
            if not targets:
                # #539：人发了话、正文里还带着 @ 形态的 token，却没落到名册 agent ——
                # 这正是「点名没解析出来 → 钩子静默不触发」的形状（大小写不一致时最常发生）。
                # 只留一行日志，不写库：以前这里 100% 静默，出问题只能靠猜。
                if "@" in str((message or {}).get("text") or ""):
                    LOG.info("队列自动入队跳过（未点名到名册 agent）session=%s author=%s"
                             " seq=%s mentions=%s", session_id, author_id,
                             (message or {}).get("seq"), ",".join(mentions or []))
                return None                      # 没点名 agent：不是派活
            owner = targets[0]
            raw = str((message or {}).get("text") or "")
            # ---- #478：先判意图，只有新任务才继续走入队 ----
            verdict = classify_human_intent(raw)
            self._todo_last_intent = verdict      # 给本次响应回带（可核对）
            if verdict.get("intent") != "task":
                self._todo_last_intent = self._todo_record_intent_skip(
                    session_id, owner, author_id, message, raw, verdict)
                return None
            text = todo_text_from_message(message)
            if not text:
                return None
            # ---- #534：抽不出任务名 = 这条没有可当任务看的正文 → 不入队 ----
            # ---- #541：这里只剩**快速预筛**的价值（规则名兜底）；真正的「是不是任务」
            #      与「叫什么名」由入队后的异步 worker 调模型重判（见下方 submit）----
            name = todo_task_name(text)
            if not name:
                self._todo_last_intent = {
                    "intent": verdict.get("intent"), "reason": "no_task_name",
                    "hits": verdict.get("hits") or [], "owner": owner}
                return None
            # 幂等看**正文指纹**：同一句话重发/脚本重试都不会把队列撑大
            key = todo_dedupe_key(text)
            for row in Runtime.db.todos_list(session_id, owner=owner):
                if row.get("status") == "done":
                    continue
                if todo_dedupe_key(row.get("text")) == key:
                    self._todo_last_intent = {
                        "intent": "task", "reason": "duplicate",
                        "hits": verdict.get("hits") or [], "owner": owner,
                        "duplicate": True}
                    return None                  # 已在队列里且未完成 → 幂等跳过
            enricher = getattr(Runtime, "enricher", None)
            # --- #541：入队先落**规则兜底名**并立刻返回 201，模型调用一律异步 ---
            item = Runtime.db.todo_create(session_id, text, owner, "confirm",
                                          None, author_id or "", intent="task",
                                          task_name=name,
                                          enrich_state=("pending"
                                                        if (enricher is not None
                                                            and enricher.running)
                                                        else ""))
            LOG.info("队列自动入队 session=%s owner=%s seq=%s todo=%s intent=task"
                     " 规则名=%r hits=%s", session_id, owner,
                     (message or {}).get("seq"), (item or {}).get("id"), name,
                     ",".join(verdict.get("hits") or []))
            # #541：丢进异步富化队列（非阻塞；满了/没起 → 保留规则名，不报错给客户端）
            if not todo_enrich_submit(item, message) and item is not None:
                try:
                    Runtime.db.todo_set_enrichment(item["id"], None, None, "fallback")
                    item["enrich_state"] = "fallback"
                except Exception as exc:
                    LOG.warning("标记富化降级状态失败：%s", scrubbed(exc))
            return item
        except Exception as exc:
            LOG.warning("队列自动入队失败（不影响发言）：%s", scrubbed(exc))
            return None

    def _todo_reply_landing(self, text: str, author_id: str,
                            message: Optional[Dict[str, Any]], now: str
                            ) -> Tuple[List[str], List[Dict[str, str]]]:
        """自动翻 done 的落点：正文里抠到机器可验的就用它，抠不到用这条群消息本身。"""
        for cand in todo_landing_from_text(text):
            try:
                landing, evidences = normalize_landing([cand])
            except RequestError:
                continue
            if landing or evidences:
                return landing, evidences
        value = "msg: #%s %s 回群（%s）" % ((message or {}).get("seq") or "?", author_id, now)
        return [value], [{"type": TODO_MSG_EVIDENCE_TYPE, "value": value,
                          "by": author_id, "validated_at": now}]

    def _todo_reply_autotrack(self, session_id: str, author_id: str, text: str,
                              message: Optional[Dict[str, Any]],
                              decl: Optional[Dict[str, Any]] = None
                              ) -> List[Dict[str, Any]]:
        """#448-2：agent 回群报进度 → 把**自己的**在办条目翻「进行中 / 已完成」。

        规则（写在发言落库之后，与入队同一条写入路径上合成）：
          · 只有名册里的 agent（= 干活的人）触发；人发言只走入队方向，不翻状态。
          · 正文里认到「完成词」或「开工词」才动；**否定词优先**——
            「还没完成 / 未修好」不算完成，直接不翻。
          · 翻哪一条：正文写了 `#448`（对条目标题里的 `#<seq>`）或 `td_xxx` 就翻那几条；
            没写引用时，只有**在办仅一条**才敢翻（并发多条时宁可不翻，不替 agent 猜）。
          · 显式 `todo` 声明（body.todo）优先：已声明过的条目这里不再重复翻。
          · done 的落点：正文能抠到机器可验落点就用它；抠不到就用这条群消息本身
            （`msg: #<seq> … 回群`，可回查）——队列是台账，不许空手盖完成章。
          · 全程异常只告警，**绝不阻断发言**（队列不是命门）。
        """
        try:
            agents = roster_agent_ids(Runtime.roster)
            if not agents or author_id not in agents:
                return []
            body_text = str(text or "")
            low = body_text.lower()
            done = (any(w.lower() in low for w in TODO_REPLY_DONE_WORDS)
                    and not any(w.lower() in low for w in TODO_REPLY_DONE_NEG))
            doing = (not done) and any(w.lower() in low for w in TODO_REPLY_DOING_WORDS)
            if not (done or doing):
                return []
            pending = [row for row in Runtime.db.todos_list(session_id, owner=author_id)
                       if row.get("status") in ("open", "doing")]
            if not pending:
                return []
            ids = set(TODO_REF_ID_RE.findall(body_text))
            seqs = set(TODO_REF_SEQ_RE.findall(body_text))
            hits = []  # type: List[Dict[str, Any]]
            for row in pending:
                if str(row.get("id") or "") in ids:
                    hits.append(row)
                    continue
                head = re.match(r"^#\s?(\d{1,9})\b", str(row.get("text") or ""))
                if head and head.group(1) in seqs:
                    hits.append(row)
            if not hits and len(pending) == 1 and not seqs and not ids:
                # 在办只有一条、且正文**没写任何条目引用**（写了别的号就是点名别的活）才敢认
                hits = pending[:1]
            if decl:
                hits = [r for r in hits if str(r.get("id")) != str(decl.get("id"))]
            if not hits:
                LOG.info("队列回执未落到具体条目（在办 %d 条）author=%s seq=%s",
                         len(pending), author_id, (message or {}).get("seq"))
                return []
            now = iso_bj(datetime.datetime.now(UTC))
            landing, evidences = self._todo_reply_landing(body_text, author_id,
                                                          message, now)
            out = []  # type: List[Dict[str, Any]]
            for row in hits:
                if done:
                    item = Runtime.db.todo_mark_done(row["id"], author_id, evidences,
                                                     landing=landing)
                elif row.get("status") == "open":
                    item = Runtime.db.todo_mark_doing(row["id"], author_id)
                else:
                    item = None
                if item:
                    out.append(item)
            if out:
                LOG.info("队列回执翻状态 session=%s owner=%s seq=%s -> %s（%d 条）",
                         session_id, author_id, (message or {}).get("seq"),
                         "done" if done else "doing", len(out))
            return out
        except Exception as exc:
            LOG.warning("队列回执翻状态失败（不影响发言）：%s", scrubbed(exc))
            return []

    def _todo_declare_plan(self, session_id: str, actor: str, decl: Any
                           ) -> Optional[Dict[str, Any]]:
        """校验 agent 回复里带来的队列声明（#448）：body.todo = {id, status, landing}。

        **只校验、不落库**：调用点先校验再写消息，避免「消息发失败但队列已经翻了」
        或「消息已落库但接口返回 422」这种两笔账。口径与 `PATCH /api/todos/{id}` 一致
        （只能动 owner=自己的条目；翻 done 必须带机器可验落点，落点空照旧 422 no_landing）。
        """
        if not isinstance(decl, dict):
            raise RequestError(422, "bad_todo", "todo 需是对象 {id, status, landing}")
        unknown = sorted(set(decl.keys()) - {"id", "status", "landing"})
        if unknown:
            raise RequestError(422, "bad_todo", "todo 未知字段：%s" % "、".join(unknown))
        todo_id = str(decl.get("id") or "").strip()
        status = str(decl.get("status") or "").strip()
        if not todo_id:
            raise RequestError(422, "bad_todo", "todo 需给 id")
        if status not in TODO_STATUS:
            raise RequestError(422, "bad_status",
                               "status 只能是 %s" % "、".join(TODO_STATUS))
        item = Runtime.db.todo_get(todo_id)
        if item is None or item["session_id"] != session_id:
            raise RequestError(404, "not_found", "待办不存在")
        if item["owner"] != actor:
            raise RequestError(403, "not_your_field",
                               "只能改 owner=自己的条目（本条 owner=%s）" % item["owner"])
        landing, evidences = normalize_landing(decl.get("landing"))
        if status == "done":
            have_landing = bool(landing or evidences or item.get("landing")
                                or item.get("evidences"))
            if not have_landing:
                raise RequestError(422, "no_landing",
                                   "不能自盖完成章：status=done 需先交落点（landing），"
                                   "落点为空，条目继续挂原状态")
        return {"id": todo_id, "status": status, "actor": actor,
                "landing": landing, "evidences": evidences}

    def _todo_declare_apply(self, plan: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """按已校验的声明落库（`_todo_declare_plan` 之后调用，这里不再抛校验错）。"""
        todo_id = plan["id"]
        status = plan["status"]
        actor = plan["actor"]
        if status == "open":
            return Runtime.db.todo_reopen(todo_id)
        if status == "doing":
            return Runtime.db.todo_mark_doing(todo_id, actor)
        now = iso_bj(datetime.datetime.now(UTC))
        stamped = []  # type: List[Dict[str, str]]
        for entry in plan["evidences"]:
            checked = dict(entry)
            checked["by"] = actor
            checked["validated_at"] = now
            stamped.append(checked)
        return Runtime.db.todo_mark_done(todo_id, actor, stamped,
                                         landing=plan["landing"])

    def _todo_create(self, visitor: Visitor, session_id: str,
                     body: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        """POST /api/sessions/{sid}/todos —— 新增条目。

        责任田（#114 冻结）：**谁都能给自己加、不能替别人加** ——
        owner 缺省 = 自己；写成别人一律 403，不"顺手代填"。
        """
        session = Runtime.db.get_session(session_id, visitor.role)
        if session is None or not self._can_read_session(visitor, session):
            raise RequestError(404, "not_found", "会话不存在")
        actor = self._memo_actor(visitor, session)
        self._todo_write_guard(session)

        unknown = sorted(set(body.keys()) - {"text", "kind", "owner", "deadline", "landing"})
        if unknown:
            raise RequestError(422, "bad_todo", "未知字段：%s" % "、".join(unknown))
        text = str(body.get("text") or "").strip()
        if not text:
            raise RequestError(422, "bad_todo", "text 不能为空")
        if len(text) > TODO_TEXT_MAX:
            raise RequestError(422, "bad_todo", "text 超过 %d 字" % TODO_TEXT_MAX)
        kind = str(body.get("kind") or "confirm").strip() or "confirm"
        if kind not in TODO_KINDS:
            raise RequestError(422, "bad_todo", "kind 只能是 %s" % "、".join(TODO_KINDS))
        owner = str(body.get("owner") or "").strip() or actor
        if owner != actor:
            raise RequestError(403, "not_your_field",
                               "只能给自己加条目（owner=%s），不能替别人加" % actor)
        deadline = None  # type: Optional[datetime.datetime]
        if body.get("deadline"):
            deadline = parse_time(body.get("deadline"))
            if deadline is None:
                raise RequestError(422, "bad_deadline",
                                   "deadline 需 ISO8601（如 2026-09-23T18:00:00+08:00）")
        landing, _evidences = normalize_landing(body.get("landing"))
        item = Runtime.db.todo_create(session_id, text, owner, kind, deadline, actor,
                                      landing=landing)
        return 201, {
            "ok": True,
            "session_id": session_id,
            "item": self._todo_out(item),
            "count": len(Runtime.db.todos_list(session_id)),
            "server_time": iso_bj(datetime.datetime.now(UTC)),
        }

    def _todo_patch(self, visitor: Visitor, todo_id: str,
                    body: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        """PATCH /api/todos/{id} —— 单条状态变更。

        口径（#131 §2 收紧，本轮改动）：**agent 不能自盖完成章** ——
        `status=done` 只有在**带机器可验落点**时才被接受：落点取自同一体的
        `landing`（字符串数组，新字段）或 `evidence(s)`（老口径 {type,value}），
        也可以先 PUT 过落点再翻。**`status=done` 而落点为空 → 422 `no_landing`**，
        条目继续挂 open，后端绝不代翻。
        `status=open` 是撤回（重开），归 owner。责任田（#114）：只能改 owner=自己的条目。
        """
        item = Runtime.db.todo_get(todo_id)
        if item is None:
            raise RequestError(404, "not_found", "待办不存在")
        session = Runtime.db.get_session(item["session_id"], visitor.role)
        if session is None or not self._can_read_session(visitor, session):
            raise RequestError(404, "not_found", "待办不存在")   # 无权即不可见
        actor = self._memo_actor(visitor, session)
        self._todo_write_guard(session)
        if actor != item["owner"]:
            raise RequestError(403, "not_your_field",
                               "只能改 owner=自己的条目（本条 owner=%s）" % item["owner"])
        unknown = sorted(set(body.keys()) - {"status", "evidence", "evidences", "landing"})
        if unknown:
            raise RequestError(422, "bad_todo", "未知字段：%s" % "、".join(unknown))

        raw = body.get("landing")
        if raw is None:
            raw = body.get("evidences") if body.get("evidences") is not None \
                else body.get("evidence")
        landing, evidences = normalize_landing(raw)

        status = body.get("status")
        if status is not None:
            status = str(status).strip()
            if status not in TODO_STATUS:
                raise RequestError(422, "bad_status", "status 只能是 %s" % "、".join(TODO_STATUS))
            if status == "open":
                updated = Runtime.db.todo_reopen(todo_id)
                return 200, {
                    "ok": True, "session_id": item["session_id"],
                    "item": self._todo_out(updated), "validated": False,
                    "server_time": iso_bj(datetime.datetime.now(UTC)),
                }
            if status == "doing":
                # #448：开工（待办 -> 进行中）。只认领、不算完，**不要求落点**
                # （落点是完成章的门槛，见下面 status=done 分支）。
                updated = Runtime.db.todo_mark_doing(todo_id, actor)
                return 200, {
                    "ok": True, "session_id": item["session_id"],
                    "item": self._todo_out(updated), "validated": False,
                    "server_time": iso_bj(datetime.datetime.now(UTC)),
                }
            # status=done：落点必须已在本次提交里，或此前已经落过
            have_landing = bool(landing or evidences or item.get("landing") or item.get("evidences"))
            if not have_landing:
                raise RequestError(422, "no_landing",
                                   "不能自盖完成章：status=done 需先交落点（landing）——"
                                   "commit 哈希 / 字面 curl+HTTP 码 / 文件路径+时间戳 / 重启 PID+时间戳；"
                                   "落点为空，条目继续挂 open")
            now = iso_bj(datetime.datetime.now(UTC))
            stamped = []  # type: List[Dict[str, str]]
            for entry in evidences:
                checked = dict(entry)
                checked["by"] = actor
                checked["validated_at"] = now
                stamped.append(checked)
            updated = Runtime.db.todo_mark_done(todo_id, actor, stamped, landing=landing)
            return 200, {
                "ok": True, "session_id": item["session_id"],
                "item": self._todo_out(updated), "validated": True,
                "landing_count": len(landing),
                "server_time": now,
            }

        if not (landing or evidences):
            raise RequestError(422, "bad_todo",
                               "PATCH 需带 status（撤回）或 landing/evidence(s)（落点）")
        now = iso_bj(datetime.datetime.now(UTC))
        stamped = []  # type: List[Dict[str, str]]
        for entry in evidences:
            checked = dict(entry)
            checked["by"] = actor
            checked["validated_at"] = now
            stamped.append(checked)
        updated = Runtime.db.todo_mark_done(todo_id, actor, stamped, landing=landing)
        return 200, {
            "ok": True,
            "session_id": item["session_id"],
            "item": self._todo_out(updated),
            "validated": True,
            "landing_count": len(landing),
            "server_time": now,
        }

    def _todos_clear(self, visitor: Visitor, session_id: str,
                     body: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        """POST /api/sessions/{sid}/todos/clear —— 批量勾选递交（#114/#124）。

        体 `{"ids":["td_...", ...], "landing":[...]}`；**幂等**：重复递交不双清（第二次进 skipped）。
        责任田同 PATCH：只清 owner=自己的条目。
        落点校验（本轮补上，与 PATCH /api/todos/{id} 同口径，#131 §1/§2）：
        本次**真会被清**的条目，必须「本次递交带落点」或「条目上已有落点」，否则整批 422
        `no_landing`、一条都不翻 —— 不许用批量勾选绕过落点。非本人条目 / 已 done 的进 skipped，
        不参与落点校验（幂等重复递交仍然通）。
        """
        session = Runtime.db.get_session(session_id, visitor.role)
        if session is None or not self._can_read_session(visitor, session):
            raise RequestError(404, "not_found", "会话不存在")
        actor = self._memo_actor(visitor, session)
        self._todo_write_guard(session)

        unknown = sorted(set(body.keys()) - {"ids", "landing", "evidence", "evidences"})
        if unknown:
            raise RequestError(422, "bad_todos", "未知字段：%s" % "、".join(unknown))
        ids = body.get("ids")
        if isinstance(ids, str):
            ids = [ids]
        if not isinstance(ids, list) or not ids:
            raise RequestError(422, "bad_todos", "ids 必须是非空数组")
        ids = [str(item or "").strip() for item in ids]
        ids = [item for item in ids if item]
        if not ids:
            raise RequestError(422, "bad_todos", "ids 不能为空")

        raw = body.get("landing")
        if raw is None:
            raw = body.get("evidences") if body.get("evidences") is not None \
                else body.get("evidence")
        landing, evidences = normalize_landing(raw)

        # 落点校验（#131 §1）：先算本次真会被清的条目，缺落点就整批拒，别半清
        rows = Runtime.db.todos_rows(session_id, ids)
        target = [r for r in rows if r["owner"] == actor and r["status"] == "open"]
        if not (landing or evidences):
            missing = [r["id"] for r in target
                       if not (r.get("landing") or r.get("evidences"))]
            if missing:
                raise RequestError(422, "no_landing",
                                   "不能自盖完成章：批量勾选递交需先交落点（landing）——"
                                   "commit 哈希 / 字面 curl+HTTP 码 / 文件路径+时间戳 / 重启 PID+时间戳；"
                                   "以下条目既无落点、本次也没带：%s（一条都没清）"
                                   % "、".join(sorted(missing)[:8]))

        now = iso_bj(datetime.datetime.now(UTC))
        stamped = []  # type: List[Dict[str, str]]
        for entry in evidences:
            checked = dict(entry)
            checked["by"] = actor
            checked["validated_at"] = now
            stamped.append(checked)
        outcome = Runtime.db.todos_clear(session_id, ids, actor,
                                         landing=landing, evidences=stamped)
        return 200, {
            "ok": True,
            "session_id": session_id,
            "cleared": outcome["cleared"],
            "skipped": outcome["skipped"],
            "changed": len(outcome["cleared"]),
            "validated": bool(landing or evidences),
            "landing_count": len(landing),
            "server_time": now,
        }

    def _todos_purge(self, visitor: Visitor, session_id: str,
                     body: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        """POST /api/sessions/{sid}/todos/purge —— 清空任务队列（#448-3）。

        与 `/todos/clear`（勾选递交 = 逐条盖完成章 + 落点校验）**分开**：purge 是显式的
        「这个队列不要了」，整批删掉本群队列条目，左栏「任务队列」立刻归空、
        **刷新后仍为空**（状态在服务端，不在 localStorage）。
        权限：必须是**本群成员**（人按会话身份 / 人 tailnet 默认 <USER_ID>；agent 带
        X-Agent-Id）。群已归档只读（与其它写口同口径）。
        体：`{}` 或 `{"scope":"all"}`（只支持 all，别的值 422）。
        """
        session = Runtime.db.get_session(session_id, visitor.role)
        if session is None or not self._can_read_session(visitor, session):
            raise RequestError(404, "not_found", "会话不存在")
        actor = self._memo_actor(visitor, session)
        self._todo_write_guard(session)
        scope = str((body or {}).get("scope") or "all").strip() or "all"
        if scope != "all":
            raise RequestError(422, "bad_scope", "scope 只支持 all（逐条完成走 /todos/clear）")
        unknown = sorted(set((body or {}).keys()) - {"scope"})
        if unknown:
            raise RequestError(422, "bad_todos", "未知字段：%s" % "、".join(unknown))
        before = len(Runtime.db.todos_list(session_id))
        removed = Runtime.db.todos_purge(session_id)
        after = len(Runtime.db.todos_list(session_id))
        LOG.info("队列清空 session=%s by=%s 清掉 %d 条（清前 %d / 清后 %d）：%s",
                 session_id, actor, len(removed), before, after,
                 "、".join(removed[:12]) or "（本来就空）")
        return 200, {
            "ok": True,
            "session_id": session_id,
            "scope": scope,
            "cleared_by": actor,
            "removed": removed,
            "changed": len(removed),
            "before": before,
            "after": after,
            "count": after,
            "server_time": iso_bj(datetime.datetime.now(UTC)),
        }

    def _todos_backfill(self, visitor: Visitor, session_id: str,
                        body: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        """POST /api/sessions/{sid}/todos/backfill —— 历史任务回填（#448-1）。

        扫本群**已落库的消息**，对「作者是人（名册外）+ 点名了名册里的 agent」的发言
        按与实时入队**同一套规则**补建队列条目，所以历史几轮的任务也能在左栏看到。
        幂等：同群同 owner 已有同正文条目（**任何状态**）就跳过，重复调不会撑大队列。
        体：`{"limit": N}`（默认 500，1..5000）。
        """
        session = Runtime.db.get_session(session_id, visitor.role)
        if session is None or not self._can_read_session(visitor, session):
            raise RequestError(404, "not_found", "会话不存在")
        self._memo_actor(visitor, session)          # 必须本群成员（只读历史，仍要门禁）
        unknown = sorted(set((body or {}).keys()) - {"limit"})
        if unknown:
            raise RequestError(422, "bad_todos", "未知字段：%s" % "、".join(unknown))
        limit = clamp((body or {}).get("limit"), 1, 5000, 500)
        agents = roster_agent_ids(Runtime.roster)
        if not agents:
            return 200, {"ok": True, "session_id": session_id, "scanned": 0,
                         "created": [], "skipped": 0, "items": [],
                         "server_time": iso_bj(datetime.datetime.now(UTC))}
        members = [m for m in (session.get("member_ids") or []) if m]
        messages, _more, _nb = Runtime.db.list_messages(session_id, limit, None, None)
        scanned, skipped, followups = 0, 0, 0
        created = []  # type: List[Dict[str, Any]]
        for msg in messages:
            author = msg.get("author") or {}
            author_id = str(author.get("id") or "")
            if author.get("type") != "human" or author_id in agents:
                continue
            meta = msg.get("meta") if isinstance(msg.get("meta"), dict) else {}
            declared = [str(x) for x in (meta.get("mentions") or [])]
            text_mentions = parse_text_mentions(msg.get("text") or "", members)
            targets = [m for m in (declared + text_mentions) if m in agents]
            if not targets:
                continue
            scanned += 1
            owner = targets[0]
            # ---- #478：回填走**与实时入队同一套**意图闸门：判为进度追问的历史消息
            # 不补进队列，只记跳过台账（幂等，重复回填不会重复计数）。
            verdict = classify_human_intent(msg.get("text") or "")
            if verdict.get("intent") != "task":
                # 回填只留台账、不动 followup_count（历史追问归因到"当前在办那条"是错的）
                self._todo_record_intent_skip(session_id, owner, author_id, msg,
                                              str(msg.get("text") or ""), verdict,
                                              count_followup=False)
                followups += 1
                continue
            entry = todo_text_from_message(msg)
            if not entry:
                continue
            key = todo_dedupe_key(entry)
            rows = Runtime.db.todos_list(session_id, owner=owner)
            if any(todo_dedupe_key(r.get("text")) == key for r in rows):
                skipped += 1
                continue
            item = Runtime.db.todo_create(session_id, entry, owner, "confirm",
                                          None, author_id or "", intent="task")
            if item:
                created.append(self._todo_out(item))
        LOG.info("队列回填 session=%s by=%s 扫到 %d 条人@agent 发言，新入队 %d 条，"
                 "跳过(重复) %d 条，判为追问不入队 %d 条",
                 session_id, display_of(visitor.username or ""), scanned,
                 len(created), skipped, followups)
        return 200, {
            "ok": True,
            "session_id": session_id,
            "scanned": scanned,
            "created": created,
            "created_count": len(created),
            "skipped": skipped,
            "followup": followups,
            "count": len(Runtime.db.todos_list(session_id)),
            "items": [self._todo_out(r) for r in Runtime.db.todos_list(session_id)],
            "server_time": iso_bj(datetime.datetime.now(UTC)),
        }

    def _todos_enrich(self, visitor: Visitor, session_id: str,
                      body: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        """POST /api/sessions/{sid}/todos/enrich —— #541 模型富化回填（手动触发）。

        给**历史上还没被模型富化过**的条目补 task_name / task_summary。新条目走的是
        入队时的异步 worker，**不用**调这里。限速两层：
          · 一次调用最多排 `limit` 条（默认 10，上限 20）；
          · worker 两次模型调用之间至少隔 `CHAT_ENRICH_MIN_INTERVAL_S`（默认 1s）。
        **绝不自动跑**：只有显式调这个端点才动，也不会开机全量补历史。
        体：`{"limit": N}`。
        """
        session = Runtime.db.get_session(session_id, visitor.role)
        if session is None or not self._can_read_session(visitor, session):
            raise RequestError(404, "not_found", "会话不存在")
        self._memo_actor(visitor, session)          # 必须本群成员（与其它写口同口径）
        unknown = sorted(set((body or {}).keys()) - {"limit"})
        if unknown:
            raise RequestError(422, "bad_todos", "未知字段：%s" % "、".join(unknown))
        limit = clamp((body or {}).get("limit"), 1, ENRICH_BACKFILL_MAX,
                      ENRICH_BACKFILL_DEFAULT)
        enricher = getattr(Runtime, "enricher", None)
        if enricher is None or not enricher.running:
            raise RequestError(503, "enricher_not_running",
                               "模型富化 worker 没在跑（%s=0？）：本次不排队，条目保持现状"
                               % ENRICH_ENABLE_ENV)
        rows = Runtime.db.todos_unenriched(session_id, limit)
        queued = []  # type: List[str]
        for row in rows:
            if enricher.submit(todo_enrich_job(row, source="backfill")):
                queued.append(str(row.get("id")))
        LOG.info("模型富化回填 session=%s 待办 %d 条 → 已排队 %d 条",
                 session_id, len(rows), len(queued))
        return 200, {"ok": True, "session_id": session_id, "requested": limit,
                     "scanned": len(rows), "queued": len(queued), "ids": queued,
                     "enricher": enricher.stats_snapshot(),
                     "server_time": iso_bj(datetime.datetime.now(UTC))}

    def _todos_intents(self, visitor: Visitor, session_id: str,
                       query: Dict[str, List[str]]) -> Tuple[int, Dict[str, Any]]:
        """GET /api/sessions/{sid}/todos/intents —— #478 意图分类台账（只读）。

        回答的是「为什么这条没入队」：每条被判为 `followup` 的人的发言一行，
        带 reason（命中的规则）+ hits（命中的词）+ 记到了哪个条目上。
        `?limit=` 默认 200（1..1000）。
        """
        session = Runtime.db.get_session(session_id, visitor.role)
        if session is None or not self._can_read_session(visitor, session):
            raise RequestError(404, "not_found", "会话不存在")
        limit = clamp((query.get("limit") or ["200"])[0], 1, 1000, 200)
        skips = Runtime.db.todos_skips_list(session_id, limit)
        items = Runtime.db.todos_list(session_id)
        return 200, {
            "ok": True,
            "session_id": session_id,
            "count": len(skips),
            "skips": skips,
            "todos_count": len(items),
            "followup_total": sum(int(i.get("followup_count") or 0) for i in items),
            "server_time": iso_bj(datetime.datetime.now(UTC)),
        }

    def _not_implemented(self, method: str, path: str) -> Tuple[int, Dict[str, Any]]:
        return 501, {"ok": False, "error": {
            "code": "not_implemented",
            "message": "本版尚未实现（契约 §12.4 实现顺序中）：%s %s" % (method, path),
        }}

    # --- 端点实现 --------------------------------------------------
    def _healthz(self) -> Dict[str, Any]:
        db_ok = False
        try:
            Runtime.db.query("SELECT 1")
            db_ok = True
        except Exception as exc:
            LOG.debug("healthz 探测 DB 失败：%s", scrubbed(exc))
        roster = Runtime.roster
        return {
            "ok": True,
            "service": "agent-team-chat",
            "version": VERSION,
            "phase": "group",
            # 构建时间戳（进程启动时算一次，请求期间恒定；见 _init_build_stamp）
            **BUILD_STAMP,
            "db": db_ok,
            "members": len(roster.ids()) if roster else 0,
            # #541：模型富化 worker 的实况（running/queued/done/fallback/not_task）
            "enrich": (Runtime.enricher.stats_snapshot()
                       if getattr(Runtime, "enricher", None) else {"running": False}),
            "uptime_s": int(time.time() - (Runtime.started_at or time.time())),
            "server_time": iso_bj(datetime.datetime.now(UTC)),
        }

    def _whoami(self, visitor: Visitor) -> Dict[str, Any]:
        return {
            "ok": True,
            "user": {
                "name": visitor.username or visitor.source,
                "display": visitor.display,
                "role": visitor.role,
                "role_cn": view_auth.ROLE_CN.get(visitor.role or "", "只读访客"),
                "source": visitor.source,
            },
            "caps": visitor.caps(),
            "server_time": iso_bj(datetime.datetime.now(UTC)),
        }

    def _agents(self, visitor: Visitor) -> Dict[str, Any]:
        roster = Runtime.roster
        items = []
        for member in (roster.all() if roster else []):
            items.append({
                "id": member["id"],
                "display": member["display"],
                # 在线态要订阅 NATS 心跳（v1.1 §B.1）—— 本版不猜，一律 None
                "online": None,
                "busy": None,
                "last_hb_at": None,
                "meta": {
                    "node_id": member.get("node_id") or "",
                    "host": member.get("host") or "",
                    "model": member.get("model") or "",
                    "description": member.get("description") or "",
                },
            })
        items.sort(key=lambda it: it["id"])
        return {"ok": True, "agents": items, "server_time": iso_bj(datetime.datetime.now(UTC))}

    def _sessions(self, visitor: Visitor, query: Dict[str, List[str]]) -> Dict[str, Any]:
        def first(name: str) -> str:
            values = query.get(name) or []
            return (values[0] or "").strip() if values else ""

        limit = clamp(first("limit"), 1, PAGE_LIMIT_MAX, PAGE_LIMIT_DEFAULT)
        kind = first("kind") or None
        if kind and kind not in ("dm", "meet"):
            raise RequestError(422, "bad_kind", "kind 只能是 dm / meet")
        state = first("state") or None
        filters = {
            "kind": kind,
            "state": state,
            "member": first("member") or None,
            "cursor": first("cursor") or None,
            "limit": limit,
        }
        sessions = Runtime.db.list_sessions(visitor.role, filters)

        out = []
        for session in sessions:
            row = dict(session)
            last = Runtime.db.last_message(row["session_id"])
            if last:
                row["last_message"] = {
                    "seq": last["seq"],
                    "author": last["author"],
                    "excerpt": excerpt(last.get("text")) or excerpt(
                        (last.get("meta") or {}).get("excerpt") if isinstance(last.get("meta"), dict) else ""),
                    "created_at": last["created_at"],
                }
            else:
                row["last_message"] = None
            row["unread"] = None          # v1 不做服务端已读游标（契约 §11.5）
            row["observing"] = self._is_observing(row["session_id"], visitor)
            row["my_role"] = visitor.role
            seen = self._seeing_filter(row)
            if seen is None:
                continue                  # 密级不够就不出现在列表里（契约 §3）
            out.append(seen)

        next_cursor = None
        if len(out) == limit and out:
            next_cursor = out[-1]["updated_at"]
        return {"ok": True, "count": len(out), "next_cursor": next_cursor,
                "sessions": out, "server_time": iso_bj(datetime.datetime.now(UTC))}

    def _seeing_filter(self, row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """meet 场才需要补会议块；dm 原样返回。"""
        if row.get("kind") != "meet":
            row.pop("member_ids", None)
            return row
        row["meeting"] = self._meeting_block(row)     # 唯一实体引用不复制（契约 §10）
        row.pop("member_ids", None)
        return row

    def _meeting_block(self, row: Dict[str, Any]) -> Dict[str, Any]:
        """会议块取自既有只读真源 roundtable_sessions；读不到就返回空壳，不编数。"""
        ref = row.get("ref") or {}
        sid = ref.get("id")
        if not sid:
            return {}
        try:
            rows = Runtime.db.query(
                "SELECT goal, state FROM roundtable_sessions WHERE session_id = %s", [sid])
        except Exception as exc:
            LOG.debug("会议真源读不到：%s", scrubbed(exc))
            return {}
        if not rows:
            return {}
        goal, state = rows[0]
        return {"goal": goal or "", "state": state or "", "ref_id": sid}

    def _is_observing(self, session_id: str, visitor: Visitor) -> bool:
        if not visitor.username:
            return False
        try:
            rows = Runtime.db.query(
                "SELECT 1 FROM chat_observers WHERE session_id = %s AND username = %s"
                " AND left_at IS NULL", [session_id, visitor.username])
            return bool(rows)
        except Exception:
            return False

    def _can_read_session(self, visitor: Visitor, session: Dict[str, Any]) -> bool:
        """会话可见性（契约 §I.3）。

        owner（管理员）与匿名 tailnet 放行；**具名访客必须在成员表里** ——
        否则"无权即不可见"（回 404，与"不存在"同码，不泄露存在性）。
        """
        if visitor.role == "owner":
            return True
        _author_type, me = self._speaker(visitor)
        if me is None:
            return True
        return me in (session.get("member_ids") or [])

    def _session_detail(self, visitor: Visitor, session_id: str) -> Tuple[int, Dict[str, Any]]:
        session = Runtime.db.get_session(session_id, visitor.role)
        if session is None or not self._can_read_session(visitor, session):
            # 无权即不可见：与"不存在"返回同一个码（契约 §3 / §I.3）
            raise RequestError(404, "not_found", "会话不存在")
        session.pop("member_ids", None)
        if session.get("kind") == "meet":
            session["meeting"] = self._meeting_block(session)
        return 200, {"ok": True, "session": session}

    def _members(self, visitor: Visitor, session_id: str,
                 query: Dict[str, List[str]]) -> Tuple[int, Dict[str, Any]]:
        """会话成员清单（GET /api/sessions/{sid}/members）。

        契约：?prefix=<str>&limit=20 →
             {"items":[{"name":..., "agent_id":..., "kind":"agent"|"human"}],"truncated":bool}
          * name     —— 写进 mentions 的原文（成员 id，大小写/繁简原样不动）
          * agent_id —— agents.json 名册里的 agent id；人（名册里没有的）为空串
          * kind     —— 名册里有 = agent，其余 = human
          * prefix   —— **大小写敏感**，name 与 agent_id 任一前缀命中即返回；空串不过滤
          * 顺序     —— 会话成员表原序（与前端 @ 栏一致），截断只发生在尾部
          * limit    —— 默认 20（clamp 到 1..200）
        数据源：chat_sessions.members（成员表真源）+ Runtime.roster（agents.json）。
        不臆造名单：名册里没有的 id 只按人处理，不额外补成员。
        """
        def first(name: str) -> str:
            values = query.get(name) or []
            return (values[0] or "").strip() if values else ""

        session = Runtime.db.get_session(session_id, visitor.role)
        if session is None or not self._can_read_session(visitor, session):
            # 与 _messages / _session_detail 同口径：无权即不可见
            raise RequestError(404, "not_found", "会话不存在")

        prefix = first("prefix")
        limit = clamp(first("limit"), 1, PAGE_LIMIT_MAX, PAGE_LIMIT_DEFAULT)
        roster = Runtime.roster

        items = []  # type: List[Dict[str, Any]]
        for mid in (session.get("member_ids") or []):
            member = roster.get(mid) if roster else None
            entry = {
                "name": mid,
                "agent_id": mid if member else "",
                "kind": "agent" if member else "human",
            }
            if prefix and not (entry["name"].startswith(prefix)
                               or entry["agent_id"].startswith(prefix)):
                continue
            items.append(entry)

        truncated = len(items) > limit
        if truncated:
            items = items[:limit]
        return 200, {"ok": True, "items": items, "truncated": truncated,
                     "server_time": iso_bj(datetime.datetime.now(UTC))}

    def _messages(self, visitor: Visitor, session_id: str, query: Dict[str, List[str]]) -> Tuple[int, Dict[str, Any]]:
        def first(name: str) -> str:
            values = query.get(name) or []
            return (values[0] or "").strip() if values else ""

        session = Runtime.db.get_session(session_id, visitor.role)
        if session is None or not self._can_read_session(visitor, session):
            raise RequestError(404, "not_found", "会话不存在")

        limit = clamp(first("limit"), 1, MSG_LIMIT_MAX, MSG_LIMIT_DEFAULT)

        def as_seq(name: str) -> Optional[int]:
            raw = first(name)
            if not raw:
                return None
            try:
                return int(raw)
            except ValueError:
                raise RequestError(422, "bad_seq", "%s 必须是整数" % name)

        before_seq = as_seq("before_seq")
        after_seq = as_seq("after_seq")
        # 兼容别名：前端按"增量拉"的措辞写 since 时不要静默忽略 —— 忽略了就变成
        # 每轮返回全量，页面一追加就是一条消息无限重复（踩过）。
        if after_seq is None:
            after_seq = as_seq("since")
        if before_seq is not None and after_seq is not None:
            raise RequestError(422, "bad_seq", "before_seq 与 after_seq 不能同时给")

        messages, has_more, next_before = Runtime.db.list_messages(
            session_id, limit, before_seq, after_seq)

        # 契约 §I：谁拉，就告诉他"群里有哪些话是他欠着的"。
        _author_type, me = self._speaker(visitor)
        pending = []  # type: List[Dict[str, Any]]
        if me:
            # "回过了"的口径：我在这条之后发过任何言。够宽、可解释，且不依赖
            # Agent 一定带 reply_to（带任务里漏了也不会把待办卡死）。
            spoke_through = max(
                [m["seq"] for m in messages if m["author"]["id"] == me] or [0])
            for msg in messages:
                meta = msg.get("meta") or {}
                if me in (meta.get("needs_response") or []) and msg["seq"] > spoke_through:
                    pending.append({
                        "seq": msg["seq"],
                        "message_id": msg["message_id"],
                        "from": msg["author"]["id"],
                        "text": (msg.get("text") or "")[:EXCERPT_LEN],
                    })

        return 200, {
            "ok": True,
            "session_id": session_id,
            "me": me,
            "messages": messages,
            "my_pending": pending,
            "has_more": has_more,
            "next_before_seq": next_before,
            "server_time": iso_bj(datetime.datetime.now(UTC)),
        }

    # --- 访客解析 --------------------------------------------------
    def _peer_ip(self) -> str:
        """直连对端地址（**不看任何头**）。免登录白名单只认它。"""
        try:
            return str(self.client_address[0])
        except Exception:
            return ""

    def _client_ip(self) -> str:
        return self._peer_ip()

    def _ua(self) -> str:
        return self.headers.get("User-Agent") or ""

    def _cookie(self, name: str) -> Optional[str]:
        raw = self.headers.get("Cookie") or ""
        for chunk in raw.split(";"):
            key, _, value = chunk.strip().partition("=")
            if key == name:
                return value or None
        return None

    def _resolve_visitor(self) -> Optional[Visitor]:
        """tailnet 直连 / 有效会话 / 都不是（None）。

        ⚠️ 与看板同一条纪律：**127.0.0.1 不在免登录名单里** —— cloudflared 正是
        从本机回环回连本服务，放行回环等于给公网开后门。
        """
        ip = self._peer_ip()
        # 先看会话 cookie —— **tailnet 内也要认**：否则<USER_ID>在自己网里打开网页时，
        # 系统只知道"来了个 owner"、认不出"你是 <USER_ID>"，群协议里"人"就不成立。
        cookie = self._cookie(view_auth.SESSION_COOKIE)
        if cookie and Runtime.auth is not None:
            principal, _reason = Runtime.auth.check_cookie(cookie, self._client_ip(),
                                                           self._ua())
            if principal is not None:
                return Visitor(principal.username, principal.role, "session")
        if view_auth.in_tailnet(ip):
            # tailnet = 团队内部网：无会话时视为 owner（与看板"tailnet 免登录"同口径）
            return Visitor(None, "owner", "tailnet")
        return None

    # --- 收发 ------------------------------------------------------
    def _drain_body(self) -> None:
        """把请求体读完并**缓存**（写端点要用；不读干净会污染下一个请求）。"""
        self._body_raw = b""  # type: bytes
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return
        remaining = min(length, BODY_MAX)
        if remaining < length:
            self.close_connection = True
        chunks = []  # type: List[bytes]
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        self._body_raw = b"".join(chunks)

    def _json_body(self) -> Dict[str, Any]:
        """缓存的请求体 -> JSON 对象（契约 §8：错误一律对象信封）。"""
        raw = getattr(self, "_body_raw", b"") or b""
        if not raw:
            return {}
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise RequestError(400, "bad_json", "请求体不是合法 JSON")
        if not isinstance(parsed, dict):
            raise RequestError(400, "bad_json", "请求体必须是 JSON 对象")
        return parsed

    def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
        """本件的 JSON 应答出口；socket 那一段全在 L1 的 `_reply()` 里（刀8a）。

        字典的插入顺序**就是**发出去的头顺序，所以四个头按迁移前的次序摆：
        Content-Type → Content-Length → Cache-Control → X-Content-Type-Options。
        `Content-Length` 必须自己填（而不是让 L1 按 body 长度补）：HEAD 不写体，
        但报的仍是**真实长度**，交给 L1 补就会变成 0。
        """
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._sent = True
        self._reply(status, b"" if self.command == "HEAD" else body, {
            "Content-Type": "application/json; charset=utf-8",
            "Content-Length": str(len(body)),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        })

    # --- 计算器反代（<MEMBER_ID> · 2026-09-23 · 前后台配合）------------------

    #: 计算器后台地址（默认本机 9985；可用环境变量 CHAT_CALC_UPSTREAM 覆盖）
    CHAT_CALC_UPSTREAM = os.environ.get("CHAT_CALC_UPSTREAM", "http://127.0.0.1:9985")
    CALC_TIMEOUT_S = 10.0

    def _calc_proxy(self, method: str, api_path: str, query: Dict[str, List[str]]
                    ) -> Optional[Tuple[int, Dict[str, Any]]]:
        """把 /calc/api/* 原样反代到后台计算服务；成功时响应已自行发出（返回 None）。"""
        pairs = [(k, v) for k, vals in (query or {}).items() for v in vals]
        suffix = ("?" + urllib.parse.urlencode(pairs)) if pairs else ""
        url = self.CHAT_CALC_UPSTREAM + api_path + suffix
        body = getattr(self, "_body_raw", b"") or None
        req = urllib.request.Request(url, data=body, method=method)
        ctype = self.headers.get("Content-Type")
        if ctype:
            req.add_header("Content-Type", ctype)
        try:
            with urllib.request.urlopen(req, timeout=self.CALC_TIMEOUT_S) as resp:
                status = resp.status
                raw = resp.read()
                out_type = resp.headers.get("Content-Type") or "application/json"
        except urllib.error.HTTPError as exc:      # 400（除零）/404 按原样透传
            status = exc.code
            raw = exc.read()
            out_type = exc.headers.get("Content-Type") or "application/json"
        except Exception as exc:                   # 后台没起 / 超时：502，不炸本服务
            LOG.warning("计算器后台不可用：%s", scrubbed(exc))
            return 502, {"ok": False, "error": {
                "code": "calc_upstream_unavailable",
                "message": "计算器后台暂不可用：%s" % scrubbed(exc)}}
        self._send_bytes(status, raw, out_type, "no-store")
        return None

    # --- 计算器后台侧管理后台静态托管（<MEMBER_ID> 责任田 · 2026-09-24）------

    #: 后台侧 Vue 产物目录（<RUNTIME_DIR>/admin-ui/）。可用 CHAT_ADMIN_UI_DIR 覆盖。
    #: 与 ui/（<MEMBER_ID> 前台）**完全分离**：两边目录不同、路由前缀不同，互不覆盖。
    CHAT_ADMIN_UI_DIR = os.environ.get("CHAT_ADMIN_UI_DIR", AGENT_TEAM_RUNTIME_DIR + "/admin-ui")
    ADMIN_DOC = "index.html"

    def _serve_admin(self, rel_path: str, method: str) -> Optional[Tuple[int, Dict[str, Any]]]:
        """托管 /calc/admin 静态资源。返回 None 表示响应已自行发出。

        与 _serve_static 同规矩：目录穿越/隐藏文件/排除表/备份临时件一律拦，
        带扩展名未命中 → 404（不回 index.html，否则浏览器把 HTML 当 JS 解析），
        无扩展名未命中 → SPA fallback 回 index.html。
        """
        if method not in ("GET", "HEAD"):
            return 405, {"ok": False, "error": {
                "code": "method_not_allowed", "message": "静态资源只支持 GET/HEAD"}}

        root = self.CHAT_ADMIN_UI_DIR
        if not root or not os.path.isdir(root):
            return 404, {"ok": False, "error": {
                "code": "admin_ui_not_deployed",
                "message": "管理后台产物尚未部署（admin-ui/ 不存在）：%s" % (root or "(未配置)")}}

        base = os.path.realpath(root)
        rel = urllib.parse.unquote(rel_path).lstrip("/")
        candidate = os.path.realpath(os.path.join(base, rel))

        inside = candidate == base or candidate.startswith(base + os.sep)
        parts = [p for p in os.path.relpath(candidate, base).split(os.sep)
                 if p not in ("", ".")]
        blocked = ((not inside)
                   or any(p.startswith(".") for p in parts)
                   or any(p in STATIC_EXCLUDE for p in parts)
                   or any(STATIC_BACKUP_RE.search(p) for p in parts))   # 刀34：备份/临时件

        target = None                          # type: Optional[str]
        if not blocked and os.path.isfile(candidate):
            target = candidate

        if target is None:
            ext = os.path.splitext(rel)[1].lower()
            if blocked or ext in STATIC_EXTS or ext:
                return 404, {"ok": False, "error": {
                    "code": "not_found", "message": "管理后台静态资源不存在：%s" % rel}}
            index = os.path.join(base, self.ADMIN_DOC)
            if not os.path.isfile(index):
                return 404, {"ok": False, "error": {
                    "code": "admin_ui_not_deployed",
                    "message": "admin-ui/ 里没有 " + self.ADMIN_DOC}}
            target = index                      # SPA fallback

        self._send_file(target)
        return None

    # --- 同源静态托管（会议 §H · <MEMBER_ID> 8 条清单）-------------

    def _redirect(self, location: str) -> None:
        """302 跳转。响应自行发出 —— _handle 返回 None 即表示响应已发。

        socket 那一段在 L1 的 `_reply()` 里（刀8a）；头顺序照旧
        Location → Cache-Control → Content-Length，body 本来就是空的。
        """
        self._sent = True
        self._reply(302, b"", {
            "Location": location,
            "Cache-Control": "no-cache",
            "Content-Length": "0",
        })

    def _login_redirect(self, path: str, query: Dict[str, List[str]]) -> None:
        """未登录访客 → 门户登录页（带 next=回本页的绝对 URL）。

        回跳目标由看板侧 safe_next() 的白名单把关（只认自有域名），
        所以这里即便 Host 头被改也跳不出站外。
        """
        nxt = (query.get("next") or ["/"])[0]
        if not isinstance(nxt, str) or not nxt.startswith("/"):
            nxt = "/"
        host = (self.headers.get("Host") or "").split(":")[0].strip().lower()
        scope = urllib.parse.urlsplit(LOGIN_BASE_URL).hostname or ""
        home = ("https://" + host
                if host and scope and (host == scope or host.endswith("." + scope))
                else PUBLIC_URL)
        target = "%s/login?next=%s" % (
            LOGIN_BASE_URL, urllib.parse.quote(home + nxt, safe=""))
        self._redirect(target)

    def _serve_static(self, path: str, method: str) -> Optional[Tuple[int, Dict[str, Any]]]:
        """静态兜底。返回 None 表示响应已自行发出。

        清单对照：①排除 serve.py/mock.js ③SPA fallback ⑤no-cache ⑦MIME 钉死。
        刀34 起再加一条：备份 / 临时件（`STATIC_BACKUP_RE`）一律拦 —— 见该常量处的说明。
        """
        if method not in ("GET", "HEAD"):
            # 写操作永远不做静态（清单第 2 条的精神：静态只读）
            return 405, {"ok": False, "error": {
                "code": "method_not_allowed", "message": "静态资源只支持 GET/HEAD"}}

        root = Runtime.ui_dir
        if not root or not os.path.isdir(root):
            return 404, {"ok": False, "error": {
                "code": "ui_not_deployed",
                "message": "前端产物尚未部署（ui/ 不存在）：%s" % (root or "(未配置)")}}

        base = os.path.realpath(root)
        rel = urllib.parse.unquote(path).lstrip("/")
        candidate = os.path.realpath(os.path.join(base, rel))

        # 目录穿越 / 排除表 / 隐藏文件 / 备份临时件 —— 四条一起判，别拆开（拆开容易漏）
        inside = candidate == base or candidate.startswith(base + os.sep)
        parts = [p for p in os.path.relpath(candidate, base).split(os.sep)
                 if p not in ("", ".")]
        blocked = ((not inside)
                   or any(p.startswith(".") for p in parts)
                   or any(p in STATIC_EXCLUDE for p in parts)
                   or any(STATIC_BACKUP_RE.search(p) for p in parts))   # 刀34：备份/临时件

        target = None                          # type: Optional[str]
        if not blocked:
            if os.path.isdir(candidate):
                index = os.path.join(candidate, DEFAULT_DOC)
                if os.path.isfile(index):
                    target = index
            elif os.path.isfile(candidate):
                target = candidate

        if target is None:
            ext = os.path.splitext(rel)[1].lower()
            if blocked or ext:
                # 带扩展名（或已被拦）的未命中 → 404。
                # 不能回 index.html：浏览器会把 HTML 当 JS/CSS 解析，报 MIME 错。
                return 404, {"ok": False, "error": {
                    "code": "not_found", "message": "静态资源不存在"}}
            index = os.path.join(base, DEFAULT_DOC)
            if not os.path.isfile(index):
                return 404, {"ok": False, "error": {
                    "code": "ui_not_deployed", "message": "ui/ 里没有 " + DEFAULT_DOC}}
            target = index                      # SPA fallback（清单第 3 条）

        self._send_file(target)
        return None

    def _send_file(self, full: str) -> None:
        try:
            with open(full, "rb") as handle:
                data = handle.read()
        except OSError as exc:
            LOG.warning("静态文件读失败 %s：%s", full, exc)
            self._send_json(500, {"ok": False, "error": {
                "code": "internal", "message": "静态文件读失败"}})
            return
        ext = os.path.splitext(full)[1].lower()
        ctype = MIME_OVERRIDE.get(ext) or mimetypes.guess_type(full)[0] \
            or "application/octet-stream"
        self._send_bytes(200, data, ctype, STATIC_CACHE)

    def _send_bytes(self, status: int, data: bytes, content_type: str,
                    cache: str = "no-store") -> None:
        """字节应答出口（静态文件 / 计算器反代都从这里出去）。

        socket 那一段在 L1 的 `_reply()` 里（刀8a）；头顺序与 `Content-Length`
        的自填理由同 `_send_json()`。L1 的 `_reply()` 本来就 `if body:` 才写，
        与迁移前 `data` 为空时不写 socket 的写法一致。
        """
        self._sent = True
        self._reply(status, b"" if self.command == "HEAD" else data, {
            "Content-Type": content_type,
            "Content-Length": str(len(data)),
            "Cache-Control": cache,
            "X-Content-Type-Options": "nosniff",
        })


# ------------------------------------------------------------------ 启动

def build_runtime(dsn: str, roster_paths: Sequence[str],
                  importer: Optional[Callable[[str], Any]] = None) -> None:
    Runtime.reset()
    Runtime.started_at = time.time()
    Runtime.db = ChatDB(dsn, importer=importer)
    try:
        Runtime.db.ensure_memo_schema()
    except Exception as exc:
        # 建表失败不阻断服务：群记忆是附加能力，聊天不能因此起不来。
        LOG.warning("群记忆锚建表失败（不影响聊天）：%s", scrubbed(exc))
    try:
        Runtime.db.ensure_todo_schema()
    except Exception as exc:
        # 同上：待办台账是附加能力，建表失败只告警，不阻断聊天。
        LOG.warning("待办台账建表失败（不影响聊天）：%s", scrubbed(exc))
    try:
        Runtime.db.ensure_meta_schema()
    except Exception as exc:
        # 群名/备注可编辑（#90）同样是附加能力：建列失败只告警，不阻断聊天。
        LOG.warning("群名/备注建列失败（不影响聊天）：%s", scrubbed(exc))
    Runtime.roster = Roster(roster_paths)
    Runtime.ui_dir = resolve_ui_dir()
    # #541：模型富化 worker 只**建对象**，线程由 main() 起（单测导入本模块不该起模型调用）
    Runtime.enricher = TodoEnricher()
    try:
        Runtime.session_key = view_auth.load_session_key(
            os.environ.get("CHAT_SESSION_KEY_FILE") or view_auth.DEFAULT_KEY_FILE)
        store = view_auth.PgStore(dsn)
        Runtime.auth = view_auth.Auth(store, Runtime.session_key)
    except Exception as exc:
        # 鉴权起不来不算致命：tailnet 直连照样能用，只是会话登录不可用
        LOG.warning("鉴权初始化失败（tailnet 直连不受影响）：%s", scrubbed(exc))
        Runtime.auth = None


def resolve_ui_dir() -> str:
    """静态根（同源托管）：CHAT_UI_DIR > 本文件同级的 ui/。"""
    raw = (os.environ.get(UI_DIR_ENV) or "").strip()
    if raw:
        return raw
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), UI_DIR_NAME)


def resolve_dsn() -> str:
    for env_name in (DSN_ENV_PRIMARY, DSN_ENV_FALLBACK):
        value = (os.environ.get(env_name) or "").strip()
        if value:
            return value
    try:
        return view_auth.read_dsn(DSN_FILE)
    except Exception:
        return ""


def resolve_roster_paths() -> List[str]:
    raw = (os.environ.get(ROSTER_ENV) or "").strip()
    if raw:
        parts = [p.strip() for p in re.split(r"[;:]", raw) if p.strip()]
        # Windows 盘符（C:\...）会被冒号切开，拼回
        merged = []  # type: List[str]
        for part in parts:
            if merged and len(part) == 0:
                continue
            if merged and re.match(r"^[\\/]", part) and re.match(r"^[A-Za-z]$", merged[-1]):
                merged[-1] = merged[-1] + ":" + part
            else:
                merged.append(part)
        if merged:
            return merged
    return list(ROSTER_DEFAULTS)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="人机对话台（team_chat）后端服务")
    ap.add_argument("--host", default=os.environ.get("CHAT_HOST") or DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=int(os.environ.get("CHAT_PORT") or DEFAULT_PORT))
    ap.add_argument("--dsn", default=None, help="PG 连接串（默认读 CHAT_PG_DSN / BB_PG_DSN）")
    ap.add_argument("--agents-json", action="append", default=None,
                    help="agents.json 路径，可给多次（默认 <ETC_DIR>/agents.json）")
    ap.add_argument("--ui-dir", default=None,
                    help="前端静态根（同源托管；默认读 CHAT_UI_DIR / 本文件同级 ui/）")
    ap.add_argument("--log-level", default=os.environ.get("CHAT_LOG_LEVEL") or "INFO")
    args = ap.parse_args(list(argv) if argv is not None else None)

    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    dsn = args.dsn or resolve_dsn()
    roster_paths = args.agents_json or resolve_roster_paths()
    build_runtime(dsn, roster_paths)
    if args.ui_dir:
        Runtime.ui_dir = args.ui_dir

    # #541：起模型富化 worker（单线程）。CHAT_ENRICH_ENABLE=0 时不起，
    # 条目一律保留规则名 —— 服务照常跑，模型不是命门。
    if getattr(Runtime, "enricher", None) is not None:
        Runtime.enricher.start()

    httpd = ThreadingHTTPServer((args.host, args.port), ChatHandler)
    httpd.daemon_threads = True
    LOG.info("agent-team-chat v%s 起在 %s:%d（只读阶段；成员 %d 个；DSN %s）",
             VERSION, args.host, args.port,
             len(Runtime.roster.ids()), "已配" if dsn else "**未配**")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        LOG.info("收到中断，退出")
    finally:
        httpd.server_close()
        if Runtime.db:
            Runtime.db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
