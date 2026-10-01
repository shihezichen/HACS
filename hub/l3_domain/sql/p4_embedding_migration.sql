-- P4 迁移：嵌入列对齐实际模型维度（bge-small-zh-v1.5 = 512 维）
-- 执行者：Hermes 总控（黑板的 DDL 由我执行，不交给 coding agent）
-- 前置事实：embedding 列当前全为 NULL（P3 只留了 vector(1024) 占位列）

\echo '=== 执行前：列状态与行数 ==='
SELECT count(*) AS rows_total,
       count(embedding) AS rows_with_vec
FROM blackboard_entries;

\echo '=== 迁移：vector(1024) -> vector(512) ==='
ALTER TABLE blackboard_entries
    ALTER COLUMN embedding TYPE vector(512);

\echo '=== 执行后：列类型核对 ==='
SELECT column_name, data_type, udt_name,
       (SELECT atttypmod FROM pg_attribute
         WHERE attrelid = 'blackboard_entries'::regclass AND attname = 'embedding') AS typmod
FROM information_schema.columns
WHERE table_name = 'blackboard_entries' AND column_name = 'embedding';

-- 注：HNSW 索引**不在此脚本建** —— 必须等 backfill 灌完向量后再建，
--     空表建索引等于白建（且 pgvector 不会自动为新数据补索引结构）。
