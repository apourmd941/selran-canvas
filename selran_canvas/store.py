"""SQLite-backed state store.

Schema:
    pages         (page_id PK, position, title, content_md, guidance, updated_at)
    mcqs          (mcq_id PK, page_id FK, question, options_json, answer, anchor, asked_at, answered_at)
    comments      (comment_id PK, page_id FK, anchor_text, prefix, suffix, body, status, created_at, resolved_at)
    references    (citation_id PK, csl_json, added_at)
    kv            (key PK, value)         # singleton config (current_page, journal_style, theme, mode)

Concurrency:
    Single-process. SQLite WAL is enabled for reader/writer concurrency between the MCP
    tools (writers) and the FastAPI handlers (readers). All writes go through this module.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS pages (
    page_id     TEXT PRIMARY KEY,
    position    INTEGER NOT NULL,
    title       TEXT NOT NULL,
    content_md  TEXT NOT NULL,
    guidance    TEXT,
    updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS mcqs (
    mcq_id       TEXT PRIMARY KEY,
    page_id      TEXT NOT NULL,
    question     TEXT NOT NULL,
    options_json TEXT NOT NULL,
    answer       TEXT,
    anchor       TEXT,
    asked_at     REAL NOT NULL,
    answered_at  REAL
);
CREATE TABLE IF NOT EXISTS comments (
    comment_id  TEXT PRIMARY KEY,
    page_id     TEXT NOT NULL,
    anchor_text TEXT NOT NULL,
    prefix      TEXT,
    suffix      TEXT,
    body        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open',
    created_at  REAL NOT NULL,
    resolved_at REAL
);
CREATE TABLE IF NOT EXISTS refs (
    citation_id TEXT PRIMARY KEY,
    csl_json    TEXT NOT NULL,
    added_at    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mcqs_page ON mcqs(page_id);
CREATE INDEX IF NOT EXISTS idx_comments_page ON comments(page_id);
"""

DEFAULTS_KV = {
    "current_page": "",
    "journal_style": "vancouver",
    "visual_theme": "draft",
    "viewing_mode": "section",  # section | manuscript | diff
    "companions_json": json.dumps({}),
}


@dataclass
class Page:
    page_id: str
    position: int
    title: str
    content_md: str
    updated_at: float
    guidance: str | None = None  # "what this section should look like" note (from a template scaffold)


@dataclass
class Mcq:
    mcq_id: str
    page_id: str
    question: str
    options: list[str]
    answer: str | None
    anchor: str | None
    asked_at: float
    answered_at: float | None


@dataclass
class Comment:
    comment_id: str
    page_id: str
    anchor_text: str       # exact text the user highlighted in the rendered page
    prefix: str | None     # a few chars before the selection (disambiguation context)
    suffix: str | None     # a few chars after the selection
    body: str              # the user's instruction ("make this concise", "add a limitation…")
    status: str            # "open" | "resolved"
    created_at: float
    resolved_at: float | None


@dataclass
class Reference:
    citation_id: str
    csl: dict
    added_at: float


@dataclass
class State:
    current_page: str
    journal_style: str
    visual_theme: str
    viewing_mode: str
    companions: dict[str, bool]
    pages: list[Page] = field(default_factory=list)
    mcqs: list[Mcq] = field(default_factory=list)
    comments: list[Comment] = field(default_factory=list)
    references: list[Reference] = field(default_factory=list)


class Store:
    """Thread-safe wrapper around a SQLite connection.

    Writes update an in-memory broadcast revision counter so the WebSocket layer
    can detect changes and push diffs to connected browsers.
    """

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._lock = threading.RLock()
        self._revision = 0
        self._listeners: list[threading.Event] = []
        self._init_db()

    def _init_db(self):
        with self._connect() as cx:
            cx.executescript(SCHEMA)
            cx.execute("PRAGMA journal_mode=WAL;")
            # Migration: older DBs predate the pages.guidance column. ADD it
            # if missing so upgrading an existing ~/.selran-canvas/canvas_state.db
            # doesn't error on the new template-scaffold writes.
            cols = {r["name"] for r in cx.execute("PRAGMA table_info(pages)").fetchall()}
            if "guidance" not in cols:
                cx.execute("ALTER TABLE pages ADD COLUMN guidance TEXT")
            for k, v in DEFAULTS_KV.items():
                cx.execute("INSERT OR IGNORE INTO kv(key, value) VALUES(?, ?)", (k, v))
            cx.commit()

    @contextmanager
    def _connect(self):
        cx = sqlite3.connect(self.db_path, isolation_level=None, timeout=5.0)
        cx.row_factory = sqlite3.Row
        try:
            yield cx
        finally:
            cx.close()

    # ---- Notify ---------------------------------------------------------

    def _bump(self):
        with self._lock:
            self._revision += 1
            for ev in self._listeners:
                ev.set()

    def revision(self) -> int:
        with self._lock:
            return self._revision

    def ping(self) -> bool:
        """GL-R1-005: cheap DB liveness check for /api/health (raises on failure)."""
        with self._connect() as cx:
            cx.execute("SELECT 1")
        return True

    def add_listener(self) -> threading.Event:
        ev = threading.Event()
        with self._lock:
            self._listeners.append(ev)
        return ev

    def remove_listener(self, ev: threading.Event):
        with self._lock:
            if ev in self._listeners:
                self._listeners.remove(ev)

    # ---- KV (singletons) ------------------------------------------------

    def get_kv(self, key: str) -> str:
        with self._connect() as cx:
            row = cx.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
            return row["value"] if row else ""

    def set_kv(self, key: str, value: str):
        with self._connect() as cx:
            cx.execute(
                "INSERT INTO kv(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
        self._bump()

    # ---- Pages ----------------------------------------------------------

    def upsert_page(
        self,
        page_id: str,
        title: str,
        content_md: str,
        guidance: str | None = None,
    ) -> Page:
        """Create or update a page.

        guidance semantics: None means "leave the existing guidance note
        untouched" (so Claude calling canvas_set_page on a scaffolded page
        keeps the section note). Pass a string to set it, or "" to clear it.
        """
        now = time.time()
        with self._connect() as cx:
            existing = cx.execute(
                "SELECT position, guidance FROM pages WHERE page_id=?", (page_id,)
            ).fetchone()
            if existing:
                new_guidance = existing["guidance"] if guidance is None else guidance
                cx.execute(
                    "UPDATE pages SET title=?, content_md=?, guidance=?, updated_at=? WHERE page_id=?",
                    (title, content_md, new_guidance, now, page_id),
                )
                pos = existing["position"]
            else:
                next_pos = (cx.execute("SELECT COALESCE(MAX(position), -1) + 1 AS n FROM pages").fetchone())["n"]
                new_guidance = guidance  # None on first insert is fine (no note)
                cx.execute(
                    "INSERT INTO pages(page_id, position, title, content_md, guidance, updated_at) VALUES(?, ?, ?, ?, ?, ?)",
                    (page_id, next_pos, title, content_md, new_guidance, now),
                )
                pos = next_pos
        self._bump()
        return Page(page_id, pos, title, content_md, now, new_guidance)

    def scaffold_pages(self, specs: list[dict]) -> dict:
        """Create section pages from a template's section list, in order.

        Each spec: {page_id, title, guidance}. Pages that already exist are
        left untouched (we never overwrite drafted content); only missing
        page_ids are created (empty content_md + the section's guidance note).

        Returns {created: [...], skipped: [...]}.
        """
        created: list[str] = []
        skipped: list[str] = []
        now = time.time()
        with self._connect() as cx:
            for spec in specs:
                pid = spec.get("page_id")
                if not pid:
                    continue
                exists = cx.execute("SELECT 1 FROM pages WHERE page_id=?", (pid,)).fetchone()
                if exists:
                    skipped.append(pid)
                    continue
                next_pos = (cx.execute("SELECT COALESCE(MAX(position), -1) + 1 AS n FROM pages").fetchone())["n"]
                cx.execute(
                    "INSERT INTO pages(page_id, position, title, content_md, guidance, updated_at) VALUES(?, ?, ?, ?, ?, ?)",
                    (pid, next_pos, spec.get("title", pid), "", spec.get("guidance"), now),
                )
                created.append(pid)
            # Default the current page to the first scaffolded section.
            if created:
                cur = cx.execute("SELECT value FROM kv WHERE key='current_page'").fetchone()
                if not cur or not cur["value"]:
                    cx.execute(
                        "INSERT INTO kv(key, value) VALUES('current_page', ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (created[0],),
                    )
        self._bump()
        return {"created": created, "skipped": skipped}

    def get_pages(self) -> list[Page]:
        with self._connect() as cx:
            rows = cx.execute("SELECT * FROM pages ORDER BY position ASC").fetchall()
            return [Page(r["page_id"], r["position"], r["title"], r["content_md"], r["updated_at"], r["guidance"]) for r in rows]

    def get_page(self, page_id: str) -> Page | None:
        with self._connect() as cx:
            r = cx.execute("SELECT * FROM pages WHERE page_id=?", (page_id,)).fetchone()
            if not r:
                return None
            return Page(r["page_id"], r["position"], r["title"], r["content_md"], r["updated_at"], r["guidance"])

    def delete_page(self, page_id: str):
        # GL-R1-014: atomic — the connection is autocommit (isolation_level=None), so
        # 3 bare DELETEs committed separately could leave orphaned mcqs/comments on a
        # mid-way crash. Wrap in one transaction (children first, then parent).
        with self._connect() as cx:
            cx.execute("BEGIN")
            try:
                cx.execute("DELETE FROM mcqs WHERE page_id=?", (page_id,))
                cx.execute("DELETE FROM comments WHERE page_id=?", (page_id,))
                cx.execute("DELETE FROM pages WHERE page_id=?", (page_id,))
                cx.execute("COMMIT")
            except Exception:
                cx.execute("ROLLBACK")
                raise
        self._bump()

    # ---- MCQs -----------------------------------------------------------

    def upsert_mcq(
        self,
        mcq_id: str,
        page_id: str,
        question: str,
        options: list[str],
        anchor: str | None = None,
    ) -> Mcq:
        now = time.time()
        with self._connect() as cx:
            existing = cx.execute("SELECT * FROM mcqs WHERE mcq_id=?", (mcq_id,)).fetchone()
            if existing:
                cx.execute(
                    "UPDATE mcqs SET page_id=?, question=?, options_json=?, anchor=? WHERE mcq_id=?",
                    (page_id, question, json.dumps(options), anchor, mcq_id),
                )
            else:
                cx.execute(
                    "INSERT INTO mcqs(mcq_id, page_id, question, options_json, anchor, asked_at) VALUES(?, ?, ?, ?, ?, ?)",
                    (mcq_id, page_id, question, json.dumps(options), anchor, now),
                )
        self._bump()
        return Mcq(mcq_id, page_id, question, options, None, anchor, now, None)

    def answer_mcq(self, mcq_id: str, answer: str) -> bool:
        now = time.time()
        with self._connect() as cx:
            cur = cx.execute(
                "UPDATE mcqs SET answer=?, answered_at=? WHERE mcq_id=?",
                (answer, now, mcq_id),
            )
            ok = cur.rowcount > 0
        if ok:
            self._bump()
        return ok

    def get_mcqs(self, page_id: str | None = None) -> list[Mcq]:
        sql = "SELECT * FROM mcqs"
        params: tuple = ()
        if page_id is not None:
            sql += " WHERE page_id=?"
            params = (page_id,)
        sql += " ORDER BY asked_at ASC"
        with self._connect() as cx:
            rows = cx.execute(sql, params).fetchall()
            return [
                Mcq(
                    r["mcq_id"], r["page_id"], r["question"], json.loads(r["options_json"]),
                    r["answer"], r["anchor"], r["asked_at"], r["answered_at"],
                )
                for r in rows
            ]

    # ---- Comments -------------------------------------------------------
    #
    # Comments are the user → Claude channel: the user selects text in the
    # rendered page and attaches an instruction ("make this concise", "add
    # a limitation here"). It mirrors the MCQ flow (which is Claude → user)
    # in reverse. The browser POSTs a comment; Claude reads open comments
    # via canvas_get_state and resolves them via canvas_resolve_comment
    # after addressing the edit.

    def add_comment(
        self,
        page_id: str,
        anchor_text: str,
        body: str,
        prefix: str | None = None,
        suffix: str | None = None,
    ) -> Comment:
        """Create a comment anchored to selected text on a page. The
        comment_id is server-generated (the browser doesn't supply one,
        unlike MCQs whose ids come from Claude)."""
        now = time.time()
        comment_id = "c_" + uuid.uuid4().hex[:12]
        with self._connect() as cx:
            cx.execute(
                "INSERT INTO comments(comment_id, page_id, anchor_text, prefix, suffix, body, status, created_at) "
                "VALUES(?, ?, ?, ?, ?, ?, 'open', ?)",
                (comment_id, page_id, anchor_text, prefix, suffix, body, now),
            )
        self._bump()
        return Comment(comment_id, page_id, anchor_text, prefix, suffix, body, "open", now, None)

    def resolve_comment(self, comment_id: str) -> bool:
        """Mark a comment resolved (Claude calls this after addressing the
        edit; the browser turns the pin green)."""
        now = time.time()
        with self._connect() as cx:
            cur = cx.execute(
                "UPDATE comments SET status='resolved', resolved_at=? WHERE comment_id=?",
                (now, comment_id),
            )
            ok = cur.rowcount > 0
        if ok:
            self._bump()
        return ok

    def delete_comment(self, comment_id: str) -> bool:
        """Remove a comment entirely (user dismisses it without action)."""
        with self._connect() as cx:
            cur = cx.execute("DELETE FROM comments WHERE comment_id=?", (comment_id,))
            ok = cur.rowcount > 0
        if ok:
            self._bump()
        return ok

    def get_comments(self, page_id: str | None = None, status: str | None = None) -> list[Comment]:
        sql = "SELECT * FROM comments"
        clauses: list[str] = []
        params: list[Any] = []
        if page_id is not None:
            clauses.append("page_id=?")
            params.append(page_id)
        if status is not None:
            clauses.append("status=?")
            params.append(status)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at ASC"
        with self._connect() as cx:
            rows = cx.execute(sql, tuple(params)).fetchall()
            return [
                Comment(
                    r["comment_id"], r["page_id"], r["anchor_text"], r["prefix"], r["suffix"],
                    r["body"], r["status"], r["created_at"], r["resolved_at"],
                )
                for r in rows
            ]

    # ---- References -----------------------------------------------------

    def upsert_references(self, refs: Iterable[dict]) -> int:
        n = 0
        now = time.time()
        with self._connect() as cx:
            for csl in refs:
                cid = csl.get("id") or csl.get("citation_id")
                if not cid:
                    continue
                cx.execute(
                    "INSERT INTO refs(citation_id, csl_json, added_at) VALUES(?, ?, ?) "
                    "ON CONFLICT(citation_id) DO UPDATE SET csl_json=excluded.csl_json",
                    (cid, json.dumps(csl), now),
                )
                n += 1
        self._bump()
        return n

    def get_references(self) -> list[Reference]:
        with self._connect() as cx:
            rows = cx.execute("SELECT * FROM refs ORDER BY added_at ASC").fetchall()
            return [Reference(r["citation_id"], json.loads(r["csl_json"]), r["added_at"]) for r in rows]

    def remove_reference(self, citation_id: str) -> bool:
        with self._connect() as cx:
            cur = cx.execute("DELETE FROM refs WHERE citation_id=?", (citation_id,))
            ok = cur.rowcount > 0
        if ok:
            self._bump()
        return ok

    # ---- Aggregate state -----------------------------------------------

    def snapshot(self) -> State:
        return State(
            current_page=self.get_kv("current_page"),
            journal_style=self.get_kv("journal_style"),
            visual_theme=self.get_kv("visual_theme"),
            viewing_mode=self.get_kv("viewing_mode"),
            companions=json.loads(self.get_kv("companions_json") or "{}"),
            pages=self.get_pages(),
            mcqs=self.get_mcqs(),
            comments=self.get_comments(),
            references=self.get_references(),
        )

    def snapshot_dict(self) -> dict[str, Any]:
        s = self.snapshot()
        return {
            "current_page": s.current_page,
            "journal_style": s.journal_style,
            "visual_theme": s.visual_theme,
            "viewing_mode": s.viewing_mode,
            "companions": s.companions,
            "pages": [asdict(p) for p in s.pages],
            "mcqs": [asdict(m) for m in s.mcqs],
            "comments": [asdict(c) for c in s.comments],
            "references": [asdict(r) for r in s.references],
            "revision": self.revision(),
        }
