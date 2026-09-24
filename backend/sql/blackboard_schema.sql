-- blackboard_schema.sql —— 团队黑板的表结构（幂等，可重复执行）
--
-- 用法：
--   psql "host=127.0.0.1 port=9950 dbname=blackboard user=bb" -f sql/blackboard_schema.sql
--
-- 设计口径：**append-only（只追加，不覆盖）**
--   黑板是团队的共享记忆，历史必须可复盘。任何"更正/撤回/补充"都靠
--   **追加一条新条目**，并在 refs 里写上被更正条目的 entry_id，
--   例如：{"kind": "correction", "refs": [17]} —— 绝不原地改写或删除旧条目。
--   下面的触发器会让 UPDATE / DELETE 直接抛错，从数据库层面把这条规矩钉死。
--   （因此 status 列一经写入也不可改；"作废"同样靠追加 kind='retract' 的新条目表达。）

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS blackboard_entries (
  entry_id   bigserial PRIMARY KEY,
  ts         timestamptz NOT NULL DEFAULT now(),
  author     text        NOT NULL,
  topic      text        NOT NULL,
  kind       text        NOT NULL,
  content    text        NOT NULL,
  refs       jsonb       NOT NULL DEFAULT '[]'::jsonb,
  embedding  vector(1024),
  status     text        NOT NULL DEFAULT 'active'
);

CREATE INDEX IF NOT EXISTS bb_topic_kind_ts ON blackboard_entries (topic, kind, ts DESC);
CREATE INDEX IF NOT EXISTS bb_author_ts ON blackboard_entries (author, ts DESC);

-- ---------------------------------------------------------------- append-only 强制
-- 不是靠应用层"约定"，而是数据库层面拒绝：写错了只能再追加一条更正。
CREATE OR REPLACE FUNCTION blackboard_append_only() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'blackboard is append-only (rejected % on blackboard_entries)', TG_OP
    USING HINT = '更正靠追加新条目 + 在 refs 里指回旧 entry_id，不覆盖旧条目';
END;
$$ LANGUAGE plpgsql;

-- DROP + CREATE 而不是 CREATE OR REPLACE TRIGGER：对 PG11+ 都成立，且保持幂等。
DROP TRIGGER IF EXISTS blackboard_no_update ON blackboard_entries;
CREATE TRIGGER blackboard_no_update
  BEFORE UPDATE OR DELETE ON blackboard_entries
  FOR EACH ROW EXECUTE FUNCTION blackboard_append_only();

-- TRUNCATE 绕过行级触发器，单独挡一道（语句级）。
DROP TRIGGER IF EXISTS blackboard_no_truncate ON blackboard_entries;
CREATE TRIGGER blackboard_no_truncate
  BEFORE TRUNCATE ON blackboard_entries
  FOR EACH STATEMENT EXECUTE FUNCTION blackboard_append_only();

-- ---------------------------------------------------------------- TODO(P4) 语义检索
-- P3 只按 topic/kind/时间 检索，embedding 列先留空。P4 接入 1024 维向量后再建索引：
--   CREATE INDEX IF NOT EXISTS bb_embedding_hnsw
--     ON blackboard_entries USING hnsw (embedding vector_cosine_ops);
-- 建索引前先灌够数据，否则 HNSW/IVFFlat 的召回质量没有意义。
