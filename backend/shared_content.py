#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
shared_content.py —— 共享内容（任务级 / 永久级）的管理 CLI

共享内容 ≠ 会议：受众与管控都不一样，是两套独立功能。会议在 roundtable.py / rt_remind.py，
这个脚本一个字都不碰会议表。它管的是：

  * **任务级**（--scope task）：几个 Agent 做同一个任务时共享的背景与任务信息。
    有时间限制，有销毁条件 —— 任务一结束（或过了兜底 TTL）就销毁。
  * **永久级**（--scope permanent）：集群节点公网 IP、各机装了什么 Agent、主人（刚哥）
    画像与关键信息……一份共享，不让每个 Agent 各存一份。
  * 两种都能改 / 刷新（信息会变）；改动与作废由数据库触发器自动留痕
    （谁、何时、改了哪些字段），**不靠调用方自觉**，见 sql/shared_content_schema.sql。
  * 会过期，也能被"读完就撤下"（revoke）或彻底销毁（destroy：正文置空，行留着，痕迹还在）。
  * 支持检索：关键词（ILIKE + tags）与语义（复用 embedder 的余弦距离，不可用就降级）。

纪律（与 rt_remind.py / blackboard_svc.py 同一套）：
  * 纯标准库 + psycopg2（唯一第三方依赖，且只在真连库时才导入）；嵌入走同目录的
    embedder.py，不可用就留 NULL / 退回关键词，**绝不让写入或检索因为模型而失败**。
  * 凭据一律走**文件路径**参数（--dsn-file），源码、日志、打印里都不出现凭据值。
  * --dsn-file 指向的是 **systemd 环境文件**，取值等价于
    `sed -n 's/^BB_PG_DSN=//p' <file> | head -1`。**绝不能 bash source 它** ——
    DSN 里带空格（host=... port=... dbname=...），shell 会在第一个空格处截断成错值。
  * sweep 缺省是 **dry-run**：只打印不落库，`--commit` 才真销毁。
    "把别人的共享信息删了"这种事，不确认就不干。
  * 销毁 = status='destroyed' + 正文置空 + destroyed_at 落时间，**不是 DELETE**。
    库里的触发器也不让删行 —— 否则"留痕"和"销毁"互相矛盾。

用法：
    # 建一个任务容器（48 小时兜底 TTL），再往里发任务级共享内容
    python shared_content.py task-add --task-id T-0919-backup --title "备份策略落地" \\
        --owner hermes --ttl-hours 48 --dsn-file <deploy-dir>/pg.env
    python shared_content.py put --scope task --task-id T-0919-backup --kind note \\
        --title "任务背景" --body-file bg.md --owner hermes --expires-in-days 2

    # 永久级：节点公网 IP
    python shared_content.py put --scope permanent --kind node --title "龙虾公网IP" \\
        --body-file ip.txt --owner hermes --tags node,ip --review-in-days 30

    # 改一次 / 查历史 / 撤下 / 销毁
    python shared_content.py update --id 2 --body-file ip-new.txt --by hermes --note "换了宽带"
    python shared_content.py get --id 2
    python shared_content.py revoke --id 2 --by hermes --note "这条不再适合共享"
    python shared_content.py destroy --id 2 --by hermes --note "按刚哥要求清正文"

    # 检索
    python shared_content.py list --scope permanent --kind node
    python shared_content.py search --q "公网 IP" --semantic

    # 过期扫描：先演练，看清楚了再 --commit
    python shared_content.py sweep
    python shared_content.py sweep --commit

    # 离线自测 / 演示：走本地 sqlite 替身，**不连任何数据库、不联网**
    python shared_content.py --sqlite /tmp/demo.db task-add --task-id T1 --title t --owner me

退出码：0 成功；1 数据库 / 驱动出错；2 参数不对或前置条件不满足（没这条内容、任务不存在、
        任务级内容没给过期时间……）。
"""

import argparse
import datetime
import json
import os
import re
import sys
import unicodedata
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import embedder  # 同目录模块；导入只读环境变量，不会加载模型

VERSION = "1.0"

UTC = datetime.timezone.utc

DEFAULT_DSN_FILE = "<deploy-dir>/pg.env"
DEFAULT_LIMIT = 50
MAX_LIMIT = 500

SCOPES = ("task", "permanent")
SENSITIVITIES = ("public", "internal", "sensitive")
CONTENT_STATUSES = ("active", "expired", "revoked", "destroyed")
TASK_STATUSES = ("active", "done", "aborted")
TASK_END_STATUSES = ("done", "aborted")
# 默认不端给 Agent 的状态：撤下的和销毁的
HIDDEN_STATUSES = ("revoked", "destroyed")
# update 能改到的状态（destroyed 不在里面：销毁要连正文一起清，走 destroy 子命令）
UPDATABLE_STATUSES = ("active", "expired", "revoked")

# 取列顺序 = row_to_content 的下标顺序，**改一处必须改两处**
CONTENT_COLUMNS = ("content_id, scope, task_id, kind, title, body, tags, owner, audience, "
                   "sensitivity, status, expires_at, review_at, version, created_at, "
                   "updated_at, updated_by, destroyed_at, source_refs")

# 只读 API 的响应契约（D2 看板按它写）。blackboard_svc.SHARED_FIELDS 必须与它逐字一致，
# 两边各有一份是有意的（服务与 CLI 互不 import，部署时少拷一个文件也不会起不来），
# 由 tests/test_shared_content.py 断言两份完全相同。
CONTENT_FIELDS = ("content_id", "scope", "task_id", "kind", "title", "body", "tags",
                  "owner", "audience", "sensitivity", "status", "expires_at", "review_at",
                  "version", "created_at", "updated_at", "updated_by", "destroyed_at",
                  "source_refs", "expired")

TASK_COLUMNS = ("task_id, title, owner, status, note, created_at, ended_at, ttl_at, "
                "destroyed_at")

REVISION_COLUMNS = "rev_id, content_id, action, actor, changed, note, ts"

# 人话标签：英文枚举进来、中文出去（与看板、rt_remind 的口径一致）
SCOPE_CN = {"task": "任务级", "permanent": "永久级"}
STATUS_CN = {"active": "生效", "expired": "已过期", "revoked": "已撤下", "destroyed": "已销毁"}
TASK_STATUS_CN = {"active": "进行中", "done": "已完成", "aborted": "已中止"}
SENSITIVITY_CN = {"public": "公开", "internal": "内部", "sensitive": "敏感"}
ACTION_CN = {"create": "创建", "update": "修改", "revoke": "撤下", "expire": "过期",
             "destroy": "销毁", "restore": "恢复"}


class CliError(Exception):
    """参数或业务错误：main 捕获后打一行中文原因，不打栈。"""


# ================================================================ 小工具

def now_utc() -> datetime.datetime:
    return datetime.datetime.now(UTC)


def iso(value: Any) -> Optional[str]:
    """统一成带时区的 ISO8601 字符串（写库、比时间都用它）。"""
    moment = to_utc(value)
    return moment.isoformat() if moment else None


def to_utc(value: Any) -> Optional[datetime.datetime]:
    """timestamptz / ISO 字符串统一成带 UTC 时区的 datetime；认不出来返回 None。

    裸时间（不带时区）一律按 UTC 解释 —— 口径与 blackboard_svc._to_utc 一字不差，
    两边算出来的"过期没过期"必须是同一件事。
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


def fmt_time(value: Any) -> str:
    """人类那一栏统一显示北京时间（UTC+8），和看板、提醒对得上口。"""
    moment = to_utc(value)
    if moment is None:
        return "-"
    return (moment + datetime.timedelta(hours=8)).strftime("%m-%d %H:%M")


def is_expired(expires_at: Any, now: datetime.datetime) -> bool:
    """过期 = 有过期时间 且 **严格早于**现在。正好等于此刻还不算过期。

    边界口径与 blackboard_svc.is_overdue 一致：截止时间是"到点为止"。
    """
    moment = to_utc(expires_at)
    return moment is not None and moment < now


def one_line(text: Any, limit: int = 40) -> str:
    """压成一行并截断 —— 表格里一条正文不能把屏幕撑爆。"""
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


def _width(text: str) -> int:
    """中文按两格算的显示宽度，否则中英混排的表格永远是歪的。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _width(text))


def render_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    """中文对齐的纯文本表格（不依赖任何第三方库）。空表也给一句人话。"""
    if not rows:
        return "（没有匹配的记录）"
    cells = [[("" if c is None else str(c)) for c in row] for row in rows]
    widths = [_width(h) for h in headers]
    for row in cells:
        for i, cell in enumerate(row):
            if i < len(widths):
                widths[i] = max(widths[i], _width(cell))
    lines = ["  ".join(_pad(h, widths[i]) for i, h in enumerate(headers)).rstrip(),
             "  ".join("-" * w for w in widths)]
    for row in cells:
        lines.append("  ".join(_pad(c, widths[i]) for i, c in enumerate(row)).rstrip())
    return "\n".join(lines)


def read_dsn(path: str) -> str:
    """从 systemd 环境文件里取 BB_PG_DSN（等价于 sed -n 's/^BB_PG_DSN=//p'）。

    **绝不能 source 这个文件**：值里带空格，shell 会截断成错值。
    """
    with open(path, "r", encoding="utf-8-sig") as fh:
        for line in fh:
            if line.startswith("BB_PG_DSN="):
                value = line[len("BB_PG_DSN="):].rstrip("\r\n").strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                    value = value[1:-1]
                return value
    return ""


_SECRET_RE = re.compile(r"(password|passfile|sslpassword)\s*=\s*\S+", re.IGNORECASE)


def safe_err(exc: Any) -> str:
    """异常 -> 可以安全打印的一行字：抹掉 DSN 口令再截断（口径同 blackboard_svc）。"""
    text = str(exc).replace("\n", " ").strip() or exc.__class__.__name__
    return _SECRET_RE.sub(r"\1=***", text)[:400]


def split_list(raw: Optional[str]) -> Optional[List[str]]:
    """`a,b , c` -> ["a","b","c"]；没给（None）返回 None，给了空串返回 []。"""
    if raw is None:
        return None
    return [item.strip() for item in raw.split(",") if item.strip()]


def parse_source_refs(raw: Optional[Sequence[str]]) -> List[Dict[str, Any]]:
    """`entry:61` -> {"type":"entry","value":61}；没有冒号就整串当 value。

    形状与黑板 refs 一致（[{"type":..,"value":..}]），看板两边可以用同一段渲染。
    """
    out = []  # type: List[Dict[str, Any]]
    for item in raw or []:
        text = str(item).strip()
        if not text:
            continue
        kind, sep, value = text.partition(":")
        if not sep:
            out.append({"type": "ref", "value": text})
            continue
        value = value.strip()
        out.append({"type": kind.strip() or "ref",
                    "value": int(value) if value.isdigit() else value})
    return out


def as_json_list(value: Any) -> List[Any]:
    """jsonb 列：psycopg2 通常已解成 list；sqlite 替身给字符串，就补一次解析。"""
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def as_json_obj(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


# ================================================================ 行 -> 字典

def row_to_content(row: Sequence[Any], now: datetime.datetime) -> Dict[str, Any]:
    """CONTENT_COLUMNS 的取列顺序 -> 只读 API 的响应契约（字段名一个不许改）。"""
    return {
        "content_id": row[0],
        "scope": row[1],
        "task_id": row[2],
        "kind": row[3],
        "title": row[4],
        "body": row[5],
        "tags": as_json_list(row[6]),
        "owner": row[7],
        "audience": as_json_list(row[8]),
        "sensitivity": row[9],
        "status": row[10],
        "expires_at": iso(row[11]),
        "review_at": iso(row[12]),
        "version": row[13],
        "created_at": iso(row[14]),
        "updated_at": iso(row[15]),
        "updated_by": row[16],
        "destroyed_at": iso(row[17]),
        "source_refs": as_json_list(row[18]) if len(row) > 18 else [],
        # 过期与否**由服务端/CLI 算好**：各端时区不一致时，"过期了没有"不能有两种说法
        "expired": row[10] == "expired" or is_expired(row[11], now),
    }


def row_to_task(row: Sequence[Any], now: datetime.datetime) -> Dict[str, Any]:
    return {
        "task_id": row[0],
        "title": row[1],
        "owner": row[2],
        "status": row[3],
        "note": row[4],
        "created_at": iso(row[5]),
        "ended_at": iso(row[6]),
        "ttl_at": iso(row[7]),
        "destroyed_at": iso(row[8]),
        # 兜底 TTL 到点 = 就算没人来标结束，这个任务下的内容也该被销毁
        "ttl_expired": is_expired(row[7], now),
    }


def row_to_revision(row: Sequence[Any]) -> Dict[str, Any]:
    return {
        "rev_id": row[0],
        "content_id": row[1],
        "action": row[2],
        "actor": row[3],
        "changed": as_json_obj(row[4]),
        "note": row[5],
        "ts": iso(row[6]),
    }


# ================================================================ 存储层

class PgStore(object):
    """真库访问层。只在真连库时才导入 psycopg2。

    写的只有 shared_tasks / shared_content 两张表（留痕由数据库触发器自己写），
    一条 DELETE 都没有 —— 销毁走 status + 正文置空。
    """

    kind = "pg"

    def __init__(self, dsn: str) -> None:
        if not dsn:
            raise CliError("DSN 为空：检查 --dsn-file 指向的文件里有没有 BB_PG_DSN=")
        import psycopg2  # noqa: F401  —— 唯一的第三方依赖，延迟导入
        self._conn = psycopg2.connect(dsn, connect_timeout=5)
        self._conn.autocommit = True

    def exec(self, sql: str, params: Sequence[Any] = (), fetch: str = "none") -> Any:
        cur = self._conn.cursor()
        try:
            cur.execute(sql, tuple(params))
            if fetch == "one":
                return cur.fetchone()
            if fetch == "all":
                return cur.fetchall()
            return None
        finally:
            try:
                cur.close()
            except Exception:
                pass

    def set_note(self, note: Optional[str]) -> None:
        """把"为什么改"塞进会话变量，触发器写 revision 时会取它。

        用 set_config 而不是往业务表加列：这次改动的理由跟着这次改动走，
        下一条写入之前一定会被重设（见 Runner._write），不会串味。
        """
        self.exec("SELECT set_config('shared_content.note', %s, false)", (note or "",), "one")

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass


# sqlite 替身的建表脚本由下面的函数生成（触发器要按字段展开，手写太长也容易漏字段）
SQLITE_DIFF_FIELDS = ("scope", "task_id", "kind", "title", "body", "tags", "owner",
                      "audience", "sensitivity", "status", "expires_at", "review_at",
                      "updated_by", "destroyed_at", "source_refs")

_PLACEHOLDER_RE = re.compile(r"%s")
_CAST_RE = re.compile(r"::(jsonb|timestamptz|vector|text|int)\b")
_ILIKE_RE = re.compile(r"\bILIKE\b", re.IGNORECASE)
# `tags @> %s::jsonb`（jsonb 包含）在 sqlite 里没有对应物，翻成 json_each 的存在性判断。
# 只覆盖"数组里有没有这一个元素"这一种用法 —— 本文件的 @> 也只这么用。
_CONTAINS_RE = re.compile(r"(\w+) @> %s::jsonb")


def _sqlite_diff_expr() -> str:
    """生成"字段级 diff"表达式：与 PG 触发器同一套口径，只是按字段展开写死。

    * 正文 body 一律只记 {"changed": true}（长，且销毁后不能留残）；
    * 整行 sensitivity 沾上 sensitive 的，所有字段都只记"已变更"，一个值不落；
    * embedding / updated_at / version 不进 diff（派生物 + 每次必变的噪音）。
    """
    acc = "json('{}')"
    for field in SQLITE_DIFF_FIELDS:
        hidden = "json_object('%s', json_object('changed', json('true')))" % field
        shown = ("json_object('%s', json_object('from', OLD.%s, 'to', NEW.%s))"
                 % (field, field, field))
        value = hidden if field == "body" else (
            "CASE WHEN NEW.sensitivity = 'sensitive' OR OLD.sensitivity = 'sensitive' "
            "THEN %s ELSE %s END" % (hidden, shown))
        acc = ("json_patch(%s, CASE WHEN NEW.%s IS NOT OLD.%s THEN %s ELSE json('{}') END)"
               % (acc, field, field, value))
    return acc


def sqlite_schema() -> str:
    """sqlite 替身的建表脚本 —— **镜像** sql/shared_content_schema.sql 的口径。

    只给离线自测与演示用（--sqlite），生产一律 PG。两边必须同时约束的东西：
    CHECK（scope/task_id/expires_at 的形状、枚举、销毁后正文为空）、冻结列、
    自动留痕。两份不许走偏，tests/test_shared_content.py 会逐条比对。
    """
    now_default = "strftime('%Y-%m-%dT%H:%M:%S+00:00','now')"
    return """
CREATE TABLE IF NOT EXISTS shared_tasks (
  task_id      text PRIMARY KEY,
  title        text NOT NULL,
  owner        text NOT NULL,
  status       text NOT NULL DEFAULT 'active'
               CHECK (status IN ('active','done','aborted')),
  note         text,
  created_at   text NOT NULL DEFAULT (%(now)s),
  ended_at     text,
  ttl_at       text,
  destroyed_at text
);

CREATE TABLE IF NOT EXISTS shared_content (
  content_id   INTEGER PRIMARY KEY AUTOINCREMENT,
  scope        text NOT NULL CHECK (scope IN ('task','permanent')),
  task_id      text REFERENCES shared_tasks (task_id),
  kind         text NOT NULL DEFAULT 'note',
  title        text NOT NULL,
  body         text NOT NULL,
  tags         text NOT NULL DEFAULT '[]',
  owner        text NOT NULL,
  audience     text NOT NULL DEFAULT '["all"]',
  sensitivity  text NOT NULL DEFAULT 'internal'
               CHECK (sensitivity IN ('public','internal','sensitive')),
  status       text NOT NULL DEFAULT 'active'
               CHECK (status IN ('active','expired','revoked','destroyed')),
  expires_at   text,
  review_at    text,
  version      int  NOT NULL DEFAULT 1,
  created_at   text NOT NULL DEFAULT (%(now)s),
  updated_at   text NOT NULL DEFAULT (%(now)s),
  updated_by   text,
  destroyed_at text,
  source_refs  text NOT NULL DEFAULT '[]',
  embedding    text,
  CHECK ((scope = 'task' AND task_id IS NOT NULL AND expires_at IS NOT NULL)
      OR (scope = 'permanent' AND task_id IS NULL)),
  CHECK (status <> 'destroyed' OR (body = '' AND destroyed_at IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS shared_content_revisions (
  rev_id     INTEGER PRIMARY KEY AUTOINCREMENT,
  content_id int  NOT NULL,
  action     text NOT NULL
             CHECK (action IN ('create','update','revoke','expire','destroy','restore')),
  actor      text NOT NULL,
  changed    text NOT NULL DEFAULT '{}',
  note       text,
  ts         text NOT NULL DEFAULT (%(now)s)
);

-- 会话变量的替身：PG 用 set_config('shared_content.note', ...)，这里用一张单行表
CREATE TABLE IF NOT EXISTS shared_ctx (id int PRIMARY KEY, note text);
INSERT OR IGNORE INTO shared_ctx (id, note) VALUES (1, NULL);

-- 冻结列：改 scope/task_id/created_at/content_id 直接报错（对应 PG 的 BEFORE UPDATE guard）
CREATE TRIGGER IF NOT EXISTS shared_content_immutable_cols
BEFORE UPDATE ON shared_content
WHEN NEW.content_id IS NOT OLD.content_id OR NEW.scope IS NOT OLD.scope
  OR NEW.task_id IS NOT OLD.task_id OR NEW.created_at IS NOT OLD.created_at
BEGIN
  SELECT RAISE(ABORT, 'shared_content 的 content_id/scope/task_id/created_at 不可变');
END;

-- 只增不删（对应 PG 的 DELETE/TRUNCATE 拒）
CREATE TRIGGER IF NOT EXISTS shared_content_no_delete
BEFORE DELETE ON shared_content
BEGIN
  SELECT RAISE(ABORT, 'shared_content 不允许 DELETE（销毁请走 status=destroyed）');
END;

CREATE TRIGGER IF NOT EXISTS shared_revisions_no_update
BEFORE UPDATE ON shared_content_revisions
BEGIN
  SELECT RAISE(ABORT, 'shared_content_revisions is append-only');
END;

CREATE TRIGGER IF NOT EXISTS shared_revisions_no_delete
BEFORE DELETE ON shared_content_revisions
BEGIN
  SELECT RAISE(ABORT, 'shared_content_revisions is append-only');
END;

CREATE TRIGGER IF NOT EXISTS shared_tasks_immutable_cols
BEFORE UPDATE ON shared_tasks
WHEN NEW.task_id IS NOT OLD.task_id OR NEW.title IS NOT OLD.title
  OR NEW.owner IS NOT OLD.owner OR NEW.created_at IS NOT OLD.created_at
BEGIN
  SELECT RAISE(ABORT, 'shared_tasks 的 task_id/title/owner/created_at 不可变');
END;

-- 自动留痕：新建
CREATE TRIGGER IF NOT EXISTS shared_content_rev_insert
AFTER INSERT ON shared_content
BEGIN
  INSERT INTO shared_content_revisions (content_id, action, actor, changed, note)
  VALUES (NEW.content_id, 'create',
          COALESCE(NULLIF(NEW.updated_by,''), NEW.owner),
          json_object('scope', NEW.scope, 'kind', NEW.kind,
                      'title', CASE WHEN NEW.sensitivity = 'sensitive'
                                    THEN '（敏感，不记值）' ELSE NEW.title END,
                      'status', NEW.status),
          (SELECT note FROM shared_ctx WHERE id = 1));
END;

-- 自动留痕：修改（字段级 diff；什么都没改就不写，免得历史里全是空条目）
CREATE TRIGGER IF NOT EXISTS shared_content_rev_update
AFTER UPDATE ON shared_content
BEGIN
  INSERT INTO shared_content_revisions (content_id, action, actor, changed, note)
  SELECT NEW.content_id,
         CASE
           WHEN NEW.status = 'destroyed' AND OLD.status IS NOT 'destroyed' THEN 'destroy'
           WHEN NEW.status = 'revoked'   AND OLD.status IS NOT 'revoked'   THEN 'revoke'
           WHEN NEW.status = 'expired'   AND OLD.status IS NOT 'expired'   THEN 'expire'
           WHEN NEW.status = 'active'    AND OLD.status IS NOT 'active'    THEN 'restore'
           ELSE 'update'
         END,
         COALESCE(NULLIF(NEW.updated_by,''), 'unknown'),
         %(diff)s,
         (SELECT note FROM shared_ctx WHERE id = 1)
  WHERE %(diff)s <> '{}';
END;
""" % {"now": now_default, "diff": _sqlite_diff_expr()}


class SqliteStore(object):
    """**离线自测 / 演示专用**的 sqlite 替身（--sqlite）。不是生产路径。

    为什么留这么一个后门：没有它，"建任务 → 发内容 → 改一次 → 查历史 → sweep 销毁"
    这条链路就只能对着真库演示，而真库在龙虾上 —— 演示一次就污染一次生产数据。
    单测同理：跑的是**真的 CLI 命令**，不是把逻辑抄一遍的假货。

    与 PG 的差别只在方言，不在口径：占位符 %s -> ?，PG 的类型转换（::jsonb 等）在
    sqlite 里没有对应物，直接抹掉；CHECK / 冻结列 / 自动留痕全部照抄一份（见 sqlite_schema）。
    """

    kind = "sqlite"

    def __init__(self, path: str) -> None:
        import sqlite3
        self._conn = sqlite3.connect(path)
        self._conn.isolation_level = None      # 自动提交，与 PG 侧的 autocommit 对齐
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(sqlite_schema())

    @staticmethod
    def translate(sql: str) -> str:
        """PG 方言 -> sqlite 方言。只改语法，不改口径。

        * `tags @> %s::jsonb` -> json_each 的存在性判断（参数仍是 '["标签"]'，取它的第 0 项）；
        * ILIKE -> LIKE（sqlite 的 LIKE 对 ASCII 本来就不区分大小写）；
        * ::jsonb / ::timestamptz / ::vector 等类型转换直接抹掉（sqlite 没有类型标注）；
        * %s -> ?。
        """
        sql = _CONTAINS_RE.sub(
            r"EXISTS (SELECT 1 FROM json_each(\1) WHERE value = json_extract(%s, '$[0]'))", sql)
        sql = _ILIKE_RE.sub("LIKE", sql)
        return _PLACEHOLDER_RE.sub("?", _CAST_RE.sub("", sql))

    def exec(self, sql: str, params: Sequence[Any] = (), fetch: str = "none") -> Any:
        cur = self._conn.execute(self.translate(sql), tuple(params))
        try:
            if fetch == "one":
                return cur.fetchone()
            if fetch == "all":
                return cur.fetchall()
            return None
        finally:
            cur.close()

    def set_note(self, note: Optional[str]) -> None:
        self._conn.execute("UPDATE shared_ctx SET note = ? WHERE id = 1", (note or None,))

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass


def open_store(args: argparse.Namespace) -> Any:
    """按参数挑后端：--sqlite 走替身，否则从 --dsn-file 读 DSN 连 PG。"""
    if getattr(args, "sqlite", None):
        return SqliteStore(args.sqlite)
    path = getattr(args, "dsn_file", None) or DEFAULT_DSN_FILE
    try:
        dsn = read_dsn(path)
    except OSError as exc:
        raise CliError("读不到 DSN 文件 %s：%s" % (path, safe_err(exc)))
    return PgStore(dsn)


# ================================================================ 嵌入（可选增强）

def compute_embedding(title: str, body: str) -> Optional[List[float]]:
    """算一条共享内容的向量。**算不出来就返回 None**，调用方把 embedding 留 NULL。

    编码的是"标题 + 正文"：检索侧用的是裸查询串（非对称的 doc/query 用法，bge 系列
    本来就是这么用的）。维度对不上库列时也返回 None —— 与其让整条写入被 PG 拒，
    不如少一条向量，事后补。
    """
    text = (title or "").strip()
    body = (body or "").strip()
    if body:
        text = (text + "\n" + body) if text else body
    if not text:
        return None
    try:
        vectors = embedder.embed_texts([text])
    except Exception:      # embedder 承诺不抛；这里是最后一道保险，宁可降级
        return None
    if not vectors:
        return None
    vector = vectors[0]
    return vector if len(vector) == embedder.embed_dim() else None


def vector_literal(embedding: Optional[Sequence[float]]) -> Optional[str]:
    """pgvector 的文本字面量形式，避免为了一列向量引入 pgvector 的 python 适配器。"""
    if embedding is None:
        return None
    return "[" + ",".join(repr(float(x)) for x in embedding) + "]"


_LIKE_SPECIAL_RE = re.compile(r"([\\%_])")


def like_pattern(q: str) -> str:
    """把用户输入包成 ILIKE 模式串：先转义 \\ % _，免得输个 % 就全表命中。"""
    return "%" + _LIKE_SPECIAL_RE.sub(r"\\\1", q) + "%"


# ================================================================ 查询构造

def content_filters(scope: Optional[str] = None, kind: Optional[str] = None,
                    owner: Optional[str] = None, tag: Optional[str] = None,
                    task_id: Optional[str] = None, status: Optional[str] = None,
                    expired: bool = False,
                    now: Optional[datetime.datetime] = None) -> Tuple[List[str], List[Any]]:
    """把列表 / 检索共用的过滤条件拼成 (where[], params[])。全部参数化，不拼值。

    默认口径：**不返回撤下与销毁的**（那正是"撤下"的意思），也不返回已过期的；
    expired=True 才把过期的一并带出来（给 owner 自己清理用）。
    status 显式指定时一切听它的 —— 想看 revoked 的历史就明写 --status revoked。
    """
    where = []   # type: List[str]
    params = []  # type: List[Any]
    now = now or now_utc()
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
        params.extend(HIDDEN_STATUSES)
        if not expired:
            where.append("status <> %s")
            params.append("expired")
            where.append("(expires_at IS NULL OR expires_at >= %s::timestamptz)")
            params.append(iso(now))
    return where, params


def select_contents(store: Any, where: Sequence[str], params: Sequence[Any],
                    limit: int, now: datetime.datetime) -> List[Dict[str, Any]]:
    sql = "SELECT " + CONTENT_COLUMNS + " FROM shared_content"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY updated_at DESC, content_id DESC LIMIT %s"
    rows = store.exec(sql, list(params) + [limit], "all") or []
    return [row_to_content(r, now) for r in rows]


def get_content(store: Any, content_id: int,
                now: datetime.datetime) -> Optional[Dict[str, Any]]:
    row = store.exec("SELECT " + CONTENT_COLUMNS + " FROM shared_content WHERE content_id = %s",
                     (content_id,), "one")
    return row_to_content(row, now) if row else None


def get_revisions(store: Any, content_id: int, limit: int = 100) -> List[Dict[str, Any]]:
    rows = store.exec("SELECT " + REVISION_COLUMNS + " FROM shared_content_revisions "
                      "WHERE content_id = %s ORDER BY ts ASC, rev_id ASC LIMIT %s",
                      (content_id, limit), "all") or []
    return [row_to_revision(r) for r in rows]


def get_task(store: Any, task_id: str, now: datetime.datetime) -> Optional[Dict[str, Any]]:
    row = store.exec("SELECT " + TASK_COLUMNS + " FROM shared_tasks WHERE task_id = %s",
                     (task_id,), "one")
    return row_to_task(row, now) if row else None


# ================================================================ 子命令实现
#
# 每个 run_* 返回一个 dict（--json 直接打它），外加一个 "_text" 键给人类那一份。
# 这样两种输出只有一个真相来源，不会出现"JSON 说成了、人话说没成"。


def _require(value: Any, field: str) -> Any:
    if value in (None, ""):
        raise CliError("%s 必填" % field)
    return value


def _body_text(args: argparse.Namespace, required: bool) -> Optional[str]:
    """正文来源：--body-file 优先，其次 --body。两个都没给时按 required 决定报不报错。"""
    path = getattr(args, "body_file", None)
    if path:
        try:
            with open(path, "r", encoding="utf-8-sig") as fh:
                return fh.read()
        except OSError as exc:
            raise CliError("读不到正文文件 %s：%s" % (path, safe_err(exc)))
    body = getattr(args, "body", None)
    if body is not None:
        return body
    if required:
        raise CliError("正文必填：用 --body-file 指一个文件，或 --body 直接给一段")
    return None


def _when(now: datetime.datetime, days: Optional[float], hours: Optional[float],
          explicit: Optional[str], field: str) -> Optional[str]:
    """把 --xx-in-days / --xx-in-hours / --xx-at 三种写法归一成 ISO 串。"""
    if explicit:
        moment = to_utc(explicit)
        if moment is None:
            raise CliError("%s 不是合法时间（要 ISO8601，例如 2026-10-01T12:00:00+08:00）" % field)
        return moment.isoformat()
    if days is not None:
        return (now + datetime.timedelta(days=days)).isoformat()
    if hours is not None:
        return (now + datetime.timedelta(hours=hours)).isoformat()
    return None


def run_task_add(store: Any, args: argparse.Namespace,
                 now: datetime.datetime) -> Dict[str, Any]:
    task_id = _require(args.task_id, "--task-id")
    ttl_at = _when(now, None, args.ttl_hours, args.ttl_at, "--ttl-at")
    store.set_note(args.note)
    store.exec("INSERT INTO shared_tasks (task_id, title, owner, note, created_at, ttl_at) "
               "VALUES (%s, %s, %s, %s, %s::timestamptz, %s::timestamptz)",
               (task_id, _require(args.title, "--title"), _require(args.owner, "--owner"),
                args.note, now.isoformat(), ttl_at))
    task = get_task(store, task_id, now)
    lines = ["已建任务容器 %s（%s）" % (task_id, task["title"]),
             "  负责人 %s；兜底 TTL %s" % (task["owner"],
                                          fmt_time(task["ttl_at"]) if task["ttl_at"]
                                          else "未设（只能靠 task-end 收尾）")]
    return {"ok": True, "task": task, "_text": "\n".join(lines)}


def run_task_end(store: Any, args: argparse.Namespace,
                 now: datetime.datetime) -> Dict[str, Any]:
    task_id = _require(args.task_id, "--task-id")
    if args.status not in TASK_END_STATUSES:
        raise CliError("--status 只能是 %s 之一" % "/".join(TASK_END_STATUSES))
    store.set_note(args.note)
    row = store.exec("UPDATE shared_tasks SET status = %s, ended_at = %s::timestamptz, "
                     "note = COALESCE(%s, note) WHERE task_id = %s AND status = %s "
                     "RETURNING task_id",
                     (args.status, now.isoformat(), args.note, task_id, "active"), "one")
    if not row:
        raise CliError("任务 %s 不存在，或它已经结束了（结束过的任务不重复收尾）" % task_id)
    task = get_task(store, task_id, now)
    pending = store.exec("SELECT count(*) FROM shared_content WHERE task_id = %s "
                         "AND status <> %s", (task_id, "destroyed"), "one")
    left = int(pending[0] if pending else 0)
    lines = ["任务 %s 已标记为 %s" % (task_id, TASK_STATUS_CN.get(args.status, args.status)),
             "  这个任务下还有 %d 条任务级共享内容没销毁。" % left,
             "  **任务结束不等于内容已销毁** —— 跑 `sweep --commit` 才真清："
             "先 `sweep` 演练看清楚。"]
    return {"ok": True, "task": task, "pending": left, "_text": "\n".join(lines)}


def run_put(store: Any, args: argparse.Namespace, now: datetime.datetime) -> Dict[str, Any]:
    scope = args.scope
    if scope not in SCOPES:
        raise CliError("--scope 只能是 %s 之一" % "/".join(SCOPES))
    if args.sensitivity not in SENSITIVITIES:
        raise CliError("--sensitivity 只能是 %s 之一" % "/".join(SENSITIVITIES))
    title = _require(args.title, "--title")
    owner = _require(args.owner, "--owner")
    body = _body_text(args, required=True)
    expires_at = _when(now, args.expires_in_days, args.expires_in_hours,
                       args.expires_at, "--expires-at")
    review_at = _when(now, args.review_in_days, None, args.review_at, "--review-at")

    task_id = args.task_id or None
    if scope == "task":
        # 任务级的两条硬口径：必须挂任务、必须有过期时间。库里的 CHECK 是最后一道，
        # 这里先挡一次是为了给人话报错（"临时共享必须说清什么时候没"）。
        if not task_id:
            raise CliError("任务级共享内容必须 --task-id：不挂任务就没有销毁条件")
        task = get_task(store, task_id, now)
        if task is None:
            raise CliError("任务 %s 不存在，先 task-add" % task_id)
        if task["status"] != "active":
            raise CliError("任务 %s 已经是 %s，不该再往里发共享内容"
                           % (task_id, TASK_STATUS_CN.get(task["status"], task["status"])))
        if not expires_at:
            expires_at = task["ttl_at"]     # 没显式给就继承任务的兜底 TTL
        if not expires_at:
            raise CliError("任务级共享内容必须有过期时间：给 --expires-in-days，"
                           "或者给任务设 --ttl-hours 让它继承")
    elif task_id:
        raise CliError("永久级共享内容不许挂 --task-id：挂了就会被任务结束连坐销毁")

    tags = split_list(args.tags) or []
    audience = split_list(args.audience) or ["all"]
    refs = parse_source_refs(args.source_ref)
    actor = args.by or owner
    embedding = None if args.no_embed else compute_embedding(title, body)

    store.set_note(args.note)
    row = store.exec(
        "INSERT INTO shared_content (scope, task_id, kind, title, body, tags, owner, "
        "audience, sensitivity, expires_at, review_at, created_at, updated_at, updated_by, "
        "source_refs, embedding) VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s::jsonb, %s, "
        "%s::timestamptz, %s::timestamptz, %s::timestamptz, %s::timestamptz, %s, %s::jsonb, "
        "%s::vector) RETURNING content_id",
        (scope, task_id, args.kind, title, body,
         json.dumps(tags, ensure_ascii=False), owner,
         json.dumps(audience, ensure_ascii=False), args.sensitivity,
         expires_at, review_at, now.isoformat(), now.isoformat(), actor,
         json.dumps(refs, ensure_ascii=False), vector_literal(embedding)), "one")
    if not row:
        raise CliError("写入没有返回 content_id")
    item = get_content(store, int(row[0]), now)
    lines = ["已发布 #%d [%s/%s] %s" % (item["content_id"], SCOPE_CN.get(scope, scope),
                                        item["kind"], item["title"]),
             "  维护人 %s；受众 %s；敏感度 %s；向量 %s"
             % (item["owner"], "/".join(item["audience"]),
                SENSITIVITY_CN.get(item["sensitivity"], item["sensitivity"]),
                "已生成" if embedding else "未生成（检索退回关键词，可事后补）"),
             "  过期 %s；复审 %s"
             % (fmt_time(item["expires_at"]) if item["expires_at"] else "不过期",
                fmt_time(item["review_at"]) if item["review_at"] else "不复审")]
    return {"ok": True, "content": item, "_text": "\n".join(lines)}


def run_update(store: Any, args: argparse.Namespace,
               now: datetime.datetime) -> Dict[str, Any]:
    content_id = int(_require(args.id, "--id"))
    actor = _require(args.by, "--by")
    before = get_content(store, content_id, now)
    if before is None:
        raise CliError("没有 #%d 这条共享内容" % content_id)
    if before["status"] == "destroyed":
        raise CliError("#%d 已经销毁了，正文已清空，不能再改（要重发请新开一条）" % content_id)

    sets = []    # type: List[str]
    params = []  # type: List[Any]
    changed = []  # type: List[str]

    def put(column: str, value: Any, cast: str = "") -> None:
        sets.append("%s = %%s%s" % (column, cast))
        params.append(value)
        changed.append(column)

    body = _body_text(args, required=False)
    title = args.title
    if title is not None:
        put("title", title)
    if body is not None:
        put("body", body)
    if args.tags is not None:
        put("tags", json.dumps(split_list(args.tags) or [], ensure_ascii=False), "::jsonb")
    if args.audience is not None:
        put("audience", json.dumps(split_list(args.audience) or ["all"],
                                   ensure_ascii=False), "::jsonb")
    if args.owner is not None:
        put("owner", args.owner)
    if args.kind is not None:
        put("kind", args.kind)
    if args.sensitivity is not None:
        if args.sensitivity not in SENSITIVITIES:
            raise CliError("--sensitivity 只能是 %s 之一" % "/".join(SENSITIVITIES))
        put("sensitivity", args.sensitivity)
    if args.status is not None:
        if args.status not in UPDATABLE_STATUSES:
            raise CliError("--status 只能是 %s 之一（销毁请用 destroy 子命令，它会一并清正文）"
                           % "/".join(UPDATABLE_STATUSES))
        put("status", args.status)
    if args.source_ref:
        put("source_refs", json.dumps(parse_source_refs(args.source_ref),
                                      ensure_ascii=False), "::jsonb")
    expires_at = _when(now, args.expires_in_days, None, args.expires_at, "--expires-at")
    if expires_at:
        put("expires_at", expires_at, "::timestamptz")
    review_at = _when(now, args.review_in_days, None, args.review_at, "--review-at")
    if review_at:
        put("review_at", review_at, "::timestamptz")

    if not sets:
        raise CliError("没给任何要改的字段：--title/--body-file/--tags/--status/... 至少给一个")

    # 正文或标题动了就重算向量：不重算的话，检索还按老正文找人，等于悄悄骗人
    if (title is not None or body is not None) and not args.no_embed:
        vector = compute_embedding(title if title is not None else before["title"],
                                   body if body is not None else before["body"])
        sets.append("embedding = %s::vector")
        params.append(vector_literal(vector))

    sets.append("version = version + 1")
    sets.append("updated_at = %s::timestamptz")
    params.append(now.isoformat())
    sets.append("updated_by = %s")
    params.append(actor)

    store.set_note(args.note)
    row = store.exec("UPDATE shared_content SET " + ", ".join(sets) +
                     " WHERE content_id = %s AND status <> %s RETURNING content_id",
                     params + [content_id, "destroyed"], "one")
    if not row:
        raise CliError("改不动 #%d（可能刚刚被销毁了）" % content_id)
    after = get_content(store, content_id, now)
    revisions = get_revisions(store, content_id)
    lines = ["已更新 #%d：%s（v%s -> v%s）"
             % (content_id, after["title"], before["version"], after["version"]),
             "  改了：%s；操作人 %s；理由 %s"
             % ("、".join(changed), actor, args.note or "（没写）"),
             "  留痕共 %d 条（get --id %d 看全）" % (len(revisions), content_id)]
    return {"ok": True, "content": after, "changed": changed,
            "revisions": revisions, "_text": "\n".join(lines)}


def _status_change(store: Any, content_id: int, status: str, actor: str,
                   note: Optional[str], now: datetime.datetime,
                   clear_body: bool) -> Dict[str, Any]:
    """revoke / destroy 共用的一次状态推进。**销毁连正文一起清，行留着。**"""
    before = get_content(store, content_id, now)
    if before is None:
        raise CliError("没有 #%d 这条共享内容" % content_id)
    if before["status"] == status:
        raise CliError("#%d 已经是 %s 了，不重复操作" % (content_id, STATUS_CN.get(status, status)))
    if before["status"] == "destroyed":
        raise CliError("#%d 已经销毁，没什么可再撤的" % content_id)

    sets = ["status = %s", "updated_at = %s::timestamptz", "updated_by = %s",
            "version = version + 1"]
    params = [status, now.isoformat(), actor]  # type: List[Any]
    if clear_body:
        # 正文置空 + 销毁时间：销毁是"内容真的没了"，不是"标记一下还留着给人看"
        sets.insert(1, "body = %s")
        params.insert(1, "")
        sets.append("destroyed_at = %s::timestamptz")
        params.append(now.isoformat())
        sets.append("embedding = NULL")   # 向量是正文的派生物，正文没了它也不该留
    store.set_note(note)
    row = store.exec("UPDATE shared_content SET " + ", ".join(sets) +
                     " WHERE content_id = %s AND status <> %s RETURNING content_id",
                     params + [content_id, "destroyed"], "one")
    if not row:
        raise CliError("动不了 #%d（可能刚刚被别人销毁了）" % content_id)
    return get_content(store, content_id, now)


def run_revoke(store: Any, args: argparse.Namespace,
               now: datetime.datetime) -> Dict[str, Any]:
    content_id = int(_require(args.id, "--id"))
    actor = _require(args.by, "--by")
    item = _status_change(store, content_id, "revoked", actor, args.note, now, False)
    lines = ["已撤下 #%d：%s" % (content_id, item["title"]),
             "  默认列表与只读 API 不再返回它（正文还在，`get --id %d` 查得到）；"
             "操作人 %s；理由 %s" % (content_id, actor, args.note or "（没写）")]
    return {"ok": True, "content": item, "_text": "\n".join(lines)}


def run_destroy(store: Any, args: argparse.Namespace,
                now: datetime.datetime) -> Dict[str, Any]:
    content_id = int(_require(args.id, "--id"))
    actor = _require(args.by, "--by")
    item = _status_change(store, content_id, "destroyed", actor, args.note, now, True)
    lines = ["已销毁 #%d：%s" % (content_id, item["title"]),
             "  正文已清空、向量已清、destroyed_at=%s；行与留痕保留（谁销毁的查得到）。"
             % fmt_time(item["destroyed_at"]),
             "  操作人 %s；理由 %s" % (actor, args.note or "（没写）")]
    return {"ok": True, "content": item, "_text": "\n".join(lines)}


def run_get(store: Any, args: argparse.Namespace, now: datetime.datetime) -> Dict[str, Any]:
    content_id = int(_require(args.id, "--id"))
    item = get_content(store, content_id, now)
    if item is None:
        raise CliError("没有 #%d 这条共享内容" % content_id)
    revisions = get_revisions(store, content_id)
    head = ["#%d [%s/%s] %s" % (item["content_id"], SCOPE_CN.get(item["scope"], item["scope"]),
                                item["kind"], item["title"]),
            "  状态 %s%s；v%s；维护人 %s；受众 %s；敏感度 %s"
            % (STATUS_CN.get(item["status"], item["status"]),
               "（已过期）" if item["expired"] and item["status"] == "active" else "",
               item["version"], item["owner"], "/".join(item["audience"]),
               SENSITIVITY_CN.get(item["sensitivity"], item["sensitivity"])),
            "  任务 %s；标签 %s；来源 %s"
            % (item["task_id"] or "-", "、".join(item["tags"]) or "-",
               json.dumps(item["source_refs"], ensure_ascii=False)),
            "  过期 %s；复审 %s；更新 %s by %s"
            % (fmt_time(item["expires_at"]) if item["expires_at"] else "不过期",
               fmt_time(item["review_at"]) if item["review_at"] else "不复审",
               fmt_time(item["updated_at"]), item["updated_by"] or "-"),
            "",
            "正文：",
            item["body"] or "（已销毁，正文不留残）",
            "",
            "改动历史（谁、何时、改了哪些字段）："]
    rows = [[r["rev_id"], ACTION_CN.get(r["action"], r["action"]), r["actor"],
             fmt_time(r["ts"]), "、".join(sorted(r["changed"].keys())) or "-",
             one_line(r["note"], 24)] for r in revisions]
    text = "\n".join(head) + "\n" + render_table(
        ["#", "动作", "操作人", "时间", "字段", "理由"], rows)
    return {"ok": True, "content": item, "revisions": revisions, "_text": text}


def _content_table(items: Sequence[Dict[str, Any]]) -> str:
    rows = [[it["content_id"], SCOPE_CN.get(it["scope"], it["scope"]), it["kind"],
             one_line(it["title"], 28), it["owner"],
             STATUS_CN.get(it["status"], it["status"]) + ("·过期" if it["expired"] and
                                                          it["status"] == "active" else ""),
             fmt_time(it["expires_at"]) if it["expires_at"] else "不过期",
             "、".join(it["tags"]) or "-"]
            for it in items]
    return render_table(["ID", "范围", "类型", "标题", "维护人", "状态", "过期", "标签"], rows)


def run_list(store: Any, args: argparse.Namespace, now: datetime.datetime) -> Dict[str, Any]:
    if args.status and args.status not in CONTENT_STATUSES:
        raise CliError("--status 只能是 %s 之一" % "/".join(CONTENT_STATUSES))
    if args.scope and args.scope not in SCOPES:
        raise CliError("--scope 只能是 %s 之一" % "/".join(SCOPES))
    where, params = content_filters(args.scope, args.kind, args.owner, args.tag,
                                    args.task_id, args.status, args.expired, now)
    items = select_contents(store, where, params, min(args.limit, MAX_LIMIT), now)
    text = "共享内容 %d 条（截至 %s）\n%s" % (len(items), fmt_time(now), _content_table(items))
    return {"ok": True, "count": len(items), "contents": items, "_text": text}


def run_search(store: Any, args: argparse.Namespace,
               now: datetime.datetime) -> Dict[str, Any]:
    q = _require(args.q, "--q")
    limit = min(args.limit, MAX_LIMIT)
    where, params = content_filters(args.scope, args.kind, args.owner, args.tag,
                                    args.task_id, args.status, args.expired, now)
    mode = "keyword"
    vector = compute_embedding(q, "") if args.semantic else None
    if vector is not None:
        mode = "semantic"
        literal = vector_literal(vector)
        sql = ("SELECT " + CONTENT_COLUMNS + ", 1 - (embedding <=> %s::vector) AS score "
               "FROM shared_content WHERE " +
               " AND ".join(list(where) + ["embedding IS NOT NULL"]) +
               " ORDER BY embedding <=> %s::vector LIMIT %s")
        rows = store.exec(sql, [literal] + list(params) + [literal, limit], "all") or []
        items = []  # type: List[Dict[str, Any]]
        for row in rows:
            item = row_to_content(row, now)
            item["score"] = round(float(row[19]), 4) if len(row) > 19 else None
            items.append(item)
    else:
        # 降级路径（没开 --semantic，或者模型不可用）：标题 / 正文 / 标签的子串匹配。
        # score 一律 null —— 没有可比的相似度，不编一个出来。
        pattern = like_pattern(q)
        sql = ("SELECT " + CONTENT_COLUMNS + " FROM shared_content WHERE " +
               " AND ".join(list(where) +
                            ["(title ILIKE %s OR body ILIKE %s OR tags @> %s::jsonb)"]) +
               " ORDER BY updated_at DESC, content_id DESC LIMIT %s")
        rows = store.exec(sql, list(params) + [pattern, pattern,
                                               json.dumps([q], ensure_ascii=False), limit],
                          "all") or []
        items = [row_to_content(r, now) for r in rows]
        for item in items:
            item["score"] = None
    note = "语义检索" if mode == "semantic" else (
        "关键词检索（要语义请加 --semantic；加了还是这行，就是嵌入模型不可用，已降级）")
    text = "%s：q=%r 命中 %d 条\n%s" % (note, q, len(items), _content_table(items))
    return {"ok": True, "mode": mode, "query": q, "count": len(items),
            "contents": items, "_text": text}


def run_tasks(store: Any, args: argparse.Namespace, now: datetime.datetime) -> Dict[str, Any]:
    where = []   # type: List[str]
    params = []  # type: List[Any]
    if args.status:
        if args.status not in TASK_STATUSES:
            raise CliError("--status 只能是 %s 之一" % "/".join(TASK_STATUSES))
        where.append("status = %s")
        params.append(args.status)
    sql = "SELECT " + TASK_COLUMNS + " FROM shared_tasks"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC, task_id DESC LIMIT %s"
    rows = store.exec(sql, params + [min(args.limit, MAX_LIMIT)], "all") or []
    tasks = [row_to_task(r, now) for r in rows]
    table = render_table(["任务", "标题", "负责人", "状态", "TTL", "结束", "已清空"],
                         [[t["task_id"], one_line(t["title"], 24), t["owner"],
                           TASK_STATUS_CN.get(t["status"], t["status"]) +
                           ("·TTL已到" if t["ttl_expired"] and t["status"] == "active" else ""),
                           fmt_time(t["ttl_at"]) if t["ttl_at"] else "-",
                           fmt_time(t["ended_at"]) if t["ended_at"] else "-",
                           fmt_time(t["destroyed_at"]) if t["destroyed_at"] else "-"]
                          for t in tasks])
    return {"ok": True, "count": len(tasks), "tasks": tasks,
            "_text": "任务容器 %d 个\n%s" % (len(tasks), table)}


# ---------------------------------------------------------------- sweep

def sweep_plan(store: Any, now: datetime.datetime, limit: int) -> Dict[str, Any]:
    """只读地把"该销毁 / 该过期 / 该复审"三份清单查出来。**一个字都不写。**

    三条规则（刚哥定的销毁条件）：
      1. 任务已结束（done/aborted）或过了兜底 ttl_at -> 这个任务下的**全部**任务级内容销毁；
      2. 任务级内容自己到期（expires_at < now）-> 直接销毁（临时共享不留过期残渣）；
      3. 永久级内容到期 -> 只标 expired（永久级的东西多半还有考古价值，不清正文）。
    另外把 review_at 已到的列出来提醒 owner 刷新 —— 那不是销毁，只是"该看一眼了"。
    """
    now_iso = now.isoformat()
    dead_tasks = [row_to_task(r, now) for r in (store.exec(
        "SELECT " + TASK_COLUMNS + " FROM shared_tasks WHERE destroyed_at IS NULL "
        "AND (status IN (%s, %s) OR (ttl_at IS NOT NULL AND ttl_at < %s::timestamptz)) "
        "ORDER BY created_at ASC LIMIT %s",
        ("done", "aborted", now_iso, limit), "all") or [])]

    by_task = []  # type: List[Dict[str, Any]]
    for task in dead_tasks:
        rows = store.exec(
            "SELECT " + CONTENT_COLUMNS + " FROM shared_content WHERE task_id = %s "
            "AND status <> %s ORDER BY content_id ASC LIMIT %s",
            (task["task_id"], "destroyed", limit), "all") or []
        for row in rows:
            item = row_to_content(row, now)
            item["reason"] = ("任务%s" % TASK_STATUS_CN.get(task["status"], task["status"])
                               if task["status"] in TASK_END_STATUSES else "任务 TTL 到点")
            by_task.append(item)

    picked = set(it["content_id"] for it in by_task)
    expired_task = [it for it in
                    [row_to_content(r, now) for r in (store.exec(
                        "SELECT " + CONTENT_COLUMNS + " FROM shared_content "
                        "WHERE scope = %s AND status = %s AND expires_at IS NOT NULL "
                        "AND expires_at < %s::timestamptz ORDER BY content_id ASC LIMIT %s",
                        ("task", "active", now_iso, limit), "all") or [])]
                    if it["content_id"] not in picked]
    for item in expired_task:
        item["reason"] = "任务级内容已过期"

    expired_perm = [row_to_content(r, now) for r in (store.exec(
        "SELECT " + CONTENT_COLUMNS + " FROM shared_content WHERE scope = %s AND status = %s "
        "AND expires_at IS NOT NULL AND expires_at < %s::timestamptz "
        "ORDER BY content_id ASC LIMIT %s",
        ("permanent", "active", now_iso, limit), "all") or [])]

    reviews = [row_to_content(r, now) for r in (store.exec(
        "SELECT " + CONTENT_COLUMNS + " FROM shared_content WHERE status = %s "
        "AND review_at IS NOT NULL AND review_at < %s::timestamptz "
        "ORDER BY review_at ASC LIMIT %s", ("active", now_iso, limit), "all") or [])]

    return {"tasks": dead_tasks, "destroy": by_task + expired_task,
            "expire": expired_perm, "review": reviews}


def run_sweep(store: Any, args: argparse.Namespace, now: datetime.datetime) -> Dict[str, Any]:
    limit = min(args.limit, MAX_LIMIT)
    plan = sweep_plan(store, now, limit)
    actor = args.by or "sweep"
    committed = bool(args.commit)

    if committed:
        for item in plan["destroy"]:
            store.set_note("sweep：%s，自动销毁" % item.get("reason", "到销毁条件"))
            store.exec(
                "UPDATE shared_content SET status = %s, body = %s, destroyed_at = %s::timestamptz, "
                "embedding = NULL, updated_at = %s::timestamptz, updated_by = %s, "
                "version = version + 1 WHERE content_id = %s AND status <> %s",
                ("destroyed", "", now.isoformat(), now.isoformat(), actor,
                 item["content_id"], "destroyed"))
        for item in plan["expire"]:
            store.set_note("sweep：永久级内容已过期，标记 expired（正文保留）")
            store.exec(
                "UPDATE shared_content SET status = %s, updated_at = %s::timestamptz, "
                "updated_by = %s, version = version + 1 WHERE content_id = %s AND status = %s",
                ("expired", now.isoformat(), actor, item["content_id"], "active"))
        for task in plan["tasks"]:
            store.set_note("sweep：这个任务下的共享内容已清空")
            store.exec("UPDATE shared_tasks SET destroyed_at = %s::timestamptz "
                       "WHERE task_id = %s AND destroyed_at IS NULL",
                       (now.isoformat(), task["task_id"]))
        store.set_note(None)

    head = ("【过期扫描】截至 %s —— %s" %
            (fmt_time(now), "**已落库**" if committed else "演练（dry-run，一个字节都没写）"))
    blocks = [head,
              "",
              "① 该销毁 %d 条（任务结束/TTL 到点/任务级内容过期）：" % len(plan["destroy"]),
              render_table(["ID", "范围", "标题", "任务", "原因"],
                           [[it["content_id"], SCOPE_CN.get(it["scope"], it["scope"]),
                             one_line(it["title"], 24), it["task_id"] or "-",
                             it.get("reason", "")] for it in plan["destroy"]]),
              "",
              "② 该标过期 %d 条（永久级，正文保留）：" % len(plan["expire"]),
              render_table(["ID", "标题", "维护人", "过期时间"],
                           [[it["content_id"], one_line(it["title"], 24), it["owner"],
                             fmt_time(it["expires_at"])] for it in plan["expire"]]),
              "",
              "③ 该复审 %d 条（只提醒，不动数据）：" % len(plan["review"]),
              render_table(["ID", "标题", "维护人", "复审时间"],
                           [[it["content_id"], one_line(it["title"], 24), it["owner"],
                             fmt_time(it["review_at"])] for it in plan["review"]])]
    if not committed and (plan["destroy"] or plan["expire"]):
        blocks.append("")
        blocks.append("以上都没有执行。确认无误后加 --commit 才会真的落库。")
    return {"ok": True, "committed": committed,
            "destroy": plan["destroy"], "expire": plan["expire"],
            "review": plan["review"], "tasks": plan["tasks"],
            "_text": "\n".join(blocks)}


# ================================================================ 入口

HANDLERS = {
    "task-add": run_task_add,
    "task-end": run_task_end,
    "tasks": run_tasks,
    "put": run_put,
    "update": run_update,
    "revoke": run_revoke,
    "destroy": run_destroy,
    "get": run_get,
    "list": run_list,
    "search": run_search,
    "sweep": run_sweep,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="共享内容（任务级 / 永久级）管理 CLI —— 发布、刷新、撤下、销毁、检索")
    parser.add_argument("--dsn-file", default=DEFAULT_DSN_FILE,
                        help="systemd 环境文件，从中取 BB_PG_DSN=（默认 %s）" % DEFAULT_DSN_FILE)
    parser.add_argument("--sqlite", default=None,
                        help="离线自测/演示：走本地 sqlite 替身，不连 PG（不是生产路径）")
    parser.add_argument("--json", action="store_true", help="输出机器可读的 JSON")
    parser.add_argument("--version", action="version", version="shared_content " + VERSION)
    subs = parser.add_subparsers(dest="cmd")

    # --- 任务容器 ---
    p = subs.add_parser("task-add", help="建一个任务容器（任务级内容挂在它下面）")
    p.add_argument("--task-id", required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--owner", required=True)
    p.add_argument("--ttl-hours", type=float, default=None,
                   help="兜底过期：到点即使没人标结束，内容也会被 sweep 销毁")
    p.add_argument("--ttl-at", default=None, help="兜底过期的绝对时间（ISO8601）")
    p.add_argument("--note", default=None)

    p = subs.add_parser("task-end", help="任务收尾（内容的真正销毁由 sweep --commit 执行）")
    p.add_argument("--task-id", required=True)
    p.add_argument("--status", default="done", help="done|aborted")
    p.add_argument("--note", default=None)

    p = subs.add_parser("tasks", help="列任务容器")
    p.add_argument("--status", default=None, help="active|done|aborted")
    p.add_argument("--limit", type=int, default=DEFAULT_LIMIT)

    # --- 内容 ---
    p = subs.add_parser("put", help="发布一条共享内容")
    p.add_argument("--scope", required=True, help="task|permanent")
    p.add_argument("--task-id", default=None, help="scope=task 必填；permanent 一律不许给")
    p.add_argument("--kind", default="note",
                   help="node|agent|owner_profile|owner_key_info|config|note|...")
    p.add_argument("--title", required=True)
    p.add_argument("--body-file", default=None, help="正文文件（Markdown/纯文本）")
    p.add_argument("--body", default=None, help="直接给一段正文（没有文件时用）")
    p.add_argument("--owner", required=True, help="维护责任人：信息变了由他刷新")
    p.add_argument("--by", default=None, help="本次操作人（默认取 --owner）")
    p.add_argument("--tags", default=None, help="逗号分隔")
    p.add_argument("--audience", default=None, help="all 或 逗号分隔的成员名")
    p.add_argument("--sensitivity", default="internal", help="public|internal|sensitive")
    p.add_argument("--expires-in-days", type=float, default=None)
    p.add_argument("--expires-in-hours", type=float, default=None)
    p.add_argument("--expires-at", default=None, help="绝对过期时间（ISO8601）")
    p.add_argument("--review-in-days", type=float, default=None,
                   help="多少天后该复审一次（信息会变，到点提醒 owner 刷新）")
    p.add_argument("--review-at", default=None)
    p.add_argument("--source-ref", action="append", default=None,
                   help="来源，形如 entry:61 / session:rt-xxx，可重复")
    p.add_argument("--note", default=None, help="留痕理由")
    p.add_argument("--no-embed", action="store_true", help="不算向量（离线/批量导入时用）")

    p = subs.add_parser("update", help="刷新一条共享内容（自动留痕）")
    p.add_argument("--id", type=int, required=True)
    p.add_argument("--by", required=True, help="操作人：会写进 updated_by 与留痕的 actor")
    p.add_argument("--note", default=None, help="改了什么 / 为什么改")
    p.add_argument("--title", default=None)
    p.add_argument("--body-file", default=None)
    p.add_argument("--body", default=None)
    p.add_argument("--tags", default=None)
    p.add_argument("--audience", default=None)
    p.add_argument("--owner", default=None)
    p.add_argument("--kind", default=None)
    p.add_argument("--sensitivity", default=None)
    p.add_argument("--status", default=None, help="active|expired|revoked")
    p.add_argument("--expires-at", default=None)
    p.add_argument("--expires-in-days", type=float, default=None)
    p.add_argument("--review-at", default=None)
    p.add_argument("--review-in-days", type=float, default=None)
    p.add_argument("--source-ref", action="append", default=None)
    p.add_argument("--no-embed", action="store_true")

    for name, helptext in (("revoke", "撤下（status=revoked，默认不再返回给 Agent）"),
                           ("destroy", "销毁（正文置空 + destroyed，行与留痕保留）")):
        p = subs.add_parser(name, help=helptext)
        p.add_argument("--id", type=int, required=True)
        p.add_argument("--by", required=True)
        p.add_argument("--note", default=None)

    p = subs.add_parser("get", help="看一条共享内容 + 它的全部改动历史")
    p.add_argument("--id", type=int, required=True)

    p = subs.add_parser("list", help="列共享内容")
    p.add_argument("--scope", default=None, help="task|permanent")
    p.add_argument("--kind", default=None)
    p.add_argument("--owner", default=None)
    p.add_argument("--tag", default=None)
    p.add_argument("--task-id", default=None)
    p.add_argument("--status", default=None, help="active|expired|revoked|destroyed")
    p.add_argument("--expired", action="store_true", help="把已过期的也带出来")
    p.add_argument("--limit", type=int, default=DEFAULT_LIMIT)

    p = subs.add_parser("search", help="检索：关键词（默认）或语义（--semantic）")
    p.add_argument("--q", required=True)
    p.add_argument("--semantic", action="store_true",
                   help="走向量余弦；模型不可用时自动降级为关键词")
    p.add_argument("--scope", default=None)
    p.add_argument("--kind", default=None)
    p.add_argument("--owner", default=None)
    p.add_argument("--tag", default=None)
    p.add_argument("--task-id", default=None)
    p.add_argument("--status", default=None)
    p.add_argument("--expired", action="store_true")
    p.add_argument("--limit", type=int, default=DEFAULT_LIMIT)

    p = subs.add_parser("sweep", help="过期扫描：缺省只演练，--commit 才落库")
    p.add_argument("--commit", action="store_true", help="真的销毁 / 标过期")
    p.add_argument("--by", default=None, help="记进 updated_by 的操作人，默认 sweep")
    p.add_argument("--limit", type=int, default=MAX_LIMIT)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_help()
        return 2

    store = None
    try:
        store = open_store(args)
        result = HANDLERS[args.cmd](store, args, now_utc())
    except CliError as exc:
        sys.stderr.write("失败：%s\n" % safe_err(exc))
        return 2
    except Exception as exc:       # 库报错 / 驱动没装：给人话，不甩栈
        sys.stderr.write("失败：%s\n" % safe_err(exc))
        return 1
    finally:
        if store is not None:
            store.close()

    if args.json:
        payload = dict((k, v) for k, v in result.items() if k != "_text")
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    else:
        sys.stdout.write(result["_text"] + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
