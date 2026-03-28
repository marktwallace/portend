#!/bin/bash

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_NAME="$(basename "$SCRIPT_DIR")"
PID_FILE="$HOME/.$DEPLOY_NAME.pid"
LOG_FILE="$HOME/logs/$DEPLOY_NAME.log"

command -v uv &>/dev/null || . "$HOME/.local/bin/env"

set -a
source "$SCRIPT_DIR/.env"
set +a

cd "$SCRIPT_DIR"
mkdir -p "$HOME/logs"

nohup uv run uvicorn main:app \
    --host 0.0.0.0 \
    --port "$PORT" \
    --root-path "$SERVER_BASE_URL_PATH" \
    --log-config "$SCRIPT_DIR/log_config.json" \
    > "$LOG_FILE" 2>&1 &

echo $! > "$PID_FILE"
echo "$DEPLOY_NAME started on :$PORT at $SERVER_BASE_URL_PATH — log: $LOG_FILE"
