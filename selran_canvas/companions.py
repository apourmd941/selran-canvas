"""Detect sibling Selran skill plugins.

Strategy:
    1. Read Claude Code MCP server config (~/.config/claude-code/mcp.json or platform equivalent)
       and check for entries named in our known-companion list.
    2. Scan known sibling directories under the parent of this package's repo for SKILL.md.
    3. Cache results — companions don't change mid-session.

Returned shape: {"selran-medical-writer": True, "selran-design": False, ...}
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

KNOWN_COMPANIONS = {
    "selran-medical-writer": [
        "Selran writing skill/selran-medical-writer/SKILL.md",
        "selran-medical-writer/SKILL.md",
    ],
    "selran-design": [
        "Selron design director skill/SKILL.md",
        "Selran design skill platform/SKILL.md",
        "selran-design/SKILL.md",
    ],
    "selran-data-analysis": [
        "Selran datacore skill platform/SKILL.md",
        "Selran data analysis skill platform/SKILL.md",
        "selran-data-analysis/SKILL.md",
    ],
    # Sibling-detection paths for selran-librarian. Kept in lockstep
    # with COMPANION_TO_SUBDIR in projects.py + canvas.js — when one
    # gains a companion, all three need the matching entry. Without
    # this row the companion sidebar dot always renders as
    # "not installed" even when the librarian skill repo is present.
    "selran-librarian": [
        "Selran librarian skill/SKILL.md",
        "Selran librarian/SKILL.md",
        "selran-librarian/SKILL.md",
    ],
    "bio-research-pubmed": [
        # MCP plugin — detected via config file, not filesystem
    ],
}


def _claude_config_paths() -> list[Path]:
    """Possible locations of the Claude Code MCP config.

    These vary across platforms and Claude Code versions; we scan all that exist.
    """
    home = Path.home()
    return [
        home / ".config" / "claude-code" / "mcp.json",
        home / ".config" / "claude" / "mcp.json",
        home / "Library" / "Application Support" / "claude-code" / "mcp.json",
        home / "AppData" / "Roaming" / "claude-code" / "mcp.json",
        home / ".claude" / "mcp.json",
        home / ".claude" / "settings.json",
    ]


def _read_mcp_config() -> dict:
    for p in _claude_config_paths():
        if p.is_file():
            try:
                return json.loads(p.read_text())
            except (json.JSONDecodeError, OSError):
                continue
    return {}


def _scan_sibling_dirs() -> set[str]:
    """Walk up two directory levels and look for known sibling SKILL.md files."""
    here = Path(__file__).resolve()
    found: set[str] = set()
    for parent in [here.parents[2], here.parents[3] if len(here.parents) > 3 else None]:
        if parent is None or not parent.is_dir():
            continue
        for companion, candidates in KNOWN_COMPANIONS.items():
            for rel in candidates:
                target = parent / rel
                if target.is_file():
                    found.add(companion)
                    break
    return found


@lru_cache(maxsize=1)
def detect_companions() -> dict[str, bool]:
    """Probe for sibling skills. Cached for the process lifetime."""
    found: set[str] = set()

    # Strategy 1: scan filesystem for SKILL.md
    found |= _scan_sibling_dirs()

    # Strategy 2: read Claude Code MCP config
    cfg = _read_mcp_config()
    mcp_servers = cfg.get("mcpServers") or cfg.get("mcp_servers") or {}
    for server_name in mcp_servers:
        for known in KNOWN_COMPANIONS:
            if known in server_name.lower() or server_name.lower() in known:
                found.add(known)

    # Strategy 3: env-var override (testing/manual control)
    if env_override := os.environ.get("SELRAN_CANVAS_FAKE_COMPANIONS"):
        for c in env_override.split(","):
            c = c.strip()
            if c in KNOWN_COMPANIONS:
                found.add(c)

    return {name: (name in found) for name in KNOWN_COMPANIONS}


def reset_companion_cache():
    detect_companions.cache_clear()
