# Selran Canvas

[![tests](https://github.com/apourmd941/selran-canvas/actions/workflows/test.yml/badge.svg)](https://github.com/apourmd941/selran-canvas/actions/workflows/test.yml)

A page-aware canvas for Claude — render manuscript pages, ask MCQ questions inline, and format citations in any of 100+ medical journal styles via [CSL](https://citationstyles.org/).

> Companion to [`selran-medical-writer`](https://github.com/apourmd941/Selran-writing-skill); designed to also serve `selran-design` and `selran-data-analysis` skills when installed.

---

## Why

When Claude writes a manuscript, the chat sidebar fills up with paragraphs. You scroll through them in a narrow panel, lose context, and can't easily see the whole document. Selran Canvas fixes this:

- **Claude renders manuscript pages onto a browser canvas at `localhost:15000`** (or the port your `port-registry` app assigns).
- **Chat stays full-width** for the actual conversation.
- **Inline MCQ widgets** let Claude propose options ("Should the introduction lead with mechanism or epidemiology? [A/B/C]") that you click to answer — Claude sees your answer on the next turn and rewrites accordingly.
- **Select-to-comment** — highlight any sentence in a rendered page and attach a note ("tighten this", "add a limitation about residual confounding"). The comment is pinned to that exact text; Claude reads open comments on the next turn, revises, and marks them resolved (the pin turns green). It's the user → Claude mirror of the MCQ flow.
- **Template dropdown** — pick a manuscript or grant template (RCT, systematic review, cohort, NIH R01/R21/K, …) and the canvas scaffolds the section pages, each topped with a collapsible *"what this section should contain"* note. Claude fills the prose below the note. Templates are authored in the `selran-medical-writer` skill and read at runtime — when that skill isn't installed, the dropdown is simply empty.
- **Switch the journal dropdown** and every citation in the document re-formats instantly in that journal's house style — NEJM numeric, Lancet author-date, JBJS specific punctuation, etc.
- **Three viewing modes**: section-at-a-time (working), full manuscript with page numbers (submission preview), or diff (recent changes highlighted).
- **Companions**: when `selran-design`, `selran-data-analysis`, or `bio-research:pubmed` are installed, Claude uses them automatically — for journal-house templates, generated figures, or PMID-to-citation lookups. When they aren't, the canvas falls back gracefully and still works.

The canvas is **read-only for prose** by design — Claude writes the words; you direct via chat, MCQs, and now inline comments. Free-form click-to-edit ("WYSIWYG" typing directly into the page) is still deferred — it would require operational-transform / CRDT sync. Select-to-comment delivers most of the directing benefit without that complexity.

---

## Architecture

```
┌──────────────────────────┐         ┌─────────────────────────┐
│  Claude (chat)           │         │  Browser canvas         │
│                          │         │  http://localhost:PORT  │
│  Calls 10 MCP tools:     │ ──MCP─► │                         │
│  • canvas_set_page       │         │  • renders pages        │
│  • canvas_ask_mcq        │         │  • shows MCQs            │
│  • canvas_answer_mcq     │ ◄──WS── │  • select-to-comment    │
│  • canvas_resolve_comment│         │  • style + theme picker │
│  • canvas_get_state      │         │  • citeproc-js for CSL  │
│  • canvas_add_references │         │  • bibliography panel   │
│  • canvas_add_evidence   │         │                         │
│  • canvas_set_journal_…  │         │                         │
│  • canvas_set_visual_…   │         │                         │
│  • canvas_list_journal_… │         └─────────────────────────┘
└──────────────────────────┘                      │
              ▲                                   ▼
              └────── shared SQLite store ────────┘
```

Single Python process running:
1. **MCP stdio server** (handles tool calls from Claude)
2. **FastAPI HTTP server** (serves the canvas HTML + WebSocket for live updates)
3. **SQLite state store** (pages, MCQs, references, current-page, selected style/theme)

Tool calls bump a revision counter; the WebSocket pushes a fresh snapshot to every connected browser. User actions (clicking MCQ, switching journal, navigating pages) POST to the HTTP server, which mutates the store, which bumps the revision, which broadcasts.

---

## Install

```bash
git clone https://github.com/apourmd941/selran-canvas
cd selran-canvas
pip install -e .
```

That's it — Python deps install automatically (`mcp`, `fastapi`, `uvicorn[standard]`, `httpx`, `markdown`, `websockets`).

### What ships pre-bundled

- **78 medical-journal CSL XML styles** (all the ones the manifest can resolve), under `selran_canvas/csl/styles/`
- **The English (en-US) CSL locale** under `selran_canvas/csl/locale/`
- **citeproc-js, marked, DOMPurify** vendored under `selran_canvas/canvas/lib/` — no CDN dependency, works fully offline

This means: install → start → use. No first-launch downloads, no network hiccups.

### Optional: extend coverage

If you cite from a journal not in the bundled 100, the canvas will lazy-fetch the CSL from the Zotero repo on first selection (~50KB). To pre-fetch everything (e.g., for fully offline operation behind a corporate firewall):

```bash
python -m selran_canvas.fetch_styles            # fetch missing only
python -m selran_canvas.fetch_styles --force    # re-fetch everything
python -m selran_canvas.fetch_styles --category orthopaedics  # one category
```

### Verify

```bash
python -m selran_canvas --info     # show URL + port + DB path
python -m selran_canvas --demo     # seed a 3-page test manuscript and open browser
python -m pytest tests/            # 60 tests should pass (CI runs them on Linux/macOS/Windows × Python 3.10/3.11/3.12)
```

---

## Wire into Claude (two equivalent paths)

### Path 1 — via the unified `selran-mcp` (recommended)

If you also use `selran-mcp` (the unified MCP server that bridges every Selran skill into Claude desktop — writer, design, SADA, canvas, …), there is **no separate Claude config edit**. Drop the manifest, restart Claude desktop, and the canvas tools join the unified surface alongside the other Selran skills.

```bash
./install.sh   # pip-installs selran-canvas + wires into selran-mcp if present
```

Or manually:

```bash
pip install -e .
# If you have selran-mcp:
pip install -e "${HOME}/NeutronDev/Selran datacore skill platform/mcp_server"
selran-mcp install
selran-mcp status   # should show: canvas    Selran Canvas v1.0.1  plugin ✓
```

Then **⌘Q Claude desktop and reopen.** In a new conversation:

```
Call selran_status and tell me which Selran skills are connected.
```

You should see `canvas` listed with 10 tools. From that point on, any conversation can call `canvas_set_page`, `canvas_ask_mcq`, `canvas_get_state`, etc., and the browser canvas at `http://localhost:15000` (or `SELRAN_CANVAS_PORT`) will reflect every change live.

### Path 2 — direct `mcpServers` entry (Claude Code without selran-mcp)

If you don't have `selran-mcp` and just want canvas as a standalone MCP plugin, add this to `~/.claude/mcp.json` (or the Claude Code platform-equivalent):

```json
{
  "mcpServers": {
    "selran-canvas": {
      "command": "python",
      "args": ["-m", "selran_canvas"],
      "env": { "SELRAN_CANVAS_PORT": "11999" }
    }
  }
}
```

Restart Claude Code. The canvas's 10 tools become available. Open `http://localhost:11999` (or whichever port) to see the canvas.

### Both paths share the same SQLite store

If you happen to run **both** the standalone `python -m selran_canvas` AND the selran-mcp plugin pointing at the same `SELRAN_CANVAS_PORT`, the plugin detects the existing HTTP server and joins it — both routes write to the same `~/.selran-canvas/canvas_state.db`. No coordination needed; pick whichever route fits your setup.

---

## CLI

```bash
python -m selran_canvas              # MCP + HTTP (when launched by Claude Code)
python -m selran_canvas --http-only  # HTTP only, no MCP — for development
python -m selran_canvas --info       # print URL, port, DB path
python -m selran_canvas --demo       # seed examples/medical_writer_demo.py and open browser
```

---

## CSL styles

100 medical journal CSL entries are listed in `selran_canvas/csl/manifest.json`, organised by category. Only **Vancouver** is bundled as XML out of the box — all other styles are lazy-fetched from the [Zotero CSL repo](https://github.com/citation-style-language/styles) on first selection (~50KB per style; cached locally under `selran_canvas/csl/styles/`).

To pre-fetch everything offline:

```bash
python -m selran_canvas.fetch_styles            # all 100, missing only
python -m selran_canvas.fetch_styles --force    # re-fetch everything
python -m selran_canvas.fetch_styles --category orthopaedics  # one category
```

### The 100 journals (by category)

- **General medicine (5):** NEJM • JAMA • BMJ • BMJ Open • Annals of Internal Medicine
- **JAMA family (12):** JAMA Internal Medicine • JAMA Network Open • JAMA Oncology • JAMA Cardiology • JAMA Neurology • JAMA Psychiatry • JAMA Pediatrics • JAMA Surgery • JAMA Dermatology • JAMA Ophthalmology • JAMA Otolaryngology HNS • JAMA Health Forum
- **Lancet family (14):** The Lancet • Lancet Oncology • Lancet Neurology • Lancet Respiratory Medicine • Lancet Diabetes & Endocrinology • Lancet Digital Health • Lancet Healthy Longevity • Lancet Microbe • Lancet Public Health • Lancet HIV • Lancet Infectious Diseases • Lancet Rheumatology • Lancet Psychiatry • Lancet GI & Hepatology
- **Annals series (5):** Annals of Surgery • Annals of Oncology • Annals of Neurology • Annals of Emergency Medicine • Annals of the American Thoracic Society
- **Top science (5):** Nature • Nature Medicine • Cell • Science • PNAS
- **Cardiology (5):** Circulation • JACC • European Heart Journal • Stroke • Heart
- **Oncology (4):** JCO • JCO Precision Oncology • Cancer Cell • Cancer Discovery
- **Orthopaedics (25):** JBJS • Bone and Joint Journal • Bone & Joint Research • CORR • Journal of Arthroplasty • Arthroplasty Today • Spine • The Spine Journal • European Spine Journal • Global Spine Journal • JSES • Journal of Hand Surgery (American) • AJSM • Arthroscopy • KSSTA • Foot & Ankle International • Journal of Pediatric Orthopaedics • JOT • Injury • Acta Orthopaedica • JOR • Osteoarthritis and Cartilage • JBMR • JAAOS • OJSM
- **Anesthesia (3):** Anesthesiology • BJA • Anaesthesia
- **Critical care + pulmonary (4):** Critical Care Medicine • Intensive Care Medicine • AJRCCM • European Respiratory Journal
- **Nephrology (3):** Kidney International • JASN • AJKD
- **Endocrinology (2):** JCEM • Diabetes Care
- **GI & hepatology (2):** Gastroenterology • Hepatology
- **Subspecialty (8):** ARD • Blood • CID • Radiology • American Journal of Psychiatry • AJOG • American Journal of Public Health • Health Affairs
- **HSR + impl (3):** BMJ Quality & Safety • Implementation Science • PLOS Medicine
- **Pediatrics + geriatrics (2):** Pediatrics • JAGS

Total: **100 journals**, plus generic Vancouver + APA. Anything outside this list can be searched via the journal dropdown's text filter; the canvas lazy-fetches it from Zotero on first selection.

---

## The 10 MCP tools

| Tool | Description |
|---|---|
| `canvas_set_page(page_id, title, content_md)` | Render/update a page. Markdown supports `[@cite_id]` citation markers, tables, code, images, and `<!--mcq:foo-->` MCQ anchors. |
| `canvas_ask_mcq(mcq_id, page_id, question, options, anchor?)` | Show an inline MCQ on a page. 2–6 options. Anchor at a `<!--mcq:foo-->` marker if present, else at the end of the page. |
| `canvas_answer_mcq(mcq_id, answer)` | Submit an MCQ answer from chat (mirror of clicking the option in the browser). When the user types a single letter A–F in chat, forward it via this tool — the browser canvas updates live and the answer joins `canvas_get_state()` like any other. |
| `canvas_resolve_comment(comment_id)` | Mark a user's inline comment resolved after addressing the requested edit. Users select text in the browser and attach a note; open comments appear in `canvas_get_state().comments` with the anchored text + instruction. Revise the page, then resolve — the browser pin turns green. |
| `canvas_get_state()` | Read everything: current page, viewing mode, journal style, theme, all pages, all MCQ answers, all comments (open/resolved with anchored text + body), all references, detected companion skills. Call this first thing each turn. |
| `canvas_add_references(refs)` | Bulk-add CSL-JSON entries. Each must have an `id` (matches `[@id]` markers in markdown). Re-using an id replaces the entry. |
| `canvas_add_evidence(evidence, references?, page_id?, heading?)` | Insert highlighted evidence pulled from the Selran Librarian. Pass the librarian's marked passages + their CSL-JSON; references join the bibliography and each passage becomes a citeable line, optionally appended to a page as an `Evidence` section. |
| `canvas_set_journal_style(style_id)` | Switch journal — instantly reformats every citation. IDs match Zotero CSL repo (`the-new-england-journal-of-medicine`, `the-lancet`, `the-journal-of-bone-and-joint-surgery`, etc.). |
| `canvas_set_visual_theme(theme_id)` | `draft` (default working) • `print` (submission preview) • `reviewer` (track-changes-style) • `compact` |
| `canvas_list_journal_styles(query?)` | Search the manifest. Empty query returns all 100. |

---

## Chat-side signals

The canvas is the document; the chat is the conversation. The writer skill follows three light conventions to keep them in sync without flooding chat:

- **Session-start status** — Claude calls `canvas_get_state()` once at the beginning and surfaces a single italicized line, e.g. `*📋 Canvas resumed: 4 pages · NEJM · 1 MCQ pending · viewing methods*`.
- **Per-turn compact badge** — when a turn touches canvas, the **last** line of the reply is one short italicized line like `*📋 4p · NEJM · 1 MCQ pending*` (page count · journal short-name · MCQs pending · theme suffix only if not `draft`). Turns that don't touch canvas don't emit a badge.
- **MCQ chat-mirror** — every `canvas_ask_mcq` call also writes the question and lettered options as a blockquote in the chat reply. The user can either click in the canvas or type a single letter A–F in chat; Claude forwards the typed letter via `canvas_answer_mcq` and the browser card flips to "answered" in real time.

These conventions live in the writer skill's `SKILL.md` ("Chat-side signals (compact, not noisy)" subsection); other skills that drive the canvas can adopt the same pattern.

---

## Templates (manuscript / grant scaffolding)

The top-bar **Template** dropdown lists the writer skill's templates — 12 paper
types (RCT, systematic review, cohort, case-control, diagnostic accuracy, case
report, guidelines, biomechanical, economic, editorial, survey, narrative
review) and 3 NIH grant mechanisms (R01, R21, K). Picking one **scaffolds** its
section pages: each is created empty but with a collapsible *"what this section
should contain"* note (e.g. the RCT Methods note lists the 10 expected
subsections; the R01 Specific Aims note describes the 4-paragraph structure).
Claude then fills the prose below each note via `canvas_set_page`, keeping the
section's `page_id` so the note persists.

- **Source of truth:** templates are authored in
  `selran-medical-writer/templates-manifest.json`. Canvas locates that file via
  the same sibling-scan used for companion detection (or the
  `SELRAN_CANVAS_TEMPLATES_MANIFEST` env override) — there's no bundled copy to
  drift. If the writer skill isn't a sibling on disk, the dropdown is empty and
  the canvas works normally without it.
- **Non-destructive:** re-scaffolding a template skips section pages that
  already exist, so it never overwrites drafted content.
- **Endpoints:** `GET /api/templates` (dropdown list), `POST /api/templates/{id}/scaffold` (create section pages).

---

## Companion-skill detection

On startup, the server probes for sibling skills via:
1. Claude Code MCP config files (multiple known locations checked)
2. Scanning parent directories for `SKILL.md` files matching known skill names
3. The `SELRAN_CANVAS_FAKE_COMPANIONS` env var (testing)

Detected companions are exposed in `canvas_get_state().companions` so Claude can decide to delegate work to them. Examples:

- `selran-design` installed → Claude can ask it for journal-specific page templates (real NEJM 2-column, real JAMA layout, etc.)
- `selran-data-analysis` installed → Claude delegates "render KM survival curve" to it; gets back SVG; embeds in the page
- `bio-research:pubmed` installed → Claude resolves PMIDs to CSL-JSON automatically

When companions aren't installed, the canvas falls back to defaults — it always works standalone.

---

## Smoke test

```bash
python -m selran_canvas --demo
```

This seeds a 3-page mini-manuscript (Introduction, Methods, Results) with 4 references and 3 MCQs, and opens your browser. Click an MCQ option, then switch the journal dropdown from "Vancouver" to e.g. "the-new-england-journal-of-medicine" — every citation should re-format instantly. Toggle the Mode dropdown to "Manuscript" to see the full document with page numbers.

---

## Troubleshooting

**Port 11999 / 15000 busy:** Set a different port via env var.
```bash
SELRAN_CANVAS_PORT=12345 python -m selran_canvas
```

**Browser shows "Connected to MCP. Waiting for Claude…":** Canvas is running fine, just no content yet. Either invoke Claude with the canvas tools, or run `python -m selran_canvas --demo` to seed test content.

**Citations rendering as `[smith2020]` literal text instead of formatted:** Either citeproc didn't load (check browser console — `/static/lib/citeproc.js` should return 200) or the reference id you cited isn't in `canvas_add_references` yet. The bracketed-id fallback is intentional — it tells you exactly which references are missing.

**Style 404 for an obscure journal:** Click the journal you want — if it 404s, the canvas falls back to Vancouver and toasts a warning. The Zotero CSL ID may have shifted; file an issue with the manifest entry that broke and we'll fix the `csl_id` mapping. The canvas always works; only specific styles may need a corrected mapping.

**Companions not detected:** Check `~/.claude/mcp.json` for the right server names. You can force a specific set for testing:
```bash
SELRAN_CANVAS_FAKE_COMPANIONS=selran-design,selran-data-analysis python -m selran_canvas
```

**State survives across runs but you want a clean slate:**
```bash
rm ~/.selran-canvas/canvas_state.db
```

**Want to host the canvas in a different shell-launched process from the MCP server:** Run two separate processes — `python -m selran_canvas --http-only` for the canvas, and let Claude Code launch the MCP-only path. Both share the SQLite store.

---

## Roadmap

**v1.0 (initial release)** — 7 MCP tools, page rendering with `[@cite]` markers, MCQ widgets with `<!--mcq:anchor-->` placement, citeproc-js processing 100 journal styles, companion-skill detection (graceful fallback), three viewing modes (section / manuscript with page numbers / diff), four visual themes (draft / print / reviewer / compact), SQLite-persisted state, vendored CDN libs (offline-first), 46 passing pytest tests, all 78 unique CSL files bundled in repo, **cross-OS CI** (GitHub Actions matrix: Linux/macOS/Windows × Python 3.10/3.11/3.12 = 9 jobs, every push and PR), Path B `selran-mcp` plugin (`canvas_mcp_plugin/__init__.py`) with regression-locked contract tests so the unified-server discovery never silently breaks.

**v1.0.1** — Added `canvas_answer_mcq(mcq_id, answer)`, letting users answer MCQs by typing a single letter A–F in chat instead of (or in addition to) clicking in the browser. Both routes write to the same SQLite store, so the canvas card flips to "answered" live either way. Paired with three "chat-side signals" conventions in the writer skill — session-start status line, per-turn compact badge, and an MCQ chat-mirror.

**v1.1 (current)** — Project hub UI (project picker + clickable companions + per-companion artifact view); two new MCP tools — `canvas_add_evidence` (pulls highlighted passages + CSL-JSON from the Selran Librarian into the bibliography and page) and **`canvas_resolve_comment`** powering the **select-to-comment layer** (users highlight text and attach an instruction; the note is anchored via text-quote anchoring, appears in `canvas_get_state().comments`, and Claude resolves it after editing); and the **template dropdown** that scaffolds manuscript/grant section pages from the writer skill's 15-template manifest, each with a collapsible per-section guidance note (pages gain a `guidance` column). 10 MCP tools total. 70 passing pytest tests. Still planned for the v1.1 line: print-to-PDF, "lock page", history/undo, keyboard shortcut to next pending MCQ, persistent style/theme/mode preferences.

**v1.2 (later)** — free-form click-to-edit a sentence with conflict-resolved sync (operational transforms or CRDTs). This is the "Google Docs" upgrade — significant scope, deferred until needed. Select-to-comment (shipped in v1.1) covers most of the directing need without it.

**v1.3+ (further out)** — collaborative multi-user sessions, structured outline/heading nav, smart figures (markdown-defined Mermaid + plain-language → Mermaid via Claude), CONSORT-flow / PRISMA-flow / SoA Schedule-of-Activities widget primitives.

---

## Licence

MIT.

## Maintainers

Built collaboratively by Selran. Contributions welcome — please file issues for missing journal styles, broken Zotero CSL ID guesses, or companion-detection failures.
