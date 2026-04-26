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

from .config import CSL_DIR, CSL_LOCALE_DIR, CSL_MANIFEST, CSL_STYLES_DIR

CSL_REPO_RAW = "https://raw.githubusercontent.com/citation-style-language/styles/master"
CSL_LOCALES_RAW = "https://raw.githubusercontent.com/citation-style-language/locales/master"

_fetch_lock = threading.Lock()


def load_manifest() -> dict[str, Any]:
    if not CSL_MANIFEST.is_file():
        return {"styles": [], "version": 0}
    return json.loads(CSL_MANIFEST.read_text())


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
        or q in s.get("title", "").lower()
        or q in s.get("category", "").lower()
    ]


def get_style_path(style_id: str) -> Path:
    return CSL_STYLES_DIR / f"{style_id}.csl"


def is_style_local(style_id: str) -> bool:
    return get_style_path(style_id).is_file()


def fetch_style(style_id: str, timeout: float = 10.0) -> Path | None:
    """Lazy-fetch a CSL style from the Zotero repo. Returns None on failure."""
    target = get_style_path(style_id)
    if target.is_file():
        return target

    with _fetch_lock:
        if target.is_file():  # re-check after lock
            return target
        url = f"{CSL_REPO_RAW}/{style_id}.csl"
        try:
            r = httpx.get(url, timeout=timeout, follow_redirects=True)
            r.raise_for_status()
        except (httpx.HTTPError, OSError):
            return None
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(r.text, encoding="utf-8")
        return target


def get_style_xml(style_id: str) -> str | None:
    """Read CSL XML, fetching if needed."""
    p = fetch_style(style_id)
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
