#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
blackboard_svc.py —— 团队黑板·记忆网关（统一端口 9940）

设计要点（与 a2a_svc.py 同一套口径）：
  * 纯标准库 http.server + psycopg2（唯一的第三方依赖）；语法兼容 Python 3.9。
  * 鉴权：Authorization: Bearer <token> → sha256 → 反查 peers.json 得到成员名。
    **身份只由 token 决定**，请求体里的 author 字段一律忽略（防冒名）。
  * 只绑命令行传入的 host（生产传 tailscale0 的 100.x）；--admin-port 另起的
    健康口只绑 127.0.0.1。
  * 口令只从环境变量 BB_PG_DSN 读，**绝不写进代码 / 日志 / 响应**：
    所有往外吐的数据库错误都先过 _safe_err() 脱敏。
  * 黑板是 append-only 的：本服务只有 INSERT 和 SELECT，没有任何 UPDATE/DELETE 路径
    （数据库层的触发器还会再挡一道，见 sql/blackboard_schema.sql）。
  * DB 不通不崩进程：/healthz 报 503，业务口也返回 503。
  * 向量嵌入（P4）是**可选增强**：embedder 不可用时，写入照常 200（embedding 留 NULL），
    /search 自动退回关键词检索，/healthz 也照样 ok —— 黑板的可用性不押在模型上。
  * P5 加的圆桌会议接口（/sessions、/sessions/<id>）是**纯只读**：本服务对
    roundtable_sessions / roundtable_turns 只有 SELECT，一条写语句都没有。
    会议的落库由 roundtable.py 负责，两边职责不混。
  * v1.3 把"会议四要素"（目标 / 议程 / 当场确认 / 落实·遗留跟踪）读出来，同样纯只读：
      - /sessions/<id> 扩展出 goal、agenda、agenda_items、confirmations、confirm_summary、
        actions、issues、confirmed_at（老字段名一个没动，向后兼容）；
      - 新增 /actions、/issues、/tracking 三个跨会清单端点。
    **缺表 / 缺列不崩**：新表是另一条迁移在建的，库里还没有时按"这部分没记录"降级，
    在响应的 degraded[] 里点名是哪一块缺，页面据此给友好提示（区别于"库挂了"= 503）。
    **是否逾期由服务端算**（字段 overdue）：统一换算到 UTC 再比较，响应里带 now/tz，
    绝不让浏览器自己比时间 —— 各端时区不一致时"逾期"这种事实不能有两种说法。
  * v1.4 把"到会没到会"也读出来（同样纯只读、同样缺列降级）：
      - /sessions 与 /sessions/<id> 扩展出 expected_count（应到）/ present_count（实到）/
        absent_count（缺席）/ attendance_rate（**出席率**，不是缺席率），
        列表里还带一份 absent_members（缺席名单，取自 confirm_summary.attendance）；
      - issues[] 带上 kind（issue|defect）与 evidence（证据），/issues?kind=defect 可筛；
      - /tracking 的 counts 多两个数：defects_open / defects_overdue。
    口径：**出席率的分母是应到人数**（sessions.members 全集），而"结论当场确认"的分母
    是在场人数 —— 两个分母不是一个东西，见 sql/roundtable_ops_schema2.sql 的注释。
  * v1.5 加的共享内容接口（/api/shared、/api/shared/<id>、/api/shared/tasks、
    /api/shared/search）同样是**纯只读**：本服务对 shared_tasks / shared_content /
    shared_content_revisions 只有 SELECT，发布 / 刷新 / 撤下 / 销毁全在 shared_content.py 里。
    **共享内容 ≠ 会议**：受众与管控都不一样，是两套独立功能，这里不掺会议的任何字段。
      - 写请求（POST/PUT/PATCH/DELETE）一律 **405**，且一个字节都不碰库；
      - 默认不返回 revoked / destroyed（撤下就是不想再给人看了），也不返回已过期的；
        `expired=1` 才把过期的一并带出来，每条都带服务端算好的 `expired` 布尔；
      - **sensitivity 管控**：没有有效 token 一律 401（sensitive 自然也拿不到）；
        `audience` 不含请求者时，列表里**直接不出现**，按 id 直取则 403 —— 403 比 404
        诚实：东西在，只是不给你看；
      - 缺表不崩：库里还没跑 sql/shared_content_schema.sql 时按"这块没记录"降级，
        在 degraded[] 里点名（区别于"库挂了"= 503）。
  * v1.6 加「临时共享区」（tmp box，`/tmp`），是**本服务第一个带写路径的业务面**：
      - 由来：Agent 之间交互时，长文本不再直接塞进 A2A/NATS 载荷（那边 256 KiB 硬上限、
        群发还会扇出复制），改成"只传简短信息 + 共享信息 id"，正文放这里按需读取；
      - `POST /tmp` 存（owner 一律取鉴权结果）、`GET /tmp/<id>` 取、`GET /tmp` 查清单、
        `DELETE /tmp/<id>` 由 owner 主动销毁；
      - **读计数按 distinct reader 去重**：群发是"一份材料给 N 个成员各读一次"，
        按请求计数会让任一成员的重试/刷新吃掉配额 → 提前销毁（是 bug）；
      - 读满（reads_max 个不同成员）即销毁、过期即销毁，**都在读的那个请求里顺手做完**，
        不等扫描；9940 进程内另有 300s 一轮的兜底 sweep 线程；
      - **销毁 ≠ 删行**：destroyed_at + destroy_reason + body 置空，行保留留痕；
        数据库层还有触发器挡（不可变列 / body 单向清空 / 不许 DELETE·TRUNCATE / 读计数只增）。
  * v1.7 加「Agent 会话」（session，`/sess`），治"多轮对话忘记自己刚承诺过什么"：
      - 契约：Agent会话-session-设计与契约-v1.md（表 agent_sessions / agent_session_turns）；
      - **路径是 `/sess`，不是 `/sessions`** —— 后者是会议（roundtable 场次），两套东西
        绝不能混（契约 D5）；同理表也另建，不掺 roundtable_* 的任何一列（D4）；
      - `POST /sess` 开会话（owner 一律取鉴权结果）、`GET /sess` 列表（口径与会议列表对齐）、
        `GET /sess/<id>` 详情 + 最近若干轮、`GET /sess/<id>/turns` 取流水、
        `POST /sess/<id>/close` 关闭并归档；**不提供 DELETE**（D6：关闭走 close，流水不可删）；
      - 每个 session 必须带 `topic`（D8：与黑板条目 / 会议 session 同一值域，缺省 general），
        这样"按主题"能横跨黑板 / 会议 / 会话三处一起检索；
      - **关闭与空闲超时都归档**：turns 拼成一份 markdown 写进 `shared_content`
        （任务级 scope + 归档期限）→ 会话的 `archive_ref` 指向它、`state` 置 `closed`/`expired`；
        归档后 turn 正文**清空留行**（D6：归档 ≠ 删除）；
      - 空闲判据是 `now() - last_turn_at > idle_ttl_s`（D1：**不是**绝对超时），
        `last_turn_at`/`turn_count` 由数据库触发器每轮自动刷新；
        sweep **复用进程内已有的那个 sweep 线程**（不新增定时单元），/healthz 用 `sess_swept_at` 自报；
      - 本文件是 `/sess` 的**中枢侧**：会话留痕与归档。服务端回放（D2，a2a_svc）、
        team.py 默认会话（D7 之前的默认裸奔）在另外的批次里，本文件一行都不改它们。
  * 本服务对 shared_content 的越界说明：v1.7 起**归档**会往 shared_content 写一条
    （任务级 scope），这是 `/sess` 关闭路径的一部分；`/api/shared*` 那套读接口仍然纯只读。

用法：
    BB_PG_DSN='host=127.0.0.1 port=9950 dbname=blackboard user=bb password=...' \
    python blackboard_svc.py --host <NODE_IP> --port 9940 \
        --agents-json <ETC_DIR>/agents.json \
        --peers-json <ETC_DIR>/peers.json \
        --admin-port 0 --log-file /var/log/<SERVICE>.log
"""

import argparse
import base64
import node_paths as _np  # L0 平台层：宿主默认值唯一出处（env 优先）
import datetime
import hashlib
import hmac
import json
import logging
import os
import re
import socket
import sys
import threading
import time
import urllib.parse
from http.server import ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import embedder  # 同目录模块；导入只读环境变量，不会加载模型
import web_gateway  # L1 通行层：HTTP 应答出口与非 GET 方法的路由分发

VERSION = "1.9"
# 兜底默认值集中到 L0 node_paths（env 优先）
AGENT_TEAM_RUNTIME_DIR = os.environ.get("AGENT_TEAM_RUNTIME_DIR") or _np.runtime_dir()
# 兜底默认值集中到 L0 node_paths（env 优先）
AGENT_TEAM_ETC_DIR = os.environ.get("AGENT_TEAM_ETC_DIR") or _np.etc_dir()
MAX_BODY_BYTES = 262144      # 请求体上限 256 KiB，与 a2a_svc 对齐
DRAIN_MAX = 4 * 1024 * 1024  # 超限请求最多排空这么多字节，再多就直接断连接
DETAIL_MAX = 200             # 错误 detail 截断长度
DEFAULT_LIMIT = 20
MAX_LIMIT = 200
SEARCH_DEFAULT_LIMIT = 10    # /search 的默认条数
SEARCH_MAX_LIMIT = 50        # /search 的条数上限（语义检索比按 topic 拉贵得多）
SEARCH_QUERY_MAX = 2048      # q 的长度上限，挡住拿超长文本刷模型的请求
# 与 sql 里的 vector(N) 必须一致：走 embedder 的口径（BB_EMBED_DIM，默认 512），
# 这样"库列维度 / 模型维度 / 入参校验维度"只有一个真相来源。
EMBEDDING_DIM = embedder.embed_dim()
TEXT_FIELD_MAX = 512         # topic / kind 这类短字段的长度上限
DB_CONNECT_TIMEOUT_S = 5
SESSIONS_DEFAULT_LIMIT = 20  # /sessions 默认条数
SESSIONS_MAX_LIMIT = 50      # /sessions 与 /sessions/<id> 的条数上限，一律夹在 1..50
TURNS_DEFAULT_LIMIT = 50     # /sessions/<id> 的发言默认全取（一场会议本就没几条）
SESSION_ID_MAX = 128         # session_id 的长度上限，挡住拿超长路径刷库的请求
TRACK_DEFAULT_LIMIT = 20     # /actions、/issues、/tracking 的默认条数
TRACK_MAX_LIMIT = 50         # 跟踪类端点的条数上限，与会议口径一致（1..50）
OWNER_MAX = 128              # owner 过滤词的长度上限

# ---- 临时共享区（tmp box；契约：临时共享区-设计与契约-v1.md，路径 /tmp、表 tmp_shared）
TMP_DEFAULT_TTL_S = 7200     # 存活秒数缺省值（缺省 = 2 小时）
TMP_MIN_TTL_S = 60
TMP_MAX_TTL_S = 86400
TMP_READS_MIN = 1            # 读满销毁阈值 1..100
TMP_READS_MAX = 100
TMP_OWNER_ACTIVE_MAX = 200   # 同一 owner 的活跃（未销毁·未过期）条数上限，超了 429
TMP_ACTIVE_MAX = 5000        # 全局活跃条数上限，超了按 expires_at 最早的先销毁（reason=capacity）
TMP_SWEEP_INTERVAL_S = 300   # 进程内兜底 sweep 间隔（秒）
TMP_LIST_DEFAULT_LIMIT = 20  # GET /tmp 默认条数
TMP_LIST_MAX_LIMIT = 50      # GET /tmp 条数上限，与会议口径一致（1..50）
TMP_ID_PREFIX = "t-"
TMP_ID_LEN = 8               # 8 字符 base32 → 40 bit，够短能塞进载荷，也够宽不撞
TMP_ID_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"   # RFC4648 base32 字母表
TMP_ID_RE = re.compile(r"^t-[A-Z2-7]{8}$")
TMP_DESTROY_REASONS = ("read_quota", "expired", "capacity", "owner", "schema")

# ---- Agent 会话（session；契约：Agent会话-session-设计与契约-v1.md，路径 /sess）
# 路径刻意是 /sess：黑板现成的 /sessions 是**会议**，两套东西绝不能混（D5）。
SESS_ID_PREFIX = "as-"
# 'as-YYYYMMDD-HHMMSS-xxxx'（UTC）。形状先卡一道：形状不对就是"没有这个会话"，连库都不用碰。
SESS_ID_RE = re.compile(r"^as-\d{8}-\d{6}-[a-z2-7]{4}$")
SESS_ID_RAND_LEN = 4         # 随机后缀长度（base32 小写，40 bit 里的 20 bit）
SESS_ID_TRY = 5              # 主键撞了就重摇的次数（和 /tmp 同一套）
SESS_TOPIC_MAX = TEXT_FIELD_MAX   # topic 与黑板条目/会议 session 同一值域、同一长度口径
SESS_DEFAULT_TOPIC = "general"    # D8：不许空 topic，缺省 general
SESS_STATES = ("open", "closed", "expired")
SESS_TERMINAL_STATES = ("closed", "expired")
SESS_CLOSE_REASON_MANUAL = "manual"
SESS_CLOSE_REASON_IDLE = "idle"   # 空闲超时（D1）
SESS_DEFAULT_IDLE_TTL_S = 1800    # 空闲超时缺省 30 分钟
SESS_MIN_IDLE_TTL_S = 60
SESS_MAX_IDLE_TTL_S = 86400
SESS_MEMBERS_MAX = 100        # 参与成员上限（与 /tmp 的 to 对齐）
SESS_MEMBER_NAME_MAX = 64
SESS_LIST_DEFAULT_LIMIT = 20  # GET /sess 默认条数
SESS_LIST_MAX_LIMIT = 50      # GET /sess 条数上限，与会议口径一致（1..50）
SESS_TURNS_DEFAULT_LIMIT = 50 # GET /sess/<id>/turns 默认条数
SESS_TURNS_MAX_LIMIT = 200    # 取流水的上限（归档要读全量，走的是另一条无 limit 的 SQL）
SESS_DETAIL_TURNS_DEFAULT = 20  # 详情里默认带最近 20 轮
SESS_ARCHIVE_EXPIRES_DAYS = 30  # 归档文档的存活天数（任务级 scope 必须带 expires_at）
SESS_ARCHIVE_KIND = "note"      # shared_content.kind：用文档化的默认值，靠 tags 区分归档
SESS_ARCHIVE_TASK_PREFIX = "sess-archive-"   # 每个会话一个归档任务容器（task_id = 前缀 + session_id）
SESS_UNARCHIVED_BATCH = 50      # sweep 每轮补扫"已终态但没归档"的上限
SESS_ROLES = ("user", "assistant", "system")

# ---- 消息可靠投递（msg_log；契约：消息可靠性与会话-测试用例与接口契约-v1.md，路径 /msg）
# 口径：**中枢落库成功 = 送达**。发送方拿 msg_id 随时查，不必盲等 135 秒超时。
MSG_ID_PREFIX = "m-"
MSG_ID_LEN = 8                 # 8 字符 base32 → 40 bit（与 /tmp、/sess 同一套摇号）
MSG_ID_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
MSG_ID_RE = re.compile(r"^m-[A-Z2-7]{8}$")
MSG_ID_TRY = 5                 # 撞主键重摇次数
MSG_STATES = ("queued", "processing", "done", "failed", "expired", "stalled")
MSG_TERMINAL_STATES = ("done", "failed", "expired", "stalled")
MSG_OPEN_STATES = ("queued", "processing")   # 还欠着的两种状态（补拉用）
MSG_DEFAULT_TOPIC = "general"
MSG_TOPIC_MAX = TEXT_FIELD_MAX
MSG_BODY_MAX = 65536           # 短正文上限 64 KiB；再长请走 content_ref（/tmp 临时共享区）
MSG_MEMBER_MAX = 64
MSG_DEFAULT_TTL_S = 86400      # 24 小时
MSG_MIN_TTL_S = 60
MSG_MAX_TTL_S = 604800         # 7 天
MSG_LIST_DEFAULT_LIMIT = 20
MSG_LIST_MAX_LIMIT = 50
# processing 超过这么久没动静 → 标 stalled + 告警。**绝不自动重投**
# （at-least-once ≠ exactly-once：重投会把同一条再喂一遍 LLM，产生重复副作用）
MSG_STALL_AFTER_S = 900

# 状态枚举：只认这些值，其余一律 400（口径与 sql/roundtable_schema 的注释一致）
ACTION_STATUSES = ("open", "doing", "done", "dropped")
ACTION_OPEN_STATUSES = ("open", "doing")      # "未闭环"= 还欠着的落实事项
ISSUE_STATUSES = ("open", "watching", "closed")
ISSUE_OPEN_STATUSES = ("open", "watching")    # "未闭环"= 还在跟踪的遗留问题
AGENDA_STATUSES = ("pending", "concluded", "deferred")
# 遗留问题的性质：issue=普通遗留；defect=疑似程序缺陷（目前只有"成员点名无应答"会开）
ISSUE_KINDS = ("issue", "defect")
DEFECT_KIND = "defect"

LOG = logging.getLogger("blackboard")


class DBUnavailable(Exception):
    """数据库不可用（未配置 / 未装驱动 / 连不上 / 查询失败）。消息必须已脱敏。"""


class Runtime(object):
    """进程级运行时配置（由 main 填充，handler 只读）。"""

    node_id = "unknown"
    peer_map = {}        # type: Dict[str, str]  token 的 sha256 -> 成员 id
    # 可注入的数据库访问层：db_query(sql, params, fetch) -> rows/row/None
    # 生产是 PgBackend 实例，测试塞一个假的，因此本模块不依赖真 PG。
    db_query = None      # type: Optional[Callable[[str, Sequence[Any], str], Any]]
    db_configured = False
    # 临时共享区的兜底 sweep 状态（给 /healthz 自报；由 sweep 线程写，handler 只读）
    tmp_swept_at = None      # type: Optional[str]  最近一次 sweep 成功的时间（UTC ISO8601）
    tmp_sweep_error = None   # type: Optional[str]  最近一次 sweep 的失败原因（已脱敏）
    tmp_sweeps = 0           # 累计成功 sweep 轮数
    # Agent 会话的空闲超时 sweep 状态（与 tmp 共用同一个 sweep 线程，各自自报）
    sess_swept_at = None     # type: Optional[str]  最近一轮会话 sweep 成功的时间（UTC ISO8601）
    sess_sweep_error = None  # type: Optional[str]  最近一轮会话 sweep 的失败原因（已脱敏）
    sess_sweeps = 0          # 累计成功轮数
    sess_archived = 0        # 累计归档的会话数（空闲超时 + 补扫）
    # 消息可靠投递（/msg）的 sweep 状态：同一线程里的第三块，与 tmp/sess 各自自报
    msg_swept_at = None      # type: Optional[str]  最近一轮 msg sweep 成功的时间（UTC ISO8601）
    msg_sweep_error = None   # type: Optional[str]  最近一轮 msg sweep 的失败原因（已脱敏）
    msg_sweeps = 0           # 累计成功轮数


# ---------------------------------------------------------------- 脱敏

_SECRET_RE = re.compile(r"(password|passfile|sslpassword)\s*=\s*\S+", re.IGNORECASE)


def _safe_err(exc: Any) -> str:
    """把异常变成可以安全外泄的一行字：抹掉 DSN 口令，再截断。"""
    text = str(exc).replace("\n", " ").strip() or exc.__class__.__name__
    text = _SECRET_RE.sub(r"\1=***", text)
    return text[:DETAIL_MAX]


# "表/列还没建"和"库挂了"必须分开：前者降级（这部分没记录），后者 503（服务不可用）。
# PG 的原话是 relation "xxx" does not exist / column xxx does not exist。
_MISSING_SCHEMA_RE = re.compile(
    r"(does not exist|undefined\s*table|undefined\s*column|"
    r"no such table|no such column|unknown column)", re.IGNORECASE)


class SessionNotOpen(Exception):
    """会话已终态（closed / expired）还往里追加轮次。

    库里 agent_session_turns 上的 guard 会 RAISE，端点把它翻成 **409** ——
    这是"按规矩不再收新轮次"，不是服务坏了（冒 500 会让人以为黑board 崩了）。
    """


def _is_missing_schema(exc: Any) -> bool:
    """这条 DB 错误是不是"缺表 / 缺列"？是就可以安全降级成"这块没记录"。"""
    return bool(_MISSING_SCHEMA_RE.search(str(exc)))


# ---------------------------------------------------------------- 配置加载

def load_agents(path: Optional[str]) -> None:
    """解析 agents.json，只为拿 node_id（黑板不承载 agent，不需要 backend 配置）。"""
    if not path:
        return
    # utf-8-sig：对无 BOM 的 UTF-8 等价，顺手容忍 Windows 编辑器写出来的 BOM
    with open(path, "r", encoding="utf-8-sig") as f:
        cfg = json.load(f)
    if not isinstance(cfg, dict):
        raise ValueError("agents.json 顶层必须是对象")
    Runtime.node_id = str(cfg.get("node_id") or "unknown")


def load_peers(path: Optional[str]) -> None:
    """解析 peers.json：{成员id: token的sha256十六进制}。只存摘要，不存明文。"""
    if not path:
        Runtime.peer_map = {}
        return
    with open(path, "r", encoding="utf-8-sig") as f:
        peers = json.load(f)
    if not isinstance(peers, dict):
        raise ValueError("peers.json 顶层必须是对象")
    peer_map = {}  # type: Dict[str, str]
    for pid, digest in peers.items():
        digest = str(digest or "").strip().lower()
        if digest:
            peer_map[digest] = str(pid)
    Runtime.peer_map = peer_map


# ---------------------------------------------------------------- 数据库层

def _import_psycopg2(importer: Optional[Callable[[str], Any]] = None) -> Any:
    """导入 psycopg2；没装就抛 DBUnavailable 并给出人话安装提示，而不是崩栈。

    延迟到这里才导入：本模块唯一的第三方依赖，缺了也要能起进程、能回 503。
    importer 参数只给测试用（注入一个必然失败的导入器）。
    """
    try:
        if importer is not None:
            return importer("psycopg2")
        import psycopg2  # noqa: F401  —— 唯一的第三方依赖
        return psycopg2
    except ImportError as exc:
        raise DBUnavailable(
            "psycopg2 未安装（%s）。装法：pip install 'psycopg2-binary>=2.9'；"
            "pip 不通时用发行版包：apt-get install -y python3-psycopg2" % _safe_err(exc))


class PgBackend(object):
    """最小连接持有者：一条连接 + 一把锁，断了自动重连一次。

    没上连接池是有意的：黑板是低频写、低频读的服务，串行化反而省心；
    真需要并发再换 psycopg2.pool，接口（__call__）不变。
    """

    def __init__(self, dsn: str, importer: Optional[Callable[[str], Any]] = None) -> None:
        if not dsn:
            raise DBUnavailable("BB_PG_DSN 为空：数据库未配置")
        self._dsn = dsn                      # 含口令，绝不打印
        self._driver = _import_psycopg2(importer)
        self._lock = threading.Lock()
        self._conn = None                    # type: Any

    def _connect(self) -> Any:
        conn = self._driver.connect(self._dsn, connect_timeout=DB_CONNECT_TIMEOUT_S)
        conn.autocommit = True               # append-only 的单句写，不需要显式事务
        return conn

    def _drop(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def __call__(self, sql: str, params: Sequence[Any] = (), fetch: str = "all") -> Any:
        with self._lock:
            last = None  # type: Optional[Exception]
            for _ in (0, 1):
                fresh = self._conn is None or getattr(self._conn, "closed", 0)
                try:
                    if fresh:
                        self._conn = self._connect()
                    cur = self._conn.cursor()
                    try:
                        cur.execute(sql, tuple(params))
                        if fetch == "all":
                            return cur.fetchall()
                        if fetch == "one":
                            return cur.fetchone()
                        return None
                    finally:
                        try:
                            cur.close()
                        except Exception:
                            pass
                except Exception as exc:
                    last = exc
                    self._drop()
                    # 连接是本轮新建的就别重试了：重试只为了救"服务端掐了老连接"这一种情况，
                    # 重试一条刚建的连接既救不回来，也可能把写操作重放一次。
                    if fresh:
                        break
            raise DBUnavailable(_safe_err(last))

    def close(self) -> None:
        with self._lock:
            self._drop()


def db_query(sql: str, params: Sequence[Any] = (), fetch: str = "all") -> Any:
    """统一入口：走 Runtime.db_query（可注入）。未配置就抛 DBUnavailable。"""
    fn = Runtime.db_query
    if fn is None:
        raise DBUnavailable("数据库未配置（BB_PG_DSN 缺失或驱动不可用）")
    return fn(sql, params, fetch)


def install_pg_backend(dsn: str, importer: Optional[Callable[[str], Any]] = None) -> None:
    """装上真正的 PG 后端。失败抛 DBUnavailable，由调用方决定降级还是退出。"""
    Runtime.db_query = PgBackend(dsn, importer=importer)
    Runtime.db_configured = True


# ---------------------------------------------------------------- 业务：读写黑板

INSERT_SQL = (
    "INSERT INTO blackboard_entries (author, topic, kind, content, refs, embedding) "
    "VALUES (%s, %s, %s, %s, %s::jsonb, %s::vector) RETURNING entry_id, ts"
)

SELECT_COLUMNS = "entry_id, ts, author, topic, kind, content, refs, status"


def _iso(value: Any) -> Any:
    """timestamptz -> ISO8601 字符串；假 DB 直接给字符串时原样返回。"""
    if isinstance(value, datetime.datetime):
        return value.isoformat()
    return value


def _as_refs(value: Any) -> List[Any]:
    """jsonb 列：psycopg2 通常已解成 list；万一是字符串就补一次解析。"""
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def row_to_entry(row: Sequence[Any]) -> Dict[str, Any]:
    return {
        "entry_id": row[0],
        "ts": _iso(row[1]),
        "author": row[2],
        "topic": row[3],
        "kind": row[4],
        "content": row[5],
        "refs": _as_refs(row[6]),
        "status": row[7],
    }


def _vector_literal(embedding: Optional[Sequence[float]]) -> Optional[str]:
    """pgvector 的文本字面量形式，避免为了一列向量引入 pgvector 的 python 适配器。"""
    if embedding is None:
        return None
    return "[" + ",".join(repr(float(x)) for x in embedding) + "]"


def insert_entry(author: str, topic: str, kind: str, content: str,
                 refs: List[Any], embedding: Optional[List[float]]) -> Dict[str, Any]:
    """写一条黑板条目。author 由调用方（鉴权结果）给定，不接受外部输入。"""
    vec = _vector_literal(embedding)
    row = db_query(INSERT_SQL,
                   (author, topic, kind, content, json.dumps(refs, ensure_ascii=False), vec),
                   "one")
    if not row:
        raise DBUnavailable("insert 没有返回 entry_id")
    return {"entry_id": row[0], "ts": _iso(row[1])}


def select_entries(topic: Optional[str], kind: Optional[str], author: Optional[str],
                   limit: int, since: Optional[str], status: Optional[str]) -> List[Dict[str, Any]]:
    """按 topic/kind/author/时间过滤，ts 倒序。全部走参数化，不拼字符串。"""
    where = []   # type: List[str]
    params = []  # type: List[Any]
    if topic:
        where.append("topic = %s")
        params.append(topic)
    if kind:
        where.append("kind = %s")
        params.append(kind)
    if author:
        where.append("author = %s")
        params.append(author)
    if since:
        where.append("ts >= %s::timestamptz")
        params.append(since)
    if status != "all":
        where.append("status = %s")
        params.append(status or "active")
    sql = "SELECT " + SELECT_COLUMNS + " FROM blackboard_entries"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY ts DESC, entry_id DESC LIMIT %s"
    params.append(limit)
    rows = db_query(sql, params, "all") or []
    return [row_to_entry(r) for r in rows]


# ---------------------------------------------------------------- 业务：语义检索

SEARCH_COLUMNS = SELECT_COLUMNS          # 检索返回的条目字段与 /entries 完全一致
_LIKE_SPECIAL_RE = re.compile(r"([\\%_])")


def compute_embedding(text: str) -> Optional[List[float]]:
    """算一条文本的向量。嵌入不可用就返回 None —— 调用方降级，绝不让请求失败。"""
    try:
        vectors = embedder.embed_texts([text])
    except Exception as exc:     # embedder 承诺不抛；这里是最后一道保险，宁可降级
        LOG.warning("嵌入异常（已降级）: %s", _safe_err(exc))
        return None
    if not vectors:
        return None
    return vectors[0]


def has_any_embedding() -> bool:
    """库里是否至少有一条带向量的行。一条都没有就没必要走语义排序。"""
    row = db_query(
        "SELECT count(*) FROM blackboard_entries WHERE embedding IS NOT NULL", (), "one")
    return bool(row and row[0])


def _like_pattern(q: str) -> str:
    """把用户输入包成 ILIKE 模式串：先把 \\ % _ 转义掉，免得用户输个 % 就全表命中。"""
    return "%" + _LIKE_SPECIAL_RE.sub(r"\\\1", q) + "%"


def _score(distance: Any) -> Optional[float]:
    """pgvector 的 <=> 是余弦距离，1 - 距离即相似度。保留 4 位小数。"""
    try:
        return round(float(distance), 4)
    except (TypeError, ValueError):
        return None


def search_semantic(vector: Sequence[float], topic: Optional[str],
                    limit: int) -> List[Dict[str, Any]]:
    """按余弦距离 <=> 排序。**只有 embedding 非 NULL 的行参与**（NULL 排序没有意义）。"""
    vec = _vector_literal(vector)
    where = ["embedding IS NOT NULL", "status = %s"]
    params = [vec, "active"]     # 第一个 %s 在 SELECT 列表里（算 score）
    if topic:
        where.append("topic = %s")
        params.append(topic)
    sql = ("SELECT " + SEARCH_COLUMNS + ", 1 - (embedding <=> %s::vector) AS score "
           "FROM blackboard_entries WHERE " + " AND ".join(where) +
           " ORDER BY embedding <=> %s::vector LIMIT %s")
    params.append(vec)
    params.append(limit)
    rows = db_query(sql, params, "all") or []
    out = []  # type: List[Dict[str, Any]]
    for row in rows:
        entry = row_to_entry(row)
        entry["score"] = _score(row[8]) if len(row) > 8 else None
        out.append(entry)
    return out


def search_keyword(q: str, topic: Optional[str], limit: int) -> List[Dict[str, Any]]:
    """降级路径：content 的 ILIKE 子串匹配，ts 倒序。score 一律 null（没有可比的相似度）。"""
    where = ["content ILIKE %s", "status = %s"]
    params = [_like_pattern(q), "active"]  # type: List[Any]
    if topic:
        where.append("topic = %s")
        params.append(topic)
    sql = ("SELECT " + SEARCH_COLUMNS + " FROM blackboard_entries WHERE " +
           " AND ".join(where) + " ORDER BY ts DESC, entry_id DESC LIMIT %s")
    params.append(limit)
    rows = db_query(sql, params, "all") or []
    out = []  # type: List[Dict[str, Any]]
    for row in rows:
        entry = row_to_entry(row)
        entry["score"] = None    # 字段形状与语义分支保持一致，客户端不用分叉
        out.append(entry)
    return out


# ---------------------------------------------------------------- 业务：圆桌会议（只读）
#
# 只有 SELECT。会议的写入全在 roundtable.py 里，且发言表在库里也是 append-only
# （见 sql/roundtable_schema.sql 的触发器），本服务连"能写"的代码都不存在。

SESSION_COLUMNS = ("s.session_id, s.topic, s.question, s.moderator, s.members, s.state, "
                   "s.round_no, s.max_rounds, s.started_at, s.ended_at, s.conclusion_entry")

# v1.3 的新列一律**追加在最后**，老列的下标一个都不挪 —— row_to_session 只读 0..12，
# 新列由各自的 select_* 显式取，缺列时整条查询退回老 SQL 也就不影响老字段。
SESSION_EXTRA_COLUMNS = "s.goal, s.agenda, s.confirmed_at, s.confirm_summary"
# v1.4 的出席计量列，同样追加在最后。缺这几列时**只降级这一块**（degraded 里记
# session_attendance），goal/agenda 那一组照常读得到 —— 两次迁移可以分开上线。
SESSION_ATTENDANCE_COLUMNS = ("s.expected_count, s.present_count, s.absent_count, "
                              "s.attendance_rate")

# 发言条数 / 发言人数用**一个聚合子查询**一次性算完，绝不对每场会议再查一遍（N+1）。
TURN_STATS_JOIN = (
    " LEFT JOIN (SELECT session_id, count(*) AS turns_total, "
    "                   count(DISTINCT speaker) AS speakers_total "
    "            FROM roundtable_turns GROUP BY session_id) t "
    "       ON t.session_id = s.session_id"
)

TURN_COLUMNS = ("turn_id, round_no, seq, speaker, status, kind, stance, vote, confidence, "
                "content, refs, entry_id, error, ts, elapsed_ms")


def _as_members(value: Any) -> List[Any]:
    """members 是 jsonb 数组；psycopg2 通常已解成 list，字符串就补一次解析。"""
    return _as_refs(value)


def row_to_session(row: Sequence[Any]) -> Dict[str, Any]:
    return {
        "session_id": row[0],
        "topic": row[1],
        "question": row[2],
        "moderator": row[3],
        "members": _as_members(row[4]),
        "state": row[5],
        "round_no": row[6],
        "max_rounds": row[7],
        "started_at": _iso(row[8]),
        "ended_at": _iso(row[9]),
        "conclusion_entry": row[10],
        "turns_total": row[11] or 0,
        "speakers_total": row[12] or 0,
    }


def _as_float(value: Any) -> Optional[float]:
    """confidence 是 real；假 DB 可能给字符串，统一成 float 或 None。"""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> Optional[int]:
    """计数列统一成 int 或 None（布尔不算数，免得 True 被当成 1）。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_names(value: Any) -> List[Any]:
    """名单：只接受数组，别的一律当没记录（页面宁可显示"未记录"，不显示半截）。"""
    return [n for n in value] if isinstance(value, list) else []


def attendance_fields(row: Sequence[Any], start: int) -> Dict[str, Any]:
    """从 row[start:start+4] 取出席计量。缺列时（row 不够长）一律 None。

    **字段名是出席率 attendance_rate，不是缺席率** —— 分母是应到人数
    （expected_count = sessions.members 全集），不是在场人数。
    """
    def pick(offset: int) -> Any:
        index = start + offset
        return row[index] if len(row) > index else None

    return {"expected_count": _as_int(pick(0)),
            "present_count": _as_int(pick(1)),
            "absent_count": _as_int(pick(2)),
            "attendance_rate": _as_float(pick(3))}


EMPTY_ATTENDANCE = {"expected_count": None, "present_count": None,
                    "absent_count": None, "attendance_rate": None}


def row_to_turn(row: Sequence[Any]) -> Dict[str, Any]:
    return {
        "turn_id": row[0],
        "round_no": row[1],
        "seq": row[2],
        "speaker": row[3],
        "status": row[4],
        "kind": row[5],
        "stance": row[6],
        "vote": row[7],
        "confidence": _as_float(row[8]),
        "content": row[9],
        "refs": _as_refs(row[10]),
        "entry_id": row[11],
        "error": row[12],
        "ts": _iso(row[13]),
        "elapsed_ms": row[14],
    }


def _note_degraded(degraded: Optional[List[str]], name: str) -> None:
    """记一笔"这块因为缺表/缺列没读到"，同一块只记一次。"""
    if degraded is not None and name not in degraded:
        degraded.append(name)


def select_sessions(state: Optional[str], topic: Optional[str], limit: int,
                    since: Optional[str],
                    degraded: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """按 state/topic/时间过滤，开始时间倒序。全部参数化。

    会顺带取一列 goal（会议目标）；库里还没这列就退回老 SQL，列表照常出。
    """
    where = []   # type: List[str]
    params = []  # type: List[Any]
    if state:
        where.append("s.state = %s")
        params.append(state)
    if topic:
        where.append("s.topic = %s")
        params.append(topic)
    if since:
        where.append("s.started_at >= %s::timestamptz")
        params.append(since)
    head = ("SELECT " + SESSION_COLUMNS + ", COALESCE(t.turns_total, 0), "
            "COALESCE(t.speakers_total, 0)")
    tail = " FROM roundtable_sessions s" + TURN_STATS_JOIN
    if where:
        tail += " WHERE " + " AND ".join(where)
    tail += " ORDER BY s.started_at DESC, s.session_id DESC LIMIT %s"
    params.append(limit)
    # 三级回退：出席计量（v1.4）→ 只有 goal（v1.3）→ 老库。两次迁移可以分开上线，
    # 少哪一块就只降级哪一块，列表永远出得来。
    level = "attendance"
    try:
        rows = db_query(head + ", s.goal, " + SESSION_ATTENDANCE_COLUMNS +
                        ", s.confirm_summary" + tail, params, "all") or []
    except DBUnavailable as exc:
        if not _is_missing_schema(exc):
            raise
        _note_degraded(degraded, "session_attendance")
        level = "goal"
        try:
            rows = db_query(head + ", s.goal" + tail, params, "all") or []
        except DBUnavailable as exc2:
            if not _is_missing_schema(exc2):
                raise
            _note_degraded(degraded, "session_goal")
            level = "legacy"
            rows = db_query(head + tail, params, "all") or []
    out = []  # type: List[Dict[str, Any]]
    for row in rows:
        item = row_to_session(row)
        item["goal"] = row[13] if len(row) > 13 else None
        if level == "attendance":
            item.update(attendance_fields(row, 14))
            # 缺席名单只有 confirm_summary 里有（会议侧唯一放得下名字的地方）
            summary = _as_summary(row[18] if len(row) > 18 else None) or {}
            item["absent_members"] = _as_names((summary.get("attendance") or {})
                                               .get("absent_members"))
        else:
            item.update(EMPTY_ATTENDANCE)
            item["absent_members"] = []
        out.append(item)
    return out


def select_session(session_id: str,
                   degraded: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
    """取一场会议（含结论正文 + 四要素里的会话级字段）。不存在返回 None，调用方回 404。

    先按 v1.3 的列查；库里还没加 goal/agenda/confirmed_at/confirm_summary 就退回老 SQL，
    老会议照样能看（新字段一律给 None，页面显示"（未记录目标/议程）"）。
    """
    head = ("SELECT " + SESSION_COLUMNS + ", COALESCE(t.turns_total, 0), "
            "COALESCE(t.speakers_total, 0), b.content")
    tail = (" FROM roundtable_sessions s" + TURN_STATS_JOIN +
            " LEFT JOIN blackboard_entries b ON b.entry_id = s.conclusion_entry "
            "WHERE s.session_id = %s")
    # 三级回退，与 select_sessions 一致：出席计量 → 四要素 → 老库
    level = "attendance"
    try:
        row = db_query(head + ", " + SESSION_EXTRA_COLUMNS + ", " +
                       SESSION_ATTENDANCE_COLUMNS + tail, (session_id,), "one")
    except DBUnavailable as exc:
        if not _is_missing_schema(exc):
            raise
        _note_degraded(degraded, "session_attendance")
        level = "extra"
        try:
            row = db_query(head + ", " + SESSION_EXTRA_COLUMNS + tail, (session_id,), "one")
        except DBUnavailable as exc2:
            if not _is_missing_schema(exc2):
                raise
            _note_degraded(degraded, "session_goal")
            level = "legacy"
            row = db_query(head + tail, (session_id,), "one")
    if not row:
        return None
    extended = level in ("attendance", "extra")
    session = row_to_session(row)
    session["conclusion_text"] = row[13] if len(row) > 13 else None
    # 新字段：缺列时一律 None/空，字段名照出，客户端不用分叉判断版本
    session["goal"] = row[14] if extended and len(row) > 14 else None
    session["agenda"] = _as_refs(row[15]) if extended and len(row) > 15 else []
    session["confirmed_at"] = _iso(row[16]) if extended and len(row) > 16 else None
    session["confirm_summary"] = _as_summary(row[17]) if extended and len(row) > 17 else None
    if level == "attendance":
        session.update(attendance_fields(row, 18))
    else:
        session.update(EMPTY_ATTENDANCE)
    summary = session.get("confirm_summary") or {}
    session["absent_members"] = _as_names((summary.get("attendance") or {})
                                          .get("absent_members"))
    return session


def select_turns(session_id: str, limit: int) -> List[Dict[str, Any]]:
    """一场会议的逐条发言，按 (round_no, seq) 正序 —— 会议记录得按时间读。"""
    sql = ("SELECT " + TURN_COLUMNS + " FROM roundtable_turns WHERE session_id = %s "
           "ORDER BY round_no ASC, seq ASC, turn_id ASC LIMIT %s")
    rows = db_query(sql, (session_id, limit), "all") or []
    return [row_to_turn(r) for r in rows]


# ---------------------------------------------------------------- 业务：会议四要素（只读）
#
# 口径（<USER_ID>定的）：会议有目标，会前有议程，会中有结论（当场确认、指定责任人+截止时间），
# 会有落实（遗留问题进跟踪系统）。这一段把这四件事从库里读出来，**只有 SELECT**。
#
# 时间一律换算到 UTC 再比较：逾期与否是服务端算好的事实（字段 overdue），响应里带
# now / tz，浏览器只负责显示成北京时间，不参与判断。

UTC = datetime.timezone.utc


def utc_now() -> datetime.datetime:
    """当前时刻（带 UTC 时区）。单独抽出来是为了单测能替换。"""
    return datetime.datetime.now(UTC)


def _to_utc(value: Any) -> Optional[datetime.datetime]:
    """把 timestamptz / ISO 字符串统一成带 UTC 时区的 datetime；认不出来返回 None。

    裸时间（没带时区）一律按 UTC 解释 —— 宁可口径单一，也不要"看你在哪台机器上跑"。
    """
    if isinstance(value, datetime.datetime):
        moment = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text[-1] in ("Z", "z"):
            text = text[:-1] + "+00:00"
        try:
            moment = datetime.datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def is_overdue(due_at: Any, status: Any, open_states: Sequence[str],
               now: datetime.datetime) -> bool:
    """逾期 = 还没闭环 且 有截止时间 且 截止时间**严格早于**现在。

    边界故意取严格小于：正好等于 now 的那一刻还不算逾期（截止时间是"到点为止"）。
    没有 due_at、已经 done/dropped/closed 的，一律不算逾期。
    """
    if status not in tuple(open_states):
        return False
    moment = _to_utc(due_at)
    if moment is None:
        return False
    return moment < now


def _as_summary(value: Any) -> Optional[Dict[str, Any]]:
    """confirm_summary（jsonb 对象）：补齐四个名单 + required/passed；不是对象就当没记录。

    **passed 只认真正的布尔 true**：拿不准一律 None，页面按"未确认"标黄 ——
    "没确认"绝不能显示成"通过了"。
    """
    data = value
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            return None
    if not isinstance(data, dict):
        return None
    out = {}  # type: Dict[str, Any]
    for key in ("agree", "object", "abstain", "absent"):
        names = data.get(key)
        out[key] = [n for n in names] if isinstance(names, list) else []
    required = data.get("required")
    out["required"] = required if isinstance(required, int) and not isinstance(required, bool) else None
    # present = 计入确认分母的**在场人数**（absent 不计入）；缺了就给 None，别拿 total 冒充
    out["present"] = _as_int(data.get("present"))
    passed = data.get("passed")
    out["passed"] = passed if isinstance(passed, bool) else None
    out["attendance"] = _as_attendance(data.get("attendance"))
    return out


def _as_attendance(value: Any) -> Optional[Dict[str, Any]]:
    """confirm_summary.attendance：应到 / 实到 / 缺席 / 出席率 + 两份名单。

    会议侧把缺席名单挂在这儿（sessions 上没有放名字的列），看板要红标缺席的人。
    """
    if not isinstance(value, dict):
        return None
    rate = _as_float(value.get("rate"))
    return {"expected": _as_int(value.get("expected")),
            "present": _as_int(value.get("present")),
            "absent": _as_int(value.get("absent")),
            "rate": rate,
            "present_members": _as_names(value.get("present_members")),
            "absent_members": _as_names(value.get("absent_members"))}


AGENDA_SELECT = ("item_id, session_id, seq, title, expected, conclusion, conclusion_entry, "
                 "status, note")
CONFIRM_SELECT = "confirm_id, session_id, target, member, verdict, note, ts"
# actions / issues 都左连一次会议表，顺手把"来源会议"的 topic 带出来（页面要显示来源）
ACTION_SELECT = ("a.action_id, a.session_id, a.agenda_seq, a.what, a.owner, a.due_at, "
                 "a.status, a.evidence, a.note, a.created_at, a.closed_at, a.closed_by, s.topic")
ACTION_FROM = (" FROM roundtable_actions a "
               "LEFT JOIN roundtable_sessions s ON s.session_id = a.session_id")
ISSUE_SELECT = ("i.issue_id, i.session_id, i.title, i.detail, i.owner, i.status, i.due_at, "
                "i.next_check_at, i.refs, i.created_at, i.closed_at, i.closed_by, i.note, s.topic")
# v1.4 的两列同样**追加在最后**（14=kind，15=evidence），老下标一个不挪
ISSUE_KIND_COLUMNS = "i.kind, i.evidence"
ISSUE_FROM = (" FROM roundtable_issues i "
              "LEFT JOIN roundtable_sessions s ON s.session_id = i.session_id")


def row_to_agenda_item(row: Sequence[Any]) -> Dict[str, Any]:
    status = row[7]
    return {
        "item_id": row[0],
        "session_id": row[1],
        "seq": row[2],
        "title": row[3],
        "expected": row[4],
        "conclusion": row[5],
        "conclusion_entry": row[6],
        # 状态只认白名单，认不出来的一律当 pending（页面按灰色徽标显示）
        "status": status if status in AGENDA_STATUSES else "pending",
        "note": row[8],
    }


def row_to_confirmation(row: Sequence[Any]) -> Dict[str, Any]:
    return {
        "confirm_id": row[0],
        "session_id": row[1],
        "target": row[2],
        "member": row[3],
        "verdict": row[4],
        "note": row[5],
        "ts": _iso(row[6]),
    }


def row_to_action(row: Sequence[Any], now: datetime.datetime) -> Dict[str, Any]:
    return {
        "action_id": row[0],
        "session_id": row[1],
        "agenda_seq": row[2],
        "what": row[3],
        "owner": row[4],
        "due_at": _iso(row[5]),
        "status": row[6],
        "evidence": row[7],
        "note": row[8],
        "created_at": _iso(row[9]),
        "closed_at": _iso(row[10]),
        "closed_by": row[11],
        "session_topic": row[12] if len(row) > 12 else None,
        "overdue": is_overdue(row[5], row[6], ACTION_OPEN_STATUSES, now),
    }


def row_to_issue(row: Sequence[Any], now: datetime.datetime) -> Dict[str, Any]:
    return {
        "issue_id": row[0],
        "session_id": row[1],
        "title": row[2],
        "detail": row[3],
        "owner": row[4],
        "status": row[5],
        "due_at": _iso(row[6]),
        "next_check_at": _iso(row[7]),
        "refs": _as_refs(row[8]),
        "created_at": _iso(row[9]),
        "closed_at": _iso(row[10]),
        "closed_by": row[11],
        "note": row[12],
        "session_topic": row[13] if len(row) > 13 else None,
        "overdue": is_overdue(row[6], row[5], ISSUE_OPEN_STATUSES, now),
        # 性质：认不出来的一律当普通遗留问题 —— 宁可少标一个"疑似缺陷"，
        # 也不要把普通问题误标成缺陷（缺陷单是要人 24h 内闭环的）
        "kind": row[14] if len(row) > 14 and row[14] in ISSUE_KINDS else "issue",
        "evidence": _as_refs(row[15]) if len(row) > 15 else [],
    }


def _in_clause(column: str, values: Sequence[str], params: List[Any]) -> str:
    """生成 `col IN (%s, %s)` 并把值塞进 params —— 枚举也走参数化，不拼字符串。"""
    params.extend(values)
    return "%s IN (%s)" % (column, ", ".join(["%s"] * len(values)))


def select_agenda_items(session_id: str,
                        degraded: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """一场会议的议程条目，按 seq 正序。缺表就降级成空列表。"""
    sql = ("SELECT " + AGENDA_SELECT + " FROM roundtable_agenda_items "
           "WHERE session_id = %s ORDER BY seq ASC, item_id ASC")
    try:
        rows = db_query(sql, (session_id,), "all") or []
    except DBUnavailable as exc:
        if not _is_missing_schema(exc):
            raise
        _note_degraded(degraded, "agenda_items")
        return []
    return [row_to_agenda_item(r) for r in rows]


def select_confirmations(session_id: str,
                         degraded: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """一场会议的当场确认记录（总体 + 各议程项），按时间正序。缺表降级成空列表。"""
    sql = ("SELECT " + CONFIRM_SELECT + " FROM roundtable_confirmations "
           "WHERE session_id = %s ORDER BY ts ASC, confirm_id ASC")
    try:
        rows = db_query(sql, (session_id,), "all") or []
    except DBUnavailable as exc:
        if not _is_missing_schema(exc):
            raise
        _note_degraded(degraded, "confirmations")
        return []
    return [row_to_confirmation(r) for r in rows]


def select_actions(status: Optional[str], owner: Optional[str], overdue: bool,
                   limit: int, now: datetime.datetime,
                   session_id: Optional[str] = None,
                   degraded: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """落实事项清单（跨会或单会）。默认按 due_at 升序，没填截止时间的排最后。

    overdue=True 时只留"还欠着且已过期"的：status in (open,doing) 且 due_at < now。
    比较用的 now 由调用方传进来，和返回里的 overdue 字段是**同一个时刻**，不会打架。
    """
    where = []   # type: List[str]
    params = []  # type: List[Any]
    if session_id:
        where.append("a.session_id = %s")
        params.append(session_id)
    if status:
        where.append("a.status = %s")
        params.append(status)
    if owner:
        where.append("a.owner = %s")
        params.append(owner)
    if overdue:
        where.append(_in_clause("a.status", ACTION_OPEN_STATUSES, params))
        where.append("a.due_at IS NOT NULL")
        where.append("a.due_at < %s::timestamptz")
        params.append(now.isoformat())
    sql = "SELECT " + ACTION_SELECT + ACTION_FROM
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY a.due_at ASC NULLS LAST, a.action_id ASC LIMIT %s"
    params.append(limit)
    try:
        rows = db_query(sql, params, "all") or []
    except DBUnavailable as exc:
        if not _is_missing_schema(exc):
            raise
        _note_degraded(degraded, "actions")
        return []
    return [row_to_action(r, now) for r in rows]


def select_issues(status: Optional[str], owner: Optional[str], overdue: bool,
                  limit: int, now: datetime.datetime,
                  session_id: Optional[str] = None,
                  degraded: Optional[List[str]] = None,
                  kind: Optional[str] = None) -> List[Dict[str, Any]]:
    """遗留问题清单（跨会或单会）。口径与 select_actions 完全一致，只是未闭环的状态不同。

    kind='defect' 只看疑似缺陷（缺席自动开的单）。库里还没有 kind 列时：
      * 没要求筛 → 退回老 SQL，kind 一律给 'issue'；
      * 要求筛了 → **返回空 + degraded**，绝不把没筛过的清单冒充成筛过的。
    """
    def build(select_cols: str, with_kind: bool) -> Tuple[str, List[Any]]:
        where = []   # type: List[str]
        params = []  # type: List[Any]
        if session_id:
            where.append("i.session_id = %s")
            params.append(session_id)
        if status:
            where.append("i.status = %s")
            params.append(status)
        if owner:
            where.append("i.owner = %s")
            params.append(owner)
        if kind and with_kind:
            where.append("i.kind = %s")
            params.append(kind)
        if overdue:
            where.append(_in_clause("i.status", ISSUE_OPEN_STATUSES, params))
            where.append("i.due_at IS NOT NULL")
            where.append("i.due_at < %s::timestamptz")
            params.append(now.isoformat())
        sql = "SELECT " + select_cols + ISSUE_FROM
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY i.due_at ASC NULLS LAST, i.issue_id ASC LIMIT %s"
        params.append(limit)
        return sql, params

    sql, params = build(ISSUE_SELECT + ", " + ISSUE_KIND_COLUMNS, True)
    try:
        return [row_to_issue(r, now)
                for r in (db_query(sql, params, "all") or [])]
    except DBUnavailable as exc:
        if not _is_missing_schema(exc):
            raise
    # 第一条挂了：可能是**缺 kind 列**，也可能是**整张表还没建** —— 用老 SQL 分辨一次，
    # 别把两件事混成一个 degraded 名字（页面提示的话术不一样）。
    sql, params = build(ISSUE_SELECT, False)
    try:
        rows = db_query(sql, params, "all") or []
    except DBUnavailable as exc2:
        if not _is_missing_schema(exc2):
            raise
        _note_degraded(degraded, "issues")
        return []
    _note_degraded(degraded, "issue_kind")
    if kind:
        return []           # 筛不了就别给：绝不把没筛过的清单冒充成筛过的
    return [row_to_issue(r, now) for r in rows]


def _count_pair(table: str, status_col: str, due_col: str, open_states: Sequence[str],
                now: datetime.datetime, name: str,
                degraded: Optional[List[str]],
                kind_col: Optional[str] = None,
                kind_value: Optional[str] = None) -> Tuple[Optional[int], Optional[int]]:
    """一次查出 (未闭环数, 逾期数) —— 计数是全库口径，不受 limit 影响。缺表返回 (None, None)。

    kind_col/kind_value 给了就只数那一类（用来数 kind='defect' 的缺陷单）。
    """
    params = []  # type: List[Any]

    def clause(with_due: bool) -> str:
        """拼一个 FILTER 条件，顺手把参数按**出现顺序**塞进 params。"""
        parts = [_in_clause(status_col, open_states, params)]
        if kind_col:
            parts.append("%s = %%s" % kind_col)
            params.append(kind_value)
        if with_due:
            parts.append("%s IS NOT NULL" % due_col)
            parts.append("%s < %%s::timestamptz" % due_col)
            params.append(now.isoformat())
        return " AND ".join(parts)

    open_clause = clause(False)
    late_clause = clause(True)
    sql = ("SELECT count(*) FILTER (WHERE %s), count(*) FILTER (WHERE %s) FROM %s"
           % (open_clause, late_clause, table))
    try:
        row = db_query(sql, params, "one")
    except DBUnavailable as exc:
        if not _is_missing_schema(exc):
            raise
        _note_degraded(degraded, name)
        return None, None
    if not row:
        return 0, 0
    return int(row[0] or 0), int(row[1] or 0)


def tracking_counts(now: datetime.datetime,
                    degraded: Optional[List[str]] = None) -> Dict[str, Any]:
    """看板顶部的计数：落实/遗留/**缺陷**各自的未闭环数与逾期数。缺表时对应项为 None。

    缺陷（kind='defect'）单独数一份：缺席 = 疑似 Bug，"还有几个缺陷没闭环"这件事
    得摆在最显眼的地方，不能混在遗留问题的总数里看不见。
    """
    actions_open, actions_overdue = _count_pair(
        "roundtable_actions", "status", "due_at", ACTION_OPEN_STATUSES, now,
        "actions", degraded)
    issues_open, issues_overdue = _count_pair(
        "roundtable_issues", "status", "due_at", ISSUE_OPEN_STATUSES, now,
        "issues", degraded)
    defects_open, defects_overdue = (None, None)
    if issues_open is not None:          # 表都没有就别再多打一次库
        defects_open, defects_overdue = _count_pair(
            "roundtable_issues", "status", "due_at", ISSUE_OPEN_STATUSES, now,
            "issue_kind", degraded, kind_col="kind", kind_value=DEFECT_KIND)
    return {"actions_open": actions_open, "actions_overdue": actions_overdue,
            "issues_open": issues_open, "issues_overdue": issues_overdue,
            "defects_open": defects_open, "defects_overdue": defects_overdue}


# ---------------------------------------------------------------- 业务：共享内容（只读）
#
# 口径（<USER_ID>定的）：共享内容 ≠ 会议。任务级是临时共享（有时间限制与销毁条件，任务结束即销毁），
# 永久级是常驻共享（节点公网 IP、各机装了什么 Agent、主人画像与关键信息，一份共享）。
# 两种都会变、都会过期、都能被撤下 —— 因此每一条都带 status 与 expires_at，
# 且**过期与否由服务端算**（字段 expired），浏览器不参与判断。
#
# 本服务只有 SELECT。写入、刷新、撤下、销毁全走 shared_content.py，
# 数据库层还有触发器兜底（见 sql/shared_content_schema.sql）。

SHARED_DEFAULT_LIMIT = 20    # /api/shared 默认条数
SHARED_MAX_LIMIT = 50        # 与会议口径一致，一律夹在 1..50
SHARED_REVISION_LIMIT = 100  # 一条内容最多带出多少条改动历史
SHARED_SCOPES = ("task", "permanent")
SHARED_STATUSES = ("active", "expired", "revoked", "destroyed")
SHARED_TASK_STATUSES = ("active", "done", "aborted")
# 默认不端给 Agent 的状态：撤下的和销毁的（"撤下"的意思就是不想再给人看了）
SHARED_HIDDEN_STATUSES = ("revoked", "destroyed")
SHARED_ALL_AUDIENCE = "all"

# 取列顺序 = row_to_shared 的下标顺序，**改一处必须改两处**。
# 与 shared_content.CONTENT_COLUMNS 逐字相同（两边各一份是有意的：服务与 CLI 互不 import，
# 部署时少拷一个文件也不至于起不来），由 tests/test_shared_content.py 断言两份一致。
SHARED_COLUMNS = ("content_id, scope, task_id, kind, title, body, tags, owner, audience, "
                  "sensitivity, status, expires_at, review_at, version, created_at, "
                  "updated_at, updated_by, destroyed_at, source_refs")

# **响应契约**（D2 看板按它写，不许改）
SHARED_FIELDS = ("content_id", "scope", "task_id", "kind", "title", "body", "tags",
                 "owner", "audience", "sensitivity", "status", "expires_at", "review_at",
                 "version", "created_at", "updated_at", "updated_by", "destroyed_at",
                 "source_refs", "expired")

SHARED_TASK_COLUMNS = ("task_id, title, owner, status, note, created_at, ended_at, ttl_at, "
                       "destroyed_at")
SHARED_REVISION_COLUMNS = "rev_id, content_id, action, actor, changed, note, ts"


def _as_obj(value: Any) -> Dict[str, Any]:
    """jsonb 对象列：psycopg2 通常已解成 dict；字符串就补一次解析，别的一律当没记录。"""
    data = value
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            return {}
    return data if isinstance(data, dict) else {}


def shared_is_expired(expires_at: Any, now: datetime.datetime) -> bool:
    """过期 = 有过期时间 且 **严格早于**现在。正好等于此刻还不算过期。

    边界与 is_overdue 取同一条：截止时间是"到点为止"。两处算出来的红标必须是同一件事。
    """
    moment = _to_utc(expires_at)
    return moment is not None and moment < now


def row_to_shared(row: Sequence[Any], now: datetime.datetime) -> Dict[str, Any]:
    """SHARED_COLUMNS 的取列顺序 -> 响应契约。字段名一个都不许动。"""
    return {
        "content_id": row[0],
        "scope": row[1],
        "task_id": row[2],
        "kind": row[3],
        "title": row[4],
        "body": row[5],
        "tags": _as_refs(row[6]),
        "owner": row[7],
        "audience": _as_refs(row[8]),
        "sensitivity": row[9],
        "status": row[10],
        "expires_at": _iso(row[11]),
        "review_at": _iso(row[12]),
        "version": row[13],
        "created_at": _iso(row[14]),
        "updated_at": _iso(row[15]),
        "updated_by": row[16],
        "destroyed_at": _iso(row[17]),
        "source_refs": _as_refs(row[18]) if len(row) > 18 else [],
        # 已经标了 expired 的、或者过期时间已过的，都算过期 —— 扫描任务可能还没跑到它
        "expired": row[10] == "expired" or shared_is_expired(row[11], now),
    }


def row_to_shared_task(row: Sequence[Any], now: datetime.datetime) -> Dict[str, Any]:
    return {
        "task_id": row[0],
        "title": row[1],
        "owner": row[2],
        "status": row[3],
        "note": row[4],
        "created_at": _iso(row[5]),
        "ended_at": _iso(row[6]),
        "ttl_at": _iso(row[7]),
        "destroyed_at": _iso(row[8]),
        # 兜底 TTL 到点 = 就算没人来标结束，这个任务下的内容也该被销毁了
        "ttl_expired": shared_is_expired(row[7], now),
    }


def row_to_shared_revision(row: Sequence[Any]) -> Dict[str, Any]:
    """一条改动痕迹。changed 里对正文与敏感字段只有"已变更"，没有值（库里就不记值）。"""
    return {
        "rev_id": row[0],
        "content_id": row[1],
        "action": row[2],
        "actor": row[3],
        "changed": _as_obj(row[4]),
        "note": row[5],
        "ts": _iso(row[6]),
    }


def audience_clause(peer: str, params: List[Any]) -> str:
    """受众过滤：要么写着给所有人（["all"]），要么点名到请求者。

    请求者身份来自 token（_auth_peer），**请求参数里说自己是谁一律不作数** —— 与
    /entries 的防冒名同一套。走 jsonb 的 `@>`，能吃到 shared_content_audience_gin 索引。
    """
    params.append(json.dumps([SHARED_ALL_AUDIENCE]))
    params.append(json.dumps([peer], ensure_ascii=False))
    return "(audience @> %s::jsonb OR audience @> %s::jsonb)"


def shared_filters(peer: str, now: datetime.datetime, scope: Optional[str] = None,
                   kind: Optional[str] = None, owner: Optional[str] = None,
                   tag: Optional[str] = None, task_id: Optional[str] = None,
                   status: Optional[str] = None,
                   expired: bool = False) -> Tuple[List[str], List[Any]]:
    """列表 / 检索共用的过滤条件。全部参数化，一个值都不拼进 SQL。"""
    where = []   # type: List[str]
    params = []  # type: List[Any]
    if scope:
        where.append("scope = %s")
        params.append(scope)
    if kind:
        where.append("kind = %s")
        params.append(kind)
    if owner:
        where.append("owner = %s")
        params.append(owner)
    if task_id:
        where.append("task_id = %s")
        params.append(task_id)
    if tag:
        where.append("tags @> %s::jsonb")
        params.append(json.dumps([tag], ensure_ascii=False))
    if status:
        where.append("status = %s")
        params.append(status)
    else:
        where.append("status NOT IN (%s, %s)")
        params.extend(SHARED_HIDDEN_STATUSES)
        if not expired:
            where.append("status <> %s")
            params.append("expired")
            where.append("(expires_at IS NULL OR expires_at >= %s::timestamptz)")
            params.append(now.isoformat())
    where.append(audience_clause(peer, params))
    return where, params


def select_shared(where: Sequence[str], params: Sequence[Any], limit: int,
                  now: datetime.datetime,
                  degraded: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """共享内容清单，最近更新的排前面。缺表就降级成空列表（库没挂，只是还没建表）。"""
    sql = ("SELECT " + SHARED_COLUMNS + " FROM shared_content WHERE " +
           " AND ".join(where) + " ORDER BY updated_at DESC, content_id DESC LIMIT %s")
    try:
        rows = db_query(sql, list(params) + [limit], "all") or []
    except DBUnavailable as exc:
        if not _is_missing_schema(exc):
            raise
        _note_degraded(degraded, "shared_content")
        return []
    return [row_to_shared(r, now) for r in rows]


def search_shared_semantic(vector: Sequence[float], where: Sequence[str],
                           params: Sequence[Any], limit: int, now: datetime.datetime,
                           degraded: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """按余弦距离排序。**只有 embedding 非 NULL 的行参与**（NULL 排序没有意义）。"""
    vec = _vector_literal(vector)
    sql = ("SELECT " + SHARED_COLUMNS + ", 1 - (embedding <=> %s::vector) AS score "
           "FROM shared_content WHERE " +
           " AND ".join(list(where) + ["embedding IS NOT NULL"]) +
           " ORDER BY embedding <=> %s::vector LIMIT %s")
    try:
        rows = db_query(sql, [vec] + list(params) + [vec, limit], "all") or []
    except DBUnavailable as exc:
        if not _is_missing_schema(exc):
            raise
        _note_degraded(degraded, "shared_content")
        return []
    out = []  # type: List[Dict[str, Any]]
    for row in rows:
        item = row_to_shared(row, now)
        item["score"] = _score(row[19]) if len(row) > 19 else None
        out.append(item)
    return out


def search_shared_keyword(q: str, where: Sequence[str], params: Sequence[Any], limit: int,
                          now: datetime.datetime,
                          degraded: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """降级路径：标题 / 正文 / 标签的子串匹配。score 一律 null（没有可比的相似度）。"""
    pattern = _like_pattern(q)
    sql = ("SELECT " + SHARED_COLUMNS + " FROM shared_content WHERE " +
           " AND ".join(list(where) +
                        ["(title ILIKE %s OR body ILIKE %s OR tags @> %s::jsonb)"]) +
           " ORDER BY updated_at DESC, content_id DESC LIMIT %s")
    values = list(params) + [pattern, pattern, json.dumps([q], ensure_ascii=False), limit]
    try:
        rows = db_query(sql, values, "all") or []
    except DBUnavailable as exc:
        if not _is_missing_schema(exc):
            raise
        _note_degraded(degraded, "shared_content")
        return []
    out = []  # type: List[Dict[str, Any]]
    for row in rows:
        item = row_to_shared(row, now)
        item["score"] = None     # 字段形状与语义分支一致，客户端不用分叉
        out.append(item)
    return out


def has_any_shared_embedding(degraded: Optional[List[str]] = None) -> bool:
    """库里是否至少有一条带向量的共享内容。一条都没有就没必要走语义排序。"""
    try:
        row = db_query(
            "SELECT count(*) FROM shared_content WHERE embedding IS NOT NULL", (), "one")
    except DBUnavailable as exc:
        if not _is_missing_schema(exc):
            raise
        _note_degraded(degraded, "shared_content")
        return False
    return bool(row and row[0])


def select_shared_one(content_id: int, now: datetime.datetime,
                      degraded: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
    """按 id 直取一条（**不带受众过滤**：能不能看由调用方判，好区分 403 与 404）。"""
    try:
        row = db_query("SELECT " + SHARED_COLUMNS + " FROM shared_content "
                       "WHERE content_id = %s", (content_id,), "one")
    except DBUnavailable as exc:
        if not _is_missing_schema(exc):
            raise
        _note_degraded(degraded, "shared_content")
        return None
    return row_to_shared(row, now) if row else None


def select_shared_revisions(content_id: int,
                            degraded: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """一条内容的全部改动痕迹，按时间正序 —— "哪些 Agent 改过"就看这个。"""
    try:
        rows = db_query("SELECT " + SHARED_REVISION_COLUMNS + " FROM shared_content_revisions "
                        "WHERE content_id = %s ORDER BY ts ASC, rev_id ASC LIMIT %s",
                        (content_id, SHARED_REVISION_LIMIT), "all") or []
    except DBUnavailable as exc:
        if not _is_missing_schema(exc):
            raise
        _note_degraded(degraded, "shared_revisions")
        return []
    return [row_to_shared_revision(r) for r in rows]


def select_shared_tasks(status: Optional[str], limit: int, now: datetime.datetime,
                        degraded: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """任务容器清单（任务级共享内容的生命周期由它决定）。缺表降级成空列表。"""
    where = []   # type: List[str]
    params = []  # type: List[Any]
    if status:
        where.append("status = %s")
        params.append(status)
    sql = "SELECT " + SHARED_TASK_COLUMNS + " FROM shared_tasks"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC, task_id DESC LIMIT %s"
    try:
        rows = db_query(sql, params + [limit], "all") or []
    except DBUnavailable as exc:
        if not _is_missing_schema(exc):
            raise
        _note_degraded(degraded, "shared_tasks")
        return []
    return [row_to_shared_task(r, now) for r in rows]


def shared_visible_to(item: Dict[str, Any], peer: str) -> bool:
    """这条内容给不给这个成员看：audience 写着 all，或者点名到他。

    audience 空数组（谁也没写）一律**不给看** —— 拿不准就别给，与"宁可少标一个缺陷"
    同一个保守方向。
    """
    audience = item.get("audience") or []
    return SHARED_ALL_AUDIENCE in audience or peer in audience


# ---------------------------------------------------------------- 业务：临时共享区（tmp box）
#
# 契约 = 临时共享区-设计与契约-v1.md。这里只放 SQL 与口径，HTTP 分发在 handler 里。
#
# 两条销毁路（都是"改写标记 + 清空正文"，**绝不删行**）：
#   1. 读时懒判 + 读满即时销毁（主路径）：过期的在读的同一个请求里顺手销毁；
#      第 reads_max 个不同 reader 读到时，正文照给、同一次 UPDATE 里销毁，响应带 destroyed=true。
#   2. 进程内 sweep 线程（兜底，每 300s）：把"没人再来读"的过期行扫掉 + 全局容量兜底。
# 读计数按 **distinct reader** 去重：群发是"一份材料给 N 个成员各读一次"，
# 按请求计数会让任一成员的重试/刷新吃掉配额 → 提前销毁（是 bug）。

TMP_COLUMNS = ("tmp_id, owner, audience, title, body, bytes, digest, reads_max, "
               "reads_count, readers, expires_at, created_at, destroyed_at, destroy_reason")

TMP_INSERT_SQL = (
    "INSERT INTO tmp_shared (tmp_id, owner, audience, title, body, bytes, digest, "
    "reads_max, expires_at) "
    "VALUES (%s, %s, %s::text[], %s, %s, %s, %s, %s, now() + make_interval(secs => %s::double precision)) "
    "RETURNING expires_at"
)

# 活跃条数 = 未销毁 **且** 未过期（已过期但还没扫到的，不算占配额）
TMP_OWNER_ACTIVE_SQL = ("SELECT count(*) FROM tmp_shared WHERE owner = %s "
                        "AND destroyed_at IS NULL AND expires_at > now()")
TMP_ACTIVE_COUNT_SQL = "SELECT count(*) FROM tmp_shared WHERE destroyed_at IS NULL"

TMP_SELECT_ONE_SQL = "SELECT " + TMP_COLUMNS + " FROM tmp_shared WHERE tmp_id = %s"

# 计数读：**一条 UPDATE 里同时**做「记一个 reader」和「读满就销毁」，所以它天然是原子的。
# body 先由 SELECT 拿到手（响应照给全文），这条 UPDATE 负责把库里的正文清掉。
TMP_READ_SQL = (
    "UPDATE tmp_shared SET "
    "readers = array_append(readers, %s::text), "
    "reads_count = reads_count + 1, "
    "destroyed_at = CASE WHEN reads_count + 1 >= reads_max THEN now() ELSE destroyed_at END, "
    "destroy_reason = CASE WHEN reads_count + 1 >= reads_max THEN 'read_quota' ELSE destroy_reason END, "
    "body = CASE WHEN reads_count + 1 >= reads_max THEN '' ELSE body END "
    "WHERE tmp_id = %s AND destroyed_at IS NULL AND NOT (%s = ANY(readers)) "
    "RETURNING reads_count, reads_max, destroyed_at, destroy_reason"
)

# 读时懒判：已经过期但还没被扫到 → 同一个请求里顺手销毁（不等 sweep）
TMP_EXPIRE_ONE_SQL = (
    "UPDATE tmp_shared SET body = '', destroyed_at = now(), destroy_reason = 'expired' "
    "WHERE tmp_id = %s AND destroyed_at IS NULL AND expires_at <= now() "
    "RETURNING destroyed_at"
)
# owner 主动销毁
TMP_DESTROY_SQL = (
    "UPDATE tmp_shared SET body = '', destroyed_at = now(), destroy_reason = 'owner' "
    "WHERE tmp_id = %s AND destroyed_at IS NULL RETURNING destroyed_at"
)
# sweep：把过期的清掉
TMP_SWEEP_SQL = (
    "UPDATE tmp_shared SET body = '', destroyed_at = now(), destroy_reason = 'expired' "
    "WHERE destroyed_at IS NULL AND expires_at <= now() RETURNING tmp_id"
)
# 容量兜底：全局活跃 > TMP_ACTIVE_MAX 时，按 expires_at 最早优先销毁
TMP_CAPACITY_SQL = (
    "UPDATE tmp_shared SET body = '', destroyed_at = now(), destroy_reason = 'capacity' "
    "WHERE tmp_id IN (SELECT tmp_id FROM tmp_shared WHERE destroyed_at IS NULL "
    "ORDER BY expires_at ASC LIMIT %s) RETURNING tmp_id"
)


def new_tmp_id() -> str:
    """'t-' + 8 字符 base32。5 字节随机 = 40 bit，短到能塞进载荷，也够宽不撞。"""
    raw = base64.b32encode(os.urandom(5)).decode("ascii").rstrip("=")
    return TMP_ID_PREFIX + raw[:TMP_ID_LEN]


def tmp_id_ok(tmp_id: Any) -> bool:
    """id 形状先卡一道：形状不对就是"没有这条"，连库都不用碰。"""
    return bool(isinstance(tmp_id, str) and TMP_ID_RE.match(tmp_id))


def known_members() -> List[str]:
    """认得出来的成员名（peers.json 的身份集合）—— `to` 里出现别的名字一律 400。"""
    return sorted({str(m) for m in Runtime.peer_map.values()})


def _iso_z(value: Any) -> Any:
    """timestamptz -> '...Z' 的 ISO8601（契约里的示例就是这个形状）。"""
    if isinstance(value, datetime.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=datetime.timezone.utc)
        return value.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return value


def row_to_tmp(row: Sequence[Any], now: datetime.datetime,
               with_body: bool = True) -> Dict[str, Any]:
    """tmp_shared 一行 -> 响应里的那份字典。with_body=False 给列表用（列表不吐正文）。"""
    (tmp_id, owner, audience, title, body, nbytes, digest, reads_max, reads_count,
     readers, expires_at, created_at, destroyed_at, destroy_reason) = row[:14]
    expires_dt = _to_utc(expires_at)
    reads_max = int(reads_max or 0)
    reads_count = int(reads_count or 0)
    item = {
        "tmp_id": tmp_id,
        "owner": owner,
        "audience": _as_names(audience),
        "title": title,
        "bytes": nbytes,
        "digest": digest,
        "reads_max": reads_max,
        "reads_count": reads_count,
        "reads_left": max(0, reads_max - reads_count),
        "readers": _as_names(readers),
        "expires_at": _iso_z(expires_at),
        "created_at": _iso_z(created_at),
        "destroyed_at": _iso_z(destroyed_at),
        "destroy_reason": destroy_reason,
        "destroyed": destroyed_at is not None,
        "expired": bool(expires_dt is not None and expires_dt <= now),
    }
    if with_body:
        item["body"] = body
    return item


def select_tmp(tmp_id: str) -> Optional[Dict[str, Any]]:
    """按 id 取一行（含正文），没有就是 None。"""
    now = utc_now()
    row = db_query(TMP_SELECT_ONE_SQL, (tmp_id,), "one")
    if row is None:
        return None
    return row_to_tmp(row, now)


def tmp_owner_active_count(owner: str) -> int:
    row = db_query(TMP_OWNER_ACTIVE_SQL, (owner,), "one")
    return int(row[0]) if row else 0


def insert_tmp(owner: str, audience: Sequence[str], title: Optional[str],
               body: str, reads_max: int, ttl_s: int) -> Dict[str, Any]:
    """写一条。tmp_id 撞了就重摇 —— 主键在库里，撞了会抛 duplicate key。"""
    raw_body = body.encode("utf-8")
    digest = hashlib.sha256(raw_body).hexdigest()
    last = None  # type: Optional[Exception]
    for _ in range(5):
        tmp_id = new_tmp_id()
        try:
            row = db_query(TMP_INSERT_SQL,
                           (tmp_id, owner, list(audience), title, body,
                            len(raw_body), digest, reads_max, ttl_s), "one")
        except DBUnavailable as exc:
            if "duplicate key" in str(exc).lower():
                last = exc
                continue
            raise
        return {"tmp_id": tmp_id,
                "expires_at": _iso_z(row[0] if row else None),
                "reads_max": reads_max,
                "bytes": len(raw_body),
                "digest": digest}
    raise DBUnavailable("tmp_id 连续 5 次撞主键，放弃: %s" % _safe_err(last))


def tmp_counted_read(tmp_id: str, peer: str
                     ) -> Optional[Tuple[int, int, bool, Optional[str]]]:
    """记一次「不同 reader 的读」，返回 (reads_count, reads_max, 本次是否读满销毁, 销毁时间)。

    已经读过的人 / 已经被销毁的行都**不**匹配这条 UPDATE → 返回 None（调用方据此回 repeat）。
    """
    row = db_query(TMP_READ_SQL, (peer, tmp_id, peer), "one")
    if row is None:
        return None
    reads_count, reads_max, destroyed_at, _reason = row[:4]
    return int(reads_count), int(reads_max), destroyed_at is not None, _iso_z(destroyed_at)


def tmp_expire_now(tmp_id: str) -> Optional[str]:
    """读时懒判的过期销毁。返回本次销毁时间；已经是销毁态则返回 None（幂等）。"""
    row = db_query(TMP_EXPIRE_ONE_SQL, (tmp_id,), "one")
    return _iso_z(row[0]) if row else None


def tmp_destroy_by_owner(tmp_id: str) -> Optional[str]:
    """owner 主动销毁。返回销毁时间；已经是销毁态则返回 None（调用方回幂等 200）。"""
    row = db_query(TMP_DESTROY_SQL, (tmp_id,), "one")
    return _iso_z(row[0]) if row else None


def tmp_enforce_capacity() -> int:
    """全局容量兜底：活跃条数超 TMP_ACTIVE_MAX 就按 expires_at 最早优先销毁（reason=capacity）。"""
    row = db_query(TMP_ACTIVE_COUNT_SQL, (), "one")
    active = int(row[0]) if row else 0
    over = active - TMP_ACTIVE_MAX
    if over <= 0:
        return 0
    rows = db_query(TMP_CAPACITY_SQL, (over,), "all") or []
    return len(rows)


# ================================================================ msg_log 校验
def validate_msg_payload(body: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], str, int]:
    """校验 POST /msg 的载荷，返回 (payload, 错误说明, HTTP 码)。

    注意这里**不看 from**：发送方身份一律来自 Bearer token 反查，
    请求体里给了也当没看见（与 _tmp_write 对 owner 的处理同一套）。
    """
    to = body.get("to")
    if not isinstance(to, str) or not to.strip():
        return None, "to 必填（收件成员名）", 400
    to = to.strip()
    if len(to) > MSG_MEMBER_MAX:
        return None, "to 超长（上限 %d）" % MSG_MEMBER_MAX, 400

    text = body.get("text", body.get("body"))
    ref = body.get("content_ref")
    if text is None and ref is None:
        return None, "text 与 content_ref 至少给一个（长文本请走 content_ref 临时共享区）", 400
    if text is not None:
        if not isinstance(text, str):
            return None, "text 必须是字符串", 400
        if len(text.encode("utf-8")) > MSG_BODY_MAX:
            return None, "text 超过 %d 字节（长文本请走 content_ref 临时共享区）" % MSG_BODY_MAX, 413
    if ref is not None and not isinstance(ref, dict):
        return None, "content_ref 必须是对象", 400

    topic = body.get("topic") or MSG_DEFAULT_TOPIC
    if not isinstance(topic, str) or not topic.strip() or len(topic) > MSG_TOPIC_MAX:
        return None, "topic 非法（非空字符串，长度上限 %d）" % MSG_TOPIC_MAX, 400

    session_id = body.get("session_id")
    if session_id is not None and not isinstance(session_id, str):
        return None, "session_id 必须是字符串", 400

    ttl = body.get("ttl_s", MSG_DEFAULT_TTL_S)
    if isinstance(ttl, bool) or not isinstance(ttl, int):
        return None, "ttl_s 必须是整数", 400
    if ttl < MSG_MIN_TTL_S or ttl > MSG_MAX_TTL_S:
        return None, "ttl_s 必须在 %d..%d 之间" % (MSG_MIN_TTL_S, MSG_MAX_TTL_S), 400

    mid = body.get("msg_id")
    if mid is not None and not msg_id_ok(mid):
        return None, "msg_id 形状不合法（应为 m-XXXXXXXX）", 400

    return {"to": to, "body": text, "content_ref": ref, "session_id": session_id,
            "topic": topic.strip(), "ttl_s": ttl, "msg_id": mid}, "", 200


# ================================================================ msg_log 数据访问
def new_msg_id() -> str:
    """'m-' + 8 字符 base32（与 /tmp、/sess 同一套摇号，短到能塞进载荷）。"""
    raw = base64.b32encode(os.urandom(5)).decode("ascii").rstrip("=")
    return MSG_ID_PREFIX + raw[:MSG_ID_LEN]


def msg_id_ok(msg_id: Any) -> bool:
    """id 形状先卡一道：形状不对就是「没有这条」，连库都不用碰。"""
    return isinstance(msg_id, str) and bool(MSG_ID_RE.match(msg_id))


def row_to_msg(row: Sequence[Any]) -> Dict[str, Any]:
    """msg_log 行 -> 对外 dict（列顺序与 MSG_COLUMNS 严格一致；时间统一 ISO8601 Z）。"""
    (msg_id, seq, from_member, to_member, session_id, topic, body, content_ref,
     state, attempts, last_error, created_at, updated_at, acked_at, done_at,
     expires_at) = row
    return {
        "msg_id": msg_id,
        "seq": int(seq) if seq is not None else None,
        "from": from_member,
        "to": to_member,
        "session_id": session_id,
        "topic": topic,
        "body": body,
        "content_ref": content_ref,
        "state": state,
        "attempts": int(attempts or 0),
        "last_error": last_error,
        "created_at": _iso_z(created_at),
        "updated_at": _iso_z(updated_at),
        "acked_at": _iso_z(acked_at),
        "done_at": _iso_z(done_at),
        "expires_at": _iso_z(expires_at),
        # 两个给人看的语义字段：发送方最关心的就是这两个
        "acked": state in ("processing", "done"),      # 对方「已收到并开始处理」
        "finished": state in MSG_TERMINAL_STATES,      # 已经有结论了
    }


def insert_msg(from_member: str, to_member: str, body: Optional[str],
               content_ref: Optional[Dict[str, Any]], session_id: Optional[str],
               topic: str, ttl_s: int, msg_id: Optional[str] = None) -> Dict[str, Any]:
    """落库一条消息。**落库成功 = 送达**：返回即代表中枢已持有，对方离线也不丢。

    幂等：同一个 msg_id 重复写不会产生第二行 —— 网络重试对调用方是安全的。
    from_member 由调用方从**鉴权结果**传入，绝不来自请求体（防冒名）。
    """
    ref_json = json.dumps(content_ref, ensure_ascii=False) if content_ref else None
    last = None  # type: Optional[str]
    for _ in range(MSG_ID_TRY):
        candidate = msg_id or new_msg_id()
        row = db_query(MSG_INSERT_SQL,
                       (candidate, from_member, to_member, session_id, topic,
                        body, ref_json, int(ttl_s)), "one")
        if row is not None:
            return {"msg_id": candidate, "seq": int(row[0]),
                    "created_at": _iso_z(row[1]), "expires_at": _iso_z(row[2]),
                    "duplicate": False}
        # 走到这儿 = 撞了已有 msg_id（ON CONFLICT DO NOTHING 没返回行）
        if msg_id:
            existing = get_msg(msg_id)
            if existing is not None:
                existing["duplicate"] = True       # 调用方据此知道「这条我之前就发过了」
                return existing
        last = candidate
    raise DBUnavailable("msg_id 连续 %d 次撞主键，放弃（最后一次 %s）" % (MSG_ID_TRY, last))


def get_msg(msg_id: str) -> Optional[Dict[str, Any]]:
    """按 id 取一条（发送方靠它查「我那条到底送没送到、对方开没开工」）。"""
    row = db_query(MSG_SELECT_ONE_SQL, (msg_id,), "one")
    return row_to_msg(row) if row else None


def list_inbox(to_member: str, states: Sequence[str], limit: int) -> List[Dict[str, Any]]:
    """收件箱：某人还欠着的消息，按 seq 升序（对方上线时照这个补拉）。"""
    rows = db_query(MSG_INBOX_SQL, (to_member, list(states), int(limit)), "all") or []
    return [row_to_msg(r) for r in rows]


def msg_ack(msg_id: str) -> Optional[Dict[str, Any]]:
    """queued -> processing，落 acked_at。这就是对方在说「已收到并开始处理」。

    不是 queued（已 ack 过 / 已终态 / 不存在）→ 返回 None。
    幂等靠那条 UPDATE 的 `AND state = 'queued'` 乐观锁：并发重复 ack 只会成功一次。
    """
    row = db_query(MSG_ACK_SQL, (msg_id,), "one")
    return row_to_msg(row) if row else None


def msg_finish(msg_id: str, to_state: str,
               error: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """processing -> done|failed，落 done_at。不是 processing 就返回 None。"""
    row = db_query(MSG_FINISH_SQL, (to_state, to_state, error, msg_id), "one")
    return row_to_msg(row) if row else None


def msg_stats() -> Dict[str, int]:
    """各状态计数（给 /healthz 自报）。**任何异常都吞掉返回空**。

    自报字段绝不能把健康口带崩（与 embedder.status() 同一哲学：库挂了 ok 该是 false，
    但 /healthz 本身必须答得出来 —— 否则监控只能看到"连接被断"，看不出是哪一块坏）。
    真实环境里"表还没建"是 DBUnavailable，但假库/半初始化状态可能是别的异常，一律不追究。
    """
    try:
        rows = db_query(MSG_STATS_SQL, (), "all") or []
        return dict((str(r[0]), int(r[1])) for r in rows)
    except Exception as exc:                     # 自报口故意兜底
        LOG.debug("msg_stats 取不到（不影响 healthz）: %s", _safe_err(exc))
        return {}


def msg_sweep_once() -> int:
    """跑一轮 sweep：到期未取 → expired；processing 卡死 → stalled。

    **绝不自动重投**：卡死的只标 stalled 并告警，把「要不要重试」留给人和调用方。
    at-least-once ≠ exactly-once —— 自动重投会把同一条再喂一遍 LLM，产生重复副作用。
    """
    n = 0
    try:
        rows = db_query(MSG_SWEEP_EXPIRE_SQL, (), "all") or []
        n += len(rows)
        stuck = db_query(MSG_SWEEP_STALL_SQL,
                         (MSG_STALL_AFTER_S, MSG_STALL_AFTER_S), "all") or []
        if stuck:
            LOG.warning("msg_log：%d 条 processing 卡死超过 %d 秒，已标 stalled"
                        "（待人工确认，未自动重投）",
                        len(stuck), MSG_STALL_AFTER_S)
        n += len(stuck)
    except DBUnavailable as exc:
        if not _is_missing_schema(exc):
            raise
        return 0        # 表还没建（schema 还没执行）：静默跳过，别把 sweep 线程带崩
    Runtime.msg_sweeps += 1
    Runtime.msg_swept_at = _iso_z(utc_now())
    Runtime.msg_sweep_error = None
    return n


def tmp_sweep_once() -> int:
    """跑一轮兜底 sweep：过期的 + 容量超额的。异常只记日志，绝不把服务带崩。"""
    destroyed = 0
    try:
        rows = db_query(TMP_SWEEP_SQL, (), "all") or []
        destroyed += len(rows)
        destroyed += tmp_enforce_capacity()
    except DBUnavailable as exc:
        Runtime.tmp_sweep_error = _safe_err(exc)
        LOG.warning("tmp 兜底 sweep 失败: %s", _safe_err(exc))
        return destroyed
    Runtime.tmp_sweep_error = None
    Runtime.tmp_sweeps += 1
    Runtime.tmp_swept_at = _iso_z(utc_now())
    if destroyed:
        LOG.info("tmp 兜底 sweep 销毁 %d 条", destroyed)
    return destroyed


def tmp_sweep_loop(interval_s: int = TMP_SWEEP_INTERVAL_S) -> None:
    """9940 进程内的 daemon 线程：**每 300s** 一轮，一轮里跑两块兜底。

    选服务内线程而不是新加 systemd timer：自包含、不新增部署单元、与服务同生命周期。
    启动时先立刻跑一轮（否则重启后要等 300s 才轮到第一轮，/healthz 的 tmp_swept_at 会一直空着）。

    v1.7 起这一轮里还跑 **Agent 会话的空闲超时 sweep**（契约 D1/§4.3 明确要求：
    复用本进程已有的这个线程，不新增定时单元/service）。
    两块各自 try：会话 sweep 挂了不能把 tmp 的兜底也带下水，反之亦然。
    """
    while True:
        try:
            tmp_sweep_once()
        except Exception as exc:                      # 兜底中的兜底：线程不能死
            runtime_error = _safe_err(exc)
            Runtime.tmp_sweep_error = runtime_error
            LOG.warning("tmp 兜底 sweep 线程异常: %s", runtime_error)
        try:
            sess_sweep_once()
        except Exception as exc:                      # 同上：会话 sweep 也不能把线程带走
            sess_error = _safe_err(exc)
            Runtime.sess_sweep_error = sess_error
            LOG.warning("session 兜底 sweep 线程异常: %s", sess_error)
        try:
            msg_sweep_once()                          # v1.8：消息到期/卡死收口（**绝不自动重投**）
        except Exception as exc:                      # 同上：三块互不连坐
            msg_error = _safe_err(exc)
            Runtime.msg_sweep_error = msg_error
            LOG.warning("msg 兜底 sweep 线程异常: %s", msg_error)
        time.sleep(max(1, interval_s))


# ---------------------------------------------------------------- 业务：Agent 会话（/sess）
#
# 契约：Agent会话-session-设计与契约-v1.md（表 agent_sessions / agent_session_turns，
# 路径 /sess；设计裁决 D1..D8 见契约 §3）。
#
# 这一块解决的是"多轮对话忘记自己刚承诺过什么"：会话开一次、每轮留一条流水（append-only）、
# 关或空闲超时就把流水归档成一份 markdown 存进 shared_content，会话本身变成只读。
# **中枢统一记，Agent 侧无状态**（D3）：成员机上不放历史。
#
# 归档的三条纪律（照抄既有那套，不另造）：
#   1. 归档 ≠ 删除（D6）：只落 state/closed_at/close_reason/archive_ref，行永远保留；
#      归档后 turn 正文"清空留行"（content=''），查得着"谁在什么时候说了话"。
#   2. 关闭与空闲超时走**同一条**归档路径，只有 close_reason 不同（manual / idle）。
#   3. 状态先认领（一条 UPDATE ... WHERE state='open' 原子落终态）再归档：并发的两个关闭
#      只有一个能认领成功，绝不会把同一场会话归档两遍。归档失败不回退状态 ——
#      状态已经落了，sweep 每轮还会补扫"已终态但 archive_ref 为空"的会话，直到归档成功。

SESS_COLUMNS = ("session_id, topic, title, owner, members, state, created_at, last_turn_at, "
                "idle_ttl_s, turn_count, closed_at, close_reason, archive_ref, note")

SESS_TURN_COLUMNS = ("turn_id, session_id, seq, from_member, to_member, role, content, "
                     "content_ref, bytes, ts")

# ================================================================ msg_log（消息可靠投递）
# 契约：消息可靠性与会话-测试用例与接口契约-v1.md（表 sql/msg_log_schema.sql）
MSG_COLUMNS = ("msg_id, seq, from_member, to_member, session_id, topic, body, content_ref, "
               "state, attempts, last_error, created_at, updated_at, acked_at, done_at, expires_at")

# 落库。**落库成功 = 送达**：这一条 INSERT 成功，消息就归中枢保管，对方离线也不丢。
# ON CONFLICT DO NOTHING：调用方可以拿同一个 msg_id 安全重试（幂等）。
MSG_INSERT_SQL = (
    "INSERT INTO msg_log (msg_id, from_member, to_member, session_id, topic, body, content_ref, "
    "expires_at) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, now() + make_interval(secs => %s::double precision)) "
    "ON CONFLICT (msg_id) DO NOTHING "
    "RETURNING seq, created_at, expires_at"
)

MSG_SELECT_ONE_SQL = "SELECT " + MSG_COLUMNS + " FROM msg_log WHERE msg_id = %s"

# 收件箱：某人所有还欠着的消息（对方一上线就按这个补拉）。按 seq 排序（不按 msg_id）。
MSG_INBOX_SQL = ("SELECT " + MSG_COLUMNS + " FROM msg_log WHERE to_member = %s "
                 "AND state = ANY(%s::text[]) ORDER BY seq LIMIT %s")

# 发件箱：某人发过的（审计 + 发送方自己查「我发出去的那条现在怎么样了」）
MSG_OUTBOX_SQL = ("SELECT " + MSG_COLUMNS + " FROM msg_log WHERE from_member = %s "
                  "ORDER BY seq DESC LIMIT %s")

# ack：queued -> processing，同时落 acked_at。带 state 条件 = 乐观锁，
# 并发重复 ack 只会成功一次（第二次匹配不到行，返回空）。
MSG_ACK_SQL = (
    "UPDATE msg_log SET state = 'processing', acked_at = now(), attempts = attempts + 1 "
    "WHERE msg_id = %s AND state = 'queued' "
    "RETURNING " + MSG_COLUMNS
)

# 收尾：processing -> done/failed，落 done_at。同样带 state 条件。
MSG_FINISH_SQL = (
    "UPDATE msg_log SET state = %s, "
    "done_at = CASE WHEN %s IN ('done','failed') THEN now() ELSE done_at END, "
    "last_error = COALESCE(%s, last_error), attempts = attempts + 1 "
    "WHERE msg_id = %s AND state IN ('queued','processing') "
    "RETURNING " + MSG_COLUMNS
)

# sweep 之一：到期还没被取走 → expired（**不删行**，留痕）
MSG_SWEEP_EXPIRE_SQL = (
    "UPDATE msg_log SET state = 'expired', last_error = 'expired: 到期仍未被取走' "
    "WHERE state IN ('queued','processing') AND expires_at <= now() "
    "RETURNING msg_id"
)

# sweep 之二：processing 卡死 → stalled + 告警（**绝不回 queued 自动重投**）
MSG_SWEEP_STALL_SQL = (
    "UPDATE msg_log SET state = 'stalled', "
    "last_error = 'processing 卡死超过 %s 秒，已标记待人工确认（不自动重投）' "
    "WHERE state = 'processing' "
    "AND updated_at <= now() - make_interval(secs => %s::double precision) "
    "RETURNING msg_id"
)

MSG_STATS_SQL = "SELECT state, count(*) FROM msg_log GROUP BY state"

SESS_INSERT_SQL = (
    "INSERT INTO agent_sessions (session_id, topic, title, owner, members, idle_ttl_s) "
    "VALUES (%s, %s, %s, %s, %s::jsonb, %s) "
    "RETURNING " + SESS_COLUMNS
)

SESS_SELECT_ONE_SQL = "SELECT " + SESS_COLUMNS + " FROM agent_sessions WHERE session_id = %s"

# 追加一轮流水（契约 §5）。**seq / bytes / ts 一律由库里的触发器给**：应用层只递 6 个
# 业务字段 —— 自己算 seq 在并发下必然撞车，而且会和触发器`s 的取值打架（单一权威）。
# content 与 content_ref 至少给一个；content_ref 是 jsonb 列，显式转型。
SESS_TURN_INSERT_SQL = (
    "INSERT INTO agent_session_turns (session_id, from_member, to_member, role, content, "
    "content_ref) VALUES (%s, %s, %s, %s, %s, %s::jsonb) RETURNING " + SESS_TURN_COLUMNS
)

SESS_TURN_CONTENT_MAX = MAX_BODY_BYTES   # 正文上限与请求体上限同一口径，不另造一个数

SESS_SELECT_TURNS_SQL = ("SELECT " + SESS_TURN_COLUMNS + " FROM agent_session_turns "
                         "WHERE session_id = %s ORDER BY seq DESC LIMIT %s")

SESS_SELECT_ALL_TURNS_SQL = ("SELECT " + SESS_TURN_COLUMNS + " FROM agent_session_turns "
                             "WHERE session_id = %s ORDER BY seq ASC")

# 关闭/超时：一条 UPDATE 里**同时**认领状态机迁移，所以它天然是原子的。
SESS_CLAIM_SQL = (
    "UPDATE agent_sessions SET state = %s, closed_at = now(), close_reason = %s, "
    "note = COALESCE(%s, note) "
    "WHERE session_id = %s AND state = 'open' "
    "RETURNING " + SESS_COLUMNS
)

# 空闲超时（D1）：判据是 now() - last_turn_at > idle_ttl_s。
# last_turn_at 为 NULL（开了会话一条都没说）时退到 created_at —— 否则"开了就不说话"的
# 会话永远关不掉；契约只写了 last_turn_at，这一条是本实现的兜底口径（验收里写明）。
SESS_IDLE_SWEEP_SQL = (
    "UPDATE agent_sessions "
    "SET state = 'expired', closed_at = now(), close_reason = 'idle' "
    "WHERE state = 'open' "
    "AND now() - COALESCE(last_turn_at, created_at) > make_interval(secs => idle_ttl_s) "
    "RETURNING session_id"
)

# 补扫：已经终态但归档没落成的（比如归档那一刻库抖了一下）——每轮重试直到 archive_ref 有值。
SESS_UNARCHIVED_SQL = (
    "SELECT session_id FROM agent_sessions "
    "WHERE state IN ('closed', 'expired') AND archive_ref IS NULL "
    "ORDER BY COALESCE(closed_at, created_at) ASC LIMIT %s"
)

SESS_SET_ARCHIVE_SQL = (
    "UPDATE agent_sessions SET archive_ref = %s "
    "WHERE session_id = %s AND archive_ref IS NULL RETURNING archive_ref"
)

# 归档后清正文留行（D6）。只动 content；触发器只放行这一种 UPDATE。
SESS_CLEAR_TURNS_SQL = (
    "UPDATE agent_session_turns SET content = '' "
    "WHERE session_id = %s AND content IS NOT NULL AND content <> ''"
)

# 归档文档落到共享内容平面：**任务级 scope**（契约 §4.3）。
# scope='task' 在库里有硬 CHECK：task_id 与 expires_at 都必须非空 —— 所以先确保有一个
# 归档任务容器（task_id = 'sess-archive-' + session_id），再挂文档上去。
SESS_ARCHIVE_TASK_SQL = (
    "INSERT INTO shared_tasks (task_id, title, owner, status, ttl_at) "
    "VALUES (%s, %s, %s, 'active', now() + make_interval(days => %s::int)) "
    "ON CONFLICT (task_id) DO NOTHING"
)

SESS_ARCHIVE_CONTENT_SQL = (
    "INSERT INTO shared_content (scope, task_id, kind, title, body, tags, owner, audience, "
    "sensitivity, status, expires_at, source_refs, updated_by, updated_at, embedding) "
    "VALUES ('task', %s, %s, %s, %s, %s::jsonb, %s, %s::jsonb, 'internal', 'active', "
    "now() + make_interval(days => %s::int), %s::jsonb, %s, now(), %s::vector) "
    "RETURNING content_id"
)


def new_session_id(now: Optional[datetime.datetime] = None) -> str:
    """'as-YYYYMMDD-HHMMSS-xxxx'（UTC）。后缀走 base32 小写字母表（a-z2-7），与 id 正则同源。"""
    now = now or utc_now()
    raw = base64.b32encode(os.urandom(3)).decode("ascii").rstrip("=").lower()
    return "%s%s-%s" % (SESS_ID_PREFIX, now.strftime("%Y%m%d-%H%M%S"), raw[:SESS_ID_RAND_LEN])


def session_id_ok(session_id: Any) -> bool:
    """id 形状先卡一道：形状不对就是"没有这个会话"，连库都不用碰。"""
    return bool(isinstance(session_id, str) and SESS_ID_RE.match(session_id))


def row_to_agent_session(row: Sequence[Any]) -> Dict[str, Any]:
    """agent_sessions 一行 -> 响应里的那份字典（字段名与契契约 §4.1 同名）。"""
    (session_id, topic, title, owner, members, state, created_at, last_turn_at,
     idle_ttl_s, turn_count, closed_at, close_reason, archive_ref, note) = row[:14]
    return {
        "session_id": session_id,
        "topic": topic,
        "title": title,
        "owner": owner,
        "members": _as_names(members),
        "state": state,
        "created_at": _iso_z(created_at),
        "last_turn_at": _iso_z(last_turn_at),
        "idle_ttl_s": int(idle_ttl_s) if idle_ttl_s is not None else None,
        "turn_count": int(turn_count or 0),
        "closed_at": _iso_z(closed_at),
        "close_reason": close_reason,
        "archive_ref": int(archive_ref) if archive_ref is not None else None,
        "note": note,
        "open": state == "open",
    }


def row_to_agent_turn(row: Sequence[Any]) -> Dict[str, Any]:
    """agent_session_turns 一行 -> 响应里的那份字典。归档后 content 为空是**预期**的（D6）。"""
    (turn_id, session_id, seq, from_member, to_member, role, content, content_ref,
     nbytes, ts) = row[:10]
    return {
        "turn_id": int(turn_id) if turn_id is not None else None,
        "session_id": session_id,
        "seq": int(seq) if seq is not None else None,
        "from_member": from_member,
        "to_member": to_member,
        "role": role,
        "content": content,
        "content_ref": content_ref,
        "bytes": int(nbytes) if nbytes is not None else None,
        "ts": _iso_z(ts),
        "content_cleared": content is None or content == "",
    }


def sess_visible_to(session: Dict[str, Any], peer: str) -> bool:
    """这个成员能不能看/关这个会话：发起方，或者点名在 members 里。其余 403。"""
    if session.get("owner") == peer:
        return True
    return peer in (session.get("members") or [])


def select_agent_sessions(state: Optional[str], topic: Optional[str], limit: int,
                          since: Optional[str]) -> List[Dict[str, Any]]:
    """GET /sess 的列表口径，与会议 GET /sessions 对齐：state / topic / since / limit，created_at 倒序。"""
    where = []    # type: List[str]
    params = []   # type: List[Any]
    if state:
        where.append("state = %s")
        params.append(state)
    if topic:
        where.append("topic = %s")
        params.append(topic)
    if since:
        where.append("created_at >= %s::timestamptz")
        params.append(since)
    sql = "SELECT " + SESS_COLUMNS + " FROM agent_sessions"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC, session_id DESC LIMIT %s"
    params.append(limit)
    rows = db_query(sql, params, "all") or []
    return [row_to_agent_session(r) for r in rows]


def select_agent_session(session_id: str) -> Optional[Dict[str, Any]]:
    row = db_query(SESS_SELECT_ONE_SQL, (session_id,), "one")
    if row is None:
        return None
    return row_to_agent_session(row)


def select_agent_turns(session_id: str, limit: int) -> List[Dict[str, Any]]:
    """最近 limit 轮，返回**按 seq 升序**（读起来是一条顺着的时间线）。"""
    rows = db_query(SESS_SELECT_TURNS_SQL, (session_id, limit), "all") or []
    return [row_to_agent_turn(r) for r in reversed(list(rows))]


def select_all_agent_turns(session_id: str) -> List[Dict[str, Any]]:
    """全量流水（归档用）。不设上限：归档漏掉尾巴就等于把"它说过的话"删了。"""
    rows = db_query(SESS_SELECT_ALL_TURNS_SQL, (session_id,), "all") or []
    return [row_to_agent_turn(r) for r in rows]


def insert_agent_turn(session_id: str, from_member: str, to_member: Optional[str],
                      role: str, content: str, content_ref: Any) -> Dict[str, Any]:
    """追加一轮流水，返回落库后的那一行（seq / bytes / ts 都是库里给的）。

    库里父会话非 open 态就 RAISE —— 这里翻成 SessionNotOpen，端点据此回 409。
    断言"是不是终态"这件事**只有库能说了算**（应用层先 select 再 insert 中间有窗口，
    并发下会把话说进已经归档的会话里），所以判定权交给触发器，我们只翻译异常。
    """
    if content_ref is not None and not isinstance(content_ref, str):
        content_ref = json.dumps(content_ref, ensure_ascii=False)
    try:
        row = db_query(SESS_TURN_INSERT_SQL,
                       (session_id, from_member, to_member, role, content, content_ref),
                       "one")
    except DBUnavailable as exc:
        low = str(exc).lower()
        if "closed" in low or "expired" in low or "terminal" in low \
                or "not open" in low or "不是 open" in str(exc):
            raise SessionNotOpen(_safe_err(exc))
        raise
    if not row:
        raise DBUnavailable("turn insert 没有返回行")
    return row_to_agent_turn(row)


def insert_agent_session(owner: str, topic: str, title: Optional[str],
                         members: Sequence[str], idle_ttl_s: int) -> Dict[str, Any]:
    """开一个会话。主键撞了就重摇（会话 id 形如 as-<秒>-<4位随机>，同秒并发才会撞）。"""
    last = None  # type: Optional[Exception]
    for _ in range(SESS_ID_TRY):
        session_id = new_session_id()
        try:
            row = db_query(SESS_INSERT_SQL,
                           (session_id, topic, title, owner,
                            json.dumps(list(members), ensure_ascii=False), idle_ttl_s),
                           "one")
        except DBUnavailable as exc:
            if "duplicate key" in str(exc).lower():
                last = exc
                continue
            raise
        if not row:
            raise DBUnavailable("insert 没有返回 session_id")
        session = row_to_agent_session(row)
        session["created"] = True
        return session
    raise DBUnavailable("session_id 连续 %d 次撞主键，放弃: %s" % (SESS_ID_TRY, _safe_err(last)))


def build_session_archive_markdown(session: Dict[str, Any], turns: Sequence[Dict[str, Any]],
                                   now: datetime.datetime) -> str:
    """把一场会话的流水拼成一份 markdown 归档文档。

    正文为空但带 content_ref 的（D7 长正文外置）如实标出 tmp_id —— 归档文档不搬正文，
    只留"去哪儿找"；两处都没有的就是被归档纪律清过正文的行（D6）。
    """
    members = session.get("members") or []
    lines = [
        "# Agent 会话归档 %s" % session.get("session_id"),
        "",
        "- topic: `%s`" % (session.get("topic") or SESS_DEFAULT_TOPIC),
        "- 标题: %s" % (session.get("title") or "-"),
        "- 发起方: %s" % (session.get("owner") or "-"),
        "- 参与成员: %s" % (", ".join(str(m) for m in members) if members else "-"),
        "- 状态: %s（关闭原因 %s）" % (session.get("state"),
                                    session.get("close_reason") or "-"),
        "- 轮次: %d" % int(session.get("turn_count") or 0),
        "- 创建: %s" % (session.get("created_at") or "-"),
        "- 最近一轮: %s" % (session.get("last_turn_at") or "-"),
        "- 关闭: %s" % (session.get("closed_at") or "-"),
        "- 归档: %s" % _iso_z(now),
        "",
    ]
    if session.get("note"):
        lines += ["- 收尾说明: %s" % session["note"], ""]
    lines += ["---", "", "## 对话流水", ""]
    if not turns:
        lines += ["（本会话没有留下任何轮次）", ""]
    for turn in turns:
        lines.append("### #%s %s %s → %s [%s]" % (
            turn.get("seq"), turn.get("ts"), turn.get("from_member") or "-",
            turn.get("to_member") or "-", turn.get("role")))
        lines.append("")
        content = turn.get("content")
        if content:
            lines.append(content)
        elif turn.get("content_ref"):
            lines.append("（正文已外置，见临时共享区 %s；归档文档不搬正文）" % turn["content_ref"])
        else:
            lines.append("（正文已按归档纪律清空）")
        lines.append("")
    return "\n".join(lines)


def _sess_archive_audience(session: Dict[str, Any]) -> List[str]:
    """归档文档的受众 = 全体成员（`["all"]`）。

    口径（本批自行判定，契约 §4.3 只写了"任务级 scope"）：归档是**团队的**留痕，
    不是只给当事人看的私信。两条硬理由：
      - 只读侧（看板 9941/9942 与 /api/shared）每一条查询都带
        `audience @> '["all"]' OR audience @> '["某人"]'`（schema 注释在，
        `audience_clause()` 也在）——写成员名单等于归档完**看板上看不见**；
      - 契约 §10 批 2 明确要"归档文档的看板展示"，且库里既有 3 条 team 内容全是 `["all"]`。
    发起方与参与成员仍逐条记进 source_refs / 正文抬头，不丢"谁参与过"这条信息。
    """
    return [SHARED_ALL_AUDIENCE]


def archive_agent_session(session_id: str,
                          days: int = SESS_ARCHIVE_EXPIRES_DAYS) -> Optional[int]:
    """把一场会话归档：流水 -> markdown -> shared_content（任务级 scope）-> 写回 archive_ref。

    返回 archive_ref（shared_content.content_id）；会话不存在返回 None。
    幂等：已经归档过（archive_ref 非空）直接返回原值，不重复写文档。
    调用方负责先把 state 落到终态（认领），这里只管"留下一份查得到的归档"。
    """
    session = select_agent_session(session_id)
    if session is None:
        return None
    if session.get("archive_ref"):
        return int(session["archive_ref"])
    turns = select_all_agent_turns(session_id)
    body = build_session_archive_markdown(session, turns, utc_now())
    task_id = SESS_ARCHIVE_TASK_PREFIX + session_id
    title = "会话归档 %s%s" % (session_id,
                             ("（%s）" % session["title"]) if session.get("title") else "")
    tags = ["session", "archive", "topic:%s" % (session.get("topic") or SESS_DEFAULT_TOPIC)]
    source_refs = [{"kind": "agent_session", "session_id": session_id,
                    "topic": session.get("topic"), "turn_count": session.get("turn_count"),
                    "close_reason": session.get("close_reason")}]

    db_query(SESS_ARCHIVE_TASK_SQL,
             (task_id, title, session.get("owner"), int(days)), "none")
    row = db_query(SESS_ARCHIVE_CONTENT_SQL,
                   (task_id, SESS_ARCHIVE_KIND, title, body,
                    json.dumps(tags, ensure_ascii=False),
                    session.get("owner") or "unknown",
                    json.dumps(_sess_archive_audience(session), ensure_ascii=False),
                    int(days), json.dumps(source_refs, ensure_ascii=False),
                    "blackboard_svc",
                    _vector_literal(compute_embedding(body))), "one")
    if not row:
        raise DBUnavailable("归档写入 shared_content 没有返回 content_id")
    content_id = int(row[0])
    db_query(SESS_SET_ARCHIVE_SQL, (content_id, session_id), "none")
    return content_id


def clear_agent_session_turn_bodies(session_id: str) -> int:
    """归档后把 turn 正文清空留行（D6）。返回被清空的行数（触发器只放行这一种 UPDATE）。"""
    row = db_query("SELECT count(*) FROM agent_session_turns "
                   "WHERE session_id = %s AND content IS NOT NULL AND content <> ''",
                   (session_id,), "one")
    pending = int(row[0]) if row else 0
    db_query(SESS_CLEAR_TURNS_SQL, (session_id,), "none")
    return pending


def close_agent_session(session_id: str, reason: str,
                        note: Optional[str] = None) -> Dict[str, Any]:
    """关闭 + 归档 + 清正文留行。reason 取 manual（手动关）/ idle（空闲超时）。

    返回 {"claimed": 是否由本次调用认领了状态迁移, "session": 终态后的那行,
          "archive_ref": 归档 id（None 表示没归档成）, "turns_cleared": 清了几行正文}。
    并发保护：状态迁移是一条 `WHERE state='open'` 的 UPDATE，只有一个人能认领成功；
    没认领到的调用方**什么都不做**（不做第二次归档）。
    """
    if reason not in (SESS_CLOSE_REASON_MANUAL, SESS_CLOSE_REASON_IDLE):
        raise ValueError("close 原因只能是 %s / %s" % (SESS_CLOSE_REASON_MANUAL,
                                                      SESS_CLOSE_REASON_IDLE))
    # manual -> closed，idle（空闲超时）-> expired；两个终态共享同一条归档路径。
    state = "expired" if reason == SESS_CLOSE_REASON_IDLE else "closed"
    row = db_query(SESS_CLAIM_SQL, (state, reason, note, session_id), "one")
    if row is None:
        return {"claimed": False, "session": None, "archive_ref": None, "turns_cleared": 0}
    session = row_to_agent_session(row)
    archive_ref = None   # type: Optional[int]
    turns_cleared = 0
    try:
        archive_ref = archive_agent_session(session_id)
        turns_cleared = clear_agent_session_turn_bodies(session_id)
    except DBUnavailable as exc:
        # 状态已经落了终态就不回退（能查、能读，只是归档还没落成）；
        # sweep 每轮会补扫"终态但 archive_ref 为空"的会话，直到归档成功。
        LOG.warning("会话归档未完成 session_id=%s: %s", session_id, _safe_err(exc))
    fresh = select_agent_session(session_id) or session
    return {"claimed": True, "session": fresh, "archive_ref": archive_ref,
            "turns_cleared": turns_cleared}


def sess_sweep_once() -> int:
    """跑一轮会话兜底 sweep：①空闲超时的会话归档置 expired；②补扫已终态但没归档的。

    异常只记日志（Runtime.sess_sweep_error），绝不把服务带崩 —— 和 tmp 那套一个口径。
    """
    archived = 0
    try:
        rows = db_query(SESS_IDLE_SWEEP_SQL, (), "all") or []
        for row in rows:
            session_id = row[0]
            if archive_agent_session(session_id) is not None:
                archived += 1
            # 顺手把正文清空留行（D6）；已经清过的就是 0 行
            clear_agent_session_turn_bodies(session_id)
            LOG.info("会话空闲超时归档 session_id=%s", session_id)
        left = db_query(SESS_UNARCHIVED_SQL, (SESS_UNARCHIVED_BATCH,), "all") or []
        for row in left:
            session_id = row[0]
            if archive_agent_session(session_id) is not None:
                archived += 1
                clear_agent_session_turn_bodies(session_id)
    except DBUnavailable as exc:
        Runtime.sess_sweep_error = _safe_err(exc)
        LOG.warning("session 兜底 sweep 失败: %s", _safe_err(exc))
        return archived
    Runtime.sess_sweep_error = None
    Runtime.sess_sweeps += 1
    Runtime.sess_archived += archived
    Runtime.sess_swept_at = _iso_z(utc_now())
    if archived:
        LOG.info("session 兜底 sweep 归档 %d 场", archived)
    return archived


def db_health() -> Tuple[bool, Dict[str, Any]]:
    """探活 + 统计条目总数。返回 (是否健康, 载荷)。"""
    started = time.time()
    try:
        row = db_query("SELECT count(*) FROM blackboard_entries", (), "one")
    except DBUnavailable as exc:
        return False, {"db": {"ok": False, "error": _safe_err(exc)},
                       "entries": {"total": None}}
    latency_ms = int(round((time.time() - started) * 1000))
    total = row[0] if row else 0
    return True, {"db": {"ok": True, "latency_ms": latency_ms},
                  "entries": {"total": total}}


def health_payload() -> Tuple[int, Dict[str, Any]]:
    """两个健康口共用同一份载荷，免得业务口和管理口讲两套话。

    **嵌入不可用不拉低 ok**：库里没向量只是检索退回关键词，黑板本身照常读写。
    embedder.status() 不会触发模型加载，所以 /healthz 仍然是廉价的。
    """
    ok, detail = db_health()
    payload = {"ok": ok, "version": VERSION, "node": Runtime.node_id,
               "ts": int(time.time())}
    payload.update(detail)
    payload["embed"] = embedder.status()
    # 临时共享区的兜底 sweep 自报（契约 §4.2：最近一次 sweep 时间暴露在 tmp_swept_at）。
    # 这两个值都是进程内变量，库挂了也照样报得出来。
    payload["tmp_swept_at"] = Runtime.tmp_swept_at
    payload["tmp"] = {"swept_at": Runtime.tmp_swept_at,
                      "sweeps": Runtime.tmp_sweeps,
                      "sweep_interval_s": TMP_SWEEP_INTERVAL_S,
                      "last_error": Runtime.tmp_sweep_error}
    # Agent 会话的空闲超时 sweep 自报（契约 D1/§4.3：复用同一个线程，各自暴露心跳）。
    # 与 tmp 同口径：都是进程内变量，库挂了也照样报得出来。
    payload["sess_swept_at"] = Runtime.sess_swept_at
    payload["sess"] = {"swept_at": Runtime.sess_swept_at,
                       "sweeps": Runtime.sess_sweeps,
                       "archived": Runtime.sess_archived,
                       "sweep_interval_s": TMP_SWEEP_INTERVAL_S,
                       "last_error": Runtime.sess_sweep_error}
    # 消息可靠投递（/msg）自报：sweep 心跳 + 各状态计数。
    # states 是现查库的（表还没建时 msg_stats 返回空 dict，不会拖垮 healthz）。
    payload["msg_swept_at"] = Runtime.msg_swept_at
    payload["msg"] = {"swept_at": Runtime.msg_swept_at,
                      "sweeps": Runtime.msg_sweeps,
                      "sweep_interval_s": TMP_SWEEP_INTERVAL_S,
                      "last_error": Runtime.msg_sweep_error,
                      "states": msg_stats()}
    return (200 if ok else 503), payload


# ---------------------------------------------------------------- 入参校验

def _norm_text(value: Any, field: str, required: bool, default: str = "") -> Tuple[Optional[str], Optional[str]]:
    """返回 (值, 错误原因)。短文本字段统一 strip + 限长。"""
    if value is None:
        if required:
            return None, "%s 必填" % field
        return default, None
    if not isinstance(value, str):
        return None, "%s 必须是字符串" % field
    value = value.strip()
    if not value:
        if required:
            return None, "%s 不能为空" % field
        return default, None
    if len(value) > TEXT_FIELD_MAX:
        return None, "%s 超过 %d 字符" % (field, TEXT_FIELD_MAX)
    return value, None


def validate_entry_payload(body: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """校验 POST /entries 的 body。**不看 body 里的 author**，那个字段由鉴权决定。"""
    topic, err = _norm_text(body.get("topic"), "topic", True)
    if err:
        return None, err
    kind, err = _norm_text(body.get("kind"), "kind", False, default="note")
    if err:
        return None, err

    content = body.get("content")
    if not isinstance(content, str) or not content.strip():
        return None, "content 不能为空"

    refs = body.get("refs")
    if refs is None:
        refs = []
    if not isinstance(refs, list):
        return None, "refs 必须是数组"

    embedding = body.get("embedding")
    if embedding is not None:
        # 客户端自带向量就用它的（维度必须对得上库列）；不传则由服务端用本地模型补算，
        # 补算失败留 NULL —— 见 do_POST。
        if not isinstance(embedding, list):
            return None, "embedding 必须是数组"
        if len(embedding) != EMBEDDING_DIM:
            return None, "embedding 维度必须是 %d" % EMBEDDING_DIM
        for x in embedding:
            if isinstance(x, bool) or not isinstance(x, (int, float)):
                return None, "embedding 只能包含数字"

    return {"topic": topic, "kind": kind, "content": content,
            "refs": refs, "embedding": embedding}, None


def validate_tmp_payload(body: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    """校验 POST /tmp 的 body。**不看 body 里的 owner/author**，写者身份由鉴权决定。

    返回 (payload, 错误原因, HTTP 码)：413 与 400 在契约里是两个不同的错误码，得分开报。
    """
    content = body.get("body")
    if not isinstance(content, str) or not content.strip():
        return None, "body 不能为空", 400
    nbytes = len(content.encode("utf-8"))
    if nbytes > MAX_BODY_BYTES:
        return None, "body 有 %d 字节，超过 %d" % (nbytes, MAX_BODY_BYTES), 413

    to = body.get("to")
    if to is None:
        to = []
    if not isinstance(to, list) or any(not isinstance(x, str) for x in to):
        return None, "to 必须是成员名数组", 400
    # 去重保序：audience 是"人"的集合，重复没有意义；reads 缺省值按去重后的人头算
    audience = []  # type: List[str]
    for name in to:
        name = name.strip()
        if name and name not in audience:
            audience.append(name)
    if len(audience) > 100:
        return None, "to 最多 100 个成员", 400
    known = set(known_members())
    unknown = sorted({x for x in audience if x not in known})
    if unknown:
        return None, "to 里有未知成员名: %s" % ",".join(unknown), 400

    reads = body.get("reads")
    if reads is None:
        reads = max(1, len(audience))          # 缺省 = 收件人数（不足 1 则 1）
    if isinstance(reads, bool) or not isinstance(reads, int):
        return None, "reads 必须是整数", 400
    if not (TMP_READS_MIN <= reads <= TMP_READS_MAX):
        return None, "reads 必须在 %d..%d" % (TMP_READS_MIN, TMP_READS_MAX), 400

    ttl = body.get("ttl_s")
    if ttl is None:
        ttl = TMP_DEFAULT_TTL_S
    if isinstance(ttl, bool) or not isinstance(ttl, int):
        return None, "ttl_s 必须是整数", 400
    if not (TMP_MIN_TTL_S <= ttl <= TMP_MAX_TTL_S):
        return None, "ttl_s 必须在 %d..%d" % (TMP_MIN_TTL_S, TMP_MAX_TTL_S), 400

    title = body.get("title")
    if title is not None:
        title, err = _norm_text(title, "title", False)
        if err:
            return None, err, 400
        title = title or None

    return {"body": content, "to": audience, "title": title,
            "reads": reads, "ttl_s": ttl}, None, 200


def validate_sess_payload(body: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """校验 POST /sess 的 body。**不看 body 里的 owner/author**，发起方由鉴权结果决定。

    口径：
      * topic 缺省 general（D8：值域与黑板条目 / 会议 session 同一套，NOT NULL 但可以不写）；
      * members 去重保序、最多 100 个；**不校验是不是"已知成员名"** —— 会话是"谁参与"的
        声明，成员机上的名字未必都在中枢 peers.json 里（登记不全不该拦住开会）；
      * idle_ttl_s 60..86400，缺省 1800（D1：这是**空闲**超时，不是会话总时长）。
    """
    topic, err = _norm_text(body.get("topic"), "topic", False, default=SESS_DEFAULT_TOPIC)
    if err:
        return None, err
    if len(topic or "") > SESS_TOPIC_MAX:
        return None, "topic 超过 %d 字符" % SESS_TOPIC_MAX

    title, err = _norm_text(body.get("title"), "title", False, default="")
    if err:
        return None, err
    title = title or None

    raw_members = body.get("members")
    if raw_members is None:
        raw_members = []
    if not isinstance(raw_members, list) or any(not isinstance(x, str) for x in raw_members):
        return None, "members 必须是成员名数组"
    members = []  # type: List[str]
    for name in raw_members:
        name = name.strip()
        if not name:
            continue
        if len(name) > SESS_MEMBER_NAME_MAX:
            return None, "members 里的成员名超过 %d 字符" % SESS_MEMBER_NAME_MAX
        if name not in members:
            members.append(name)
    if len(members) > SESS_MEMBERS_MAX:
        return None, "members 最多 %d 个成员" % SESS_MEMBERS_MAX

    ttl = body.get("idle_ttl_s")
    if ttl is None:
        ttl = SESS_DEFAULT_IDLE_TTL_S
    if isinstance(ttl, bool) or not isinstance(ttl, int):
        return None, "idle_ttl_s 必须是整数"
    if not (SESS_MIN_IDLE_TTL_S <= ttl <= SESS_MAX_IDLE_TTL_S):
        return None, "idle_ttl_s 必须在 %d..%d" % (SESS_MIN_IDLE_TTL_S, SESS_MAX_IDLE_TTL_S)

    return {"topic": topic, "title": title, "members": members,
            "idle_ttl_s": ttl}, None


def validate_close_note(body: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """校验 POST /sess/<id>/close 的 body：只有一个可选的 note。"""
    return _norm_text(body.get("note"), "note", False, default="")


def validate_turn_payload(body: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    """校验 POST /sess/<id>/turns 的 body。返回 (payload, 错误, HTTP 码)。

    role 只认 user / assistant / system；content 与 content_ref **至少一个非空** ——
    两边都空的一轮流水没有任何信息量，收下来只会让回放拼出一串空消息。
    from_member 不在这里读：它必须来自鉴权结果（防冒名）。
    """
    role = body.get("role")
    if not isinstance(role, str) or role.strip().lower() not in SESS_ROLES:
        return None, "role 必须是 %s 之一" % " / ".join(SESS_ROLES), 400
    role = role.strip().lower()

    content, err = _norm_text(body.get("content"), "content", False, default="")
    if err:
        return None, err, 400
    content = content or ""
    if len(content) > SESS_TURN_CONTENT_MAX:
        return None, "content 超过 %d 字符" % SESS_TURN_CONTENT_MAX, 413

    content_ref = body.get("content_ref")
    if isinstance(content_ref, str):
        content_ref = content_ref.strip() or None
    elif content_ref is not None and not isinstance(content_ref, dict):
        return None, "content_ref 必须是字符串或对象", 400
    elif isinstance(content_ref, dict) and not content_ref:
        content_ref = None

    to_member = body.get("to_member")
    if to_member is not None:
        if not isinstance(to_member, str) or not to_member.strip():
            return None, "to_member 必须是非空字符串", 400
        to_member = to_member.strip()

    if not content.strip() and not content_ref:
        return None, "content 与 content_ref 至少要有一个", 400

    return {"role": role, "content": content, "content_ref": content_ref,
            "to_member": to_member}, None, 200


def parse_since(raw: str) -> Tuple[Optional[str], Optional[str]]:
    """since 支持 ISO8601 或 unix 秒；返回 (可直接喂给 ::timestamptz 的串, 错误)。"""
    raw = (raw or "").strip()
    if not raw:
        return None, None
    if re.match(r"^\d{1,12}(\.\d+)?$", raw):
        try:
            seconds = float(raw)
        except ValueError:
            return None, "since 不是合法时间"
        return datetime.datetime.utcfromtimestamp(seconds).strftime("%Y-%m-%dT%H:%M:%S") + "+00:00", None
    # 只做粗校验，真正的解析交给 PG 的 timestamptz（参数化，注入不了）
    if len(raw) > 64 or not re.match(r"^[0-9TZ:\-+. ]+$", raw):
        return None, "since 不是合法时间"
    return raw, None


_FLAG_TRUE = ("1", "true", "yes", "on")
_FLAG_FALSE = ("", "0", "false", "no", "off")


def parse_flag(raw: str, field: str) -> Tuple[Optional[bool], Optional[str]]:
    """开关型参数：只认 0/1（以及 true/false 这类同义写法），其余一律 400。"""
    value = (raw or "").strip().lower()
    if value in _FLAG_FALSE:
        return False, None
    if value in _FLAG_TRUE:
        return True, None
    return None, "%s 只接受 0 或 1" % field


def parse_status(raw: str, allowed: Sequence[str],
                 field: str = "status") -> Tuple[Optional[str], Optional[str]]:
    """状态过滤词：空表示不过滤；非白名单值直接 400，不去库里碰运气。"""
    value = (raw or "").strip()
    if not value:
        return None, None
    if value not in tuple(allowed):
        return None, "%s 只能是 %s 之一" % (field, "/".join(allowed))
    return value, None


def parse_owner(raw: str) -> Tuple[Optional[str], Optional[str]]:
    """责任人/跟踪人过滤词：空表示不过滤，超长直接 400。"""
    value = (raw or "").strip()
    if not value:
        return None, None
    if len(value) > OWNER_MAX:
        return None, "owner 超过 %d 字符" % OWNER_MAX
    return value, None


# ---------------------------------------------------------------- HTTP 处理
#
# 「应答出口」这层**传输职责**交给 L1 `web_gateway`（阶段2 刀7a）：`send_response` →
# 逐条写头 → `end_headers` → 写 socket → 客户端早断时吞异常，全在 L1 的 `_reply()`
# 里；JSON 编码与 `Content-Type` 默认值走 L1 的 `_envelope()`。本件只剩三样 L1
# 给不了的东西：
#   · **本次特有的头**（目前只有 405 那条 `Allow: GET`）与它们相对 Content-Length 的顺序；
#   · 每个响应一行 `<方法> <路径> -> <码> from=<成员> ip=<ip>` 的日志（要带鉴权成员名）；
#   · `sys_version=""` + HTTP/1.1（保住 `Server: blackboard_svc/<VERSION>` 与 keep-alive）。
#
# **路由分派**也全部登记进 L1（下面的登记表）：非 GET 是 POST 的七条业务路径 + /entries、
# DELETE 的 /tmp/<id>（阶段2 刀7a，7 条）；GET 是 13 组判定 → 13 个处理函数 → 16 条
# 登记（10 条精确 + 6 条前缀，阶段2 刀7b），
# PUT/PATCH 本来就没有可登记的路径（「不是共享路径就 501」）。
#
# 迁移前 do_GET 是一串**有序的 if/elif**，L1 的分派是「精确表优先 → 前缀表按长度降序」。
# 两者等价的理由：13 组判定的 `parts[0]` 两两不相交（healthz/msg/entries/search/
# sessions/actions/issues/tracking/tmp/sess 与 api|shared），任一路径最多命中一组，
# **顺序无歧义**；`/sess` 与 `/sessions`（D5 要求分开）靠 L1 的前缀规则天然分开 ——
# 前缀只认「等于前缀」或「以 前缀+'/' 开头」，`/sessions` 不以 `/sess/` 开头。
# 各组内部「形状不对」的兜底仍各自判：`/msg/a/b`、`/sessions/a/b`、`/tmp/a/b`、
# `/sess/a/b/c` 照旧落**裸 404**（走 `_gw_not_found` 的 GET 支，不鉴权、不带 peer），
# 而 `/sess/<id>/<非 turns>` 是**鉴权之后**那条带 `detail` 文案的 404 —— 两种文案不能并。

JSON_CT = "application/json; charset=utf-8"

GW = web_gateway.Gateway("blackboard_svc", version=VERSION, quiet=True)

# 登记的处理函数一律**自己调 `_send_json()` 直接写出**（迁移前的推式出口一个字节没动：
# 叶子方法有四十来个、各自还会在中途多次早退写 400/404/503，改成返式等于把它们全重写，
# 那就不是「收路由」而是改业务了），回这个占位信封；`_handle()` 看见 `self._sent`
# 为真就把它丢掉，绝不会再发第二份响应。
_SENT = (204, None, {})  # type: Tuple[int, Any, Dict[str, Any]]


# --- 非 GET 的路由登记表：路径 → 处理函数（语义全在 BlackboardHandler 的同名方法里）---

def _gw_peer(req: "web_gateway.Request") -> Optional[str]:
    """取鉴权成员；不通过就地回 401（迁移前每个分支重复的那三行）。"""
    peer = req.svc._auth_peer()
    if peer is None:
        req.svc._send_json(401, {"ok": False, "error": "unauthorized"})
    return peer


@GW.pre
def _gw_pre(req: "web_gateway.Request") -> Any:
    """所有方法的公共前奏（迁移前每个 do_<METHOD> 开头那一两行）。

    1. **先读完请求体**：哪怕接下来就 405/501，也要把 socket 排空，否则 keep-alive
       连接会错位（超限排空与断连由 `_read_body()` 自己处理）。迁移前 do_GET 开头
       那句光秃秃的 `self._read_body()`（读完即弃）就是这一步，GET 照走。
    2. 共享内容只读纪律：写请求打到 /api/shared、/shared 一律 405，一个字节不碰库。
       迁移前 DELETE 是「先判 /tmp/<id> 再 _reject_write()」，这里提到前面等价 ——
       `_is_shared_path()` 只认 api/shared 与 shared 两种开头，`/tmp` 不在其中。
       **GET 必须绕开这一步**：迁移前 do_GET 从不调 `_reject_write()`，共享内容本来
       就只许 GET 读；不绕开的话 `GET /api/shared` 会从 200 变成 405。
    """
    svc = req.svc
    req.raw, req.too_large = svc._read_body()
    if req.method == "GET":
        return None
    if svc._reject_write():
        return _SENT
    return None


@GW.not_found
def _gw_not_found(req: "web_gateway.Request") -> Any:
    """没登记的路径：GET / POST 落**裸** 404 信封（不鉴权、不带 peer，与迁移前
    do_GET 末尾那一行、do_POST 末尾那一行逐字一致），其余写方法照旧落基类的 501。

    带 `detail` 文案的那条 404（`/sess/<id>/<非 turns>`）**不在这里** —— 它是鉴权
    之后的业务判定，留在 `_gw_get_sess_sub()` 里，两种文案绝不能并成一条。
    """
    svc = req.svc
    if req.method in ("GET", "POST"):
        svc._send_json(404, {"ok": False, "error": "not_found"})
    else:
        svc._send_501(req.method)
    return _SENT


@GW.route("/msg", methods=("POST",))
def _gw_post_msg(req: "web_gateway.Request") -> Any:
    """POST /msg —— 落库（=送达）。from 取鉴权结果，不采信请求体。"""
    peer = _gw_peer(req)
    if peer is not None:
        req.svc._msg_write(peer, req.raw, req.too_large)
    return _SENT


@GW.route("/msg", methods=("POST",), prefix=True)
def _gw_post_msg_action(req: "web_gateway.Request") -> Any:
    """POST /msg/<id>/ack —— 收件方回「已收到并开始处理」；
    POST /msg/<id>/done —— 收件方报「处理完了/失败了」。别的形状照旧落 404。"""
    parts = req.parts
    if len(parts) != 3 or parts[2] not in ("ack", "done"):
        return _gw_not_found(req)
    peer = _gw_peer(req)
    if peer is not None:
        if parts[2] == "ack":
            req.svc._msg_ack(peer, parts[1])
        else:
            req.svc._msg_done(peer, parts[1], req.raw, req.too_large)
    return _SENT


@GW.route("/tmp", methods=("POST",))
def _gw_post_tmp(req: "web_gateway.Request") -> Any:
    """POST /tmp —— 临时共享区：存长文本，换一个短 id（契约 §3.1）。"""
    peer = _gw_peer(req)
    if peer is not None:
        req.svc._tmp_write(peer, req.raw, req.too_large)
    return _SENT


@GW.route("/tmp", methods=("DELETE",), prefix=True)
def _gw_delete_tmp(req: "web_gateway.Request") -> Any:
    """DELETE /tmp/<id> —— 临时共享区：仅 owner 能主动销毁（契约 §3.4）。

    形状不是 /tmp/<id> 的（/tmp、/tmp/a/b）照旧落基类 501，与迁移前逐字一致。
    """
    parts = req.parts
    if len(parts) != 2:
        return _gw_not_found(req)
    peer = _gw_peer(req)
    if peer is not None:
        req.svc._tmp_delete(peer, parts[1])
    return _SENT


@GW.route("/sess", methods=("POST",))
def _gw_post_sess(req: "web_gateway.Request") -> Any:
    """POST /sess —— 开一个 Agent 会话（契约 §5）。owner 一律取鉴权结果（防冒名）。"""
    peer = _gw_peer(req)
    if peer is not None:
        req.svc._sess_open(peer, req.raw, req.too_large)
    return _SENT


@GW.route("/sess", methods=("POST",), prefix=True)
def _gw_post_sess_action(req: "web_gateway.Request") -> Any:
    """POST /sess/<id>/close —— 关闭 + 归档（契约 §5/§4.3）。**没有 DELETE 路径**：流水不可删。
    POST /sess/<id>/turns —— 追加一轮会话流水（契约 §5）。先落库，回放才有权威可依。"""
    parts = req.parts
    if len(parts) != 3 or parts[2] not in ("close", "turns"):
        return _gw_not_found(req)
    peer = _gw_peer(req)
    if peer is not None:
        if parts[2] == "close":
            req.svc._sess_close(peer, parts[1], req.raw, req.too_large)
        else:
            req.svc._sess_turn_write(peer, parts[1], req.raw, req.too_large)
    return _SENT


@GW.route("/entries", methods=("POST",))
def _gw_post_entries(req: "web_gateway.Request") -> Any:
    """POST /entries —— 往黑板 append 一条（本件唯一的黑板写路径）。"""
    req.svc._post_entry(req.raw, req.too_large)
    return _SENT


# --- GET 的路由登记表（阶段2 刀7b）：13 组判定 → 16 条登记 ----------------------
#
# **不挂 `@GW.auth` 全局鉴权钩子**：/healthz 与「未知路径」这两支迁移前根本不鉴权，
# 挂上去会把未知路径从 404 变成 401（顺序行为就变了）。所以 2~12 组每支自己先
# `_gw_peer()`，1 与 13 不鉴权 —— 这正是迁移前那 11 次重复的三行的归属。
#
# 「列表 / 单条」这类一组两形状的，一律**精确登记管列表、前缀登记管带 id 的**：
# L1 先查精确表，前缀只在精确表落空时才轮到，于是 `/msg` → 列表、`/msg/<id>` → 单条，
# 与迁移前 `parts == ["msg"]` / `len(parts) == 2 and parts[0] == "msg"` 一一对上。
# 形状不对的（段数超了）一律在**鉴权之前**转 `_gw_not_found()`，落裸 404。


@GW.route("/healthz")
def _gw_get_healthz(req: "web_gateway.Request") -> Any:
    """GET /healthz —— **不鉴权**：探活得能匿名打（k8s/监控没有成员令牌）。"""
    svc = req.svc
    code, payload = svc._health_payload()
    svc._send_json(code, payload)
    return _SENT


@GW.route("/msg")
def _gw_get_msg(req: "web_gateway.Request") -> Any:
    """GET /msg?to=<自己>&state=queued,processing —— 收件箱 / 补拉。"""
    peer = _gw_peer(req)
    if peer is not None:
        req.svc._msg_list(peer, req.svc._query())
    return _SENT


@GW.route("/msg", prefix=True)
def _gw_get_msg_one(req: "web_gateway.Request") -> Any:
    """GET /msg/<id> —— 查单条：发送方靠它知道「送没送到、对方开没开工」。"""
    parts = req.parts
    if len(parts) != 2:                      # /msg/a/b：迁移前落到末尾的裸 404
        return _gw_not_found(req)
    peer = _gw_peer(req)
    if peer is not None:
        req.svc._msg_get(peer, parts[1])
    return _SENT


@GW.route("/entries")
def _gw_get_entries(req: "web_gateway.Request") -> Any:
    """GET /entries —— 黑板条目列表（只读）。"""
    peer = _gw_peer(req)
    if peer is not None:
        req.svc._get_entries(peer)
    return _SENT


@GW.route("/search")
def _gw_get_search(req: "web_gateway.Request") -> Any:
    """GET /search?q= —— 黑板检索（只读）。"""
    peer = _gw_peer(req)
    if peer is not None:
        req.svc._get_search(peer)
    return _SENT


@GW.route("/sessions")
def _gw_get_sessions(req: "web_gateway.Request") -> Any:
    """GET /sessions —— **会议**（roundtable 场次）列表。与 /sess 是两套东西（D5）。"""
    peer = _gw_peer(req)
    if peer is not None:
        req.svc._get_sessions(peer)
    return _SENT


@GW.route("/sessions", prefix=True)
def _gw_get_session_detail(req: "web_gateway.Request") -> Any:
    """GET /sessions/<session_id> —— 一场会议 + 逐条发言。"""
    parts = req.parts
    if len(parts) != 2:                      # /sessions/a/b：迁移前落末尾的裸 404
        return _gw_not_found(req)
    peer = _gw_peer(req)
    if peer is not None:
        req.svc._get_session_detail(peer, parts[1])
    return _SENT


@GW.route("/actions")
@GW.route("/issues")
@GW.route("/tracking")
def _gw_get_tracking(req: "web_gateway.Request") -> Any:
    """GET /actions | /issues | /tracking —— 迁移前共用一个 `parts in (...)` 块，
    所以这里也共用一个处理函数，靠 `parts[0]` 分道，语义与文案一个字没动。"""
    peer = _gw_peer(req)
    if peer is None:
        return _SENT
    kind = req.parts[0]
    if kind == "tracking":
        req.svc._get_tracking(peer)
    else:
        req.svc._get_tracking_list(peer, kind)
    return _SENT


@GW.route("/tmp")
def _gw_get_tmp(req: "web_gateway.Request") -> Any:
    """GET /tmp —— 临时共享区清单（契约 §3.2）。`/tmp` 不在 `_is_shared_path()` 里，
    所以不会被 `_reject_write()` 的 405 拦掉。"""
    peer = _gw_peer(req)
    if peer is not None:
        req.svc._tmp_list(peer)
    return _SENT


@GW.route("/tmp", prefix=True)
def _gw_get_tmp_read(req: "web_gateway.Request") -> Any:
    """GET /tmp/<name> —— 临时共享区取文（契约 §3.3）。"""
    parts = req.parts
    if len(parts) != 2:                      # /tmp/a/b：迁移前落末尾的裸 404
        return _gw_not_found(req)
    peer = _gw_peer(req)
    if peer is not None:
        req.svc._tmp_read(peer, parts[1])
    return _SENT


@GW.route("/sess")
def _gw_get_sess(req: "web_gateway.Request") -> Any:
    """GET /sess —— **Agent 会话**列表（契约 §5）。别和 /sessions（会议）混（D5）。"""
    peer = _gw_peer(req)
    if peer is not None:
        req.svc._sess_list(peer)
    return _SENT


@GW.route("/sess", prefix=True)
def _gw_get_sess_sub(req: "web_gateway.Request") -> Any:
    """GET /sess/<id> —— 会话详情；GET /sess/<id>/turns —— 逐轮流水。

    `/sess/<id>/<别的>` 落**带 detail 文案**的 404（且带 peer）—— 这是鉴权后的业务
    判定，和末尾那条裸 404 是两回事；段数超过 3 的才是裸 404（鉴权之前就转走）。
    """
    parts = req.parts
    if len(parts) > 3:                       # /sess/a/b/c：迁移前落末尾的裸 404
        return _gw_not_found(req)
    peer = _gw_peer(req)
    if peer is None:
        return _SENT
    svc = req.svc
    if len(parts) == 2:
        svc._sess_detail(peer, parts[1])
    elif parts[2] == "turns":
        svc._sess_turns(peer, parts[1])
    else:
        svc._send_json(404, {"ok": False, "error": "not_found",
                             "detail": "只有 /sess/<id>/turns 这一个子路径"}, peer)
    return _SENT


@GW.route("/api/shared", prefix=True)
@GW.route("/shared", prefix=True)
def _gw_get_shared(req: "web_gateway.Request") -> Any:
    """GET /api/shared[/tasks|/search|/<id>] —— 共享内容（只读）。

    **两种写法都要通**：看板（blackboard_view）按无前缀口径转发，所以 `/shared...`
    与 `/api/shared...` 各登记一条前缀路由，`rest` 的切法沿用 `_is_shared_path()`
    那一套（`parts[2:]` / `parts[1:]`）。这是最宽的一组，但 `parts[0]` 与前 11 组
    互不相交，前缀表里也没有别的路由会抢 —— 迁移前「放最后判」的语义因此成立。

    没有有效 token 一律 401：共享内容里有主人画像与节点 IP，不给匿名读
    （sensitive 也好 public 也好）。过深路径落**裸** 404，但带 peer。
    """
    peer = _gw_peer(req)
    if peer is None:
        return _SENT
    parts = req.parts
    svc = req.svc
    rest = parts[2:] if parts[0] == "api" else parts[1:]
    if not rest:
        svc._get_shared_list(peer, "list")
    elif rest == ["tasks"]:
        svc._get_shared_tasks(peer)
    elif rest == ["search"]:
        svc._get_shared_list(peer, "search")
    elif len(rest) == 1:
        svc._get_shared_detail(peer, rest[0])
    else:
        svc._send_json(404, {"ok": False, "error": "not_found"}, peer)
    return _SENT


# 必须在全部 @GW.route 之后取：handler_class() 会按「已登记的方法」删掉多余的
# do_<METHOD>，只剩 GET/POST/DELETE —— PUT/PATCH 由下面的子类自己定义（没有
# 可登记的路径，但必须把请求体读完），HEAD/OPTIONS/TRACE 则落回基类 501，
# 与迁移前逐字一致。
_GatewayHandler = GW.handler_class()


class BlackboardHandler(_GatewayHandler):
    """L1 网关处理器 + 本件特有的差异；应答出口与**全部**路由分派都沿用 L1
    （非 GET 是阶段2 刀7a，GET 是刀7b）。

    1. **响应形态**：`sys_version=""` + HTTP/1.1，保住 `Server: blackboard_svc/<VERSION>`
       与 keep-alive 语义（L1 默认 HTTP/1.0 且不清 sys_version）。
    2. **应答出口**：`_send_json()` 只管 JSON 编码、本次特有的头与那一行日志，
       socket 那一段（写头 / end_headers / 写体 / 吞异常）全在 L1 的 `_reply()` 里。
    3. **路径归一**：沿用迁移前的 `_path_parts()`（剥 #fragment、吃掉空段与尾斜杠），
       比 L1 的 `partition("?")` + `rstrip("/")` 宽，`/msg//x` 照样认。
    """

    server_version = "blackboard_svc/" + VERSION
    sys_version = ""
    protocol_version = "HTTP/1.1"

    _sent = False       # 本次请求是否已经写出响应（登记的处理函数一律推式出口）

    # --- 基础工具 -------------------------------------------------
    def log_message(self, fmt: str, *args: Any) -> None:
        """屏蔽默认 stderr 日志，统一由 _send_json 打一行。"""
        return

    def _handle(self) -> None:
        """L1 的统一入口（**所有方法都走这里**）：请求交给 GW.dispatch。

        登记的处理函数一律自己调 `_send_json()` 写出，回来的是占位信封 —— 所以
        `self._sent` 为真时直接丢掉，不发第二份响应。
        """
        self._sent = False
        req = web_gateway.Request(self)
        req.svc = self
        req.parts = self._path_parts()
        req.path = "/" + "/".join(req.parts)
        req.raw, req.too_large = b"", False     # _gw_pre 会覆盖；这里只保证属性存在
        status, body, headers = GW.dispatch(req)
        if not self._sent:                      # 兜底：理论上走不到（每条路径都写过）
            self._reply(status, body, headers)

    def _send_501(self, method: str) -> None:
        """未支持的方法：沿用基类的 send_error 页面，不自行发明 JSON 信封。"""
        self._sent = True
        self.send_error(501, "Unsupported method (%s)" % method)

    def _send_json(self, code: int, payload: Dict[str, Any],
                   peer: Optional[str] = None,
                   headers: Optional[Sequence[Tuple[str, str]]] = None) -> None:
        """本件唯一的 JSON 应答出口；socket 那一段全在 L1 的 `_reply()` 里。

        头的顺序就是发出去的字节顺序：Content-Type（L1 的 `_envelope()` 补）→
        Content-Length → 本次特有的头。Content-Length **必须自己补在这里**：
        交给 L1 的 `_reply()` 补会排到特有头后面，`Allow: GET` 那一例字节就变了。
        """
        code, body, out = web_gateway._envelope(code, payload)
        out["Content-Length"] = str(len(body))
        for name, value in (headers or ()):
            out[name] = value
        self._sent = True
        self._reply(code, body, out)
        # 日志只记方法/路径/状态/成员名/来源 IP —— 不记 token、不记 body、不记 DSN。
        LOG.info("%s %s -> %d from=%s ip=%s", self.command,
                 urllib.parse.urlsplit(self.path).path, code,
                 peer or "-", self.client_address[0] if self.client_address else "-")

    def _read_body(self) -> Tuple[bytes, bool]:
        """读完请求体，返回 (内容, 是否超限)。

        超限时不把内容留在内存里，但仍**把 socket 里的字节排空**再回 413 —— 否则
        keep-alive 连接会错位，客户端还可能在写到一半被 RST 打断而读不到 413。
        实在太大（> DRAIN_MAX）就排一部分然后断连接，不给人用 Content-Length 撑内存的机会。
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return b"", False
        if length > MAX_BODY_BYTES:
            remaining = min(length, DRAIN_MAX)
            if remaining < length:
                self.close_connection = True
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 65536))
                if not chunk:
                    break
                remaining -= len(chunk)
            return b"", True
        return self.rfile.read(length), False

    def _auth_peer(self) -> Optional[str]:
        """校验 Bearer token，返回命中的成员 id；不通过返回 None。

        与 a2a_svc 同一套：服务端只存 sha256，用 hmac.compare_digest 比对，
        且不提前 break，避免比对次数泄露命中位置。
        """
        header = self.headers.get("Authorization") or ""
        if not header.startswith("Bearer "):
            return None
        token = header[7:].strip()
        if not token:
            return None
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        matched = None  # type: Optional[str]
        for known, pid in Runtime.peer_map.items():
            if hmac.compare_digest(digest, known):
                matched = pid
        return matched

    def _path_parts(self) -> List[str]:
        return [p for p in urllib.parse.urlsplit(self.path).path.split("/") if p]

    def _query(self) -> Dict[str, List[str]]:
        return urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)

    def _health_payload(self) -> Tuple[int, Dict[str, Any]]:
        return health_payload()

    # --- 路由 -----------------------------------------------------
    # GET 的分派也在 L1 了（模块顶部的 @GW.route 登记表，阶段2 刀7b）：这里只把方法
    # 接上 L1 的统一入口。基类其实已经有一份同样的 do_GET（GET 有登记路由，
    # handler_class() 就不会删它），这里显式留一行是为了和 do_PUT/do_PATCH 一样
    # 「本件支持哪些方法」一眼看得见。
    def do_GET(self) -> None:  # noqa: N802
        self._handle()

    @staticmethod
    def _is_shared_path(parts: Sequence[str]) -> bool:
        """/api/shared、/api/shared/<id>、/api/shared/tasks、/api/shared/search

        兼容**不带 /api 前缀**的 /shared、/shared/search —— 看板（blackboard_view）
        按无前缀口径转发共享内容，两条路径都要能通，否则页面只会显示"上游未上线"。
        """
        if len(parts) >= 2 and parts[0] == "api" and parts[1] == "shared":
            return True
        return bool(parts) and parts[0] == "shared"

    def _clamp_limit(self, raw: str, default: int, maximum: int,
                     peer: str) -> Optional[int]:
        """与 /entries 同一套口径：非整数 -> 400（返回 None），其余夹在 1..maximum。"""
        if not raw:
            return default
        try:
            limit = int(raw)
        except ValueError:
            self._send_json(400, {"ok": False, "error": "bad_request",
                                  "detail": "limit 必须是整数"}, peer)
            return None
        if limit <= 0:
            limit = default
        return min(limit, maximum)

    def _get_sessions(self, peer: str) -> None:
        """GET /sessions?state=&topic=&limit=&since= —— 会议列表（只读）。"""
        query = self._query()

        def first(name: str) -> str:
            values = query.get(name) or []
            return (values[0] or "").strip() if values else ""

        limit = self._clamp_limit(first("limit"), SESSIONS_DEFAULT_LIMIT,
                                  SESSIONS_MAX_LIMIT, peer)
        if limit is None:
            return
        since, err = parse_since(first("since"))
        if err:
            self._send_json(400, {"ok": False, "error": "bad_request", "detail": err}, peer)
            return

        try:
            sessions = select_sessions(first("state") or None, first("topic") or None,
                                       limit, since)
        except DBUnavailable as exc:
            LOG.warning("会议列表查询失败 from=%s: %s", peer, _safe_err(exc))
            self._send_json(503, {"ok": False, "error": "db_unavailable",
                                  "detail": _safe_err(exc)}, peer)
            return

        self._send_json(200, {"ok": True, "count": len(sessions), "sessions": sessions}, peer)

    def _get_session_detail(self, peer: str, session_id: str) -> None:
        """GET /sessions/<session_id> —— 一场会议 + 逐条发言（只读）。"""
        session_id = urllib.parse.unquote(session_id)
        if not session_id or len(session_id) > SESSION_ID_MAX:
            self._send_json(404, {"ok": False, "error": "not_found",
                                  "detail": "没有这场会议"}, peer)
            return

        query = self._query()
        values = query.get("limit") or []
        limit = self._clamp_limit((values[0] or "").strip() if values else "",
                                  TURNS_DEFAULT_LIMIT, SESSIONS_MAX_LIMIT, peer)
        if limit is None:
            return

        degraded = []  # type: List[str]
        now = utc_now()
        try:
            session = select_session(session_id, degraded)
            if session is None:
                self._send_json(404, {"ok": False, "error": "not_found",
                                      "detail": "没有这场会议"}, peer)
                return
            # 四要素先取（缺表各自降级），逐条发言放最后 —— 发言最长，先把结论性的东西拿到手
            agenda_items = select_agenda_items(session_id, degraded)
            confirmations = select_confirmations(session_id, degraded)
            actions = select_actions(None, None, False, TRACK_MAX_LIMIT, now,
                                     session_id=session_id, degraded=degraded)
            issues = select_issues(None, None, False, TRACK_MAX_LIMIT, now,
                                   session_id=session_id, degraded=degraded)
            turns = select_turns(session_id, limit)
        except DBUnavailable as exc:
            LOG.warning("会议详情查询失败 from=%s: %s", peer, _safe_err(exc))
            self._send_json(503, {"ok": False, "error": "db_unavailable",
                                  "detail": _safe_err(exc)}, peer)
            return

        self._send_json(200, {"ok": True, "session": session, "turns": turns,
                              "count": len(turns), "agenda_items": agenda_items,
                              "confirmations": confirmations, "actions": actions,
                              "issues": issues, "degraded": degraded,
                              "now": now.isoformat(), "tz": "UTC"}, peer)

    # --- 跟踪：落实事项 / 遗留问题（跨会只读清单） -------------------
    def _tracking_params(self, peer: str, kind: str
                         ) -> Optional[Tuple[Optional[str], Optional[str], bool, int,
                                             Optional[str]]]:
        """三个跟踪端点共用的入参口径：status / owner / overdue / limit / kind。不合法回 400。"""
        query = self._query()

        def first(name: str) -> str:
            values = query.get(name) or []
            return (values[0] or "").strip() if values else ""

        # /tracking 是"两张表一起拉"，两边的状态枚举不一样，就不收 status 这个参数了
        status = None  # type: Optional[str]
        if kind != "tracking":
            allowed = ACTION_STATUSES if kind == "actions" else ISSUE_STATUSES
            status, err = parse_status(first("status"), allowed)
            if err:
                self._send_json(400, {"ok": False, "error": "bad_request",
                                      "detail": err}, peer)
                return None
        # kind 只有 /issues 收（落实事项没有"性质"这一说），取值走白名单，其余 400
        issue_kind = None  # type: Optional[str]
        if kind == "issues":
            issue_kind, err = parse_status(first("kind"), ISSUE_KINDS, "kind")
            if err:
                self._send_json(400, {"ok": False, "error": "bad_request",
                                      "detail": err}, peer)
                return None
        owner, err = parse_owner(first("owner"))
        if err:
            self._send_json(400, {"ok": False, "error": "bad_request", "detail": err}, peer)
            return None
        overdue, err = parse_flag(first("overdue"), "overdue")
        if err or overdue is None:
            self._send_json(400, {"ok": False, "error": "bad_request", "detail": err}, peer)
            return None
        limit = self._clamp_limit(first("limit"), TRACK_DEFAULT_LIMIT, TRACK_MAX_LIMIT, peer)
        if limit is None:
            return None
        return status, owner, overdue, limit, issue_kind

    def _get_tracking_list(self, peer: str, kind: str) -> None:
        """GET /actions|/issues?status=&owner=&overdue=1&kind=defect&limit= —— 跨会清单（只读）。"""
        parsed = self._tracking_params(peer, kind)
        if parsed is None:
            return
        status, owner, overdue, limit, issue_kind = parsed
        degraded = []  # type: List[str]
        now = utc_now()
        try:
            if kind == "actions":
                rows = select_actions(status, owner, overdue, limit, now, degraded=degraded)
            else:
                rows = select_issues(status, owner, overdue, limit, now,
                                     degraded=degraded, kind=issue_kind)
        except DBUnavailable as exc:
            LOG.warning("%s 查询失败 from=%s: %s", kind, peer, _safe_err(exc))
            self._send_json(503, {"ok": False, "error": "db_unavailable",
                                  "detail": _safe_err(exc)}, peer)
            return
        payload = {"ok": True, "count": len(rows), kind: rows,
                   "degraded": degraded, "now": now.isoformat(), "tz": "UTC"}
        self._send_json(200, payload, peer)

    def _get_tracking(self, peer: str) -> None:
        """GET /tracking?overdue=1&limit= —— 看板一次拉完：两张清单 + 四个计数。"""
        parsed = self._tracking_params(peer, "tracking")
        if parsed is None:
            return
        _status, owner, overdue, limit, _kind = parsed
        degraded = []  # type: List[str]
        now = utc_now()
        try:
            actions = select_actions(None, owner, overdue, limit, now, degraded=degraded)
            issues = select_issues(None, owner, overdue, limit, now, degraded=degraded)
            counts = tracking_counts(now, degraded)
        except DBUnavailable as exc:
            LOG.warning("跟踪清单查询失败 from=%s: %s", peer, _safe_err(exc))
            self._send_json(503, {"ok": False, "error": "db_unavailable",
                                  "detail": _safe_err(exc)}, peer)
            return
        self._send_json(200, {"ok": True, "actions": actions, "issues": issues,
                              "counts": counts, "degraded": degraded,
                              "now": now.isoformat(), "tz": "UTC"}, peer)

    # --- 共享内容（只读） -------------------------------------------
    def _shared_params(self, peer: str, mode: str
                       ) -> Optional[Tuple[Dict[str, Any], int, str, bool]]:
        """/api/shared 与 /api/shared/search 共用的入参口径。不合法一律 400，不碰库。"""
        query = self._query()

        def first(name: str) -> str:
            values = query.get(name) or []
            return (values[0] or "").strip() if values else ""

        scope, err = parse_status(first("scope"), SHARED_SCOPES, "scope")
        if err:
            self._send_json(400, {"ok": False, "error": "bad_request", "detail": err}, peer)
            return None
        status, err = parse_status(first("status"), SHARED_STATUSES)
        if err:
            self._send_json(400, {"ok": False, "error": "bad_request", "detail": err}, peer)
            return None
        expired, err = parse_flag(first("expired"), "expired")
        if err or expired is None:
            self._send_json(400, {"ok": False, "error": "bad_request", "detail": err}, peer)
            return None
        semantic, err = parse_flag(first("semantic"), "semantic")
        if err or semantic is None:
            self._send_json(400, {"ok": False, "error": "bad_request", "detail": err}, peer)
            return None
        # kind / owner / tag / task_id 都是自由文本，只限长（值进参数，注入不了）
        texts = {}  # type: Dict[str, Optional[str]]
        for name in ("kind", "owner", "tag", "task_id"):
            value, err = parse_owner(first(name))
            if err:
                self._send_json(400, {"ok": False, "error": "bad_request",
                                      "detail": err.replace("owner", name)}, peer)
                return None
            texts[name] = value
        q = first("q")
        if mode == "search" and not q:
            self._send_json(400, {"ok": False, "error": "bad_request",
                                  "detail": "q 不能为空"}, peer)
            return None
        if len(q) > SEARCH_QUERY_MAX:
            self._send_json(400, {"ok": False, "error": "bad_request",
                                  "detail": "q 超过 %d 字符" % SEARCH_QUERY_MAX}, peer)
            return None
        limit = self._clamp_limit(first("limit"), SHARED_DEFAULT_LIMIT,
                                  SHARED_MAX_LIMIT, peer)
        if limit is None:
            return None
        filters = {"scope": scope, "kind": texts["kind"], "owner": texts["owner"],
                   "tag": texts["tag"], "task_id": texts["task_id"], "status": status,
                   "expired": expired}
        return filters, limit, q, semantic

    def _get_shared_list(self, peer: str, mode: str) -> None:
        """GET /api/shared 与 /api/shared/search —— 共享内容清单（只读）。

        带 q 就是检索：semantic=1 走向量，模型或向量不可用自动**降级**成关键词
        （响应里的 mode 会如实说是哪一种，绝不把关键词结果冒充成语义结果）。
        """
        parsed = self._shared_params(peer, mode)
        if parsed is None:
            return
        filters, limit, q, semantic = parsed

        degraded = []  # type: List[str]
        now = utc_now()
        try:
            where, params = shared_filters(peer, now, **filters)
            used = "list"
            if q:
                used = "keyword"
                vector = compute_embedding(q) if semantic else None
                if vector is not None and has_any_shared_embedding(degraded):
                    used = "semantic"
                if used == "semantic":
                    items = search_shared_semantic(vector, where, params, limit, now, degraded)
                else:
                    items = search_shared_keyword(q, where, params, limit, now, degraded)
            else:
                items = select_shared(where, params, limit, now, degraded)
        except DBUnavailable as exc:
            LOG.warning("共享内容查询失败 from=%s: %s", peer, _safe_err(exc))
            self._send_json(503, {"ok": False, "error": "db_unavailable",
                                  "detail": _safe_err(exc)}, peer)
            return

        payload = {"ok": True, "count": len(items), "shared": items, "mode": used,
                   "query": q or None, "degraded": degraded,
                   "now": now.isoformat(), "tz": "UTC"}
        self._send_json(200, payload, peer)

    def _get_shared_detail(self, peer: str, raw_id: str) -> None:
        """GET /api/shared/<id> —— 一条内容 + 它的改动历史（谁改过、改了哪些字段）。"""
        raw_id = urllib.parse.unquote(raw_id).strip()
        if not raw_id.isdigit() or len(raw_id) > 19:
            # id 是 bigserial：不是纯数字就没有这条，直接 404，连库都不碰
            self._send_json(404, {"ok": False, "error": "not_found",
                                  "detail": "没有这条共享内容"}, peer)
            return
        degraded = []  # type: List[str]
        now = utc_now()
        try:
            item = select_shared_one(int(raw_id), now, degraded)
            if item is None:
                self._send_json(404, {"ok": False, "error": "not_found",
                                      "detail": "没有这条共享内容"}, peer,)
                return
            if not shared_visible_to(item, peer):
                # 403 而不是 404：东西在，只是不给你看 —— 对着 audience 撒谎没有意义，
                # 请求方看见 403 才知道该去找 owner 要授权。
                LOG.warning("受众不含请求者，拒绝 content_id=%s from=%s", item["content_id"], peer)
                self._send_json(403, {"ok": False, "error": "forbidden",
                                      "detail": "这条共享内容没有共享给你"}, peer)
                return
            revisions = select_shared_revisions(item["content_id"], degraded)
        except DBUnavailable as exc:
            LOG.warning("共享内容详情查询失败 from=%s: %s", peer, _safe_err(exc))
            self._send_json(503, {"ok": False, "error": "db_unavailable",
                                  "detail": _safe_err(exc)}, peer)
            return
        self._send_json(200, {"ok": True, "shared": item, "revisions": revisions,
                              "degraded": degraded, "now": now.isoformat(),
                              "tz": "UTC"}, peer)

    def _get_shared_tasks(self, peer: str) -> None:
        """GET /api/shared/tasks?status= —— 任务容器清单（任务级内容的生命周期看它）。"""
        query = self._query()
        values = query.get("status") or []
        status, err = parse_status((values[0] or "").strip() if values else "",
                                   SHARED_TASK_STATUSES)
        if err:
            self._send_json(400, {"ok": False, "error": "bad_request", "detail": err}, peer)
            return
        values = query.get("limit") or []
        limit = self._clamp_limit((values[0] or "").strip() if values else "",
                                  SHARED_DEFAULT_LIMIT, SHARED_MAX_LIMIT, peer)
        if limit is None:
            return
        degraded = []  # type: List[str]
        now = utc_now()
        try:
            tasks = select_shared_tasks(status, limit, now, degraded)
        except DBUnavailable as exc:
            LOG.warning("共享任务查询失败 from=%s: %s", peer, _safe_err(exc))
            self._send_json(503, {"ok": False, "error": "db_unavailable",
                                  "detail": _safe_err(exc)}, peer)
            return
        self._send_json(200, {"ok": True, "count": len(tasks), "tasks": tasks,
                              "degraded": degraded, "now": now.isoformat(),
                              "tz": "UTC"}, peer)

    def _get_search(self, peer: str) -> None:
        """GET /search?q=&topic=&limit= —— 语义优先，库里没向量就退关键词。"""
        query = self._query()

        def first(name: str) -> str:
            values = query.get(name) or []
            return (values[0] or "").strip() if values else ""

        q = first("q")
        if not q:
            self._send_json(400, {"ok": False, "error": "bad_request",
                                  "detail": "q 不能为空"}, peer)
            return
        if len(q) > SEARCH_QUERY_MAX:
            self._send_json(400, {"ok": False, "error": "bad_request",
                                  "detail": "q 超过 %d 字符" % SEARCH_QUERY_MAX}, peer)
            return

        limit_raw = first("limit")
        limit = SEARCH_DEFAULT_LIMIT
        if limit_raw:
            try:
                limit = int(limit_raw)
            except ValueError:
                self._send_json(400, {"ok": False, "error": "bad_request",
                                      "detail": "limit 必须是整数"}, peer)
                return
            if limit <= 0:
                limit = SEARCH_DEFAULT_LIMIT
            limit = min(limit, SEARCH_MAX_LIMIT)

        topic = first("topic") or None

        # 先算查询向量（嵌入不可用就直接是关键词模式，连库都不用多查一次）。
        vector = compute_embedding(q)
        try:
            mode = "keyword"
            if vector is not None and has_any_embedding():
                mode = "semantic"
            if mode == "semantic":
                entries = search_semantic(vector, topic, limit)
            else:
                entries = search_keyword(q, topic, limit)
        except DBUnavailable as exc:
            LOG.warning("检索失败 from=%s: %s", peer, _safe_err(exc))
            self._send_json(503, {"ok": False, "error": "db_unavailable",
                                  "detail": _safe_err(exc)}, peer)
            return

        self._send_json(200, {"ok": True, "mode": mode, "query": q,
                              "count": len(entries), "entries": entries}, peer)

    def _get_entries(self, peer: str) -> None:
        query = self._query()

        def first(name: str) -> str:
            values = query.get(name) or []
            return (values[0] or "").strip() if values else ""

        limit_raw = first("limit")
        limit = DEFAULT_LIMIT
        if limit_raw:
            try:
                limit = int(limit_raw)
            except ValueError:
                self._send_json(400, {"ok": False, "error": "bad_request",
                                      "detail": "limit 必须是整数"}, peer)
                return
            if limit <= 0:
                limit = DEFAULT_LIMIT
            limit = min(limit, MAX_LIMIT)

        since, err = parse_since(first("since"))
        if err:
            self._send_json(400, {"ok": False, "error": "bad_request", "detail": err}, peer)
            return

        try:
            entries = select_entries(first("topic") or None, first("kind") or None,
                                     first("author") or None, limit, since,
                                     first("status") or None)
        except DBUnavailable as exc:
            LOG.warning("查询失败 from=%s: %s", peer, _safe_err(exc))
            self._send_json(503, {"ok": False, "error": "db_unavailable",
                                  "detail": _safe_err(exc)}, peer)
            return

        self._send_json(200, {"ok": True, "entries": entries, "count": len(entries)}, peer)

    # --- 临时共享区（tmp box） ---------------------------------------
    def _tmp_write(self, peer: str, raw: bytes, too_large: bool) -> None:
        """POST /tmp —— 存正文，换一个短 id。owner 一律取鉴权结果（防冒名）。"""
        if too_large:
            self._send_json(413, {"ok": False, "error": "payload_too_large",
                                  "detail": "请求体上限 %d 字节" % MAX_BODY_BYTES}, peer)
            return
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            self._send_json(400, {"ok": False, "error": "bad_request",
                                  "detail": "body 不是合法 JSON"}, peer)
            return
        if not isinstance(body, dict):
            self._send_json(400, {"ok": False, "error": "bad_request",
                                  "detail": "body 必须是 JSON 对象"}, peer)
            return

        payload, err, code = validate_tmp_payload(body)
        if err or payload is None:
            self._send_json(code, {"ok": False,
                                   "error": "payload_too_large" if code == 413 else "bad_request",
                                   "detail": err}, peer)
            return
        if body.get("owner") and str(body.get("owner")) != peer:
            LOG.warning("忽略请求体里的 owner 字段 from=%s", peer)

        try:
            active = tmp_owner_active_count(peer)
            if active >= TMP_OWNER_ACTIVE_MAX:
                self._send_json(429, {"ok": False, "error": "quota_exceeded",
                                      "detail": "活跃临时共享已达 %d 条（先等旧的销毁或过期）"
                                                % TMP_OWNER_ACTIVE_MAX}, peer)
                return
            result = insert_tmp(peer, payload["to"], payload["title"], payload["body"],
                                payload["reads"], payload["ttl_s"])
            # 全局容量兜底：超过 TMP_ACTIVE_MAX 就按 expires_at 最早优先销毁（reason=capacity）
            tmp_enforce_capacity()
        except DBUnavailable as exc:
            LOG.warning("tmp 写入失败 from=%s: %s", peer, _safe_err(exc))
            self._send_json(503, {"ok": False, "error": "db_unavailable",
                                  "detail": _safe_err(exc)}, peer)
            return

        self._send_json(200, {"ok": True, "tmp_id": result["tmp_id"],
                              "expires_at": result["expires_at"],
                              "reads_max": result["reads_max"],
                              "bytes": result["bytes"]}, peer)

    def _tmp_gone(self, peer: str, item: Dict[str, Any], reason: str,
                  destroyed_at: Optional[str] = None) -> None:
        """410：正文已经（或刚刚）销毁 —— destroy_reason 一并返回，调用方才知道是读满还是过期。"""
        self._send_json(410, {"ok": False, "error": "gone", "tmp_id": item.get("tmp_id"),
                              "destroy_reason": reason,
                              "destroyed_at": destroyed_at if destroyed_at is not None
                              else item.get("destroyed_at")}, peer)

    def _tmp_read(self, peer: str, raw_id: str) -> None:
        """GET /tmp/<id> —— 取全文。按 distinct reader 计数；读满/过期都在本请求内销毁。"""
        tmp_id = urllib.parse.unquote(raw_id or "").strip()
        if not tmp_id_ok(tmp_id):
            self._send_json(404, {"ok": False, "error": "not_found",
                                  "detail": "没有这条临时共享"}, peer)
            return
        try:
            item = select_tmp(tmp_id)
        except DBUnavailable as exc:
            LOG.warning("tmp 读取失败 from=%s: %s", peer, _safe_err(exc))
            self._send_json(503, {"ok": False, "error": "db_unavailable",
                                  "detail": _safe_err(exc)}, peer)
            return
        if item is None:
            self._send_json(404, {"ok": False, "error": "not_found",
                                  "detail": "没有这条临时共享"}, peer)
            return
        # 403 比 404 诚实：东西在，不给你。先把权限判掉，别把"存在/已销毁"泄露给无关的人。
        if item["owner"] != peer and peer not in item["audience"]:
            LOG.warning("受众不含请求者，拒绝 tmp_id=%s from=%s", tmp_id, peer)
            self._send_json(403, {"ok": False, "error": "forbidden",
                                  "detail": "这条临时共享没有共享给你"}, peer)
            return
        if item["destroyed"]:
            self._tmp_gone(peer, item, item.get("destroy_reason") or "destroyed")
            return
        if item["expired"]:
            # 已过期但还没被 sweep 扫到：这个请求里顺手销毁，destroy_reason='expired'
            ts = None
            try:
                ts = tmp_expire_now(tmp_id)
            except DBUnavailable as exc:
                LOG.warning("tmp 过期销毁失败 tmp_id=%s: %s", tmp_id, _safe_err(exc))
            self._tmp_gone(peer, item, "expired",
                           destroyed_at=ts or _iso_z(utc_now()))
            return

        body = item["body"]
        repeat = False
        destroyed = False
        reads_count = item["reads_count"]
        reads_max = item["reads_max"]
        if peer in item["readers"]:
            # 同一 reader 重读：正文照给，计数不增，repeat=true（重试/刷新不该吃配额）
            repeat = True
        else:
            try:
                got = tmp_counted_read(tmp_id, peer)
                if got is None:
                    # 并发：刚好被别的请求抢先销毁 / 同一个人的并发读。回读一次真源再定口径。
                    fresh = select_tmp(tmp_id)
                    if fresh is None or fresh["destroyed"] or fresh["expired"]:
                        if fresh is not None:
                            self._tmp_gone(peer, fresh, fresh.get("destroy_reason") or "gone")
                        else:
                            self._tmp_gone(peer, item, "gone")
                        return
                    if peer in fresh["readers"]:
                        repeat = True
                        reads_count = fresh["reads_count"]
                        reads_max = fresh["reads_max"]
                    else:
                        self._send_json(503, {"ok": False, "error": "db_unavailable",
                                              "detail": "并发读冲突，请重试"}, peer)
                        return
                else:
                    reads_count, reads_max, destroyed, _destroyed_at = got
            except DBUnavailable as exc:
                LOG.warning("tmp 计数读失败 from=%s tmp_id=%s: %s", peer, tmp_id, _safe_err(exc))
                self._send_json(503, {"ok": False, "error": "db_unavailable",
                                      "detail": _safe_err(exc)}, peer)
                return

        self._send_json(200, {"ok": True, "tmp_id": tmp_id, "title": item["title"],
                              "body": body, "bytes": item["bytes"],
                              "reads_count": reads_count, "reads_max": reads_max,
                              "reads_left": max(0, int(reads_max) - int(reads_count)),
                              "repeat": repeat, "destroyed": destroyed,
                              "destroy_reason": "read_quota" if destroyed else None,
                              "expires_at": item["expires_at"]}, peer)

    def _tmp_list(self, peer: str) -> None:
        """GET /tmp?mine=1&inbox=1&limit= —— 清单，**不吐正文**（省流量，也防误扩大读取面）。"""
        query = self._query()

        def first(name: str) -> str:
            values = query.get(name) or []
            return (values[0] or "").strip() if values else ""

        mine, err = parse_flag(first("mine"), "mine")
        if err or mine is None:
            self._send_json(400, {"ok": False, "error": "bad_request", "detail": err}, peer)
            return
        inbox, err = parse_flag(first("inbox"), "inbox")
        if err or inbox is None:
            self._send_json(400, {"ok": False, "error": "bad_request", "detail": err}, peer)
            return
        limit = self._clamp_limit(first("limit"), TMP_LIST_DEFAULT_LIMIT,
                                 TMP_LIST_MAX_LIMIT, peer)
        if limit is None:
            return

        if mine and not inbox:
            mode = "mine"
            where = "owner = %s"
            params = [peer]  # type: List[Any]
        elif inbox and not mine:
            mode = "inbox"
            where = "destroyed_at IS NULL AND expires_at > now() AND %s = ANY(audience)"
            params = [peer]
        else:
            # 缺省（以及两个都给）= 两者合并；inbox 那半边只要未销毁未过期的
            mode = "both"
            where = ("(owner = %s OR (destroyed_at IS NULL AND expires_at > now() "
                     "AND %s = ANY(audience)))")
            params = [peer, peer]

        now = utc_now()
        sql = "SELECT %s FROM tmp_shared WHERE %s ORDER BY created_at DESC LIMIT %%s" % (
            TMP_COLUMNS, where)
        try:
            rows = db_query(sql, tuple(params) + (limit,), "all") or []
        except DBUnavailable as exc:
            LOG.warning("tmp 清单查询失败 from=%s: %s", peer, _safe_err(exc))
            self._send_json(503, {"ok": False, "error": "db_unavailable",
                                  "detail": _safe_err(exc)}, peer)
            return
        items = [row_to_tmp(row, now, with_body=False) for row in rows]
        self._send_json(200, {"ok": True, "mode": mode, "count": len(items),
                              "items": items, "now": now.isoformat(), "tz": "UTC"}, peer)

    def _tmp_delete(self, peer: str, raw_id: str) -> None:
        """DELETE /tmp/<id> —— 仅 owner 可调；已销毁则幂等 200。"""
        tmp_id = urllib.parse.unquote(raw_id or "").strip()
        if not tmp_id_ok(tmp_id):
            self._send_json(404, {"ok": False, "error": "not_found",
                                  "detail": "没有这条临时共享"}, peer)
            return
        try:
            item = select_tmp(tmp_id)
            if item is None:
                self._send_json(404, {"ok": False, "error": "not_found",
                                      "detail": "没有这条临时共享"}, peer)
                return
            if item["owner"] != peer:
                LOG.warning("非 owner 试图销毁 tmp_id=%s from=%s owner=%s",
                            tmp_id, peer, item["owner"])
                self._send_json(403, {"ok": False, "error": "forbidden",
                                      "detail": "只有写入者能销毁这条临时共享"}, peer)
                return
            if item["destroyed"]:
                # 幂等：已经是销毁态，如实回它当初为什么销毁
                self._send_json(200, {"ok": True, "tmp_id": tmp_id,
                                      "destroyed_at": item["destroyed_at"],
                                      "destroy_reason": item.get("destroy_reason")}, peer)
                return
            destroyed_at = tmp_destroy_by_owner(tmp_id)
            if destroyed_at is None:
                fresh = select_tmp(tmp_id)
                if fresh is not None:
                    self._send_json(200, {"ok": True, "tmp_id": tmp_id,
                                          "destroyed_at": fresh["destroyed_at"],
                                          "destroy_reason": fresh.get("destroy_reason")}, peer)
                    return
                destroyed_at = _iso_z(utc_now())
        except DBUnavailable as exc:
            LOG.warning("tmp 主动销毁失败 from=%s tmp_id=%s: %s", peer, tmp_id, _safe_err(exc))
            self._send_json(503, {"ok": False, "error": "db_unavailable",
                                  "detail": _safe_err(exc)}, peer)
            return
        self._send_json(200, {"ok": True, "tmp_id": tmp_id,
                              "destroyed_at": destroyed_at,
                              "destroy_reason": "owner"}, peer)

    # --- Agent 会话（/sess） -----------------------------------------
    def _sess_open(self, peer: str, raw: bytes, too_large: bool) -> None:
        """POST /sess —— 开一个会话。owner 一律取鉴权结果（防冒名）。"""
        if too_large:
            self._send_json(413, {"ok": False, "error": "payload_too_large",
                                  "detail": "请求体上限 %d 字节" % MAX_BODY_BYTES}, peer)
            return
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            self._send_json(400, {"ok": False, "error": "bad_request",
                                  "detail": "body 不是合法 JSON"}, peer)
            return
        if not isinstance(body, dict):
            self._send_json(400, {"ok": False, "error": "bad_request",
                                  "detail": "body 必须是 JSON 对象"}, peer)
            return

        payload, err = validate_sess_payload(body)
        if err or payload is None:
            self._send_json(400, {"ok": False, "error": "bad_request", "detail": err}, peer)
            return
        if body.get("owner") and str(body.get("owner")) != peer:
            LOG.warning("忽略请求体里的 owner 字段 from=%s", peer)

        try:
            session = insert_agent_session(peer, payload["topic"], payload["title"],
                                           payload["members"], payload["idle_ttl_s"])
        except DBUnavailable as exc:
            LOG.warning("开会话失败 from=%s: %s", peer, _safe_err(exc))
            self._send_json(503, {"ok": False, "error": "db_unavailable",
                                  "detail": _safe_err(exc)}, peer)
            return

        self._send_json(200, {"ok": True, "session_id": session["session_id"],
                              "topic": session["topic"], "state": session["state"],
                              "title": session["title"], "owner": session["owner"],
                              "members": session["members"],
                              "idle_ttl_s": session["idle_ttl_s"],
                              "turn_count": session["turn_count"],
                              "created_at": session["created_at"]}, peer)

    def _sess_list(self, peer: str) -> None:
        """GET /sess?topic=&state=&limit=&since= —— 会话清单（口径与会议 GET /sessions 对齐）。

        **不按"我参没参加"过滤**：与会议列表同一个口径（黑板/会议/会话都是全队可见的协作面），
        按 id 取详情、取流水、关闭才判"是不是成员"（非成员 403）。
        """
        query = self._query()

        def first(name: str) -> str:
            values = query.get(name) or []
            return (values[0] or "").strip() if values else ""

        limit = self._clamp_limit(first("limit"), SESS_LIST_DEFAULT_LIMIT,
                                  SESS_LIST_MAX_LIMIT, peer)
        if limit is None:
            return
        state, err = parse_status(first("state"), SESS_STATES, "state")
        if err:
            self._send_json(400, {"ok": False, "error": "bad_request", "detail": err}, peer)
            return
        topic = first("topic") or None
        if topic and len(topic) > SESS_TOPIC_MAX:
            self._send_json(400, {"ok": False, "error": "bad_request",
                                  "detail": "topic 超过 %d 字符" % SESS_TOPIC_MAX}, peer)
            return
        since, err = parse_since(first("since"))
        if err:
            self._send_json(400, {"ok": False, "error": "bad_request", "detail": err}, peer)
            return

        try:
            sessions = select_agent_sessions(state, topic, limit, since)
        except DBUnavailable as exc:
            LOG.warning("会话列表查询失败 from=%s: %s", peer, _safe_err(exc))
            self._send_json(503, {"ok": False, "error": "db_unavailable",
                                  "detail": _safe_err(exc)}, peer)
            return

        self._send_json(200, {"ok": True, "count": len(sessions), "sessions": sessions,
                              "now": _iso_z(utc_now()), "tz": "UTC"}, peer)

    def _sess_detail(self, peer: str, raw_id: str) -> None:
        """GET /sess/<id> —— 会话详情 + 最近若干轮（默认 20 轮）。非成员 403。"""
        session_id = urllib.parse.unquote(raw_id or "").strip()
        if not session_id_ok(session_id):
            self._send_json(404, {"ok": False, "error": "not_found",
                                  "detail": "没有这个会话"}, peer)
            return
        query = self._query()
        values = query.get("limit") or []
        limit = self._clamp_limit((values[0] or "").strip() if values else "",
                                  SESS_DETAIL_TURNS_DEFAULT, SESS_TURNS_MAX_LIMIT, peer)
        if limit is None:
            return
        try:
            session = select_agent_session(session_id)
            if session is None:
                self._send_json(404, {"ok": False, "error": "not_found",
                                      "detail": "没有这个会话"}, peer)
                return
            if not sess_visible_to(session, peer):
                LOG.warning("非成员读会话 session_id=%s from=%s", session_id, peer)
                self._send_json(403, {"ok": False, "error": "forbidden",
                                      "detail": "这个会话没有邀请你"}, peer)
                return
            turns = select_agent_turns(session_id, limit)
        except DBUnavailable as exc:
            LOG.warning("会话详情查询失败 from=%s: %s", peer, _safe_err(exc))
            self._send_json(503, {"ok": False, "error": "db_unavailable",
                                  "detail": _safe_err(exc)}, peer)
            return

        self._send_json(200, {"ok": True, "session": session, "turns": turns,
                              "count": len(turns), "now": _iso_z(utc_now()),
                              "tz": "UTC"}, peer)

    def _sess_turns(self, peer: str, raw_id: str) -> None:
        """GET /sess/<id>/turns?limit= —— 取流水（最近 limit 轮，按 seq 升序返回）。"""
        session_id = urllib.parse.unquote(raw_id or "").strip()
        if not session_id_ok(session_id):
            self._send_json(404, {"ok": False, "error": "not_found",
                                  "detail": "没有这个会话"}, peer)
            return
        query = self._query()
        values = query.get("limit") or []
        limit = self._clamp_limit((values[0] or "").strip() if values else "",
                                  SESS_TURNS_DEFAULT_LIMIT, SESS_TURNS_MAX_LIMIT, peer)
        if limit is None:
            return
        try:
            session = select_agent_session(session_id)
            if session is None:
                self._send_json(404, {"ok": False, "error": "not_found",
                                      "detail": "没有这个会话"}, peer)
                return
            if not sess_visible_to(session, peer):
                LOG.warning("非成员读会话流水 session_id=%s from=%s", session_id, peer)
                self._send_json(403, {"ok": False, "error": "forbidden",
                                      "detail": "这个会话没有邀请你"}, peer)
                return
            turns = select_agent_turns(session_id, limit)
        except DBUnavailable as exc:
            LOG.warning("会话流水查询失败 from=%s: %s", peer, _safe_err(exc))
            self._send_json(503, {"ok": False, "error": "db_unavailable",
                                  "detail": _safe_err(exc)}, peer)
            return

        self._send_json(200, {"ok": True, "session_id": session_id,
                              "state": session["state"], "count": len(turns),
                              "turn_count": session["turn_count"], "turns": turns,
                              "now": _iso_z(utc_now()), "tz": "UTC"}, peer)

    def _sess_close(self, peer: str, raw_id: str, raw: bytes, too_large: bool) -> None:
        """POST /sess/<id>/close —— 关闭 + **同时归档**（契约 §5/§4.3）。已关闭则幂等 200。"""
        session_id = urllib.parse.unquote(raw_id or "").strip()
        if not session_id_ok(session_id):
            self._send_json(404, {"ok": False, "error": "not_found",
                                  "detail": "没有这个会话"}, peer)
            return
        if too_large:
            self._send_json(413, {"ok": False, "error": "payload_too_large",
                                  "detail": "请求体上限 %d 字节" % MAX_BODY_BYTES}, peer)
            return
        note = None  # type: Optional[str]
        if raw:
            try:
                body = json.loads(raw.decode("utf-8"))
            except Exception:
                self._send_json(400, {"ok": False, "error": "bad_request",
                                      "detail": "body 不是合法 JSON"}, peer)
                return
            if not isinstance(body, dict):
                self._send_json(400, {"ok": False, "error": "bad_request",
                                      "detail": "body 必须是 JSON 对象"}, peer)
                return
            note, err = validate_close_note(body)
            if err:
                self._send_json(400, {"ok": False, "error": "bad_request",
                                      "detail": err}, peer)
                return
            note = note or None

        try:
            session = select_agent_session(session_id)
            if session is None:
                self._send_json(404, {"ok": False, "error": "not_found",
                                      "detail": "没有这个会话"}, peer)
                return
            if not sess_visible_to(session, peer):
                LOG.warning("非成员试图关闭会话 session_id=%s from=%s", session_id, peer)
                self._send_json(403, {"ok": False, "error": "forbidden",
                                      "detail": "这个会话没有邀请你"}, peer)
                return
            if session["state"] != "open":
                # 幂等：已经是终态，如实回现状（**不重复归档**，也不再写库）
                self._send_json(200, self._sess_close_payload(session, already=True), peer)
                return
            result = close_agent_session(session_id, SESS_CLOSE_REASON_MANUAL, note)
            fresh = result.get("session") if result.get("claimed") else None
            if fresh is None:
                # 并发：别的请求刚好抢先认领了状态迁移 —— 回读真源，如实回报（不重复归档）
                fresh = select_agent_session(session_id) or session
                self._send_json(200, self._sess_close_payload(fresh, already=True), peer)
                return
        except DBUnavailable as exc:
            LOG.warning("关闭会话失败 from=%s session_id=%s: %s",
                        peer, session_id, _safe_err(exc))
            self._send_json(503, {"ok": False, "error": "db_unavailable",
                                  "detail": _safe_err(exc)}, peer)
            return

        payload = self._sess_close_payload(fresh, already=False)
        payload["turns_cleared"] = result.get("turns_cleared") or 0
        self._send_json(200, payload, peer)

    # --- /sess/<id>/turns：会话流水（契约 §5）-------------------------------------
    def _sess_turn_write(self, peer: str, raw_id: str, raw: bytes, too_large: bool) -> None:
        """POST /sess/<id>/turns —— 追加一轮对话流水。

        为什么非要有它：**中枢统一记，Agent 侧无状态**（D3）。会话上下文要能回放，
        前提是每一轮都先落到这里；否则 9940 只是开了个空会话，谁说过什么都查不着。
        from_member 一律取鉴权结果；seq / bytes / ts 由触发器给。
        """
        session_id = urllib.parse.unquote(raw_id or "").strip()
        if not session_id_ok(session_id):
            self._send_json(404, {"ok": False, "error": "not_found",
                                  "detail": "没有这个会话"}, peer)
            return
        if too_large:
            self._send_json(413, {"ok": False, "error": "payload_too_large",
                                  "detail": "请求体上限 %d 字节" % MAX_BODY_BYTES}, peer)
            return
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            self._send_json(400, {"ok": False, "error": "bad_request",
                                  "detail": "body 不是合法 JSON"}, peer)
            return
        if not isinstance(body, dict):
            self._send_json(400, {"ok": False, "error": "bad_request",
                                  "detail": "body 必须是 JSON 对象"}, peer)
            return

        payload, err, code = validate_turn_payload(body)
        if err or payload is None:
            self._send_json(code, {"ok": False,
                                   "error": "payload_too_large" if code == 413 else "bad_request",
                                   "detail": err}, peer)
            return
        claimed = body.get("from_member") or body.get("from")
        if claimed:
            LOG.warning("忽略 turns 请求体里的 from 字段（身份只认 token）claimed=%s actual=%s",
                        claimed, peer)

        try:
            session = select_agent_session(session_id)
            if session is None:
                self._send_json(404, {"ok": False, "error": "not_found",
                                      "detail": "没有这个会话"}, peer)
                return
            if not sess_visible_to(session, peer):
                LOG.warning("非成员写会话流水 session_id=%s from=%s", session_id, peer)
                self._send_json(403, {"ok": False, "error": "forbidden",
                                      "detail": "这个会话没有邀请你"}, peer)
                return
            turn = insert_agent_turn(session_id, peer, payload["to_member"],
                                     payload["role"], payload["content"],
                                     payload["content_ref"])
        except SessionNotOpen as exc:
            LOG.warning("终态会话写流水 session_id=%s from=%s: %s",
                        session_id, peer, _safe_err(exc))
            self._send_json(409, {"ok": False, "error": "session_not_open",
                                  "detail": "这场会话已经结束了，不能再追加轮次"}, peer)
            return
        except DBUnavailable as exc:
            LOG.warning("会话流水写入失败 session_id=%s from=%s: %s",
                        session_id, peer, _safe_err(exc))
            self._send_json(503, {"ok": False, "error": "db_unavailable",
                                  "detail": _safe_err(exc)}, peer)
            return

        self._send_json(201, {"ok": True, "session_id": session_id, "turn": turn,
                              "now": _iso_z(utc_now()), "tz": "UTC"}, peer)

    # --- /msg：消息可靠投递（落库=送达）--------------------------------------------
    def _msg_write(self, peer: str, raw: bytes, too_large: bool) -> None:
        """POST /msg —— 落库。**落库成功 = 送达**；from 一律取鉴权结果（防冒名）。"""
        if too_large:
            self._send_json(413, {"ok": False, "error": "payload_too_large",
                                  "detail": "请求体上限 %d 字节" % MAX_BODY_BYTES}, peer)
            return
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            self._send_json(400, {"ok": False, "error": "bad_request",
                                  "detail": "body 不是合法 JSON"}, peer)
            return
        if not isinstance(body, dict):
            self._send_json(400, {"ok": False, "error": "bad_request",
                                  "detail": "body 必须是 JSON 对象"}, peer)
            return

        payload, err, code = validate_msg_payload(body)
        if err or payload is None:
            self._send_json(code, {"ok": False,
                                   "error": "payload_too_large" if code == 413 else "bad_request",
                                   "detail": err}, peer)
            return
        if body.get("from") and str(body.get("from")) != peer:
            LOG.warning("忽略 msg 请求体里的 from 字段（身份只认 token）claimed=%s actual=%s",
                        body.get("from"), peer)

        try:
            result = insert_msg(peer, payload["to"], payload["body"], payload["content_ref"],
                                payload["session_id"], payload["topic"], payload["ttl_s"],
                                payload["msg_id"])
        except DBUnavailable as exc:
            LOG.warning("msg 落库失败 from=%s: %s", peer, _safe_err(exc))
            self._send_json(503, {"ok": False, "error": "db_unavailable",
                                  "detail": _safe_err(exc)}, peer)
            return

        out = {"ok": True, "delivered": True}   # 落库即送达：发送方这下就"知道"了
        out.update(result)
        self._send_json(200, out, peer)

    def _msg_get(self, peer: str, raw_id: str) -> None:
        """GET /msg/<id> —— 发送方查「送没送到、对方开没开工」。"""
        if not msg_id_ok(raw_id):
            self._send_json(404, {"ok": False, "error": "not_found",
                                  "detail": "msg_id 形状不对"}, peer)
            return
        try:
            msg = get_msg(raw_id)
        except DBUnavailable as exc:
            self._send_json(503, {"ok": False, "error": "db_unavailable",
                                  "detail": _safe_err(exc)}, peer)
            return
        if msg is None:
            self._send_json(404, {"ok": False, "error": "not_found"}, peer)
            return
        if peer not in (msg["from"], msg["to"]):
            self._send_json(403, {"ok": False, "error": "forbidden",
                                  "detail": "只允许发件人或收件人查看"}, peer)
            return
        self._send_json(200, {"ok": True, "msg": msg}, peer)

    def _msg_list(self, peer: str, query: Dict[str, List[str]]) -> None:
        """GET /msg?to=<member>&state=queued,processing —— 收件箱（上线补拉用）。"""
        to = (query.get("to") or [peer])[0]
        if to != peer:
            self._send_json(403, {"ok": False, "error": "forbidden",
                                  "detail": "只能查自己的收件箱"}, peer)
            return
        raw_states = (query.get("state") or [",".join(MSG_OPEN_STATES)])[0]
        states = [s.strip() for s in raw_states.split(",") if s.strip()]
        bad = [s for s in states if s not in MSG_STATES]
        if bad:
            self._send_json(400, {"ok": False, "error": "bad_request",
                                  "detail": "未知 state: %s" % ",".join(bad)}, peer)
            return
        try:
            limit = int((query.get("limit") or [str(MSG_LIST_DEFAULT_LIMIT)])[0])
        except Exception:
            self._send_json(400, {"ok": False, "error": "bad_request",
                                  "detail": "limit 必须是整数"}, peer)
            return
        limit = max(1, min(MSG_LIST_MAX_LIMIT, limit))
        try:
            items = list_inbox(peer, states, limit)
        except DBUnavailable as exc:
            self._send_json(503, {"ok": False, "error": "db_unavailable",
                                  "detail": _safe_err(exc)}, peer)
            return
        self._send_json(200, {"ok": True, "count": len(items), "to": peer,
                              "states": states, "messages": items}, peer)

    def _msg_ack(self, peer: str, raw_id: str) -> None:
        """POST /msg/<id>/ack —— 收件方说「已收到并开始处理」。

        这就是<USER_ID>要的那句反馈：「我发送消息后，类似 TCP 一样，对方要反馈
        '已经收到并且开始处理'，让发送方知道已经送达。」
        """
        if not msg_id_ok(raw_id):
            self._send_json(404, {"ok": False, "error": "not_found"}, peer)
            return
        try:
            current = get_msg(raw_id)
            if current is None:
                self._send_json(404, {"ok": False, "error": "not_found"}, peer)
                return
            if current["to"] != peer:
                self._send_json(403, {"ok": False, "error": "forbidden",
                                      "detail": "只有收件人可以 ack"}, peer)
                return
            msg = msg_ack(raw_id)
        except DBUnavailable as exc:
            self._send_json(503, {"ok": False, "error": "db_unavailable",
                                  "detail": _safe_err(exc)}, peer)
            return
        if msg is None:
            # 已经 ack 过、或已经是终态：不是错误，是幂等重复 —— 如实回报当前状态
            self._send_json(200, {"ok": True, "acked": current["acked"],
                                  "state": current["state"], "repeat": True,
                                  "msg": current}, peer)
            return
        self._send_json(200, {"ok": True, "acked": True, "repeat": False, "msg": msg}, peer)

    def _msg_done(self, peer: str, raw_id: str, raw: bytes, too_large: bool) -> None:
        """POST /msg/<id>/done —— 收件方报「处理完了」或「处理失败」。

        两级确认的第二级：ack 只证明「收到并开工」，这里才是「有结论了」。
        """
        if not msg_id_ok(raw_id):
            self._send_json(404, {"ok": False, "error": "not_found"}, peer)
            return
        body = {}  # type: Dict[str, Any]
        if raw and not too_large:
            try:
                parsed = json.loads(raw.decode("utf-8"))
                if isinstance(parsed, dict):
                    body = parsed
            except Exception:
                body = {}
        ok_flag = body.get("ok", True)
        if not isinstance(ok_flag, bool):
            self._send_json(400, {"ok": False, "error": "bad_request",
                                  "detail": "ok 必须是布尔"}, peer)
            return
        err_text = body.get("error")
        if err_text is not None and not isinstance(err_text, str):
            self._send_json(400, {"ok": False, "error": "bad_request",
                                  "detail": "error 必须是字符串"}, peer)
            return
        if err_text and len(err_text) > 1000:
            err_text = err_text[:1000]
        to_state = "done" if ok_flag else "failed"
        try:
            current = get_msg(raw_id)
            if current is None:
                self._send_json(404, {"ok": False, "error": "not_found"}, peer)
                return
            if current["to"] != peer:
                self._send_json(403, {"ok": False, "error": "forbidden",
                                      "detail": "只有收件人可以报完成"}, peer)
                return
            msg = msg_finish(raw_id, to_state, err_text)
        except DBUnavailable as exc:
            self._send_json(503, {"ok": False, "error": "db_unavailable",
                                  "detail": _safe_err(exc)}, peer)
            return
        if msg is None:
            self._send_json(200, {"ok": True, "state": current["state"],
                                  "repeat": True, "msg": current}, peer)
            return
        self._send_json(200, {"ok": True, "state": msg["state"],
                              "repeat": False, "msg": msg}, peer)

    @staticmethod
    def _sess_close_payload(session: Dict[str, Any], already: bool) -> Dict[str, Any]:
        """关闭响应的公共形状。archive_ref 为空 = 归档还没落成（sweep 会补扫）。"""
        return {"ok": True, "session_id": session["session_id"], "state": session["state"],
                "close_reason": session.get("close_reason"),
                "closed_at": session.get("closed_at"),
                "archive_ref": session.get("archive_ref"),
                "archived": session.get("archive_ref") is not None,
                "turn_count": session.get("turn_count"),
                "already_closed": already}

    def _reject_write(self) -> bool:
        """共享内容的任何写请求一律 405（带 Allow: GET），**一个字节都不碰库**。

        写在服务里只会多一条能改别人共享信息的路。发布 / 刷新 / 撤下 / 销毁全走
        shared_content.py（凭据在本机文件里，人或 Agent 显式执行），库里还有触发器兜底。
        """
        if not self._is_shared_path(self._path_parts()):
            return False
        self._send_json(405, {"ok": False, "error": "method_not_allowed",
                              "detail": "共享内容接口只读；发布与修改请用 shared_content.py"},
                        None, [("Allow", "GET")])
        return True

    # 非 GET 方法的分派全在 L1（模块顶部的 @GW.route 登记表）：这里只把方法接上
    # L1 的统一入口。PUT/PATCH 一条可登记的路径都没有（「不是共享路径就 501」），
    # 但 do_<METHOD> 必须留着 —— 否则落回基类 501 时**请求体不会被读完**，
    # keep-alive 连接就错位了。
    def do_PUT(self) -> None:     # noqa: N802
        self._handle()

    def do_PATCH(self) -> None:   # noqa: N802
        self._handle()

    def _post_entry(self, raw: bytes, too_large: bool) -> None:
        """POST /entries 的正体（迁移前内联在 do_POST 尾部，一行没动）。"""
        peer = self._auth_peer()
        if peer is None:
            self._send_json(401, {"ok": False, "error": "unauthorized"})
            return

        if too_large:
            self._send_json(413, {"ok": False, "error": "payload_too_large",
                                  "detail": "请求体上限 %d 字节" % MAX_BODY_BYTES}, peer)
            return

        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            self._send_json(400, {"ok": False, "error": "bad_request",
                                  "detail": "body 不是合法 JSON"}, peer)
            return
        if not isinstance(body, dict):
            self._send_json(400, {"ok": False, "error": "bad_request",
                                  "detail": "body 必须是 JSON 对象"}, peer)
            return

        payload, err = validate_entry_payload(body)
        if err or payload is None:
            self._send_json(400, {"ok": False, "error": "bad_request", "detail": err}, peer)
            return

        # author 一律取鉴权结果，请求体里传了也当没看见 —— 防冒名。
        if body.get("author") and str(body.get("author")) != peer:
            LOG.warning("忽略请求体里的 author 字段 from=%s", peer)

        # 客户端没自带向量就本地补算。**算不出来就留 NULL，照常 200** —— 黑板的
        # 写入路径不能因为一个可选的检索增强而失败；漏掉的行事后用
        # backfill_embeddings.py 补即可。只编码 content：查询侧也是裸文本，
        # 两边口径一致，向量空间才可比。
        embedding = payload["embedding"]
        if embedding is None:
            embedding = compute_embedding(payload["content"])

        try:
            result = insert_entry(peer, payload["topic"], payload["kind"],
                                  payload["content"], payload["refs"], embedding)
        except DBUnavailable as exc:
            LOG.warning("写入失败 from=%s: %s", peer, _safe_err(exc))
            self._send_json(503, {"ok": False, "error": "db_unavailable",
                                  "detail": _safe_err(exc)}, peer)
            return

        self._send_json(200, {"ok": True, "entry_id": result["entry_id"],
                              "ts": result["ts"]}, peer)


# --- 本机健康口：整件收进 L1（阶段2 刀7a）-----------------------------

ADMIN_GW = web_gateway.Gateway("blackboard_admin", version=VERSION, quiet=True)


@ADMIN_GW.route("/healthz")
def _admin_healthz(req: "web_gateway.Request") -> Any:
    """唯一的路径；JSON 编码与 Content-Type 由 L1 的 `_envelope()` 出。"""
    return health_payload()


@ADMIN_GW.not_found
def _admin_not_found(req: "web_gateway.Request") -> Any:
    """404 体是**手写的紧凑字节**（没有 json.dumps 的那两个空格），一个字节都不许动，
    所以直接给 bytes —— L1 见 bytes 原样发出，Content-Type 由本件自己带。"""
    return 404, b'{"ok":false,"error":"not_found"}', {"Content-Type": JSON_CT}


_AdminGatewayHandler = ADMIN_GW.handler_class()


class AdminHandler(_AdminGatewayHandler):
    """只绑 127.0.0.1 的最小健康口：只有 /healthz，不碰任何业务数据。

    传输职责全在 L1；本件只留两处差异：`sys_version=""` + HTTP/1.1（保住
    `Server: blackboard_admin/<VERSION>` 与 keep-alive），以及**路径归一**——
    迁移前是 `urlsplit(path).path != "/healthz"` 的严格比对，L1 默认还会 rstrip
    尾斜杠、且不剥 #fragment，两边口径不同（`/healthz/` 迁移前是 404、
    `/healthz#x` 迁移前是 200），所以这里照迁移前的口径重算一次 req.path。
    """

    server_version = "blackboard_admin/" + VERSION
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def _handle(self) -> None:
        req = web_gateway.Request(self)
        req.path = urllib.parse.urlsplit(self.path).path
        status, body, headers = ADMIN_GW.dispatch(req)
        self._reply(status, body, headers)


# ---------------------------------------------------------------- 入口

def setup_logging(log_file: Optional[str]) -> None:
    """单行日志：stdout（+ 可选文件）。日志里永远不出现 token / 口令 / DSN。"""
    handlers = [logging.StreamHandler(sys.stdout)]  # type: List[logging.Handler]
    if log_file:
        try:
            directory = os.path.dirname(log_file)
            if directory:
                os.makedirs(directory, exist_ok=True)
            handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
        except OSError as exc:
            sys.stdout.write("日志文件不可写(%s)，仅输出到 stdout\n" % exc)
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(message)s")


def make_server(host: str, port: int, handler: Any = BlackboardHandler) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    return httpd


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="团队黑板·记忆网关（stdlib + psycopg2）")
    parser.add_argument("--host", default="127.0.0.1",
                        help="监听地址，默认 127.0.0.1；生产传 tailscale0 的 100.x 地址")
    parser.add_argument("--port", type=int, default=9940, help="监听端口，默认 9940")
    parser.add_argument("--agents-json", default=None, help="agents.json 路径（取 node_id）")
    parser.add_argument("--peers-json", default=None, help="peers.json 路径（token 的 sha256 表）")
    parser.add_argument("--admin-port", type=int, default=0,
                        help="本地健康口端口，只绑 127.0.0.1；0 表示不启用")
    parser.add_argument("--log-file", default=None, help="单行日志文件路径（可选）")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.log_file)

    try:
        load_agents(args.agents_json)
    except Exception as exc:
        LOG.error("加载 agents.json 失败: %s", _safe_err(exc))
        return 2
    try:
        load_peers(args.peers_json)
    except Exception as exc:
        LOG.error("加载 peers.json 失败: %s", _safe_err(exc))
        return 2

    if not Runtime.peer_map:
        LOG.warning("peers 表为空，所有业务请求都会 401")

    # 口令只走环境变量。连不上不退出：healthz 报 503，业务口报 503，运维能看见。
    dsn = os.environ.get("BB_PG_DSN") or ""
    if not dsn:
        LOG.error("环境变量 BB_PG_DSN 未设置，进入降级模式（业务口一律 503）")
    else:
        try:
            install_pg_backend(dsn)
            LOG.info("数据库后端已装载（DSN 来自环境变量，不打印内容）")
        except DBUnavailable as exc:
            LOG.error("数据库后端不可用，进入降级模式: %s", _safe_err(exc))

    admin_httpd = None  # type: Optional[ThreadingHTTPServer]
    if args.admin_port and args.admin_port > 0:
        try:
            admin_httpd = make_server("127.0.0.1", args.admin_port, AdminHandler)
        except OSError as exc:
            LOG.error("管理口监听失败 127.0.0.1:%d: %s", args.admin_port, _safe_err(exc))
            return 2
        threading.Thread(target=admin_httpd.serve_forever, daemon=True).start()
        LOG.info("admin healthz on 127.0.0.1:%d", admin_httpd.server_address[1])

    try:
        httpd = make_server(args.host, args.port)
    except (OSError, socket.error) as exc:
        LOG.error("监听失败 %s:%d: %s", args.host, args.port, _safe_err(exc))
        return 2

    # 兜底 sweep：进程内一个 daemon 线程，每 TMP_SWEEP_INTERVAL_S(300s) 一轮，
    # 一轮里跑两块 —— 临时共享区的到期销毁 + Agent 会话的空闲超时归档（契约 D1/§4.3 要求复用同一线程）。
    # 选服务内线程而不是新增 systemd timer：自包含、不新增部署单元、与服务同生命周期。
    threading.Thread(target=tmp_sweep_loop, kwargs={"interval_s": TMP_SWEEP_INTERVAL_S},
                     daemon=True).start()
    LOG.info("兜底 sweep 线程已启动（每 %ds 一轮：tmp 到期销毁 + session 空闲超时归档；"
             "/healthz 的 tmp_swept_at / sess_swept_at 各自自报）", TMP_SWEEP_INTERVAL_S)

    LOG.info("blackboard_svc %s node=%s listening on %s:%d db=%s",
             VERSION, Runtime.node_id, httpd.server_address[0], httpd.server_address[1],
             "on" if Runtime.db_configured else "degraded")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        LOG.info("收到中断，退出")
    finally:
        httpd.server_close()
        if admin_httpd is not None:
            admin_httpd.shutdown()
            admin_httpd.server_close()
        if isinstance(Runtime.db_query, PgBackend):
            Runtime.db_query.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
