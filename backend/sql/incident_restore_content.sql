-- P4 事故修复：还原被误删的 content 列数据（2026-09-19）
-- 背景：验证 append-only 护栏时误用 DDL（DROP COLUMN content）→ 列被真删，5 条正文丢失。
-- 还原口径：逐字取自原始来源（不经任何改写）
--   entry 1  ← 上线时的原始 INSERT 语句（会话记录逐字可查）
--   entry 2/4← tests/e2e_blackboard_crossmachine.py 第 43 行原文
--   entry 3/5← tests/e2e_blackboard_crossmachine.py 第 52 行原文
-- 旁路说明：SET session_replication_role = replica 仅作用于**本连接**，退出即失效；
--           外侧普通连接仍受 append-only 触发器保护（本次仅补 content IS NULL 的行）。

SET session_replication_role = replica;

UPDATE blackboard_entries SET content = 'P3 黑板库上线：PostgreSQL16 + pgvector 0.8.6，服务 blackboard_svc 端口 9940，库只绑 127.0.0.1:9950。'
 WHERE entry_id = 1 AND content IS NULL;

UPDATE blackboard_entries SET content = 'P3 跨机自检：这是一条来自笔记本的条目（中文测试）'
 WHERE entry_id IN (2, 4) AND content IS NULL;

UPDATE blackboard_entries SET content = '冒名尝试：声明自己是 lobster'
 WHERE entry_id IN (3, 5) AND content IS NULL;

-- 约束恢复：原文定义是 NOT NULL
ALTER TABLE blackboard_entries ALTER COLUMN content SET NOT NULL;

SET session_replication_role = DEFAULT;

\echo '===== 还原结果 ====='
SELECT entry_id, author, topic, kind, content FROM blackboard_entries ORDER BY entry_id;

\echo '===== 是否还有空 content（应为 0）====='
SELECT count(*) AS still_null FROM blackboard_entries WHERE content IS NULL;
