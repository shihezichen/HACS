-- roundtable_schema.sql —— 圆桌会议（发言留痕）的表结构（幂等，可重复执行）
--
-- 用法：
--   psql "host=127.0.0.1 port=9950 dbname=blackboard user=bb" -f sql/roundtable_schema.sql
--
-- 设计口径（一句话）：**发言（turns）不可改不可删；只有会话的状态机列可变。**
--   * roundtable_turns 与 blackboard_entries 同级：纯 append-only，UPDATE / DELETE / TRUNCATE
--     一律被触发器拒掉。发言错了只能再追加一条新发言，不许原地洗稿。
--   * roundtable_sessions 是**状态机**：会议要跑动，state / round_no / ended_at /
--     conclusion_entry / note 必须能改。除这 5 列以外的一切（topic / question /
--     members / moderator / max_rounds / started_at / session_id）一经写入即冻结，
--     由 BEFORE UPDATE 触发器逐列比对拦截 —— 谁也别想事后改会议题目。
--   * DELETE / TRUNCATE 对两张表都一律拒绝。
--
-- 与黑板的关系：每条被采纳的发言都会在 blackboard_entries 里留一条 kind='opinion'
--   的条目，roundtable_turns.entry_id 指过去；会议结论是一条 kind='decision' 条目，
--   roundtable_sessions.conclusion_entry 指过去。两张表都**不加外键约束到黑板**，
--   因为黑板是 append-only 的独立事实源，会议侧只做软引用（引用不到也不该阻塞写入）。

-- ---------------------------------------------------------------- 会话（状态机）
CREATE TABLE IF NOT EXISTS roundtable_sessions (
  session_id        text        PRIMARY KEY,           -- rt-YYYYMMDD-HHMMSS-xxxx 或 uuid4 hex，由调度器生成
  topic             text        NOT NULL,              -- 与黑板条目的 topic 同一个值
  question          text        NOT NULL,              -- 本场要回答的问题（全文）
  moderator         text,                              -- 主持人成员名（出结论的那个）
  members           jsonb       NOT NULL,              -- 成员名数组，如 ["hermes","lobster","top001"]
  max_rounds        int         NOT NULL DEFAULT 4,
  round_no          int         NOT NULL DEFAULT 0,    -- 已完成的轮次
  state             text        NOT NULL DEFAULT 'running',  -- running|converged|timeout|aborted
  started_at        timestamptz NOT NULL DEFAULT now(),
  ended_at          timestamptz,
  conclusion_entry  bigint,                            -- 指向 blackboard_entries.entry_id（软引用）
  note              text                               -- 收尾说明：为什么 timeout / 为什么 aborted
);

CREATE INDEX IF NOT EXISTS rt_sessions_state_started
  ON roundtable_sessions (state, started_at DESC);
CREATE INDEX IF NOT EXISTS rt_sessions_topic_started
  ON roundtable_sessions (topic, started_at DESC);

-- ---------------------------------------------------------------- 发言（append-only）
CREATE TABLE IF NOT EXISTS roundtable_turns (
  turn_id      bigserial   PRIMARY KEY,
  session_id   text        NOT NULL REFERENCES roundtable_sessions (session_id),
  round_no     int         NOT NULL,
  seq          int         NOT NULL,                   -- 同一轮内的发言顺序，从 1 起
  speaker      text        NOT NULL,
  status       text        NOT NULL,                   -- ok|absent|error|skipped
  kind         text,                                   -- 如 opinion
  stance       text,                                   -- support|oppose|neutral|need_info
  vote         text,                                   -- continue|converge
  confidence   real,
  content      text,
  refs         jsonb       NOT NULL DEFAULT '[]'::jsonb,
  entry_id     bigint,                                 -- 对应的黑板条目（软引用）；未入库则为 NULL
  error        text,                                   -- absent/error 的原因，或降级解析的说明
  ts           timestamptz NOT NULL DEFAULT now(),
  elapsed_ms   int,
  prompt_chars int                                     -- 本次发言的 prompt 字符数（用于核省 token）
);

CREATE INDEX IF NOT EXISTS rt_turns_session_round_seq
  ON roundtable_turns (session_id, round_no, seq);
CREATE INDEX IF NOT EXISTS rt_turns_speaker_ts
  ON roundtable_turns (speaker, ts DESC);

-- ---------------------------------------------------------------- append-only 强制
-- 与 sql/blackboard_schema.sql 同一套写法：不靠应用层"约定"，而是数据库层面拒绝。
CREATE OR REPLACE FUNCTION roundtable_turns_append_only() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'roundtable_turns is append-only (rejected % on roundtable_turns)', TG_OP
    USING HINT = '发言不可改不可删：更正靠追加一条新发言，并在 refs 里指回旧 turn/entry';
END;
$$ LANGUAGE plpgsql;

-- DROP + CREATE 而不是 CREATE OR REPLACE TRIGGER：对 PG11+ 都成立，且保持幂等。
DROP TRIGGER IF EXISTS roundtable_turns_no_update ON roundtable_turns;
CREATE TRIGGER roundtable_turns_no_update
  BEFORE UPDATE OR DELETE ON roundtable_turns
  FOR EACH ROW EXECUTE FUNCTION roundtable_turns_append_only();

-- TRUNCATE 绕过行级触发器，单独挡一道（语句级）。
DROP TRIGGER IF EXISTS roundtable_turns_no_truncate ON roundtable_turns;
CREATE TRIGGER roundtable_turns_no_truncate
  BEFORE TRUNCATE ON roundtable_turns
  FOR EACH STATEMENT EXECUTE FUNCTION roundtable_turns_append_only();

-- ---------------------------------------------------------------- 会话：只放行状态机列
-- 允许变的只有这 5 列：state / round_no / ended_at / conclusion_entry / note。
-- 其余任何一列被改动都抛错（IS DISTINCT FROM 顺手把 NULL 的情况一起管了）。
CREATE OR REPLACE FUNCTION roundtable_sessions_guard() RETURNS trigger AS $$
BEGIN
  IF TG_OP <> 'UPDATE' THEN
    RAISE EXCEPTION 'roundtable_sessions 不允许 %（会议记录只增不删）', TG_OP
      USING HINT = '作废一场会议请把 state 改成 aborted 并写 note，不要删行';
  END IF;

  IF NEW.session_id IS DISTINCT FROM OLD.session_id THEN
    RAISE EXCEPTION 'roundtable_sessions.session_id 不可变';
  END IF;
  IF NEW.topic IS DISTINCT FROM OLD.topic THEN
    RAISE EXCEPTION 'roundtable_sessions.topic 不可变';
  END IF;
  IF NEW.question IS DISTINCT FROM OLD.question THEN
    RAISE EXCEPTION 'roundtable_sessions.question 不可变';
  END IF;
  IF NEW.moderator IS DISTINCT FROM OLD.moderator THEN
    RAISE EXCEPTION 'roundtable_sessions.moderator 不可变';
  END IF;
  IF NEW.members IS DISTINCT FROM OLD.members THEN
    RAISE EXCEPTION 'roundtable_sessions.members 不可变（成员被移出只在 turns 与 note 里留痕）';
  END IF;
  IF NEW.max_rounds IS DISTINCT FROM OLD.max_rounds THEN
    RAISE EXCEPTION 'roundtable_sessions.max_rounds 不可变';
  END IF;
  IF NEW.started_at IS DISTINCT FROM OLD.started_at THEN
    RAISE EXCEPTION 'roundtable_sessions.started_at 不可变';
  END IF;

  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS roundtable_sessions_immutable_cols ON roundtable_sessions;
CREATE TRIGGER roundtable_sessions_immutable_cols
  BEFORE UPDATE OR DELETE ON roundtable_sessions
  FOR EACH ROW EXECUTE FUNCTION roundtable_sessions_guard();

DROP TRIGGER IF EXISTS roundtable_sessions_no_truncate ON roundtable_sessions;
CREATE TRIGGER roundtable_sessions_no_truncate
  BEFORE TRUNCATE ON roundtable_sessions
  FOR EACH STATEMENT EXECUTE FUNCTION roundtable_sessions_guard();
