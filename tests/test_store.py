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
    store.upsert_references([{"id": "ref1", "title": "T"}])

    snap = store.snapshot_dict()
    assert set(snap.keys()) == {
        "current_page", "journal_style", "visual_theme", "viewing_mode",
        "companions", "pages", "mcqs", "references", "revision",
    }
    assert len(snap["pages"]) == 1
    assert len(snap["mcqs"]) == 1
    assert len(snap["references"]) == 1
    assert snap["revision"] > 0
