-- shared_content_schema.sql —— 共享内容（任务级 / 永久级）的表结构（幂等，可重复执行）
--
-- 用法（与黑板、圆桌同一套）：
--   psql "host=127.0.0.1 port=9950 dbname=blackboard user=bb" -f sql/shared_content_schema.sql
--   本文件**不依赖**任何一个 roundtable_* 表，可以单独执行；只要求 vector 扩展在。
--
-- 产品口径（刚哥定的，逐条落到库里）：
--   * 共享内容 ≠ 会议：受众与管控都不一样，是两套独立的东西。会议在 roundtable_*，
--     这里一个字都不碰会议表。
--   * **任务级**（scope='task'）：临时共享，有时间限制与销毁条件 —— 因此 task_id 与
--     expires_at 两列在 scope='task' 时是**硬必填**（CHECK 拒，不是应用层约定）；
--     任务结束（shared_tasks.status in (done,aborted)）或过了 ttl_at，
--     这个任务下的共享内容一律销毁。
--   * **永久级**（scope='permanent'）：节点公网 IP、各机装了什么 Agent、主人画像与关键
--     信息……一份共享，不让每个 Agent 各存一份。永久级**不许挂 task_id**（CHECK 拒），
--     否则任务一结束就把常驻信息连坐销毁了。
--   * 两种都可以改 / 刷新（信息会变）：改动由 AFTER 触发器自动写 shared_content_revisions，
--     谁、何时、改了哪些字段，全部留痕，不靠调用方自觉。
--   * 会过期，也可以被"读完就撤下"：status 走 active|expired|revoked|destroyed 四态。
--   * **销毁 ≠ 删行**：destroy = status='destroyed' + body 置空 + destroyed_at 落时间 +
--     留一条 revision。行本身保留 —— 否则"留痕"和"销毁"互相矛盾：删了行，谁销毁的
--     也就查不出来了。正文不留残：revision 的 changed 里对 body 只记"已变更"，不记值。
--
-- 纪律沿用黑板那套：不是应用层"约定"，而是数据库层面拒绝。
--   shared_tasks / shared_content：DELETE、TRUNCATE 一律拒（销毁走 destroyed_at 标记）；
--   shared_content_revisions：UPDATE、DELETE、TRUNCATE 一律拒（append-only，留痕不可改）。

CREATE EXTENSION IF NOT EXISTS vector;


-- ================================================================ 一、任务容器
-- 任务级共享内容的生命周期由它决定：任务结束 / 过了兜底 TTL，内容就该销毁。

CREATE TABLE IF NOT EXISTS shared_tasks (
  task_id      text        PRIMARY KEY,
  title        text        NOT NULL,
  owner        text        NOT NULL,
  status       text        NOT NULL DEFAULT 'active',  -- active|done|aborted
  note         text,
  created_at   timestamptz NOT NULL DEFAULT now(),
  ended_at     timestamptz,
  ttl_at       timestamptz,                            -- 兜底过期：到点即使没人标结束也自动销毁
  destroyed_at timestamptz                             -- 这个任务下的内容已经清干净的时间
);

CREATE INDEX IF NOT EXISTS shared_tasks_status_created
  ON shared_tasks (status, created_at DESC);
CREATE INDEX IF NOT EXISTS shared_tasks_ttl
  ON shared_tasks (ttl_at);

-- 冻结 task_id / title / owner / created_at；放行 status / ended_at / ttl_at / note / destroyed_at
CREATE OR REPLACE FUNCTION shared_tasks_guard() RETURNS trigger AS $$
BEGIN
  IF TG_OP <> 'UPDATE' THEN
    RAISE EXCEPTION 'shared_tasks 不允许 %（任务只增不删）', TG_OP
      USING HINT = '任务作废请把 status 改成 aborted 并写 note；内容的销毁走 shared_content.destroyed_at';
  END IF;

  IF NEW.task_id IS DISTINCT FROM OLD.task_id THEN
    RAISE EXCEPTION 'shared_tasks.task_id 不可变';
  END IF;
  IF NEW.title IS DISTINCT FROM OLD.title THEN
    RAISE EXCEPTION 'shared_tasks.title 不可变（改口径请新开一个任务并在 note 里指回）';
  END IF;
  IF NEW.owner IS DISTINCT FROM OLD.owner THEN
    RAISE EXCEPTION 'shared_tasks.owner 不可变（谁发起的任务是事实）';
  END IF;
  IF NEW.created_at IS DISTINCT FROM OLD.created_at THEN
    RAISE EXCEPTION 'shared_tasks.created_at 不可变';
  END IF;

  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS shared_tasks_immutable_cols ON shared_tasks;
CREATE TRIGGER shared_tasks_immutable_cols
  BEFORE UPDATE OR DELETE ON shared_tasks
  FOR EACH ROW EXECUTE FUNCTION shared_tasks_guard();

DROP TRIGGER IF EXISTS shared_tasks_no_truncate ON shared_tasks;
CREATE TRIGGER shared_tasks_no_truncate
  BEFORE TRUNCATE ON shared_tasks
  FOR EACH STATEMENT EXECUTE FUNCTION shared_tasks_guard();


-- ================================================================ 二、共享内容主体

CREATE TABLE IF NOT EXISTS shared_content (
  content_id  bigserial   PRIMARY KEY,
  scope       text        NOT NULL,                     -- task|permanent
  task_id     text        REFERENCES shared_tasks (task_id),  -- task 必填；permanent 必须为 NULL
  kind        text        NOT NULL DEFAULT 'note',      -- node|agent|owner_profile|owner_key_info|config|note|...
  title       text        NOT NULL,
  body        text        NOT NULL,                     -- Markdown / 纯文本；destroy 后置空串
  tags        jsonb       NOT NULL DEFAULT '[]'::jsonb,
  owner       text        NOT NULL,                     -- 维护责任人（信息变了由他刷新）
  audience    jsonb       NOT NULL DEFAULT '["all"]'::jsonb,  -- ["all"] 或成员名列表
  sensitivity text        NOT NULL DEFAULT 'internal',  -- public|internal|sensitive
  status      text        NOT NULL DEFAULT 'active',    -- active|expired|revoked|destroyed
  expires_at  timestamptz,                              -- 过期时间（scope='task' 必填）
  review_at   timestamptz,                              -- 复审时间：到点提醒 owner 刷新（信息会变）
  version     int         NOT NULL DEFAULT 1,
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now(),
  updated_by  text,
  destroyed_at timestamptz,
  -- source_refs：这条共享内容是从哪来的（黑板条目 / 会议 / 文档），给"顺藤摸瓜"用。
  -- 只读 API 的响应契约里有这个字段（D2 看板按它写），所以它必须是一等列。
  source_refs jsonb       NOT NULL DEFAULT '[]'::jsonb,
  -- 维度必须与 embedder.embed_dim() 和 blackboard_entries.embedding 一致。
  -- 本仓库的真相是 **512**：模型 bge-small-zh-v1.5 出 512 维，
  -- sql/p4_embedding_migration.sql 已把黑板列迁到 vector(512)。
  -- 若哪天全队换成 384 维的模型（并同步改 BB_EMBED_DIM），改法是：
  --   ALTER TABLE shared_content ALTER COLUMN embedding TYPE vector(384);
  -- 三处（模型 / 环境变量 / 列）必须同时改，只改一处必然写不进去。
  embedding   vector(512)
);

-- 新库一步到位；老库（如果先建过没有 source_refs 的版本）也能补上
ALTER TABLE shared_content ADD COLUMN IF NOT EXISTS source_refs jsonb NOT NULL DEFAULT '[]'::jsonb;

CREATE INDEX IF NOT EXISTS shared_content_scope_status
  ON shared_content (scope, status);
CREATE INDEX IF NOT EXISTS shared_content_kind
  ON shared_content (kind);
CREATE INDEX IF NOT EXISTS shared_content_owner
  ON shared_content (owner);
CREATE INDEX IF NOT EXISTS shared_content_expires_at
  ON shared_content (expires_at);
CREATE INDEX IF NOT EXISTS shared_content_review_at
  ON shared_content (review_at);
CREATE INDEX IF NOT EXISTS shared_content_task_id
  ON shared_content (task_id);
CREATE INDEX IF NOT EXISTS shared_content_tags_gin
  ON shared_content USING gin (tags);
-- audience 也走 gin：只读 API 每一条查询都带 `audience @> '["all"]' OR audience @> '["某人"]'`
CREATE INDEX IF NOT EXISTS shared_content_audience_gin
  ON shared_content USING gin (audience);

-- HNSW（余弦距离），与 P4 同法。这里是新表，一开始就建是对的：
-- pgvector 会把之后 INSERT 的行自动纳入索引，只有"先灌历史数据再建索引"那种场景
-- 才必须等 backfill 完（见 sql/p4_hnsw_index.sql 的注释）。
SET maintenance_work_mem = '128MB';   -- 2 核小机，别开太大
CREATE INDEX IF NOT EXISTS shared_content_embedding_hnsw
  ON shared_content USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);


-- ================================================================ 三、变更留痕（append-only）

CREATE TABLE IF NOT EXISTS shared_content_revisions (
  rev_id     bigserial   PRIMARY KEY,
  content_id bigint      NOT NULL,
  action     text        NOT NULL,                      -- create|update|revoke|expire|destroy|restore
  actor      text        NOT NULL,
  -- 字段级 diff：{"title": {"from": "旧", "to": "新"}}。
  -- **正文与敏感内容只记"已变更"不记值**：{"body": {"changed": true}}
  changed    jsonb       NOT NULL DEFAULT '{}'::jsonb,
  note       text,
  ts         timestamptz NOT NULL DEFAULT now()
);
-- 刻意**不加**外键到 shared_content：留痕的生命周期比内容长，将来真要归档内容行，
-- 痕迹也不该被级联带走。

CREATE INDEX IF NOT EXISTS shared_revisions_content_ts
  ON shared_content_revisions (content_id, ts DESC);
CREATE INDEX IF NOT EXISTS shared_revisions_actor_ts
  ON shared_content_revisions (actor, ts DESC);
CREATE INDEX IF NOT EXISTS shared_revisions_action_ts
  ON shared_content_revisions (action, ts DESC);

CREATE OR REPLACE FUNCTION shared_revisions_append_only() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'shared_content_revisions is append-only (rejected % on shared_content_revisions)', TG_OP
    USING HINT = '留痕不可改不可删：记错了请再改一次内容，会自动追加一条新的 revision';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS shared_revisions_no_update ON shared_content_revisions;
CREATE TRIGGER shared_revisions_no_update
  BEFORE UPDATE OR DELETE ON shared_content_revisions
  FOR EACH ROW EXECUTE FUNCTION shared_revisions_append_only();

DROP TRIGGER IF EXISTS shared_revisions_no_truncate ON shared_content_revisions;
CREATE TRIGGER shared_revisions_no_truncate
  BEFORE TRUNCATE ON shared_content_revisions
  FOR EACH STATEMENT EXECUTE FUNCTION shared_revisions_append_only();


-- ================================================================ 四、内容的冻结列 + 自动留痕

-- BEFORE UPDATE：冻结 content_id / scope / task_id / created_at。
-- scope 与 task_id 冻死的理由：一条"任务级"内容不许中途改成永久级（等于绕过销毁条件），
-- 反过来也不许把永久内容挂到某个任务上（任务一结束就被连坐销毁）。要改口径就新发一条。
CREATE OR REPLACE FUNCTION shared_content_guard() RETURNS trigger AS $$
BEGIN
  IF TG_OP <> 'UPDATE' THEN
    RAISE EXCEPTION 'shared_content 不允许 %（共享内容只增不删）', TG_OP
      USING HINT = '撤下请 status=revoked；销毁请 status=destroyed + body 置空 + destroyed_at 落时间，不要删行';
  END IF;

  IF NEW.content_id IS DISTINCT FROM OLD.content_id THEN
    RAISE EXCEPTION 'shared_content.content_id 不可变';
  END IF;
  IF NEW.scope IS DISTINCT FROM OLD.scope THEN
    RAISE EXCEPTION 'shared_content.scope 不可变（任务级与永久级的销毁条件不同，改口径请新发一条）';
  END IF;
  IF NEW.task_id IS DISTINCT FROM OLD.task_id THEN
    RAISE EXCEPTION 'shared_content.task_id 不可变（挂哪个任务决定它什么时候被销毁）';
  END IF;
  IF NEW.created_at IS DISTINCT FROM OLD.created_at THEN
    RAISE EXCEPTION 'shared_content.created_at 不可变';
  END IF;

  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS shared_content_immutable_cols ON shared_content;
CREATE TRIGGER shared_content_immutable_cols
  BEFORE UPDATE OR DELETE ON shared_content
  FOR EACH ROW EXECUTE FUNCTION shared_content_guard();

DROP TRIGGER IF EXISTS shared_content_no_truncate ON shared_content;
CREATE TRIGGER shared_content_no_truncate
  BEFORE TRUNCATE ON shared_content
  FOR EACH STATEMENT EXECUTE FUNCTION shared_content_guard();

-- ---------------------------------------------------------------- 自动留痕
-- AFTER INSERT/UPDATE：写一条 shared_content_revisions。**调用方不用自己记**，
-- 也就没法"改了不记"。
--
-- 三个口径：
--   * actor 取 NEW.updated_by（CLI 的 --by 写进这一列），INSERT 时退到 owner；
--     实在没有就记 current_user —— 宁可记个库账号，也不留空。
--   * note 取会话级变量 shared_content.note（CLI 在同一条连接上先 set_config 再写），
--     没设就是 NULL。这样"为什么改"跟着这次改动走，不用往业务表加列。
--   * changed 排除 embedding / updated_at / version 三个噪音字段：向量是派生物，
--     后两个每次必变，记了只会淹没真正的改动。
CREATE OR REPLACE FUNCTION shared_content_revision() RETURNS trigger AS $$
DECLARE
  old_j     jsonb;
  new_j     jsonb;
  diff      jsonb := '{}'::jsonb;
  key_name  text;
  act       text;
  hide      boolean;
  actor     text;
  why       text;
BEGIN
  why := NULLIF(current_setting('shared_content.note', true), '');

  IF TG_OP = 'INSERT' THEN
    INSERT INTO shared_content_revisions (content_id, action, actor, changed, note)
    VALUES (NEW.content_id, 'create',
            COALESCE(NULLIF(NEW.updated_by, ''), NEW.owner, current_user),
            jsonb_build_object('scope', NEW.scope, 'kind', NEW.kind,
                               'title', CASE WHEN NEW.sensitivity = 'sensitive'
                                             THEN '（敏感，不记值）' ELSE NEW.title END,
                               'status', NEW.status),
            why);
    RETURN NULL;
  END IF;

  old_j := to_jsonb(OLD) - 'embedding' - 'updated_at' - 'version';
  new_j := to_jsonb(NEW) - 'embedding' - 'updated_at' - 'version';
  -- 敏感内容：改前改后只要有一边是 sensitive，就整条只记"已变更"，一个值都不落痕迹表
  hide := (NEW.sensitivity = 'sensitive' OR OLD.sensitivity = 'sensitive');

  FOR key_name IN SELECT jsonb_object_keys(old_j || new_j) LOOP
    IF (old_j -> key_name) IS DISTINCT FROM (new_j -> key_name) THEN
      IF hide OR key_name = 'body' THEN
        -- 正文一律只记"已变更"：一来正文可能很长，二来销毁后正文绝不能在痕迹里留残
        diff := diff || jsonb_build_object(key_name, jsonb_build_object('changed', true));
      ELSE
        diff := diff || jsonb_build_object(
          key_name, jsonb_build_object('from', old_j -> key_name, 'to', new_j -> key_name));
      END IF;
    END IF;
  END LOOP;

  -- 什么都没改（只是 updated_at/version 动了）就不留痕，免得历史里全是空条目
  IF diff = '{}'::jsonb THEN
    RETURN NULL;
  END IF;

  act := CASE
           WHEN NEW.status = 'destroyed' AND OLD.status IS DISTINCT FROM 'destroyed' THEN 'destroy'
           WHEN NEW.status = 'revoked'   AND OLD.status IS DISTINCT FROM 'revoked'   THEN 'revoke'
           WHEN NEW.status = 'expired'   AND OLD.status IS DISTINCT FROM 'expired'   THEN 'expire'
           WHEN NEW.status = 'active'    AND OLD.status IS DISTINCT FROM 'active'    THEN 'restore'
           ELSE 'update'
         END;
  actor := COALESCE(NULLIF(NEW.updated_by, ''), current_user);

  INSERT INTO shared_content_revisions (content_id, action, actor, changed, note)
  VALUES (NEW.content_id, act, actor, diff, why);
  RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS shared_content_revision_log ON shared_content;
CREATE TRIGGER shared_content_revision_log
  AFTER INSERT OR UPDATE ON shared_content
  FOR EACH ROW EXECUTE FUNCTION shared_content_revision();


-- ================================================================ 五、取值约束（幂等补挂）
-- PG16 的 ALTER TABLE ... ADD CONSTRAINT 没有 IF NOT EXISTS，用 DO 块查 pg_constraint。
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'shared_tasks_status_ck') THEN
    ALTER TABLE shared_tasks ADD CONSTRAINT shared_tasks_status_ck
      CHECK (status IN ('active', 'done', 'aborted'));
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'shared_content_scope_ck') THEN
    ALTER TABLE shared_content ADD CONSTRAINT shared_content_scope_ck
      CHECK (scope IN ('task', 'permanent'));
  END IF;

  -- 任务级的两条硬口径写在一个 CHECK 里：必须挂任务、必须有过期时间；
  -- 永久级则必须**不**挂任务。少一样就写不进去 —— 这是"任务结束即销毁"能成立的前提。
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'shared_content_scope_shape_ck') THEN
    ALTER TABLE shared_content ADD CONSTRAINT shared_content_scope_shape_ck
      CHECK ((scope = 'task' AND task_id IS NOT NULL AND expires_at IS NOT NULL)
          OR (scope = 'permanent' AND task_id IS NULL));
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'shared_content_sensitivity_ck') THEN
    ALTER TABLE shared_content ADD CONSTRAINT shared_content_sensitivity_ck
      CHECK (sensitivity IN ('public', 'internal', 'sensitive'));
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'shared_content_status_ck') THEN
    ALTER TABLE shared_content ADD CONSTRAINT shared_content_status_ck
      CHECK (status IN ('active', 'expired', 'revoked', 'destroyed'));
  END IF;

  -- tags / audience / source_refs 必须是 JSON 数组：写成对象或裸串，检索侧的
  -- `@>` 判定会变成"看着像能用其实筛不着"，不如落库就拒。
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'shared_content_jsonb_arrays_ck') THEN
    ALTER TABLE shared_content ADD CONSTRAINT shared_content_jsonb_arrays_ck
      CHECK (jsonb_typeof(tags) = 'array'
         AND jsonb_typeof(audience) = 'array'
         AND jsonb_typeof(source_refs) = 'array');
  END IF;

  -- 销毁的行必须正文为空 + 有销毁时间：把"销毁"这件事钉成库里的事实，
  -- 而不是"CLI 记得清就清、忘了就留着"。
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'shared_content_destroyed_ck') THEN
    ALTER TABLE shared_content ADD CONSTRAINT shared_content_destroyed_ck
      CHECK (status <> 'destroyed' OR (body = '' AND destroyed_at IS NOT NULL));
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'shared_revisions_action_ck') THEN
    ALTER TABLE shared_content_revisions ADD CONSTRAINT shared_revisions_action_ck
      CHECK (action IN ('create', 'update', 'revoke', 'expire', 'destroy', 'restore'));
  END IF;
END
$$;
