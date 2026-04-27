"""CSL style management.

The manifest at csl/manifest.json maps human-readable journal IDs to:
    - the Zotero CSL repo file ID (used to fetch from GitHub)
    - the display title and category
    - whether the file is bundled locally or must be lazy-fetched

Bundled subset (committed to repo):
    vancouver, apa, the-new-england-journal-of-medicine, the-lancet,
    american-medical-association, bmj, the-journal-of-bone-and-joint-surgery

All others are lazy-fetched from
    https://raw.githubusercontent.com/citation-style-language/styles/master/{id}.csl
on first request and cached under csl/styles/.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import httpx

from .config import CSL_LOCALE_DIR, CSL_MANIFEST, CSL_STYLES_DIR

CSL_REPO_RAW = "https://raw.githubusercontent.com/citation-style-language/styles/master"
CSL_LOCALES_RAW = "https://raw.githubusercontent.com/citation-style-language/locales/master"

_fetch_lock = threading.Lock()


def load_manifest() -> dict[str, Any]:
    if not CSL_MANIFEST.is_file():
        return {"styles": [], "version": 0}
    return json.loads(CSL_MANIFEST.read_text())


def _resolve_csl_id(manifest_id: str) -> str:
    """Map a user-facing manifest id (e.g. 'jama-cardiology') to the actual Zotero CSL repo id
    that should be fetched (e.g. 'american-medical-association'). When no manifest entry matches,
    the input is treated as already a real Zotero id (allows lazy-fetch of journals not in our manifest).
    """
    for s in load_manifest().get("styles", []):
        if s.get("id") == manifest_id:
            return s.get("csl_id") or manifest_id
    return manifest_id


def list_styles(query: str | None = None) -> list[dict]:
    """Search the manifest. Empty query returns all styles."""
    manifest = load_manifest()
    styles = manifest.get("styles", [])
    if not query:
        return styles
    q = query.lower().strip()
    return [
        s for s in styles
        if q in s.get("id", "").lower()
        or q in s.get("csl_id", "").lower()
        or q in s.get("title", "").lower()
        or q in s.get("category", "").lower()
    ]


def get_style_path(csl_id: str) -> Path:
    """Path to a style on disk. Argument is a Zotero CSL id, not a manifest user-facing id."""
    return CSL_STYLES_DIR / f"{csl_id}.csl"


def is_style_local(manifest_id_or_csl_id: str) -> bool:
    csl_id = _resolve_csl_id(manifest_id_or_csl_id)
    return get_style_path(csl_id).is_file()


def fetch_style(manifest_id_or_csl_id: str, timeout: float = 10.0) -> Path | None:
    """Lazy-fetch a CSL style from the Zotero repo (master/ first, then dependent/).

    Accepts either a manifest user-facing id (e.g. 'jama-cardiology') — which is resolved
    to its underlying Zotero csl_id via the manifest — or a raw Zotero csl_id.
    Returns the local file path on success, None on failure.
    """
    csl_id = _resolve_csl_id(manifest_id_or_csl_id)
    target = get_style_path(csl_id)
    if target.is_file():
        return target

    with _fetch_lock:
        if target.is_file():  # re-check after lock
            return target
        # Try master/ first, then dependent/
        urls = [
            f"{CSL_REPO_RAW}/{csl_id}.csl",
            f"{CSL_REPO_RAW}/dependent/{csl_id}.csl",
        ]
        for url in urls:
            try:
                r = httpx.get(url, timeout=timeout, follow_redirects=True)
                if r.status_code == 200:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(r.text, encoding="utf-8")
                    return target
            except (httpx.HTTPError, OSError):
                continue
        return None


def get_style_xml(manifest_id_or_csl_id: str) -> str | None:
    """Read CSL XML, fetching if needed. Resolves manifest ids to Zotero csl_ids."""
    p = fetch_style(manifest_id_or_csl_id)
    return p.read_text(encoding="utf-8") if p else None


def get_locale(lang: str = "en-US") -> str | None:
    """Fetch CSL locale XML. Bundled English by default; others lazy-fetched."""
    target = CSL_LOCALE_DIR / f"locales-{lang}.xml"
    if target.is_file():
        return target.read_text(encoding="utf-8")

    with _fetch_lock:
        if target.is_file():
            return target.read_text(encoding="utf-8")
        url = f"{CSL_LOCALES_RAW}/locales-{lang}.xml"
        try:
            r = httpx.get(url, timeout=10.0, follow_redirects=True)
            r.raise_for_status()
        except (httpx.HTTPError, OSError):
            return None
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(r.text, encoding="utf-8")
        return r.text
