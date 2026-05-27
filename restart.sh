#!/bin/bash
# EvoClaw 重启脚本
# Usage:
#   ./restart.sh           正常重启
#   ./restart.sh --watch   看门狗模式（自动检测并重启）
#   ./restart.sh --status  查看进程状态

set -e

PROJECT_DIR="/home/claudeuser/EvoClaw"
LOG_DIR="$PROJECT_DIR/data"
PORT=8080
MAX_WAIT=10

# ---- 颜色输出 ----
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

# ---- 状态查询 ----
do_status() {
    pids=$(pgrep -f "python.*main\\.py" 2>/dev/null || true)
    if [ -z "$pids" ]; then
        echo -e "${RED}EvoClaw: NOT RUNNING${NC}"
        if [ -f "$LOG_DIR/crash.log" ]; then
            echo "Recent crashes:"
            tail -5 "$LOG_DIR/crash.log"
        fi
    else
        for pid in $pids; do
            uptime_sec=$(ps -p "$pid" -o etimes= 2>/dev/null | tr -d ' ' || echo 0)
            hours=$((uptime_sec / 3600))
            mins=$(((uptime_sec % 3600) / 60))
            mem=$(ps -p "$pid" -o rss= 2>/dev/null | tr -d ' ' || echo 0)
            mem_mb=$((mem / 1024))
            echo -e "${GREEN}EvoClaw: RUNNING${NC}  PID=$pid  Uptime=${hours}h${mins}m  RSS=${mem_mb}MB  Port=$PORT"
        done
        echo ""
        echo "Recent logs:"
        tail -3 "$LOG_DIR/trader.log" 2>/dev/null | sed 's/^/  /'
    fi
}

# ---- 杀进程 ----
kill_all() {
    # Step 1: graceful shutdown with SIGTERM
    pids=$(pgrep -f "python.*main\\.py" 2>/dev/null || true)
    if [ -n "$pids" ]; then
        echo "  Sending SIGTERM to $pids"
        for pid in $pids; do
            kill -TERM "$pid" 2>/dev/null || true
        done
    fi

    # Step 2: wait up to 8s for graceful exit
    waited=0
    while [ $waited -lt 8 ]; do
        pids=$(pgrep -f "python.*main\\.py" 2>/dev/null || true)
        if [ -z "$pids" ]; then
            echo "  All processes stopped gracefully"
            return 0
        fi
        sleep 1
        waited=$((waited + 1))
    done

    # Step 3: force kill remaining
    pids=$(pgrep -f "python.*main\\.py" 2>/dev/null || true)
    if [ -n "$pids" ]; then
        echo "  Force killing remaining: $pids"
        for pid in $pids; do
            kill -9 "$pid" 2>/dev/null || true
        done
        sleep 1
    fi

    remaining=$(pgrep -f "python.*main\\.py" 2>/dev/null || true)
    if [ -n "$remaining" ]; then
        echo -e "  ${RED}ERROR: Failed to kill processes after ${MAX_WAIT}s: $remaining${NC}"
        exit 1
    fi
}

# ---- 清理端口 ----
clean_port() {
    if command -v fuser >/dev/null 2>&1; then
        fuser -k "${PORT}/tcp" 2>/dev/null || true
    fi
    waited=0
    while [ $waited -lt $MAX_WAIT ]; do
        occupiers=$(lsof -ti :$PORT 2>/dev/null || true)
        if [ -z "$occupiers" ]; then
            echo "  Port $PORT is free"
            break
        fi
        for pid in $occupiers; do
            kill -9 "$pid" 2>/dev/null || true
        done
        sleep 1
        waited=$((waited + 1))
    done
    if lsof -ti :$PORT >/dev/null 2>&1; then
        echo -e "  ${RED}ERROR: Port $PORT is still occupied after ${MAX_WAIT}s${NC}"
        exit 1
    fi
}

# ---- 启动服务 ----
start_service() {
    cd "$PROJECT_DIR"
    mkdir -p "$LOG_DIR"
    export PYTHONPATH=/home/claudeuser/.local/lib/python3.12/site-packages:$PYTHONPATH
    # Redirect stdout/stderr to /dev/null because main.py uses RotatingFileHandler
    # for trader.log. If we redirect stdout to trader.log too, every log appears twice.
    nohup python3 main.py > /dev/null 2>&1 &
    PID=$!
    echo "  Started with PID $PID"
    sleep 3
    if ps -p "$PID" > /dev/null 2>&1; then
        echo -e "  ${GREEN}Service is running (PID $PID)${NC}"
        echo "  Web UI: http://localhost:8080"
        tail -3 "$LOG_DIR/trader.log" 2>/dev/null | sed 's/^/    /'
    else
        echo -e "  ${RED}ERROR: Service failed to start!${NC}"
        tail -20 "$LOG_DIR/trader.log" 2>/dev/null | sed 's/^/    /'
        return 1
    fi
}

# ---- 正常重启 ----
do_restart() {
    echo "=== EvoClaw Restart ==="
    cd "$PROJECT_DIR"
    echo "[1/4] Stopping existing processes..."
    kill_all
    echo "[2/4] Cleaning port $PORT..."
    clean_port
    echo "[3/4] Starting EvoClaw..."
    start_service
    echo "=== Restart complete ==="
}

# ---- 看门狗模式 ----
do_watchdog() {
    echo -e "${YELLOW}=== EvoClaw Watchdog Started ===${NC}"
    echo "  Check interval: 30s"
    echo "  Crash log: $LOG_DIR/crash.log"
    echo ""

    mkdir -p "$LOG_DIR"

    while true; do
        pids=$(pgrep -f "python.*main\\.py" 2>/dev/null || true)

        if [ -z "$pids" ]; then
            TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')
            echo -e "${RED}[$TIMESTAMP] Process died! Restarting...${NC}"
            echo "[$TIMESTAMP] CRASH: process not found, restarting" >> "$LOG_DIR/crash.log"

            kill_all 2>/dev/null || true
            clean_port 2>/dev/null || true

            # 备份 crash 前的最后日志
            if [ -f "$LOG_DIR/trader.log" ]; then
                echo "  Last 10 lines before crash:" >> "$LOG_DIR/crash.log"
                tail -10 "$LOG_DIR/trader.log" 2>/dev/null | sed 's/^/    /' >> "$LOG_DIR/crash.log"
                echo "" >> "$LOG_DIR/crash.log"
            fi

            if start_service; then
                echo -e "${GREEN}[$TIMESTAMP] Service restarted successfully${NC}"
            else
                echo -e "${RED}[$TIMESTAMP] Restart failed, retrying in 30s...${NC}"
            fi
        fi

        sleep 30
    done
}

# ---- 入口 ----
case "${1:-}" in
    --status|-s)
        do_status
        ;;
    --watch|-w)
        do_watchdog
        ;;
    *)
        do_restart
        ;;
esac
