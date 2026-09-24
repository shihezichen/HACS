-- roundtable_ops_schema.sql —— 圆桌会议的「四要素」扩展：目标 / 议程 / 当场确认 / 落实
--                              （幂等、非破坏，可重复执行）
--
-- 用法（**必须先跑 roundtable_schema.sql，再跑本文件**）：
--   psql "host=127.0.0.1 port=9950 dbname=blackboard user=bb" -f sql/roundtable_schema.sql
--   psql "host=127.0.0.1 port=9950 dbname=blackboard user=bb" -f sql/roundtable_ops_schema.sql
--
-- 为什么有先后：本文件用 CREATE OR REPLACE 重写了 roundtable_sessions_guard()，
--   在原来 7 列冻结的基础上再冻结 goal / agenda。反过来跑会把 goal/agenda 的冻结冲掉。
--
-- 落地的是刚哥那句「会议有目标，会前有议程，会中有结论，会有落实」：
--   * 目标 goal / 议程 agenda —— **会前写死**，进冻结列，开会中途谁也别想改口径；
--   * 议程项 roundtable_agenda_items —— 逐项要结论，只有结论侧的列可变；
--   * 当场确认 roundtable_confirmations —— 纯 append-only，跟发言同级：
--     谁在什么时候投了 agree/object/abstain 是事实，不可改不可删，改口只能再投一条；
--   * 落实 roundtable_actions —— 责任人与截止时间是承诺，进冻结列；只有执行状态可推进；
--   * 遗留问题 roundtable_issues —— 跨会跟踪，首次提出的会议与标题冻结，跟踪状态可变。
--
-- 纪律沿用黑板那套：不靠应用层"约定"，而是数据库层面拒绝。DELETE / TRUNCATE 对
--   本文件新增的四张表一律拒绝 —— 会议痕迹只增不删。

-- ================================================================ 一、会话新增四列

-- 会前必填：本场会议要达成什么（冻结）
ALTER TABLE roundtable_sessions ADD COLUMN IF NOT EXISTS goal text;
-- 会前议程快照 [{"seq":1,"title":"..","expected":".."}]（冻结）
ALTER TABLE roundtable_sessions ADD COLUMN IF NOT EXISTS agenda jsonb;
-- 总体结论当场确认通过的时间（放行列：会议跑动中回写）
ALTER TABLE roundtable_sessions ADD COLUMN IF NOT EXISTS confirmed_at timestamptz;
-- {"agree":[],"object":[],"abstain":[],"absent":[],"required":2,"passed":true}（放行列）
ALTER TABLE roundtable_sessions ADD COLUMN IF NOT EXISTS confirm_summary jsonb;

CREATE INDEX IF NOT EXISTS rt_sessions_confirmed_at
  ON roundtable_sessions (confirmed_at DESC);

-- ---------------------------------------------------------------- 会话 guard（重写）
-- 放行列一共 7 个：state / round_no / ended_at / conclusion_entry / note
--                  + confirmed_at / confirm_summary
-- 冻结列一共 9 个：session_id / topic / question / moderator / members / max_rounds /
--                  started_at + goal / agenda
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
  IF NEW.goal IS DISTINCT FROM OLD.goal THEN
    RAISE EXCEPTION 'roundtable_sessions.goal 不可变（会议目标会前定死，开会途中不许改口）';
  END IF;
  IF NEW.agenda IS DISTINCT FROM OLD.agenda THEN
    RAISE EXCEPTION 'roundtable_sessions.agenda 不可变（议程是会前快照；逐项结论写 roundtable_agenda_items）';
  END IF;

  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- 触发器本身在 roundtable_schema.sql 里已建（roundtable_sessions_immutable_cols /
-- roundtable_sessions_no_truncate），这里只换函数体；重建一遍保证单独执行本文件也成立。
DROP TRIGGER IF EXISTS roundtable_sessions_immutable_cols ON roundtable_sessions;
CREATE TRIGGER roundtable_sessions_immutable_cols
  BEFORE UPDATE OR DELETE ON roundtable_sessions
  FOR EACH ROW EXECUTE FUNCTION roundtable_sessions_guard();

DROP TRIGGER IF EXISTS roundtable_sessions_no_truncate ON roundtable_sessions;
CREATE TRIGGER roundtable_sessions_no_truncate
  BEFORE TRUNCATE ON roundtable_sessions
  FOR EACH STATEMENT EXECUTE FUNCTION roundtable_sessions_guard();


-- ================================================================ 二、议程项（逐项要结论）

CREATE TABLE IF NOT EXISTS roundtable_agenda_items (
  item_id          bigserial   PRIMARY KEY,
  session_id       text        NOT NULL REFERENCES roundtable_sessions (session_id),
  seq              int         NOT NULL,            -- 议程序号，从 1 起
  title            text        NOT NULL,            -- 议程项标题（冻结）
  expected         text        NOT NULL,            -- 预期产出（冻结）
  conclusion       text,                            -- 该项结论（会后写入，可变）
  conclusion_entry bigint,                          -- 对应黑板 decision 条目（软引用，可变）
  status           text        NOT NULL DEFAULT 'pending',  -- pending|concluded|deferred
  note             text,
  UNIQUE (session_id, seq)
);

CREATE INDEX IF NOT EXISTS rt_agenda_items_status
  ON roundtable_agenda_items (status, session_id);
CREATE INDEX IF NOT EXISTS rt_agenda_items_session_seq
  ON roundtable_agenda_items (session_id, seq);

-- 放行 conclusion / conclusion_entry / status / note，其余列改即报错
CREATE OR REPLACE FUNCTION roundtable_agenda_items_guard() RETURNS trigger AS $$
BEGIN
  IF TG_OP <> 'UPDATE' THEN
    RAISE EXCEPTION 'roundtable_agenda_items 不允许 %（议程只增不删）', TG_OP
      USING HINT = '某项不谈了请把 status 改成 deferred 并写 note，不要删行';
  END IF;

  IF NEW.item_id IS DISTINCT FROM OLD.item_id THEN
    RAISE EXCEPTION 'roundtable_agenda_items.item_id 不可变';
  END IF;
  IF NEW.session_id IS DISTINCT FROM OLD.session_id THEN
    RAISE EXCEPTION 'roundtable_agenda_items.session_id 不可变';
  END IF;
  IF NEW.seq IS DISTINCT FROM OLD.seq THEN
    RAISE EXCEPTION 'roundtable_agenda_items.seq 不可变';
  END IF;
  IF NEW.title IS DISTINCT FROM OLD.title THEN
    RAISE EXCEPTION 'roundtable_agenda_items.title 不可变（议程会前定死）';
  END IF;
  IF NEW.expected IS DISTINCT FROM OLD.expected THEN
    RAISE EXCEPTION 'roundtable_agenda_items.expected 不可变（预期产出会前定死）';
  END IF;

  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS roundtable_agenda_items_immutable_cols ON roundtable_agenda_items;
CREATE TRIGGER roundtable_agenda_items_immutable_cols
  BEFORE UPDATE OR DELETE ON roundtable_agenda_items
  FOR EACH ROW EXECUTE FUNCTION roundtable_agenda_items_guard();

DROP TRIGGER IF EXISTS roundtable_agenda_items_no_truncate ON roundtable_agenda_items;
CREATE TRIGGER roundtable_agenda_items_no_truncate
  BEFORE TRUNCATE ON roundtable_agenda_items
  FOR EACH STATEMENT EXECUTE FUNCTION roundtable_agenda_items_guard();


-- ================================================================ 三、当场确认（append-only）

CREATE TABLE IF NOT EXISTS roundtable_confirmations (
  confirm_id bigserial   PRIMARY KEY,
  session_id text        NOT NULL REFERENCES roundtable_sessions (session_id),
  target     text        NOT NULL DEFAULT 'overall',  -- 'overall' 或 'agenda:<seq>'
  member     text        NOT NULL,
  verdict    text        NOT NULL,                    -- agree|object|abstain|absent
  note       text,
  ts         timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS rt_confirmations_session_target
  ON roundtable_confirmations (session_id, target);
CREATE INDEX IF NOT EXISTS rt_confirmations_member_ts
  ON roundtable_confirmations (member, ts DESC);
CREATE INDEX IF NOT EXISTS rt_confirmations_verdict
  ON roundtable_confirmations (verdict, ts DESC);

-- 投过的票就是事实：UPDATE / DELETE / TRUNCATE 全拒，改口只能再投一条
CREATE OR REPLACE FUNCTION roundtable_confirmations_append_only() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'roundtable_confirmations is append-only (rejected % on roundtable_confirmations)', TG_OP
    USING HINT = '确认结果不可改不可删：改口请追加一条新的确认记录';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS roundtable_confirmations_no_update ON roundtable_confirmations;
CREATE TRIGGER roundtable_confirmations_no_update
  BEFORE UPDATE OR DELETE ON roundtable_confirmations
  FOR EACH ROW EXECUTE FUNCTION roundtable_confirmations_append_only();

DROP TRIGGER IF EXISTS roundtable_confirmations_no_truncate ON roundtable_confirmations;
CREATE TRIGGER roundtable_confirmations_no_truncate
  BEFORE TRUNCATE ON roundtable_confirmations
  FOR EACH STATEMENT EXECUTE FUNCTION roundtable_confirmations_append_only();


-- ================================================================ 四、落实事项（必须有人有期限）

CREATE TABLE IF NOT EXISTS roundtable_actions (
  action_id  bigserial   PRIMARY KEY,
  session_id text        NOT NULL REFERENCES roundtable_sessions (session_id),
  agenda_seq int,                                   -- 关联议程项（可空；冻结）
  what       text        NOT NULL,                  -- 要做的事（冻结）
  owner      text        NOT NULL,                  -- 责任人（冻结；必须是 roster 内成员）
  due_at     timestamptz NOT NULL,                  -- 截止时间（冻结；必须晚于 created_at）
  status     text        NOT NULL DEFAULT 'open',   -- open|doing|done|dropped
  evidence   text,
  note       text,
  created_at timestamptz NOT NULL DEFAULT now(),
  closed_at  timestamptz,
  closed_by  text
);

CREATE INDEX IF NOT EXISTS rt_actions_status_due
  ON roundtable_actions (status, due_at);
CREATE INDEX IF NOT EXISTS rt_actions_owner_status
  ON roundtable_actions (owner, status, due_at);
CREATE INDEX IF NOT EXISTS rt_actions_session_created
  ON roundtable_actions (session_id, created_at DESC);

-- 冻结 session_id/agenda_seq/what/owner/due_at/created_at；放行 status/evidence/note/closed_at/closed_by
CREATE OR REPLACE FUNCTION roundtable_actions_guard() RETURNS trigger AS $$
BEGIN
  IF TG_OP <> 'UPDATE' THEN
    RAISE EXCEPTION 'roundtable_actions 不允许 %（承诺只增不删）', TG_OP
      USING HINT = '不做了请把 status 改成 dropped 并写 note，不要删行';
  END IF;

  IF NEW.action_id IS DISTINCT FROM OLD.action_id THEN
    RAISE EXCEPTION 'roundtable_actions.action_id 不可变';
  END IF;
  IF NEW.session_id IS DISTINCT FROM OLD.session_id THEN
    RAISE EXCEPTION 'roundtable_actions.session_id 不可变';
  END IF;
  IF NEW.agenda_seq IS DISTINCT FROM OLD.agenda_seq THEN
    RAISE EXCEPTION 'roundtable_actions.agenda_seq 不可变';
  END IF;
  IF NEW.what IS DISTINCT FROM OLD.what THEN
    RAISE EXCEPTION 'roundtable_actions.what 不可变（要做的事当场定死）';
  END IF;
  IF NEW.owner IS DISTINCT FROM OLD.owner THEN
    RAISE EXCEPTION 'roundtable_actions.owner 不可变（换人请新开一条 action 并在 note 里指回）';
  END IF;
  IF NEW.due_at IS DISTINCT FROM OLD.due_at THEN
    RAISE EXCEPTION 'roundtable_actions.due_at 不可变（改期请新开一条 action 并在 note 里指回）';
  END IF;
  IF NEW.created_at IS DISTINCT FROM OLD.created_at THEN
    RAISE EXCEPTION 'roundtable_actions.created_at 不可变';
  END IF;

  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS roundtable_actions_immutable_cols ON roundtable_actions;
CREATE TRIGGER roundtable_actions_immutable_cols
  BEFORE UPDATE OR DELETE ON roundtable_actions
  FOR EACH ROW EXECUTE FUNCTION roundtable_actions_guard();

DROP TRIGGER IF EXISTS roundtable_actions_no_truncate ON roundtable_actions;
CREATE TRIGGER roundtable_actions_no_truncate
  BEFORE TRUNCATE ON roundtable_actions
  FOR EACH STATEMENT EXECUTE FUNCTION roundtable_actions_guard();


-- ================================================================ 五、遗留问题跟踪（跨会）

CREATE TABLE IF NOT EXISTS roundtable_issues (
  issue_id      bigserial   PRIMARY KEY,
  session_id    text        NOT NULL,               -- 首次提出的会议 id；手工登记用 'manual'（冻结）
  title         text        NOT NULL,               -- 冻结
  detail        text,
  owner         text,                               -- 跟踪人（可改）
  status        text        NOT NULL DEFAULT 'open',-- open|watching|closed
  due_at        timestamptz,
  next_check_at timestamptz,
  refs          jsonb       NOT NULL DEFAULT '[]'::jsonb,
  created_at    timestamptz NOT NULL DEFAULT now(),
  closed_at     timestamptz,
  closed_by     text,
  note          text
);
-- 刻意**不加**外键到 roundtable_sessions：手工登记允许 session_id='manual'。

CREATE INDEX IF NOT EXISTS rt_issues_status_next_check
  ON roundtable_issues (status, next_check_at);
CREATE INDEX IF NOT EXISTS rt_issues_owner_status
  ON roundtable_issues (owner, status);
CREATE INDEX IF NOT EXISTS rt_issues_status_due
  ON roundtable_issues (status, due_at);
CREATE INDEX IF NOT EXISTS rt_issues_session_created
  ON roundtable_issues (session_id, created_at DESC);

-- 冻结 session_id/title/created_at；放行其余。DELETE/TRUNCATE 拒。
CREATE OR REPLACE FUNCTION roundtable_issues_guard() RETURNS trigger AS $$
BEGIN
  IF TG_OP <> 'UPDATE' THEN
    RAISE EXCEPTION 'roundtable_issues 不允许 %（遗留问题只增不删）', TG_OP
      USING HINT = '问题解决了请把 status 改成 closed 并写 closed_by/note，不要删行';
  END IF;

  IF NEW.issue_id IS DISTINCT FROM OLD.issue_id THEN
    RAISE EXCEPTION 'roundtable_issues.issue_id 不可变';
  END IF;
  IF NEW.session_id IS DISTINCT FROM OLD.session_id THEN
    RAISE EXCEPTION 'roundtable_issues.session_id 不可变（首次提出的会议是事实）';
  END IF;
  IF NEW.title IS DISTINCT FROM OLD.title THEN
    RAISE EXCEPTION 'roundtable_issues.title 不可变（改描述请写 note 或 detail）';
  END IF;
  IF NEW.created_at IS DISTINCT FROM OLD.created_at THEN
    RAISE EXCEPTION 'roundtable_issues.created_at 不可变';
  END IF;

  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS roundtable_issues_immutable_cols ON roundtable_issues;
CREATE TRIGGER roundtable_issues_immutable_cols
  BEFORE UPDATE OR DELETE ON roundtable_issues
  FOR EACH ROW EXECUTE FUNCTION roundtable_issues_guard();

DROP TRIGGER IF EXISTS roundtable_issues_no_truncate ON roundtable_issues;
CREATE TRIGGER roundtable_issues_no_truncate
  BEFORE TRUNCATE ON roundtable_issues
  FOR EACH STATEMENT EXECUTE FUNCTION roundtable_issues_guard();


-- ================================================================ 六、取值约束（幂等补挂）
-- PG16 的 ALTER TABLE ... ADD CONSTRAINT 没有 IF NOT EXISTS，用 DO 块查 pg_constraint。
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'rt_agenda_items_status_ck') THEN
    ALTER TABLE roundtable_agenda_items ADD CONSTRAINT rt_agenda_items_status_ck
      CHECK (status IN ('pending', 'concluded', 'deferred'));
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'rt_confirmations_verdict_ck') THEN
    ALTER TABLE roundtable_confirmations ADD CONSTRAINT rt_confirmations_verdict_ck
      CHECK (verdict IN ('agree', 'object', 'abstain', 'absent'));
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'rt_actions_status_ck') THEN
    ALTER TABLE roundtable_actions ADD CONSTRAINT rt_actions_status_ck
      CHECK (status IN ('open', 'doing', 'done', 'dropped'));
  END IF;

  -- 「必须有期限」的硬口径：截止时间必须晚于创建时间，落库层面挡住"当场就过期"的假承诺
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'rt_actions_due_after_created_ck') THEN
    ALTER TABLE roundtable_actions ADD CONSTRAINT rt_actions_due_after_created_ck
      CHECK (due_at > created_at);
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'rt_issues_status_ck') THEN
    ALTER TABLE roundtable_issues ADD CONSTRAINT rt_issues_status_ck
      CHECK (status IN ('open', 'watching', 'closed'));
  END IF;
END
$$;
