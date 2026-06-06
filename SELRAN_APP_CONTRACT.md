# Selran App Contract — Selran Canvas (`canvas`)

> **Read this first before changing this app — in any thread.** It is the contract
> between this app and the **Selran Launchpad** platform: how the Launchpad runs it,
> how models + the GPU get chosen, and the rules a change must not break (residency,
> auth, install layout). Generated 2026-05-29 from this app's `selran-app.json`.

## 1. This app at a glance

| field | value |
|---|---|
| id | `canvas` |
| status | active |
| category | writing |
| launch | `spawn` |
| MCP skill id | `canvas` |
| database | postgres · `canvas` |
| **egress ceiling** | **default — but Canvas makes **no model calls of its own**** |
| secrets | — |
| scheduled jobs | — |

Canvas is the companion manuscript/design surface that Writer, Datacore, and Design render into; Claude drives it via MCP tools.

## 2. The platform in one picture

Selran v3 is **two planes**:
- **Control plane** — the Tauri Launchpad app. Opens on demand; runs the first-run
  wizard, installs/builds/updates apps, enters keys, starts/stops the daemon.
- **Data/compute plane** — the **orchestrator daemon**, always-on under launchd,
  bound to **loopback `127.0.0.1:15454` only**. It owns data and brokers compute.

**This app is a thin client of the orchestrator.** It does not own model choice,
the GPU relationship, cloud keys, or cross-app data.

## 3. The five boundary rules (MUST)

1. **Apps → orchestrator only.** Reach models, secrets, and (where adopted) data
   through the loopback API — never around it.
2. **Never call the GPU server / Tailscale box directly.** The app must not know a
   server address; only the orchestrator does, and it routes + fails over.
3. **Never read the OS Keychain directly.** macOS ACLs would prompt every time. Use
   the orchestrator **secret bridge** (`GET /v1/secret/<id>`).
4. **Never hold a cloud API key or call `api.anthropic.com` / `api.openai.com`
   directly.** Route generation through `POST /v1/generate`; the orchestrator holds
   the key and enforces the egress ceiling. (This is exactly what R1-016 fixed in
   context-keeper.)
5. **Send the loopback badge.** Every orchestrator call must carry
   `x-selran-token`. The bundled client reads it from `~/.selran/loopback.badge`
   (or `$SELRAN_APP_TOKEN`) automatically — **don't strip it**. Enforcement is on by
   default; without the badge you get `401` on everything except `/v1/health`.

## 4. How models + the GPU are chosen — *you don't choose*

The app **never** picks a model or talks to the GPU. It calls the routed API:

```
POST /v1/embed     { app: "canvas", texts[] }            -> vectors[]
POST /v1/rerank    { app: "canvas", query, candidates[] } -> scores[]
POST /v1/generate  { app: "canvas", messages[] }          -> text
POST /v1/extract   { app: "canvas", messages[] }          -> text
POST /v1/exec/local{ app: "canvas", code }                -> structured JSON (sandboxed Python)
```

The orchestrator routes each request **local (Mac MLX/Ollama) ↔ server (your GPU
box over Tailscale) ↔ cloud**, by configured policy + payload size, with automatic
failover — **always capped by this app's egress ceiling (§5).** Size heuristic:
batch roles (embed/rerank) prefer the GPU at/above the AUTO threshold; a warm GPU
role is preferred at any size. The brain (generate) is Mac-default + a manual
toggle. An app may pass an optional `prefer` hint, but it can never exceed the
ceiling or reach an unhealthy/forbidden tier.

Local Python analysis (stats, plotting, data prep) runs in the **shared analysis
env `~/.selran/venv`** via `POST /v1/exec/local`, which is sandboxed: **network
denied**, filesystem **fenced** to the per-job scratch dir + the venv (so it can't
read another app's data or PHI).

## 5. This app's compute + residency specifics

**Egress ceiling:** default — but Canvas makes **no model calls of its own**

Canvas **renders**; it does not call `/v1/generate` itself. It has a managed `canvas` Postgres for its own store and is the `companion_starter` target (`python -m selran_canvas`).

**Hard constraints:**
- No model routing of its own. If you ever add server-side generation, route it through the orchestrator.

## 6. Install / data layout (where things live)

- **Code:** installed to `${install_root}/canvas` (copied from the repo by the
  Launchpad; the dev repo is never run in place).
- **Data: NEVER in the app folder.** It lives in the managed Postgres (one DB per
  app) and/or a **per-project** dir under `projects_root`. This is what makes an
  update re-sync (`rsync --delete`) safe.
- **`projects_root`** resolves to `$SELRAN_PROJECTS_ROOT` → `~/.selran/setup.json`
  `projects_root` → default `~/Documents/Selran Projects` (one answer everywhere).
- **Managed Postgres** lives at `~/.selran/pg` (not relocatable). Get this app's DB
  connection string from `GET /v1/db/canvas` (a role URL with the password inline)
  — **don't hardcode** a DSN.
- **Secrets** come from `GET /v1/secret/<id>` (the bridge), never from a file or a
  per-app Keychain namespace. Canonical Keychain service: `design.selran.launchpad`.
- **Updates:** the Launchpad's "Check for updates" re-syncs revised code, then
  re-runs the per-app build + `install.db.migrate`. Keep data out of the app folder.

## 7. How to call the orchestrator (the client)

Use the bundled thin client — `_selran_client.py` (Python) or `selran_client.ts`
(Node) — not your own HTTP. It exposes `db_url()`, `secret()`, `embed()`,
`rerank()`, `generate()`, `extract()` and **auto-attaches the badge**. Copy the
current client from the Launchpad's `integration/selran_client.{py,ts}` if you need
a fresh one.

## 8. Rules for changing this app (the guardrail)

- **DO** keep every embedding / rerank / generation / extraction call going through
  the orchestrator client.
- **DO** get the DB URL from `/v1/db/canvas` and secrets from `/v1/secret/<id>`.
- **DON'T** add a direct cloud-API call, a direct GPU/Tailscale call, a direct
  Keychain read, or write app data into the app folder.
- **DON'T** raise this app's **egress ceiling** without the owner's sign-off — it's
  the residency guarantee.
- **DON'T** strip the `x-selran-token` badge from orchestrator calls.
- After editing source, the user applies it via the Launchpad **"Check for
  updates"** (re-sync + rebuild + migrate) — don't invent a parallel install path.

## Shared user memory

A single, suite-wide record of **who the user is** — name, a one-line bio, role, focus,
and preferences — captured **once** at the Launchpad's first-run **"About you"**
questionnaire and never re-asked by an app. Plus an **append-only memory** of facts
apps learn about the user over time. Both are orchestrator-owned: `~/.selran/user_profile.json`
and `~/.selran/user_memory.jsonl`, served on `127.0.0.1:15454`.

Access (badge-authenticated, `x-selran-token` from `~/.selran/loopback.badge`):
```
GET  /v1/user/profile                          → who the user is (name, role, focus, preferences)
GET  /v1/user/memory?limit=N                   → recent learned facts
POST /v1/user/memory  { app:"canvas", text, kind } → append an observed fact
```
Or via the **Selran hub** (MCP): `user_get_profile`, `user_get_memory`, `user_remember`.

**Rule:** **READ** the profile to personalize (know the user's name/role/preferences);
**APPEND** observations via the memory endpoint/tool; **DON'T** ask the user their
identity (that's the Launchpad's job); **DON'T** overwrite the profile
(`PUT /v1/user/profile` is the Launchpad's first-run write only).

---

*Canonical sources (in the `Selran-Launchpad-V3` repo): `docs/ARCHITECTURE.md` (the
two planes + the five arrows), `docs/DATA_RESIDENCY.md` (egress ceilings),
`docs/MODEL_FABRIC.md` / `docs/MODEL_FABRIC_OPS.md` (routing + GPU), and
`control-plane/INSTALLER_DESIGN.md` (install/update/data layout). If those move, they
win over this copy.*
