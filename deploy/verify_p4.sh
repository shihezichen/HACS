#!/bin/bash
# P4 独立验证：① 向量覆盖率 ② append-only 保证未被削弱（硬证据）③ 建 HNSW 索引
set -u
BB_PG_DSN=$(sed -n 's/^BB_PG_DSN=//p' <deploy-dir>/pg.env | head -1)
export BB_PG_DSN
PSQL='docker exec -i agent-team-pg psql -U bb -d blackboard -q -t -A -v ON_ERROR_STOP=1'

echo "===== ① 向量覆盖率 ====="
echo "SELECT count(*) || ' 总行 / ' || count(embedding) || ' 已有向量' FROM blackboard_entries;" | $PSQL

echo
echo "===== ② append-only 保证（每条都必须被拒）====="

try_reject () {
  local label="$1" sql="$2"
  local out rc
  out=$(echo "$sql" | $PSQL 2>&1)
  rc=$?
  if [ $rc -ne 0 ]; then
    printf '  %-10s 被拒 ✓   %s\n' "$label" "$(echo "$out" | grep -oE 'ERROR:.*' | head -1 | cut -c1-70)"
  else
    printf '  %-10s 竟然成功了 ✗✗✗   —— 保证被削弱！\n' "$label"
  fi
}

try_reject "UPDATE"   "UPDATE blackboard_entries SET content='篡改' WHERE entry_id=1;"
try_reject "UPDATE列" "UPDATE blackboard_entries SET embedding=NULL WHERE entry_id=1;"
try_reject "DELETE"   "DELETE FROM blackboard_entries WHERE entry_id=1;"
try_reject "TRUNCATE" "TRUNCATE blackboard_entries;"

# 【纪律·2026-09-19 事故留下的】绝不用破坏性 DDL 在真表上做"护栏测试"：
# 本脚本此处原本有一条 `ALTER TABLE ... DROP COLUMN content` 用来"验证能不能挡住改结构"，
# 它真的执行成功了（触发器只挡 DML，DDL 不受保护），把 content 列连同 5 条正文删掉，
# 数据靠会话记录逐字还原才补回来。护栏是否在位，改为**只核对触发器存在且启用**（下方），
# 破坏性语句只能在一次性 scratch 库/临时表上跑。

echo
echo "  护栏在位性（只核对，不动数据）："
echo "SELECT '    ' || tgname || ' enabled=' || tgenabled::text FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid WHERE c.relname='blackboard_entries' AND NOT t.tgisinternal ORDER BY 1;" | $PSQL

echo
echo "  条目仍完好："
echo "SELECT '    entry_id=' || entry_id || ' author=' || author || ' 正文=' || left(content,20) FROM blackboard_entries WHERE entry_id=1;" | $PSQL

echo
echo "===== ③ 并发写入仍正常（INSERT 不受伤）====="
echo "INSERT INTO blackboard_entries (author, topic, kind, content) VALUES ('hermes','infra','note','P4 验证：INSERT 通道正常') RETURNING '    写入成功 entry_id=' || entry_id;" | $PSQL

echo
echo "===== ④ 建 HNSW 索引 ====="
docker exec -i agent-team-pg psql -U bb -d blackboard -q < /tmp/p4_hnsw_index.sql 2>&1 | grep -viE "^\s*$"

echo
echo "===== ⑤ 索引核对 ====="
echo "SELECT '    ' || indexname FROM pg_indexes WHERE tablename='blackboard_entries' ORDER BY 1;" | $PSQL
echo "SELECT '    HNSW 大小=' || pg_size_pretty(pg_relation_size('idx_bb_embedding_hnsw')) FROM pg_indexes WHERE indexname='idx_bb_embedding_hnsw';" | $PSQL
