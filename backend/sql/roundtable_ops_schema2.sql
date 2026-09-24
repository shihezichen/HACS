-- roundtable_ops_schema2.sql —— 会议计量口径更正 + 缺席开缺陷单 + 每日逾期提醒
--                              （幂等、非破坏，可重复执行）
--
-- 用法（**顺序不能换**）：
--   psql "..." -f sql/roundtable_schema.sql
--   psql "..." -f sql/roundtable_ops_schema.sql
--   psql "..." -f sql/roundtable_ops_schema2.sql        <-- 本文件
--
-- 本文件只做三件事，一件都不碰已有的冻结口径：
--   ① 会话表补 4 个**场次级计量列**（应到 / 在场 / 缺席 / 出席率），全是放行列；
--   ② 遗留问题表补 kind（issue|defect）与 evidence（证据），同样是放行列 ——
--      缺席单要能闭环、能补证据，所以刻意**不进冻结名单**；
--   ③ 新建 reminder_log（每日逾期提醒的发送流水，append-only：UPDATE/DELETE/TRUNCATE 全拒）。
--
-- 口径（刚哥澄清后的最终版，写在这儿免得下次又跑偏）：
--   * **结论确认**的分母 = **点名时在场的人**（本场至少应答过一次点名的成员）；
--     absent（无应答）**不计入分母**，单独记录。判据：无 object 且 agree >= ⌊在场/2⌋+1。
--   * **统计指标叫「出席率」**，分母 = **应到人数**（sessions.members 全集）：
--     attendance_rate = present_count / expected_count。**不要**再出现 absent_rate 这个名字。
--   * 成员都是云主机 / 常驻 Agent，**设计上不允许不在线** —— 所以出席率 < 100%
--     就是异常，必须开 kind='defect' 的单跟踪，必须闭环。

-- ================================================================ 一、会话：场次级计量（放行列）

ALTER TABLE roundtable_sessions ADD COLUMN IF NOT EXISTS expected_count  int;   -- 应到人数（members 长度）
ALTER TABLE roundtable_sessions ADD COLUMN IF NOT EXISTS present_count   int;   -- 在场人数：至少应答过一次点名的人数
ALTER TABLE roundtable_sessions ADD COLUMN IF NOT EXISTS absent_count    int;   -- 缺席人数 = 应到 - 在场
ALTER TABLE roundtable_sessions ADD COLUMN IF NOT EXISTS attendance_rate real;  -- 出席率 = present_count / expected_count

COMMENT ON COLUMN roundtable_sessions.expected_count  IS '应到人数：sessions.members 的长度（原定期望成员全集）';
COMMENT ON COLUMN roundtable_sessions.present_count   IS '在场人数：本场至少应答过一次点名的成员数';
COMMENT ON COLUMN roundtable_sessions.absent_count    IS '缺席人数：应到 - 在场；缺席名单在 confirm_summary.attendance.absent_members';
COMMENT ON COLUMN roundtable_sessions.attendance_rate IS '出席率 = present_count / expected_count（**不是**缺席率）';

-- 这 4 列是**放行列**：roundtable_sessions_guard() 只逐列拒绝冻结列，没点名的列默认放行，
-- 所以这里不需要改 guard 函数。冻结列清单仍旧是 9 个（见 roundtable_ops_schema.sql）。

-- 出席率不达标的场次要能一把捞出来（缺席 = 疑似 Bug，运维要按场查）
CREATE INDEX IF NOT EXISTS rt_sessions_attendance
  ON roundtable_sessions (attendance_rate)
  WHERE attendance_rate IS NOT NULL AND attendance_rate < 1.0;


-- ================================================================ 二、遗留问题：性质 + 证据（放行列）

-- issue  = 普通遗留问题（会上没定的事）
-- defect = 疑似程序缺陷（目前唯一来源：成员点名无应答）
ALTER TABLE roundtable_issues ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'issue';
-- 缺席单的证据：[{"session_id":..,"round_no":..,"点名次数":N,"失败原因":"timeout/无应答","ts":..}]
ALTER TABLE roundtable_issues ADD COLUMN IF NOT EXISTS evidence jsonb NOT NULL DEFAULT '[]'::jsonb;

COMMENT ON COLUMN roundtable_issues.kind     IS 'issue=普通遗留问题；defect=疑似程序缺陷（缺席自动开单）';
COMMENT ON COLUMN roundtable_issues.evidence IS '证据数组：点名时间 / 点名次数 / 失败原因 / 来源会议';

-- kind 与 evidence 同样是**放行列**（defect 要能闭环、要能补证据），guard 不动。

-- 跟踪页签按 "性质 + 状态 + 截止" 捞单，索引照这个顺序建
CREATE INDEX IF NOT EXISTS rt_issues_kind_status_due
  ON roundtable_issues (kind, status, due_at);

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'rt_issues_kind_ck') THEN
    ALTER TABLE roundtable_issues ADD CONSTRAINT rt_issues_kind_ck
      CHECK (kind IN ('issue', 'defect'));
  END IF;
END
$$;


-- ================================================================ 三、提醒流水（append-only）

-- 每日逾期提醒的"发过没有"账本。口径：同一事项**当天只提醒一次**，
-- rt_remind.py 发送前先查当天有没有记录。发过就是发过 —— UPDATE/DELETE/TRUNCATE 全拒。
CREATE TABLE IF NOT EXISTS reminder_log (
  reminder_id bigserial   PRIMARY KEY,
  target_kind text        NOT NULL,          -- action|issue
  target_id   bigint      NOT NULL,          -- roundtable_actions.action_id / roundtable_issues.issue_id
  owner       text,                          -- 被提醒的人（留痕用，可空：事项没写责任人时）
  sent_at     timestamptz NOT NULL DEFAULT now()
);

-- "今天发过没有" 是最热的一条查询：按 (kind, id, 时间倒序) 建索引
CREATE INDEX IF NOT EXISTS reminder_log_target_sent
  ON reminder_log (target_kind, target_id, sent_at DESC);
CREATE INDEX IF NOT EXISTS reminder_log_owner_sent
  ON reminder_log (owner, sent_at DESC);

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'reminder_log_kind_ck') THEN
    ALTER TABLE reminder_log ADD CONSTRAINT reminder_log_kind_ck
      CHECK (target_kind IN ('action', 'issue'));
  END IF;
END
$$;

CREATE OR REPLACE FUNCTION reminder_log_append_only() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'reminder_log is append-only (rejected % on reminder_log)', TG_OP
    USING HINT = '提醒发过就是发过：不许改不许删，改口径请追加新行';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS reminder_log_no_update ON reminder_log;
CREATE TRIGGER reminder_log_no_update
  BEFORE UPDATE OR DELETE ON reminder_log
  FOR EACH ROW EXECUTE FUNCTION reminder_log_append_only();

DROP TRIGGER IF EXISTS reminder_log_no_truncate ON reminder_log;
CREATE TRIGGER reminder_log_no_truncate
  BEFORE TRUNCATE ON reminder_log
  FOR EACH STATEMENT EXECUTE FUNCTION reminder_log_append_only();
