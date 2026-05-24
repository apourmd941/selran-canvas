"""selran-mcp Path B plugin for Selran Canvas.

When selran-mcp loads this module via the manifest's plugin.module_path, it
calls register(mcp, manifest). This function:

  1. Starts the canvas HTTP/WebSocket server in a background thread (idempotent
     — if the port is already bound by a standalone `python -m selran_canvas`
     instance, we use that one and just register the tools).
  2. Registers the 10 canvas tools onto selran-mcp's FastMCP instance via the
     existing `selran_canvas.server.build_mcp_server` helper.
  3. Returns the count of tools registered (always 10) — selran-mcp shows this
     in `selran-mcp status`.

Failure modes are explicit:
  - If `selran_canvas` is not pip-installed (or its deps are missing), we
    print a one-line warning to stderr and return 0. selran-mcp's loader
    swallows this and the rest of the system keeps working.
  - If the HTTP port is in use by some unrelated process, we still register
    the MCP tools — the canvas browser UI may not connect, but the SQLite
    store still works and Claude can drive the canvas state via tools alone.
"""
from __future__ import annotations

import socket
import sys
from typing import Any


_state: dict[str, Any] = {"http_started": False, "store": None, "url": None}


def _is_port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.2)
        try:
            s.connect((host, port))
            return True
        except (OSError, socket.timeout):
            return False


def _ensure_canvas_running():
    """Start the canvas HTTP server in a daemon thread if not already up.

    Returns (store, url). The store is the shared SQLite-backed state both the
    HTTP server and the MCP tools mutate.
    """
    if _state["http_started"]:
        return _state["store"], _state["url"]

    # Local imports — keep the plugin import-cheap so a missing dep doesn't
    # break selran-mcp's loader during scanning.
    from selran_canvas.config import get_config
    from selran_canvas.store import Store
    from selran_canvas.webapp import build_webapp
    from selran_canvas.__main__ import _start_http_server  # type: ignore

    cfg = get_config()
    store = Store(cfg.db_path)

    # If something is already listening on the configured port, we assume it's
    # a standalone `python -m selran_canvas` instance sharing the same SQLite
    # store (`~/.selran-canvas/canvas_state.db` by default). In that case we
    # don't try to start a second server — they'd contend for the port — but
    # the MCP tools we register will still drive the same store.
    if _is_port_open(cfg.host, cfg.port):
        _state["http_started"] = True
        _state["store"] = store
        _state["url"] = cfg.url
        print(
            f"[canvas-plugin] HTTP server already running at {cfg.url} — joining shared store",
            file=sys.stderr,
        )
        return store, cfg.url

    app = build_webapp(store)
    try:
        _start_http_server(app, cfg.host, cfg.port)
    except Exception as e:  # noqa: BLE001
        print(f"[canvas-plugin] HTTP server start failed: {e!r} — tools still registered", file=sys.stderr)

    _state["http_started"] = True
    _state["store"] = store
    _state["url"] = cfg.url
    return store, cfg.url


def register(mcp, manifest) -> int:  # noqa: ARG001  (manifest unused — repo paths come from package install)
    """selran-mcp Path B entry point.

    Args:
        mcp: A FastMCP instance owned by selran-mcp.
        manifest: SkillManifest dataclass; manifest.repo_root would point to
            this skill's repo, but we don't need it because selran_canvas is
            pip-installed and locates its own assets via __file__.

    Returns:
        Number of tools registered. Returns 0 on any failure (selran-mcp logs
        but does not crash).
    """
    try:
        store, url = _ensure_canvas_running()
        from selran_canvas.server import build_mcp_server  # local import for failure isolation
        build_mcp_server(store, http_url=url, mcp=mcp)
        return 10
    except ImportError as e:
        print(
            f"[canvas-plugin] selran_canvas not importable ({e!r}). "
            "Run: pip install -e ~/NeutronDev/Selran\\ canvas\\ skill\\ platform",
            file=sys.stderr,
        )
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"[canvas-plugin] register() failed: {e!r}", file=sys.stderr)
        return 0
