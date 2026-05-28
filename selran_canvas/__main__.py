"""Entry point: run MCP server + HTTP/WebSocket server in one process.

Modes:
    python -m selran_canvas               # MCP-over-stdio + HTTP/WS in background
    python -m selran_canvas --http-only   # HTTP/WS only (development; no MCP)
    python -m selran_canvas --info        # print URL + port + state DB path
    python -m selran_canvas --demo        # load examples/medical_writer_demo.py
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import webbrowser

import uvicorn

from .config import get_config
from .server import build_mcp_server
from .store import Store
from .webapp import build_webapp


def _open_store(cfg):
    """Pick the state backend.

    Selran Launchpad v3's orchestrator injects ``CANVAS_DATABASE_URL`` (the app's
    managed-Postgres role URL) at launch; when it's present we use the Postgres
    backend, otherwise the local SQLite file. SQLite stays the default and its code
    path is unchanged, so this is a reversible cutover — unset the env var and
    Canvas is exactly as it was.
    """
    url = os.environ.get("CANVAS_DATABASE_URL", "").strip()
    if url:
        from .pg_store import PgStore
        return PgStore(url)
    return Store(cfg.db_path)


def _start_http_server(app, host: str, port: int) -> threading.Thread:
    """Run uvicorn in a background daemon thread."""
    config = uvicorn.Config(app, host=host, port=port, log_level="warning", access_log=False)
    server = uvicorn.Server(config)

    def _run():
        # Suppress KeyboardInterrupt traceback if user Ctrl-Cs
        try:
            server.run()
        except (KeyboardInterrupt, SystemExit):
            pass

    th = threading.Thread(target=_run, name="selran-canvas-http", daemon=True)
    th.start()
    # Wait briefly for the server to bind the port
    for _ in range(50):
        if server.started:
            break
        time.sleep(0.05)
    return th


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Selran Canvas — page-aware canvas for Claude.")
    parser.add_argument("--http-only", action="store_true", help="Run HTTP/WS only; skip MCP stdio loop.")
    parser.add_argument("--info", action="store_true", help="Print configuration and exit.")
    parser.add_argument("--demo", action="store_true", help="Seed demo content from examples/medical_writer_demo.py.")
    parser.add_argument("--no-browser", action="store_true", help="Do not auto-open browser.")
    args = parser.parse_args(argv)

    cfg = get_config()
    store = _open_store(cfg)

    if args.info:
        pg_url = os.environ.get("CANVAS_DATABASE_URL", "").strip()
        backend = "postgres (managed)" if pg_url else "sqlite"
        print("Selran Canvas")
        print(f"  URL          : {cfg.url}")
        print(f"  Port         : {cfg.port}  (env SELRAN_CANVAS_PORT={os.environ.get('SELRAN_CANVAS_PORT','')!r})")
        print(f"  Backend      : {backend}")
        print(f"  State DB     : {pg_url or cfg.db_path}")
        print(f"  Auto-open    : {cfg.auto_open_browser and not args.no_browser}")
        return 0

    if args.demo:
        # Lazy import — keeps the demo dependency optional
        from examples.medical_writer_demo import seed_demo  # type: ignore
        seed_demo(store)
        print(f"Demo seeded. Open {cfg.url}")

    app = build_webapp(store)
    _start_http_server(app, cfg.host, cfg.port)
    print(f"Selran Canvas running at {cfg.url}", file=sys.stderr)

    # Auto-open browser on first start (unless disabled or running under MCP stdio,
    # in which case the user typically already has Claude Code open and will navigate manually)
    auto_open = cfg.auto_open_browser and not args.no_browser and (args.http_only or args.demo)
    if auto_open:
        try:
            webbrowser.open(cfg.url)
        except Exception:
            pass

    if args.http_only or args.demo:
        # Keep alive; HTTP thread is daemon
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            print("\nbye", file=sys.stderr)
            return 0

    # MCP mode: build server and run stdio loop in main thread
    mcp = build_mcp_server(store, http_url=cfg.url)
    mcp.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
