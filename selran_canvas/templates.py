"""Locate + load the medical-writer templates manifest.

Templates (RCT, systematic review, NIH R01, …) are authored canonically
in the writer skill's repo at
`selran-medical-writer/templates-manifest.json`. Canvas reads that file at
runtime via the same sibling-scan that companion detection uses — there is
no bundled copy to drift out of sync. If the writer skill isn't a sibling
on disk (or the manifest is absent), the templates dropdown is simply empty
and Canvas keeps working standalone.

The manifest shape (see the writer repo for the authoritative version):

    {
      "templates_manifest_version": 1,
      "templates": [
        {
          "id": "rct",
          "category": "paper",            # paper | grant
          "title": "Randomized Controlled Trial",
          "reporting_guideline": "CONSORT 2010",
          "evidence_level": "Level I",
          "word_count_range": "3000-4500",
          "description": "...",
          "sections": [
            {"id": "abstract", "title": "Abstract", "guidance": "..."},
            ...
          ]
        }
      ]
    }
"""
from __future__ import annotations

import json
import os
from pathlib import Path

# Relative paths to the writer's manifest, checked under each scanned parent.
# Kept in lockstep with companions.KNOWN_COMPANIONS["selran-medical-writer"].
_MANIFEST_RELPATHS = [
    "Selran writing skill/selran-medical-writer/templates-manifest.json",
    "selran-medical-writer/templates-manifest.json",
]

_EMPTY: dict = {"templates_manifest_version": 1, "templates": []}


def _locate_manifest() -> Path | None:
    # Test / manual override first.
    env = os.environ.get("SELRAN_CANVAS_TEMPLATES_MANIFEST")
    if env:
        p = Path(env)
        return p if p.is_file() else None

    here = Path(__file__).resolve()
    parents = [here.parents[2]]
    if len(here.parents) > 3:
        parents.append(here.parents[3])
    for parent in parents:
        if parent is None or not parent.is_dir():
            continue
        for rel in _MANIFEST_RELPATHS:
            candidate = parent / rel
            if candidate.is_file():
                return candidate
    return None


def load_manifest() -> dict:
    """Return the manifest dict, or an empty manifest if not locatable."""
    p = _locate_manifest()
    if p is None:
        return dict(_EMPTY)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return dict(_EMPTY)
    if not isinstance(data, dict) or not isinstance(data.get("templates"), list):
        return dict(_EMPTY)
    return data


def list_templates() -> list[dict]:
    """Lightweight list for the dropdown — drops the per-section guidance
    to keep the payload small (sections are fetched on scaffold)."""
    out = []
    for t in load_manifest().get("templates", []):
        out.append({
            "id": t.get("id"),
            "category": t.get("category"),
            "title": t.get("title"),
            "reporting_guideline": t.get("reporting_guideline"),
            "evidence_level": t.get("evidence_level"),
            "word_count_range": t.get("word_count_range"),
            "description": t.get("description"),
            "n_sections": len(t.get("sections") or []),
        })
    return out


def get_template(template_id: str) -> dict | None:
    for t in load_manifest().get("templates", []):
        if t.get("id") == template_id:
            return t
    return None
