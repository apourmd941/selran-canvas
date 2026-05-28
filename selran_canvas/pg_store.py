"""Postgres-backed state store — the v3 (Selran Launchpad) backend.

A drop-in alternative to the SQLite :class:`~selran_canvas.store.Store`, with the
**identical public interface** (same methods, same dataclass returns, same
revision/listener semantics). The orchestrator provisions a managed `canvas`
Postgres database and hands this process its role URL via ``CANVAS_DATABASE_URL``;
:func:`selran_canvas.__main__` selects this backend when that env var is set,
otherwise it keeps using SQLite. Nothing in ``store.py`` changes — this is purely
additive, so the SQLite path stays exactly as it was (reversible cutover).

Dialect notes vs. SQLite:
  - ``%s`` placeholders (psycopg) instead of ``?``.
  - ``ON CONFLICT … DO UPDATE/NOTHING`` (the SQLite UPSERTs were already written
    this way; ``INSERT OR IGNORE`` becomes ``ON CONFLICT DO NOTHING``).
  - timestamps are ``DOUBLE PRECISION`` (SQLite ``REAL`` is only float4 — too
    coarse for unix epoch seconds).
  - rows come back as dicts (``psycopg.rows.dict_row``), mirroring ``sqlite3.Row``.

psycopg is imported lazily so the SQLite-only path never needs it installed.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from typing import Any, Iterable

# Reuse the data model + KV defaults verbatim — one source of truth.
from .store import Comment, DEFAULTS_KV, Mcq, Page, Reference, State

PG_SCHEMA = """
CREATE TABLE IF NOT EXISTS pages (
    page_id     TEXT PRIMARY KEY,
    position    INTEGER NOT NULL,
    title       TEXT NOT NULL,
    content_md  TEXT NOT NULL,
    guidance    TEXT,
    updated_at  DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS mcqs (
    mcq_id       TEXT PRIMARY KEY,
    page_id      TEXT NOT NULL,
    question     TEXT NOT NULL,
    options_json TEXT NOT NULL,
    answer       TEXT,
    anchor       TEXT,
    asked_at     DOUBLE PRECISION NOT NULL,
    answered_at  DOUBLE PRECISION
);
CREATE TABLE IF NOT EXISTS comments (
    comment_id  TEXT PRIMARY KEY,
    page_id     TEXT NOT NULL,
    anchor_text TEXT NOT NULL,
    prefix      TEXT,
    suffix      TEXT,
    body        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open',
    created_at  DOUBLE PRECISION NOT NULL,
    resolved_at DOUBLE PRECISION
);
CREATE TABLE IF NOT EXISTS refs (
    citation_id TEXT PRIMARY KEY,
    csl_json    TEXT NOT NULL,
    added_at    DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mcqs_page ON mcqs(page_id);
CREATE INDEX IF NOT EXISTS idx_comments_page ON comments(page_id);
"""


class PgStore:
    """Postgres-backed, interface-compatible twin of ``store.Store``.

    Like ``Store``, opens a short-lived connection per operation (canvas is a
    single, low-traffic interactive process; per-op connect to a loopback
    Postgres is plenty and keeps thread-safety trivial). A connection pool is a
    later perf tweak, not a correctness need.
    """

    def __init__(self, dsn: str):
        self.dsn = dsn
        self._lock = threading.RLock()
        self._revision = 0
        self._listeners: list[threading.Event] = []
        self._init_db()

    @contextmanager
    def _connect(self):
        import psycopg  # lazy: SQLite path never imports psycopg
        from psycopg.rows import dict_row

        cx = psycopg.connect(self.dsn, autocommit=True, row_factory=dict_row)
        try:
            yield cx
        finally:
            cx.close()

    def _init_db(self):
        with self._connect() as cx:
            for stmt in (s.strip() for s in PG_SCHEMA.split(";")):
                if stmt:
                    cx.execute(stmt)
            for k, v in DEFAULTS_KV.items():
                cx.execute(
                    "INSERT INTO kv(key, value) VALUES(%s, %s) ON CONFLICT(key) DO NOTHING",
                    (k, v),
                )

    # ---- Notify (identical semantics to Store) --------------------------

    def _bump(self):
        with self._lock:
            self._revision += 1
            for ev in self._listeners:
                ev.set()

    def revision(self) -> int:
        with self._lock:
            return self._revision

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
            row = cx.execute("SELECT value FROM kv WHERE key=%s", (key,)).fetchone()
            return row["value"] if row else ""

    def set_kv(self, key: str, value: str):
        with self._connect() as cx:
            cx.execute(
                "INSERT INTO kv(key, value) VALUES(%s, %s) "
                "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",
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
        now = time.time()
        with self._connect() as cx:
            existing = cx.execute(
                "SELECT position, guidance FROM pages WHERE page_id=%s", (page_id,)
            ).fetchone()
            if existing:
                new_guidance = existing["guidance"] if guidance is None else guidance
                cx.execute(
                    "UPDATE pages SET title=%s, content_md=%s, guidance=%s, updated_at=%s WHERE page_id=%s",
                    (title, content_md, new_guidance, now, page_id),
                )
                pos = existing["position"]
            else:
                pos = cx.execute(
                    "SELECT COALESCE(MAX(position), -1) + 1 AS n FROM pages"
                ).fetchone()["n"]
                new_guidance = guidance
                cx.execute(
                    "INSERT INTO pages(page_id, position, title, content_md, guidance, updated_at) "
                    "VALUES(%s, %s, %s, %s, %s, %s)",
                    (page_id, pos, title, content_md, new_guidance, now),
                )
        self._bump()
        return Page(page_id, pos, title, content_md, now, new_guidance)

    def scaffold_pages(self, specs: list[dict]) -> dict:
        created: list[str] = []
        skipped: list[str] = []
        now = time.time()
        with self._connect() as cx:
            for spec in specs:
                pid = spec.get("page_id")
                if not pid:
                    continue
                exists = cx.execute("SELECT 1 FROM pages WHERE page_id=%s", (pid,)).fetchone()
                if exists:
                    skipped.append(pid)
                    continue
                next_pos = cx.execute(
                    "SELECT COALESCE(MAX(position), -1) + 1 AS n FROM pages"
                ).fetchone()["n"]
                cx.execute(
                    "INSERT INTO pages(page_id, position, title, content_md, guidance, updated_at) "
                    "VALUES(%s, %s, %s, %s, %s, %s)",
                    (pid, next_pos, spec.get("title", pid), "", spec.get("guidance"), now),
                )
                created.append(pid)
            if created:
                cur = cx.execute("SELECT value FROM kv WHERE key='current_page'").fetchone()
                if not cur or not cur["value"]:
                    cx.execute(
                        "INSERT INTO kv(key, value) VALUES('current_page', %s) "
                        "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",
                        (created[0],),
                    )
        self._bump()
        return {"created": created, "skipped": skipped}

    def get_pages(self) -> list[Page]:
        with self._connect() as cx:
            rows = cx.execute("SELECT * FROM pages ORDER BY position ASC").fetchall()
            return [
                Page(r["page_id"], r["position"], r["title"], r["content_md"], r["updated_at"], r["guidance"])
                for r in rows
            ]

    def get_page(self, page_id: str) -> Page | None:
        with self._connect() as cx:
            r = cx.execute("SELECT * FROM pages WHERE page_id=%s", (page_id,)).fetchone()
            if not r:
                return None
            return Page(r["page_id"], r["position"], r["title"], r["content_md"], r["updated_at"], r["guidance"])

    def delete_page(self, page_id: str):
        with self._connect() as cx:
            cx.execute("DELETE FROM pages WHERE page_id=%s", (page_id,))
            cx.execute("DELETE FROM mcqs WHERE page_id=%s", (page_id,))
            cx.execute("DELETE FROM comments WHERE page_id=%s", (page_id,))
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
            existing = cx.execute("SELECT 1 FROM mcqs WHERE mcq_id=%s", (mcq_id,)).fetchone()
            if existing:
                cx.execute(
                    "UPDATE mcqs SET page_id=%s, question=%s, options_json=%s, anchor=%s WHERE mcq_id=%s",
                    (page_id, question, json.dumps(options), anchor, mcq_id),
                )
            else:
                cx.execute(
                    "INSERT INTO mcqs(mcq_id, page_id, question, options_json, anchor, asked_at) "
                    "VALUES(%s, %s, %s, %s, %s, %s)",
                    (mcq_id, page_id, question, json.dumps(options), anchor, now),
                )
        self._bump()
        return Mcq(mcq_id, page_id, question, options, None, anchor, now, None)

    def answer_mcq(self, mcq_id: str, answer: str) -> bool:
        now = time.time()
        with self._connect() as cx:
            cur = cx.execute(
                "UPDATE mcqs SET answer=%s, answered_at=%s WHERE mcq_id=%s",
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
            sql += " WHERE page_id=%s"
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

    def add_comment(
        self,
        page_id: str,
        anchor_text: str,
        body: str,
        prefix: str | None = None,
        suffix: str | None = None,
    ) -> Comment:
        now = time.time()
        comment_id = "c_" + uuid.uuid4().hex[:12]
        with self._connect() as cx:
            cx.execute(
                "INSERT INTO comments(comment_id, page_id, anchor_text, prefix, suffix, body, status, created_at) "
                "VALUES(%s, %s, %s, %s, %s, %s, 'open', %s)",
                (comment_id, page_id, anchor_text, prefix, suffix, body, now),
            )
        self._bump()
        return Comment(comment_id, page_id, anchor_text, prefix, suffix, body, "open", now, None)

    def resolve_comment(self, comment_id: str) -> bool:
        now = time.time()
        with self._connect() as cx:
            cur = cx.execute(
                "UPDATE comments SET status='resolved', resolved_at=%s WHERE comment_id=%s",
                (now, comment_id),
            )
            ok = cur.rowcount > 0
        if ok:
            self._bump()
        return ok

    def delete_comment(self, comment_id: str) -> bool:
        with self._connect() as cx:
            cur = cx.execute("DELETE FROM comments WHERE comment_id=%s", (comment_id,))
            ok = cur.rowcount > 0
        if ok:
            self._bump()
        return ok

    def get_comments(self, page_id: str | None = None, status: str | None = None) -> list[Comment]:
        sql = "SELECT * FROM comments"
        clauses: list[str] = []
        params: list[Any] = []
        if page_id is not None:
            clauses.append("page_id=%s")
            params.append(page_id)
        if status is not None:
            clauses.append("status=%s")
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
                    "INSERT INTO refs(citation_id, csl_json, added_at) VALUES(%s, %s, %s) "
                    "ON CONFLICT(citation_id) DO UPDATE SET csl_json=EXCLUDED.csl_json",
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
            cur = cx.execute("DELETE FROM refs WHERE citation_id=%s", (citation_id,))
            ok = cur.rowcount > 0
        if ok:
            self._bump()
        return ok

    # ---- Aggregate state (identical to Store) ---------------------------

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
