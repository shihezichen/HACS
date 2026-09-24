\echo '===== 触发器清单 ====='
SELECT tgname, pg_get_triggerdef(t.oid) AS def
FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid
WHERE c.relname = 'blackboard_entries' AND NOT t.tgisinternal
ORDER BY tgname;

\echo ''
\echo '===== 触发器函数体 ====='
SELECT p.proname, pg_get_functiondef(p.oid) AS def
FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE p.proname LIKE '%blackboard%'
ORDER BY p.proname;

\echo ''
\echo '===== bb 角色权限 ====='
SELECT rolname, rolsuper, rolcreatedb FROM pg_roles WHERE rolname = current_user;

\echo ''
\echo '===== 当前向量状态 ====='
SELECT count(*) AS rows_total, count(embedding) AS rows_with_vec FROM blackboard_entries;
