#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pg_literals_old_k16.py —— 刀16 的「旧实现」快照（等价装置的**旧侧**）

内容 = 搬迁前散落在 L3 三个件里的那三份重复实现，**逐字节原样拷贝**自改前树
（`git archive HEAD` 独立解包的副本，非工作区自比）：

  * blackboard_svc.py  L448  `_vector_literal`  → 本件 `bb_vector_literal`
  * blackboard_svc.py  L833  `_to_utc`          → 本件 `bb_to_utc`
  * shared_content.py  L653  `vector_literal`   → 本件 `sc_vector_literal`
  * shared_content.py  L137  `to_utc`           → 本件 `sc_to_utc`
  * backfill_embeddings.py L79 `_vector_literal` → 本件 `bf_vector_literal`

对外名加了 bb_/sc_/bf_ 前缀，只为三份能在同一个模块里共存；**函数体与 docstring
一个字符没改**，签名（参数名、类型标注、默认值）也照旧。本件由
tools/equivalence/pg_literals_equiv.py 当旧侧 import，不进任何运行路径。

⚠ 只许新增，不许覆盖既有 _old/* 历史基线。
"""

import datetime
from typing import Any, Optional, Sequence

# blackboard_svc.py / shared_content.py 各自的 UTC 常量，定义一模一样
UTC = datetime.timezone.utc


# --- 来自 blackboard_svc.py（改前树 L448）：原名 _vector_literal ---
def bb_vector_literal(embedding: Optional[Sequence[float]]) -> Optional[str]:
    """pgvector 的文本字面量形式，避免为了一列向量引入 pgvector 的 python 适配器。"""
    if embedding is None:
        return None
    return "[" + ",".join(repr(float(x)) for x in embedding) + "]"


# --- 来自 blackboard_svc.py（改前树 L833）：原名 _to_utc ---
def bb_to_utc(value: Any) -> Optional[datetime.datetime]:
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


# --- 来自 shared_content.py（改前树 L653）：原名 vector_literal ---
def sc_vector_literal(embedding: Optional[Sequence[float]]) -> Optional[str]:
    """pgvector 的文本字面量形式，避免为了一列向量引入 pgvector 的 python 适配器。"""
    if embedding is None:
        return None
    return "[" + ",".join(repr(float(x)) for x in embedding) + "]"


# --- 来自 shared_content.py（改前树 L137）：原名 to_utc ---
def sc_to_utc(value: Any) -> Optional[datetime.datetime]:
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


# --- 来自 backfill_embeddings.py（改前树 L79）：原名 _vector_literal ---
def bf_vector_literal(values: Sequence[float]) -> str:
    """pgvector 文本字面量，与 blackboard_svc._vector_literal 同一口径。"""
    return "[" + ",".join(repr(float(x)) for x in values) + "]"


__all__ = ["UTC", "bb_vector_literal", "bb_to_utc",
           "sc_vector_literal", "sc_to_utc", "bf_vector_literal"]
