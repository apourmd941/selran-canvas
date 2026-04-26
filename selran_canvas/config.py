"""Configuration: ports, paths, defaults.

Resolution order for the HTTP port:
    1. SELRAN_CANVAS_PORT environment variable (set by your port-registry app)
    2. 15000 (default; verified low-collision range)
    3. If 15000 is busy, try 15001..15004 then fail.
"""
from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PORT = 15000
PORT_RETRY_RANGE = 5

PACKAGE_ROOT = Path(__file__).resolve().parent
CANVAS_DIR = PACKAGE_ROOT / "canvas"
CSL_DIR = PACKAGE_ROOT / "csl"
CSL_STYLES_DIR = CSL_DIR / "styles"
CSL_LOCALE_DIR = CSL_DIR / "locale"
CSL_MANIFEST = CSL_DIR / "manifest.json"

# State DB location: prefer XDG_DATA_HOME, fallback to ~/.selran-canvas
def _state_dir() -> Path:
    if xdg := os.environ.get("XDG_DATA_HOME"):
        d = Path(xdg) / "selran-canvas"
    else:
        d = Path.home() / ".selran-canvas"
    d.mkdir(parents=True, exist_ok=True)
    return d


@dataclass(frozen=True)
class Config:
    port: int
    host: str
    db_path: Path
    auto_open_browser: bool

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"


def _is_port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def _resolve_port(host: str) -> int:
    requested = os.environ.get("SELRAN_CANVAS_PORT")
    if requested:
        try:
            return int(requested)  # honor user's port-registry choice; do not auto-shift
        except ValueError:
            pass

    for candidate in range(DEFAULT_PORT, DEFAULT_PORT + PORT_RETRY_RANGE):
        if _is_port_free(host, candidate):
            return candidate

    raise RuntimeError(
        f"No free port in range {DEFAULT_PORT}..{DEFAULT_PORT + PORT_RETRY_RANGE - 1}. "
        f"Set SELRAN_CANVAS_PORT to a known-free port."
    )


def get_config() -> Config:
    host = os.environ.get("SELRAN_CANVAS_HOST", "127.0.0.1")
    return Config(
        port=_resolve_port(host),
        host=host,
        db_path=_state_dir() / "canvas_state.db",
        auto_open_browser=os.environ.get("SELRAN_CANVAS_AUTO_OPEN", "1") == "1",
    )
