#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
SESSION="geo-monitor-worker"
SCREEN_DIR="$ROOT/runtime/.screen"
PID_FILE="$ROOT/runtime/remote_worker.pid"
mkdir -p "$SCREEN_DIR"
chmod 700 "$SCREEN_DIR"
export SCREENDIR="$SCREEN_DIR"
if ! screen -list 2>/dev/null | grep -q "\.${SESSION}[[:space:]]" && [ ! -f "$PID_FILE" ]; then
  echo "Worker 未运行"
  exit 0
fi
if [ -f "$PID_FILE" ]; then
  pid=$(sed -n '1p' "$PID_FILE")
  case "$pid" in
    *[!0-9]*|'') ;;
    *) kill -TERM "$pid" 2>/dev/null || true ;;
  esac
fi
sleep 1
screen -S "$SESSION" -X quit 2>/dev/null || true
find "$PID_FILE" -delete 2>/dev/null || true
echo "Worker 已停止"
