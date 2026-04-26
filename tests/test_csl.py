"""Tests for the CSL manifest + index."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from selran_canvas import csl_index
from selran_canvas.config import CSL_MANIFEST


def test_manifest_loads():
    m = csl_index.load_manifest()
    assert m["version"] >= 2
    assert "styles" in m
    assert len(m["styles"]) >= 100, "manifest must list 100+ journals"


def test_every_manifest_entry_has_required_fields():
    for s in csl_index.list_styles():
        assert "id" in s, f"missing id: {s}"
        assert "csl_id" in s, f"missing csl_id: {s}"
        assert "title" in s, f"missing title: {s}"
        assert "category" in s, f"missing category: {s}"


def test_search_filters():
    lancet_hits = csl_index.list_styles("lancet")
    assert len(lancet_hits) >= 12
    assert all("lancet" in (s["id"] + s["title"]).lower() for s in lancet_hits)

    ortho_hits = csl_index.list_styles("orthop")
    assert len(ortho_hits) >= 4

    empty = csl_index.list_styles("xyzzynonexistent")
    assert empty == []

    all_styles = csl_index.list_styles()
    assert len(all_styles) >= 100


def test_resolve_csl_id():
    # JAMA Cardiology has no own file; resolves to AMA umbrella
    assert csl_index._resolve_csl_id("jama-cardiology") == "american-medical-association"
    # JAMA Dermatology has its own file; resolves to itself
    assert csl_index._resolve_csl_id("jama-dermatology") == "jama-dermatology"
    # Lancet Respiratory Medicine resolves to The Lancet
    assert csl_index._resolve_csl_id("the-lancet-respiratory-medicine") == "the-lancet"
    # Vancouver -> vancouver-nlm (canonical biomedical numeric)
    assert csl_index._resolve_csl_id("vancouver") == "vancouver-nlm"
    # Unknown id passes through as-is (allows lazy-fetch of journals not in our manifest)
    assert csl_index._resolve_csl_id("some-random-journal") == "some-random-journal"


def test_bundled_styles_exist():
    """All bundled styles should be physically present (ship in repo for offline use)."""
    bundled = []
    for s in csl_index.list_styles():
        if csl_index.is_style_local(s["id"]):
            bundled.append(s["id"])
    # We expect a substantial bundled set after running fetch_styles.py
    assert len(bundled) >= 50, f"only {len(bundled)} bundled — run python -m selran_canvas.fetch_styles"


def test_categories_are_complete():
    """Every entry has a category from a known set."""
    valid_categories = {
        "generic", "general-medicine", "jama-family", "lancet-family", "annals",
        "top-science", "cardiology", "oncology", "orthopaedics", "anesthesia",
        "critical-care", "pulmonary", "nephrology", "endocrinology", "gastroenterology",
        "rheumatology", "hematology", "infectious-disease", "radiology", "psychiatry",
        "obgyn", "public-health", "health-services", "pediatrics", "geriatrics",
        "implementation-science", "open-access",
    }
    for s in csl_index.list_styles():
        assert s["category"] in valid_categories, f"unknown category: {s}"


def test_orthopaedics_coverage():
    """The user-requested orthopaedic depth must be present."""
    ortho = csl_index.list_styles("orthop") + csl_index.list_styles("spine") + csl_index.list_styles("arthro") + csl_index.list_styles("bone")
    ids = {s["id"] for s in ortho}
    must_have = {
        "the-journal-of-bone-and-joint-surgery",
        "the-bone-and-joint-journal",
        "clinical-orthopaedics-and-related-research",
        "the-journal-of-arthroplasty",
        "arthroplasty-today",
        "spine",
        "the-spine-journal",
        "european-spine-journal",
        "global-spine-journal",
    }
    missing = must_have - ids
    assert not missing, f"missing orthopaedic journals: {missing}"
