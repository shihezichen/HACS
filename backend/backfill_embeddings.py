#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backfill_embeddings.py —— 给历史条目补算向量（幂等、可中断、可 --dry-run）

只处理 `embedding IS NULL` 的行，按 entry_id 游标向前推进，每批 commit 一次：
中途 Ctrl-C 或断连都不会重复算已经写好的行，再跑一次接着往下走。

用法：
    BB_PG_DSN='host=127.0.0.1 port=9950 dbname=blackboard user=bb password=...' \
    python backfill_embeddings.py --dry-run          # 先看要处理多少条
    python backfill_embeddings.py --batch-size 16    # 真干

⚠ 与 append-only 触发器的冲突（必读）
    sql/blackboard_schema.sql 里的 blackboard_no_update 触发器对
    **任何 UPDATE 都抛错**，而回填向量本质上就是 UPDATE。所以本脚本在默认配置下
    会被数据库挡住。两条出路，二选一（都由你执行，脚本自己不改 schema）：

    A. 临时旁路（本会话内生效，需要 superuser）：加 --bypass-trigger，
       脚本会在同一个连接里执行 `SET session_replication_role = replica`。
       只影响这一个连接，脚本退出即失效；期间该连接的 UPDATE/DELETE 不再被拦，
       所以**只在跑回填时用**。

    B. 永久收窄触发器（推荐，一次性 DDL）：只放行"把 NULL 的 embedding 填上"
       这一种 UPDATE，其余照旧拒绝：

           DROP TRIGGER IF EXISTS blackboard_no_update ON blackboard_entries;
           CREATE TRIGGER blackboard_no_update
             BEFORE UPDATE ON blackboard_entries
             FOR EACH ROW
             WHEN (OLD.embedding IS NOT NULL
                   OR NEW.entry_id IS DISTINCT FROM OLD.entry_id
                   OR NEW.ts      IS DISTINCT FROM OLD.ts
                   OR NEW.author  IS DISTINCT FROM OLD.author
                   OR NEW.topic   IS DISTINCT FROM OLD.topic
                   OR NEW.kind    IS DISTINCT FROM OLD.kind
                   OR NEW.content IS DISTINCT FROM OLD.content
                   OR NEW.refs    IS DISTINCT FROM OLD.refs
                   OR NEW.status  IS DISTINCT FROM OLD.status)
             EXECUTE FUNCTION blackboard_append_only();

           -- DELETE 必须单独再挡一道（WHEN 子句里不能引用 NEW，所以拆成两个触发器）
           DROP TRIGGER IF EXISTS blackboard_no_delete ON blackboard_entries;
           CREATE TRIGGER blackboard_no_delete
             BEFORE DELETE ON blackboard_entries
             FOR EACH ROW EXECUTE FUNCTION blackboard_append_only();

       这样"历史不可改写"的语义一分没丢：正文/作者/时间/状态还是动不了，
       只有那列本来就是空的派生数据能补上。

依赖：psycopg2（与主服务同一个）+ embedder.py（fastembed）。
"""

import argparse
import os
import sys
import time
from typing import Any, List, Optional, Sequence

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import embedder  # noqa: E402

COUNT_SQL = "SELECT count(*) FROM blackboard_entries WHERE embedding IS NULL"
FETCH_SQL = ("SELECT entry_id, content FROM blackboard_entries "
             "WHERE embedding IS NULL AND entry_id > %s ORDER BY entry_id LIMIT %s")
# 注意 AND embedding IS NULL：并发写入时也不会覆盖别人刚写好的向量。
FILL_SQL = ("UPDATE blackboard_entries SET embedding = %s::vector "
            "WHERE entry_id = %s AND embedding IS NULL")
BYPASS_SQL = "SET session_replication_role = replica"

TRIGGER_HINT = (
    "写入被 append-only 触发器拒绝。要么加 --bypass-trigger（需要 superuser，"
    "只影响本连接），要么按本文件顶部注释里的方案 B 把触发器收窄。")


def _vector_literal(values: Sequence[float]) -> str:
    """pgvector 文本字面量，与 blackboard_svc._vector_literal 同一口径。"""
    return "[" + ",".join(repr(float(x)) for x in values) + "]"


def _import_psycopg2() -> Any:
    try:
        import psycopg2
        return psycopg2
    except ImportError as exc:
        sys.stderr.write("psycopg2 未安装（%s）。装法：pip install 'psycopg2-binary>=2.9'\n" % exc)
        raise SystemExit(2)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="给 blackboard_entries 里 embedding 为 NULL 的行补算向量（幂等）")
    parser.add_argument("--batch-size", type=int, default=16,
                        help="每批处理多少条，默认 16")
    parser.add_argument("--max-rows", type=int, default=0,
                        help="最多处理多少条，0 表示不限（默认）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只统计和试算，不写库")
    parser.add_argument("--bypass-trigger", action="store_true",
                        help="本连接内执行 SET session_replication_role=replica，"
                             "绕开 append-only 触发器（需要 superuser）")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    batch_size = args.batch_size if args.batch_size > 0 else 16

    dsn = os.environ.get("BB_PG_DSN") or ""
    if not dsn:
        sys.stderr.write("环境变量 BB_PG_DSN 未设置\n")
        return 2

    psycopg2 = _import_psycopg2()
    started = time.time()

    try:
        conn = psycopg2.connect(dsn, connect_timeout=5)
    except Exception as exc:      # 口令在 DSN 里，只打异常类型不打 DSN
        sys.stderr.write("连库失败：%s\n" % str(exc).replace("\n", " ")[:200])
        return 2

    filled = 0
    skipped = 0
    try:
        conn.autocommit = False
        cur = conn.cursor()

        if args.bypass_trigger and not args.dry_run:
            cur.execute(BYPASS_SQL)
            conn.commit()
            sys.stdout.write("[警告] 已在本连接内旁路 append-only 触发器\n")

        cur.execute(COUNT_SQL)
        row = cur.fetchone()
        pending = row[0] if row else 0
        sys.stdout.write("待处理（embedding IS NULL）：%d 条；模型=%s 维度=%d%s\n"
                         % (pending, embedder.model_name(), embedder.embed_dim(),
                            "；dry-run" if args.dry_run else ""))
        if not pending:
            sys.stdout.write("没有要补的行，退出。\n")
            return 0

        last_id = 0
        while True:
            if args.max_rows and filled + skipped >= args.max_rows:
                break
            take = batch_size
            if args.max_rows:
                take = min(take, args.max_rows - filled - skipped)
            cur.execute(FETCH_SQL, (last_id, take))
            rows = cur.fetchall()
            if not rows:
                break
            last_id = rows[-1][0]      # 游标只进不退：失败的行也不会把循环卡死

            texts = [r[1] or "" for r in rows]
            vectors = embedder.embed_texts(texts)
            if vectors is None:
                conn.rollback()
                sys.stderr.write("嵌入不可用，中止：%s\n"
                                 % (embedder.status().get("last_error") or "未知原因"))
                return 3

            if args.dry_run:
                skipped += len(rows)
                sys.stdout.write("[dry-run] 试算 %d 条（entry_id ≤ %d）\n" % (len(rows), last_id))
                continue

            try:
                for (entry_id, _content), vec in zip(rows, vectors):
                    cur.execute(FILL_SQL, (_vector_literal(vec), entry_id))
                    filled += cur.rowcount or 0
                conn.commit()
            except Exception as exc:
                conn.rollback()
                message = str(exc).replace("\n", " ")[:200]
                sys.stderr.write("写入失败：%s\n" % message)
                if "append-only" in message or "append_only" in message:
                    sys.stderr.write(TRIGGER_HINT + "\n")
                return 4
            sys.stdout.write("已补 %d/%d 条（entry_id ≤ %d）\n" % (filled, pending, last_id))
    except KeyboardInterrupt:
        sys.stdout.write("\n收到中断；已提交的批次保留，重跑可接着补。\n")
    finally:
        try:
            conn.close()
        except Exception:
            pass

    elapsed = time.time() - started
    sys.stdout.write("完成：写入 %d 条，试算 %d 条，耗时 %.1f 秒\n" % (filled, skipped, elapsed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
