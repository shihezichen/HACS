#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rt_remind.py —— 每日逾期提醒（一次性跑完即退，**不常驻、不监听任何端口**）

一句话：把 roundtable_actions（落实事项）与 roundtable_issues（遗留问题，含
kind='defect' 的**缺席缺陷单**）里「还欠着且已经过期」的条目捞出来，按责任人分组，
打印一份汇总，再给每个责任人生成一条**点名用**的短消息（subject = a2a.v1.req.<owner>）。

两条硬口径：
  * **逾期** = status 还没闭环（action: open/doing；issue: open/watching）
    且 due_at 非空且 **严格早于**当前时刻。正好等于 now 的那一刻还不算逾期。
  * **同一事项当天只提醒一次**：发送前查 reminder_log 里今天（UTC 日历日）有没有
    这条记录，有就跳过 —— 逾期三天不该被吼三十遍。reminder_log 是 append-only
    （见 sql/roundtable_ops_schema2.sql，UPDATE/DELETE/TRUNCATE 全拒）。

纪律（与 roundtable.py / blackboard_svc.py 同一套）：
  * 纯标准库 + psycopg2（唯一第三方依赖，且只在真连库时才导入）；NATS 复用同目录的
    nats_client.py（team_call.py 用的也是它），绝不自己重写协议。
  * 凭据一律走**文件路径**参数（--dsn-file / --nats-password-file），源码、日志、
    打印里都不出现凭据值。
  * --dsn-file 指向的是 **systemd 环境文件**，取值等价于
    `sed -n 's/^BB_PG_DSN=//p' <file> | head -1`。**绝不能 bash source 它** ——
    DSN 里带空格（host=... port=... dbname=...），shell 会在第一个空格处截断成错值。
  * `--dry-run` 只读不写不发；`--commit` 才写 reminder_log 并真发 NATS 点名。
    两个都不给时按 --dry-run 处理（宁可少发一次，也不在没人确认时乱喊人）。

用法：
    # 演练：连库读逾期清单，打印汇总与点名文本，一个字节都不落库、一条消息都不发
    python rt_remind.py --dry-run --dsn-file <ETC_DIR>/pg.env

    # 正式跑（每天一次，交给 systemd timer / cron）
    python rt_remind.py --commit --dsn-file <ETC_DIR>/pg.env \\
        --nats-host <NODE_IP> --nats-user <MEMBER_ID> \\
        --nats-password-file <PASS_FILE>

    # 离线自测：从 JSON 读清单，**不连任何数据库、不联网**
    python rt_remind.py --dry-run --fixture tests/fixtures/overdue.json

退出码：0 跑完（没有逾期也算跑完）；1 有条目发送失败；2 参数或初始化错误。
"""

import argparse
import datetime
import json
import os
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple
import node_paths as _np  # L0 平台层：宿主默认值唯一出处（env 优先）

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

VERSION = "1.0"

UTC = datetime.timezone.utc

ACTION_OPEN_STATUSES = ("open", "doing")        # 落实事项"还欠着"的状态
ISSUE_OPEN_STATUSES = ("open", "watching")      # 遗留问题"还在跟踪"的状态

# 兜底默认值集中到 L0 node_paths（env 优先）
AGENT_TEAM_ETC_DIR = os.environ.get("AGENT_TEAM_ETC_DIR") or _np.etc_dir()
DEFAULT_DSN_FILE = os.environ.get("AGENT_TEAM_PG_ENV_FILE") or (AGENT_TEAM_ETC_DIR + "/pg.env")
DEFAULT_LIMIT = 200          # 单张表一次最多捞这么多条（逾期真到这个量级，问题不在提醒上）
DEFAULT_TIMEOUT_S = 60.0
MAX_TIMEOUT_S = 300.0
TITLE_MAX = 120              # 进短消息的标题截断长度
CALL_ITEMS_MAX = 5           # 一条点名消息里最多列几项，超了写"…等 N 项"
CALL_TEXT_MAX = 900          # 点名消息整体长度上限

# 人话标签：英文枚举进来、中文出去（与看板口径一致）
KIND_CN = {"action": "落实事项", "issue": "遗留问题", "defect": "疑似缺陷"}
STATUS_CN = {"open": "待办", "doing": "进行中", "watching": "观察中"}
NO_OWNER = "（未指定责任人）"


# ================================================================ 小工具

def now_utc() -> datetime.datetime:
    return datetime.datetime.now(UTC)


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


def read_secret(path: str) -> str:
    """读凭据文件（只取内容去空白）。内容绝不打印、绝不进日志。"""
    with open(path, "r", encoding="utf-8-sig") as fh:
        return fh.read().strip()


def truncate(text: Any, limit: int) -> str:
    text = "" if text is None else str(text)
    return text if len(text) <= limit else text[:limit] + "…"


def one_line(text: Any, limit: int) -> str:
    return truncate(" ".join(str(text or "").split()), limit)


def to_utc(value: Any) -> Optional[datetime.datetime]:
    """timestamptz / ISO 字符串统一成带 UTC 时区的 datetime；认不出来返回 None。

    裸时间（不带时区）一律按 UTC 解释 —— 口径单一，不看跑在哪台机器上。
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


def day_start(now: datetime.datetime) -> datetime.datetime:
    """当天（UTC 日历日）的零点 —— "今天提醒过没有"就以它为界。"""
    moment = to_utc(now) or now_utc()
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


def is_overdue(due_at: Any, status: Any, open_states: Sequence[str],
               now: datetime.datetime) -> bool:
    """逾期 = 还没闭环 且 有截止时间 且 截止时间**严格早于** now。

    口径与 blackboard_svc.is_overdue 一字不差：两边算出来的红标必须是同一件事。
    """
    if status not in tuple(open_states):
        return False
    moment = to_utc(due_at)
    if moment is None:
        return False
    return moment < now


def human_late(due_at: Any, now: datetime.datetime) -> str:
    """逾期多久，人话版：`3 天 5 小时` / `2 小时 10 分钟` / `7 分钟`。"""
    moment = to_utc(due_at)
    if moment is None:
        return "未定截止"
    seconds = int((now - moment).total_seconds())
    if seconds < 0:
        return "还没到期"
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return "%d 天 %d 小时" % (days, hours)
    if hours:
        return "%d 小时 %d 分钟" % (hours, minutes)
    return "%d 分钟" % minutes


def fmt_time(value: Any) -> str:
    """统一显示成北京时间（UTC+8），和看板对得上口。"""
    moment = to_utc(value)
    if moment is None:
        return "未定截止"
    return (moment + datetime.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M") + " CST"


# ================================================================ 事项的统一形状

def make_item(target_kind: str, target_id: Any, title: Any, owner: Any, due_at: Any,
              status: Any, session_id: Any = None, issue_kind: Any = None,
              topic: Any = None) -> Dict[str, Any]:
    """把 action / issue 两种行规整成同一个形状，后面的分组与渲染只认它。"""
    kind = str(issue_kind or "").strip().lower()
    if target_kind != "issue" or kind not in ("issue", "defect"):
        kind = None if target_kind != "issue" else "issue"
    return {"target_kind": target_kind, "target_id": target_id,
            "title": one_line(title, 500), "owner": (str(owner).strip() if owner else None),
            "due_at": to_utc(due_at), "status": status, "session_id": session_id,
            "issue_kind": kind, "topic": topic}


def label_of(item: Dict[str, Any]) -> str:
    """这条事项在人话里叫什么：落实事项 / 遗留问题 / 疑似缺陷。"""
    if item["target_kind"] == "action":
        return KIND_CN["action"]
    return KIND_CN.get(item.get("issue_kind") or "issue", KIND_CN["issue"])


def item_key(item: Dict[str, Any]) -> Tuple[str, Any]:
    return (item["target_kind"], item["target_id"])


def select_overdue(items: Sequence[Dict[str, Any]],
                   now: datetime.datetime) -> List[Dict[str, Any]]:
    """从一堆事项里筛出逾期的，按"逾期最久的排最前"。

    PG 那边同样的条件下推到了 SQL（见 PgStore），这里是内存版 / 单测口径 —— 两边
    共用 is_overdue，判据只有一份。
    """
    out = []  # type: List[Dict[str, Any]]
    for item in items:
        states = (ACTION_OPEN_STATUSES if item["target_kind"] == "action"
                  else ISSUE_OPEN_STATUSES)
        if is_overdue(item.get("due_at"), item.get("status"), states, now):
            out.append(item)
    out.sort(key=lambda r: (r["due_at"], str(r["target_kind"]), r["target_id"]))
    return out


def group_by_owner(items: Sequence[Dict[str, Any]]) -> List[Tuple[str, List[Dict[str, Any]]]]:
    """按责任人分组。无主的归到最后一组，**不许被悄悄丢掉**。"""
    buckets = {}  # type: Dict[str, List[Dict[str, Any]]]
    order = []    # type: List[str]
    for item in items:
        owner = item.get("owner") or NO_OWNER
        if owner not in buckets:
            buckets[owner] = []
            order.append(owner)
        buckets[owner].append(item)
    named = sorted(o for o in order if o != NO_OWNER)
    if NO_OWNER in buckets:
        named.append(NO_OWNER)
    return [(owner, buckets[owner]) for owner in named]


# ================================================================ 文本渲染

def render_summary(groups: Sequence[Tuple[str, List[Dict[str, Any]]]],
                   now: datetime.datetime, skipped: int = 0) -> str:
    """① 按责任人分组的汇总文本（给人看的那一份）。"""
    total = sum(len(rows) for _owner, rows in groups)
    lines = ["【逾期提醒】截至 %s，待提醒 %d 项，涉及 %d 人"
             % (fmt_time(now), total, len(groups))]
    if skipped:
        lines.append("（另有 %d 项今天已经提醒过，本次不再重复）" % skipped)
    if not total:
        lines.append("没有逾期事项 —— 今天不用喊人。")
        return "\n".join(lines)
    for owner, rows in groups:
        lines.append("")
        lines.append("== %s（%d 项）==" % (owner, len(rows)))
        if owner == NO_OWNER:
            lines.append("   ！这几项没有责任人，点名发不出去，需要人工认领。")
        for idx, item in enumerate(rows, 1):
            lines.append("   %d. [%s #%s] %s" % (idx, label_of(item), item["target_id"],
                                                 one_line(item["title"], 200)))
            lines.append("      截止 %s，已逾期 %s；状态 %s；来源会议 %s"
                         % (fmt_time(item["due_at"]), human_late(item["due_at"], now),
                            STATUS_CN.get(item["status"], item["status"]),
                            item.get("topic") or item.get("session_id") or "-"))
    return "\n".join(lines)


def render_call(owner: str, rows: Sequence[Dict[str, Any]],
                now: datetime.datetime) -> str:
    """② 给一个责任人的**点名短消息**（发到 a2a.v1.req.<owner>）。

    刻意写短：点名是要人干活，不是给人读报告。超过 CALL_ITEMS_MAX 项就只列前几条。
    """
    head = "[逾期提醒] %s：你有 %d 项已逾期未闭环。" % (owner, len(rows))
    parts = []  # type: List[str]
    for idx, item in enumerate(rows[:CALL_ITEMS_MAX], 1):
        parts.append("%d) 【%s】%s（截止 %s，逾期 %s）"
                     % (idx, label_of(item), one_line(item["title"], TITLE_MAX),
                        fmt_time(item["due_at"]), human_late(item["due_at"], now)))
    if len(rows) > CALL_ITEMS_MAX:
        parts.append("…等共 %d 项" % len(rows))
    tail = ("请今天内做到二选一：把事办完并回帖闭环，或者说明为什么改期、改到什么时候。"
            "缺陷单（疑似程序缺陷）必须查清原因，不许直接关掉。")
    return truncate(head + " " + " ".join(parts) + " " + tail, CALL_TEXT_MAX)


# ================================================================ 存储层

class PgStore(object):
    """真库访问层。只在真连库时才导入 psycopg2。

    读的是 roundtable_actions / roundtable_issues / reminder_log；
    写的只有 **reminder_log 的 INSERT** —— 这个脚本不改任何业务表的状态。
    """

    def __init__(self, dsn: str) -> None:
        if not dsn:
            raise RuntimeError("DSN 为空：检查 --dsn-file 指向的文件里有没有 BB_PG_DSN=")
        import psycopg2  # noqa: F401  —— 唯一的第三方依赖，延迟导入
        self._conn = psycopg2.connect(dsn, connect_timeout=5)
        self._conn.autocommit = True

    def _exec(self, sql: str, params: Sequence[Any] = (), fetch: str = "none") -> Any:
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

    def overdue(self, now: datetime.datetime, limit: int) -> List[Dict[str, Any]]:
        """两张表各捞一次逾期清单。过滤条件全部参数化，比较用的 now 由调用方给。"""
        rows = self._exec(
            "SELECT a.action_id, a.what, a.owner, a.due_at, a.status, a.session_id, s.topic "
            "FROM roundtable_actions a "
            "LEFT JOIN roundtable_sessions s ON s.session_id = a.session_id "
            "WHERE a.status IN (%s, %s) AND a.due_at IS NOT NULL "
            "  AND a.due_at < %s::timestamptz "
            "ORDER BY a.due_at ASC, a.action_id ASC LIMIT %s",
            tuple(ACTION_OPEN_STATUSES) + (now.isoformat(), limit), fetch="all") or []
        out = [make_item("action", r[0], r[1], r[2], r[3], r[4], r[5], None, r[6])
               for r in rows]

        rows = self._exec(
            "SELECT i.issue_id, i.title, i.owner, i.due_at, i.status, i.session_id, "
            "       i.kind, s.topic "
            "FROM roundtable_issues i "
            "LEFT JOIN roundtable_sessions s ON s.session_id = i.session_id "
            "WHERE i.status IN (%s, %s) AND i.due_at IS NOT NULL "
            "  AND i.due_at < %s::timestamptz "
            "ORDER BY i.due_at ASC, i.issue_id ASC LIMIT %s",
            tuple(ISSUE_OPEN_STATUSES) + (now.isoformat(), limit), fetch="all") or []
        out.extend(make_item("issue", r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7])
                   for r in rows)
        out.sort(key=lambda r: (r["due_at"], str(r["target_kind"]), r["target_id"]))
        return out

    def sent_today(self, now: datetime.datetime) -> set:
        """今天（UTC 日历日）已经提醒过的 (kind, id) 集合。"""
        rows = self._exec(
            "SELECT target_kind, target_id FROM reminder_log "
            "WHERE sent_at >= %s::timestamptz",
            (day_start(now).isoformat(),), fetch="all") or []
        return set((r[0], r[1]) for r in rows)

    def log_reminder(self, target_kind: str, target_id: Any,
                     owner: Optional[str]) -> Optional[int]:
        got = self._exec(
            "INSERT INTO reminder_log (target_kind, target_id, owner) "
            "VALUES (%s,%s,%s) RETURNING reminder_id",
            (target_kind, target_id, owner), fetch="one")
        return got[0] if got else None

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass


class MemoryStore(object):
    """内存版存储：--fixture 离线自测与单测用，**不碰数据库、不联网**。

    过滤口径与 PgStore 共用 select_overdue / is_overdue，不在这儿重写一遍判据。
    """

    def __init__(self, items: Optional[Sequence[Dict[str, Any]]] = None,
                 sent: Optional[Sequence[Tuple[str, Any]]] = None) -> None:
        self.items = list(items or [])
        self.sent = [(k, i, to_utc(t) or now_utc()) for (k, i, t) in (sent or [])]
        self.logged = []  # type: List[Dict[str, Any]]

    def overdue(self, now: datetime.datetime, limit: int) -> List[Dict[str, Any]]:
        # limit 在 PgStore 那边是**每张表**的上限，两张表合起来就是 2×limit，这里对齐
        return select_overdue(self.items, now)[:limit * 2]

    def sent_today(self, now: datetime.datetime) -> set:
        start = day_start(now)
        return set((k, i) for (k, i, ts) in self.sent if ts >= start)

    def log_reminder(self, target_kind: str, target_id: Any,
                     owner: Optional[str]) -> Optional[int]:
        self.logged.append({"target_kind": target_kind, "target_id": target_id,
                            "owner": owner})
        self.sent.append((target_kind, target_id, now_utc()))
        return len(self.logged)

    def close(self) -> None:
        return None


def load_fixture(path: str) -> MemoryStore:
    """从 JSON 造一个内存库（离线自测用）。格式见本文件顶部的 --fixture 说明。"""
    with open(path, "r", encoding="utf-8-sig") as fh:
        data = json.load(fh)
    items = []  # type: List[Dict[str, Any]]
    for row in data.get("actions") or ():
        items.append(make_item("action", row.get("action_id"), row.get("what"),
                               row.get("owner"), row.get("due_at"),
                               row.get("status", "open"), row.get("session_id"),
                               None, row.get("topic")))
    for row in data.get("issues") or ():
        items.append(make_item("issue", row.get("issue_id"), row.get("title"),
                               row.get("owner"), row.get("due_at"),
                               row.get("status", "open"), row.get("session_id"),
                               row.get("kind", "issue"), row.get("topic")))
    sent = [(r.get("target_kind"), r.get("target_id"), r.get("sent_at"))
            for r in (data.get("reminder_log") or ())]
    return MemoryStore(items, sent)


# ================================================================ 发送层

class NatsSender(object):
    """走 NATS 点名（subject = a2a.v1.req.<owner>）。复用同目录的 nats_client.py。"""

    name = "nats"

    def __init__(self, host: str, port: int, user: str, password_file: str,
                 timeout: float = DEFAULT_TIMEOUT_S) -> None:
        from nats_client import NatsClient  # 纯标准库，同目录
        self._timeout = float(timeout)
        self._client = NatsClient(host=host, port=int(port), user=user,
                                  password=read_secret(password_file) if password_file else "",
                                  name="rt_remind")
        self._opened = False

    def send(self, owner: str, text: str) -> Tuple[bool, str]:
        envelope = {"from": "rt_remind", "text": text, "timeout_s": self._timeout}
        raw = json.dumps(envelope, ensure_ascii=False).encode("utf-8")
        try:
            if not self._opened:
                self._client.connect()
                self._opened = True
            reply = self._client.request("a2a.v1.req.%s" % owner, raw,
                                         timeout=self._timeout + 15.0)
        except Exception as exc:
            return False, "NATS 请求异常：%s" % truncate(exc, 160)
        if reply is None:
            return False, "点名超时（%.0fs 内无应答）" % self._timeout
        return True, truncate(reply.decode("utf-8", "replace"), 200)

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass


class PrintSender(object):
    """--dry-run 用：只打印要发什么，**一条消息都不发、一个字节都不落库**。"""

    name = "dry-run"

    def __init__(self, out: Any = None) -> None:
        self.sent = []  # type: List[Tuple[str, str]]
        self._out = out or (lambda line: sys.stdout.write(line + "\n"))

    def send(self, owner: str, text: str) -> Tuple[bool, str]:
        self.sent.append((owner, text))
        self._out("[dry-run] 本该点名 a2a.v1.req.%s，内容如下（没发）：" % owner)
        self._out("          " + text)
        return True, "dry-run 未发送"

    def close(self) -> None:
        return None


# ================================================================ 主流程

def run_remind(store: Any, sender: Any, now: Optional[datetime.datetime] = None,
               commit: bool = False, limit: int = DEFAULT_LIMIT,
               owner_filter: Optional[str] = None,
               out: Optional[Any] = None) -> Dict[str, Any]:
    """跑一次提醒。返回统计字典（单测直接断言它）。

    commit=False（--dry-run）时：**不写 reminder_log、不发消息**，只打印。
    """
    say = out or (lambda line: sys.stdout.write(line + "\n"))
    now = to_utc(now) or now_utc()

    items = store.overdue(now, limit)
    items = select_overdue(items, now)          # PG 已经筛过一遍，这里兜底保证口径一致
    if owner_filter:
        items = [i for i in items if (i.get("owner") or "") == owner_filter]

    already = store.sent_today(now)
    pending = [i for i in items if item_key(i) not in already]
    skipped = len(items) - len(pending)

    groups = group_by_owner(pending)
    say(render_summary(groups, now, skipped))
    say("")

    sent_ok, failed, logged = 0, 0, 0
    for owner, rows in groups:
        text = render_call(owner, rows, now)
        if owner == NO_OWNER:
            say("[跳过] %d 项没有责任人，点名发不出去，等人工认领" % len(rows))
            continue
        if not commit:
            sender.send(owner, text)            # PrintSender：只打印
            continue
        ok, detail = sender.send(owner, text)
        if ok:
            sent_ok += 1
            say("[已点名] %s（%d 项）→ %s" % (owner, len(rows), truncate(detail, 120)))
            for item in rows:                   # 发成功才记账，失败留到明天再喊
                store.log_reminder(item["target_kind"], item["target_id"], owner)
                logged += 1
        else:
            failed += 1
            say("[点名失败] %s（%d 项）：%s —— 本次不记 reminder_log，下次还会提醒"
                % (owner, len(rows), detail))

    stats = {"overdue": len(items), "pending": len(pending), "skipped": skipped,
             "owners": len(groups), "sent": sent_ok, "failed": failed,
             "logged": logged, "commit": bool(commit)}
    say("[汇总] 逾期 %d 项（今天已提醒过 %d 项）／待提醒 %d 项／责任人 %d 人／"
        "已点名 %d 人／失败 %d 人／记账 %d 条／模式 %s"
        % (stats["overdue"], stats["skipped"], stats["pending"], stats["owners"],
           stats["sent"], stats["failed"], stats["logged"],
           "commit" if commit else "dry-run"))
    return stats


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="每日逾期提醒（落实事项 + 遗留问题/缺席缺陷单），跑完即退")
    parser.add_argument("--dry-run", action="store_true",
                        help="只读只打印：不写 reminder_log、不发任何消息（缺省行为）")
    parser.add_argument("--commit", action="store_true",
                        help="真发：写 reminder_log 并走 NATS 点名责任人")
    parser.add_argument("--dsn-file", default=DEFAULT_DSN_FILE,
                        help="systemd 环境文件，从中取 BB_PG_DSN=（默认 %s）" % DEFAULT_DSN_FILE)
    parser.add_argument("--fixture", default=None,
                        help="离线自测：从 JSON 文件读清单，不连数据库（只能配 --dry-run）")
    parser.add_argument("--owner", default=None, help="只提醒这一个责任人（可选）")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                        help="单张表一次最多捞多少条，默认 %d" % DEFAULT_LIMIT)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S,
                        help="单次点名等回包上限秒，默认 %d（最大 %d）"
                             % (DEFAULT_TIMEOUT_S, MAX_TIMEOUT_S))
    parser.add_argument("--nats-host", default=os.environ.get("AGENT_TEAM_NATS_HOST") or "<host>", help="NATS 中枢地址")
    parser.add_argument("--nats-port", type=int, default=9930, help="NATS 端口，默认 9930")
    parser.add_argument("--nats-user", default=None, help="自己的 NATS 用户名")
    parser.add_argument("--nats-password-file", default=None, help="自己的 NATS 口令文件")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    out = lambda line: sys.stdout.write(line + "\n")  # noqa: E731

    commit = bool(args.commit) and not args.dry_run
    # ── 停用开关（2026-09-20 <USER_ID>指令）──────────────────────────────────────
    # <USER_ID>：「旧的会议有个遗留提醒功能, 一直在发消息提醒各agent,你去把它停了」
    # 本脚本 = 旧会议的「遗留问题 / 逾期提醒」，会按责任人逐个点名，往
    # a2a.v1.req.<owner> 发提醒消息给各成员。**发送路径现已被拒**：
    # 任何带 --commit（真发 + 写 reminder_log）的调用都直接返回，不发任何消息；
    # 只读演练（--dry-run / 不加 --commit）仍然可用，便于保留验证能力。
    # 确需恢复：先经<USER_ID>同意，再以 RT_REMIND_ENABLE=1 运行。
    if commit and os.environ.get("RT_REMIND_ENABLE") != "1":
        out("[已停用] rt_remind.py —— 按 2026-09-20 <USER_ID> 指令，旧会议的遗留/逾期提醒"
            "功能已停止：不再向任何成员点名发消息，--commit 路径被拒（不连库、不发消息）。"
            "只读演练仍可用；确需恢复请先经 <USER_ID> 同意，再以 RT_REMIND_ENABLE=1 运行。")
        return 0
    if args.fixture and commit:
        sys.stderr.write("--fixture 是离线自测用的假数据，不许配 --commit\n")
        return 2
    if not commit:
        out("[演练] 未给 --commit：只读清单、只打印点名文本，不写 reminder_log、不发消息")

    # --- 存储
    try:
        store = load_fixture(args.fixture) if args.fixture else PgStore(read_dsn(args.dsn_file))
    except Exception as exc:
        sys.stderr.write("初始化存储失败：%s: %s\n"
                         % (exc.__class__.__name__, truncate(exc, 200)))
        return 2

    # --- 发送通道：只有 --commit 才接真 NATS
    sender = None  # type: Any
    try:
        if commit:
            if not args.nats_user or not args.nats_password_file:
                sys.stderr.write("--commit 需要 --nats-user 与 --nats-password-file\n")
                return 2
            sender = NatsSender(args.nats_host, args.nats_port, args.nats_user,
                                args.nats_password_file,
                                max(1.0, min(float(args.timeout), MAX_TIMEOUT_S)))
        else:
            sender = PrintSender(out)
    except Exception as exc:
        store.close()
        sys.stderr.write("点名通道不可用：%s\n" % truncate(exc, 200))
        return 2

    try:
        stats = run_remind(store, sender, now=now_utc(), commit=commit,
                           limit=max(1, int(args.limit)), owner_filter=args.owner, out=out)
    except Exception as exc:
        sys.stderr.write("提醒过程异常：%s: %s\n"
                         % (exc.__class__.__name__, truncate(exc, 200)))
        return 2
    finally:
        try:
            sender.close()
        except Exception:
            pass
        try:
            store.close()
        except Exception:
            pass

    return 1 if stats["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
