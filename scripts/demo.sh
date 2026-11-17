#!/usr/bin/env bash
# 一键演示完整生命周期（需要服务已在 127.0.0.1:8000 运行）
# 用法: bash scripts/demo.sh
set -euo pipefail
B=http://127.0.0.1:8000
j() { python3 -c "import json,sys;print(json.load(sys.stdin)$1)"; }

echo "① 造数 + 缩短 orders 留存期到 3 天"
curl -s -X POST $B/api/seed >/dev/null
curl -s -X POST $B/api/policy -d '{"zone":"orders","retention_days":3}' >/dev/null

echo "② 按新策略建分区并创建加工任务"
PID=$(curl -s -X POST $B/api/partitions -d '{"zone":"orders","business_day":"2026-11-20"}' | j "['partition_id']")
TID=$(curl -s -X POST $B/api/tasks -d "{\"name\":\"演示任务\",\"partition_ids\":[$PID]}" | j "['task_id']")
sleep 2

echo "③ 封存 COPY 报告（必要副本，绑定 60 虚拟天）"
curl -s -X POST $B/api/reports \
  -d "{\"task_id\":$TID,\"title\":\"演示副本报告\",\"mode\":\"COPY\",\"partition_ids\":[$PID],\"hold_days\":60}" >/dev/null

echo "④ 快进 5 天 → 3 天留存期已过，预览（原件可回收、副本保留）"
curl -s -X POST $B/api/clock/advance -d '{"seconds":432000}' >/dev/null
curl -s $B/api/preview | python3 -m json.tool | head -30

echo "⑤ 真实回收，随后核对磁盘：原件消失、副本存活"
curl -s -X POST $B/api/reap -d '{}' | j "['bytes_freed_this_run']" | xargs echo "  本次释放字节:"
find var/data/partitions -name data.csv | wc -l | xargs echo "  剩余分区文件:"
find var/data/copies -name data.csv | wc -l | xargs echo "  存活副本文件:"
