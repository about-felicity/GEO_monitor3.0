#!/usr/bin/env bash
set -euo pipefail

mkdir -p runtime/unified_control runtime/web_profiles runtime/web_results runtime/web_state runtime/logs
chown -R monitor:monitor runtime

gosu monitor Xvfb :99 -screen 0 1440x1100x24 -nolisten tcp >runtime/logs/xvfb.log 2>&1 &
gosu monitor fluxbox >runtime/logs/fluxbox.log 2>&1 &
gosu monitor x11vnc -display :99 -forever -shared -nopw -rfbport 5900 >runtime/logs/x11vnc.log 2>&1 &
gosu monitor websockify --web=/usr/share/novnc 6080 localhost:5900 >runtime/logs/novnc.log 2>&1 &

gosu monitor python -u doubao_dashboard_server.py >runtime/logs/dashboard.out.log 2>runtime/logs/dashboard.err.log &
BACKEND_PID=$!

gosu monitor node yuanbao_monitor/dashboard/node_modules/vinext/dist/cli.js start -H 0.0.0.0 -p 3000 \
  >runtime/logs/frontend.out.log 2>runtime/logs/frontend.err.log &
FRONTEND_PID=$!

if [[ -n "${MONITOR_DATABASE_URL:-}" ]] && [[ -n "${DEEPSEEK_API_KEY:-}${ANTHROPIC_API_KEY:-}${OPENAI_API_KEY:-}" ]]; then
  gosu monitor python -u product_ai_worker.py >runtime/logs/product_ai.out.log 2>runtime/logs/product_ai.err.log &
fi

trap 'kill ${BACKEND_PID} ${FRONTEND_PID} 2>/dev/null || true' TERM INT
wait -n ${BACKEND_PID} ${FRONTEND_PID}
