#!/bin/bash

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_NAME="$(basename "$SCRIPT_DIR")"
PID_FILE="$HOME/.$DEPLOY_NAME.pid"

echo "$DEPLOY_NAME: stopping..."
if [ -f "$PID_FILE" ]; then
    PID="$(cat "$PID_FILE")"
    kill "$PID" 2>/dev/null
    rm -f "$PID_FILE"
fi

echo "$DEPLOY_NAME: pulling..."
git -C "$SCRIPT_DIR" pull

echo "$DEPLOY_NAME: starting..."
exec "$SCRIPT_DIR/start.sh"
