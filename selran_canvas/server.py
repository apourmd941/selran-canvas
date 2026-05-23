"""FastMCP server exposing 7 tools for Claude.

Tools:
    canvas_set_page         — render/update a manuscript page (markdown)
    canvas_ask_mcq          — show an inline MCQ on a page
    canvas_get_state        — read everything (current page, mcq answers, journal, theme, mode, companions)
    canvas_add_references   — bulk-add CSL-JSON references to the bibliography
    canvas_set_journal_style— select a journal CSL style (e.g., the-new-england-journal-of-medicine)
    canvas_set_visual_theme — select a visual theme (draft | print | reviewer | compact)
    canvas_list_journal_styles — search the manifest
"""
from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP

from .companions import detect_companions
from .csl_index import is_style_local, list_styles
from .store import Store

VALID_THEMES = {"draft", "print", "reviewer", "compact"}


def build_mcp_server(
    store: Store,
    http_url: str,
    mcp: FastMCP | None = None,
) -> FastMCP:
    """Register the 7 canvas tools.

    Two call modes:
        1. Standalone (default) — `mcp=None`: creates a new FastMCP("selran-canvas")
           and registers tools onto it. Used by `python -m selran_canvas` for the
           direct-MCP path.
        2. Embedded — `mcp=<existing FastMCP>`: registers tools onto the supplied
           instance. Used by the selran-mcp Path B plugin so the canvas tools join
           selran-mcp's unified tool surface (alongside writer/design/sada).
    """
    if mcp is None:
        mcp = FastMCP("selran-canvas")

    # On startup, write detected companions into the store so canvas_get_state can return them.
    companions = detect_companions()
    store.set_kv("companions_json", json.dumps(companions))

    # --- canvas_set_page --------------------------------------------------

    @mcp.tool()
    def canvas_set_page(
        page_id: str,
        title: str,
        content_md: str,
    ) -> dict:
        """Render or update a page on the canvas.

        Args:
            page_id: stable identifier (e.g. "introduction", "methods", "results"). Reusing
                an id updates that page in place; new ids are appended in document order.
            title: human-readable page title (shown in sidebar nav).
            content_md: GitHub-flavored Markdown. Use `[@cite_id]` markers for citations
                — citeproc will format them in the chosen journal style. Tables and
                fenced code blocks are supported. Embed images with the standard
                `![alt](data:image/png;base64,...)` syntax or as URLs.

        Returns:
            {ok, page_id, position, canvas_url}
        """
        if not page_id or not page_id.replace("_", "").replace("-", "").isalnum():
            return {"ok": False, "error": "page_id must be alphanumeric/underscore/hyphen"}
        page = store.upsert_page(page_id, title, content_md)
        # If no current page is set, default to this one
        if not store.get_kv("current_page"):
            store.set_kv("current_page", page_id)
        return {
            "ok": True,
            "page_id": page.page_id,
            "position": page.position,
            "canvas_url": http_url,
        }

    # --- canvas_ask_mcq --------------------------------------------------

    @mcp.tool()
    def canvas_ask_mcq(
        mcq_id: str,
        page_id: str,
        question: str,
        options: list[str],
        anchor: str | None = None,
    ) -> dict:
        """Show an inline multiple-choice question on a page.

        Args:
            mcq_id: stable id; re-using it updates the same MCQ in place.
            page_id: which page to anchor the MCQ to (must already exist).
            question: the prompt text.
            options: 2–6 strings labeled A/B/C/... in the UI.
            anchor: optional string — if the page content contains a marker like
                `<!--mcq:foo-->`, the MCQ card renders at that location. Otherwise
                it appears at the end of the page.

        Returns:
            {ok, mcq_id, status: "pending"|"answered", answer?: str}
        """
        if not (2 <= len(options) <= 6):
            return {"ok": False, "error": "options must have 2–6 entries"}
        if not store.get_page(page_id):
            return {"ok": False, "error": f"page_id '{page_id}' does not exist — call canvas_set_page first"}
        store.upsert_mcq(mcq_id, page_id, question, options, anchor)
        existing = next((m for m in store.get_mcqs(page_id) if m.mcq_id == mcq_id), None)
        return {
            "ok": True,
            "mcq_id": mcq_id,
            "status": "answered" if existing and existing.answer else "pending",
            "answer": existing.answer if existing else None,
        }

    # --- canvas_answer_mcq -----------------------------------------------

    @mcp.tool()
    def canvas_answer_mcq(mcq_id: str, answer: str) -> dict:
        """Submit an answer to a pending MCQ from chat.

        This mirrors what clicking the option in the browser canvas does — both
        write to the same SQLite store. Use this when the user types a single
        letter (A/B/C/...) in chat in response to an MCQ I just asked. The
        browser canvas updates live; the next canvas_get_state() will show the
        MCQ as answered.

        Args:
            mcq_id: the mcq_id we created with canvas_ask_mcq.
            answer: a single letter A-F matching one of the MCQ's options.

        Returns:
            {ok, mcq_id, answer, pending_remaining}
        """
        ans = (answer or "").strip().upper()
        if len(ans) != 1 or not ans.isalpha() or ans not in "ABCDEF":
            return {"ok": False, "error": "answer must be a single letter A-F"}
        if not store.answer_mcq(mcq_id, ans):
            return {"ok": False, "error": f"unknown mcq_id '{mcq_id}'"}
        pending_remaining = sum(1 for m in store.get_mcqs() if not m.answer)
        return {"ok": True, "mcq_id": mcq_id, "answer": ans, "pending_remaining": pending_remaining}

    # --- canvas_get_state ------------------------------------------------

    @mcp.tool()
    def canvas_get_state() -> dict:
        """Read everything Claude needs to act:
            - current_page (which page user is viewing)
            - viewing_mode (section | manuscript | diff)
            - journal_style + visual_theme
            - all pages (id, title, position, last-updated)
            - all MCQs with their answers (or null if pending)
            - all references (CSL-JSON entries, by id)
            - companion_skills detected on this machine
        """
        snap = store.snapshot_dict()
        snap["http_url"] = http_url
        return snap

    # --- canvas_add_references -------------------------------------------

    @mcp.tool()
    def canvas_add_references(references: list[dict]) -> dict:
        """Bulk-add CSL-JSON references to the bibliography.

        Each reference must have an `id` field — reusing an id replaces that entry.
        Standard CSL-JSON fields:
            id, type, author[], title, container-title, container-title-short,
            volume, issue, page, issued.{date-parts}, DOI, PMID, URL, etc.

        Example:
            canvas_add_references([{
                "id": "polack2020",
                "type": "article-journal",
                "author": [{"family":"Polack","given":"FP"},{"family":"Thomas","given":"SJ"}],
                "title": "Safety and Efficacy of the BNT162b2 mRNA COVID-19 Vaccine",
                "container-title": "New England Journal of Medicine",
                "container-title-short": "N Engl J Med",
                "volume": "383", "issue": "27", "page": "2603-2615",
                "issued": {"date-parts": [[2020,12,31]]},
                "DOI": "10.1056/NEJMoa2034577", "PMID": "33301246"
            }])

        Returns:
            {ok, n_added}
        """
        n = store.upsert_references(references or [])
        return {"ok": True, "n_added": n}

    # --- canvas_add_evidence ---------------------------------------------

    @mcp.tool()
    def canvas_add_evidence(
        evidence: list[dict],
        references: list[dict] | None = None,
        page_id: str | None = None,
        heading: str = "Evidence",
    ) -> dict:
        """Insert highlighted evidence pulled from the Selran Librarian.

        The librarian's `librarian_list_evidence` returns two lists; pass them
        straight through:
          - `evidence`: the user's marked passages, each with `citation_id`,
            `category` (from the highlight color), `page`, and `text`.
          - `references`: one CSL-JSON per cited paper (id = citation_id).

        The references are added to the bibliography so each `[@cite_id]`
        resolves; every marked passage becomes a citeable line. If `page_id`
        is given, an `Evidence` section is appended to that page; otherwise the
        ready-to-insert markdown is returned for you to place with
        `canvas_set_page`.

        Returns:
            {ok, n_refs, citations:[{citation_id, marker, markdown}], page_id?}
        """
        items = evidence or []
        refs = list(references or [])
        if not refs:  # fall back to any csl_json carried on the items
            seen: set[str] = set()
            for it in items:
                csl = it.get("csl_json")
                cid = it.get("citation_id") or (csl or {}).get("id")
                if isinstance(csl, dict) and cid and cid not in seen:
                    refs.append({**csl, "id": cid})
                    seen.add(cid)
        n = store.upsert_references(refs)

        citations: list[dict] = []
        lines: list[str] = []
        for it in items:
            cid = it.get("citation_id") or (it.get("csl_json") or {}).get("id")
            if not cid:
                continue
            text = (it.get("text") or it.get("note") or "").strip()
            page = it.get("page")
            category = it.get("category")
            marker = f"[@{cid}]"
            pg = f" (p. {page})" if page else ""
            prefix = f"**{category}** — " if category else ""
            md = f'- {prefix}“{text}” {marker}{pg}' if text else f"- {marker}{pg}"
            citations.append({"citation_id": cid, "marker": marker, "markdown": md})
            lines.append(md)

        result: dict = {"ok": True, "n_refs": n, "citations": citations}
        if page_id and lines:
            existing = store.get_page(page_id)
            body = (existing.content_md + "\n\n") if (existing and existing.content_md) else ""
            title = existing.title if existing else heading
            content = f"{body}## {heading}\n\n" + "\n".join(lines) + "\n"
            store.upsert_page(page_id, title, content)
            result["page_id"] = page_id
        return result

    # --- canvas_set_journal_style ----------------------------------------

    @mcp.tool()
    def canvas_set_journal_style(style_id: str) -> dict:
        """Select a journal CSL style. Citations in all pages re-format instantly.

        The `style_id` must match a Zotero CSL repo entry (the manifest documents 100
        common medical journals). Example IDs:
            - the-new-england-journal-of-medicine
            - american-medical-association  (JAMA)
            - the-lancet
            - the-journal-of-bone-and-joint-surgery  (JBJS)
            - vancouver  (generic numeric — works for most med journals)

        Use canvas_list_journal_styles() to search.
        """
        # Don't fail if the style isn't bundled yet — the browser will lazy-fetch.
        store.set_kv("journal_style", style_id)
        return {"ok": True, "style_id": style_id, "bundled": is_style_local(style_id)}

    # --- canvas_set_visual_theme -----------------------------------------

    @mcp.tool()
    def canvas_set_visual_theme(theme_id: str) -> dict:
        """Select a visual theme — independent of journal-citation style.

        Themes:
            draft     (default; clean serif, generous whitespace, working mode)
            print     (submission-ready; journal-style page layout, page numbers)
            reviewer  (track-changes-style highlights of recent edits)
            compact   (tight spacing for review/scrolling)
        """
        if theme_id not in VALID_THEMES:
            return {"ok": False, "error": f"theme must be one of {sorted(VALID_THEMES)}"}
        store.set_kv("visual_theme", theme_id)
        return {"ok": True, "theme_id": theme_id}

    # --- canvas_list_journal_styles --------------------------------------

    @mcp.tool()
    def canvas_list_journal_styles(query: str | None = None) -> dict:
        """Search the journal-style manifest.

        Args:
            query: optional substring matched against id/title/category.
                None or "" returns all 100 manifest entries.

        Returns:
            {ok, n, styles: [{id, title, category, bundled?}, ...]}
        """
        results = list_styles(query)
        return {"ok": True, "n": len(results), "styles": results}

    return mcp
