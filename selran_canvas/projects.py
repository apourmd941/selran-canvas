"""Project model — Canvas-side mirror of the on-disk project layout.

Projects are filesystem-rooted at ``~/Documents/Selran Projects/<slug>/``
and are the unifying context every Selran skill writes into. This module
is the read/write API Canvas's webapp uses to expose project listing,
creation, switching, and per-companion artifact browsing to the
browser.

Layout (created by ``create``):

    project.json    — id, name, kind, created_at, schema_version
    memory.md       — Claude's evolving notes (Intent / Decisions /
                      Open Questions / Current Focus / Activity Log)
    manuscript/     — Writer outputs                    (paper / exam / learning)
    data/           — Datacore imports + plans + results (analysis / paper)
    figures/        — Design Director outputs           (paper / design)
    references/     — Librarian curated bibliography    (any project)
    pubmed/         — cached PubMed searches            (paper / learning / exam)
    notes/          — free-form notes                   (learning / exam / general)
    flashcards/     — spaced-repetition cards           (exam / learning)
    canvas/         — rendered manuscript pages, MCQ history

Subdirectories aren't all created up-front — companions create them
on first write, but ``create`` pre-seeds the kind-appropriate ones so
clicking a companion in the sidebar always lands somewhere sensible.

Why this lives in Canvas (and is duplicated in the MCP-server side):
the Canvas webapp serves the browser UI, which needs first-class
project endpoints without round-tripping through the MCP server. The
MCP-server module and this one read/write the same directory, so a
project created from Claude desktop appears in Canvas and vice versa
— filesystem is the single source of truth.

The Launchpad is a third reader of the same directory (Rust-side,
its own implementation). Three independent surfaces, one shared
filesystem.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


PROJECTS_ROOT = Path.home() / "Documents" / "Selran Projects"
CURRENT_PROJECT_FILE = Path.home() / ".selran" / "current_project"
SCHEMA_VERSION = 1

# Project kinds the launchpad UI knows about. The kind is advisory —
# stored verbatim on disk so unknown values pass through, but
# downstream UIs use it to pick default subdirectory layouts and
# colour palettes.
KNOWN_KINDS = {"paper", "design", "analysis", "learning", "exam", "general"}

# Default subdirectory layout per kind. Pre-seeded at create time so
# clicking a companion in the sidebar always lands somewhere
# (vs. a 404 because the directory wasn't there yet).
LAYOUT_BY_KIND: dict[str, list[str]] = {
    "paper":    ["manuscript", "data", "figures", "references", "pubmed", "canvas"],
    "design":   ["artifacts", "figures", "references", "canvas"],
    "analysis": ["data", "results", "figures", "references", "canvas"],
    "learning": ["notes", "references", "pubmed", "flashcards", "canvas"],
    "exam":     ["notes", "references", "flashcards", "practice", "canvas"],
    "general":  ["notes", "files", "canvas"],
}

# Map companion-id (as shown in Canvas's left sidebar) to the
# project subdirectory whose contents Canvas should show when the
# companion tab is clicked. Some companions back onto the same
# directory (PubMed and Librarian both contribute references).
COMPANION_TO_SUBDIR: dict[str, str] = {
    "selran-medical-writer": "manuscript",
    "selran-design":         "figures",
    "selran-data-analysis":  "data",
    "selran-librarian":      "references",
    "bio-research-pubmed":   "pubmed",
}

MEMORY_TEMPLATE = """# {name}

> {kind} project · created {created_at}

## Intent

(What this project is and what success looks like. Filled in by Claude
during the first conversation and stable thereafter unless the user
explicitly redirects.)

## Decisions

(Decisions taken so far — each as a one-liner with date. Append-only.)

## Open Questions

(What's blocking forward motion. Bulleted; Claude updates as
questions get answered.)

## Current Focus

(What the user is working on right now. Replaced (not appended)
whenever focus shifts.)

## Activity Log

(One-line entry per meaningful conversation turn or completed
artifact. Append-only; newest at the bottom.)
"""


# ─── Pure helpers ─────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _slugify(name: str) -> str:
    """Filesystem-safe slug: ASCII, lowercase, dashes."""
    s = re.sub(r"[^a-zA-Z0-9\s-]", "", name).strip().lower()
    s = re.sub(r"[\s-]+", "-", s)
    return s[:60] or f"project-{int(time.time())}"


def _project_path(slug: str) -> Path:
    return PROJECTS_ROOT / slug


def _meta_file(slug: str) -> Path:
    return _project_path(slug) / "project.json"


def _memory_file(slug: str) -> Path:
    return _project_path(slug) / "memory.md"


def _ensure_root() -> None:
    PROJECTS_ROOT.mkdir(parents=True, exist_ok=True)
    CURRENT_PROJECT_FILE.parent.mkdir(parents=True, exist_ok=True)


def _read_meta(slug: str) -> Optional[dict]:
    f = _meta_file(slug)
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _write_meta(slug: str, meta: dict) -> None:
    f = _meta_file(slug)
    f.parent.mkdir(parents=True, exist_ok=True)
    # Atomic-ish: write .tmp then rename, so a partial flush can't
    # corrupt an existing project.json.
    tmp = f.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    tmp.replace(f)


# ─── Public API ───────────────────────────────────────────────────────


def list_all(kind: Optional[str] = None) -> list[dict]:
    """Walk the projects root, return every project's metadata.

    Skips directories without a ``project.json`` so the user can drop
    unrelated folders under ``Documents/Selran Projects/`` without
    breaking discovery. Optional ``kind`` filter trims the list to one
    type (e.g., ``paper`` only).
    """
    _ensure_root()
    out: list[dict] = []
    for child in sorted(PROJECTS_ROOT.iterdir()):
        if not child.is_dir():
            continue
        meta = _read_meta(child.name)
        if meta is None:
            continue
        meta["path"] = str(child)
        if kind and meta.get("kind") != kind:
            continue
        out.append(meta)
    return out


def get(slug: str) -> Optional[dict]:
    """One project's metadata + path. Returns None if not found."""
    meta = _read_meta(slug)
    if meta is None:
        return None
    meta["path"] = str(_project_path(slug))
    return meta


def get_current_id() -> Optional[str]:
    """Read ~/.selran/current_project. Returns None if absent or empty."""
    if not CURRENT_PROJECT_FILE.exists():
        return None
    try:
        v = CURRENT_PROJECT_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return v or None


def set_current(slug: str) -> dict:
    """Set the current project. Validates that the project exists.

    Raises ``ValueError`` if the slug doesn't exist on disk — silently
    setting a bogus pointer would make every downstream read fail.
    """
    meta = _read_meta(slug)
    if meta is None:
        raise ValueError(f"Project '{slug}' not found under {PROJECTS_ROOT}")
    _ensure_root()
    CURRENT_PROJECT_FILE.write_text(slug, encoding="utf-8")
    meta["path"] = str(_project_path(slug))
    return meta


def create(
    name: str,
    kind: str = "general",
    description: Optional[str] = None,
    set_as_current: bool = True,
) -> dict:
    """Create a project. Pre-seeds kind-appropriate subdirectories +
    a memory.md template. Returns the metadata.

    If a slug collision happens (rare — same name twice), suffixes the
    slug with a unix timestamp so the second project gets its own
    directory rather than silently overwriting.
    """
    _ensure_root()
    slug = _slugify(name)
    proj_dir = _project_path(slug)
    if proj_dir.exists():
        slug = f"{slug}-{int(time.time())}"
        proj_dir = _project_path(slug)
    proj_dir.mkdir(parents=True)

    layout = LAYOUT_BY_KIND.get(kind, LAYOUT_BY_KIND["general"])
    for sub in layout:
        (proj_dir / sub).mkdir()

    meta = {
        "schema_version": SCHEMA_VERSION,
        "id": slug,
        "name": name,
        "kind": kind if kind in KNOWN_KINDS else "general",
        "raw_kind": kind,
        "description": description,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "layout": layout,
    }
    _write_meta(slug, meta)

    _memory_file(slug).write_text(
        MEMORY_TEMPLATE.format(
            name=name, kind=meta["kind"], created_at=meta["created_at"]
        ),
        encoding="utf-8",
    )

    if set_as_current:
        CURRENT_PROJECT_FILE.write_text(slug, encoding="utf-8")

    meta["path"] = str(proj_dir)
    return meta


def list_artifacts(slug: str, subdir: str) -> list[dict]:
    """List files (top-level only) under ``<project>/<subdir>/``.

    Returns one record per file with name, path, size, mtime. Skips
    nested directories at the top level — caller can recurse with a
    plain filesystem walk if needed. Missing subdirectory returns an
    empty list rather than raising; that's the case for a brand-new
    project where the companion hasn't written anything yet.
    """
    sub = _project_path(slug) / subdir
    if not sub.exists() or not sub.is_dir():
        return []
    out: list[dict] = []
    for child in sorted(sub.iterdir()):
        if not child.is_file():
            continue
        try:
            stat = child.stat()
        except OSError:
            continue
        out.append({
            "name": child.name,
            "path": str(child),
            "size_bytes": stat.st_size,
            "modified_at": datetime.fromtimestamp(
                stat.st_mtime, tz=timezone.utc
            ).isoformat(),
        })
    return out


def read_artifact(slug: str, subdir: str, filename: str) -> Optional[str]:
    """Read one artifact's text content. Returns None if missing.

    Path-traversal guard: rejects filenames that contain ``/`` or
    ``..`` so the browser can't request ``manuscript/../../etc/passwd``
    via the HTTP endpoint.
    """
    if "/" in filename or ".." in filename:
        return None
    f = _project_path(slug) / subdir / filename
    if not f.is_file():
        return None
    try:
        return f.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def companion_subdir(companion_id: str) -> Optional[str]:
    """Map a companion id (as shown in the sidebar) to its project
    subdirectory. None for unrecognised companions."""
    return COMPANION_TO_SUBDIR.get(companion_id)
