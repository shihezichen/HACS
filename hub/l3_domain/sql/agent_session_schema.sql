-- agent_session_schema.sql —— 「Agent 会话」的两张表（幂等，可重复执行）
--
-- 唯一依据：Agent会话-session-设计与契约-v1.md（表 agent_sessions / agent_session_turns，
-- 路径 /sess，字段名与设计裁决 D1..D8 已冻结）。
-- 用法（与黑板 / 圆桌 / 共享内容 / 临时共享区同一套）：
--   docker exec -i agent-team-pg psql -U bb -d blackboard < sql/agent_session_schema.sql
--   本文件不依赖 roundtable_* / shared_* / tmp_shared，可以单独执行。
--   （归档那一步会往 shared_content 写一份 markdown，那是 shared_content_schema.sql
--    已经建好的表，不在本文件范围内。）
--
-- 产品口径（逐条落到库里，不是应用层"约定"）：
--   * D1 空闲超时而不是绝对超时：判据是 now() - last_turn_at > idle_ttl_s，
--     last_turn_at / turn_count 由**触发器**每轮自动刷新，不靠调用方自觉。
--   * D4 抄会议的表纪律，不复用会议的表：roundtable_* 是"主持人/最大轮数/出席率"语义，
--     掺一起互相污染；这里一张自己的表，字段不掺会议的任何一列。
--   * D6 归档 ≠ 删除：关闭只落 state/closed_at/close_reason/archive_ref，行永远保留；
--     归档后 turn 正文可以单向清空留行（照 tmp_shared.body 的同一条纪律）。
--   * D8 每个 session 必须带 topic（与黑板条目 / 会议 session 同一值域），NOT NULL，
--     缺省 general。
--
-- 两条守卫（照抄 roundtable_turns / tmp_shared 的写法）：
--   1. agent_session_turns：INSERT 只在会话 state='open' 时放行（归档后只读）；
--      UPDATE 只放行"正文单向清空"；DELETE / TRUNCATE 一律拒。seq 由触发器给。
--   2. agent_sessions：session_id / topic / owner / created_at / members 写入后不可变；
--      state 只许 open -> closed|expired（不许回退、不许复活）；turn_count 只增；
--      置终态必须同时带 closed_at 与 close_reason。DELETE / TRUNCATE 一律拒。

-- ================================================================ 一、会话注册表

CREATE TABLE IF NOT EXISTS agent_sessions (
  session_id   text        PRIMARY KEY,          -- 'as-YYYYMMDD-HHMMSS-xxxx'（UTC）
  topic        text        NOT NULL,             -- D8：与黑板条目/会议 session 同一值域，缺省 general
  title        text,                             -- 会话标题（人读用）
  owner        text        NOT NULL,             -- 发起方成员名（一律取鉴权结果，防冒名）
  members      jsonb       NOT NULL DEFAULT '[]'::jsonb,  -- 参与成员数组
  state        text        NOT NULL DEFAULT 'open',       -- open|closed|expired
  created_at   timestamptz NOT NULL DEFAULT now(),
  last_turn_at timestamptz,                      -- 每轮由触发器刷新（D1 空闲判据的依据）
  idle_ttl_s   integer     NOT NULL DEFAULT 1800,-- 空闲超时秒数，60..86400
  turn_count   integer     NOT NULL DEFAULT 0,   -- 轮次计数（触发器维护，只增）
  closed_at    timestamptz,
  close_reason text,                             -- manual|idle|owner
  archive_ref  bigint,                           -- 归档后指向 shared_content.content_id（软引用）
  note         text,                             -- 收尾说明
  CONSTRAINT agent_sessions_state_ck
    CHECK (state IN ('open', 'closed', 'expired')),
  CONSTRAINT agent_sessions_idle_ttl_ck
    CHECK (idle_ttl_s BETWEEN 60 AND 86400),
  CONSTRAINT agent_sessions_turn_count_ck
    CHECK (turn_count >= 0),
  CONSTRAINT agent_sessions_members_ck
    CHECK (jsonb_typeof(members) = 'array'),
  CONSTRAINT agent_sessions_close_reason_ck
    CHECK (close_reason IS NULL OR close_reason IN ('manual', 'idle', 'owner')),
  -- 终态必须有收尾时间与原因：把"到底关没关过"钉成库里的事实
  CONSTRAINT agent_sessions_closed_ck
    CHECK (state = 'open' OR (closed_at IS NOT NULL AND close_reason IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS agent_sessions_topic_created
  ON agent_sessions (topic, created_at DESC);
CREATE INDEX IF NOT EXISTS agent_sessions_state_last_turn
  ON agent_sessions (state, last_turn_at);
CREATE INDEX IF NOT EXISTS agent_sessions_owner_created
  ON agent_sessions (owner, created_at DESC);


-- ================================================================ 二、对话流水（append-only）

CREATE TABLE IF NOT EXISTS agent_session_turns (
  turn_id     bigserial   PRIMARY KEY,
  session_id  text        NOT NULL REFERENCES agent_sessions (session_id),
  seq         integer     NOT NULL,              -- 会话内单调递增（触发器给）
  from_member text,                              -- 谁说的
  to_member   text,                              -- 说给谁
  role        text        NOT NULL,              -- user（对方说的）|assistant（本机回的）|system
  content     text,                              -- 正文；外置（D7）或归档后置空
  content_ref text,                              -- D7：正文落 /tmp 时存 tmp_id
  bytes       integer,                           -- 正文字节数
  ts          timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT agent_session_turns_seq_uk UNIQUE (session_id, seq),
  CONSTRAINT agent_session_turns_role_ck
    CHECK (role IN ('user', 'assistant', 'system')),
  CONSTRAINT agent_session_turns_bytes_ck
    CHECK (bytes IS NULL OR bytes >= 0)
);

CREATE INDEX IF NOT EXISTS agent_session_turns_session_seq_desc
  ON agent_session_turns (session_id, seq DESC);
CREATE INDEX IF NOT EXISTS agent_session_turns_session_ts
  ON agent_session_turns (session_id, ts DESC);

-- 归档后 session 转只读：不能再追加。数据库层面挡住，靠的是 INSERT 时查父会话 state。


-- ================================================================ 三、守卫触发器

-- ---- agent_sessions：冻结列 + 状态机 + 终态形状
CREATE OR REPLACE FUNCTION agent_sessions_guard() RETURNS trigger AS $$
BEGIN
  IF TG_OP <> 'UPDATE' THEN
    RAISE EXCEPTION 'agent_sessions 不允许 %（会话只增不删）', TG_OP
      USING HINT = '关闭请走 state=''closed''|''expired'' + closed_at + close_reason；删了行就查不到"当初为什么关"';
  END IF;

  IF NEW.session_id IS DISTINCT FROM OLD.session_id THEN
    RAISE EXCEPTION 'agent_sessions.session_id 不可变（它是调用方手里那把钥匙）';
  END IF;
  IF NEW.topic IS DISTINCT FROM OLD.topic THEN
    RAISE EXCEPTION 'agent_sessions.topic 不可变（D8：话题决定这次会话归到哪儿）';
  END IF;
  IF NEW.owner IS DISTINCT FROM OLD.owner THEN
    RAISE EXCEPTION 'agent_sessions.owner 不可变（谁开的会话是事实，防冒名）';
  END IF;
  IF NEW.created_at IS DISTINCT FROM OLD.created_at THEN
    RAISE EXCEPTION 'agent_sessions.created_at 不可变';
  END IF;
  IF NEW.members IS DISTINCT FROM OLD.members THEN
    RAISE EXCEPTION 'agent_sessions.members 不可变（开的时候定下的参与人，事后改等于改历史）';
  END IF;

  -- 状态机：只许 open -> closed / expired，不许回退、不许终态之间乱跳、不许复活
  IF NEW.state IS DISTINCT FROM OLD.state THEN
    IF NOT (OLD.state = 'open' AND NEW.state IN ('closed', 'expired')) THEN
      RAISE EXCEPTION 'agent_sessions.state 只允许 open -> closed|expired（% -> %）',
        OLD.state, NEW.state
        USING HINT = '已终态的会话不许复活；要接着聊请新开一个 session（历史在 agent_session_turns 里查得到）';
    END IF;
  END IF;

  -- 轮次计数是触发器维护的事实列：只增不减
  IF NEW.turn_count < OLD.turn_count THEN
    RAISE EXCEPTION 'agent_sessions.turn_count 只许单调增（% -> %）', OLD.turn_count, NEW.turn_count;
  END IF;

  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS agent_sessions_immutable_cols ON agent_sessions;
CREATE TRIGGER agent_sessions_immutable_cols
  BEFORE UPDATE OR DELETE ON agent_sessions
  FOR EACH ROW EXECUTE FUNCTION agent_sessions_guard();

DROP TRIGGER IF EXISTS agent_sessions_no_truncate ON agent_sessions;
CREATE TRIGGER agent_sessions_no_truncate
  BEFORE TRUNCATE ON agent_sessions
  FOR EACH STATEMENT EXECUTE FUNCTION agent_sessions_guard();


-- ---- agent_session_turns：append-only + 关闭后只读 + seq 自动给
CREATE OR REPLACE FUNCTION agent_session_turns_guard() RETURNS trigger AS $$
DECLARE
  st  text;
  nxt integer;
BEGIN
  IF TG_OP = 'DELETE' OR TG_OP = 'TRUNCATE' THEN
    RAISE EXCEPTION 'agent_session_turns 不允许 %（对话流水不可改不可删）', TG_OP
      USING HINT = '归档是"清正文留行"（content=''''），删了行"它当时说过什么"就永远查不到';
  END IF;

  IF TG_OP = 'UPDATE' THEN
    -- 唯一放行的改动：归档时把正文**单向清空**，其余列一个都不许动
    IF NEW.turn_id IS DISTINCT FROM OLD.turn_id
       OR NEW.session_id IS DISTINCT FROM OLD.session_id
       OR NEW.seq IS DISTINCT FROM OLD.seq
       OR NEW.from_member IS DISTINCT FROM OLD.from_member
       OR NEW.to_member IS DISTINCT FROM OLD.to_member
       OR NEW.role IS DISTINCT FROM OLD.role
       OR NEW.content_ref IS DISTINCT FROM OLD.content_ref
       OR NEW.bytes IS DISTINCT FROM OLD.bytes
       OR NEW.ts IS DISTINCT FROM OLD.ts THEN
      RAISE EXCEPTION 'agent_session_turns 的流水列不可变（只允许归档时单向清空 content）';
    END IF;
    IF NEW.content IS DISTINCT FROM OLD.content AND NEW.content IS DISTINCT FROM '' THEN
      RAISE EXCEPTION 'agent_session_turns.content 不允许改写（只允许归档时单向清空一次）'
        USING HINT = '要说新的话请追加一条新 turn；归档后的会话本来就不再收新轮次';
    END IF;
    RETURN NEW;
  END IF;

  -- INSERT：父会话必须存在，且**还开着**（归档后只读，契约 §4.3）
  SELECT s.state INTO st FROM agent_sessions s WHERE s.session_id = NEW.session_id;
  IF st IS NULL THEN
    RAISE EXCEPTION 'agent_session_turns.session_id=% 没有对应的会话', NEW.session_id;
  END IF;
  IF st <> 'open' THEN
    RAISE EXCEPTION '会话 % 已是 % 态，不能再追加轮次（契约 §4.3：归档后只读）', NEW.session_id, st;
  END IF;

  -- seq 由触发器给：同会话并发插入先用事务级 advisory lock 串起来，再取 max+1
  IF NEW.seq IS NULL OR NEW.seq <= 0 THEN
    PERFORM pg_advisory_xact_lock(hashtext('agent_session_turns:' || NEW.session_id));
    SELECT COALESCE(max(t.seq), 0) + 1 INTO nxt
      FROM agent_session_turns t WHERE t.session_id = NEW.session_id;
    NEW.seq := nxt;
  END IF;
  IF NEW.bytes IS NULL THEN
    NEW.bytes := COALESCE(octet_length(NEW.content), 0);
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS agent_session_turns_append_only ON agent_session_turns;
CREATE TRIGGER agent_session_turns_append_only
  BEFORE INSERT OR UPDATE OR DELETE ON agent_session_turns
  FOR EACH ROW EXECUTE FUNCTION agent_session_turns_guard();

DROP TRIGGER IF EXISTS agent_session_turns_no_truncate ON agent_session_turns;
CREATE TRIGGER agent_session_turns_no_truncate
  BEFORE TRUNCATE ON agent_session_turns
  FOR EACH STATEMENT EXECUTE FUNCTION agent_session_turns_guard();


-- ---- D1：每轮自动刷新 last_turn_at / turn_count（不靠调用方自觉）
CREATE OR REPLACE FUNCTION agent_session_turns_touch() RETURNS trigger AS $$
BEGIN
  UPDATE agent_sessions
     SET last_turn_at = now(),
         turn_count   = turn_count + 1
   WHERE session_id = NEW.session_id;
  RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS agent_session_turns_touch_bi ON agent_session_turns;
CREATE TRIGGER agent_session_turns_touch_bi
  AFTER INSERT ON agent_session_turns
  FOR EACH ROW EXECUTE FUNCTION agent_session_turns_touch();


-- ================================================================ 四、取值约束（幂等补挂）
-- 表已经建好的情况下（或从更早的版本升上来），把缺的 CHECK 补上。
-- PG16 的 ALTER TABLE ... ADD CONSTRAINT 没有 IF NOT EXISTS，用 DO 块查 pg_constraint；
-- 只在**本表**上找同名约束（conname 全局可重名，光看名字会误判）。
DO $$
DECLARE
  sess oid := 'agent_sessions'::regclass;
  turn oid := 'agent_session_turns'::regclass;
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint
                 WHERE conname = 'agent_sessions_state_ck' AND conrelid = sess) THEN
    ALTER TABLE agent_sessions ADD CONSTRAINT agent_sessions_state_ck
      CHECK (state IN ('open', 'closed', 'expired'));
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint
                 WHERE conname = 'agent_sessions_idle_ttl_ck' AND conrelid = sess) THEN
    ALTER TABLE agent_sessions ADD CONSTRAINT agent_sessions_idle_ttl_ck
      CHECK (idle_ttl_s BETWEEN 60 AND 86400);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint
                 WHERE conname = 'agent_sessions_turn_count_ck' AND conrelid = sess) THEN
    ALTER TABLE agent_sessions ADD CONSTRAINT agent_sessions_turn_count_ck
      CHECK (turn_count >= 0);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint
                 WHERE conname = 'agent_sessions_members_ck' AND conrelid = sess) THEN
    ALTER TABLE agent_sessions ADD CONSTRAINT agent_sessions_members_ck
      CHECK (jsonb_typeof(members) = 'array');
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint
                 WHERE conname = 'agent_sessions_close_reason_ck' AND conrelid = sess) THEN
    ALTER TABLE agent_sessions ADD CONSTRAINT agent_sessions_close_reason_ck
      CHECK (close_reason IS NULL OR close_reason IN ('manual', 'idle', 'owner'));
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint
                 WHERE conname = 'agent_sessions_closed_ck' AND conrelid = sess) THEN
    ALTER TABLE agent_sessions ADD CONSTRAINT agent_sessions_closed_ck
      CHECK (state = 'open' OR (closed_at IS NOT NULL AND close_reason IS NOT NULL));
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_constraint
                 WHERE conname = 'agent_session_turns_seq_uk' AND conrelid = turn) THEN
    ALTER TABLE agent_session_turns ADD CONSTRAINT agent_session_turns_seq_uk
      UNIQUE (session_id, seq);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint
                 WHERE conname = 'agent_session_turns_role_ck' AND conrelid = turn) THEN
    ALTER TABLE agent_session_turns ADD CONSTRAINT agent_session_turns_role_ck
      CHECK (role IN ('user', 'assistant', 'system'));
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint
                 WHERE conname = 'agent_session_turns_bytes_ck' AND conrelid = turn) THEN
    ALTER TABLE agent_session_turns ADD CONSTRAINT agent_session_turns_bytes_ck
      CHECK (bytes IS NULL OR bytes >= 0);
  END IF;
END
$$;
