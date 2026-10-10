#!/bin/bash
# EvoClaw v2.0 重启脚本（PM2 优先）
# Usage: ./restart-v2.sh [--status]

set -e
PROJECT_DIR="/home/claudeuser/EvoClaw"
PORT=8091
cd "$PROJECT_DIR"

case "${1:-}" in
  --status|-s)
    if command -v pm2 >/dev/null 2>&1 && pm2 describe evoclaw-v2 >/dev/null 2>&1; then
      pm2 describe evoclaw-v2 | grep -E 'status|uptime|restarts' | head -3
    else
      pids=$(pgrep -f "evoclaw-v2" 2>/dev/null || pgrep -f "python3 main.py" 2>/dev/null || true)
      [ -n "$pids" ] && echo "运行中 PID=$pids" || echo "未运行"
    fi
    echo ""
    curl -s --max-time 5 "http://localhost:$PORT/api/state" | head -c 400 || echo "(Web 未响应)"
    echo ""
    exit 0
    ;;
esac

echo "=== EvoClaw v2.0 重启 ==="
if command -v pm2 >/dev/null 2>&1 && pm2 describe evoclaw-v2 >/dev/null 2>&1; then
  pm2 restart evoclaw-v2
  echo "  Web UI: http://localhost:$PORT"
  exit 0
fi

# 非 PM2：手动重启
kill $(cat data/evoclaw-v2.pid 2>/dev/null) 2>/dev/null || true
sleep 2
rm -f data/evoclaw-v2.pid
nohup python3 main.py > /dev/null 2>&1 &
sleep 4
if ss -ltn 2>/dev/null | grep -q ":$PORT"; then
  echo "  已启动 (端口 $PORT)"
  echo "  Web UI: http://localhost:$PORT"
else
  echo "  启动失败，请检查 data/v2.log"
  tail -20 data/v2.log
  exit 1
fi
