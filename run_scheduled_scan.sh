#!/bin/bash
set -euo pipefail

# 自动加载 .env（支持手动执行和 systemd）
if [[ -f "$(dirname "$0")/civitai-bot.env" ]]; then
    set -a
    source "$(dirname "$0")/civitai-bot.env"
    set +a
fi

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
INTERVAL_FILE="$SCRIPT_DIR/interval.json"
LAST_RUN_FILE="$SCRIPT_DIR/.last_scheduled_scan"
MONITOR_SCRIPT="$SCRIPT_DIR/monitor.py"
LOCK_FILE="$SCRIPT_DIR/.monitor.lock"

DEFAULT_INTERVAL=600

if ! command -v jq >/dev/null 2>&1; then
    echo "[ERROR] jq is required but not installed."
    exit 1
fi

if [[ -f "$INTERVAL_FILE" ]]; then
    INTERVAL=$(jq -r ".seconds // $DEFAULT_INTERVAL" "$INTERVAL_FILE" 2>/dev/null || echo "$DEFAULT_INTERVAL")
else
    INTERVAL=$DEFAULT_INTERVAL
fi

if [[ -f "$LOCK_FILE" ]]; then
    LOCK_AGE=$(( $(date +%s) - $(stat -c %Y "$LOCK_FILE" 2>/dev/null || echo 0) ))
    if (( LOCK_AGE < 3600 )); then
        echo "[$(date "+%F %T")] Skipping: monitor.py appears to be running."
        exit 0
    fi
fi

if [[ -f "$LAST_RUN_FILE" ]]; then
    LAST_RUN=$(cat "$LAST_RUN_FILE")
else
    LAST_RUN=0
fi

NOW=$(date +%s)
ELAPSED=$(( NOW - LAST_RUN ))

if (( ELAPSED >= INTERVAL )); then
    echo "[$(date "+%F %T")] Interval reached. Running monitor.py..."
    cd "$SCRIPT_DIR"
    set +e
    python3 "$MONITOR_SCRIPT" --mode incremental
    py_exit=$?
    set -e
    if [ $py_exit -eq 0 ]; then
        echo "$NOW" > "$LAST_RUN_FILE"
        echo "[$(date "+%F %T")] Scheduled scan completed."
    elif [ $py_exit -eq 75 ]; then
        echo "[$(date "+%F %T")] Skipped due to concurrent lock (normal behavior, exit 75)."
    else
        echo "[$(date "+%F %T")] Scan failed (exit code $py_exit)."
    fi
else
    echo "[$(date "+%F %T")] Skipping (only ${ELAPSED}s elapsed)."
fi
