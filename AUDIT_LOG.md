# AUDIT_LOG.md — Selran Canvas

Durable record of greenloop `app-audit` rounds. Newest round first. Findings are
durable across rounds: a round never re-derives a previous round's findings; it
re-reads their status (open / fixed / deferred / dismissed). This is the first
greenloop audit round for this repo.

---

## Audit run: 2026-06-17 — Round 1 — categories [1,2,3,4,5,6,7,8,9,10] (all)

**Profile:** standard · **Mode:** non-interactive (DevLoop driver) · **Change-scope:** whole-codebase
**Scope rationale:** No prior AUDIT_LOG round exists, so the "original scope of the latest round"
resolves, per the non-interactive scope rule, to the first-audit default — **all categories**.
**Execution:** Phase 3 parallel reviewers (7 specialized `gl-*` agents), Phase 4.5 blind challenge (`gl-challenger`).

### Pre-commit baseline at start (folded in from pre-commit-verification + smoke harness)

- **Tests:** 70 passed, 0 failed (`pytest tests/`, run against the worktree code via `~/.selran/venv`).
- **Lint (ruff):** clean — "All checks passed!"
- **Typecheck:** N/A — project configures `ruff` only, no `mypy`/`pyright` in CI (mirrors `.github/workflows/test.yml`).
- **Build / import-smoke:** clean — config/store/csl_index/companions/webapp all import; CSL manifest = 106 entries.
- **Smoke / integration harness (per category):**
  - [1+4] HTTP health smoke: **PASS** — server launches (`--http-only`, SQLite), `GET /api/health` → `{"ok":true,"revision":0}`, `GET /` → 200, `GET /api/state` → 200.
  - [2] dev/prod matrix: **PARTIAL** — dev path (SQLite, http-only) exercised and green; prod path (managed Postgres) NOT exercised (no managed PG at audit host). See GL-R1-004.
  - [3] migration / schema idempotency: **PASS** — `Store` opened twice on same SQLite DB; schema re-applied without error (`CREATE TABLE/INDEX IF NOT EXISTS`).
  - [5] webview e2e: **NOT SCAFFOLDED** — no headless-browser harness present; root serves 200 HTML only. (Operational-readiness note, not a blocker.)
  - [7] launch-env probe: **PASS** — `start.sh` venv resolution + `python -m selran_canvas --http-only` launch succeeded.
  - [8] provider mocks (MCP): **PASS** — `build_mcp_server(store, url)` constructs the FastMCP server (10 tools).
  - [9] onboarding clickthrough: **N/A** — no onboarding flow.
- **Harness failures converted to findings:** none (no harness check FAILED; the partial/not-scaffolded items are captured as operational/test-coverage findings, not harness failures).
- **Git:** clean working tree at audit start; now known-dirty: `.codemap/*` (cartographer refresh), `.audit/*`, `AUDIT_LOG.md`.
- **Commit at audit start:** `ec558d3` (v2.0.1).

### Codemap state at start

- **State:** refreshed incrementally (was at `6c479b6`, 7 commits behind → refreshed to `ec558d3`).
- **Files mapped:** 41 (25 python, 4 js, 3 md, 3 json, 3 bash, 1 yaml/toml/html).
- **Stages run:** 1_tags, 2_spec_refs, 3_qualified_names, 4_call_graph.
- **Capabilities caveat:** `functions.json` is empty (Python function extraction did not populate in this script version) and `state.json.capabilities` is empty — per the codemap honesty contract, reviewers did **not** trust the codemap for call graphs/function lists and fell back to Read/Grep. `structure.json` tags + `warnings.json` were usable.
- **Warnings:** 30 (0 high, 30 medium): 1 `__init__.py` duplicate-basename (conventional/benign FP), 29 orphans (expected for a Python app without heavy cross-imports). None blocked the audit.

### Deterministic detectors (Step 3.0)

- **gitleaks** (secrets): clean — no leaks across 33 commits / 1.79 MB.
- **semgrep** (bundled greenloop.yml SAST): 1 hit — `python-sql-string-format` at `migrate_to_pg.py:48`. **Adjudicated clean (false positive):** the formatted identifiers come from the hardcoded `TABLES` constant, not untrusted input; row values use parameterized placeholders. Identifiers can't be parameterized anyway. Recorded as Info-grade, not a finding.
- **osv-scanner** (dependency CVEs, resolved versions): grounds GL-R1-012 — `starlette 0.41.3` carries 7 known CVEs (3 High, CVSS 7.5).
- **No SPEC.md** — `SELRAN_APP_CONTRACT.md` (+ README, GPU_AND_LAUNCHPAD.md, selran-app.json) used as the spec surface for Category 7. spec-bootstrap not invoked (non-interactive; a contract doc exists).

### Challenge summary (Phase 4.5)

Findings challenged: 6 judgment-based High findings via **blind** subagent (`gl-challenger`), plus 5 test-coverage Highs self-verified by negative grep. **Confirmed: all. Refuted: 0. Weakened: 1** (GL-R1-001 impact narrowed from "arbitrary file read" to a bounded 2-level directory escape into the home dir). GL-R1-004 reproduced end-to-end (crash before HTTP bind). No cleared-registry (first round).

---

## Findings (Round 1) — 0 Critical · 10 High · 17 Medium · 11 Low · (1 Info)

> All findings are **open** (first round; nothing fixed yet). Provenance = whole-codebase (no diff base). Ordered severity → category.

### High

**GL-R1-001 [High] Path-traversal directory escape → file disclosure on artifact-read endpoint** — `open`
- Category 3 — Security · `selran_canvas/projects.py:320-322`, `selran_canvas/webapp.py:467-479` · confidence 0.85 · source static-review (blind-confirmed, weakened)
- `read_artifact(slug, subdir, filename)` validates only `filename` (rejects `/`,`..`); `slug` and `subdir` flow unguarded into `_project_path(slug)/subdir/filename`. The read route passes `subdir` straight through, unlike the list route (`webapp.py:459`) which guards it. `GET /api/projects/%2e%2e/artifacts/%2e%2e/<file>` escapes the projects root. **Bounded** (per blind challenge) to a 2-level climb → reads direct-child files of `~/` (e.g. `~/.netrc`, `~/.zsh_history`, `~/.gitconfig`); cannot reach files needing a further `/` hop (e.g. `~/.selran/loopback.badge`). Reachable remotely if chained with GL-R1-011 (no Origin/Host check).
- Fix: apply the list-route guard to `subdir` AND resolve+assert `is_relative_to(PROJECTS_ROOT.resolve())` inside `read_artifact` (the pattern already exists at `webapp.py:294-296`). Effort: small.

**GL-R1-002 [High] Direct internet egress to GitHub bypasses the orchestrator (contract §3.1/§8)** — `open`
- Category 3 — Security / 7 — Spec compliance · `selran_canvas/csl_index.py:98,125` · confidence 0.9 · static-review (blind-confirmed)
- `fetch_style()`/`get_locale()` call `httpx.get("https://raw.githubusercontent.com/...", follow_redirects=True)` directly, reachable via `GET /api/csl/style/{id}.csl` and `GET /api/csl/locale.xml`. Violates §3 rule 1 ("through the loopback API — never around it"), the §5 egress ceiling, and §7 (use the bundled client). Undeclared, un-ceilinged egress; no `x-selran-token` badge.
- Fix: route fetches through the orchestrator or fully pre-bundle styles; if direct fetch stays, pin host, `follow_redirects=False`, declare egress, get owner sign-off. Effort: medium.

**GL-R1-003 [High] Orchestrator client default 300s timeout blocks launch** — `open`
- Category 4 — Error handling · `selran_canvas/_selran_client.py:68,77` · confidence 0.85 · static-review (blind-confirmed)
- `_req(..., timeout=300.0)` via `urllib.request.urlopen`. `load_user_profile()` runs synchronously on the main thread at `__main__.py:96` **before** `_start_http_server`, so a connected-but-silent orchestrator stalls the entire launch (HTTP bind + MCP stdio) for up to 5 minutes before degrading to anonymous.
- Fix: pass a small timeout (~2s) for control-plane calls (user_profile/health/db_url); move the profile read off the boot-critical path. Effort: small.

**GL-R1-004 [High] Postgres unreachable at boot crashes before HTTP bind (respawn crash-loop)** — `open`
- Category 8 — Operational readiness · `selran_canvas/__main__.py:35-39,73`, `selran_canvas/pg_store.py:92,99-106` · confidence 0.9 · static-review (blind-confirmed, reproduced end-to-end)
- `_open_store()` returns `PgStore(url)` whenever `CANVAS_DATABASE_URL` is set, no try/except, no SQLite fallback; `PgStore.__init__` eagerly `psycopg.connect()`s with no retry. `main()` opens the store before binding the port, and `start.sh:59` always exports a fallback DSN — so a down/slow PG at boot kills the process before the port binds, and the `required:true` Launchpad respawns it into a crash loop.
- Fix: bounded connect-retry/backoff; on persistent failure, one actionable log line (or policy-gated SQLite degrade); don't connect eagerly in `__init__` on the boot path. Effort: medium.

**GL-R1-005 [High] `/api/health` never checks the database (lying health check)** — `open`
- Category 10 — Diagnosability · `selran_canvas/webapp.py:190-192`, `pg_store.py:124-126`, `store.py:178-180` · confidence 0.88 · static-review (blind-confirmed)
- Health returns `{ok:true, revision: store.revision()}` where `revision()` is an in-memory counter that never touches the DB. If Postgres drops after boot, real requests 500 while `/api/health` stays 200 — probes are misled, delaying incident detection.
- Fix: execute a cheap liveness query (`SELECT 1`), return 503 on failure, and report backend type + redacted DB target. Effort: small.

**GL-R1-006 [High] Near-zero operational logging / no structured logs / no request IDs** — `open`
- Category 10 — Diagnosability · `selran_canvas/__main__.py:44`, `user_profile.py:25` · confidence 0.85 · static-review
- uvicorn at `log_level="warning"`, `access_log=False`; only an unconfigured `getLogger("selran_canvas")` (no `basicConfig`/handler/file). DB errors mid-request return 500 with the traceback going nowhere durable — incidents leave essentially no trace.
- Fix: configure an app logger (stderr + rotating file under the app home) at INFO, enable access/exception logging, stamp per-request correlation IDs. Effort: medium.

**GL-R1-007 [High] Path-traversal guard has no asserting test (UNMAPPED security promise)** — `open`
- Category 9 — Test coverage · `selran_canvas/projects.py:313-328`, `webapp.py:450-479` · confidence 0.95 · static-review (grep-confirmed)
- No test references projects/artifacts/traversal; the guard could be deleted and all 70 tests stay green. Directly tied to GL-R1-001.
- Fix: add tests asserting traversal inputs return None/400/404 and never escape `PROJECTS_ROOT`, plus a happy-path read. Effort: small.

**GL-R1-008 [High] Postgres backend (production store) has zero asserting tests** — `open`
- Category 9 — Test coverage · `selran_canvas/pg_store.py` (whole module) · confidence 0.95 · static-review (grep-confirmed)
- `PgStore` mirrors the 22-method `Store` interface and is the backend selected whenever `CANVAS_DATABASE_URL` is set, but nothing instantiates or exercises it. The production data path is untested while its SQLite twin has full coverage.
- Fix: parametrize the Store suite against both backends behind a marker that skips cleanly without PG, but runs in CI. Effort: large.

**GL-R1-009 [High] SQLite→Postgres migration is untested (data-loss-risk promise UNMAPPED)** — `open`
- Category 9 — Test coverage · `selran_canvas/migrate_to_pg.py` (whole module) · confidence 0.95 · static-review (grep-confirmed)
- The migration copies every row and documents three correctness promises (SQLite read-only/intact, idempotent UPSERT, preserved timestamps); none are asserted. A migration bug would corrupt/lose user data during the v3 cutover undetected.
- Fix: round-trip test — seed SQLite, migrate, assert counts + preserved timestamps in target, assert SQLite unchanged, assert a 2nd run is idempotent. Effort: medium.

**GL-R1-010 [High] Orchestrator client (badge / loopback-only / secret-bridge) has no test** — `open`
- Category 9 — Test coverage · `selran_canvas/_selran_client.py:56-83,150-153` · confidence 0.85 · static-review (grep-confirmed)
- `_req` is the single chokepoint enforcing two contract MUSTs (attach `x-selran-token`, target loopback). No test asserts the header is set or the URL stays on loopback — a regression (dropped badge, off-loopback base) would pass silently.
- Fix: monkeypatch `urlopen` + token env/badge file; assert the header is attached, the URL is `127.0.0.1`, and `HTTPError→SelranError` mapping. Effort: medium.

### Medium

**GL-R1-011 [Medium] No inbound auth / CORS / Origin / Host validation (drive-by CSRF + DNS-rebinding)** — `open`
- Category 3 — Security · `selran_canvas/webapp.py:47`, `/ws` at `:483-485` · confidence 0.7 · static-review
- No auth dependency, no CORS middleware, no Origin/Host check on any mutating route or the WebSocket. Trust rests solely on loopback binding → a malicious website can drive cross-origin state mutations; DNS-rebinding defeats SOP and (with GL-R1-001) enables cross-origin file exfiltration.
- Fix: Host/Origin allow-list middleware (reject non-loopback Host, foreign Origin); reject foreign-Origin WS upgrades. Effort: medium.

**GL-R1-012 [Medium] Unpinned deps permit vulnerable starlette 0.41.3 (3 High CVEs)** — `open`
- Category 3 — Security · `pyproject.toml:15-25` · confidence 0.8 · detector:osv-scanner
- Floor-only constraints (`fastapi>=0.115.0`, `uvicorn[standard]>=0.32.0`) permit `starlette 0.41.3` (7 CVEs; 3 High CVSS 7.5: GHSA-7f5h-v6xp-fcq8, GHSA-82w8-qh3p-5jfq, GHSA-wqp7-x3pw-xc5r). No lockfile/upper bound.
- Fix: pin starlette to a patched release (and the fastapi/uvicorn floors that require it); add a lockfile/constraints. Effort: small.

**GL-R1-013 [Medium] No FK / cascade between mcqs·comments and pages (both backends)** — `open`
- Category 1 — Schema integrity · `selran_canvas/store.py:35-66`, `pg_store.py:43-74` · confidence 0.8 · static-review
- Docstrings claim `page_id FK`, but both schemas declare plain `TEXT NOT NULL` with no `REFERENCES`; integrity relies on `delete_page` issuing 3 manual DELETEs. Any other delete path or partial failure orphans rows that `get_*` still returns.
- Fix: add `REFERENCES pages(page_id) ON DELETE CASCADE` in both schemas (+ `PRAGMA foreign_keys=ON` for SQLite). Effort: medium.

**GL-R1-014 [Medium] `delete_page` runs 3 separate autocommitted DELETEs — non-atomic** — `open`
- Category 2 — Data flow · `selran_canvas/store.py:298-303`, `pg_store.py:237-242` · confidence 0.85 · static-review
- Both backends connect in autocommit (`isolation_level=None` / `autocommit=True`); a crash after the first DELETE orphans mcqs/comments.
- Fix: wrap the deletes in one transaction (or rely on ON DELETE CASCADE). Effort: medium.

**GL-R1-015 [Medium] `scaffold_pages` inserts N rows + kv non-atomically** — `open`
- Category 2 — Data flow · `selran_canvas/store.py:247-284`, `pg_store.py:189-220` · confidence 0.8 · static-review
- A crash mid-loop leaves a half-scaffolded template under autocommit; the route's created/skipped report can misrepresent on-disk state.
- Fix: run the scaffold body in a single explicit transaction in both backends. Effort: medium.

**GL-R1-016 [Medium] Concurrent writers can assign duplicate `pages.position` (TOCTOU)** — `open`
- Category 5 — Concurrency / 2 — Data flow · `selran_canvas/store.py:237`, `pg_store.py:177-185` · confidence 0.78 · static-review (merged: data-integrity + concurrency reviewers)
- `SELECT COALESCE(MAX(position),-1)+1` then a separate INSERT, under autocommit, with `self._lock` NOT held across the read-modify-write. The MCP main thread and the uvicorn daemon thread are genuinely concurrent (`__main__.py`), and the PG backend permits parallel inserts — two writers read the same MAX and both write position N → nondeterministic ordering / shadowed pages. No UNIQUE(position).
- Fix: hold the lock (or a write transaction / `SELECT … FOR UPDATE`) across the read+insert, or add UNIQUE(position) and retry on conflict. Effort: small.

**GL-R1-017 [Medium] Blocking `snapshot_dict()` called on the async event loop** — `open`
- Category 5 — Concurrency · `selran_canvas/webapp.py:79,504` · confidence 0.8 · static-review
- `snapshot_dict()` issues ~9 synchronous DB queries but is awaited directly in `async` handlers (`/api/state`, the WS broadcast loop) without `run_in_executor` — a contended read (up to the 5s SQLite busy timeout) freezes the whole event loop, stalling every WS push and HTTP request.
- Fix: `await loop.run_in_executor(None, store.snapshot_dict)` (or sync route so FastAPI offloads). Effort: small.

**GL-R1-018 [Medium] Each `/ws` connection pins one default-executor thread** — `open`
- Category 6 — Resource bounds · `selran_canvas/webapp.py:492-498` · confidence 0.7 · static-review
- `loop.run_in_executor(None, listener.wait, 5.0)` in a tight loop holds one default-executor thread (~22 on this host) per open socket. ~22+ tabs / a reconnect storm saturate the shared executor, blocking all `run_in_executor` users.
- Fix: bridge `store._bump` to an `asyncio.Event` via `call_soon_threadsafe` (no thread per conn), or use a dedicated sized executor + cap concurrent `/ws`. Effort: medium.

**GL-R1-019 [Medium] Comment / page / MCQ POST bodies have no size limit** — `open`
- Category 6 — Resource bounds · `selran_canvas/webapp.py:131-153`, `server.py:86` · confidence 0.75 · static-review
- Bodies are `.strip()`+non-empty only, stored verbatim in TEXT columns; every write re-broadcasts the full snapshot to all sockets. A multi-MB comment/page grows the DB unbounded, is loaded fully into memory on every `/api/state`/snapshot, and re-sent on each subsequent edit. Starlette imposes no default body cap.
- Fix: per-field max lengths (route + MCQ/page tools) and/or a body-size middleware returning 413. Effort: small.

**GL-R1-020 [Medium] Artifact read loads entire file into memory (large-file DoS)** — `open`
- Category 6 — Resource bounds · `selran_canvas/projects.py:325-328`, `webapp.py:467-479,280,227,338` · confidence 0.7 · static-review
- `read_artifact` does `f.read_text()` with no size cap, returned inline in JSON; the artifact tree is shared with other skills that may write large exports/caches. Design endpoints also read whole files.
- Fix: stat-and-refuse above a cap (413), or stream via FileResponse / bounded prefix. Effort: small.

**GL-R1-021 [Medium] Manifest egress ceiling (`local+server+cloud`) exceeds the contract's stated "default"** — `open`
- Category 7 — Spec compliance · `selran-app.json:23`, `GPU_AND_LAUNCHPAD.md:9` vs `SELRAN_APP_CONTRACT.md §1/§5` · confidence 0.75 · static-review
- Manifest grants Full egress, but the contract says the ceiling is "default" and "Canvas makes no model calls of its own" (confirmed — zero generate/embed/rerank/extract callsites). Unused over-provisioning of residency reach without sign-off (§8).
- Fix: narrow the manifest egress to match intent, or update the contract; record the residency decision with the owner. Effort: small.

**GL-R1-022 [Medium] `start.sh`/`stop.sh` kill by port, but 12115 is shared with the Writer skill** — `open`
- Category 8 — Operational readiness · `start.sh:48-49`, `stop.sh:9-16`, `config.py:5-8` · confidence 0.78 · static-review
- `lsof -ti tcp:$PORT | xargs kill` (+ `kill -9`) terminates whatever listens on the shared port; a healthy-but-slow Writer-started server (probe timeout 2s) can be killed, dropping its in-flight work.
- Fix: confirm process identity (selran-canvas) or a PID/lock file before killing; widen the liveness probe. Effort: medium.

**GL-R1-023 [Medium] Daemon-thread HTTP server has no graceful drain** — `open`
- Category 8 — Operational readiness · `selran_canvas/__main__.py:42-61,111-115` · confidence 0.72 · static-review
- uvicorn runs in a daemon thread; no SIGTERM handler, no `Server.shutdown()`. `stop.sh` SIGTERM→SIGKILL (0.6s) tears down open WebSockets / in-flight POSTs with no drain or DB-close.
- Fix: install a SIGTERM/SIGINT handler that sets `should_exit` (or run uvicorn in the main thread); lengthen stop.sh grace. Effort: medium.

**GL-R1-024 [Medium] Hardcoded managed-pg fallback DSN can drift from the provisioned DB** — `open`
- Category 8 — Operational readiness · `start.sh:59`, `selran-app.json:27-28`, `migrate_to_pg.py:16` · confidence 0.7 · static-review
- `start.sh` hardcodes `postgresql://canvas@127.0.0.1:15432/canvas` (trust auth, port 15432) as fallback; the manifest declares neither host nor port, so there is no single source of truth. A different provisioned port/auth silently points Canvas at the wrong DB → (with GL-R1-004) a boot crash.
- Fix: source the DSN from one orchestrator-provided value; fail with an explicit message if absent rather than guessing. Effort: small.

**GL-R1-025 [Medium] `--info` reports the would-be backend, not the actual one; no connectivity check** — `open`
- Category 10 — Diagnosability · `selran_canvas/__main__.py:75-84` · confidence 0.6 · static-review
- `--info` picks the backend purely from whether `CANVAS_DATABASE_URL` is non-empty and prints the DSN with no connect attempt and no credential redaction — a misleading green diagnosis when PG is unreachable.
- Fix: attempt a short liveness check and redact the DSN password. Effort: small.

**GL-R1-026 [Medium] Shared user-profile reader and `/api/user` have no asserting test (privacy)** — `open`
- Category 9 — Test coverage · `selran_canvas/user_profile.py:34-101`, `webapp.py:85-97` · confidence 0.85 · static-review (grep-confirmed)
- The contract's "read-only, never overwrite, degrade gracefully" shared-memory promise is unmapped; `identity_line()` field-gating and `/api/user` are unexercised.
- Fix: tests for exists:true/false/raises → cached/graceful, identity_line emits only present fields, `/api/user` with a stubbed profile. Effort: medium.

**GL-R1-027 [Medium] Project & design HTTP routes (create/list/get + design save/use-starter) have no tests** — `open`
- Category 9 — Test coverage · `selran_canvas/webapp.py:211-356,394-448` · confidence 0.9 · static-review (grep-confirmed)
- These routes write files under `PROJECTS_ROOT` and embed a second `_validated_project_slug` traversal guard (`webapp.py:285-298`) that no test asserts.
- Fix: route tests with `PROJECTS_ROOT` → tmp_path, including an out-of-root slug rejection. Effort: medium.

### Low

**GL-R1-028 [Low] JSON columns stored unvalidated; one malformed row crashes the whole snapshot** — `open`
- Category 2 — Data flow · `selran_canvas/store.py:354,459,477`, `pg_store.py:294,386,404` · confidence 0.65 · static-review
- Read-side `json.loads` is unguarded; a non-JSON `options_json`/`csl_json`/`companions_json` raises `JSONDecodeError` in `snapshot()`, taking down `/api/state` + the WS broadcast for every client.
- Fix: validate JSON at write time and/or guard read-side `json.loads` to skip/log the bad row. Effort: medium.

**GL-R1-029 [Low] Enum-like fields (`comments.status`, viewing_mode, visual_theme) are unconstrained TEXT** — `open`
- Category 1 — Schema integrity · `selran_canvas/store.py:52`, `pg_store.py:60`, `webapp.py:185` · confidence 0.6 · static-review
- Value sets live only in scattered Python (one writer validates `viewing_mode`; `set_kv` and the MCP theme setter don't), so backends/paths can drift to out-of-range values the frontend can't render.
- Fix: CHECK constraints (or PG ENUM) + centralize validation in `set_kv`. Effort: small.

**GL-R1-030 [Low] Check-then-act on resolve/answer: post-action counts can misreport under concurrent delete** — `open`
- Category 5 — Concurrency · `selran_canvas/server.py:156-158,187-189` · confidence 0.55 · static-review
- The MCP tool recounts `open_remaining`/`pending_remaining` with a separate query after the UPDATE; a concurrent browser delete between UPDATE and recount yields a misleading ack to Claude (outcome bounded).
- Fix: compute the count in the same transaction as the mutation, or document it as advisory. Effort: small.

**GL-R1-031 [Low] Lazy CSL fetch writes the full response with no size bound; cache dir unbounded** — `open`
- Category 6 — Resource bounds · `selran_canvas/csl_index.py:98,101,125,130` · confidence 0.55 · static-review
- `httpx.get(...).text` written to disk with no Content-Length check; unknown ids fall through to a raw Zotero id (`csl_index.py:47`), and many distinct ids accumulate `.csl` files with no eviction.
- Fix: cap response size, validate `csl_id` against the manifest/allowlist, add a cache ceiling/LRU. Effort: medium. (Related to GL-R1-002.)

**GL-R1-032 [Low] `api_design_starters` reads each starter file with no try/except → unhandled 500** — `open`
- Category 4 — Error handling · `selran_canvas/webapp.py:279-282` · confidence 0.5 · static-review
- A permissions/race/IO error on the read escapes as a generic 500, inconsistent with the codebase's otherwise-careful degradation.
- Fix: wrap the per-file read in `try/except OSError` and skip unreadable starters. Effort: small.

**GL-R1-033 [Low] `PgStore` opens a fresh connection per op with no retry/connect-timeout → 500 on transient PG blips** — `open`
- Category 4 — Error handling · `selran_canvas/pg_store.py:95,99` · confidence 0.5 · static-review
- A PG restart/failover surfaces as an unhandled 500 across routes / WS teardown; no connect_timeout (vs SQLite's `timeout=5.0`).
- Fix: add `connect_timeout` + bounded retry/backoff (or a pool with health checks); map `OperationalError`→503. Effort: medium. (Related to GL-R1-004.)

**GL-R1-034 [Low] Default state backend is local SQLite while manifest/contract declare managed Postgres** — `open`
- Category 7 — Spec compliance · `selran_canvas/__main__.py:35-39`, `config.py:35,84`, `selran-app.json:27` · confidence 0.55 · static-review
- PG is used only when `CANVAS_DATABASE_URL` is injected; otherwise a SQLite file under `~/.selran-canvas` (outside the app folder, so §6 safety holds) — but the declared `db.kind=postgres` store-of-record diverges by default.
- Fix: confirm the orchestrator always injects the DSN in production, or fail-closed under the orchestrator; document the SQLite fallback as dev-only. Effort: small.

**GL-R1-035 [Low] README self-contradicts on how many CSL styles ship bundled** — `open`
- Category 7 — Spec compliance · `README.md:162,304,73,8` · confidence 0.85 · static-review
- "Only Vancouver is bundled as XML" vs "all 78 unique CSL files bundled" vs "100+ styles / works fully offline"; repo ships 78 `.csl` while the manifest lists 106 → the offline/"100+" promise is only partially true (28 styles need the GL-R1-002 fetch).
- Fix: reconcile the counts and state which styles are offline vs fetched. Effort: small.

**GL-R1-036 [Low] No validation of `CANVAS_DATABASE_URL` — any non-empty value routes to Postgres** — `open`
- Category 8 — Operational readiness · `selran_canvas/__main__.py:35-38` · confidence 0.55 · static-review
- A malformed DSN or stray value is handed to psycopg and raises a low-level driver error at boot (→ GL-R1-004 crash) with no friendly message; no `postgresql://` scheme check.
- Fix: validate the DSN scheme before constructing `PgStore`; emit a clear error naming the offending value's shape. Effort: small.

**GL-R1-037 [Low] Port-retry spans only 5 ports + races the real bind; `install.sh` `selran-mcp install` not fully idempotent** — `open`
- Category 8 — Operational readiness · `selran_canvas/config.py:21,69-76`, `__main__.py:99`, `install.sh:12,26` · confidence 0.5 · static-review
- Bind-test socket is closed before uvicorn binds (TOCTOU → opaque OSError); `install.sh` claims idempotent but unconditionally re-runs `selran-mcp install` (registry mutation) with no "already wired" guard.
- Fix: configurable/clearer port-range behavior; guard the MCP wiring behind a status check. Effort: small.

**GL-R1-038 [Low] No test pins Store ↔ PgStore interface + schema parity** — `open`
- Category 9 — Test coverage · `selran_canvas/store.py:471` vs `pg_store.py:398`, `migrate_to_pg.py:32` · confidence 0.6 · static-review
- The two backends + the migration `TABLES` rely on column/method parity, but nothing asserts they stay in sync — a column/method added to one diverges silently (the migration drops it). Cheap structural test, no live PG needed.
- Fix: parity test — `dir(Store)==dir(PgStore)` public set; `migrate_to_pg.TABLES` columns == PG DDL. Effort: small.

### Info (not counted as a finding)

**GL-R1-INFO-1 [Info] Badge header is `x-selran-token` (sent correctly); there is no `X-Selran-Local` header** — `n/a`
- Category 7 — Spec compliance · `selran_canvas/_selran_client.py:73-75` · COMPLIANT
- §3.5 requires `x-selran-token`; the code attaches it from `$SELRAN_APP_TOKEN`/`~/.selran/loopback.badge`. The "X-Selran-Local" name in the audit framing does not correspond to the contract. No code change. Also adjudicated clean: the semgrep `python-sql-string-format` hit at `migrate_to_pg.py:48` (identifiers from a constant, values parameterized).

---

### Patterns observed
- **Postgres path is the weakest surface.** GL-R1-004/005/008/009/010/033/036 all converge on the managed-PG path: untested, fails-closed-without-recovery at boot, with a lying health check and no logging to diagnose it. Fixing boot resilience + a real health probe + PG tests resolves a cluster.
- **Two genuinely-concurrent writer threads, no write serialization.** GL-R1-014/015/016/017 trace to autocommit + a lock that guards only the revision counter/listeners, not the DB read-modify-write.
- **The orchestrator boundary leaks in one place** (GL-R1-002 direct GitHub egress) and is otherwise well-respected (badge, loopback-only, secret bridge, no model calls — all verified compliant).

### Audit coverage declaration
- **Scope this round:** Categories 1–10 (all), whole-codebase.
- **Items covered:** all scoped categories reviewed by specialized reviewers + deterministic detectors (gitleaks/semgrep/osv) + a runtime smoke harness.
- **Could not verify (and why):**
  - Postgres runtime behavior (concurrency/migration under load) — no managed PG at the audit host; reviewed statically only.
  - Webview/UI end-to-end behavior — no headless-browser harness scaffolded.
- **Confidence:** HIGH that the listed High findings are real (6 blind-confirmed, 1 reproduced end-to-end, 4 grep-confirmed); MODERATE on the Medium set; Low/Info not exhaustively pursued.
- **Out of scope:** none (all categories included).
- **Recommended next round:** re-verify after fixes, with a live Postgres so GL-R1-008/009/016 can be exercised dynamically.

### Auditor notes / handoff
- This round made **no code changes** (read-only review window enforced; findings only).
- Open `smoke-harness`/`detector`-sourced findings: none blocking — pre-commit baseline is GREEN; GL-R1-012 is detector-sourced (osv) and is re-verified by re-scanning deps.
- Natural next step (not auto-invoked): `audit-fix` — order by blast radius, pre-commit after each fix, re-audit when done. Start with the Postgres-resilience cluster (GL-R1-004/005) and the path-traversal pair (GL-R1-001/007).

## Audit-fix pass — 2026-06-18 (in-session, branch claude/greenloop-fix, not pushed)

Pure-Python local app (orchestrator-routed, no docker) → test-verifiable. Baseline 70 passed → **74 passed** (+4 new tests).

### FIXED & verified (9) — 6 commits
**Highs (7):** GL-R1-001 (read_artifact path-traversal — guard all components + is_relative_to) · GL-R1-007 (traversal test) · GL-R1-003 (control-plane timeout 300s→5s; LLM calls keep 300s) · GL-R1-005 (real /api/health DB probe; store.ping) · GL-R1-006 (logging.basicConfig + uvicorn info/access logs) · GL-R1-004 (bounded Postgres connect-retry at boot; no crash-loop) · GL-R1-010 (orchestrator-client badge+loopback+error-mapping tests).
**Med/Low (2):** GL-R1-014 (delete_page atomic on both backends) · GL-R1-032 (design-starters skip unreadable file, no 500).

### ⚠️ FLAGGED — Highs
- **GL-R1-002** direct GitHub egress bypasses orchestrator (contract §3.1/§8). Route via orchestrator OR pre-bundle styles + (if direct) pin host/`follow_redirects=False`/declare egress. Orchestrator-contract → flagged per owner.
- **GL-R1-008** PgStore (production backend) has zero tests → parametrize the Store suite against both backends behind a PG-skip marker (needs PG/CI). Large.
- **GL-R1-009** SQLite→Postgres migration untested → round-trip test (seed→migrate→assert counts/timestamps/idempotent). Needs PG.

### ⚠️ FLAGGED — Mediums
GL-R1-011 (no inbound auth/CORS/Origin/Host — DNS-rebinding/CSRF; trust-model + risk of breaking the local UI without a browser test) · **GL-R1-012 (unpinned deps permit vulnerable starlette 0.41.3 — add `starlette>=0.47` floor + bounds; pyproject = hard-rule, needs approval)** · GL-R1-013 (schema FK ON DELETE CASCADE — schema change, needs approval; runtime cascade already atomic via GL-R1-014) · GL-R1-015 (scaffold_pages non-atomic) · GL-R1-016 (duplicate `pages.position` TOCTOU) · GL-R1-017 (blocking snapshot_dict on event loop) · GL-R1-018 (ws pins executor thread) · GL-R1-019 (POST bodies no size limit) · GL-R1-020 (artifact read loads whole file) · GL-R1-021 (egress ceiling exceeds contract default — manifest/contract) · GL-R1-022 (port 12115 shared with Writer skill) · GL-R1-023 (no graceful drain) · GL-R1-024 (hardcoded PG fallback DSN drift) · GL-R1-025 (--info reports would-be backend) · GL-R1-026/027 (user-profile + project/design routes untested).

### ⚠️ FLAGGED — Lows
GL-R1-028 (unvalidated JSON columns) · 029 (enum-like unconstrained TEXT) · 030 (resolve/answer check-then-act) · 031 (CSL fetch unbounded write/cache) · 033 (PgStore per-op connect no retry/timeout) · 034 (default SQLite vs declared PG) · 035 (README CSL-count contradiction) · 036 (no CANVAS_DATABASE_URL validation) · 037 (port-retry 5-port race; install idempotency) · 038 (no Store↔PgStore parity test).

**canvas pass: 9 fixed & verified (6 commits, branch claude/greenloop-fix, not pushed); 29 flagged with precise recorded fixes (1 orchestrator-contract + 1 dep-pin/pyproject + 1 schema-FK need approval; rest mechanical/test/PG/lower-conf). Every finding triaged. Suite green 74 passed.**
