#!/bin/bash

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_NAME="$(basename "$SCRIPT_DIR")"
PID_FILE="$HOME/.$DEPLOY_NAME.pid"

if [ ! -f "$PID_FILE" ]; then
    echo "$DEPLOY_NAME: no PID file found at $PID_FILE"
    exit 1
fi

PID="$(cat "$PID_FILE")"
kill "$PID" 2>/dev/null && echo "$DEPLOY_NAME (PID $PID) stopped" || echo "$DEPLOY_NAME (PID $PID) was not running"
rm -f "$PID_FILE"
