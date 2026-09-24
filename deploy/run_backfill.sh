#!/bin/bash
# P4 回填：dry-run → 真跑（--bypass-trigger 仅本连接生效）→ 复核覆盖率
set -u

if [ ! -f <deploy-dir>/pg.env ]; then
  echo "缺 <deploy-dir>/pg.env"; exit 1
fi
# 注意：pg.env 是 **systemd EnvironmentFile 格式**，值是带空格且不加引号的 DSN。
# 用 bash source 会被截断（只取到第一个空格前），必须按 systemd 口径整行取值。
BB_PG_DSN=$(sed -n 's/^BB_PG_DSN=//p' <deploy-dir>/pg.env | head -1)
export BB_PG_DSN
if [ -z "$BB_PG_DSN" ]; then
  echo "pg.env 里没找到 BB_PG_DSN"; exit 1
fi
echo "DSN 已载入（值不外显）：字段数=$(echo "$BB_PG_DSN" | wc -w)"

export HF_HUB_DISABLE_XET=1
export HF_ENDPOINT=https://hf-mirror.com
cd <deploy-dir> || exit 1

echo
echo "===== 1) 嵌入可用性自检 ====="
python3 -c "
import embedder
v = embedder.embed_texts(['回填前自检'])
print('  embed_texts ->', 'OK 维度=%d' % len(v[0]) if v else 'FAILED: %s' % embedder.status().get('last_error'))
"

echo
echo "===== 2) dry-run ====="
python3 backfill_embeddings.py --dry-run 2>&1 | tail -5

echo
echo "===== 3) 真跑（本连接旁路触发器，退出即失效）====="
python3 backfill_embeddings.py --bypass-trigger --batch-size 16 2>&1 | tail -8
