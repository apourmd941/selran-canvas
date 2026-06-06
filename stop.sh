#!/usr/bin/env bash
# Stop the Selran Canvas companion. The Launchpad runs this when its window is
# closed (close-to-stop). Canvas serves its UI on 12115; free that port
# (SIGTERM first, then SIGKILL).
set -uo pipefail

PORT="${1:-12115}"

pids="$(lsof -ti tcp:"$PORT" -sTCP:LISTEN 2>/dev/null || true)"
if [ -n "$pids" ]; then
  echo "[canvas/stop] stopping server on :$PORT (pids: $pids)"
  for p in $pids; do kill "$p" 2>/dev/null || true; done
  sleep 0.6
  for p in $(lsof -ti tcp:"$PORT" -sTCP:LISTEN 2>/dev/null || true); do
    kill -9 "$p" 2>/dev/null || true
  done
  echo "[canvas/stop] stopped."
else
  echo "[canvas/stop] nothing listening on :$PORT"
fi
