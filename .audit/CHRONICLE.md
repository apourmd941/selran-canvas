# Audit Chronicle — Selran Canvas

Per-repo lessons and cleared findings, carried forward between rounds. Keep terse.

## Lessons (repo gotchas)

- **Run tests/harness against the worktree, not the installed package.** `~/.selran/venv`
  has the runtime deps + `pytest-asyncio` but is an editable install pointing at the *main*
  repo. To exercise this worktree's code, run with `PYTHONPATH="$PWD" ~/.selran/venv/bin/python`
  (cwd shadows the editable install). System `python3` lacks fastapi/uvicorn.
- **Smoke harness:** launch with `XDG_DATA_HOME=$(mktemp -d)` + `SELRAN_CANVAS_PORT=<free>` +
  `SELRAN_CANVAS_AUTO_OPEN=0`, no `CANVAS_DATABASE_URL` (→ SQLite). Health = `GET /api/health`.
  Store API: `snapshot()` not `get_state()`; `build_mcp_server(store, http_url)` needs the url arg.
- **Two concurrent writer threads are real:** uvicorn in a daemon thread (`__main__.py`) + MCP
  stdio in the main thread, both sharing one `Store`. `self._lock` guards only the revision
  counter + listener set, NOT DB read-modify-write. Connections are per-op (no cross-thread sqlite
  handle sharing — that hazard is correctly avoided).
- **Two store backends must stay in lockstep:** `store.py` (SQLite) and `pg_store.py` (Postgres)
  + `migrate_to_pg.py` `TABLES`. They are column/method-equivalent today; a change to one without
  the others drifts silently (no parity test — GL-R1-038).
- **`migrate_to_pg.py:48` semgrep SQL-format hit is a known false positive** — identifiers come
  from the hardcoded `TABLES` constant; values are parameterized. Don't re-flag as injection.
- **Contract badge is `x-selran-token` (not `X-Selran-Local`).** The orchestrator client attaches
  it correctly; loopback-only, secret-bridge, and no-model-calls are all verified compliant. The
  one boundary leak is direct GitHub egress in `csl_index.py` (GL-R1-002).
- **Codemap `functions.json` is empty in this script version** (no Python fn extraction); don't
  trust the codemap for call graphs — use Read/Grep.

## Cleared registry (findings REFUTED in challenge)

- (none — Round 1 refuted 0; GL-R1-001 was *weakened*, not cleared: impact bounded to a 2-level
  directory escape into the home dir, still a real finding.)
