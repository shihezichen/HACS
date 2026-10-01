#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pg_literals.py —— 对外部存储（PostgreSQL / pgvector）的**类型口径适配**·单一出处

层归属 L2 适配层。理由：这里没有任何协作域语义（黑板 / 共享内容 / 会议一个字都不沾），
只有「Python 的值怎么写给 PG、PG 的值怎么读回 Python」这一件事 —— 外部依赖的差异
落在适配层，是 L2 的本分。方向上被 L3（blackboard_svc / shared_content /
backfill_embeddings）import，L2→L3 的反向依赖一条没有。

为什么要有这个件（刀16 · 阶段3 第三刀）：
  同一份实现在 L3 里抄了三遍 —— blackboard_svc._vector_literal / _to_utc、
  shared_content.vector_literal / to_utc、backfill_embeddings._vector_literal。
  三份函数体逐字节相同（backfill 那份只是少了 None 分支），改一处就得改三处，
  而「裸时间按 UTC 解释」「向量字面量怎么格式化」这类口径一旦分叉，
  两边算出来的「过期没过期」「写进库的是不是同一个数」就会是两件事。
  本件把它们收成一份；三个调用件各自保留同名别名，调用点一行没动。

纪律：
  * 纯标准库，零第三方依赖（连 psycopg2 都不 import —— 本件只造字面量，不碰连接）；
  * 只读纯函数，没有状态、没有 I/O、没有日志；
  * 认不出来的输入一律返回 None，**不抛、不猜、不编造**。
"""

import datetime
from typing import Any, Optional, Sequence

UTC = datetime.timezone.utc


def vector_literal(embedding: Optional[Sequence[float]]) -> Optional[str]:
    """pgvector 的文本字面量形式，避免为了一列向量引入 pgvector 的 python 适配器。"""
    if embedding is None:
        return None
    return "[" + ",".join(repr(float(x)) for x in embedding) + "]"


def to_utc(value: Any) -> Optional[datetime.datetime]:
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


__all__ = ["UTC", "vector_literal", "to_utc"]
