#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
roundtable.py —— 圆桌会议调度器（一次性跑完即退，**不常驻、不监听任何端口**）

一句话：按**议程逐项**挨个点名成员发言，把每条发言同时写进**黑板**（走 9940 的
POST /entries，走 append-only 的正路）和 **roundtable_turns**（留痕），
每项判到收敛或撞上限就让主持人出该项结论、**当场点名确认**，全部议程走完再出
总体结论 + **落实事项（有人有期限）**，然后退出。

会议四要素（<USER_ID>口径：会议有目标，会前有议程，会中有结论，会有落实）：
  * **目标**：--goal 必填，写进 sessions.goal（冻结列），每次 prompt 都带；
  * **议程**：--agenda 必填，写进 sessions.agenda 快照 + roundtable_agenda_items
    逐项行；每项有 title / expected，会中逐项推进、逐项出结论；
  * **当场确认**：每项结论出来后向**本场实际发言过的成员**逐个点名要 verdict，
    写 roundtable_confirmations；**分母是「点名时在场的人」**（absent=无应答者不计入
    分母，只单独记录），无 object 且 agree >= ⌊在场人数/2⌋+1 才算通过，
    不通过该项记 deferred，**绝不静默当通过**；
  * **落实**：总体结论必须带 actions（what/owner/due_at），owner 必须在 roster 内、
    due_at 必须是未来时刻，校验不过重试 1 次，仍不过 → 会议记 incomplete。
  * **遗留问题**：deferred 项 / 被 object 的结论 / 缺席导致证据不全 → roundtable_issues。

出席率与缺席开单（<USER_ID>澄清后的最终口径，别再写成"缺席率"）：
  * **出席率 = 在场人数 / 应到人数**。应到 = sessions.members 全集（原定期望成员）；
    在场 = 本场**至少应答过一次点名**的成员。两个数与缺席名单都落回 sessions
    （expected_count / present_count / absent_count / attendance_rate）。
  * **注意两个分母不是一个东西**：确认通过的分母是「在场人数」，出席率的分母是
    「应到人数」。前者防的是"缺席把门槛降下来"，后者量的是"人到齐了没有"。
  * **缺席 = 疑似 Bug**：成员都是云主机 / 常驻 Agent，设计上不允许不在线。只要有人
    点名无应答，就自动落一条 kind='defect' 的遗留问题（owner=该成员、due_at=24h 后、
    evidence 里写清点名次数与失败原因），同一成员同一场会**只开一条**，多次缺席合并计数。

设计要点（与 blackboard_svc.py / teamd.py 同一套口径）：
  * 纯标准库 + psycopg2（唯一第三方依赖，且只在真写库时才导入）；NATS 复用
    本目录的 nats_client.py，绝不自己重写协议。
  * **省 token 是硬指标**：prompt 里永远不放任何成员的完整历史发言，只放
    「问题 + 本轮选中的黑板片段（k<=12 条，每条截断）+ 输出契约」，总长度夹在
    --max-chars 以内。每轮每人的 prompt 字符数与片段数都会打印出来备查。
  * **在线判定不花 token**：点名前先发一个缺 text 字段的探针包，teamd 会立刻回
    bad_request（它在调 A2A 之前就校验完了），拿到任何回包即判在线；没回包判 absent。
  * **refs 空就不入库**：没有证据的观点不进黑板，turn 记 error。这是纪律，不是可选项。
  * **不留 running 僵尸**：进程内任何异常都会把 session 落成 aborted + note 再退出。
  * 凭据一律走文件路径参数（--token-file / --nats-password-file / --dsn-file），
    源码里不出现任何凭据值，日志与打印也不带。

解析降级策略（一句话）：能抠出 JSON 就按契约字段走；抠不出来（重试 1 次后仍失败）
  就**把整段原文塞进 content、refs 自引本 turn**，照常入库并在 turn.error 里记
  parse_degraded 原因 —— 宁可留一条"格式不合规但内容还在"的发言，也不让整场崩。

用法：
    # 演练：真点名、真解析，但一个字节都不落库（没有 --commit）
    python roundtable.py --topic p5-roundtable --question "备份该多久跑一次？" \\
        --goal "定下备份频率与保留期，并落到人" \\
        --agenda "频率|给出每天/每周的明确数字; 保留期|给出天数与异地副本方案" \\
        --members <MEMBER_ID_1>,<MEMBER_ID_2>,<MEMBER_ID_3> \\
        --nats-host <NODE_IP> --nats-user <MEMBER_ID> \\
        --nats-password-file <PASS_FILE>

    # 正式跑：写黑板 + 写 PG
    python roundtable.py ... --commit \\
        --token-file <TOKEN_FILE> --dsn-file <ETC_DIR>/pg.env

    # 离线自测：假应答，不调任何 Agent、不写库、不联网
    python roundtable.py --topic t --question q --members a,b,c --dry-run

退出码：0 会议跑完（converged / timeout / incomplete，都有结论条目）；
        1 调度器异常（已落 aborted）；2 参数或初始化错误（**没开会**，含缺 goal/agenda 的会前门禁）；
        3 会议中止（aborted，已落库并写明原因）；130 被中断。
"""

import argparse
import datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
import node_paths as _np  # L0 平台层：宿主默认值唯一出处（env 优先）
import node_profile as _npf  # L0 节点档案：宿主事实唯一真源（黑板基址 / NATS 中枢；env 优先）

# 批① 起被搬件按绝对包名 import，故把仓根一并放进搜索路径（本目录仍在最前）。
for _hacs_p in (os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                os.path.dirname(os.path.abspath(__file__))):
    sys.path.insert(0, _hacs_p)

VERSION = "1.2"

# 兜底默认值集中到 L0 node_paths（env 优先）
AGENT_TEAM_ETC_DIR = os.environ.get("AGENT_TEAM_ETC_DIR") or _np.etc_dir()

DEFAULT_MAX_ROUNDS = 2       # **每个议程项**的轮次上限（不是整场）
DEFAULT_MAX_TOTAL_ROUNDS = 0  # 整场总轮数上限，0 = 不额外限制（= 议程项数 × --max-rounds）
DEFECT_DUE_HOURS = 24        # 缺席缺陷单的闭环期限：次日
DEFAULT_DEADLINE_S = 900
DEFAULT_MAX_CHARS = 6000
# 兜底默认值集中到 L0 node_paths（env 优先）
DEFAULT_MODERATOR = os.environ.get("AGENT_TEAM_MODERATOR") or "hermes"
DEFAULT_TOPIC = "roundtable"   # 没给 --topic 时的缺省话题（会打一行 warn，不静默）
# 兜底默认值集中到 L0 node_paths（env 优先）
MODERATOR_FALLBACK = tuple(m.strip() for m in (os.environ.get("AGENT_TEAM_MODERATOR_FALLBACK") or "hermes,lobster,top001").split(",") if m.strip())

SNIPPET_MAX = 12             # 单次 prompt 最多塞几条黑板片段
SNIPPET_CHARS = 240          # 每条片段截断到这么长 —— 保证"绝不含完整历史发言"
CONTENT_MAX = 8000           # 单条发言正文入库上限
SIM_THRESHOLD = 0.85         # 收敛判据①的相似度阈值
SIM_RATIO = 2.0 / 3.0        # 收敛判据①的占比阈值
TOKENS_PER_MEMBER_ROUND = 4000
CHARS_PER_TOKEN = 2          # 粗估：字符数 / 2 ≈ token 数
ABSENT_STREAK_LIMIT = 2      # 连续几轮 absent 就把人移出本场
PING_TIMEOUT_S = 6.0
ASK_TIMEOUT_S = 180.0
HTTP_TIMEOUT_S = 15.0

STANCES = ("support", "oppose", "neutral", "need_info")
VOTES = ("continue", "converge")
REF_TYPES = ("entry", "file", "measurement", "session", "turn")
VERDICTS = ("agree", "object", "abstain", "absent")

AGENDA_TITLE_CHARS = 60      # 议程清单进 prompt 时每行截断到这么长
AGENDA_MAX_ITEMS = 20        # 单场议程项数上限（再多就不是一场会了）
CONCLUSION_CHARS = 600       # 结论进确认 prompt 时截断到这么长

# 会话里允许被 UPDATE 的列（与 sql/roundtable_schema.sql + roundtable_ops_schema.sql
# 的 roundtable_sessions_guard() 放行清单一一对应）。
MUTABLE_SESSION_COLUMNS = ("state", "round_no", "ended_at", "conclusion_entry", "note",
                           "confirmed_at", "confirm_summary",
                           # 场次级计量（sql/roundtable_ops_schema2.sql 加的 4 列，均为放行列）
                           "expected_count", "present_count", "absent_count",
                           "attendance_rate")
JSONB_SESSION_COLUMNS = ("confirm_summary",)
# 议程项里允许被 UPDATE 的列（与 roundtable_agenda_items_guard() 一一对应）。
MUTABLE_AGENDA_COLUMNS = ("conclusion", "conclusion_entry", "status", "note")
# 会议终态：incomplete 是本版新增的——「跑完了但没拿到有效落实事项」，不许混进 converged。
SESSION_STATES = ("running", "converged", "timeout", "aborted", "incomplete")


# ================================================================ 小工具

def now_utc() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def new_session_id() -> str:
    """rt-YYYYMMDD-HHMMSS-xxxx，尾巴 4 位取 uuid4，够防撞又能一眼看出时间。"""
    stamp = now_utc().strftime("%Y%m%d-%H%M%S")
    return "rt-%s-%s" % (stamp, uuid.uuid4().hex[:4])


def truncate(text: Any, limit: int) -> str:
    text = "" if text is None else str(text)
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def one_line(text: Any, limit: int) -> str:
    """压成一行再截断：片段进 prompt 前用，免得一条长正文把版面撑爆。"""
    flat = " ".join(str(text or "").split())
    return truncate(flat, limit)


def read_secret(path: str) -> str:
    """读凭据文件（只取内容去空白）。内容绝不打印、绝不进日志。"""
    with open(path, "r", encoding="utf-8-sig") as fh:
        return fh.read().strip()


def read_dsn(path: str) -> str:
    """从 systemd 环境文件里取 BB_PG_DSN。

    等价于：sed -n 's/^BB_PG_DSN=//p' <file> | head -1
    **绝不能 source 这个文件**：DSN 的值里带空格（host=... port=... dbname=...），
    shell 会在第一个空格处把它截断成错值 —— pg-blackboard-ops 技能里记过这个坑。
    """
    with open(path, "r", encoding="utf-8-sig") as fh:
        for line in fh:
            if line.startswith("BB_PG_DSN="):
                value = line[len("BB_PG_DSN="):].rstrip("\r\n").strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                    value = value[1:-1]
                return value
    return ""


# ================================================================ 解析：从回包里抠 JSON

_FENCE_RE = re.compile(r"```[ \t]*(?:json|JSON)?[ \t]*\r?\n(.*?)```", re.S)


def _balanced_objects(text: str) -> List[str]:
    """扫出文本里所有"括号平衡"的顶层 {...} 片段（字符串内的括号不计）。"""
    out = []  # type: List[str]
    depth = 0
    start = -1
    in_str = False
    escaped = False
    for i, ch in enumerate(text):
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    out.append(text[start:i + 1])
                    start = -1
    return out


def extract_json_object(raw: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """**只接受单个 JSON 对象**。返回 (对象, 错误原因)。

    依次尝试三条路：① ```json 代码块里的内容；② 整段直接解析；
    ③ 从脏文本里扫出第一个括号平衡的 {...}（前后有寒暄、有解释都能救回来）。
    """
    text = (raw or "").strip()
    if not text:
        return None, "回包为空"

    candidates = []  # type: List[str]
    for block in _FENCE_RE.findall(text):
        candidates.append(block.strip())
    candidates.append(text)
    candidates.extend(_balanced_objects(text))

    for cand in candidates:
        cand = cand.strip()
        if not cand.startswith("{"):
            continue
        try:
            obj = json.loads(cand)
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj, None
    return None, "回包里找不到单个合法 JSON 对象"


def normalize_refs(value: Any) -> List[Dict[str, Any]]:
    """把 refs 规整成 [{"type": ..., "value": ...}]。认不出来的元素直接丢掉。"""
    if not isinstance(value, list):
        return []
    out = []  # type: List[Dict[str, Any]]
    for item in value:
        if isinstance(item, dict):
            rtype = str(item.get("type") or "").strip().lower()
            rvalue = item.get("value")
            if rvalue is None:
                rvalue = item.get("entry_id")
            if rvalue is None or str(rvalue).strip() == "":
                continue
            if rtype not in REF_TYPES:
                rtype = "entry" if isinstance(rvalue, int) else "file"
            out.append({"type": rtype, "value": rvalue})
        elif isinstance(item, bool):
            continue
        elif isinstance(item, int):
            out.append({"type": "entry", "value": item})
        elif isinstance(item, str):
            token = item.strip()
            if not token:
                continue
            if token.lstrip("#").isdigit():
                out.append({"type": "entry", "value": int(token.lstrip("#"))})
            else:
                out.append({"type": "file", "value": token})
    return out


def parse_turn(raw: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """解析一条发言回包。返回 (规整后的字段, 错误原因)。

    错误分两种，调用方要分开处理：
      * "回包里找不到..."  → 整体解析失败，重试 1 次后降级（整段塞 content + refs 自引）
      * "refs 为空..."     → 格式对但没证据，重试 1 次后**不入库**，turn 记 error
    """
    obj, err = extract_json_object(raw)
    if obj is None:
        return None, err

    content = obj.get("content")
    if not isinstance(content, str) or not content.strip():
        return None, "content 为空或不是字符串"

    kind = str(obj.get("kind") or "opinion").strip().lower() or "opinion"

    stance = str(obj.get("stance") or "neutral").strip().lower()
    if stance not in STANCES:
        stance = "neutral"

    vote = str(obj.get("vote") or "continue").strip().lower()
    if vote not in VOTES:
        vote = "continue"

    confidence = obj.get("confidence")
    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        confidence = 0.5
    confidence = max(0.0, min(1.0, confidence))

    refs = normalize_refs(obj.get("refs"))
    turn = {"kind": kind, "content": truncate(content.strip(), CONTENT_MAX),
            "stance": stance, "vote": vote, "confidence": confidence, "refs": refs,
            "actions": obj.get("actions"), "issues": obj.get("issues")}
    if not refs:
        return turn, "refs 为空或缺失：没有证据的观点不入库"
    return turn, None


# ================================================================ 议程：会前门禁的解析

def parse_agenda(raw_items: Sequence[str]) -> Tuple[List[Dict[str, Any]], str]:
    """把 --agenda 的原始串解析成 [{"seq","title","expected"}]。返回 (议程, 错误原因)。

    格式：`"标题|预期产出; 标题|预期产出"`，也支持 --agenda 重复传入（多次累加）。
    分隔符中英文都认（; ；| ｜）。**标题为空即判非法** —— 会前门禁就是要在这里拦住
    「随便写点什么糊弄过去」的议程。预期产出没写就给一句兜底，但标题不许缺。
    """
    items = []  # type: List[Dict[str, Any]]
    for raw in raw_items or ():
        for chunk in re.split(r"[;；\n]", str(raw or "")):
            chunk = chunk.strip()
            if not chunk:
                continue
            parts = re.split(r"[|｜]", chunk, maxsplit=1)
            title = parts[0].strip()
            expected = parts[1].strip() if len(parts) > 1 else ""
            if not title:
                return [], "议程项 %r 的标题为空" % chunk
            items.append({"seq": len(items) + 1, "title": title,
                          "expected": expected or "给出该项的明确结论"})
            if len(items) > AGENDA_MAX_ITEMS:
                return [], "议程项超过 %d 条，拆成多场会开" % AGENDA_MAX_ITEMS
    if not items:
        return [], "议程为空：至少要有 1 项"
    return items, ""


# ================================================================ 当场确认

def parse_confirm(raw: str) -> Tuple[str, str, str]:
    """解析一条确认回包。返回 (verdict, note, 错误原因)。

    抠不出 JSON 也不硬判 agree —— **默认 abstain 并记原因**。
    「静默当通过」正是这次要根治的毛病，解析失败只能弃权，不能替人点头。
    """
    obj, err = extract_json_object(raw)
    if obj is None:
        return "abstain", "", err or "确认回包无法解析"
    verdict = str(obj.get("verdict") or "").strip().lower()
    note = str(obj.get("note") or "").strip()
    if verdict not in ("agree", "object", "abstain"):
        return "abstain", note, "verdict 取值非法（拿到 %r）" % (obj.get("verdict"),)
    return verdict, note, ""


def tally_confirmations(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """把若干条确认汇总成 confirm_summary。

    通过判据（硬）：**无 object**，且 agree 数 >= ⌊**在场人数**/2⌋+1。

    分母口径（<USER_ID>澄清后的最终版）：**在场人数 = 被点名且应答了的人**。
    absent（无应答）**不计入分母**，只单独记在 absent 名单里 —— 一个人没应答，
    不等于剩下的人就能替他决定，但也不该让"没人应答"这件事把门槛抬到不可能。
    缺席本身另有去处：出席率（分母是应到人数）+ 自动开 defect 单。

    字段：total=被点名人数（含 absent），present=计入分母的在场人数，required 由 present 算。
    """
    summary = {"agree": [], "object": [], "abstain": [], "absent": []}  # type: Dict[str, Any]
    for row in rows:
        verdict = row.get("verdict")
        if verdict in summary:
            summary[verdict].append(row.get("member"))
    total = len(rows)
    present = total - len(summary["absent"])
    required = present // 2 + 1 if present else 0
    summary["required"] = required
    summary["total"] = total
    summary["present"] = present
    summary["passed"] = (bool(present) and not summary["object"]
                         and len(summary["agree"]) >= required)
    return summary


# ================================================================ 落实事项：有人有期限

def parse_iso_datetime(value: Any) -> Optional[datetime.datetime]:
    """解析 ISO8601 时间串，统一成带时区的 UTC。认不出来返回 None。

    naive（不带时区）的一律当 UTC 处理 —— 全链路都用 UTC，不猜本地时区。
    """
    if isinstance(value, datetime.datetime):
        dt = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.datetime.fromisoformat(text)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc)


def validate_actions(value: Any, roster: Sequence[str], now: datetime.datetime,
                     agenda_seqs: Sequence[int] = ()) -> Tuple[List[Dict[str, Any]], List[str]]:
    """校验主持人给的 actions。返回 (合格的事项, 不合格的原因列表)。

    三条硬口径，一条不满足这条 action 就作废：
      ① what 非空；② owner 必须在 roster 里（不认"全体""大家""待定"）；
      ③ due_at 必须是 ISO8601 且**晚于当前时间**（已经过期的截止时间等于没有截止时间）。
    """
    roster_set = set(str(m).strip() for m in roster or () if str(m).strip())
    seq_set = set(int(s) for s in agenda_seqs or ())
    ok = []  # type: List[Dict[str, Any]]
    errors = []  # type: List[str]

    if not isinstance(value, list) or not value:
        return [], ["actions 缺失或不是非空数组"]

    for idx, item in enumerate(value, 1):
        if not isinstance(item, dict):
            errors.append("第 %d 条 action 不是对象" % idx)
            continue
        what = str(item.get("what") or "").strip()
        owner = str(item.get("owner") or "").strip()
        due_raw = item.get("due_at")
        reasons = []  # type: List[str]
        if not what:
            reasons.append("what 为空")
        if not owner:
            reasons.append("owner 为空")
        elif roster_set and owner not in roster_set:
            reasons.append("owner=%r 不在 roster 内（可选：%s）" % (owner, ",".join(sorted(roster_set))))
        due = parse_iso_datetime(due_raw)
        if due is None:
            reasons.append("due_at=%r 不是合法 ISO8601 时间" % (due_raw,))
        elif due <= now:
            reasons.append("due_at=%s 不晚于当前时间" % due.isoformat())
        if reasons:
            errors.append("第 %d 条 action：%s" % (idx, "；".join(reasons)))
            continue

        agenda_seq = item.get("agenda_seq")
        try:
            agenda_seq = int(agenda_seq)
        except (TypeError, ValueError):
            agenda_seq = None
        if agenda_seq is not None and seq_set and agenda_seq not in seq_set:
            agenda_seq = None      # 指了个不存在的议程项：不算错，挂空即可
        ok.append({"what": truncate(what, 500), "owner": owner, "due_at": due,
                   "agenda_seq": agenda_seq})
    return ok, errors


def parse_issues(value: Any) -> List[Dict[str, Any]]:
    """解析主持人主动登记的遗留问题（可选字段）。认不出来的直接丢。"""
    out = []  # type: List[Dict[str, Any]]
    if not isinstance(value, list):
        return out
    for item in value:
        if isinstance(item, str):
            title = item.strip()
            if title:
                out.append({"title": truncate(title, 300), "detail": "", "owner": None})
        elif isinstance(item, dict):
            title = str(item.get("title") or item.get("what") or "").strip()
            if not title:
                continue
            owner = str(item.get("owner") or "").strip() or None
            out.append({"title": truncate(title, 300),
                        "detail": truncate(item.get("detail") or "", 1000),
                        "owner": owner,
                        "due_at": parse_iso_datetime(item.get("due_at"))})
    return out


# ================================================================ 相似度（trgm 口径，不依赖 embedder）

_NON_WORD_RE = re.compile(r"[^0-9a-z一-鿿]+")


def _normalize(text: str) -> str:
    return _NON_WORD_RE.sub(" ", str(text or "").lower()).strip()


def _trigrams(text: str) -> set:
    """字符三元组集合 —— 与 pg_trgm 同一个思路，纯标准库实现。"""
    norm = _normalize(text)
    if not norm:
        return set()
    padded = "  " + norm + " "
    return set(padded[i:i + 3] for i in range(len(padded) - 2))


def similarity(a: str, b: str) -> float:
    """两段文本的相似度（0~1）。三元组不够就退回按词的 Jaccard。

    **刻意不依赖 embedder**：收敛判据不能押在一个可能加载失败的模型上。
    """
    ta, tb = _trigrams(a), _trigrams(b)
    if ta and tb:
        return len(ta & tb) / float(len(ta | tb))
    wa = set(_normalize(a).split())
    wb = set(_normalize(b).split())
    if not wa or not wb:
        return 1.0 if _normalize(a) == _normalize(b) else 0.0
    return len(wa & wb) / float(len(wa | wb))


def max_similarity(text: str, corpus: Sequence[str]) -> float:
    best = 0.0
    for other in corpus:
        score = similarity(text, other)
        if score > best:
            best = score
    return best


# ================================================================ 收敛判据

def repetition_ratio(new_texts: Sequence[str], prior_texts: Sequence[str]) -> float:
    """本轮新增条目里"跟既有条目高度重复"的占比。没有既有条目就是 0。"""
    if not new_texts or not prior_texts:
        return 0.0
    hits = sum(1 for t in new_texts if max_similarity(t, prior_texts) >= SIM_THRESHOLD)
    return hits / float(len(new_texts))


def check_convergence(round_no: int, turns: Sequence[Dict[str, Any]],
                      new_texts: Sequence[str],
                      prior_texts: Sequence[str]) -> Tuple[bool, str, str]:
    """判据①②③。返回 (是否收敛, 判据代号, 人话说明)。

    **round_no < 2 一律不许收敛** —— 一轮就"达成共识"多半是没人真读过题。
    """
    if round_no < 2:
        return False, "", "第 %d 轮：未满 2 轮，不允许判收敛" % round_no

    spoke = [t for t in turns if t.get("status") == "ok"]

    ratio = repetition_ratio(new_texts, prior_texts)
    if ratio >= SIM_RATIO:
        return True, "similarity", ("本轮 %d/%d 条与既有条目相似度 >= %.2f（占比 %.0f%%）"
                                    % (int(round(ratio * len(new_texts))), len(new_texts),
                                       SIM_THRESHOLD, ratio * 100))

    support = sum(1 for t in spoke if t.get("stance") == "support")
    against = sum(1 for t in spoke if t.get("stance") in ("oppose", "need_info"))
    if against == 0 and support >= 2:
        return True, "stance", "本轮无人反对/要资料，且 %d 人明确支持" % support

    converge_votes = sum(1 for t in spoke if t.get("vote") == "converge")
    need = len(spoke) // 2 + 1
    if spoke and converge_votes >= need:
        return True, "vote", "本轮 %d/%d 人投 converge（过半需 %d）" % (converge_votes, len(spoke), need)

    return False, "", "本轮未达成收敛（重复率 %.2f / 支持 %d / 反对+待资料 %d / converge %d）" % (
        ratio, support, against, converge_votes)


def check_hard_stop(round_no: int, max_rounds: int, elapsed_s: float, deadline_s: float,
                    chars_used: int, budget_chars: int) -> Tuple[bool, str]:
    """判据④：硬上限。返回 (是否该停, 原因)。"""
    if round_no >= max_rounds:
        return True, "达到最大轮次 %d" % max_rounds
    if elapsed_s >= deadline_s:
        return True, "超过 deadline %.0fs（已用 %.0fs）" % (deadline_s, elapsed_s)
    if budget_chars > 0 and chars_used >= budget_chars:
        return True, "超过字符预算 %d（已用 %d，约 %d token）" % (
            budget_chars, chars_used, chars_used // CHARS_PER_TOKEN)
    return False, ""


# ================================================================ prompt 渲染

OUTPUT_CONTRACT = (
    '只回一个 JSON 对象，前后不要任何多余文字（可以用 ```json 包起来）：\n'
    '{"kind":"opinion","content":"你的观点，200 字以内",'
    '"stance":"support|oppose|neutral|need_info","vote":"continue|converge",'
    '"confidence":0.0,"refs":[{"type":"entry","value":12}]}\n'
    'refs 必须至少一条，type 取 entry（上面片段的 # 号）/ file（文件路径）/ '
    'measurement（实测数据）——**refs 为空的发言不会被记录**。\n'
    'vote=converge 表示你认为讨论已经够了，continue 表示还需要再谈一轮。'
)

RETRY_HINT = "\n\n【上一次回复不合格】原因：%s。请**只**输出一个 JSON 对象，且 refs 至少一条。"

# 确认契约：故意只要三个取值，不给"基本同意""原则上支持"这类含糊选项。
CONFIRM_CONTRACT = (
    '只回一个 JSON 对象，前后不要任何多余文字：\n'
    '{"verdict":"agree|object|abstain","note":"一句话理由（反对必须写清反对什么）"}\n'
    'agree=当场确认这条结论；object=不同意，该项会被记为未确认并进遗留问题；'
    'abstain=弃权。**解析不出来一律按 abstain 处理，不会替你点头。**'
)

# 总体结论契约：actions 是硬要求 —— 没有责任人与截止时间的结论不算结论。
OVERALL_CONTRACT = (
    '\n【输出契约】只回一个 JSON 对象：\n'
    '{"kind":"decision","content":"总体结论 + 依据 + 还有什么没定",'
    '"stance":"neutral","vote":"converge","confidence":0.0,'
    '"refs":[{"type":"entry","value":12}],'
    '"actions":[{"what":"要做的事","owner":"成员名","due_at":"2026-01-31T18:00:00+08:00",'
    '"agenda_seq":1}],'
    '"issues":[{"title":"还没定的事","owner":"成员名","detail":"为什么没定"}]}\n'
    'actions **至少一条**，且每条都要满足：owner 必须是下面【本场成员】里的名字'
    '（不许写"全体""大家""待定"），due_at 必须是 ISO8601 且晚于现在。\n'
    'refs 必须至少一条。content 里要写清：结论是什么、基于哪几条证据、还有什么没定。'
)

ITEM_CONTRACT = (
    '\n【输出契约】只回一个 JSON 对象：\n'
    '{"kind":"decision","content":"本议程项的结论 + 依据","stance":"neutral",'
    '"vote":"converge","confidence":0.0,"refs":[{"type":"entry","value":12}]}\n'
    'content 只写**当前这一项**的结论，不要提前下别项的结论；refs 至少一条。'
)


def render_agenda_block(agenda: Sequence[Dict[str, Any]], current_seq: Optional[int],
                        title_chars: int = AGENDA_TITLE_CHARS) -> List[str]:
    """把完整议程清单渲染成若干行（当前项用 ▶ 标出来）。"""
    lines = []  # type: List[str]
    for item in agenda or ():
        mark = "▶" if current_seq is not None and item.get("seq") == current_seq else " "
        lines.append("%s %s. %s" % (mark, item.get("seq"), one_line(item.get("title"), title_chars)))
    return lines


def _fit(head: str, body: str, tail: str, max_chars: int) -> str:
    """头+体+尾拼起来，超了就硬截。契约在尾部，所以先砍的是体，这里只做最后兜底。"""
    prompt = head + body + tail
    if len(prompt) > max_chars:
        prompt = prompt[:max_chars]
    return prompt


def render_prompt(member: str, topic: str, question: str, round_no: int, max_rounds: int,
                  snippets: Sequence[Dict[str, Any]], max_chars: int,
                  retry_reason: str = "", goal: str = "",
                  agenda: Sequence[Dict[str, Any]] = (),
                  current_seq: Optional[int] = None) -> Tuple[str, int]:
    """渲染一次点名的 prompt。返回 (prompt, 实际放进去的片段数)。

    必带：会议目标、**完整议程清单**、当前在第几项、该项预期产出、黑板片段、输出契约。
    纪律：**这里绝不放任何成员的完整历史发言**。黑板片段一律 one_line + 截断到
    SNIPPET_CHARS，最多 SNIPPET_MAX 条；整体超 max_chars 就先砍片段（从最旧的开始），
    片段砍光还超就砍议程清单里**非当前项**的行，最后兜底硬截 —— 上限是死的。
    """
    current = None  # type: Optional[Dict[str, Any]]
    for item in agenda or ():
        if item.get("seq") == current_seq:
            current = item

    def build_head(agenda_rows: Sequence[Dict[str, Any]]) -> str:
        parts = ["你是本场圆桌会议的成员「%s」。" % member, "议题：%s" % topic]
        if goal:
            parts.append("【会议目标】%s" % one_line(goal, 300))
        parts.append("问题：%s" % question)
        if agenda_rows:
            parts.append("【议程】（共 %d 项，▶ 是当前这一项）" % len(agenda or ()))
            parts.extend(render_agenda_block(agenda_rows, current_seq))
        if current is not None:
            parts.append("【当前第 %s/%d 项】%s" % (current.get("seq"), len(agenda or ()),
                                                one_line(current.get("title"), 200)))
            parts.append("【该项预期产出】%s" % one_line(current.get("expected"), 200))
        parts.append("当前第 %d 轮（本项最多 %d 轮）。" % (round_no, max_rounds))
        return "\n".join(parts) + "\n"

    tail = "\n【输出契约】\n" + OUTPUT_CONTRACT
    if retry_reason:
        tail += RETRY_HINT % one_line(retry_reason, 120)

    rows = list(agenda or ())
    picked = list(snippets)[:SNIPPET_MAX]
    while True:
        if picked:
            lines = ["\n【黑板片段】（已截断，仅供引用，不是完整发言）"]
            for s in picked:
                lines.append("[#%s %s/%s] %s" % (
                    s.get("entry_id", "?"), s.get("author", "?"), s.get("kind", "?"),
                    one_line(s.get("content"), SNIPPET_CHARS)))
            body = "\n".join(lines) + "\n"
        else:
            body = "\n【黑板片段】暂无可引用的条目，请直接基于问题作答。\n"
        prompt = build_head(rows) + body + tail
        if len(prompt) <= max_chars:
            break
        if picked:
            picked = picked[1:]          # 先从最旧的一条片段开始砍
            continue
        trimmed = [r for r in rows if r.get("seq") == current_seq]
        if len(trimmed) < len(rows):
            rows = trimmed               # 片段砍光了还超：只留当前项
            continue
        break
    return _fit(build_head(rows), body, tail, max_chars), len(picked)


def render_conclusion_prompt(topic: str, question: str, rounds: int,
                             tally: Dict[str, int], snippets: Sequence[Dict[str, Any]],
                             max_chars: int, goal: str = "",
                             agenda: Sequence[Dict[str, Any]] = (),
                             item: Optional[Dict[str, Any]] = None,
                             item_results: Sequence[Dict[str, Any]] = (),
                             roster: Sequence[str] = (),
                             now_text: str = "",
                             retry_reason: str = "") -> Tuple[str, int]:
    """主持人出结论的 prompt。同样只放截断片段，不放完整发言。

    item 给了 = **该议程项**的结论（契约里不要 actions）；
    item 为 None = **总体结论**（契约里 actions 是硬要求，并附上本场成员名单与当前时间，
      免得主持人瞎编一个不在 roster 里的 owner 或一个已经过期的 due_at）。
    """
    def build_head(agenda_rows: Sequence[Dict[str, Any]]) -> str:
        parts = ["你是本场圆桌会议的主持人。", "议题：%s" % topic]
        if goal:
            parts.append("【会议目标】%s" % one_line(goal, 300))
        parts.append("问题：%s" % question)
        if agenda_rows:
            parts.append("【议程】（共 %d 项）" % len(agenda or ()))
            parts.extend(render_agenda_block(agenda_rows,
                                             item.get("seq") if item else None))
        if item is not None:
            parts.append("【现在只为第 %s 项出结论】%s" % (item.get("seq"),
                                                  one_line(item.get("title"), 200)))
            parts.append("【该项预期产出】%s" % one_line(item.get("expected"), 200))
        else:
            for res in item_results or ():
                parts.append("[第 %s 项 %s] %s" % (
                    res.get("seq"), res.get("status"),
                    one_line(res.get("conclusion") or "（无结论）", 160)))
        parts.append("本项/本场共进行了 %d 轮，立场统计：支持 %d / 反对 %d / 中立 %d / 待资料 %d。"
                     % (rounds, tally.get("support", 0), tally.get("oppose", 0),
                        tally.get("neutral", 0), tally.get("need_info", 0)))
        if item is None:
            parts.append("【本场成员】%s" % ",".join(roster or ()))
            if now_text:
                parts.append("【当前时间】%s（due_at 必须晚于它）" % now_text)
        return "\n".join(parts) + "\n"

    tail = ITEM_CONTRACT if item is not None else OVERALL_CONTRACT
    if retry_reason:
        tail += "\n【上一次结论不合格】原因：%s。请改正后重新输出。" % one_line(retry_reason, 300)

    rows = list(agenda or ())
    picked = list(snippets)[:SNIPPET_MAX]
    while True:
        if picked:
            lines = ["\n【本场黑板片段】（已截断）"]
            for s in picked:
                lines.append("[#%s %s] %s" % (s.get("entry_id", "?"), s.get("author", "?"),
                                              one_line(s.get("content"), SNIPPET_CHARS)))
            body = "\n".join(lines) + "\n"
        else:
            body = "\n【本场黑板片段】无\n"
        prompt = build_head(rows) + body + tail
        if len(prompt) <= max_chars:
            break
        if picked:
            picked = picked[1:]
            continue
        if rows:
            rows = []
            continue
        break
    return _fit(build_head(rows), body, tail, max_chars), len(picked)


def render_confirm_prompt(member: str, goal: str, target_label: str, conclusion: str,
                          max_chars: int, item: Optional[Dict[str, Any]] = None
                          ) -> str:
    """点名要一条「当场确认」。只放结论原文（截断）+ 确认契约，不放片段，省 token。"""
    parts = ["你是本场圆桌会议的成员「%s」，现在请对下面这条结论**当场表态**。" % member]
    if goal:
        parts.append("【会议目标】%s" % one_line(goal, 200))
    if item is not None:
        parts.append("【议程第 %s 项】%s" % (item.get("seq"), one_line(item.get("title"), 160)))
        parts.append("【该项预期产出】%s" % one_line(item.get("expected"), 160))
    parts.append("【待确认的结论：%s】" % target_label)
    head = "\n".join(parts) + "\n"
    tail = "\n【确认契约】\n" + CONFIRM_CONTRACT
    body = one_line(conclusion, CONCLUSION_CHARS) + "\n"
    room = max_chars - len(head) - len(tail)
    if room > 0 and len(body) > room:
        body = truncate(body, max(1, room - 1))
    return _fit(head, body, tail, max_chars)


# ================================================================ 黑板客户端（只走 9940 的公开接口）

class BlackboardClient(object):
    """黑板读写客户端。**写只走 POST /entries**，绝不直连数据库改黑板。"""

    def __init__(self, base_url: str, token_file: str, timeout: float = HTTP_TIMEOUT_S) -> None:
        self.base = (base_url or "").rstrip("/")
        self.timeout = timeout
        self._token = read_secret(token_file) if token_file else ""

    def _request(self, method: str, path: str, body: Optional[Dict[str, Any]] = None
                 ) -> Tuple[int, Dict[str, Any]]:
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json; charset=utf-8")
        if self._token:
            req.add_header("Authorization", "Bearer " + self._token)
        try:
            resp = urllib.request.urlopen(req, timeout=self.timeout)
            try:
                raw = resp.read().decode("utf-8", "replace")
                code = resp.getcode() or 200
            finally:
                resp.close()
        except urllib.error.HTTPError as exc:
            code = exc.code
            try:
                raw = exc.read().decode("utf-8", "replace")
            finally:
                exc.close()
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = {"ok": False, "error": "bad_json", "detail": truncate(raw, 200)}
        if not isinstance(payload, dict):
            payload = {"ok": False, "error": "bad_json"}
        return code, payload

    def post_entry(self, topic: str, kind: str, content: str,
                   refs: List[Dict[str, Any]]) -> Tuple[Optional[int], Optional[str]]:
        if not refs:
            return None, "refs 为空，按纪律不入库"
        try:
            code, payload = self._request("POST", "/entries", {
                "topic": topic, "kind": kind, "content": content, "refs": refs})
        except Exception as exc:
            return None, "写黑板失败：%s" % truncate(exc, 160)
        if code == 200 and payload.get("ok"):
            return payload.get("entry_id"), None
        return None, "黑板返回 %d：%s" % (code, truncate(payload.get("detail") or payload.get("error"), 160))

    def list_entries(self, topic: str, limit: int = 50) -> List[Dict[str, Any]]:
        query = urllib.parse.urlencode({"topic": topic, "limit": limit})
        try:
            code, payload = self._request("GET", "/entries?" + query)
        except Exception:
            return []
        if code != 200 or not payload.get("ok"):
            return []
        entries = payload.get("entries")
        return entries if isinstance(entries, list) else []


class NullBlackboard(object):
    """演练 / --dry-run 用：只记调用，不发任何网络请求，entry_id 给负数以示假。"""

    def __init__(self, seed_entries: Optional[List[Dict[str, Any]]] = None) -> None:
        self.posted = []  # type: List[Dict[str, Any]]
        self.entries = list(seed_entries or [])
        self._next = -1

    def post_entry(self, topic, kind, content, refs):
        # type: (str, str, str, List[Dict[str, Any]]) -> Tuple[Optional[int], Optional[str]]
        if not refs:
            return None, "refs 为空，按纪律不入库"
        entry_id = self._next
        self._next -= 1
        row = {"entry_id": entry_id, "topic": topic, "kind": kind,
               "content": content, "refs": refs, "author": "dry-run"}
        self.posted.append(row)
        self.entries.append(row)
        return entry_id, None

    def list_entries(self, topic, limit=50):
        # type: (str, int) -> List[Dict[str, Any]]
        rows = [e for e in self.entries if e.get("topic") == topic]
        return list(reversed(rows))[:limit]


# ================================================================ PG 存储

class PgStore(object):
    """会议状态与发言的落库层。只在真要写的时候才导入 psycopg2。"""

    def __init__(self, dsn: str) -> None:
        if not dsn:
            raise RuntimeError("DSN 为空：检查 --dsn-file 指向的文件里有没有 BB_PG_DSN=")
        import psycopg2  # noqa: F401  —— 唯一的第三方依赖，延迟导入
        self._driver = psycopg2
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

    def create_session(self, session: Dict[str, Any]) -> None:
        self._exec(
            "INSERT INTO roundtable_sessions "
            "(session_id, topic, question, moderator, members, max_rounds, round_no, state, "
            " goal, agenda) "
            "VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s::jsonb)",
            (session["session_id"], session["topic"], session["question"],
             session.get("moderator"), json.dumps(session["members"], ensure_ascii=False),
             session["max_rounds"], session.get("round_no", 0),
             session.get("state", "running"), session.get("goal"),
             json.dumps(session.get("agenda") or [], ensure_ascii=False)))

    def update_session(self, session_id: str, **fields: Any) -> None:
        """只允许改状态机列 —— 列名从白名单里取，永远不拼外部输入。"""
        sets, params = [], []  # type: List[str], List[Any]
        for column in MUTABLE_SESSION_COLUMNS:
            if column not in fields:
                continue
            value = fields[column]
            if column in JSONB_SESSION_COLUMNS:
                sets.append("%s = %%s::jsonb" % column)
                value = None if value is None else json.dumps(value, ensure_ascii=False)
            else:
                sets.append("%s = %%s" % column)
            params.append(value)
        if not sets:
            return
        params.append(session_id)
        self._exec("UPDATE roundtable_sessions SET " + ", ".join(sets) +
                   " WHERE session_id = %s", params)

    # ---------------------------------------------------------- 四要素：议程 / 确认 / 落实 / 遗留
    def add_agenda_items(self, session_id: str, items: Sequence[Dict[str, Any]]) -> None:
        for item in items or ():
            self._exec(
                "INSERT INTO roundtable_agenda_items (session_id, seq, title, expected, status) "
                "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (session_id, seq) DO NOTHING",
                (session_id, item["seq"], item["title"], item["expected"],
                 item.get("status", "pending")))

    def update_agenda_item(self, session_id: str, seq: int, **fields: Any) -> None:
        """只放行 conclusion / conclusion_entry / status / note（与库里的 guard 对齐）。"""
        sets, params = [], []  # type: List[str], List[Any]
        for column in MUTABLE_AGENDA_COLUMNS:
            if column in fields:
                sets.append("%s = %%s" % column)
                params.append(fields[column])
        if not sets:
            return
        params.extend([session_id, seq])
        self._exec("UPDATE roundtable_agenda_items SET " + ", ".join(sets) +
                   " WHERE session_id = %s AND seq = %s", params)

    def add_confirmation(self, row: Dict[str, Any]) -> Optional[int]:
        got = self._exec(
            "INSERT INTO roundtable_confirmations (session_id, target, member, verdict, note) "
            "VALUES (%s,%s,%s,%s,%s) RETURNING confirm_id",
            (row["session_id"], row["target"], row["member"], row["verdict"],
             row.get("note")), fetch="one")
        return got[0] if got else None

    def add_action(self, row: Dict[str, Any]) -> Optional[int]:
        got = self._exec(
            "INSERT INTO roundtable_actions "
            "(session_id, agenda_seq, what, owner, due_at, status, note) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING action_id",
            (row["session_id"], row.get("agenda_seq"), row["what"], row["owner"],
             row["due_at"], row.get("status", "open"), row.get("note")), fetch="one")
        return got[0] if got else None

    def add_issue(self, row: Dict[str, Any]) -> Optional[int]:
        """落一条遗留问题。kind/evidence 走 roundtable_ops_schema2.sql 加的两列。"""
        got = self._exec(
            "INSERT INTO roundtable_issues "
            "(session_id, title, detail, owner, status, due_at, next_check_at, refs, note, "
            " kind, evidence) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s::jsonb) RETURNING issue_id",
            (row["session_id"], row["title"], row.get("detail"), row.get("owner"),
             row.get("status", "open"), row.get("due_at"), row.get("next_check_at"),
             json.dumps(row.get("refs") or [], ensure_ascii=False), row.get("note"),
             row.get("kind", "issue"),
             json.dumps(row.get("evidence") or [], ensure_ascii=False)),
            fetch="one")
        return got[0] if got else None

    def add_turn(self, turn: Dict[str, Any]) -> Optional[int]:
        row = self._exec(
            "INSERT INTO roundtable_turns "
            "(session_id, round_no, seq, speaker, status, kind, stance, vote, confidence, "
            " content, refs, entry_id, error, elapsed_ms, prompt_chars) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s) RETURNING turn_id",
            (turn["session_id"], turn["round_no"], turn["seq"], turn["speaker"],
             turn["status"], turn.get("kind"), turn.get("stance"), turn.get("vote"),
             turn.get("confidence"), turn.get("content"),
             json.dumps(turn.get("refs") or [], ensure_ascii=False),
             turn.get("entry_id"), turn.get("error"), turn.get("elapsed_ms"),
             turn.get("prompt_chars")),
            fetch="one")
        return row[0] if row else None

    def load_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        row = self._exec(
            "SELECT session_id, topic, question, moderator, members, max_rounds, round_no, "
            "state, note, goal, agenda FROM roundtable_sessions WHERE session_id = %s",
            (session_id,), fetch="one")
        if not row:
            return None
        members = row[4]
        if isinstance(members, str):
            try:
                members = json.loads(members)
            except ValueError:
                members = []
        agenda = row[10]
        if isinstance(agenda, str):
            try:
                agenda = json.loads(agenda)
            except ValueError:
                agenda = []
        return {"session_id": row[0], "topic": row[1], "question": row[2],
                "moderator": row[3], "members": list(members or []), "max_rounds": row[5],
                "round_no": row[6], "state": row[7], "note": row[8],
                "goal": row[9], "agenda": list(agenda or [])}

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass


class NullStore(object):
    """演练 / --dry-run 用：不碰数据库，只把调用记在内存里（单测直接断言它）。"""

    def __init__(self) -> None:
        self.sessions = {}      # type: Dict[str, Dict[str, Any]]
        self.turns = []         # type: List[Dict[str, Any]]
        self.updates = []       # type: List[Tuple[str, Dict[str, Any]]]
        self.agenda_items = []  # type: List[Dict[str, Any]]
        self.confirmations = []  # type: List[Dict[str, Any]]
        self.actions = []       # type: List[Dict[str, Any]]
        self.issues = []        # type: List[Dict[str, Any]]
        self._next = 1
        self._next_aux = 1

    def create_session(self, session):
        # type: (Dict[str, Any]) -> None
        self.sessions[session["session_id"]] = dict(session)

    def update_session(self, session_id, **fields):
        # type: (str, Any) -> None
        bad = [k for k in fields if k not in MUTABLE_SESSION_COLUMNS]
        if bad:
            raise RuntimeError("试图改不可变列：%s" % ",".join(sorted(bad)))
        self.updates.append((session_id, dict(fields)))
        self.sessions.setdefault(session_id, {}).update(fields)

    # ---------------------------------------------------------- 四要素（内存版，单测直接断言）
    def add_agenda_items(self, session_id, items):
        # type: (str, Sequence[Dict[str, Any]]) -> None
        for item in items or ():
            self.agenda_items.append({"session_id": session_id, "seq": item["seq"],
                                      "title": item["title"], "expected": item["expected"],
                                      "status": item.get("status", "pending"),
                                      "conclusion": None, "conclusion_entry": None,
                                      "note": None})

    def update_agenda_item(self, session_id, seq, **fields):
        # type: (str, int, Any) -> None
        bad = [k for k in fields if k not in MUTABLE_AGENDA_COLUMNS]
        if bad:
            raise RuntimeError("试图改议程项的不可变列：%s" % ",".join(sorted(bad)))
        for row in self.agenda_items:
            if row["session_id"] == session_id and row["seq"] == seq:
                row.update(fields)

    def add_confirmation(self, row):
        # type: (Dict[str, Any]) -> int
        return self._append(self.confirmations, row)

    def add_action(self, row):
        # type: (Dict[str, Any]) -> int
        return self._append(self.actions, row)

    def add_issue(self, row):
        # type: (Dict[str, Any]) -> int
        return self._append(self.issues, row)

    def _append(self, bucket, row):
        # type: (List[Dict[str, Any]], Dict[str, Any]) -> int
        rid = self._next_aux
        self._next_aux += 1
        item = dict(row)
        item["id"] = rid
        bucket.append(item)
        return rid

    def add_turn(self, turn):
        # type: (Dict[str, Any]) -> int
        turn_id = self._next
        self._next += 1
        row = dict(turn)
        row["turn_id"] = turn_id
        self.turns.append(row)
        return turn_id

    def load_session(self, session_id):
        # type: (str) -> Optional[Dict[str, Any]]
        return self.sessions.get(session_id)

    def close(self):
        # type: () -> None
        return None


# ================================================================ 传输层：怎么把话送到成员那里

# 探针包：**故意不带 text 字段**。teamd 在调 A2A 之前就会因为"缺少必填字段 text"
# 立刻回一个 bad_request —— 拿到任何回包即证明对方 teamd 活着，且一个 token 都不烧。
PING_PAYLOAD = json.dumps({"from": "roundtable", "probe": "alive"},
                          ensure_ascii=False).encode("utf-8")


class NatsTransport(object):
    """走 NATS request/reply 点名（subject = a2a.v1.req.<member>）。复用 nats_client.py。"""

    name = "nats"

    def __init__(self, host: str, port: int, user: str, password_file: str,
                 ping_timeout: float = PING_TIMEOUT_S) -> None:
        from node.l1_transport.nats_client import NatsClient  # 纯标准库（批① 已搬到 node/l1_transport/）
        self._host, self._port = host, int(port)
        self._user = user
        self._password = read_secret(password_file) if password_file else ""
        self._ping_timeout = ping_timeout
        self._client = NatsClient(host=host, port=self._port, user=user,
                                  password=self._password, name="roundtable")
        self._opened = False

    def open(self) -> None:
        if not self._opened:
            self._client.connect()
            self._opened = True

    def ping(self, member: str) -> bool:
        try:
            self.open()
            reply = self._client.request("a2a.v1.req.%s" % member, PING_PAYLOAD,
                                         timeout=self._ping_timeout)
        except Exception:
            return False
        return reply is not None

    def ask(self, member: str, text: str, timeout: float) -> Tuple[str, str, str]:
        """返回 (status, 回复正文, 错误说明)。status ∈ ok|absent|error。"""
        envelope = {"from": "roundtable", "text": text, "timeout_s": float(timeout)}
        raw = json.dumps(envelope, ensure_ascii=False).encode("utf-8")
        try:
            self.open()
            reply = self._client.request("a2a.v1.req.%s" % member, raw, timeout=timeout + 15.0)
        except Exception as exc:
            return "error", "", "NATS 请求异常：%s" % truncate(exc, 160)
        if reply is None:
            return "absent", "", "点名超时（%.0fs 内无应答）" % timeout
        return _unwrap_reply(reply.decode("utf-8", "replace"))

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass


class A2ATransport(object):
    """走点对点 A2A HTTP（POST /a2a/<member>/messages）。<MEMBER_ID> 没有端点，只能走 NATS。"""

    name = "a2a"

    def __init__(self, endpoints: Dict[str, str], token_file: str,
                 ping_timeout: float = PING_TIMEOUT_S) -> None:
        self._endpoints = dict(endpoints or {})
        self._token = read_secret(token_file) if token_file else ""
        self._ping_timeout = ping_timeout

    def open(self) -> None:
        return None

    def ping(self, member: str) -> bool:
        base = self._endpoints.get(member)
        if not base:
            return False
        req = urllib.request.Request(base.rstrip("/") + "/healthz", method="GET")
        try:
            resp = urllib.request.urlopen(req, timeout=self._ping_timeout)
            try:
                return (resp.getcode() or 0) == 200
            finally:
                resp.close()
        except Exception:
            return False

    def ask(self, member: str, text: str, timeout: float) -> Tuple[str, str, str]:
        base = self._endpoints.get(member)
        if not base:
            return "absent", "", "没有 %s 的 A2A 端点（--a2a-endpoint 没给）" % member
        body = json.dumps({"text": text, "timeout_s": float(timeout)},
                          ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(base.rstrip("/") + "/a2a/%s/messages" % member,
                                     data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        if self._token:
            req.add_header("Authorization", "Bearer " + self._token)
        try:
            resp = urllib.request.urlopen(req, timeout=timeout + 15.0)
            try:
                raw = resp.read().decode("utf-8", "replace")
            finally:
                resp.close()
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", "replace")
            finally:
                exc.close()
            return "error", "", "A2A 返回 %d：%s" % (exc.code, truncate(detail, 160))
        except Exception as exc:
            return "error", "", "A2A 请求失败：%s" % truncate(exc, 160)
        return _unwrap_reply(raw)

    def close(self) -> None:
        return None


class DryRunTransport(object):
    """--dry-run 用：假应答，**不调任何 Agent、不写库、不联网**。

    默认按成员名轮流给出 support / oppose / neutral 三种合规回包，用来验解析与
    收敛逻辑；单测可以直接塞 scripted（member -> [回复文本, ...]）覆盖。

    **认 prompt 的类型**：看到确认契约就回 verdict，看到总体结论契约就回带
    actions/issues 的 decision —— 这样 --dry-run 能把「目标/议程/确认/落实」四条
    链路全走通，而不是走到确认那一步就散架。
    """

    name = "dry-run"

    def __init__(self, scripted: Optional[Dict[str, List[str]]] = None,
                 offline: Optional[Sequence[str]] = None,
                 verdicts: Optional[Dict[str, List[str]]] = None,
                 overall: Optional[List[str]] = None,
                 due_days: int = 7) -> None:
        self.scripted = dict(scripted or {})
        self.offline = set(offline or ())
        self.verdicts = dict(verdicts or {})   # member -> ["agree","object",...] 逐次取
        self.overall = list(overall or [])     # 总体结论的脚本回包（逐次取，空了用默认）
        self.due_days = int(due_days)
        self.asked = []  # type: List[Tuple[str, str]]
        self._round = {}  # type: Dict[str, int]

    def open(self):
        # type: () -> None
        return None

    def ping(self, member):
        # type: (str) -> bool
        return member not in self.offline

    def _canned(self, member: str) -> str:
        n = self._round.get(member, 0)
        stance = ("support", "neutral", "oppose")[n % 3]
        vote = "converge" if n >= 1 else "continue"
        return json.dumps({
            "kind": "opinion",
            "content": "（dry-run）%s 在第 %d 次发言里的看法：按现有证据，%s。" % (member, n + 1, stance),
            "stance": stance, "vote": vote, "confidence": 0.6,
            "refs": [{"type": "measurement", "value": "dry-run-%s-%d" % (member, n + 1)}],
        }, ensure_ascii=False)

    def _canned_confirm(self, member: str) -> str:
        queue = self.verdicts.get(member)
        verdict = queue.pop(0) if queue else "agree"
        return json.dumps({"verdict": verdict,
                           "note": "（dry-run）%s 的当场表态" % member}, ensure_ascii=False)

    @staticmethod
    def _roster_from_prompt(text: str) -> List[str]:
        """从总体结论 prompt 的【本场成员】行里抠出 roster —— 假主持人也得给合法 owner。"""
        match = re.search(r"【本场成员】(.*)", text or "")
        if not match:
            return []
        return [m.strip() for m in match.group(1).split(",") if m.strip()]

    def _canned_overall(self, member: str, text: str) -> str:
        if self.overall:
            return self.overall.pop(0)
        roster = self._roster_from_prompt(text) or [member]
        due = now_utc() + datetime.timedelta(days=max(1, self.due_days))
        return json.dumps({
            "kind": "decision",
            "content": "（dry-run）总体结论：按各议程项的结论执行，责任人与截止时间见 actions。",
            "stance": "neutral", "vote": "converge", "confidence": 0.7,
            "refs": [{"type": "measurement", "value": "dry-run-overall"}],
            "actions": [{"what": "把本场结论落到文档并回帖", "owner": roster[0],
                         "due_at": due.isoformat(), "agenda_seq": 1}],
            "issues": [{"title": "（dry-run）本场没定的事，留到下一场跟踪",
                        "owner": roster[-1], "detail": "dry-run 自动登记"}],
        }, ensure_ascii=False)

    @staticmethod
    def _canned_item() -> str:
        return json.dumps({
            "kind": "decision",
            "content": "（dry-run）本议程项的结论：按本项发言里的多数意见执行。",
            "stance": "neutral", "vote": "converge", "confidence": 0.7,
            "refs": [{"type": "measurement", "value": "dry-run-item"}],
        }, ensure_ascii=False)

    def ask(self, member, text, timeout):
        # type: (str, str, float) -> Tuple[str, str, str]
        self.asked.append((member, text))
        if member in self.offline:
            return "absent", "", "dry-run：该成员被标记为离线"
        # 先认 prompt 类型：scripted 只喂**发言**，别让确认/结论把发言脚本吃掉
        if "【确认契约】" in text:
            return "ok", self._canned_confirm(member), ""
        if "【本场成员】" in text:
            return "ok", self._canned_overall(member, text), ""
        if "【现在只为第" in text:
            return "ok", self._canned_item(), ""
        queue = self.scripted.get(member)
        if queue:
            reply = queue.pop(0)
            self._round[member] = self._round.get(member, 0) + 1
            return "ok", reply, ""
        reply = self._canned(member)
        self._round[member] = self._round.get(member, 0) + 1
        return "ok", reply, ""

    def close(self):
        # type: () -> None
        return None


def _unwrap_reply(raw: str) -> Tuple[str, str, str]:
    """把 teamd / a2a_svc 的回包剥成 (status, 正文, 错误)。不是 JSON 就当纯文本用。"""
    text = (raw or "").strip()
    if not text:
        return "error", "", "对方回了空包"
    try:
        obj = json.loads(text)
    except ValueError:
        return "ok", text, ""
    if not isinstance(obj, dict):
        return "ok", text, ""
    if obj.get("ok") and obj.get("reply") is not None:
        return "ok", str(obj.get("reply")), ""
    if obj.get("ok"):
        return "ok", text, ""
    return "error", "", "对方回了错误：%s/%s" % (obj.get("error"), truncate(obj.get("detail"), 120))


# ================================================================ 调度器

class Roundtable(object):
    """一场会议的完整跑动。跑完即退，不常驻。"""

    def __init__(self, cfg: Dict[str, Any], store: Any, blackboard: Any, transport: Any,
                 out: Optional[Callable[[str], None]] = None) -> None:
        self.cfg = cfg
        self.store = store
        self.bb = blackboard
        self.transport = transport
        self._out = out or (lambda line: sys.stdout.write(line + "\n"))

        self.session_id = cfg["session_id"]
        self.topic = cfg["topic"]
        self.question = cfg["question"]
        self.members = list(cfg["members"])
        self.active = list(cfg["members"])
        self.max_rounds = int(cfg["max_rounds"])
        # 整场总轮数上限（安全阀）：0 = 不额外限制。--max-rounds 仍旧按**每个议程项**计，
        # 这个只防"议程项太多导致整场长跑失控"，触发后剩余议程项一律标 deferred。
        self.max_total_rounds = int(cfg.get("max_total_rounds") or 0)
        self.deadline_s = float(cfg["deadline"])
        self.max_chars = int(cfg["max_chars"])
        self.moderator = cfg.get("moderator") or DEFAULT_MODERATOR

        # 四要素：目标 + 议程。cfg 里没给议程时按 question 兜一项 —— **会前门禁在 main()**，
        # 这里兜底是为了让"只想跑一段逻辑"的调用方（含单测）不至于拿不到议程就崩。
        self.goal = str(cfg.get("goal") or "").strip()
        agenda = list(cfg.get("agenda") or [])
        if not agenda:
            agenda = [{"seq": 1, "title": one_line(self.question, 120),
                       "expected": "给出该问题的明确结论"}]
        self.agenda = agenda
        self.agenda_seqs = [int(i["seq"]) for i in self.agenda]

        self.round_no = int(cfg.get("start_round", 0))
        self.state = "running"
        self.note = ""
        self.conclusion_entry = None  # type: Optional[int]
        self.chars_used = 0
        self.absent_streak = {}       # type: Dict[str, int]
        self.spoken = {}              # type: Dict[str, int]
        self.session_texts = []       # type: List[str]
        self.cited_entries = []       # type: List[int]
        self.tally = {"support": 0, "oppose": 0, "neutral": 0, "need_info": 0}
        self.started = time.time()
        self.prompt_log = cfg.get("prompt_log")
        self.budget_chars = (len(self.members) * self.max_rounds * len(self.agenda)
                             * TOKENS_PER_MEMBER_ROUND * CHARS_PER_TOKEN)

        # 每项议程的结果：{"seq","title","status","conclusion","entry_id","confirm"}
        self.item_results = []        # type: List[Dict[str, Any]]
        self.confirm_rows = []        # type: List[Dict[str, Any]]
        self.action_rows = []         # type: List[Dict[str, Any]]
        self.issue_rows = []          # type: List[Dict[str, Any]]
        self.overall_confirm = {}     # type: Dict[str, Any]
        self.absent_members = set()   # type: set
        # 出席计量：responded = 本场**至少应答过一次点名**的成员（= 在场），
        # absent_log = 每个人的缺席流水（次数 / 轮次 / 失败原因），缺陷单的证据就从这儿来。
        self.responded = set()        # type: set
        self.absent_log = {}          # type: Dict[str, Dict[str, Any]]

    # ---------------------------------------------------------- 输出
    def say(self, line: str) -> None:
        self._out(line)

    def _log_prompt(self, member: str, prompt: str) -> None:
        if not self.prompt_log:
            return
        try:
            with open(self.prompt_log, "a", encoding="utf-8") as fh:
                fh.write("\n===== %s | 第 %d 轮 | %s | %d 字符 =====\n%s\n"
                         % (self.session_id, self.round_no, member, len(prompt), prompt))
        except OSError as exc:
            self.say("[warn] prompt 日志写不动（%s），继续跑" % truncate(exc, 120))

    # ---------------------------------------------------------- 主流程
    def run(self) -> int:
        try:
            return self._run()
        except KeyboardInterrupt:
            self._abort("收到中断（Ctrl-C）")
            return 130
        except Exception as exc:                 # 任何意外都不许留 running 僵尸
            self._abort("调度器异常：%s: %s" % (exc.__class__.__name__, truncate(exc, 160)))
            return 1
        finally:
            try:
                self.transport.close()
            except Exception:
                pass
            try:
                self.store.close()
            except Exception:
                pass

    def _run(self) -> int:
        if self.round_no == 0:
            self.store.create_session({
                "session_id": self.session_id, "topic": self.topic,
                "question": self.question, "moderator": self.moderator,
                "members": self.members, "max_rounds": self.max_rounds,
                "round_no": 0, "state": "running",
                "goal": self.goal, "agenda": self.agenda})
            self.store.add_agenda_items(self.session_id, self.agenda)
        self.say("[会议] %s 议题=%s 成员=%s 主持=%s 议程 %d 项 / 每项上限 %d 轮 / %.0fs / %d 字符预算"
                 % (self.session_id, self.topic, ",".join(self.members), self.moderator,
                    len(self.agenda), self.max_rounds, self.deadline_s, self.budget_chars))
        self.say("[目标] %s" % (self.goal or "（未给）"))
        for item in self.agenda:
            self.say("[议程] %s. %s —— 预期产出：%s"
                     % (item["seq"], item["title"], item["expected"]))

        if self.max_total_rounds:
            self.say("[安全阀] 整场总轮数上限 %d（--max-rounds 仍按每个议程项计）"
                     % self.max_total_rounds)

        # ---- 会中：逐项推进，每项自己走轮次、出结论、当场确认
        for item in self.agenda:
            if self._total_rounds_hit():
                self._defer_item(item, "整场已跑满 %d 轮（--max-total-rounds）"
                                       % self.max_total_rounds)
                continue
            rc = self._run_item(item)
            if rc is not None:
                return rc

        # ---- 会末：总体结论 + 落实事项 + 总体确认
        self._conclude_overall()
        self._flush_issues()
        self._summary()
        return 0

    # ---------------------------------------------------------- 安全阀：整场总轮数
    def _total_rounds_hit(self) -> bool:
        """整场总轮数是否已经撞上限。0 = 不额外限制，永远返回 False。"""
        return bool(self.max_total_rounds) and self.round_no >= self.max_total_rounds

    def _defer_item(self, item: Dict[str, Any], reason: str) -> None:
        """没来得及谈的议程项：标 deferred + 开一条遗留问题，**绝不悄悄跳过**。"""
        seq = item["seq"]
        note = "未进入讨论：%s" % reason
        self.say("[warn] 议程 %s「%s」%s → 标 deferred" % (seq, item["title"], note))
        self.store.update_agenda_item(self.session_id, seq, status="deferred", note=note)
        self.item_results.append({"seq": seq, "title": item["title"], "status": "deferred",
                                  "conclusion": "", "entry_id": None,
                                  "confirm": {"agree": [], "object": []}, "rounds": 0})
        self._record_issue(
            title="议程第 %s 项「%s」未进入讨论" % (seq, one_line(item["title"], 80)),
            detail=note + "；预期产出：" + one_line(item["expected"], 200),
            owner=self.moderator,
            refs=[{"type": "session", "value": self.session_id}])
        if self.state == "running":
            self.state = "timeout"
            self.note = (self.note + "；" if self.note else "") + note

    # ---------------------------------------------------------- 一个议程项
    def _run_item(self, item: Dict[str, Any]) -> Optional[int]:
        """跑完一个议程项。正常返回 None；需要整场中止时返回退出码。"""
        seq = item["seq"]
        self.say("[议程 %s] 开始：%s（预期产出：%s）" % (seq, item["title"], item["expected"]))
        item_round = 0
        item_state = "running"
        detail = ""
        item_texts = []  # type: List[str]

        while True:
            if item_round and self._total_rounds_hit():
                item_state = "timeout"
                detail = "整场已跑满 %d 轮（--max-total-rounds）" % self.max_total_rounds
                self.say("[warn] 议程 %s 收尾：%s" % (seq, detail))
                break
            stop, reason = check_hard_stop(item_round, self.max_rounds,
                                           time.time() - self.started, self.deadline_s,
                                           self.chars_used, self.budget_chars)
            if stop:
                item_state = "timeout"
                detail = "未自然收敛：" + reason
                self.say("[议程 %s] 收尾：%s" % (seq, detail))
                break

            item_round += 1
            self.round_no += 1
            prior_texts = list(item_texts)
            turns, new_texts = self._run_round(item)
            item_texts.extend(new_texts)
            self.store.update_session(self.session_id, round_no=self.round_no)

            spoke = [t for t in turns if t.get("status") == "ok"]
            self.say("[议程 %s 轮 %d] 发言 %d 人 / 缺席 %d 人 / 异常 %d 人"
                     % (seq, item_round, len(spoke),
                        sum(1 for t in turns if t.get("status") == "absent"),
                        sum(1 for t in turns if t.get("status") == "error")))

            self._drop_chronic_absentees()

            if len(spoke) < 2:
                self._abort("本轮实际发言人数 %d < 2，会议无法继续" % len(spoke))
                return 3
            if len(self.active) < 2:
                self._abort("在场成员只剩 %d 人，会议无法继续" % len(self.active))
                return 3

            converged, rule, why = check_convergence(item_round, turns,
                                                     new_texts, prior_texts)
            self.say("[判据] %s" % why)
            if converged:
                item_state = "converged"
                detail = "收敛判据 %s：%s" % (rule, why)
                break

        if item_state == "timeout" and self.state == "running":
            self.state = "timeout"          # 有一项没自然收敛，整场就不算 converged
            self.note = self.note or detail
        elif item_state == "converged" and self.state == "running":
            self.note = self.note or detail

        self._conclude_item(item, item_round, item_state, detail)
        return None

    # ---------------------------------------------------------- 一轮
    def _run_round(self, item: Optional[Dict[str, Any]] = None
                   ) -> Tuple[List[Dict[str, Any]], List[str]]:
        turns = []      # type: List[Dict[str, Any]]
        new_texts = []  # type: List[str]
        seq = 0
        for member in list(self.active):
            seq += 1
            turn = self._run_turn(member, seq, item)
            turns.append(turn)
            if turn.get("status") == "ok" and turn.get("content"):
                new_texts.append(turn["content"])
                self.session_texts.append(turn["content"])
        return turns, new_texts

    def _run_turn(self, member: str, seq: int,
                  item: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        started = time.time()
        base = {"session_id": self.session_id, "round_no": self.round_no, "seq": seq,
                "speaker": member, "refs": [], "prompt_chars": 0}
        current_seq = item.get("seq") if item else None

        if not self.transport.ping(member):
            self.absent_streak[member] = self.absent_streak.get(member, 0) + 1
            self._mark_absent(member, "点名探针无应答，判定离线", "第 %d 轮发言" % self.round_no)
            self.say("[轮 %d] %s 离线（连续 %d 轮），跳过"
                     % (self.round_no, member, self.absent_streak[member]))
            turn = dict(base, status="absent", error="点名探针无应答，判定离线",
                        elapsed_ms=int((time.time() - started) * 1000))
            self.store.add_turn(turn)
            return turn

        snippets = self._snippets()
        retry_reason = ""
        parsed = None      # type: Optional[Dict[str, Any]]
        raw_reply = ""
        last_error = ""
        prompt_chars = 0
        for attempt in (1, 2):
            prompt, used = render_prompt(member, self.topic, self.question, self.round_no,
                                         self.max_rounds, snippets, self.max_chars,
                                         retry_reason, goal=self.goal, agenda=self.agenda,
                                         current_seq=current_seq)
            prompt_chars = len(prompt)
            self.chars_used += prompt_chars
            self._log_prompt(member, prompt)
            self.say("[轮 %d] %s 第 %d 次点名：prompt %d 字符 / 片段 %d 条"
                     % (self.round_no, member, attempt, prompt_chars, used))

            status, reply, error = self.transport.ask(member, prompt, ASK_TIMEOUT_S)
            self.chars_used += len(reply or "")
            if status != "ok":
                last_error = error or "对方无有效应答"
                if status == "absent":
                    self.absent_streak[member] = self.absent_streak.get(member, 0) + 1
                    self._mark_absent(member, last_error, "第 %d 轮发言" % self.round_no)
                    turn = dict(base, status="absent", error=last_error,
                                prompt_chars=prompt_chars,
                                elapsed_ms=int((time.time() - started) * 1000))
                    self.store.add_turn(turn)
                    return turn
                break

            self.absent_streak[member] = 0
            self._mark_present(member)      # 应答了就算在场，哪怕这条回包格式不合规
            raw_reply = reply
            parsed, err = parse_turn(reply)
            if parsed is not None and not err:
                break
            last_error = err or "解析失败"
            retry_reason = last_error
            if attempt == 2:
                break

        elapsed_ms = int((time.time() - started) * 1000)

        # ① 正常发言：refs 非空 → 写黑板 → 写 turn
        if parsed is not None and parsed.get("refs"):
            entry_id, post_err = self.bb.post_entry(self.topic, parsed["kind"],
                                                    parsed["content"], parsed["refs"])
            turn = dict(base, status="ok" if entry_id is not None else "error",
                        kind=parsed["kind"], stance=parsed["stance"], vote=parsed["vote"],
                        confidence=parsed["confidence"], content=parsed["content"],
                        refs=parsed["refs"], entry_id=entry_id, error=post_err,
                        prompt_chars=prompt_chars, elapsed_ms=elapsed_ms)
            self.store.add_turn(turn)
            if entry_id is not None:
                self.spoken[member] = self.spoken.get(member, 0) + 1
                self.tally[parsed["stance"]] = self.tally.get(parsed["stance"], 0) + 1
                self.cited_entries.append(entry_id)
                for ref in parsed["refs"]:
                    if ref.get("type") == "entry" and isinstance(ref.get("value"), int):
                        self.cited_entries.append(ref["value"])
            else:
                self.say("[轮 %d] %s 写黑板失败：%s" % (self.round_no, member, post_err))
            return turn

        # ② 格式对但 refs 空 → **整条不入库**，turn 记 error
        if parsed is not None:
            self.say("[轮 %d] %s refs 为空，按纪律不入库" % (self.round_no, member))
            turn = dict(base, status="error", kind=parsed.get("kind"),
                        stance=parsed.get("stance"), vote=parsed.get("vote"),
                        confidence=parsed.get("confidence"), content=None,
                        error=last_error or "refs 为空或缺失：没有证据的观点不入库",
                        prompt_chars=prompt_chars, elapsed_ms=elapsed_ms)
            self.store.add_turn(turn)
            return turn

        # ③ 对方压根没给出可用回包（网络/上游错误）
        if not raw_reply:
            turn = dict(base, status="error", error=last_error or "无有效应答",
                        prompt_chars=prompt_chars, elapsed_ms=elapsed_ms)
            self.store.add_turn(turn)
            return turn

        # ④ 整体解析失败 → 降级：整段塞 content + refs 自引该 turn，照常入库
        self_ref = [{"type": "turn",
                     "value": "%s#r%d-s%d" % (self.session_id, self.round_no, seq)}]
        content = truncate(raw_reply.strip(), CONTENT_MAX)
        entry_id, post_err = self.bb.post_entry(self.topic, "opinion", content, self_ref)
        reason = "parse_degraded：%s（已整段留痕，refs 自引）" % (last_error or "解析失败")
        if post_err:
            reason += "；写黑板失败：%s" % post_err
        self.say("[轮 %d] %s %s" % (self.round_no, member, reason))
        turn = dict(base, status="ok" if entry_id is not None else "error",
                    kind="opinion", stance="neutral", vote="continue", confidence=0.3,
                    content=content, refs=self_ref, entry_id=entry_id, error=reason,
                    prompt_chars=prompt_chars, elapsed_ms=elapsed_ms)
        self.store.add_turn(turn)
        if entry_id is not None:
            self.spoken[member] = self.spoken.get(member, 0) + 1
            self.tally["neutral"] = self.tally.get("neutral", 0) + 1
            self.cited_entries.append(entry_id)
        return turn

    # ---------------------------------------------------------- 辅助
    def _snippets(self) -> List[Dict[str, Any]]:
        try:
            return self.bb.list_entries(self.topic, SNIPPET_MAX * 2)[:SNIPPET_MAX]
        except Exception as exc:
            self.say("[warn] 取黑板片段失败（%s），本轮不带片段" % truncate(exc, 120))
            return []

    def _drop_chronic_absentees(self) -> None:
        """连续 ABSENT_STREAK_LIMIT 轮缺席 → 移出本场，并在黑板上留一条 system 说明。"""
        for member in list(self.active):
            if self.absent_streak.get(member, 0) < ABSENT_STREAK_LIMIT:
                continue
            self.active.remove(member)
            note = ("成员 %s 连续 %d 轮点名无应答，已移出会议 %s 的本场成员集。"
                    % (member, self.absent_streak[member], self.session_id))
            self.say("[成员] %s" % note)
            refs = [{"type": "session", "value": self.session_id}]
            entry_id, err = self.bb.post_entry(self.topic, "system", note, refs)
            if err:
                self.say("[warn] system 条目写失败：%s" % err)
            elif entry_id is not None:
                self.cited_entries.append(entry_id)

    def _pick_moderator(self) -> Optional[str]:
        """在线的优先：--moderator → <MEMBER_ID_1> → <MEMBER_ID_2> → <MEMBER_ID_3>。全离线返回 None。"""
        chain = [self.moderator] + [m for m in MODERATOR_FALLBACK if m != self.moderator]
        for candidate in chain:
            try:
                if self.transport.ping(candidate):
                    return candidate
            except Exception:
                continue
        return None

    def _speakers(self) -> List[str]:
        """本场**实际发言过**的成员（有过至少一条入库发言）。确认就找这些人。"""
        return [m for m in self.members if self.spoken.get(m, 0) > 0]

    # ---------------------------------------------------------- 出席计量
    def _mark_present(self, member: str) -> None:
        """记一次「应答了点名」。在场 = 至少应答过一次 —— 之后再缺席也不改这个事实。"""
        self.responded.add(member)

    def _mark_absent(self, member: str, reason: str, where: str) -> None:
        """记一次「点名无应答」。同一成员多次缺席合并计数，明细进缺陷单的 evidence。"""
        self.absent_members.add(member)
        log = self.absent_log.get(member)
        if log is None:
            log = {"count": 0, "where": [], "reasons": [],
                   "round_no": self.round_no, "ts": now_utc().isoformat()}
            self.absent_log[member] = log
        log["count"] += 1
        log["where"].append(where)
        text = one_line(reason or "点名无应答", 120)
        if text not in log["reasons"]:
            log["reasons"].append(text)

    def attendance(self) -> Dict[str, Any]:
        """本场出席计量。

        **出席率的分母是「应到人数」**（sessions.members 全集），不是在场人数 ——
        这跟"确认通过"的分母（在场人数）刻意不是同一个，别再混到一起。
        """
        expected = len(self.members)
        present_members = [m for m in self.members if m in self.responded]
        absent_members = [m for m in self.members if m not in self.responded]
        rate = (float(len(present_members)) / expected) if expected else None
        return {"expected": expected, "present": len(present_members),
                "absent": len(absent_members), "rate": rate,
                "present_members": present_members, "absent_members": absent_members}

    def attendance_line(self) -> str:
        """人话版的出席率：`出席率 2/3 = 66.7%`（+ 缺席名单）。"""
        att = self.attendance()
        text = "出席率 %d/%d = %s" % (
            att["present"], att["expected"],
            "-" if att["rate"] is None else ("%.1f%%" % (att["rate"] * 100)))
        if att["absent_members"]:
            text += " 缺席[%s]" % ",".join(att["absent_members"])
        return text

    def _open_absence_defects(self) -> None:
        """缺席自动开**缺陷单**：成员都是常驻 Agent，点名无应答就是疑似程序缺陷。

        同一成员同一场会**只开一条**，多次缺席合并计数写进 evidence；
        due_at = 24h 后（次日闭环要求），owner 就是缺席的那个成员本人。
        """
        if getattr(self, "_defects_opened", False):
            return
        self._defects_opened = True
        due = now_utc() + datetime.timedelta(hours=DEFECT_DUE_HOURS)
        for member in self.members:
            log = self.absent_log.get(member)
            if not log:
                continue
            reason = "；".join(log["reasons"]) or "点名无应答"
            evidence = [{"session_id": self.session_id,
                         "round_no": log["round_no"],
                         "点名次数": log["count"],
                         "失败原因": reason,
                         "环节": list(log["where"]),
                         "ts": log["ts"]}]
            self._record_issue(
                title="成员 %s 点名无应答（疑似程序缺陷）" % member,
                detail=("成员是云主机 / 常驻 Agent，设计上不允许不在线。本场共 %d 次点名"
                        "拿不到应答（%s）；失败原因：%s。请在 24 小时内查清并闭环。"
                        % (log["count"], "、".join(log["where"][:6]), reason)),
                owner=member, due_at=due, kind="defect", evidence=evidence,
                refs=[{"type": "session", "value": self.session_id}])
            self.say("[缺陷] 自动开单：成员 %s 点名无应答 %d 次 → defect（截止 %s）"
                     % (member, log["count"], due.isoformat()))

    def _ask_moderator(self, moderator: str, prompt: str, label: str) -> str:
        """向主持人要一段文本。返回正文（拿不到就返回空串）。"""
        self.chars_used += len(prompt)
        self._log_prompt(moderator + "(" + label + ")", prompt)
        status, reply, error = self.transport.ask(moderator, prompt, ASK_TIMEOUT_S)
        self.chars_used += len(reply or "")
        if status == "ok" and reply:
            return reply
        self.say("[%s] 主持人没给出回复（%s）" % (label, truncate(error, 120)))
        return ""

    # ---------------------------------------------------------- 议程项结论 + 当场确认
    def _conclude_item(self, item: Dict[str, Any], rounds: int,
                       item_state: str, detail: str) -> None:
        """该项结论 → 黑板 decision 条目 → 当场确认 → 回写 agenda_items。"""
        seq = item["seq"]
        moderator = self._pick_moderator()
        snippets = self._snippets()
        text = ""
        if moderator:
            prompt, used = render_conclusion_prompt(
                self.topic, self.question, rounds, self.tally, snippets, self.max_chars,
                goal=self.goal, agenda=self.agenda, item=item)
            self.say("[议程 %s 结论] 主持人 %s：prompt %d 字符 / 片段 %d 条"
                     % (seq, moderator, len(prompt), used))
            reply = self._ask_moderator(moderator, prompt, "议程 %s 结论" % seq)
            if reply:
                parsed, _err = parse_turn(reply)
                text = parsed["content"] if parsed else truncate(reply.strip(), CONTENT_MAX)
        else:
            self.say("[议程 %s 结论] 主持人全员离线，取本项最后一条有效发言" % seq)

        if not text:
            text = self.session_texts[-1] if self.session_texts else "本项没有产生任何有效发言。"
            text = "（主持人缺席，取最后一条有效发言作为本项结论）\n" + text
        if item_state == "timeout":
            text = "【本项未自然收敛】%s\n%s" % (detail, text)

        body = "【议程 %s：%s】预期产出：%s\n%s" % (seq, item["title"], item["expected"], text)
        refs = [{"type": "entry", "value": eid} for eid in _dedup(self.cited_entries)]
        if not refs:
            refs = [{"type": "session", "value": self.session_id}]
        entry_id, err = self.bb.post_entry(self.topic, "decision",
                                           truncate(body, CONTENT_MAX), refs)
        if err:
            self.say("[议程 %s 结论] 写黑板失败：%s" % (seq, err))
        elif entry_id is not None:
            self.cited_entries.append(entry_id)

        # ---- 当场确认（硬）：无 object 且 agree 过半才算通过
        summary = self._collect_confirmations("agenda:%s" % seq, text, item)
        if summary["passed"]:
            status = "concluded"
            note = "当场确认通过：agree %d/在场 %d 人（需 %d，被点名 %d 人）" % (
                len(summary["agree"]), summary.get("present", 0),
                summary["required"], summary["total"])
        else:
            status = "deferred"
            note = ("**未当场确认**：agree %d / object %d / abstain %d / absent %d"
                    "（在场 %d 人，需 agree %d）" % (
                        len(summary["agree"]), len(summary["object"]),
                        len(summary["abstain"]), len(summary["absent"]),
                        summary.get("present", 0), summary["required"]))
            self._record_issue(
                title="议程第 %s 项「%s」未当场确认" % (seq, one_line(item["title"], 80)),
                detail=note + "；本项结论：" + one_line(text, 400),
                owner=(summary["object"][0] if summary["object"] else None),
                refs=[{"type": "entry", "value": entry_id}] if entry_id else
                     [{"type": "session", "value": self.session_id}])
        self.say("[议程 %s 确认] %s" % (seq, note))

        self.store.update_agenda_item(self.session_id, seq, conclusion=truncate(text, CONTENT_MAX),
                                      conclusion_entry=entry_id, status=status, note=note)
        self.item_results.append({"seq": seq, "title": item["title"], "status": status,
                                  "conclusion": text, "entry_id": entry_id,
                                  "confirm": summary, "rounds": rounds})

    def _collect_confirmations(self, target: str, conclusion: str,
                               item: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """向本场实际发言过的成员逐个点名要确认。返回 confirm_summary。"""
        speakers = self._speakers()
        rows = []  # type: List[Dict[str, Any]]
        label = ("议程第 %s 项" % item["seq"]) if item else "总体结论"
        for member in speakers:
            prompt = render_confirm_prompt(member, self.goal, label, conclusion,
                                           self.max_chars, item=item)
            self.chars_used += len(prompt)
            self._log_prompt(member + "(确认 %s)" % target, prompt)
            status, reply, error = self.transport.ask(member, prompt, ASK_TIMEOUT_S)
            self.chars_used += len(reply or "")
            if status != "ok" or not reply:
                verdict, note = "absent", truncate(error or "确认点名无应答", 200)
                self._mark_absent(member, note, "确认 %s" % target)
            else:
                self._mark_present(member)
                verdict, note, perr = parse_confirm(reply)
                if perr:
                    note = ("%s（%s）" % (note, perr)).strip()
            row = {"session_id": self.session_id, "target": target, "member": member,
                   "verdict": verdict, "note": note or None}
            rows.append(row)
            self.confirm_rows.append(row)
            self.store.add_confirmation(row)
            self.say("[确认 %s] %s → %s" % (target, member, verdict))

        summary = tally_confirmations(rows)
        for row in rows:
            if row["verdict"] == "object":
                self._record_issue(
                    title="%s 被 %s 反对" % (label, row["member"]),
                    detail=truncate(row.get("note") or "（没写理由）", 1000),
                    owner=row["member"],
                    refs=[{"type": "session", "value": self.session_id}])
        return summary

    # ---------------------------------------------------------- 总体结论 + 落实事项
    def _conclude_overall(self) -> None:
        """总体结论 → actions 校验（硬）→ 黑板 decision → 总体确认 → 回写 session。"""
        moderator = self._pick_moderator()
        snippets = self._snippets()
        roster = self._roster()
        text = ""
        actions = []    # type: List[Dict[str, Any]]
        issues = []     # type: List[Dict[str, Any]]
        action_errors = []  # type: List[str]

        if moderator:
            retry_reason = ""
            for attempt in (1, 2):          # 校验不过重试 1 次，仍不过就记 incomplete
                prompt, used = render_conclusion_prompt(
                    self.topic, self.question, self.round_no, self.tally, snippets,
                    self.max_chars, goal=self.goal, agenda=self.agenda, item=None,
                    item_results=self.item_results, roster=roster,
                    now_text=now_utc().isoformat(), retry_reason=retry_reason)
                self.say("[总体结论] 主持人 %s 第 %d 次：prompt %d 字符 / 片段 %d 条"
                         % (moderator, attempt, len(prompt), used))
                reply = self._ask_moderator(moderator, prompt, "总体结论")
                if not reply:
                    action_errors = ["主持人没给出结论"]
                    break
                parsed, _err = parse_turn(reply)
                if parsed is None:
                    text = text or truncate(reply.strip(), CONTENT_MAX)
                    action_errors = ["结论回包解析不出 JSON，拿不到 actions"]
                    retry_reason = action_errors[0]
                    continue
                text = parsed["content"]
                issues = parse_issues(parsed.get("issues"))
                actions, action_errors = validate_actions(
                    parsed.get("actions"), roster, now_utc(), self.agenda_seqs)
                if actions and not action_errors:
                    break
                retry_reason = "；".join(action_errors)[:400]
                self.say("[落实] 第 %d 次 actions 不合格：%s" % (attempt, retry_reason))
        else:
            action_errors = ["主持人全员离线，拿不到落实事项"]
            self.say("[总体结论] 主持人全员离线，取最后一条有效发言")

        if not text:
            text = self.session_texts[-1] if self.session_texts else "本场没有产生任何有效发言。"
            text = "（主持人缺席，取最后一条有效发言作为结论）\n" + text
        if self.state == "timeout":
            text = "【未自然收敛】" + (self.note or "") + "\n" + text

        # ---- 落实事项：合格的落库；不合格 → incomplete，绝不静默通过
        if actions and not action_errors:
            for act in actions:
                row = {"session_id": self.session_id, "agenda_seq": act["agenda_seq"],
                       "what": act["what"], "owner": act["owner"],
                       "due_at": act["due_at"], "status": "open"}
                self.action_rows.append(row)
                self.store.add_action(row)
                self.say("[落实] %s → %s（截止 %s）"
                         % (act["what"], act["owner"], act["due_at"].isoformat()))
        else:
            reason = "落实事项缺失或不合格：%s" % ("；".join(action_errors) or "没给 actions")
            self.state = "incomplete"
            self.note = (self.note + "；" if self.note else "") + reason
            self.say("[落实] %s → 会议记 incomplete" % reason)
            self._record_issue(title="本场没有产生有效的落实事项",
                               detail=truncate(reason, 1000), owner=moderator,
                               refs=[{"type": "session", "value": self.session_id}])

        # ---- 主持人主动登记的遗留问题
        for extra in issues:
            self._record_issue(title=extra["title"], detail=extra.get("detail") or "",
                               owner=extra.get("owner"), due_at=extra.get("due_at"),
                               refs=[{"type": "session", "value": self.session_id}])

        # ---- 缺席导致证据不全的，也要进遗留问题
        #      （与缺陷单分工不同：这条说的是"结论证据不全"，缺陷单说的是"程序可能坏了"）
        missing = self.attendance()["absent_members"]
        if missing:
            self._record_issue(
                title="成员 %s 全程未发言，本场结论的证据不完整" % ",".join(missing),
                detail="缺席成员：%s；需在下一场补齐其意见或确认其弃权。" % ",".join(missing),
                owner=moderator,
                refs=[{"type": "session", "value": self.session_id}])

        # ---- 把四要素的摘要附到结论正文里，让黑板上那条 decision 自解释
        text = self._decorate_conclusion(text)

        refs = [{"type": "entry", "value": eid} for eid in _dedup(self.cited_entries)]
        if not refs:
            refs = [{"type": "session", "value": self.session_id}]
        entry_id, err = self.bb.post_entry(self.topic, "decision",
                                           truncate(text, CONTENT_MAX), refs)
        if err:
            self.say("[总体结论] 写黑板失败：%s" % err)
        self.conclusion_entry = entry_id

        # ---- 总体结论也要当场确认
        summary = self._collect_confirmations("overall", text, None)
        # 出席计量挂在 confirm_summary 里一并落库：四个数字进各自的列，**缺席名单**
        # 只有这里有地方放（看板要显示谁没来），所以做成 summary 的子对象。
        summary["attendance"] = self.attendance()
        self.overall_confirm = summary
        confirmed_at = now_utc() if summary.get("passed") else None
        if not summary.get("passed"):
            note = "总体结论**未当场确认**：agree %d / object %d（在场 %d 人，需 %d）" % (
                len(summary["agree"]), len(summary["object"]),
                summary.get("present", 0), summary["required"])
            self.note = (self.note + "；" if self.note else "") + note
            self.say("[总体确认] %s" % note)
            self._record_issue(title="总体结论未当场确认", detail=note,
                               owner=(summary["object"][0] if summary["object"] else None),
                               refs=[{"type": "entry", "value": entry_id}] if entry_id else
                                    [{"type": "session", "value": self.session_id}])
        else:
            self.say("[总体确认] 通过：agree %d/在场 %d 人（需 %d，被点名 %d 人）"
                     % (len(summary["agree"]), summary.get("present", 0),
                        summary["required"], summary["total"]))

        if self.state == "running":
            self.state = "converged"
        att = summary["attendance"]
        self.store.update_session(self.session_id, state=self.state,
                                  round_no=self.round_no, ended_at=now_utc(),
                                  conclusion_entry=entry_id, note=self.note or None,
                                  confirmed_at=confirmed_at, confirm_summary=summary,
                                  expected_count=att["expected"],
                                  present_count=att["present"],
                                  absent_count=att["absent"],
                                  attendance_rate=att["rate"])

    def _decorate_conclusion(self, text: str) -> str:
        """在结论正文后面附上：出席率 / 逐项结论 / 当场确认 / 落实事项 / 遗留问题。"""
        att = self.attendance()
        parts = [text, "",
                 "【出席】应到 %d 人，实到 %d 人，%s；缺席：%s"
                 % (att["expected"], att["present"], self.attendance_line(),
                    "、".join(att["absent_members"]) or "无"),
                 "【逐项结论】"]
        for res in self.item_results:
            mark = "已确认" if res["status"] == "concluded" else "未当场确认"
            parts.append("· 第 %s 项 %s（%s）：%s"
                         % (res["seq"], res["title"], mark, one_line(res["conclusion"], 200)))
        if self.action_rows:
            parts.append("【落实事项】")
            for row in self.action_rows:
                parts.append("· %s → %s，截止 %s"
                             % (row["what"], row["owner"], row["due_at"].isoformat()))
        else:
            parts.append("【落实事项】无有效落实事项，本场记 incomplete。")
        if self.issue_rows:
            parts.append("【遗留问题】")
            for row in self.issue_rows:
                parts.append("· %s（跟踪人：%s）" % (row["title"], row.get("owner") or "待定"))
        return "\n".join(parts)

    def _roster(self) -> List[str]:
        """本次校验 owner 用的 roster 口径：本场成员 + 主持人（去重、保序）。"""
        return _dedup_names(list(self.members) + [self.moderator])

    # ---------------------------------------------------------- 遗留问题
    def _record_issue(self, title: str, detail: str = "", owner: Optional[str] = None,
                      due_at: Optional[datetime.datetime] = None,
                      refs: Optional[List[Dict[str, Any]]] = None,
                      kind: str = "issue",
                      evidence: Optional[List[Dict[str, Any]]] = None) -> None:
        """登记一条遗留问题（只进内存，落库在 _flush_issues 里统一做）。

        kind='issue' 是普通遗留问题；kind='defect' 是疑似程序缺陷（目前只有缺席会开）。
        """
        self.issue_rows.append({
            "session_id": self.session_id, "title": truncate(title, 300),
            "detail": truncate(detail, 2000) or None, "owner": owner, "status": "open",
            "due_at": due_at, "next_check_at": None, "kind": kind,
            "evidence": list(evidence or []),
            "refs": refs or [{"type": "session", "value": self.session_id}]})

    def _flush_issues(self) -> None:
        """把攒下的遗留问题一次性落库。**放在结论之后**，因为 refs 要指回结论条目。

        缺席缺陷单在这里补开：无论会议是正常收尾还是中途 aborted，
        「有人点名没应答」这件事都必须留下一张能闭环的单子。
        """
        self._open_absence_defects()
        if self.conclusion_entry is not None:
            back = {"type": "entry", "value": self.conclusion_entry}
            for row in self.issue_rows:
                if back not in row["refs"]:
                    row["refs"] = list(row["refs"]) + [back]
        for row in self.issue_rows:
            try:
                self.store.add_issue(row)
            except Exception as exc:
                self.say("[遗留] 写 issue 失败（%s）：%s" % (truncate(exc, 120), row["title"]))

    def _abort(self, reason: str) -> None:
        self.state = "aborted"
        self.note = reason
        self.say("[中止] %s" % reason)
        att = self.attendance()
        try:
            # 中止的会同样要留下出席计量：会开崩了，往往正是因为人没到齐
            self.store.update_session(self.session_id, state="aborted",
                                      round_no=self.round_no, ended_at=now_utc(),
                                      note=truncate(reason, 500),
                                      expected_count=att["expected"],
                                      present_count=att["present"],
                                      absent_count=att["absent"],
                                      attendance_rate=att["rate"])
        except Exception as exc:
            self.say("[中止] 状态回写失败（%s）—— 请手工把 %s 置为 aborted"
                     % (truncate(exc, 120), self.session_id))
        self._flush_issues()
        self._summary()

    def _summary(self) -> None:
        elapsed = time.time() - self.started
        per_member = " ".join("%s=%d" % (m, self.spoken.get(m, 0)) for m in self.members)
        agree = sum(1 for r in self.confirm_rows if r["verdict"] == "agree")
        object_ = sum(1 for r in self.confirm_rows if r["verdict"] == "object")
        abstain = sum(1 for r in self.confirm_rows if r["verdict"] == "abstain")
        absent = sum(1 for r in self.confirm_rows if r["verdict"] == "absent")

        for res in self.item_results:
            self.say("[汇总·议程 %s] %s status=%s 确认 agree=%d/object=%d entry=%s ｜ %s"
                     % (res["seq"], res["title"], res["status"],
                        len(res["confirm"].get("agree") or []),
                        len(res["confirm"].get("object") or []),
                        res["entry_id"] if res["entry_id"] is not None else "-",
                        one_line(res["conclusion"], 120)))
        if self.overall_confirm:
            self.say("[汇总·总体确认] passed=%s agree=%d/%d required=%d"
                     % (self.overall_confirm.get("passed"),
                        len(self.overall_confirm.get("agree") or []),
                        self.overall_confirm.get("total", 0),
                        self.overall_confirm.get("required", 0)))
        for row in self.action_rows:
            self.say("[汇总·落实] %s → %s 截止 %s"
                     % (one_line(row["what"], 80), row["owner"], row["due_at"].isoformat()))
        for row in self.issue_rows:
            self.say("[汇总·遗留] %s%s（跟踪人：%s）"
                     % ("【疑似缺陷】" if row.get("kind") == "defect" else "",
                        one_line(row["title"], 100), row.get("owner") or "待定"))

        # 出席率单独来一行：应到 / 实到 / 缺席名单一次看全（分母是**应到人数**）
        att = self.attendance()
        self.say("[汇总·出席] 应到 %d 人 / 实到 %d 人 / 缺席 %d 人 —— %s；缺席名单：%s"
                 % (att["expected"], att["present"], att["absent"], self.attendance_line(),
                    "、".join(att["absent_members"]) or "无（全员到齐）"))
        defects = [r for r in self.issue_rows if r.get("kind") == "defect"]
        if defects:
            self.say("[汇总·缺陷] 缺席开单 %d 条，24 小时内必须闭环：%s"
                     % (len(defects), "、".join(r.get("owner") or "?" for r in defects)))

        self.say("[汇总] session=%s state=%s rounds=%d agenda=%d/%d concluded "
                 "%s confirm[agree=%d object=%d abstain=%d absent=%d] "
                 "actions=%d issues=%d defects=%d "
                 "speakers[%s] elapsed=%.1fs chars=%d(~%dtok) conclusion_entry=%s"
                 % (self.session_id, self.state, self.round_no,
                    sum(1 for r in self.item_results if r["status"] == "concluded"),
                    len(self.agenda), self.attendance_line(), agree, object_, abstain, absent,
                    len(self.action_rows), len(self.issue_rows), len(defects),
                    per_member, elapsed,
                    self.chars_used, self.chars_used // CHARS_PER_TOKEN,
                    self.conclusion_entry if self.conclusion_entry is not None else "-"))


def _dedup(values: Sequence[int]) -> List[int]:
    seen = set()
    out = []  # type: List[int]
    for v in values:
        if v is None or v in seen:
            continue
        seen.add(v)
        out.append(v)
    return out


# ================================================================ CLI

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="圆桌会议调度器（一次性跑完即退，不常驻、不监听端口）")
    parser.add_argument("--topic", default=None, help="议题（同时作为黑板条目的 topic）")
    parser.add_argument("--question", default=None, help="本场要回答的问题")
    parser.add_argument("--goal", default=None,
                        help="**会前必填**：本场会议的目标（缺了直接拒绝开会，退出码 2）")
    parser.add_argument("--agenda", action="append", default=[],
                        help='**会前必填**：议程，格式 "标题|预期产出; 标题|预期产出"，可重复传入')
    parser.add_argument("--members", default="", help="成员名，逗号分隔，如 <MEMBER_ID_1>,<MEMBER_ID_2>,<MEMBER_ID_3>")
    parser.add_argument("--max-rounds", type=int, default=DEFAULT_MAX_ROUNDS,
                        help="**每个议程项**的最大轮次，默认 %d" % DEFAULT_MAX_ROUNDS)
    parser.add_argument("--max-total-rounds", type=int, default=DEFAULT_MAX_TOTAL_ROUNDS,
                        help="整场总轮数上限（安全阀），默认 0 = 不额外限制"
                             "（总轮数 = 议程项数 × --max-rounds）；撞上限后剩余议程项标 deferred")
    parser.add_argument("--deadline", type=float, default=DEFAULT_DEADLINE_S,
                        help="整场时间上限（秒），默认 %d" % DEFAULT_DEADLINE_S)
    parser.add_argument("--session-id", default=None, help="会话 id（缺省自动生成）")
    parser.add_argument("--moderator", default=DEFAULT_MODERATOR,
                        help="主持人，默认 %s；离线则依次 fallback %s"
                             % (DEFAULT_MODERATOR, "/".join(MODERATOR_FALLBACK)))
    parser.add_argument("--transport", choices=("nats", "a2a"), default="nats",
                        help="点名通道，默认 nats")
    parser.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS,
                        help="单次 prompt 字符上限，默认 %d" % DEFAULT_MAX_CHARS)
    parser.add_argument("--prompt-log", default=None, help="把每次 prompt 追加写到这个文件（可选）")
    parser.add_argument("--commit", action="store_true",
                        help="真写：写黑板 + 写 PG。不给就是演练（只跑不落库）")
    parser.add_argument("--dry-run", action="store_true",
                        help="离线自测：假应答，不调任何 Agent、不写库、不联网")
    parser.add_argument("--resume", default=None, help="从 PG 里已有的 session_id 续跑")
    parser.add_argument("--dsn-file", default=os.environ.get("AGENT_TEAM_PG_ENV_FILE") or (AGENT_TEAM_ETC_DIR + "/pg.env"),
                        help="systemd 环境文件，从中取 BB_PG_DSN=")
    parser.add_argument("--blackboard", default=os.environ.get("AGENT_TEAM_BB_BASE") or _npf.blackboard_base_default(),
                        help="黑板服务地址（写入只走它的 POST /entries）")
    parser.add_argument("--token-file", default=os.environ.get("AGENT_TEAM_TOKEN_FILE") or (AGENT_TEAM_ETC_DIR + "/client-token"),
                        help="黑板 / A2A 的 Bearer token 文件路径")
    parser.add_argument("--nats-host", default=os.environ.get("AGENT_TEAM_NATS_HOST") or _npf.nats_host_default(), help="NATS 中枢地址")
    parser.add_argument("--nats-port", type=int, default=9930, help="NATS 端口，默认 9930")
    parser.add_argument("--nats-user", default=None, help="自己的 NATS 用户名")
    parser.add_argument("--nats-password-file", default=None, help="自己的 NATS 口令文件")
    parser.add_argument("--a2a-endpoint", action="append", default=[],
                        metavar="MEMBER=URL", help="--transport a2a 时的成员端点，可重复")
    return parser


def _parse_endpoints(items: Sequence[str]) -> Dict[str, str]:
    out = {}  # type: Dict[str, str]
    for item in items or ():
        member, _sep, url = str(item).partition("=")
        if member.strip() and url.strip():
            out[member.strip()] = url.strip()
    return out


def build_transport(args: argparse.Namespace) -> Any:
    if args.dry_run:
        return DryRunTransport()
    if args.transport == "a2a":
        return A2ATransport(_parse_endpoints(args.a2a_endpoint), args.token_file)
    if not args.nats_user or not args.nats_password_file:
        raise RuntimeError("--transport nats 需要 --nats-user 与 --nats-password-file")
    return NatsTransport(args.nats_host, args.nats_port, args.nats_user,
                         args.nats_password_file)


def gate_check(goal: Any, agenda_raw: Sequence[str],
               resumed: Optional[Dict[str, Any]] = None
               ) -> Tuple[str, List[Dict[str, Any]], List[str]]:
    """**会前门禁**：没目标 / 没议程一律不许开会。返回 (goal, agenda, 缺失清单)。

    --resume 续跑时目标与议程来自库里已冻结的那份，不再重新要求命令行给。
    """
    resumed = resumed or {}
    goal = str(resumed.get("goal") or goal or "").strip()
    missing = []  # type: List[str]
    if not goal:
        missing.append("--goal（会议目标）：会议必须有明确目标，不给就不开会")

    agenda = list(resumed.get("agenda") or [])
    if not agenda:
        agenda, err = parse_agenda(agenda_raw)
        if err:
            missing.append('--agenda（会前议程）：%s；格式 "标题|预期产出; 标题|预期产出"' % err)
    return goal, agenda, missing


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    out = lambda line: sys.stdout.write(line + "\n")  # noqa: E731

    commit = bool(args.commit) and not args.dry_run

    # --- 存储与黑板：--commit 才接真家伙，否则一律 Null（演练不留任何痕迹）
    store = None  # type: Any
    resumed = None  # type: Optional[Dict[str, Any]]
    try:
        if commit:
            store = PgStore(read_dsn(args.dsn_file))
            blackboard = BlackboardClient(args.blackboard, args.token_file)
        else:
            store = NullStore()
            blackboard = NullBlackboard()
            out("[演练] 未给 --commit：不写黑板、不写 PG（%s）"
                % ("dry-run 假应答" if args.dry_run else "真点名"))
    except Exception as exc:
        sys.stderr.write("初始化失败：%s: %s\n" % (exc.__class__.__name__, truncate(exc, 200)))
        return 2

    if args.resume:
        resumed = store.load_session(args.resume)
        if not resumed:
            sys.stderr.write("--resume 找不到 session：%s\n" % args.resume)
            return 2

    # 题目与问题：**目标是硬门禁，题目/问题不是** —— 没给就从目标兜底，但要把兜底
    # 这件事明明白白打出来（topic 决定黑板条目落在哪个话题下，不能悄悄换地方）。
    topic = (resumed or {}).get("topic") or args.topic
    question = (resumed or {}).get("question") or args.question or args.goal
    if not topic:
        topic = DEFAULT_TOPIC
        out("[warn] 未给 --topic，本场黑板条目落在缺省话题 %r 下" % DEFAULT_TOPIC)
    members_raw = (resumed or {}).get("members") or [
        m.strip() for m in (args.members or "").split(",") if m.strip()]
    if not question:
        sys.stderr.write("--question 必填（或给 --goal 兜底，或用 --resume 续跑已有会话）\n")
        return 2
    members = _dedup_names(members_raw)
    if len(members) < 2:
        sys.stderr.write("至少要两名成员才开得起圆桌（拿到的是 %r）\n" % (members,))
        return 2

    # ---- 会前门禁（硬）：目标与议程缺任一 → 拒绝开会，退出码 2，打印缺什么
    goal, agenda, missing = gate_check(args.goal, args.agenda, resumed)
    if missing:
        sys.stderr.write("【拒绝开会】会议有目标、会前有议程，这两条不满足就不开：\n")
        for item in missing:
            sys.stderr.write("  缺 %s\n" % item)
        return 2

    try:
        transport = build_transport(args)
    except Exception as exc:
        sys.stderr.write("点名通道不可用：%s\n" % truncate(exc, 200))
        return 2

    cfg = {
        "session_id": (resumed or {}).get("session_id") or args.session_id or new_session_id(),
        "topic": topic, "question": question, "members": members,
        "max_rounds": int((resumed or {}).get("max_rounds") or args.max_rounds),
        "max_total_rounds": max(0, int(args.max_total_rounds or 0)),
        "deadline": args.deadline, "max_chars": args.max_chars,
        "moderator": (resumed or {}).get("moderator") or args.moderator,
        "start_round": int((resumed or {}).get("round_no") or 0),
        "prompt_log": args.prompt_log,
        "goal": goal, "agenda": agenda,
    }
    if resumed:
        out("[续跑] %s 从第 %d 轮之后继续" % (cfg["session_id"], cfg["start_round"]))

    return Roundtable(cfg, store, blackboard, transport, out=out).run()


def _dedup_names(names: Sequence[str]) -> List[str]:
    seen = set()
    out = []  # type: List[str]
    for name in names:
        name = str(name).strip()
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


if __name__ == "__main__":
    sys.exit(main())
