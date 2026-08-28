#!/bin/sh
set -u

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
PYTHON="$ROOT/runtime/worker-venv/bin/python"
LOG_DIR="$ROOT/runtime/logs"
LOG_FILE="$LOG_DIR/remote_worker.log"
mkdir -p "$LOG_DIR"
cd "$ROOT"

load_cookie_file() {
  cookie_file="$1"
  cookie_var="$2"
  if [ -s "$cookie_file" ]; then
    cookie_value=$(tr -d '\r\n' <"$cookie_file")
    export "$cookie_var=$cookie_value"
    find "$cookie_file" -type f -delete
  fi
}

load_cookie_file "$ROOT/doubao.cookies.json" MONITOR_DOUBAO_COOKIES_JSON
load_cookie_file "$ROOT/yuanbao.cookies.json" MONITOR_YUANBAO_COOKIES_JSON
load_cookie_file "$ROOT/wenxin.cookies.json" MONITOR_WENXIN_COOKIES_JSON

printf '%s\n' "$$" >"$ROOT/runtime/remote_worker.pid"
export PYTHONDONTWRITEBYTECODE=1
exec "$PYTHON" -m web_collectors.remote_worker >>"$LOG_FILE" 2>&1
