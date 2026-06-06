"""Shared user profile — suite-wide identity, orchestrator-owned.

A single, suite-wide record of *who the user is* (name, a one-line bio, role,
focus, preferences) is captured once at the Launchpad's first-run "About you"
questionnaire and served by the orchestrator on the loopback API
(SELRAN_APP_CONTRACT.md §"Shared user memory"). Canvas reads it once on startup
so Claude — the agent that drives the canvas via MCP — knows who it's assisting.

Rules (from the contract):
  - READ the profile to personalize; NEVER ask the user their identity (the
    Launchpad's job) and NEVER overwrite it (PUT is the Launchpad's first-run
    write only — this module never writes).
  - All orchestrator calls go through the bundled badge-authenticated client
    (``_selran_client``); we never hand-roll auth or hit the loopback API
    directly.

Degrades gracefully: an unreachable orchestrator, a fresh install with no
profile yet (``exists: false``), or a malformed response all resolve to "no
identity" — Canvas runs anonymously, never crashes.
"""
from __future__ import annotations

import logging

_LOG = logging.getLogger("selran_canvas")

# Cached for the process lifetime: the `profile` dict when one exists, else None.
# Loaded once on startup via load_user_profile(); read by the MCP state snapshot
# and the /api/user route.
_USER_PROFILE: dict | None = None
_LOADED = False


def load_user_profile() -> dict | None:
    """Fetch the suite-wide user profile via the orchestrator client and cache it.

    Called once on backend startup. Returns the ``profile`` dict when ``exists``
    is true, else None. Never raises — an unreachable / unprovisioned
    orchestrator just means no personalization (Canvas still works anonymously).
    Logs a single line identifying the user when present.
    """
    global _USER_PROFILE, _LOADED
    _LOADED = True
    profile: dict | None = None
    try:
        from . import _selran_client as selran

        resp = selran.user_profile()
        if isinstance(resp, dict) and resp.get("exists"):
            p = resp.get("profile")
            profile = p if isinstance(p, dict) else None
    except Exception as exc:  # noqa: BLE001 — orchestrator down / not provisioned
        _LOG.info("canvas: shared user profile unavailable (%s); running anonymous", exc)
        _USER_PROFILE = None
        return None

    _USER_PROFILE = profile
    if profile:
        name = str(profile.get("name") or "").strip() or "an unnamed user"
        _LOG.info("canvas: running for %s", name)
    else:
        _LOG.info("canvas: no shared user profile set; running anonymous")
    return _USER_PROFILE


def get_cached_profile() -> dict | None:
    """The cached profile dict (or None). Loads lazily on first call if startup
    never ran load_user_profile() — keeps every read path personalized."""
    if not _LOADED:
        return load_user_profile()
    return _USER_PROFILE


def identity_line(profile: dict | None = None) -> str:
    """A single concise identity line for Claude's canvas context, built only
    from non-empty fields. Returns "" when there's nothing useful to say.

    Shape: "Assisting {name} ({role}); cares about {focus}; preferences: {prefs}."
    """
    if profile is None:
        profile = get_cached_profile()
    if not profile:
        return ""
    name = str(profile.get("name") or "").strip()
    role = str(profile.get("role") or "").strip()
    focus = str(profile.get("focus") or "").strip()
    preferences = str(profile.get("preferences") or "").strip()

    # Only worth emitting if we learned at least one real fact about the user.
    if not (name or role or focus or preferences):
        return ""

    who = f"Assisting {name}" if name else "Assisting the user"
    if role:
        who += f" ({role})"
    parts = [who]
    if focus:
        parts.append(f"cares about {focus}")
    if preferences:
        parts.append(f"preferences: {preferences}")
    return "; ".join(parts) + "."
