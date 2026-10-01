-- P4 第二步：为语义检索建 HNSW 索引
-- 执行时机：**必须在 backfill_embeddings.py 灌完向量之后**
-- 原因：空表/少量数据建索引白建；且 pgvector 不会自动把新数据补进已建索引结构（会走顺序扫描兜底，但召回与性能都不达标）

\echo '=== 建索引前：向量覆盖率（必须接近 100%）==='
SELECT count(*) AS rows_total,
       count(embedding) AS rows_with_vec,
       round(100.0 * count(embedding) / nullif(count(*), 0), 1) AS pct
FROM blackboard_entries;

\echo '=== 建 HNSW 索引（余弦距离）==='
SET maintenance_work_mem = '128MB';   -- 2 核小机，别开太大
CREATE INDEX IF NOT EXISTS idx_bb_embedding_hnsw
    ON blackboard_entries USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

\echo '=== 索引核对 ==='
SELECT indexname, indexdef
FROM pg_indexes
WHERE tablename = 'blackboard_entries';

\echo '=== 索引大小（评估是否吃掉太多存储）==='
SELECT pg_size_pretty(pg_relation_size('idx_bb_embedding_hnsw')) AS hnsw_size,
       pg_size_pretty(pg_relation_size('blackboard_entries'))  AS table_size;

-- 查询期参数：ef_search 越大召回越高越慢；默认 40，语义检索起步够用
-- 可在 /search 连接上 SET LOCAL hnsw.ef_search = 40;
