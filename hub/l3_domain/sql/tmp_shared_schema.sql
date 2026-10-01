-- tmp_shared_schema.sql —— 临时共享区（tmp box）的表结构（幂等，可重复执行）
--
-- 唯一依据：临时共享区-设计与契约-v1.md（路径 /tmp、表 tmp_shared、字段名已冻结）。
-- 用法（与黑板 / 圆桌 / 共享内容同一套）：
--   psql "host=127.0.0.1 port=9950 dbname=blackboard user=bb" -f sql/tmp_shared_schema.sql
--   本文件不依赖任何 roundtable_* / shared_* 表，可以单独执行。
--
-- 产品口径（刚哥定的，逐条落到库里）：
--   * Agent 之间只传「简短信息 + 共享信息 id」，正文放这里。
--     所以 tmp_id 必须短（'t-' + 8 字符 base32），短到能塞进 A2A/NATS 载荷。
--   * 与「共享内容平面」（shared_content）、黑板（entries）、会议（roundtable_*）
--     **三套互不相干**，一个字都不掺它们的字段。
--   * id 带**读计数**：读满限定次数（reads_max，按 distinct reader 去重）即销毁；
--     超过限定时长（expires_at）无人读够也自动销毁。
--   * **销毁 ≠ 删行**：destroy = destroyed_at 落时间 + destroy_reason + body 置空。
--     谁写的、谁读过、为什么销毁，全部查得到 —— 删了行「留痕」就不成立。
--
-- 纪律沿用 shared_content_schema.sql：不是应用层「约定」，而是数据库层面拒绝。
--   1. tmp_id / owner / created_at / reads_max / expires_at / bytes / digest 不可变；
--   2. body 只允许**一次单向清空**（别想在「读完之后偷偷改内容」）；
--   3. DELETE / TRUNCATE 一律拒（销毁走 destroyed_at 标记）；
--   4. reads_count / readers 只许单调增（不许回退）。

CREATE TABLE IF NOT EXISTS tmp_shared (
  tmp_id         text        PRIMARY KEY,        -- 't-' + 8 字符 base32 (A-Z2-7)，短到能塞进载荷
  owner          text        NOT NULL,           -- 写入者 = 鉴权结果（防冒名）
  audience       text[]      NOT NULL DEFAULT '{}',  -- 收件人成员名；空数组 = 仅 owner 可取
  title          text,                           -- 可选，便于列表排查
  body           text        NOT NULL,           -- 正文
  bytes          integer     NOT NULL,           -- 正文 UTF-8 字节数
  digest         text        NOT NULL,           -- 正文 sha256，去重用
  reads_max      integer     NOT NULL,           -- 读满销毁阈值 1..100
  reads_count    integer     NOT NULL DEFAULT 0, -- 已读**人数**（distinct reader）
  readers        text[]      NOT NULL DEFAULT '{}', -- 已读成员（去重依据 + 可审计）
  expires_at     timestamptz NOT NULL,           -- 按时长销毁
  created_at     timestamptz NOT NULL DEFAULT now(),
  destroyed_at   timestamptz,                    -- 销毁时间（正文已清空）
  destroy_reason text                            -- read_quota|expired|capacity|owner|schema
);

CREATE INDEX IF NOT EXISTS tmp_shared_expires ON tmp_shared (expires_at) WHERE destroyed_at IS NULL;
CREATE INDEX IF NOT EXISTS tmp_shared_owner   ON tmp_shared (owner, created_at DESC);
CREATE INDEX IF NOT EXISTS tmp_shared_digest  ON tmp_shared (digest) WHERE destroyed_at IS NULL;


-- ================================================================ 一、守卫触发器
-- BEFORE UPDATE：冻结不可变列 + body 单向清空 + 读计数单调增。
-- 非 UPDATE（DELETE / TRUNCATE）直接拒：销毁是标记，不是删行。
CREATE OR REPLACE FUNCTION tmp_shared_guard() RETURNS trigger AS $$
BEGIN
  IF TG_OP <> 'UPDATE' THEN
    RAISE EXCEPTION 'tmp_shared 不允许 %（临时共享区只增不删）', TG_OP
      USING HINT = '销毁请走 body='''' + destroyed_at=now() + destroy_reason（行保留才留得住痕），不要删行';
  END IF;

  IF NEW.tmp_id IS DISTINCT FROM OLD.tmp_id THEN
    RAISE EXCEPTION 'tmp_shared.tmp_id 不可变（id 是调用方手里的那把钥匙，改了等于换了一把）';
  END IF;
  IF NEW.owner IS DISTINCT FROM OLD.owner THEN
    RAISE EXCEPTION 'tmp_shared.owner 不可变（谁写的这条是事实，防冒名）';
  END IF;
  IF NEW.created_at IS DISTINCT FROM OLD.created_at THEN
    RAISE EXCEPTION 'tmp_shared.created_at 不可变';
  END IF;
  IF NEW.reads_max IS DISTINCT FROM OLD.reads_max THEN
    RAISE EXCEPTION 'tmp_shared.reads_max 不可变（改阈值＝绕过读满销毁，要改就新写一条）';
  END IF;
  IF NEW.expires_at IS DISTINCT FROM OLD.expires_at THEN
    RAISE EXCEPTION 'tmp_shared.expires_at 不可变（改到期时间＝绕过超时销毁，要改就新写一条）';
  END IF;
  IF NEW.bytes IS DISTINCT FROM OLD.bytes THEN
    RAISE EXCEPTION 'tmp_shared.bytes 不可变';
  END IF;
  IF NEW.digest IS DISTINCT FROM OLD.digest THEN
    RAISE EXCEPTION 'tmp_shared.digest 不可变';
  END IF;

  -- body：只允许**单向清空一次**。OLD 或 NEW 有一个是空串才放行，且新值只能是空串 ——
  -- 否则「读完之后偷偷改内容」和「销毁后把正文塞回去」两条路都通了。
  IF NEW.body IS DISTINCT FROM OLD.body AND NEW.body IS DISTINCT FROM '' THEN
    RAISE EXCEPTION 'tmp_shared.body 不允许改写（只允许单向清空一次）'
      USING HINT = '正文是写入时的那个快照：要发新内容就新写一条 tmp_id';
  END IF;

  -- 读计数是「已读人数」，只能往上走：回退会让读满销毁永远不成立。
  IF NEW.reads_count < OLD.reads_count THEN
    RAISE EXCEPTION 'tmp_shared.reads_count 只许单调增（% -> %）', OLD.reads_count, NEW.reads_count;
  END IF;
  IF NOT (NEW.readers @> OLD.readers) THEN
    RAISE EXCEPTION 'tmp_shared.readers 只许单调增（不许把已读的人抹掉）';
  END IF;

  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS tmp_shared_immutable_cols ON tmp_shared;
CREATE TRIGGER tmp_shared_immutable_cols
  BEFORE UPDATE OR DELETE ON tmp_shared
  FOR EACH ROW EXECUTE FUNCTION tmp_shared_guard();

DROP TRIGGER IF EXISTS tmp_shared_no_truncate ON tmp_shared;
CREATE TRIGGER tmp_shared_no_truncate
  BEFORE TRUNCATE ON tmp_shared
  FOR EACH STATEMENT EXECUTE FUNCTION tmp_shared_guard();


-- ================================================================ 二、取值约束（幂等补挂）
-- PG16 的 ALTER TABLE ... ADD CONSTRAINT 没有 IF NOT EXISTS，用 DO 块查 pg_constraint。
DO $$
DECLARE
  -- 只在**本表**上找同名约束（conname 全局可重名，光看名字会误判）
  rel oid := 'tmp_shared'::regclass;
BEGIN
  -- 读满销毁阈值 1..100（服务侧也校验，这里再钉一道）
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'tmp_shared_reads_max_ck' AND conrelid = rel) THEN
    ALTER TABLE tmp_shared ADD CONSTRAINT tmp_shared_reads_max_ck
      CHECK (reads_max BETWEEN 1 AND 100);
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'tmp_shared_reads_count_ck' AND conrelid = rel) THEN
    ALTER TABLE tmp_shared ADD CONSTRAINT tmp_shared_reads_count_ck
      CHECK (reads_count >= 0);
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'tmp_shared_bytes_ck' AND conrelid = rel) THEN
    ALTER TABLE tmp_shared ADD CONSTRAINT tmp_shared_bytes_ck CHECK (bytes >= 0);
  END IF;

  -- 销毁的行必须正文为空 + 有销毁时间：把「销毁」钉成库里的事实，
  -- 而不是「服务记得清就清、忘了就留着」。
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'tmp_shared_destroyed_ck' AND conrelid = rel) THEN
    ALTER TABLE tmp_shared ADD CONSTRAINT tmp_shared_destroyed_ck
      CHECK (destroyed_at IS NULL OR body = '');
  END IF;

  -- 销毁原因是封闭枚举：写错一个字，事后按原因排查就查不准了
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'tmp_shared_destroy_reason_ck' AND conrelid = rel) THEN
    ALTER TABLE tmp_shared ADD CONSTRAINT tmp_shared_destroy_reason_ck
      CHECK (destroy_reason IS NULL
             OR destroy_reason IN ('read_quota', 'expired', 'capacity', 'owner', 'schema'));
  END IF;
END
$$;
