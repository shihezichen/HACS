-- msg_log_schema.sql —— 消息可靠投递（落库=送达）的表结构（幂等，可重复执行）
--
-- 唯一依据：消息可靠性与会话-测试用例与接口契约-v1.md（表 msg_log、路径 /msg）。
-- 用法（与黑板 / 圆桌 / 临时共享区同一套）：
--   psql "host=127.0.0.1 port=9950 dbname=blackboard user=bb" -f sql/msg_log_schema.sql
--   本文件不依赖任何 roundtable_* / shared_* / tmp_shared / agent_sessions 表，可单独执行。
--
-- 产品口径（刚哥定的诉求，逐条落到库里）：
--   * 「不要靠心跳，而是靠协议保证。我发送消息后，类似 TCP 一样，对方要反馈
--     '已经收到并且开始处理'，让发送方知道已经送达。」
--     → 答案不是「发送前探活」，而是**发送后确认**：
--         落库成功 = 已送达（中枢持有，对方离线也不丢）
--         acked_at = 对方已收到并开工
--         done_at  = 对方处理完成
--       发送方拿 msg_id 随时 `GET /msg/<id>` 查，不必盲等 135 秒超时。
--   * 现状问题：core NATS 不做持久化（见 /etc/nats/nats.conf 自写注释「不上 JetStream：
--     真相源是黑板(PG)」），收件方离线时消息直接蒸发，上线也收不到。
--     「中枢有冗余 ≠ 消息有持久化」。
--   * 评审后的主干方案：协议从「双向多包 + 重传 + 双向 inbox」缩成
--     **单向写 + 单表 + 单查询**，中枢即真相源。
--
-- 三条不可退让的纪律（架构评审钉死的，全部由**数据库层**执行，不是应用层自觉）：
--   1) from_member 一律由 **Bearer token 反查**写入（与 a2a_svc.py:201、
--      blackboard_svc._auth_peer 同一套），**绝不采信请求体里的 from**。
--      否则「伪造身份」这个已经被堵死的口子会重新打开。
--   2) 时间戳一律由 **PG now()** 单点生成 —— 多机时钟漂移下，客户端时间
--      会让「谁先谁后」彻底失去意义。
--   3) **不静默重跑**：processing 超时只能转 stalled（+ 告警），
--      **不许回到 queued 自动重投**。at-least-once ≠ exactly-once：
--      崩在「写 acked 之前」时重投会把同一条再喂一遍 LLM，产生重复副作用。
--
-- 单一过期权威：expires_at 只在本表。正文的过期归 /tmp 临时共享区，
-- 两套各自管自己的寿命（评审采纳的「砍」：msg_inbox 里不放 expires_at）。

CREATE TABLE IF NOT EXISTS msg_log (
  msg_id       text        PRIMARY KEY,           -- 'm-' + 8 字符 base32，调用方可指定（幂等重试用）
  seq          bigserial   NOT NULL,              -- 排序权威：回放/流水一律按它排，**不按 msg_id 排**
  from_member  text        NOT NULL,              -- 只由 token 反查写入，永不采信 payload
  to_member    text        NOT NULL,              -- 收件成员（点对点）
  session_id   text,                              -- 关联会话（可空：不在会话里的单条消息）
  topic        text        NOT NULL DEFAULT 'general',  -- 与黑板/会议/会话同一套值域，可横跨检索
  body         text,                              -- 短正文（长文本请走 content_ref）
  content_ref  jsonb,                             -- {"tmp_id":"t-XXXXXXXX","bytes":N,"sha256":"..."}
  state        text        NOT NULL DEFAULT 'queued',
  attempts     integer     NOT NULL DEFAULT 0,    -- 投递尝试次数（单调增，诊断用）
  last_error   text,                              -- 最近一次失败原因（诊断用）
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now(),
  acked_at     timestamptz,                       -- 对方「已收到并开始处理」
  done_at      timestamptz,                       -- 对方处理完成
  expires_at   timestamptz NOT NULL,              -- 过期终点（单一权威）
  CONSTRAINT msg_log_state_chk CHECK (state IN ('queued','processing','done','failed','expired','stalled')),
  CONSTRAINT msg_log_body_chk  CHECK (body IS NOT NULL OR content_ref IS NOT NULL),
  CONSTRAINT msg_log_from_chk  CHECK (from_member <> '' AND length(from_member) <= 64),
  CONSTRAINT msg_log_to_chk    CHECK (to_member   <> '' AND length(to_member)   <= 64)
);

-- 补拉：某成员所有还欠着的消息（对方上线时按这个捞）
CREATE INDEX IF NOT EXISTS msg_log_inbox   ON msg_log (to_member, state, created_at);
-- 回放：某会话的全部消息，按 seq 排
CREATE INDEX IF NOT EXISTS msg_log_session ON msg_log (session_id, seq);
-- sweep：只扫还没终态的，别把终态历史一起翻
CREATE INDEX IF NOT EXISTS msg_log_expires ON msg_log (expires_at) WHERE state IN ('queued','processing');
-- 审计：某人发过什么
CREATE INDEX IF NOT EXISTS msg_log_from    ON msg_log (from_member, created_at DESC);


-- ================================================================ 一、守卫触发器
-- BEFORE UPDATE：冻结不可变列 + 校验状态机 + 终态正文只读。
-- 非 UPDATE（DELETE / TRUNCATE）一律拒：留痕靠行，删行就没有痕。
CREATE OR REPLACE FUNCTION msg_log_guard() RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'DELETE' OR TG_OP = 'TRUNCATE' THEN
    RAISE EXCEPTION 'msg_log 不允许 %（消息流水只增不删）', TG_OP
      USING HINT = '要「作废」请把 state 改成 expired/failed（行保留才留得住痕），不要删行';
  END IF;

  IF TG_OP = 'INSERT' THEN
    IF NEW.state <> 'queued' THEN
      RAISE EXCEPTION 'msg_log 新行的 state 必须是 queued（当前 %）', NEW.state
        USING HINT = '状态只能由 UPDATE 推进，不许「落库即完成」地跳步';
    END IF;
    RETURN NEW;
  END IF;

  -- ---- 不可变列 ----
  IF NEW.msg_id IS DISTINCT FROM OLD.msg_id THEN
    RAISE EXCEPTION 'msg_log.msg_id 不可变（它是发送方手里那把钥匙）';
  END IF;
  IF NEW.from_member IS DISTINCT FROM OLD.from_member THEN
    RAISE EXCEPTION 'msg_log.from_member 不可变（谁发的这条是事实，防冒名）';
  END IF;
  IF NEW.to_member IS DISTINCT FROM OLD.to_member THEN
    RAISE EXCEPTION 'msg_log.to_member 不可变（改收件人＝把消息转给别人，要转就新发一条）';
  END IF;
  IF NEW.created_at IS DISTINCT FROM OLD.created_at THEN
    RAISE EXCEPTION 'msg_log.created_at 不可变';
  END IF;
  IF NEW.seq IS DISTINCT FROM OLD.seq THEN
    RAISE EXCEPTION 'msg_log.seq 不可变（排序权威被改，回放顺序就乱了）';
  END IF;
  IF NEW.topic IS DISTINCT FROM OLD.topic THEN
    RAISE EXCEPTION 'msg_log.topic 不可变（要换话题就新发一条）';
  END IF;
  IF NEW.expires_at IS DISTINCT FROM OLD.expires_at THEN
    RAISE EXCEPTION 'msg_log.expires_at 不可变（改到期＝绕过超时，要改就新发一条）';
  END IF;

  -- ---- 状态机：终态是终点，且**不许回到 queued**（这就是「不静默重跑」）----
  IF OLD.state IN ('done', 'failed', 'expired', 'stalled') AND NEW.state IS DISTINCT FROM OLD.state THEN
    RAISE EXCEPTION 'msg_log 状态 % 是终态，不可再变成 %', OLD.state, NEW.state;
  END IF;
  IF NEW.state IS DISTINCT FROM OLD.state THEN
    IF NOT (
         (OLD.state = 'queued'     AND NEW.state IN ('processing','done','failed','expired'))
      OR (OLD.state = 'processing' AND NEW.state IN ('done','failed','expired','stalled'))
    ) THEN
      RAISE EXCEPTION 'msg_log 非法状态转移 % -> %', OLD.state, NEW.state
        USING HINT = 'processing 不许回 queued：那是「自动重投」，会重复副作用，'
                     '超时只能转 stalled 并告警（at-least-once ≠ exactly-once）';
    END IF;
  END IF;

  -- ---- 终态后正文只读（别想「处理完了再偷偷改内容」）----
  IF OLD.state IN ('done', 'failed', 'expired', 'stalled')
     AND (NEW.body IS DISTINCT FROM OLD.body OR NEW.content_ref IS DISTINCT FROM OLD.content_ref) THEN
    RAISE EXCEPTION 'msg_log 终态(%) 的正文不可改写', OLD.state;
  END IF;

  -- ---- attempts 单调增 ----
  IF NEW.attempts < OLD.attempts THEN
    RAISE EXCEPTION 'msg_log.attempts 只许单调增（% -> %）', OLD.attempts, NEW.attempts;
  END IF;

  NEW.updated_at := now();     -- 时间戳单点生成：不给调用方传时间的机会
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS msg_log_guard_trg ON msg_log;
CREATE TRIGGER msg_log_guard_trg
  BEFORE UPDATE ON msg_log
  FOR EACH ROW EXECUTE FUNCTION msg_log_guard();

-- INSERT 也要过守卫（校验「新行必须是 queued」这条跳步规则）
DROP TRIGGER IF EXISTS msg_log_guard_ins ON msg_log;
CREATE TRIGGER msg_log_guard_ins
  BEFORE INSERT ON msg_log
  FOR EACH ROW EXECUTE FUNCTION msg_log_guard();

-- DELETE / TRUNCATE 在语句层就拒（行级触发器拦不住 TRUNCATE）
CREATE OR REPLACE FUNCTION msg_log_no_wipe() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'msg_log 不允许 %（消息流水只增不删）', TG_OP
    USING HINT = '要作废请改 state，不要删行/清表';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS msg_log_no_delete_trg ON msg_log;
CREATE TRIGGER msg_log_no_delete_trg
  BEFORE DELETE ON msg_log
  FOR EACH STATEMENT EXECUTE FUNCTION msg_log_no_wipe();

DROP TRIGGER IF EXISTS msg_log_no_truncate_trg ON msg_log;
CREATE TRIGGER msg_log_no_truncate_trg
  BEFORE TRUNCATE ON msg_log
  FOR EACH STATEMENT EXECUTE FUNCTION msg_log_no_wipe();
