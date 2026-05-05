"""Tests for the 7 MCP tools — exercise their behaviour through the FastMCP API."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from selran_canvas.server import build_mcp_server
from selran_canvas.store import Store


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "test.db")


@pytest.fixture
def mcp(store: Store):
    return build_mcp_server(store, http_url="http://127.0.0.1:15000")


def call_tool(mcp, name: str, args: dict) -> dict:
    """Run an MCP tool synchronously by invoking it through FastMCP's dispatch.

    FastMCP's call_tool API has shifted across versions:
    - Some return (list[Content], dict)
    - Some return list[Content] only (with text containing JSON-encoded result)
    - Some return the raw dict directly
    Handle all three.
    """
    result = asyncio.run(mcp.call_tool(name, args))

    # tuple form: (content, structured_dict)
    if isinstance(result, tuple) and len(result) == 2:
        content, structured = result
        if isinstance(structured, dict):
            return structured
        result = content  # fall through to list handling

    # list of content blocks — parse first text block as JSON
    if isinstance(result, list):
        if not result:
            return {}
        block = result[0]
        text = getattr(block, "text", None) or (block.get("text") if isinstance(block, dict) else None)
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"_raw": text}
        return {}

    # dict form
    if isinstance(result, dict):
        return result

    return {"_unhandled": str(type(result))}


def test_tool_registry(mcp):
    tool_names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert tool_names == {
        "canvas_set_page",
        "canvas_ask_mcq",
        "canvas_answer_mcq",
        "canvas_get_state",
        "canvas_add_references",
        "canvas_set_journal_style",
        "canvas_set_visual_theme",
        "canvas_list_journal_styles",
    }


def test_set_page_creates_and_updates(mcp, store: Store):
    r1 = call_tool(mcp, "canvas_set_page", {
        "page_id": "intro",
        "title": "Introduction",
        "content_md": "Body text [@smith2020].",
    })
    assert r1["ok"] is True
    assert r1["page_id"] == "intro"
    assert r1["position"] == 0
    assert store.get_page("intro").content_md == "Body text [@smith2020]."

    # Update in place
    r2 = call_tool(mcp, "canvas_set_page", {
        "page_id": "intro",
        "title": "Introduction",
        "content_md": "Updated body.",
    })
    assert r2["position"] == 0
    assert store.get_page("intro").content_md == "Updated body."


def test_set_page_rejects_bad_id(mcp):
    r = call_tool(mcp, "canvas_set_page", {
        "page_id": "bad id with spaces!",
        "title": "x", "content_md": "y",
    })
    assert r["ok"] is False
    assert "page_id" in r["error"]


def test_set_page_sets_default_current_page(mcp, store: Store):
    # Initially no current page
    assert store.get_kv("current_page") == ""
    call_tool(mcp, "canvas_set_page", {"page_id": "p1", "title": "P1", "content_md": "x"})
    assert store.get_kv("current_page") == "p1"

    # Subsequent pages don't reset current_page
    call_tool(mcp, "canvas_set_page", {"page_id": "p2", "title": "P2", "content_md": "x"})
    assert store.get_kv("current_page") == "p1"


def test_ask_mcq(mcp, store: Store):
    call_tool(mcp, "canvas_set_page", {"page_id": "intro", "title": "Intro", "content_md": "x"})
    r = call_tool(mcp, "canvas_ask_mcq", {
        "mcq_id": "mcq1",
        "page_id": "intro",
        "question": "Pick one",
        "options": ["A", "B", "C"],
    })
    assert r["ok"] is True
    assert r["status"] == "pending"
    assert r["answer"] is None
    assert len(store.get_mcqs("intro")) == 1


def test_ask_mcq_rejects_unknown_page(mcp):
    r = call_tool(mcp, "canvas_ask_mcq", {
        "mcq_id": "mcq1",
        "page_id": "nonexistent",
        "question": "Q",
        "options": ["A", "B"],
    })
    assert r["ok"] is False
    assert "does not exist" in r["error"]


def test_ask_mcq_validates_option_count(mcp):
    call_tool(mcp, "canvas_set_page", {"page_id": "p", "title": "P", "content_md": "x"})
    r = call_tool(mcp, "canvas_ask_mcq", {
        "mcq_id": "m", "page_id": "p", "question": "Q", "options": ["A"],
    })
    assert r["ok"] is False
    assert "options" in r["error"]


def test_ask_mcq_returns_existing_answer(mcp, store: Store):
    call_tool(mcp, "canvas_set_page", {"page_id": "p", "title": "P", "content_md": "x"})
    call_tool(mcp, "canvas_ask_mcq", {"mcq_id": "m", "page_id": "p", "question": "Q", "options": ["A", "B"]})
    # Simulate user answering via the HTTP layer
    store.answer_mcq("m", "B")

    # Re-asking the same MCQ returns the existing answer
    r = call_tool(mcp, "canvas_ask_mcq", {"mcq_id": "m", "page_id": "p", "question": "Q", "options": ["A", "B"]})
    assert r["status"] == "answered"
    assert r["answer"] == "B"


def test_answer_mcq_from_chat(mcp, store: Store):
    """canvas_answer_mcq is the chat-side mirror of clicking an option in the
    browser canvas. Both write to the same store."""
    call_tool(mcp, "canvas_set_page", {"page_id": "p", "title": "P", "content_md": "x"})
    call_tool(mcp, "canvas_ask_mcq", {
        "mcq_id": "m1", "page_id": "p", "question": "Q1", "options": ["A", "B", "C"],
    })
    call_tool(mcp, "canvas_ask_mcq", {
        "mcq_id": "m2", "page_id": "p", "question": "Q2", "options": ["A", "B"],
    })

    # User types "B" in chat in response to m1
    r = call_tool(mcp, "canvas_answer_mcq", {"mcq_id": "m1", "answer": "B"})
    assert r["ok"] is True
    assert r["mcq_id"] == "m1"
    assert r["answer"] == "B"
    assert r["pending_remaining"] == 1  # m2 still open

    # Lowercase "b" + whitespace also accepted
    r = call_tool(mcp, "canvas_answer_mcq", {"mcq_id": "m2", "answer": "  a  "})
    assert r["ok"] is True
    assert r["answer"] == "A"
    assert r["pending_remaining"] == 0


def test_answer_mcq_validates_input(mcp, store: Store):
    call_tool(mcp, "canvas_set_page", {"page_id": "p", "title": "P", "content_md": "x"})
    call_tool(mcp, "canvas_ask_mcq", {
        "mcq_id": "m", "page_id": "p", "question": "Q", "options": ["A", "B"],
    })

    # multi-character answer rejected
    r = call_tool(mcp, "canvas_answer_mcq", {"mcq_id": "m", "answer": "AB"})
    assert r["ok"] is False
    assert "single letter" in r["error"]

    # number rejected
    r = call_tool(mcp, "canvas_answer_mcq", {"mcq_id": "m", "answer": "1"})
    assert r["ok"] is False

    # out-of-range letter rejected
    r = call_tool(mcp, "canvas_answer_mcq", {"mcq_id": "m", "answer": "Z"})
    assert r["ok"] is False

    # unknown mcq_id
    r = call_tool(mcp, "canvas_answer_mcq", {"mcq_id": "nonexistent", "answer": "A"})
    assert r["ok"] is False
    assert "unknown mcq_id" in r["error"]


def test_get_state_returns_full_snapshot(mcp, store: Store):
    call_tool(mcp, "canvas_set_page", {"page_id": "intro", "title": "Intro", "content_md": "x [@ref1]"})
    call_tool(mcp, "canvas_add_references", {"references": [
        {"id": "ref1", "type": "article-journal", "title": "T", "author": [{"family": "A"}]}
    ]})

    r = call_tool(mcp, "canvas_get_state", {})
    assert "current_page" in r
    assert "journal_style" in r
    assert "visual_theme" in r
    assert "viewing_mode" in r
    assert "pages" in r
    assert "mcqs" in r
    assert "references" in r
    assert "companions" in r
    assert "http_url" in r
    assert len(r["pages"]) == 1
    assert len(r["references"]) == 1


def test_add_references(mcp):
    r = call_tool(mcp, "canvas_add_references", {"references": [
        {"id": "a", "type": "article-journal", "title": "TA"},
        {"id": "b", "type": "article-journal", "title": "TB"},
    ]})
    assert r["ok"] is True
    assert r["n_added"] == 2


def test_add_references_drops_no_id(mcp):
    """References without an id should be silently skipped (can't link them to citations)."""
    r = call_tool(mcp, "canvas_add_references", {"references": [
        {"id": "good", "type": "article-journal"},
        {"type": "article-journal", "title": "missing id"},  # no id
    ]})
    assert r["n_added"] == 1


def test_set_journal_style(mcp, store: Store):
    r = call_tool(mcp, "canvas_set_journal_style", {"style_id": "the-lancet"})
    assert r["ok"] is True
    assert r["style_id"] == "the-lancet"
    assert store.get_kv("journal_style") == "the-lancet"


def test_set_visual_theme(mcp, store: Store):
    for theme in ("draft", "print", "reviewer", "compact"):
        r = call_tool(mcp, "canvas_set_visual_theme", {"theme_id": theme})
        assert r["ok"] is True, r
        assert store.get_kv("visual_theme") == theme

    bad = call_tool(mcp, "canvas_set_visual_theme", {"theme_id": "neon"})
    assert bad["ok"] is False


def test_list_journal_styles(mcp):
    r = call_tool(mcp, "canvas_list_journal_styles", {})
    assert r["ok"] is True
    assert r["n"] >= 100
    assert all("id" in s and "title" in s for s in r["styles"])

    lancet = call_tool(mcp, "canvas_list_journal_styles", {"query": "lancet"})
    assert lancet["n"] >= 12
    assert all("lancet" in (s["id"] + s["title"]).lower() for s in lancet["styles"])

    ortho = call_tool(mcp, "canvas_list_journal_styles", {"query": "orthop"})
    assert ortho["n"] >= 4
