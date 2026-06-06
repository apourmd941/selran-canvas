#!/usr/bin/env bash
# Canvas launcher — Launchpad Mechanism 1 (one process serves UI + API on one
# port). See the Launchpad's docs/APP_LAUNCH_MODEL.md.
#
# Why this script exists (not a bare `selran-canvas` spawn):
# The Launchpad spawns the launch command through `bash -lc`, so a bare
# `selran-canvas` only resolves if a venv that has it happens to be on the login
# PATH — usually none is, so Canvas silently never comes up. Worse, the repo's
# own `.venv` can be STALE (missing the `psycopg` DB driver that Canvas needs for
# its v3 Postgres backend), which crashes the process on startup.
#
# So we pick a venv that ACTUALLY has Canvas's runtime deps — `psycopg` AND the
# `selran_canvas` package — preferring the orchestrator-provisioned
# `~/.selran/venv` (the v3 source of truth, which `pip install -e .` populates
# with psycopg), then falling back to the repo's own `.venv`, then PATH.
set -uo pipefail
cd "$(dirname "$0")"

PORT="${SELRAN_CANVAS_PORT:-12115}"

# Already serving (a prior launch, or Writer started the shared server)? Hand
# off — the Launchpad opens the window at then_open_url.
if curl -sf -m 2 "http://127.0.0.1:${PORT}/" >/dev/null 2>&1; then
  echo "[canvas] already running on :${PORT} — handing off."
  exit 0
fi

# Find a selran-canvas whose venv imports BOTH psycopg and selran_canvas.
CANVAS_BIN=""
for b in "${HOME}/.selran/venv/bin/selran-canvas" "${HOME}/NeutronDev/"*[Cc]anvas*/.venv/bin/selran-canvas; do
  [ -x "$b" ] || continue
  if "$(dirname "$b")/python" -c "import psycopg, selran_canvas" >/dev/null 2>&1; then
    CANVAS_BIN="$b"; break
  fi
done
# Last resort: a selran-canvas on PATH (may lack deps, but better than nothing).
if [ -z "$CANVAS_BIN" ] && command -v selran-canvas >/dev/null 2>&1; then
  CANVAS_BIN="$(command -v selran-canvas)"
fi
if [ -z "$CANVAS_BIN" ]; then
  echo "[canvas] ERROR: no selran-canvas venv with psycopg + selran_canvas found." >&2
  echo "[canvas]        Re-run the Launchpad setup/provision for Canvas (it installs" >&2
  echo "[canvas]        psycopg + the package into ~/.selran/venv)." >&2
  exit 1
fi

# Reclaim a stale instance on our port, then run the one process.
pids=$(lsof -ti tcp:"$PORT" -sTCP:LISTEN 2>/dev/null || true)
[ -n "$pids" ] && { echo "[canvas] reclaiming :$PORT"; echo "$pids" | xargs kill 2>/dev/null || true; sleep 1; }

echo "[canvas] starting ${CANVAS_BIN} on :${PORT} (UI + API, one process)…"
# AUTO_OPEN=0 so the server does NOT pop its own browser — the Launchpad opens
# the window. CANVAS_DATABASE_URL is exported by the Launchpad (install.env).
export SELRAN_CANVAS_PORT="${PORT}"
export SELRAN_CANVAS_AUTO_OPEN=0
exec "$CANVAS_BIN"
