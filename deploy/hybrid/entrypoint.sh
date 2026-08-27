#!/bin/sh
set -eu

mkdir -p runtime/web_results runtime/logs

python3 -u doubao_dashboard_server.py >runtime/logs/dashboard.out.log 2>runtime/logs/dashboard.err.log &
backend_pid=$!

node yuanbao_monitor/dashboard/node_modules/vinext/dist/cli.js start -H 0.0.0.0 -p 3000 \
  >runtime/logs/frontend.out.log 2>runtime/logs/frontend.err.log &
frontend_pid=$!

trap 'kill "$backend_pid" "$frontend_pid" 2>/dev/null || true' TERM INT
while kill -0 "$backend_pid" 2>/dev/null && kill -0 "$frontend_pid" 2>/dev/null; do
  sleep 2
done
kill "$backend_pid" "$frontend_pid" 2>/dev/null || true
wait "$backend_pid" "$frontend_pid" 2>/dev/null || true
