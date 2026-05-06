"""FastAPI app + WebSocket broadcaster.

Endpoints:
    GET  /                       → canvas/index.html
    GET  /static/{path}          → canvas/* assets (JS/CSS)
    GET  /api/state              → full state snapshot (initial load)
    GET  /api/csl/locale.xml     → bundled or lazy-fetched CSL locale
    GET  /api/csl/style/{id}.csl → bundled or lazy-fetched CSL style
    GET  /api/csl/styles[?q=]    → search the manifest
    POST /api/mcq/{id}/answer    → user submits an MCQ answer (browser only)
    POST /api/state/current_page → user navigates to a page (browser only)
    POST /api/state/journal_style → user picks a journal (browser only)
    POST /api/state/visual_theme  → user picks a theme (browser only)
    POST /api/state/viewing_mode  → user toggles section/manuscript/diff (browser only)

    GET  /api/projects                            → list all projects
    POST /api/projects                            → create a project
    GET  /api/projects/current                    → which is "open"
    POST /api/projects/current                    → set the open project
    GET  /api/projects/{id}                       → metadata for one project
    GET  /api/projects/{id}/artifacts/{subdir}    → list files in subdir
    GET  /api/projects/{id}/artifacts/{subdir}/{file} → read one file

    WS   /ws                     → push live state updates on each store revision
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import projects
from .config import CANVAS_DIR
from .csl_index import get_locale, get_style_xml, list_styles
from .store import Store


def build_webapp(store: Store) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.store = store
        yield

    app = FastAPI(title="Selran Canvas", version="0.1.0", lifespan=lifespan)

    # Static (canvas/index.html, canvas.js, canvas.css)
    if CANVAS_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=CANVAS_DIR), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def index():
        idx = CANVAS_DIR / "index.html"
        if not idx.is_file():
            return HTMLResponse(
                "<h1>Selran Canvas</h1><p>Canvas frontend not found. "
                "Expected: <code>{0}</code></p>".format(idx),
                status_code=500,
            )
        return FileResponse(idx)

    @app.get("/api/state")
    async def api_state():
        return JSONResponse(store.snapshot_dict())

    @app.get("/api/csl/locale.xml")
    async def api_locale(lang: str = "en-US"):
        xml = get_locale(lang)
        if xml is None:
            raise HTTPException(status_code=404, detail=f"locale {lang} unavailable (network?)")
        return Response(content=xml, media_type="application/xml")

    @app.get("/api/csl/style/{style_id}.csl")
    async def api_style(style_id: str):
        xml = get_style_xml(style_id)
        if xml is None:
            raise HTTPException(
                status_code=404,
                detail=f"style {style_id} unavailable. Run: python -m selran_canvas.fetch_styles",
            )
        return Response(content=xml, media_type="application/xml")

    @app.get("/api/csl/styles")
    async def api_styles(q: str | None = None):
        return JSONResponse(list_styles(q))

    # ---- User-side mutations from the browser ----------------------------

    @app.post("/api/mcq/{mcq_id}/answer")
    async def answer_mcq(mcq_id: str, payload: dict):
        ans = payload.get("answer")
        if not isinstance(ans, str):
            raise HTTPException(400, "missing answer")
        if not store.answer_mcq(mcq_id, ans):
            raise HTTPException(404, f"unknown mcq {mcq_id}")
        return {"ok": True}

    @app.post("/api/state/current_page")
    async def set_current_page(payload: dict):
        store.set_kv("current_page", payload.get("page_id", ""))
        return {"ok": True}

    @app.post("/api/state/journal_style")
    async def set_journal_style(payload: dict):
        store.set_kv("journal_style", payload.get("style_id", "vancouver"))
        return {"ok": True}

    @app.post("/api/state/visual_theme")
    async def set_visual_theme(payload: dict):
        store.set_kv("visual_theme", payload.get("theme_id", "draft"))
        return {"ok": True}

    @app.post("/api/state/viewing_mode")
    async def set_viewing_mode(payload: dict):
        mode = payload.get("mode", "section")
        if mode not in ("section", "manuscript", "diff"):
            raise HTTPException(400, "mode must be section|manuscript|diff")
        store.set_kv("viewing_mode", mode)
        return {"ok": True}

    @app.get("/api/health")
    async def health():
        return {"ok": True, "revision": store.revision()}

    # ---- Project endpoints ----------------------------------------------
    #
    # Projects are filesystem-rooted at ~/Documents/Selran Projects/.
    # Canvas, the Launchpad, and Claude (via the MCP server) all read
    # the same directory; whichever surface most recently called
    # POST /api/projects/current wins. The "current project" pointer is
    # ~/.selran/current_project (a single slug).

    @app.get("/api/projects")
    async def api_projects_list(kind: str | None = None):
        """List every project on disk, with the current-project id."""
        return JSONResponse({
            "current_project_id": projects.get_current_id(),
            "projects": projects.list_all(kind=kind),
        })

    @app.post("/api/projects")
    async def api_projects_create(payload: dict):
        """Create a new project. Required: name. Optional: kind
        (paper | design | analysis | learning | exam | general),
        description, set_as_current (default true)."""
        name = (payload.get("name") or "").strip()
        if not name:
            raise HTTPException(400, "name is required")
        kind = payload.get("kind") or "general"
        description = payload.get("description")
        set_as_current = bool(payload.get("set_as_current", True))
        meta = projects.create(
            name=name,
            kind=kind,
            description=description,
            set_as_current=set_as_current,
        )
        return JSONResponse(meta, status_code=201)

    @app.get("/api/projects/current")
    async def api_projects_get_current():
        cid = projects.get_current_id()
        if cid is None:
            return JSONResponse({"current_project_id": None, "project": None})
        meta = projects.get(cid)
        if meta is None:
            # Stale pointer — clear it and report nothing.
            return JSONResponse({"current_project_id": None, "project": None})
        return JSONResponse({"current_project_id": cid, "project": meta})

    @app.post("/api/projects/current")
    async def api_projects_set_current(payload: dict):
        slug = (payload.get("id") or "").strip()
        if not slug:
            raise HTTPException(400, "id is required")
        try:
            meta = projects.set_current(slug)
        except ValueError as e:
            raise HTTPException(404, str(e))
        return JSONResponse({"current_project_id": slug, "project": meta})

    @app.get("/api/projects/{slug}")
    async def api_projects_get(slug: str):
        meta = projects.get(slug)
        if meta is None:
            raise HTTPException(404, f"Project '{slug}' not found")
        return JSONResponse(meta)

    @app.get("/api/projects/{slug}/artifacts/{subdir}")
    async def api_projects_artifacts(slug: str, subdir: str):
        """List files under <project>/<subdir>/. Empty list (not 404)
        when the subdir doesn't exist yet — that's the case for a new
        project where the companion hasn't written anything."""
        if projects.get(slug) is None:
            raise HTTPException(404, f"Project '{slug}' not found")
        # Path-traversal guard: caller can ask for "manuscript" but
        # not "../../etc". Reject anything with a slash or dot-dot.
        if "/" in subdir or ".." in subdir:
            raise HTTPException(400, "invalid subdir")
        return JSONResponse({
            "project_id": slug,
            "subdir": subdir,
            "artifacts": projects.list_artifacts(slug, subdir),
        })

    @app.get("/api/projects/{slug}/artifacts/{subdir}/{filename}")
    async def api_projects_artifact_read(slug: str, subdir: str, filename: str):
        """Read one artifact's text content. Used by the per-companion
        view to render manuscript pages, reference lists, etc."""
        content = projects.read_artifact(slug, subdir, filename)
        if content is None:
            raise HTTPException(404, "artifact not found")
        return JSONResponse({
            "project_id": slug,
            "subdir": subdir,
            "filename": filename,
            "content": content,
        })

    # ---- WebSocket broadcaster ------------------------------------------

    @app.websocket("/ws")
    async def ws(socket: WebSocket):
        await socket.accept()
        # Send initial snapshot
        try:
            await socket.send_json({"type": "snapshot", "data": store.snapshot_dict()})
        except Exception:
            return

        listener = store.add_listener()
        loop = asyncio.get_running_loop()
        last_rev = store.revision()
        try:
            while True:
                # Wait for a state change; poll for socket aliveness via timeout
                changed = await loop.run_in_executor(None, listener.wait, 5.0)
                if changed:
                    listener.clear()
                    rev = store.revision()
                    if rev != last_rev:
                        last_rev = rev
                        await socket.send_json({"type": "snapshot", "data": store.snapshot_dict()})
                else:
                    # heartbeat — also detects dead sockets
                    await socket.send_json({"type": "ping", "revision": store.revision()})
        except (WebSocketDisconnect, ConnectionError, RuntimeError):
            pass
        finally:
            store.remove_listener(listener)

    return app
