"""One-shot migration: copy Canvas SQLite state → managed Postgres.

Part of the Selran Launchpad v3 cutover. Reads the local SQLite file (default: the
configured ``~/.selran-canvas/canvas_state.db``) and copies every row of
``pages / mcqs / comments / refs / kv`` into the Postgres database named by
``--pg`` or ``$CANVAS_DATABASE_URL``.

Safe + reversible:
  - the SQLite file is opened **read-only** and left intact (roll back by simply
    unsetting ``CANVAS_DATABASE_URL`` — Canvas falls back to SQLite),
  - **idempotent** — every row is an UPSERT, so re-running converges,
  - original timestamps are **preserved** (we copy the stored values, we do not
    re-stamp ``now()`` like the live store methods do).

Usage:
    CANVAS_DATABASE_URL=postgresql://canvas:…@127.0.0.1:15432/canvas \
        python -m selran_canvas.migrate_to_pg
    python -m selran_canvas.migrate_to_pg --sqlite /path/canvas_state.db --pg postgresql://…
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

from .config import get_config
from .pg_store import PgStore

# table -> (columns, primary key). Column order matches both schemas.
TABLES = {
    "kv": (["key", "value"], "key"),
    "pages": (["page_id", "position", "title", "content_md", "guidance", "updated_at"], "page_id"),
    "mcqs": (
        ["mcq_id", "page_id", "question", "options_json", "answer", "anchor", "asked_at", "answered_at"],
        "mcq_id",
    ),
    "comments": (
        ["comment_id", "page_id", "anchor_text", "prefix", "suffix", "body", "status", "created_at", "resolved_at"],
        "comment_id",
    ),
    "refs": (["citation_id", "csl_json", "added_at"], "citation_id"),
}


def _copy(scx: sqlite3.Connection, pcx, table: str, cols: list[str], pk: str) -> int:
    rows = scx.execute(f"SELECT {', '.join(cols)} FROM {table}").fetchall()
    if not rows:
        return 0
    placeholders = ", ".join(["%s"] * len(cols))
    updates = ", ".join(f"{c}=EXCLUDED.{c}" for c in cols if c != pk)
    sql = (
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT ({pk}) DO UPDATE SET {updates}"
    )
    data = [tuple(r[c] for c in cols) for r in rows]
    with pcx.cursor() as cur:
        cur.executemany(sql, data)
    return len(data)


def migrate(sqlite_path: Path, pg_dsn: str) -> dict[str, int]:
    if not sqlite_path.exists():
        print(f"No SQLite state at {sqlite_path} — nothing to migrate (fresh Postgres).")
        return {}
    pg = PgStore(pg_dsn)  # ensures the Postgres schema exists
    scx = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    scx.row_factory = sqlite3.Row
    counts: dict[str, int] = {}
    try:
        with pg._connect() as pcx:
            for table, (cols, pk) in TABLES.items():
                counts[table] = _copy(scx, pcx, table, cols, pk)
    finally:
        scx.close()
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Migrate Canvas SQLite state into managed Postgres.")
    parser.add_argument("--sqlite", help="Path to the SQLite state file (default: configured path).")
    parser.add_argument("--pg", help="Postgres DSN (default: $CANVAS_DATABASE_URL).")
    args = parser.parse_args(argv)

    sqlite_path = Path(args.sqlite) if args.sqlite else get_config().db_path
    pg_dsn = (args.pg or os.environ.get("CANVAS_DATABASE_URL", "")).strip()
    if not pg_dsn:
        print("error: no Postgres DSN — pass --pg or set CANVAS_DATABASE_URL", file=sys.stderr)
        return 2

    counts = migrate(sqlite_path, pg_dsn)
    total = sum(counts.values())
    for table, n in counts.items():
        print(f"  {table:9} {n}")
    print(f"migrated {total} row(s). SQLite file left intact at {sqlite_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
