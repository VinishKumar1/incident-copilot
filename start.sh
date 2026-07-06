#!/usr/bin/env bash
# Starts the backend and keeps the Grafana token fresh automatically.
# Usage: ./start.sh

set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"

echo "🚀  Starting K8s Issue Assistant…"

# ── 1. Initial token refresh ───────────────────────────────────────────────────
echo "🔑  Getting fresh Grafana token…"
"$DIR/refresh_grafana_token.sh"

# ── 2. Start backend ──────────────────────────────────────────────────────────
echo "🖥   Starting backend on :8077…"
cd "$DIR/backend"
nohup ./.venv/bin/python -m uvicorn app.main:app --port 8077 --reload \
  > /tmp/k8s-assistant-backend.log 2>&1 &
BACKEND_PID=$!
echo "    Backend PID: $BACKEND_PID"

# Wait for backend to be ready
for i in {1..10}; do
  sleep 1
  if curl -s http://localhost:8077/healthz | grep -q "ok"; then
    echo "✅  Backend ready."
    break
  fi
done

# ── 3. Auto-refresh token every 25 minutes in the background ──────────────────
echo "⏰  Auto-refresh loop started (every 25 min)…"
(
  while true; do
    sleep 1500  # 25 minutes
    echo "[$(date)] Refreshing Grafana token…" >> /tmp/grafana-token-refresh.log
    "$DIR/refresh_grafana_token.sh" >> /tmp/grafana-token-refresh.log 2>&1
  done
) &
REFRESH_PID=$!
echo "    Refresh loop PID: $REFRESH_PID"

echo ""
echo "✅  All running. Open http://localhost:5199"
echo "    Backend logs : tail -f /tmp/k8s-assistant-backend.log"
echo "    Token refresh: tail -f /tmp/grafana-token-refresh.log"
echo ""
echo "    To stop everything:"
echo "    kill $BACKEND_PID $REFRESH_PID"
