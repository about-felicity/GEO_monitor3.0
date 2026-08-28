#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
PYTHON="$ROOT/runtime/worker-venv/bin/python"
LOG_DIR="$ROOT/runtime/logs"
LOG_FILE="$LOG_DIR/remote_worker.log"
SESSION="geo-monitor-worker"
SCREEN_DIR="$ROOT/runtime/.screen"
PID_FILE="$ROOT/runtime/remote_worker.pid"
mkdir -p "$SCREEN_DIR"
chmod 700 "$SCREEN_DIR"
export SCREENDIR="$SCREEN_DIR"

if [ ! -x "$PYTHON" ]; then
  echo "Worker 虚拟环境不存在：$PYTHON" >&2
  exit 1
fi
if [ ! -s "$ROOT/config/remote_worker.env" ]; then
  echo "Worker 配置不存在：config/remote_worker.env" >&2
  exit 1
fi
if screen -list 2>/dev/null | grep -q "\.${SESSION}[[:space:]]"; then
  echo "Worker 已由 screen 管理并处于运行状态"
  exit 0
fi
find "$PID_FILE" -delete 2>/dev/null || true

mkdir -p "$LOG_DIR"
screen -dmS "$SESSION" /bin/sh "$ROOT/scripts/operations/run_remote_worker.sh"
sleep 1
if ! screen -list 2>/dev/null | grep -q "\.${SESSION}[[:space:]]"; then
  echo "Worker 启动失败" >&2
  tail -20 "$LOG_FILE" >&2 || true
  exit 1
fi
echo "Worker 已启动，会话 ${SESSION}，日志：${LOG_FILE}"
