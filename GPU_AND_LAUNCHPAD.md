# GPU & Launchpad — how this app uses (or doesn't use) the GPU

*Generated 2026-05-31. This is the shared explainer dropped into every Selran app repo so
the plan is unambiguous locally. The **authoritative** per-app fact is the egress
ceiling in this app's `selran-app.json` (`install.egress`); the box below reflects it.*

> **This app — `canvas`**
>
> - **Egress ceiling:** `local+server+cloud` (Full)
> - **May the GPU run this app's work?** **Yes**
> - **May this app's data reach the cloud?** Yes
> - **What this app holds:** the canvas / slide artifacts and the text rendered on them
>
> The GPU is allowed, and so is the cloud. The orchestrator picks Mac vs. GPU vs. cloud per request by policy, health, and batch size (§4); you never choose the machine and never address the GPU directly.

---

## 1. What the GPU is

The **GPU** is a single Linux workstation on your **private Tailscale network** — your
own hardware, not a public cloud. Reference box: a Threadripper with **two Blackwell
GPUs** (an RTX PRO 6000 96 GB + an RTX 5090 32 GB, ~128 GB VRAM total) and 256 GB RAM.

It runs one service, **`selran_server`** (a FastAPI app), which serves the Selran model
roster with **vLLM + TEI**:

| Role | Model (reference) | What it does |
|------|-------------------|--------------|
| `embed`    | Qwen3-Embedding-8B | text → vectors (semantic search / RAG indexing) |
| `rerank`   | Qwen3-Reranker-8B  | re-order candidates by relevance to a query |
| `generate` | Qwen3 35B-A3B "brain" (MoE) | reasoning / long-form generation |
| `extract`  | Qwen3 4B instruct  | structured extraction (non-thinking) |

Two properties make the GPU safe to use as a tier:

- **It is stateless.** The orchestrator sends text, gets back vectors / scores / text.
  *The server persists nothing* — no logs of your content, no database, no cache of PHI.
- **Dynamic VRAM residency.** Each role lazy-loads on first use and is **evicted after
  it goes idle** (default 900 s), freeing VRAM. So "warm" vs. "cold" matters for speed
  (§4), and idle models don't hog the card.

You address it through endpoints `/health /embed /rerank /generate /extract`. **But your
app never calls those directly** — see §3.

## 2. The three tiers and the ceiling

Every app declares an **egress ceiling** — the furthest its data may travel:

```
Local   = the Mac          (Apple-Silicon: MLX embed/rerank, Ollama brain, sandboxed local analysis)
Server  = the GPU box       (your Tailscale network — fast, private, your hardware)
Cloud   = OpenAI / Anthropic (public providers)

local-only         -> { Local }                  data never leaves the laptop
local+server       -> { Local, Server }          laptop + your GPU, never cloud
local+server+cloud -> { Local, Server, Cloud }   everything (a.k.a. "Full")
```

The GPU is the **Server** tier. So "can this app use the GPU?" is exactly "does its
ceiling include `server`?" — i.e. `local+server` or `local+server+cloud`. A `local-only`
app can **never** reach the GPU; that is enforced in code (`residency.rs`,
`Ceiling::LocalOnly.allows(Server) == false`) and unit-tested.

## 3. How the Launchpad works with the GPU (and why your app never sees it)

The **Launchpad orchestrator** (a loopback daemon at `127.0.0.1:15454`) is the **single
chokepoint**. Your app does **not** know the GPU's address, does not hold a Tailscale key,
and does not open a socket to the box. Instead:

```
your app --(loopback, app id attached)--> orchestrator /v1/{embed|rerank|generate|extract}
                                              | 1. look up THIS app's ceiling
                                              | 2. probe health of Local / Server / Cloud
                                              | 3. keep only providers the ceiling allows AND that are up
                                              | 4. pick the best survivor (section 4); fail over if it errors
                                              v
                              Mac  -- or --> GPU box  -- or --> Cloud
your app <---------------- vectors / scores / text ---------------+
```

You name **what** you need (an embedding, a rerank, a generation); the orchestrator
decides **where** it runs. This is what makes residency a *guarantee* instead of a hope:
there is exactly one place that can talk to the GPU, and it checks the ceiling every time.

**Hard rules for this app's code:**

- ✅ Call the orchestrator (`/v1/embed`, `/v1/rerank`, `/v1/generate`, `/v1/extract`, or
  the Selran client SDK) and **always pass your app id** so the ceiling is applied.
- ❌ **Never** call the GPU box directly (no Tailscale hostname/IP in app code or config).
- ❌ **Never** embed model endpoints or provider API keys in the app — the orchestrator
  owns those.
- ❌ **Never** try to "upgrade" your own tier in code to reach the GPU or cloud — the
  ceiling lives in the manifest and is enforced server-side regardless.

## 4. How the orchestrator chooses Mac vs. GPU

For the batch roles (`embed`, `rerank`) the choice is **warm-aware and size-aware**
(measured, not guessed):

- A **warm** GPU beats the Mac at *every* batch size (~4–6× per item) → prefer GPU.
- A **cold** GPU loses on small jobs (a 10–30 s model reload dominates) → small/one-off
  work stays on the Mac; only **bulk** work (≥ a threshold) is worth waking the GPU.

For `generate` (the brain) the default is Mac, with a manual "use the GPU for this one"
preference knob. In all cases:

- **Health-gated:** if the GPU is unreachable, the request silently falls back to the Mac
  (and then cloud, only if the ceiling allows). Availability never blocks you.
- **Ceiling always wins:** a per-request "prefer server/cloud" hint can only reorder
  *within what the ceiling already permits* — it can never punch through residency.

## 5. Two kinds of compute — model inference vs. local analysis

There are two very different workloads, and they route differently:

- **Model inference** (`embed`/`rerank`/`generate`/`extract`) — GPU-eligible for apps whose
  ceiling includes `server`. This is what the GPU box exists for.
- **Statistical / heavy numeric work** (e.g. datacore's hypothesis tests: t-test, ANOVA,
  regression, survival, sample-size simulations) — these are **CPU/numeric** (scipy /
  statsmodels / pandas) and run in the **sandboxed local analysis env** on the Mac via
  `/v1/exec/local`: a hard timeout, a per-job scratch dir, **network denied** to the
  analysis code, and a filesystem fence so it can't read other apps' data or PHI. Stats
  do **not** need a GPU, and for `local-only` apps they must stay on the Mac anyway.

## 6. Calling it (the pattern)

Through the Selran client SDK (Python) or MCP — never raw HTTP to the box:

```python
from selran_client import Orchestrator
orch = Orchestrator()                      # talks to 127.0.0.1:15454 with the loopback badge

# Embeddings — orchestrator routes Mac vs GPU per ceiling + batch size:
vecs = orch.embed(app="canvas", texts=chunks)

# Rerank:
scored = orch.rerank(app="canvas", query=q, candidates=cands)

# Generation (the brain):
out = orch.generate(app="canvas", messages=msgs)
```

The `app="canvas"` argument is **not optional** — it is how the ceiling is applied. Omit
it and the orchestrator fails *safe* (treats it as a no-cloud app), it does not fail open.

## 7. The de-identified carve-out (only relevant if you hold sensitive data)

If an app needs the GPU for data it normally keeps local (the obvious case: datacore
wanting GPU-accelerated analysis on a very large dataset), the data may **not** be sent
raw. The designed path — **not yet enabled, a deliberate per-artifact decision**:

1. Run a **PHI sweep** first (datacore already has `sada_run_phi_sweep` / `check_phi`).
2. Send only **aggregate, de-identified, or synthetic** data across the boundary —
   never raw PHI rows.
3. Promote the tier for **that specific job/artifact** (not the whole app), recorded in
   the audit trail.

Until that path is turned on, the rule is simple and absolute: **`local-only` apps run all
compute on the Mac.** Raising it is a decision the user makes per dataset, never something
app code does on its own.

## 8. Health & observability

- `GET /v1/health` on the orchestrator reports which tiers are up and which GPU roles are
  currently **warm** (resident in VRAM).
- Every cross-tier decision is **audited** by the orchestrator (which app, which tier,
  allow/deny), so you can always answer "where did this run?"

## 9. Where to read more

- This app's **`SELRAN_APP_CONTRACT.md`** — the binding rules for this repo (read it first).
- Launchpad **`docs/MODEL_FABRIC.md`** + **`docs/MODEL_FABRIC_OPS.md`** — the routing policy
  and the warm/size heuristics.
- Launchpad **`docs/SERVER.md`** + `server/SERVER_HANDOFF.md` — the GPU box: the exact
  Mac<->server contract, setup, and security.
- Launchpad **`docs/DATA_RESIDENCY.md`** — the ceiling model and the two-layer egress design.

---
*If anything here conflicts with this app's `selran-app.json` (`install.egress`) or its
`SELRAN_APP_CONTRACT.md`, those win — they are the source of truth; this doc explains them.*
