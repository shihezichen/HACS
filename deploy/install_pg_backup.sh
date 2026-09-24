#!/bin/bash
# 黑板库每日备份（2026-09-19 事故后补：当时无备份、WAL 未归档，删列后无法从库内恢复）
# 口径：pg_dump 全量 → gzip；保留最近 14 份；每次追加一行日志。
# 认证：容器内本地 socket 免密（-U bb 实测可用，无需口令）。
set -u

DEST=/var/backups/agent-team
mkdir -p "$DEST"
chmod 700 "$DEST"

TS=$(date +%Y%m%d-%H%M%S)
OUT="$DEST/blackboard-$TS.sql.gz"

if docker exec agent-team-pg pg_dump -U bb -d blackboard --no-owner --no-privileges 2>"$DEST/last.err" | gzip > "$OUT"; then
  if [ -s "$OUT" ]; then
    echo "$(date -Iseconds) OK size=$(du -h "$OUT" | cut -f1) file=$(basename "$OUT")" >> "$DEST/backup.log"
  else
    echo "$(date -Iseconds) FAIL 空备份 file=$(basename "$OUT")" >> "$DEST/backup.log"
    rm -f "$OUT"; exit 1
  fi
else
  echo "$(date -Iseconds) FAIL pg_dump 非零退出" >> "$DEST/backup.log"
  rm -f "$OUT"; exit 1
fi

# 只留最近 14 份
ls -1t "$DEST"/blackboard-*.sql.gz 2>/dev/null | tail -n +15 | while read -r f; do rm -f "$f"; done
# 日志只留最近 200 行
if [ -f "$DEST/backup.log" ]; then
  tail -n 200 "$DEST/backup.log" > "$DEST/backup.log.tmp" && mv "$DEST/backup.log.tmp" "$DEST/backup.log"
fi

echo "备份完成：$OUT"
