"""Tests for the SQLite-backed store."""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from selran_canvas.store import Store


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "test.db")


def test_kv_defaults(store: Store):
    assert store.get_kv("journal_style") == "vancouver"
    assert store.get_kv("visual_theme") == "draft"
    assert store.get_kv("viewing_mode") == "section"
    assert store.get_kv("current_page") == ""


def test_set_kv_bumps_revision(store: Store):
    r0 = store.revision()
    store.set_kv("journal_style", "the-lancet")
    assert store.revision() > r0
    assert store.get_kv("journal_style") == "the-lancet"


def test_upsert_page_position(store: Store):
    p1 = store.upsert_page("intro", "Introduction", "Body 1")
    p2 = store.upsert_page("methods", "Methods", "Body 2")
    p3 = store.upsert_page("results", "Results", "Body 3")
    assert (p1.position, p2.position, p3.position) == (0, 1, 2)
    pages = store.get_pages()
    assert [p.page_id for p in pages] == ["intro", "methods", "results"]


def test_upsert_page_in_place(store: Store):
    p1 = store.upsert_page("intro", "Introduction", "v1")
    r0 = store.revision()
    p2 = store.upsert_page("intro", "Introduction", "v2")
    assert p2.position == p1.position
    assert store.get_page("intro").content_md == "v2"
    assert store.revision() > r0


def test_mcq_workflow(store: Store):
    store.upsert_page("intro", "Introduction", "body")
    store.upsert_mcq("mcq_1", "intro", "Q?", ["A", "B", "C"])
    mcqs = store.get_mcqs()
    assert len(mcqs) == 1
    assert mcqs[0].answer is None

    assert store.answer_mcq("mcq_1", "B")
    mcqs = store.get_mcqs()
    assert mcqs[0].answer == "B"
    assert mcqs[0].answered_at is not None

    # Answering an unknown id returns False
    assert not store.answer_mcq("nonexistent", "A")


def test_references_upsert_and_remove(store: Store):
    refs = [
        {"id": "smith2020", "type": "article-journal", "author": [{"family": "Smith"}], "title": "Test"},
        {"id": "jones2021", "type": "article-journal", "author": [{"family": "Jones"}], "title": "Test 2"},
    ]
    n = store.upsert_references(refs)
    assert n == 2

    got = store.get_references()
    assert len(got) == 2
    ids = {r.citation_id for r in got}
    assert ids == {"smith2020", "jones2021"}

    # Replace one
    store.upsert_references([{"id": "smith2020", "type": "book", "title": "Replaced"}])
    smith = next(r for r in store.get_references() if r.citation_id == "smith2020")
    assert smith.csl["title"] == "Replaced"

    # Remove one
    assert store.remove_reference("jones2021")
    assert len(store.get_references()) == 1
    assert not store.remove_reference("jones2021")  # second time fails


def test_listener_notification(store: Store):
    ev = store.add_listener()
    assert not ev.is_set()

    def write_after_delay():
        time.sleep(0.05)
        store.set_kv("journal_style", "the-lancet")

    threading.Thread(target=write_after_delay, daemon=True).start()
    assert ev.wait(2.0), "listener should fire on store mutation"
    store.remove_listener(ev)


def test_snapshot_dict_shape(store: Store):
    store.upsert_page("p1", "Page 1", "content")
    store.upsert_mcq("m1", "p1", "Q", ["A", "B"])
    store.add_comment("p1", "content", "tighten this")
    store.upsert_references([{"id": "ref1", "title": "T"}])

    snap = store.snapshot_dict()
    assert set(snap.keys()) == {
        "current_page", "journal_style", "visual_theme", "viewing_mode",
        "companions", "pages", "mcqs", "comments", "references", "revision",
    }
    assert len(snap["pages"]) == 1
    assert len(snap["mcqs"]) == 1
    assert len(snap["comments"]) == 1
    assert len(snap["references"]) == 1
    assert snap["revision"] > 0


def test_page_guidance_preserved_on_update(store: Store):
    # Scaffold-style create with a guidance note.
    p = store.upsert_page("rct__methods", "Methods", "", guidance="Describe the design.")
    assert p.guidance == "Describe the design."

    # Claude fills content without passing guidance → note preserved.
    p2 = store.upsert_page("rct__methods", "Methods", "We did a parallel RCT.")
    assert p2.content_md == "We did a parallel RCT."
    assert p2.guidance == "Describe the design."

    # Explicit "" clears it.
    p3 = store.upsert_page("rct__methods", "Methods", "We did a parallel RCT.", guidance="")
    assert p3.guidance == ""


def test_scaffold_pages(store: Store):
    specs = [
        {"page_id": "rct__abstract", "title": "Abstract", "guidance": "Structured, ~250 words."},
        {"page_id": "rct__methods", "title": "Methods", "guidance": "10 subsections."},
    ]
    result = store.scaffold_pages(specs)
    assert result["created"] == ["rct__abstract", "rct__methods"]
    assert result["skipped"] == []

    pages = store.get_pages()
    assert [p.page_id for p in pages] == ["rct__abstract", "rct__methods"]
    assert pages[0].content_md == ""
    assert pages[0].guidance == "Structured, ~250 words."
    # First scaffolded section becomes current.
    assert store.get_kv("current_page") == "rct__abstract"

    # Re-scaffolding skips existing pages (never overwrites drafted content).
    store.upsert_page("rct__abstract", "Abstract", "Real abstract text.")
    result2 = store.scaffold_pages(specs)
    assert result2["created"] == []
    assert set(result2["skipped"]) == {"rct__abstract", "rct__methods"}
    assert store.get_page("rct__abstract").content_md == "Real abstract text."


def test_comment_workflow(store: Store):
    store.upsert_page("intro", "Introduction", "We enrolled stage 3 CKD patients.")

    c = store.add_comment(
        "intro", "stage 3 CKD", "specify the KDIGO criteria",
        prefix="enrolled ", suffix=" patients",
    )
    assert c.status == "open"
    assert c.comment_id.startswith("c_")
    assert c.resolved_at is None

    # Visible as open
    open_comments = store.get_comments(status="open")
    assert len(open_comments) == 1
    assert open_comments[0].anchor_text == "stage 3 CKD"
    assert open_comments[0].body == "specify the KDIGO criteria"
    assert open_comments[0].prefix == "enrolled "

    # Filter by page
    assert len(store.get_comments(page_id="intro")) == 1
    assert len(store.get_comments(page_id="other")) == 0

    # Resolve
    assert store.resolve_comment(c.comment_id)
    resolved = store.get_comments(status="resolved")
    assert len(resolved) == 1
    assert resolved[0].resolved_at is not None
    assert len(store.get_comments(status="open")) == 0

    # Resolving an unknown id returns False
    assert not store.resolve_comment("c_nonexistent")


def test_comment_delete_and_page_cascade(store: Store):
    store.upsert_page("intro", "Introduction", "body text here")
    c = store.add_comment("intro", "body text", "rewrite")
    assert len(store.get_comments()) == 1

    # Explicit delete
    assert store.delete_comment(c.comment_id)
    assert len(store.get_comments()) == 0
    assert not store.delete_comment(c.comment_id)  # second time fails

    # Deleting a page cascades to its comments
    store.add_comment("intro", "body", "x")
    assert len(store.get_comments()) == 1
    store.delete_page("intro")
    assert len(store.get_comments()) == 0
