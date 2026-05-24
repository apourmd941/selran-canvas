"""End-to-end tests for the FastAPI HTTP layer."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from selran_canvas.store import Store
from selran_canvas.webapp import build_webapp


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "test.db")
    s.upsert_page("intro", "Introduction", "Body [@ref1]")
    s.upsert_page("methods", "Methods", "Methods body")
    s.upsert_mcq("m1", "intro", "Q?", ["A", "B", "C"])
    s.upsert_references([
        {"id": "ref1", "type": "article-journal", "author": [{"family": "Smith"}], "title": "T"},
    ])
    s.set_kv("current_page", "intro")
    return s


@pytest.fixture
def client(store: Store) -> TestClient:
    return TestClient(build_webapp(store))


def test_index_serves_html(client: TestClient):
    r = client.get("/")
    assert r.status_code == 200
    assert "Selran Canvas" in r.text


def test_state_endpoint(client: TestClient):
    r = client.get("/api/state")
    assert r.status_code == 200
    data = r.json()
    assert data["current_page"] == "intro"
    assert len(data["pages"]) == 2
    assert len(data["mcqs"]) == 1
    assert len(data["references"]) == 1


def test_health_endpoint(client: TestClient):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_styles_search(client: TestClient):
    r = client.get("/api/csl/styles")
    assert r.status_code == 200
    assert len(r.json()) >= 100

    r2 = client.get("/api/csl/styles", params={"q": "lancet"})
    assert len(r2.json()) >= 12


def test_bundled_style_serves(client: TestClient):
    """The Vancouver-NLM style should be locally bundled and serve quickly."""
    r = client.get("/api/csl/style/vancouver.csl")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/xml"
    assert "<style" in r.text
    assert "</style>" in r.text


def test_answer_mcq_persists(client: TestClient, store: Store):
    r = client.post("/api/mcq/m1/answer", json={"answer": "B"})
    assert r.status_code == 200
    assert r.json() == {"ok": True}

    state = client.get("/api/state").json()
    mcq = next(m for m in state["mcqs"] if m["mcq_id"] == "m1")
    assert mcq["answer"] == "B"


def test_answer_unknown_mcq_404(client: TestClient):
    r = client.post("/api/mcq/nonexistent/answer", json={"answer": "A"})
    assert r.status_code == 404


def test_add_comment_persists(client: TestClient, store: Store):
    r = client.post("/api/comments", json={
        "page_id": "intro",
        "anchor_text": "Body",
        "body": "make this more specific",
        "prefix": "",
        "suffix": " [@ref1]",
    })
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    cid = data["comment_id"]
    assert cid.startswith("c_")

    state = client.get("/api/state").json()
    assert len(state["comments"]) == 1
    c = state["comments"][0]
    assert c["comment_id"] == cid
    assert c["anchor_text"] == "Body"
    assert c["body"] == "make this more specific"
    assert c["status"] == "open"


def test_add_comment_requires_body(client: TestClient):
    r = client.post("/api/comments", json={"page_id": "intro", "anchor_text": "x", "body": ""})
    assert r.status_code == 400


def test_add_comment_unknown_page_404(client: TestClient):
    r = client.post("/api/comments", json={
        "page_id": "nonexistent", "anchor_text": "x", "body": "fix",
    })
    assert r.status_code == 404


def test_resolve_comment_via_http(client: TestClient, store: Store):
    c = store.add_comment("intro", "Body", "tighten")
    r = client.post(f"/api/comments/{c.comment_id}/resolve")
    assert r.status_code == 200
    state = client.get("/api/state").json()
    assert state["comments"][0]["status"] == "resolved"

    # Unknown id → 404
    bad = client.post("/api/comments/c_nope/resolve")
    assert bad.status_code == 404


def test_delete_comment_via_http(client: TestClient, store: Store):
    c = store.add_comment("intro", "Body", "remove me")
    r = client.delete(f"/api/comments/{c.comment_id}")
    assert r.status_code == 200
    state = client.get("/api/state").json()
    assert len(state["comments"]) == 0

    bad = client.delete("/api/comments/c_nope")
    assert bad.status_code == 404


def test_navigate_current_page(client: TestClient, store: Store):
    r = client.post("/api/state/current_page", json={"page_id": "methods"})
    assert r.status_code == 200
    assert store.get_kv("current_page") == "methods"


def test_set_journal_style_via_http(client: TestClient, store: Store):
    r = client.post("/api/state/journal_style", json={"style_id": "the-lancet"})
    assert r.status_code == 200
    assert store.get_kv("journal_style") == "the-lancet"


def test_set_visual_theme_via_http(client: TestClient, store: Store):
    r = client.post("/api/state/visual_theme", json={"theme_id": "print"})
    assert r.status_code == 200
    assert store.get_kv("visual_theme") == "print"


def test_set_viewing_mode_validates(client: TestClient):
    ok = client.post("/api/state/viewing_mode", json={"mode": "manuscript"})
    assert ok.status_code == 200
    bad = client.post("/api/state/viewing_mode", json={"mode": "neon"})
    assert bad.status_code == 400


def test_revision_increments_on_mutation(client: TestClient):
    r0 = client.get("/api/state").json()["revision"]
    client.post("/api/state/visual_theme", json={"theme_id": "compact"})
    r1 = client.get("/api/state").json()["revision"]
    assert r1 > r0
