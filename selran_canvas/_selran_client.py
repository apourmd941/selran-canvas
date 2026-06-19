"""Selran client — the thin adapter an app uses to talk to the orchestrator's
loopback API (M6 / ARCHITECTURE.md §6). Copy this into an app and replace its
bespoke DB / secret / model-call code with these functions. Stdlib only — no
third-party dependencies, so it drops into any Python app.

Resolution order for the orchestrator URL:
  1. $SELRAN_ORCHESTRATOR_URL
  2. ~/.selran/orchestrator.toml  [api] bind/port
  3. http://127.0.0.1:15454  (default)

The per-app badge token comes from $SELRAN_APP_TOKEN and is sent as the
`x-selran-token` header (inert until enforcement is enabled — ORCHESTRATOR.md §5).

Adoption is incremental (ARCHITECTURE.md §6):
  depth (a) provision-only: use db_url()/secret() for the app's DB + keys.
  depth (b) full:           also route embed()/rerank()/generate() here.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

DEFAULT_PORT = 15454


class SelranError(Exception):
    """Any failure talking to the orchestrator (unreachable, non-2xx, decode)."""


def _base_url() -> str:
    env = os.environ.get("SELRAN_ORCHESTRATOR_URL")
    if env:
        return env.rstrip("/")
    # Best-effort parse of ~/.selran/orchestrator.toml [api] (no toml dependency).
    try:
        text = (Path.home() / ".selran" / "orchestrator.toml").read_text()
        bind, port, in_api = "127.0.0.1", DEFAULT_PORT, False
        for raw in text.splitlines():
            s = raw.strip()
            if s.startswith("["):
                in_api = s == "[api]"
            elif in_api and s.startswith("port"):
                port = int(s.split("=", 1)[1].strip())
            elif in_api and s.startswith("bind"):
                bind = s.split("=", 1)[1].strip().strip('"')
        return f"http://{bind}:{port}"
    except Exception:
        return f"http://127.0.0.1:{DEFAULT_PORT}"


def _loopback_badge() -> Optional[str]:
    """The orchestrator-minted loopback badge (R1-020) — a same-user secret at
    ~/.selran/loopback.badge that authenticates local Selran processes to the
    loopback API. Read it as a fallback so any app authenticates with no env
    wiring; None if absent (e.g. enforcement off / daemon never started)."""
    try:
        tok = (Path.home() / ".selran" / "loopback.badge").read_text().strip()
        return tok or None
    except OSError:
        return None


def _req(method: str, path: str, body: Optional[dict] = None, timeout: float = 5.0) -> Any:
    # GL-R1-003: short default so a silent orchestrator can't stall boot for 5 min
    # (load_user_profile runs before the HTTP bind). LLM calls (embed/rerank/generate)
    # pass an explicit long timeout.
    url = _base_url() + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("content-type", "application/json")
    token = os.environ.get("SELRAN_APP_TOKEN") or _loopback_badge()
    if token:
        req.add_header("x-selran-token", token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        raise SelranError(f"{method} {path} -> {e.code}: {detail}") from None
    except urllib.error.URLError as e:
        raise SelranError(f"orchestrator unreachable at {url}: {e}") from None


# ── data plane ──────────────────────────────────────────────────────────────

def db_url(app: str) -> str:
    """Managed Postgres connection string for this app (role URL)."""
    return _req("GET", f"/v1/db/{app}")["url"]


def secret(secret_id: str) -> str:
    """A canonical Keychain secret (e.g. 'anthropic'), via the orchestrator bridge."""
    return _req("GET", f"/v1/secret/{secret_id}")["value"]


def provision(app: str) -> dict:
    """Ensure this app's Postgres DB + role + pgvector exist (idempotent)."""
    return _req("POST", f"/v1/apps/{app}/provision")


# ── model fabric (routed: local / server / cloud, with failover) ─────────────
#
# `prefer` is an optional per-request routing hint (MODEL_FABRIC_OPS.md): a
# provider name ("server" / "local" / "cloud") to try FIRST for this one call,
# ahead of the configured policy — still bounded by the app's egress ceiling +
# health (an unreachable/forbidden preference is ignored, not an error). Use it
# to push a heavy batch onto the GPU ("server") without changing global policy,
# or to keep a quick interactive call local. Omit it for normal policy routing.

def embed(texts: list[str], app: Optional[str] = None,
          prefer: Optional[str] = None) -> list[list[float]]:
    return _req("POST", "/v1/embed", {"app": app, "texts": texts, "prefer": prefer}, timeout=300.0)["vectors"]


def rerank(query: str, candidates: list[str], app: Optional[str] = None,
           prefer: Optional[str] = None) -> list[float]:
    return _req("POST", "/v1/rerank",
                {"app": app, "query": query, "candidates": candidates, "prefer": prefer}, timeout=300.0)["scores"]


def generate(messages: list[dict], app: Optional[str] = None,
             prefer: Optional[str] = None) -> str:
    return _req("POST", "/v1/generate", {"app": app, "messages": messages, "prefer": prefer}, timeout=300.0)["text"]


def extract(messages: list[dict], schema: Optional[dict] = None,
            app: Optional[str] = None, prefer: Optional[str] = None) -> str:
    """Structured extraction through the orchestrator. Pass a JSON Schema as
    ``schema`` to get a schema-conforming reply — the LOCAL path constrains the
    model via Ollama's ``format`` (server/cloud best-effort). The orchestrator
    owns model choice (Qwen); the app never names a model or calls a provider.
    Returns the reply text (the structured JSON when a schema is given)."""
    body = {"app": app, "messages": messages, "prefer": prefer}
    if schema is not None:
        body["schema"] = schema
    return _req("POST", "/v1/extract", body)["text"]


def health() -> dict:
    return _req("GET", "/v1/health")


# ── shared user memory (suite-wide identity, orchestrator-owned) ──────────────
# READ-only personalization: the Launchpad owns the first-run write of the profile
# ("About you" questionnaire); apps only read it to know who the user is. Never PUT
# the profile from an app (SELRAN_APP_CONTRACT.md §"Shared user memory").

def user_profile() -> dict:
    """The suite-wide user profile (name, about, role, focus, preferences).
    Returns {exists, profile:{...}}; the orchestrator owns the canonical record."""
    return _req("GET", "/v1/user/profile")


if __name__ == "__main__":
    # Smoke probe: `python3 selran_client.py`
    import sys

    try:
        print("health:", json.dumps(health()))
    except SelranError as e:
        print("error:", e, file=sys.stderr)
        sys.exit(1)
