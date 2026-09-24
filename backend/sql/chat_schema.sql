-- chat_schema.sql —— 人机对话台（team_chat）后端的表（幂等，可重复执行）
--
-- 用法（与黑板 / 圆桌 / 看板同一套库、同一套口令来源）：
--   psql "host=127.0.0.1 port=9950 dbname=blackboard user=bb" -f sql/chat_schema.sql
--   本文件不依赖任何业务表，可单独执行；不需要 vector 扩展。
--
-- 契约出处：API契约-人机对话台-v1.md §10
--
-- 三条设计纪律（都来自契约，改这里之前先改契约）：
--   * **唯一实体引用不复制冗余**：meet 场的消息正文不复制，只放 ref_id 指
--     roundtable_turns.turn_id，读的时候从真源投影。历史会议不搬正文，
--     chat_sessions 只回填索引（session_id / title / kind=meet / ref）。
--   * **消息层 append-only**：chat_messages 只写"落定"消息（done/failed/canceled），
--     queued/running 不落库 —— 所以这张表永远只增不改。UPDATE / DELETE / TRUNCATE
--     由触发器直接拒（同 view_audit 的口径）。
--   * **seq 是会话内严格递增的整数**：历史分页与 SSE 断线续推都靠它。分配走
--     pg_advisory_xact_lock(会话) + max(seq)+1，和 agent_session_turns 一个手法。

-- ================================================================ 一、会话索引层
-- 唯一枚举真源：dm 与 meet 都在这张表里有且只有一行。
CREATE TABLE IF NOT EXISTS chat_sessions (
  session_id   text        PRIMARY KEY,
  kind         text        NOT NULL CHECK (kind IN ('dm', 'meet')),
  title        text        NOT NULL,
  sensitivity  text        NOT NULL DEFAULT 'internal' CHECK (sensitivity IN ('public', 'internal')),
  members      jsonb       NOT NULL DEFAULT '[]'::jsonb,   -- 成员 id 快照（数组）
  created_by   text        NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now(),
  state        text        NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'archived')),
  ref_table    text,                                        -- meet 场指向 roundtable_sessions
  ref_id       text,
  client_ref   text,
  CONSTRAINT chat_sessions_client_ref_uniq UNIQUE (created_by, client_ref)
);

COMMENT ON TABLE  chat_sessions IS '人机对话台·会话索引层（dm/meet 唯一枚举真源）';
COMMENT ON COLUMN chat_sessions.members IS '成员 id 快照（与 agents.json 的键一致）';
COMMENT ON COLUMN chat_sessions.client_ref IS '幂等键：同 created_by+client_ref 重复建 dm 返回同一条（契约 §4.4）';
COMMENT ON COLUMN chat_sessions.ref_id IS 'meet 场指向 roundtable_sessions.session_id；正文不复制';

CREATE INDEX IF NOT EXISTS chat_sessions_updated_idx ON chat_sessions (updated_at DESC);
CREATE INDEX IF NOT EXISTS chat_sessions_kind_state_idx ON chat_sessions (kind, state, updated_at DESC);

-- ================================================================ 二、消息层
-- append-only。只有"落定"的消息才进来（queued/running 不落库）。
CREATE TABLE IF NOT EXISTS chat_messages (
  message_id   text        PRIMARY KEY,
  session_id   text        NOT NULL REFERENCES chat_sessions(session_id) ON DELETE RESTRICT,
  seq          bigint      NOT NULL,
  author_type  text        NOT NULL CHECK (author_type IN ('human', 'agent', 'system')),
  author_id    text        NOT NULL,
  kind         text        NOT NULL DEFAULT 'text',
  text         text,                                        -- dm：正文；meet：NULL（正文在真源）
  ref_table    text,                                        -- meet 场指向 roundtable_turns
  ref_id       text,
  status       text        NOT NULL CHECK (status IN ('done', 'failed', 'canceled')),
  reply_to     text,
  run          jsonb,
  meta         jsonb,
  created_at   timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT chat_messages_session_seq_uniq UNIQUE (session_id, seq)
);

COMMENT ON TABLE  chat_messages IS '人机对话台·消息层（append-only，只写落定消息）';
COMMENT ON COLUMN chat_messages.seq IS '会话内从 1 严格递增；历史分页与 SSE 断线续推的唯一依据';
COMMENT ON COLUMN chat_messages.text IS 'dm 的正文；meet 场为 NULL，正文从 ref_id 指向的真源投影';

CREATE INDEX IF NOT EXISTS chat_messages_session_seq_idx ON chat_messages (session_id, seq);

-- append-only：任何 UPDATE / DELETE / TRUNCATE 直接拒（事实不许被事后抹掉）。
CREATE OR REPLACE FUNCTION chat_messages_append_only() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'chat_messages is append-only: % is not allowed', TG_OP
    USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS chat_messages_no_update ON chat_messages;
CREATE TRIGGER chat_messages_no_update
  BEFORE UPDATE OR DELETE ON chat_messages
  FOR EACH ROW EXECUTE FUNCTION chat_messages_append_only();

DROP TRIGGER IF EXISTS chat_messages_no_truncate ON chat_messages;
CREATE TRIGGER chat_messages_no_truncate
  BEFORE TRUNCATE ON chat_messages
  FOR EACH STATEMENT EXECUTE FUNCTION chat_messages_append_only();

-- ================================================================ 三、旁听
CREATE TABLE IF NOT EXISTS chat_observers (
  session_id   text        NOT NULL REFERENCES chat_sessions(session_id) ON DELETE CASCADE,
  username     text        NOT NULL,
  joined_at    timestamptz NOT NULL DEFAULT now(),
  left_at      timestamptz,
  note         text,
  PRIMARY KEY (session_id, username)
);

COMMENT ON TABLE chat_observers IS '人机对话台·旁听名单（只读订阅；退出写 left_at，不删行）';

CREATE INDEX IF NOT EXISTS chat_observers_active_idx
  ON chat_observers (session_id) WHERE left_at IS NULL;
