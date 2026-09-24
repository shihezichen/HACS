-- view_auth_schema.sql —— 看板（blackboard_view）的账号 / 会话 / 审计表（幂等，可重复执行）
--
-- 用法（与黑板、圆桌、共享内容同一套）：
--   psql "host=127.0.0.1 port=9950 dbname=blackboard user=bb" -f sql/view_auth_schema.sql
--   本文件**不依赖**任何一张业务表，可以单独执行；也不需要 vector 扩展。
--
-- 为什么要有这几张表（刚哥的要求逐条落到库里）：
--   * 这条看板要开到公网上给人看，里面有节点公网 IP、各机装了什么 Agent、主人画像与
--     关键信息 —— **一旦对外，就必须有门槛**。门槛分两层：外层 TLS（Cloudflare 隧道）
--     终结，内层就是这里的账号 + 会话。
--   * **口令只存哈希**：pwd_hash 里放的是 `scrypt$n$r$p$<salt_b64>$<dk_b64>`，每个账号
--     一条独立随机 salt。库里、日志里、命令行历史里都不许出现明文（CLI 只收 stdin /
--     交互输入，压根没有 --password 这个参数）。
--   * **会话只存指纹**：view_sessions.token_hash 是 sha256(原始 token)。拿到整张表也
--     反推不出可用的 cookie；cookie 里还带一段 HMAC 签名，密钥在库外的文件里。
--   * **审计只增不改**：view_audit 是 append-only —— UPDATE / DELETE / TRUNCATE 由
--     触发器直接拒。"谁在什么时候从哪个 IP 登录成功 / 失败 / 被锁 / 退出"是事实，
--     事实不许被谁事后抹掉。
--   * **账号不许删行**：要停某人就 disabled=true（可再 enable），删了行审计里的
--     username 就成了悬空引用，"这个人当时是什么角色"也查不出来了。
--
-- 角色三级（rank 越大看得越多，判据写在应用层 view_auth.role_can_see）：
--   viewer(0)  给外人：只看 sensitivity='public' 的共享内容 + 会议/落实/遗留的公开字段；
--   member(1)  内部：可看 'internal'（默认级）；
--   owner(2)   刚哥本人：可看 'sensitive'（主人画像 / 关键信息 / 节点公网 IP）。


-- ================================================================ 一、账号
-- 一人一个账号、各自口令；可禁用、可改密，但**不可删**。

CREATE TABLE IF NOT EXISTS view_users (
  username       text        PRIMARY KEY,
  role           text        NOT NULL DEFAULT 'viewer',
  pwd_hash       text        NOT NULL,          -- scrypt$n$r$p$salt_b64$dk_b64，**永远不是明文**
  disabled       boolean     NOT NULL DEFAULT false,
  note           text,
  created_at     timestamptz NOT NULL DEFAULT now(),
  created_by     text,
  updated_at     timestamptz NOT NULL DEFAULT now(),
  pwd_changed_at timestamptz NOT NULL DEFAULT now(),
  last_login_at  timestamptz,
  CONSTRAINT view_users_role_ck CHECK (role IN ('viewer', 'member', 'owner')),
  -- 这一条是"明文绝不落库"的**数据库层**兜底：哈希串必须长成 scrypt$... 的样子。
  -- 应用层写错了、有人手工 INSERT 了一串明文，库直接拒。
  CONSTRAINT view_users_hash_ck CHECK (pwd_hash LIKE 'scrypt$%' AND length(pwd_hash) >= 40)
);

CREATE INDEX IF NOT EXISTS view_users_role ON view_users (role);

-- 账号表：允许 INSERT / UPDATE，拒 DELETE 与 TRUNCATE（停用走 disabled）
CREATE OR REPLACE FUNCTION view_users_guard() RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'TRUNCATE' OR TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'view_users 不允许 %（账号只停用不删除）', TG_OP
      USING HINT = '停用请执行 view_auth.py disable --user <名字>；删了行，审计里的 username 就成了悬空引用';
  END IF;
  IF TG_OP = 'UPDATE' THEN
    IF NEW.username IS DISTINCT FROM OLD.username THEN
      RAISE EXCEPTION 'view_users.username 不可变（换名字请新建账号并停用旧的）';
    END IF;
    IF NEW.created_at IS DISTINCT FROM OLD.created_at THEN
      RAISE EXCEPTION 'view_users.created_at 不可变';
    END IF;
    NEW.updated_at := now();
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS view_users_guard_row ON view_users;
CREATE TRIGGER view_users_guard_row
  BEFORE UPDATE OR DELETE ON view_users
  FOR EACH ROW EXECUTE FUNCTION view_users_guard();

DROP TRIGGER IF EXISTS view_users_guard_trunc ON view_users;
CREATE TRIGGER view_users_guard_trunc
  BEFORE TRUNCATE ON view_users
  FOR EACH STATEMENT EXECUTE FUNCTION view_users_guard();


-- ================================================================ 二、会话
-- 登录成功后服务端生成随机 token：**库里只存 sha256 指纹**，cookie 里带的是
-- "原始 token + HMAC 签名"，签名密钥在 ${CHAT_ENV_DIR}/view-session.key（600）。
-- 拿到这张表推不出 cookie；拿到 cookie 没有密钥也过不了签名校验。

CREATE TABLE IF NOT EXISTS view_sessions (
  token_hash text        PRIMARY KEY,           -- sha256(原始 token) 的 hex
  username   text        NOT NULL REFERENCES view_users(username),
  role       text        NOT NULL,              -- 登录当时的角色（快照：改角色不影响已发出的会话判定口径，靠 revoke 收回）
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL,              -- 默认 12 小时；活跃会自动续期
  last_seen  timestamptz,
  ip         text,                              -- 对端 IP（鉴权只看它）
  ua         text,
  revoked    boolean     NOT NULL DEFAULT false,
  revoked_at timestamptz,
  CONSTRAINT view_sessions_role_ck CHECK (role IN ('viewer', 'member', 'owner'))
);

CREATE INDEX IF NOT EXISTS view_sessions_user    ON view_sessions (username, created_at DESC);
CREATE INDEX IF NOT EXISTS view_sessions_expires ON view_sessions (expires_at);

-- 会话表：允许 INSERT / UPDATE（续期、撤销），拒 DELETE 与 TRUNCATE
-- （"这个会话什么时候建的、从哪个 IP、什么时候被撤的"同样是要留痕的事实）
CREATE OR REPLACE FUNCTION view_sessions_guard() RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'TRUNCATE' OR TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'view_sessions 不允许 %（下线走 revoked=true）', TG_OP
      USING HINT = '一键踢下线：view_auth.py sessions --revoke-all --user <名字>';
  END IF;
  IF TG_OP = 'UPDATE' THEN
    IF NEW.token_hash IS DISTINCT FROM OLD.token_hash
       OR NEW.username IS DISTINCT FROM OLD.username
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
      RAISE EXCEPTION 'view_sessions 的 token_hash / username / created_at 不可变';
    END IF;
    IF OLD.revoked AND NOT NEW.revoked THEN
      RAISE EXCEPTION '已撤销的会话不能复活（重新登录即可）';
    END IF;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS view_sessions_guard_row ON view_sessions;
CREATE TRIGGER view_sessions_guard_row
  BEFORE UPDATE OR DELETE ON view_sessions
  FOR EACH ROW EXECUTE FUNCTION view_sessions_guard();

DROP TRIGGER IF EXISTS view_sessions_guard_trunc ON view_sessions;
CREATE TRIGGER view_sessions_guard_trunc
  BEFORE TRUNCATE ON view_sessions
  FOR EACH STATEMENT EXECUTE FUNCTION view_sessions_guard();


-- ================================================================ 三、审计（append-only）
-- 登录成功 / 失败 / 锁定 / 退出 / 改密 / 越权尝试，一条不落。
-- detail 里**不许出现口令、token、cookie**（应用层负责脱敏，这里只是仓库）。

CREATE TABLE IF NOT EXISTS view_audit (
  audit_id bigserial   PRIMARY KEY,
  ts       timestamptz NOT NULL DEFAULT now(),
  username text,                                -- 失败时可能是"用户不存在"的那个名字，照记
  ip       text,                                -- 对端 IP
  ua       text,
  event    text        NOT NULL,                -- login_ok|login_fail|login_lock|logout|passwd|denied|forbidden|admin
  detail   text
);

CREATE INDEX IF NOT EXISTS view_audit_ts    ON view_audit (ts DESC);
CREATE INDEX IF NOT EXISTS view_audit_event ON view_audit (event, ts DESC);
CREATE INDEX IF NOT EXISTS view_audit_user  ON view_audit (username, ts DESC);

-- 审计表：只许 INSERT。UPDATE / DELETE / TRUNCATE 一律拒 —— 这是 append-only 的本体。
CREATE OR REPLACE FUNCTION view_audit_guard() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'view_audit 是 append-only：不允许 %', TG_OP
    USING HINT = '审计记录一旦写下就不能改也不能删；要清理请由 DBA 走归档表，不要在这张表上动手';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS view_audit_guard_row ON view_audit;
CREATE TRIGGER view_audit_guard_row
  BEFORE UPDATE OR DELETE ON view_audit
  FOR EACH ROW EXECUTE FUNCTION view_audit_guard();

DROP TRIGGER IF EXISTS view_audit_guard_trunc ON view_audit;
CREATE TRIGGER view_audit_guard_trunc
  BEFORE TRUNCATE ON view_audit
  FOR EACH STATEMENT EXECUTE FUNCTION view_audit_guard();


-- ================================================================ 四、登录失败流水（暴力破解防护的"表"那一半）
-- 口径：同一个 IP 在 5 分钟内失败 5 次 → 锁 15 分钟。
-- 内存里也记一份（进程内计数，快），但**进程重启不能等于解锁**，所以这里还要落表。
-- outcome='ok' 的那一行是"截断标记"：只统计最近一次成功之后的失败。

CREATE TABLE IF NOT EXISTS view_login_failures (
  fail_id  bigserial   PRIMARY KEY,
  ts       timestamptz NOT NULL DEFAULT now(),
  ip       text        NOT NULL,
  username text,
  outcome  text        NOT NULL DEFAULT 'fail', -- fail|ok
  CONSTRAINT view_login_failures_outcome_ck CHECK (outcome IN ('fail', 'ok'))
);

CREATE INDEX IF NOT EXISTS view_login_failures_ip_ts ON view_login_failures (ip, ts DESC);

-- 同样 append-only：失败次数不许被"改小"
CREATE OR REPLACE FUNCTION view_login_failures_guard() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'view_login_failures 是 append-only：不允许 %', TG_OP
    USING HINT = '锁定到点自然失效（按 ts 算），不需要删行来解锁';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS view_login_failures_guard_row ON view_login_failures;
CREATE TRIGGER view_login_failures_guard_row
  BEFORE UPDATE OR DELETE ON view_login_failures
  FOR EACH ROW EXECUTE FUNCTION view_login_failures_guard();

DROP TRIGGER IF EXISTS view_login_failures_guard_trunc ON view_login_failures;
CREATE TRIGGER view_login_failures_guard_trunc
  BEFORE TRUNCATE ON view_login_failures
  FOR EACH STATEMENT EXECUTE FUNCTION view_login_failures_guard();


-- ================================================================ 五、自检
-- 建完之后可以直接跑这几条确认（**看得到的只有哈希，没有明文**）：
--   SELECT username, role, disabled, left(pwd_hash, 14) || '…' AS pwd_hash_prefix FROM view_users;
--   SELECT event, count(*) FROM view_audit GROUP BY event ORDER BY 2 DESC;
--   DELETE FROM view_audit;   -- 预期：ERROR  view_audit 是 append-only：不允许 DELETE
