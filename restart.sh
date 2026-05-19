#!/bin/bash
# EvoClaw 重启脚本
# Usage: ./restart.sh

set -e

PROJECT_DIR="/home/claudeuser/EvoClaw"
LOG_DIR="$PROJECT_DIR/data"
PORT=8080
MAX_WAIT=10

echo "=== EvoClaw Restart ==="

# 1. 进入项目目录
cd "$PROJECT_DIR"
echo "[1/5] Entered $PROJECT_DIR"

# 2. 杀掉所有 EvoClaw 相关 Python 进程
echo "[2/5] Stopping existing processes..."
# 先优雅终止（匹配更宽松）
pkill -f "python.*main\\.py" 2>/dev/null || true
sleep 1

# 强制清理残留，循环直到进程消失或超时
waited=0
while [ $waited -lt $MAX_WAIT ]; do
    pids=$(pgrep -f "python.*main\\.py" 2>/dev/null || true)
    if [ -z "$pids" ]; then
        echo "  All processes stopped"
        break
    fi
    for pid in $pids; do
        echo "  Force killing PID $pid"
        kill -9 "$pid" 2>/dev/null || {
            owner=$(ps -p "$pid" -o user= 2>/dev/null || echo "unknown")
            echo "  WARNING: Cannot kill PID $pid (owner: $owner). Permission denied."
        }
    done
    sleep 1
    waited=$((waited + 1))
done

# 如果进程仍存在，报错退出
remaining=$(pgrep -f "python.*main\\.py" 2>/dev/null || true)
if [ -n "$remaining" ]; then
    echo "  ERROR: Failed to kill processes after ${MAX_WAIT}s: $remaining"
    ps aux | grep -E "PID|python.*main\\.py" | grep -v grep | sed 's/^/    /'
    echo "  If owner is root, run as root or configure sudo."
    exit 1
fi

# 3. 清理端口占用
echo "[3/5] Cleaning port $PORT..."
# 使用 fuser 强制清理端口（如果可用）
if command -v fuser >/dev/null 2>&1; then
    fuser -k "${PORT}/tcp" 2>/dev/null || true
fi

# 循环检查端口释放
waited=0
while [ $waited -lt $MAX_WAIT ]; do
    occupiers=$(lsof -ti :$PORT 2>/dev/null || true)
    if [ -z "$occupiers" ]; then
        echo "  Port $PORT is free"
        break
    fi
    for pid in $occupiers; do
        echo "  Killing port occupier PID $pid"
        kill -9 "$pid" 2>/dev/null || {
            owner=$(ps -p "$pid" -o user= 2>/dev/null || echo "unknown")
            echo "  WARNING: Cannot kill PID $pid (owner: $owner). Permission denied."
        }
    done
    sleep 1
    waited=$((waited + 1))
done

# 如果端口仍被占用，报错退出
if lsof -ti :$PORT >/dev/null 2>&1; then
    echo "  ERROR: Port $PORT is still occupied after ${MAX_WAIT}s"
    lsof -i :$PORT | sed 's/^/    /'
    exit 1
fi

# 4. 清理日志（备份当前主日志，清空 trader.log）
echo "[4/5] Cleaning logs..."
if [ -f "$LOG_DIR/trader.log" ]; then
    TIMESTAMP=$(date +%Y%m%d_%H%M%S)
    mv "$LOG_DIR/trader.log" "$LOG_DIR/trader.log.$TIMESTAMP.bak"
    echo "  Backed up trader.log -> trader.log.$TIMESTAMP.bak"
fi
# 清空 stdout.log
if [ -f "$LOG_DIR/stdout.log" ]; then
    > "$LOG_DIR/stdout.log"
    echo "  Cleared stdout.log"
fi

# 确保日志目录存在
mkdir -p "$LOG_DIR"

# 5. 启动服务
echo "[5/5] Starting EvoClaw..."
export PYTHONPATH=/home/claudeuser/.local/lib/python3.12/site-packages:$PYTHONPATH
nohup python3 main.py > "$LOG_DIR/trader.log" 2>&1 &
PID=$!
echo "  Started with PID $PID"

# 等待启动完成
sleep 3

# 检查是否正常运行
if ps -p "$PID" > /dev/null 2>&1; then
    echo "  Service is running (PID $PID)"
    echo "  Web UI: http://localhost:8080"
    echo "  Latest logs:"
    tail -5 "$LOG_DIR/trader.log" | sed 's/^/    /'
    echo "=== Restart complete ==="
else
    echo "  ERROR: Service failed to start!"
    echo "  Last 20 lines of log:"
    tail -20 "$LOG_DIR/trader.log" | sed 's/^/    /'
    exit 1
fi
