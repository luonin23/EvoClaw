#!/bin/bash
# EvoClaw Health Monitor — run via cron every 5 minutes
# Checks trader heartbeat and alerts if stuck.

HEALTH_URL="http://localhost:8080/api/health"
LOG_FILE="/home/claudeuser/EvoClaw/data/health_monitor.log"
ALERT_THRESHOLD=600  # Alert if no price refresh for 10 minutes

RESPONSE=$(curl -s --max-time 10 "$HEALTH_URL" 2>/dev/null)
TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')

if [ -z "$RESPONSE" ]; then
    echo "[$TIMESTAMP] CRITICAL: Health endpoint unreachable!" | tee -a "$LOG_FILE"
    exit 1
fi

STATUS=$(echo "$RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('status','unknown'))" 2>/dev/null)
PRICE_AGE=$(echo "$RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('last_price_ok_seconds_ago','null'))" 2>/dev/null)
TRADER_RUNNING=$(echo "$RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('trader_running','null'))" 2>/dev/null)

if [ "$STATUS" = "degraded" ]; then
    echo "[$TIMESTAMP] WARNING: Trader degraded — price_age=${PRICE_AGE}s, trader_running=$TRADER_RUNNING" | tee -a "$LOG_FILE"
elif [ "$STATUS" = "ok" ]; then
    echo "[$TIMESTAMP] OK: price_age=${PRICE_AGE}s, trader_running=$TRADER_RUNNING" >> "$LOG_FILE"
else
    echo "[$TIMESTAMP] UNKNOWN: status=$STATUS, response=$RESPONSE" | tee -a "$LOG_FILE"
fi
