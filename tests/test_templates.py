"""Tests for the templates loader + the scaffold HTTP endpoints.

A fixture manifest is written to a tmp file and pointed at via the
SELRAN_CANVAS_TEMPLATES_MANIFEST env var, so these tests don't depend on
the writer skill being a sibling on disk.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from selran_canvas import templates
from selran_canvas.store import Store
from selran_canvas.webapp import build_webapp

FIXTURE = {
    "templates_manifest_version": 1,
    "templates": [
        {
            "id": "rct",
            "category": "paper",
            "title": "Randomized Controlled Trial",
            "reporting_guideline": "CONSORT 2010",
            "evidence_level": "Level I",
            "word_count_range": "3000-4500",
            "description": "Parallel-group RCT manuscript.",
            "sections": [
                {"id": "abstract", "title": "Abstract", "guidance": "Structured, ~250 words."},
                {"id": "introduction", "title": "Introduction", "guidance": "4 paragraphs."},
                {"id": "methods", "title": "Methods", "guidance": "10 subsections."},
            ],
        },
        {
            "id": "nih-r01",
            "category": "grant",
            "title": "NIH R01",
            "reporting_guideline": None,
            "evidence_level": None,
            "word_count_range": "12 pages",
            "description": "Standard NIH research grant.",
            "sections": [
                {"id": "specific-aims", "title": "Specific Aims", "guidance": "1 page, 5-block."},
                {"id": "significance", "title": "Significance", "guidance": "Why it matters."},
            ],
        },
    ],
}


@pytest.fixture
def manifest_env(tmp_path: Path, monkeypatch):
    p = tmp_path / "templates-manifest.json"
    p.write_text(json.dumps(FIXTURE), encoding="utf-8")
    monkeypatch.setenv("SELRAN_CANVAS_TEMPLATES_MANIFEST", str(p))
    return p


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    return TestClient(build_webapp(Store(tmp_path / "test.db")))


# ---- Loader ----------------------------------------------------------


def test_load_manifest(manifest_env):
    m = templates.load_manifest()
    assert m["templates_manifest_version"] == 1
    assert len(m["templates"]) == 2


def test_list_templates_drops_sections(manifest_env):
    items = templates.list_templates()
    assert len(items) == 2
    rct = next(t for t in items if t["id"] == "rct")
    assert rct["n_sections"] == 3
    assert "sections" not in rct  # the list view omits the heavy field


def test_get_template(manifest_env):
    t = templates.get_template("rct")
    assert t["title"] == "Randomized Controlled Trial"
    assert len(t["sections"]) == 3
    assert templates.get_template("nonexistent") is None


def test_missing_manifest_is_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("SELRAN_CANVAS_TEMPLATES_MANIFEST", str(tmp_path / "does-not-exist.json"))
    assert templates.load_manifest()["templates"] == []
    assert templates.list_templates() == []


# ---- HTTP endpoints --------------------------------------------------


def test_api_templates_lists(client: TestClient, manifest_env):
    r = client.get("/api/templates")
    assert r.status_code == 200
    items = r.json()["templates"]
    ids = {t["id"] for t in items}
    assert ids == {"rct", "nih-r01"}


def test_scaffold_creates_pages_with_guidance(client: TestClient, manifest_env):
    r = client.post("/api/templates/rct/scaffold", json={})
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["created"] == ["rct__abstract", "rct__introduction", "rct__methods"]

    state = client.get("/api/state").json()
    pages = {p["page_id"]: p for p in state["pages"]}
    assert set(pages) == {"rct__abstract", "rct__introduction", "rct__methods"}
    assert pages["rct__abstract"]["content_md"] == ""
    assert pages["rct__abstract"]["guidance"] == "Structured, ~250 words."
    assert pages["rct__methods"]["title"] == "Methods"
    # First section becomes current.
    assert state["current_page"] == "rct__abstract"


def test_scaffold_unknown_template_404(client: TestClient, manifest_env):
    r = client.post("/api/templates/nope/scaffold", json={})
    assert r.status_code == 404


def test_scaffold_is_idempotent(client: TestClient, manifest_env, tmp_path):
    client.post("/api/templates/rct/scaffold", json={})
    # Second scaffold skips all existing section pages.
    r = client.post("/api/templates/rct/scaffold", json={})
    data = r.json()
    assert data["created"] == []
    assert set(data["skipped"]) == {"rct__abstract", "rct__introduction", "rct__methods"}
