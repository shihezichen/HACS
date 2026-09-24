#!/bin/bash
# 在龙虾安装黑板库每日备份 + 做一次真实"还原演练"（备份能不能用，只有还原过才算数）
set -u

echo "===== 1) 装备份脚本 ====="
install -m 755 /tmp/agent-team-pg-backup.sh /usr/local/bin/agent-team-pg-backup.sh
echo "  已装：$(ls -l /usr/local/bin/agent-team-pg-backup.sh | awk '{print $1, $NF}')"

echo
echo "===== 2) 排程（优先 cron，没有 cron 就用 systemd timer）====="
if systemctl is-active --quiet crond 2>/dev/null || systemctl is-active --quiet cron 2>/dev/null; then
  printf '# 黑板库每日备份（2026-09-19 事故后补）\n40 3 * * * root /usr/local/bin/agent-team-pg-backup.sh >/dev/null 2>&1\n' > /etc/cron.d/agent-team-pg-backup
  chmod 644 /etc/cron.d/agent-team-pg-backup
  echo "  用 cron：/etc/cron.d/agent-team-pg-backup（每天 03:40）"
else
  cat > /etc/systemd/system/agent-team-pg-backup.service <<'EOF'
[Unit]
Description=Agent team blackboard PG daily backup
[Service]
Type=oneshot
ExecStart=/usr/local/bin/agent-team-pg-backup.sh
EOF
  cat > /etc/systemd/system/agent-team-pg-backup.timer <<'EOF'
[Unit]
Description=Daily blackboard PG backup timer
[Timer]
OnCalendar=*-*-* 03:40:00
Persistent=true
[Install]
WantedBy=timers.target
EOF
  systemctl daemon-reload
  systemctl enable --now agent-team-pg-backup.timer >/dev/null 2>&1
  echo "  用 systemd timer：$(systemctl is-active agent-team-pg-backup.timer)"
  systemctl list-timers agent-team-pg-backup.timer --no-pager | head -3
fi

echo
echo "===== 3) 立刻跑一次备份 ====="
/usr/local/bin/agent-team-pg-backup.sh
echo "  目录："
ls -lh /var/backups/agent-team/ | tail -3

echo
echo "===== 4) 还原演练（scratch 库，不动生产库）====="
DUMP=$(ls -1t /var/backups/agent-team/blackboard-*.sql.gz | head -1)
echo "  用备份：$(basename "$DUMP")"
docker exec -i agent-team-pg psql -U bb -d postgres -q -c "DROP DATABASE IF EXISTS bb_restore_drill;" >/dev/null 2>&1
docker exec -i agent-team-pg psql -U bb -d postgres -q -c "CREATE DATABASE bb_restore_drill;" >/dev/null 2>&1
gunzip -c "$DUMP" | docker exec -i agent-team-pg psql -U bb -d bb_restore_drill -q -v ON_ERROR_STOP=1 >/dev/null 2>&1
echo -n "  还原后行数："
docker exec -i agent-team-pg psql -U bb -d bb_restore_drill -q -t -A -c "SELECT count(*) FROM blackboard_entries;"
echo -n "  还原后正文非空行数："
docker exec -i agent-team-pg psql -U bb -d bb_restore_drill -q -t -A -c "SELECT count(*) FROM blackboard_entries WHERE content IS NOT NULL AND content <> '';"
echo -n "  还原后带向量行数："
docker exec -i agent-team-pg psql -U bb -d bb_restore_drill -q -t -A -c "SELECT count(embedding) FROM blackboard_entries;"
docker exec -i agent-team-pg psql -U bb -d postgres -q -c "DROP DATABASE bb_restore_drill;" >/dev/null 2>&1
echo "  演练库已清理；生产库未动"
echo -n "  生产库行数（应不变）："
docker exec -i agent-team-pg psql -U bb -d blackboard -q -t -A -c "SELECT count(*) FROM blackboard_entries;"
