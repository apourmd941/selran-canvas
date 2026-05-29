/* Selran Canvas — frontend
 *
 * Architecture:
 *   - Connect WebSocket to /ws → receive {type:"snapshot", data: STATE}
 *   - On every snapshot: re-render sidebar, viewer, MCQs, bibliography
 *   - User actions (click MCQ, switch journal, switch theme, switch mode, navigate page)
 *     POST to /api/* endpoints; server bumps revision; WS broadcasts new snapshot.
 *   - citeproc-js processes [@cite_id] markers in markdown into journal-styled citations.
 */

(function () {
  "use strict";

  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => document.querySelectorAll(sel);

  // ---- State ----
  let STATE = null;
  let CITEPROC = null;
  let CURRENT_STYLE_XML = null;
  let LOCALE_XML = null;
  let LAST_RENDERED_PAGE_HTML = new Map(); // page_id -> html (for diff highlighting)
  const STYLE_CACHE = new Map(); // style_id -> CSL XML
  const MANIFEST = []; // populated from /api/csl/styles

  // Project hub state (Phase 2). Loaded from /api/projects on init
  // and refreshed on focus / after project_create / after switch.
  // PROJECT_VIEW.companion is non-null when the user has clicked a
  // companion in the sidebar — we render the per-companion artifact
  // list in #companion-view and hide the page/manuscript views until
  // they navigate away (click a page, switch project, etc.).
  const PROJECTS_STATE = {
    list: [],
    current_id: null,
    project: null,
  };
  const PROJECT_VIEW = {
    companion: null,           // e.g. "selran-medical-writer" when a tab is active
    artifacts: [],             // most recent fetch
    open_artifact: null,       // filename currently rendered in the reader
  };
  // Mapping kept in lockstep with the server's projects.COMPANION_TO_SUBDIR.
  // If you add a companion in companions.py / projects.py, mirror the
  // mapping here so clicking it lands in the right subdirectory.
  const COMPANION_TO_SUBDIR = {
    "selran-medical-writer": "manuscript",
    "selran-design":         "figures",
    "selran-data-analysis":  "data",
    "selran-librarian":      "references",
    "bio-research-pubmed":   "pubmed",
  };

  // ---- WebSocket ----
  function connectWS() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${proto}//${location.host}/ws`);
    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.type === "snapshot") {
        applyState(msg.data);
      } else if (msg.type === "ping") {
        // heartbeat
      }
    };
    ws.onclose = () => {
      toast("Connection lost — reconnecting…");
      setTimeout(connectWS, 1500);
    };
    ws.onerror = () => ws.close();
  }

  // ---- Initial fetch ----
  async function fetchManifest() {
    const r = await fetch("/api/csl/styles");
    const items = await r.json();
    MANIFEST.length = 0;
    MANIFEST.push(...items);
    populateJournalSelect();
  }

  function populateJournalSelect(filter = "") {
    const sel = $("#journal-select");
    const q = filter.toLowerCase().trim();
    sel.innerHTML = "";
    let groups = {};
    MANIFEST.forEach((s) => {
      if (q && !(s.id.toLowerCase().includes(q) || (s.title || "").toLowerCase().includes(q) || (s.category || "").toLowerCase().includes(q))) return;
      groups[s.category || "other"] = groups[s.category || "other"] || [];
      groups[s.category || "other"].push(s);
    });
    const order = ["generic","general-medicine","jama-family","lancet-family","annals","top-science","cardiology","oncology","orthopaedics","anesthesia","critical-care","pulmonary","nephrology","endocrinology","gastroenterology","rheumatology","hematology","infectious-disease","radiology","psychiatry","obgyn","public-health","health-services","pediatrics","geriatrics","implementation-science","open-access","other"];
    for (const cat of order) {
      if (!groups[cat]) continue;
      const og = document.createElement("optgroup");
      og.label = cat.replace(/-/g, " ");
      for (const s of groups[cat]) {
        const opt = document.createElement("option");
        opt.value = s.id;
        opt.textContent = (s.bundled ? "★ " : "") + (s.title || s.id);
        og.appendChild(opt);
      }
      sel.appendChild(og);
    }
    if (STATE) sel.value = STATE.journal_style || "vancouver";
  }

  // ---- citeproc setup ----
  async function loadLocale() {
    if (LOCALE_XML) return LOCALE_XML;
    const r = await fetch("/api/csl/locale.xml?lang=en-US");
    if (!r.ok) {
      toast("CSL locale unavailable (network blocked?). Citations may not render.");
      return null;
    }
    LOCALE_XML = await r.text();
    return LOCALE_XML;
  }

  async function loadStyle(styleId) {
    if (STYLE_CACHE.has(styleId)) return STYLE_CACHE.get(styleId);
    const r = await fetch(`/api/csl/style/${encodeURIComponent(styleId)}.csl`);
    if (!r.ok) {
      toast(`Style "${styleId}" unavailable. Falling back to Vancouver.`);
      if (styleId !== "vancouver") return loadStyle("vancouver");
      return null;
    }
    const xml = await r.text();
    STYLE_CACHE.set(styleId, xml);
    return xml;
  }

  function buildCiteproc(styleXml, refs) {
    if (!window.CSL || !styleXml || !LOCALE_XML) return null;
    const refsById = {};
    refs.forEach((r) => { refsById[r.citation_id] = r.csl; });
    const sys = {
      retrieveLocale: () => LOCALE_XML,
      retrieveItem: (id) => refsById[id] || { id, title: `[unresolved: ${id}]` },
    };
    try {
      const engine = new CSL.Engine(sys, styleXml);
      engine.updateItems(Object.keys(refsById));
      return engine;
    } catch (e) {
      console.error("citeproc init failed", e);
      return null;
    }
  }

  // ---- Markdown render with [@cite_id] resolution ----
  function renderMarkdownWithCitations(md, citeproc, refs) {
    if (!md) return "";
    // Pre-pass: substitute [@cite_id] with placeholders so marked() doesn't escape them
    const cites = [];
    const refsById = {};
    (refs || []).forEach((r) => { refsById[r.citation_id] = r.csl; });
    let processed = md.replace(/\[@([A-Za-z0-9_:-]+)\]/g, (m, id) => {
      cites.push(id);
      const idx = cites.length - 1;
      return `CITE${idx}`;
    });
    let html = window.marked ? marked.parse(processed) : `<pre>${escapeHtml(processed)}</pre>`;
    // Sanitize first
    html = window.DOMPurify ? DOMPurify.sanitize(html, { ADD_ATTR: ["data-page-num"] }) : html;

    // Render citations via citeproc (numeric or author-date depending on style)
    html = html.replace(/CITE(\d+)/g, (m, i) => {
      const id = cites[parseInt(i, 10)];
      if (!citeproc) return `<span class="cite cite-fallback" title="${id}">[${id}]</span>`;
      try {
        const result = citeproc.makeCitationCluster([{ id }]);
        return `<span class="cite" data-cite-id="${id}" title="${escapeAttr(refDisplay(refsById[id]))}">${result}</span>`;
      } catch (e) {
        return `<span class="cite cite-fallback" title="${id}">[${id}]</span>`;
      }
    });
    return html;
  }

  function refDisplay(csl) {
    if (!csl) return "";
    const author = (csl.author || []).slice(0, 2).map((a) => a.family || a.literal || "").filter(Boolean).join(", ");
    const year = (csl.issued && csl.issued["date-parts"] && csl.issued["date-parts"][0] && csl.issued["date-parts"][0][0]) || "";
    return `${author}${year ? " " + year : ""}: ${csl.title || csl.id}`;
  }

  function escapeHtml(s) { return String(s).replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])); }
  function escapeAttr(s) { return escapeHtml(s).replace(/\n/g, " "); }

  // ---- Render orchestration ----
  async function applyState(state) {
    const firstLoad = STATE === null;
    const journalChanged = !STATE || STATE.journal_style !== state.journal_style;
    STATE = state;

    // Body attributes
    document.body.dataset.theme = state.visual_theme || "draft";
    document.body.dataset.mode = state.viewing_mode || "section";

    // Top-bar selects (don't dispatch events while we set them)
    setSelect("#mode-select", state.viewing_mode || "section");
    setSelect("#theme-select", state.visual_theme || "draft");
    setSelect("#journal-select", state.journal_style || "vancouver");

    // Indicator
    $("#rev-indicator").textContent = `r${state.revision}`;

    // Companions
    renderCompanions(state.companions || {});

    // Sidebar nav
    renderPageNav();

    // Reload citeproc if journal changed
    if (firstLoad || journalChanged) {
      await loadLocale();
      const xml = await loadStyle(state.journal_style || "vancouver");
      CURRENT_STYLE_XML = xml;
      CITEPROC = buildCiteproc(xml, state.references || []);
    } else {
      // Keep engine but refresh its references (in case new ones added)
      CITEPROC = buildCiteproc(CURRENT_STYLE_XML, state.references || []);
    }

    // Render viewer
    renderViewer();

    // Comments panel (open + resolved). Highlights are injected inside
    // renderSection / renderManuscript after their innerHTML is set.
    renderCommentsPanel();
  }

  function setSelect(sel, value) {
    const el = $(sel);
    if (!el) return;
    if (el.value !== value) el.value = value;
  }

  function renderCompanions(comps) {
    const ul = $("#companion-list");
    ul.innerHTML = "";
    const labels = {
      "selran-medical-writer": "selran-medical-writer",
      "selran-design": "selran-design",
      "selran-data-analysis": "selran-data-analysis",
      "selran-librarian": "selran-librarian",
      "bio-research-pubmed": "bio-research:pubmed",
    };
    for (const [k, label] of Object.entries(labels)) {
      const li = document.createElement("li");
      if (comps[k]) li.classList.add("installed");
      // Phase 2: companions are clickable when there's a current
      // project AND we know which subdirectory the companion writes
      // into. Without a project they're informational only — clicking
      // would have nowhere to land.
      const subdir = COMPANION_TO_SUBDIR[k];
      const canClick = !!(PROJECTS_STATE.current_id && subdir);
      if (canClick) {
        li.classList.add("clickable");
        if (PROJECT_VIEW.companion === k) li.classList.add("active");
        li.onclick = () => openCompanionView(k);
      }
      li.innerHTML = `<span class="dot"></span><span>${label}</span>`;
      ul.appendChild(li);
    }
  }

  function renderPageNav() {
    const ul = $("#page-nav");
    ul.innerHTML = "";
    if (!STATE.pages || !STATE.pages.length) {
      const li = document.createElement("li");
      li.style.cursor = "default";
      li.textContent = "(no pages yet)";
      ul.appendChild(li);
      return;
    }
    for (const p of STATE.pages) {
      const li = document.createElement("li");
      const pendingMcqs = (STATE.mcqs || []).filter((m) => m.page_id === p.page_id && !m.answer).length;
      li.innerHTML = `<span>${escapeHtml(p.title || p.page_id)}</span>` +
        (pendingMcqs > 0 ? `<span class="badge pending">${pendingMcqs}</span>` : "");
      if (p.page_id === STATE.current_page) li.classList.add("active");
      li.onclick = () => navigateTo(p.page_id);
      ul.appendChild(li);
    }
  }

  function renderViewer() {
    // If the user clicked a companion in the sidebar, the companion
    // artifact view takes over the main column. It's the "project hub"
    // surface — switching projects or clicking another companion
    // updates this view; closing it falls back to the page/manuscript
    // flow Canvas already had.
    if (PROJECT_VIEW.companion) {
      $("#viewer-empty").hidden = true;
      $("#page-view").hidden = true;
      $("#manuscript-view").hidden = true;
      $("#bibliography-bar").hidden = true;
      $("#companion-view").hidden = false;
      return;
    }
    $("#companion-view").hidden = true;
    if (!STATE || !STATE.pages || !STATE.pages.length) {
      $("#viewer-empty").hidden = false;
      $("#page-view").hidden = true;
      $("#manuscript-view").hidden = true;
      $("#bibliography-bar").hidden = true;
      return;
    }
    $("#viewer-empty").hidden = true;
    const mode = STATE.viewing_mode || "section";
    if (mode === "section") {
      $("#page-view").hidden = false;
      $("#manuscript-view").hidden = true;
      renderSection();
    } else {
      $("#page-view").hidden = true;
      $("#manuscript-view").hidden = false;
      renderManuscript(mode === "diff");
    }
    renderBibliography();
  }

  function renderSection() {
    let pageId = STATE.current_page;
    if (!pageId || !STATE.pages.find((p) => p.page_id === pageId)) {
      pageId = STATE.pages[0].page_id;
    }
    const page = STATE.pages.find((p) => p.page_id === pageId);
    const mcqs = (STATE.mcqs || []).filter((m) => m.page_id === pageId);
    let html = `<h1>${escapeHtml(page.title)}</h1>`;
    html += guidanceNoteHtml(page);
    let body = renderMarkdownWithCitations(page.content_md, CITEPROC, STATE.references || []);
    body = injectMcqs(body, mcqs);
    html += body;
    const view = $("#page-view");
    view.dataset.pageId = pageId;
    view.innerHTML = html;
    bindMcqHandlers();
    highlightCommentsIn(view, pageId);
    LAST_RENDERED_PAGE_HTML.set(pageId, body);
  }

  function renderManuscript(highlightDiff) {
    const sections = STATE.pages.map((p, idx) => {
      const mcqs = (STATE.mcqs || []).filter((m) => m.page_id === p.page_id);
      let body = renderMarkdownWithCitations(p.content_md, CITEPROC, STATE.references || []);
      body = injectMcqs(body, mcqs);
      // Diff highlighting: if previous render exists and differs, mark new content
      if (highlightDiff && LAST_RENDERED_PAGE_HTML.has(p.page_id) && LAST_RENDERED_PAGE_HTML.get(p.page_id) !== body) {
        body = `<div class="diff-add">${body}</div>`;
      }
      LAST_RENDERED_PAGE_HTML.set(p.page_id, body);
      return `<section class="ms-page" data-page-num="p. ${idx + 1}" data-page-id="${p.page_id}">
        <h1>${escapeHtml(p.title)}</h1>
        ${guidanceNoteHtml(p)}
        ${body}
      </section>`;
    }).join("");
    const view = $("#manuscript-view");
    view.innerHTML = sections;
    bindMcqHandlers();
    // Highlight comments per-section (each .ms-page carries data-page-id).
    view.querySelectorAll(".ms-page[data-page-id]").forEach((sec) => {
      highlightCommentsIn(sec, sec.dataset.pageId);
    });
  }

  function injectMcqs(html, mcqs) {
    if (!mcqs.length) return html;
    let out = html;
    const tail = [];
    for (const m of mcqs) {
      const card = mcqCardHtml(m);
      if (m.anchor && out.includes(`<!--mcq:${m.anchor}-->`)) {
        out = out.replace(`<!--mcq:${m.anchor}-->`, card);
      } else if (m.anchor && out.includes(`&lt;!--mcq:${m.anchor}--&gt;`)) {
        out = out.replace(`&lt;!--mcq:${m.anchor}--&gt;`, card);
      } else {
        tail.push(card);
      }
    }
    return out + tail.join("");
  }

  // Collapsible "what this section should contain" note, shown atop a page
  // that was scaffolded from a template. Uses a native <details> so it's
  // collapsible with zero JS; open by default on an empty page (so the user
  // sees the guidance), collapsed once the section has content.
  function guidanceNoteHtml(page) {
    if (!page || !page.guidance) return "";
    const hasContent = (page.content_md || "").trim().length > 0;
    const open = hasContent ? "" : "open";
    return `<details class="guidance-note" ${open}>
      <summary>📝 What this section should contain</summary>
      <div class="guidance-note-body">${escapeHtml(page.guidance)}</div>
    </details>`;
  }

  function mcqCardHtml(m) {
    const opts = m.options.map((opt, i) => {
      const letter = String.fromCharCode(65 + i);
      const sel = m.answer === letter ? "selected" : "";
      const dis = m.answer ? "disabled" : "";
      return `<button class="${sel}" ${dis} data-mcq-id="${escapeAttr(m.mcq_id)}" data-answer="${letter}">
        <strong>${letter}.</strong> ${escapeHtml(opt)}
      </button>`;
    }).join("");
    const meta = m.answer
      ? `Answered: ${m.answer}`
      : "Click to answer — Claude will use your choice in the next turn.";
    return `<div class="mcq-card ${m.answer ? "answered" : ""}" data-mcq-id="${escapeAttr(m.mcq_id)}">
      <div class="mcq-question">${escapeHtml(m.question)}</div>
      <div class="mcq-options">${opts}</div>
      <div class="mcq-meta">${meta}</div>
    </div>`;
  }

  function bindMcqHandlers() {
    $$(".mcq-card button:not([disabled])").forEach((btn) => {
      btn.onclick = async () => {
        const mcqId = btn.dataset.mcqId;
        const answer = btn.dataset.answer;
        try {
          const r = await fetch(`/api/mcq/${encodeURIComponent(mcqId)}/answer`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ answer }),
          });
          if (!r.ok) toast("Answer rejected");
          else toast(`Answered ${answer}`);
        } catch (e) {
          toast("Network error");
        }
      };
    });
  }

  function renderBibliography() {
    const bar = $("#bibliography-bar");
    const list = $("#bibliography-list");
    if (!CITEPROC || !STATE.references.length) {
      bar.hidden = true;
      return;
    }
    try {
      const result = CITEPROC.makeBibliography();
      if (!result || !result[1] || !result[1].length) {
        bar.hidden = true;
        return;
      }
      list.innerHTML = result[1].map((line) => `<li>${line.replace(/^<div[^>]*>|<\/div>$/g, "")}</li>`).join("");
      bar.hidden = false;
    } catch (e) {
      bar.hidden = true;
    }
  }

  // ---- Toasts ----
  function toast(msg) {
    const t = document.createElement("div");
    t.className = "toast";
    t.textContent = msg;
    $("#toast-stack").appendChild(t);
    setTimeout(() => t.remove(), 4500);
  }

  // ---- User-driven mutations ----
  async function navigateTo(pageId) {
    await fetch("/api/state/current_page", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ page_id: pageId }),
    });
    // WS push will re-render
  }

  function bindControls() {
    $("#mode-select").addEventListener("change", async (e) => {
      await fetch("/api/state/viewing_mode", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode: e.target.value }),
      });
    });
    $("#theme-select").addEventListener("change", async (e) => {
      await fetch("/api/state/visual_theme", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ theme_id: e.target.value }),
      });
    });
    $("#journal-select").addEventListener("change", async (e) => {
      await fetch("/api/state/journal_style", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ style_id: e.target.value }),
      });
    });
    $("#journal-search").addEventListener("input", (e) => populateJournalSelect(e.target.value));

    // Template picker: choosing a template scaffolds its section pages
    // (each with a collapsible guidance note). The select resets to the
    // placeholder afterward so it reads as an action, not a sticky setting.
    $("#template-select").addEventListener("change", async (e) => {
      const id = e.target.value;
      e.target.value = "";
      if (!id) return;
      await scaffoldTemplate(id);
    });

    // Front door ("What are you writing?"): filter the Template dropdown to
    // the chosen category and, for a journal paper, surface the Journal picker.
    document.querySelectorAll(".frontdoor-card").forEach((card) => {
      card.addEventListener("click", () => selectDocKind(card.dataset.kind));
    });

    // Project picker: switching the dropdown sets the new current
    // project on the server, then re-fetches so the sidebar +
    // companion view reflect the new context. The empty-string
    // value at the top represents "no project" which clears the
    // pointer.
    $("#project-select").addEventListener("change", async (e) => {
      const slug = e.target.value;
      if (!slug) return; // ignore the placeholder
      const r = await fetch("/api/projects/current", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: slug }),
      });
      if (!r.ok) {
        toast(`Couldn't switch project: ${await r.text()}`);
        return;
      }
      // Switching project closes any companion view we had open;
      // the artifact list belongs to the previous project.
      PROJECT_VIEW.companion = null;
      PROJECT_VIEW.open_artifact = null;
      await loadProjects();
      renderCompanions(STATE?.companions || {});
      renderViewer();
    });

    // "+ New" button — minimal flow using browser prompts. A richer
    // modal can come later; the prompt-based flow keeps the JS small
    // and matches Canvas's existing dialog-free aesthetic.
    $("#project-new-btn").addEventListener("click", async () => {
      const name = window.prompt("New project name:");
      if (!name || !name.trim()) return;
      const kindRaw = window.prompt(
        "Kind? (paper / design / analysis / learning / exam / general)",
        "general",
      );
      const kind = (kindRaw || "general").trim().toLowerCase();
      const r = await fetch("/api/projects", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: name.trim(), kind }),
      });
      if (!r.ok) {
        toast(`Couldn't create project: ${await r.text()}`);
        return;
      }
      const meta = await r.json();
      toast(`Created project "${meta.name}"`);
      await loadProjects();
      renderCompanions(STATE?.companions || {});
    });

    // Artifact reader close button.
    $("#artifact-reader-close").addEventListener("click", () => {
      PROJECT_VIEW.open_artifact = null;
      $("#companion-artifact-reader").hidden = true;
    });

    // Refresh project list when the window regains focus — picks up
    // projects created from another surface (Launchpad, Claude
    // desktop) without requiring a manual reload.
    window.addEventListener("focus", () => {
      loadProjects().then(() => {
        if (PROJECT_VIEW.companion) refreshCompanionView();
      });
    });

    bindCommentUI();
  }

  // ---- Comment layer (v1.1) -------------------------------------------
  //
  // User → Claude channel (the mirror of MCQs, which are Claude → user).
  // Select text in a rendered page → floating "Comment" button → composer
  // → POST /api/comments. The note is anchored to the highlighted text;
  // Claude reads open comments via canvas_get_state and resolves them with
  // canvas_resolve_comment after editing.

  let PENDING_SELECTION = null;

  function bindCommentUI() {
    // Detect text selections inside the page/manuscript views.
    $("#viewer").addEventListener("mouseup", onViewerMouseUp);

    $("#comment-bubble").addEventListener("mousedown", (e) => {
      // mousedown (not click) so the selection isn't cleared first.
      e.preventDefault();
      openCommentComposer();
    });
    $("#comment-composer-cancel").addEventListener("click", closeCommentUI);
    $("#comment-composer-save").addEventListener("click", submitComment);
    $("#comment-composer-text").addEventListener("keydown", (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "Enter") submitComment();
      if (e.key === "Escape") closeCommentUI();
    });

    // Clicking elsewhere dismisses the floating bubble (but not the
    // composer, which has explicit Cancel/Save).
    document.addEventListener("mousedown", (e) => {
      if (e.target.closest("#comment-bubble, #comment-composer")) return;
      if (!$("#comment-composer").hidden) return;
      hideCommentBubble();
    });
  }

  function onViewerMouseUp(e) {
    if (e.target.closest(".mcq-card, #comment-bubble, #comment-composer, #comments-panel")) return;
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed) { hideCommentBubble(); return; }
    const text = sel.toString().trim();
    if (text.length < 2) { hideCommentBubble(); return; }

    const range = sel.getRangeAt(0);
    const startEl = range.startContainer.nodeType === 3
      ? range.startContainer.parentElement
      : range.startContainer;
    // In manuscript view each .ms-page carries data-page-id; in section
    // view #page-view does. Fall back to the resolved current page.
    const pageEl = startEl && startEl.closest("[data-page-id]");
    let pageId = pageEl ? pageEl.dataset.pageId : (STATE && STATE.current_page);
    if (!pageId && STATE && STATE.pages && STATE.pages.length) pageId = STATE.pages[0].page_id;
    if (!pageId) { hideCommentBubble(); return; }

    PENDING_SELECTION = {
      page_id: pageId,
      anchor_text: text,
      prefix: extractContext(range.startContainer, range.startOffset, -40),
      suffix: extractContext(range.endContainer, range.endOffset, 40),
    };
    showCommentBubble(range.getBoundingClientRect());
  }

  function extractContext(container, offset, len) {
    if (!container || container.nodeType !== 3) return "";
    const t = container.nodeValue || "";
    return len < 0 ? t.slice(Math.max(0, offset + len), offset) : t.slice(offset, offset + len);
  }

  function showCommentBubble(rect) {
    const b = $("#comment-bubble");
    // position:fixed → viewport coords from getBoundingClientRect directly.
    b.style.left = `${Math.max(8, rect.left)}px`;
    b.style.top = `${rect.bottom + 6}px`;
    b.hidden = false;
  }

  function hideCommentBubble() {
    $("#comment-bubble").hidden = true;
  }

  function openCommentComposer() {
    if (!PENDING_SELECTION) return;
    const b = $("#comment-bubble");
    const c = $("#comment-composer");
    c.style.left = b.style.left;
    c.style.top = b.style.top;
    $("#comment-composer-anchor").textContent = `“${truncate(PENDING_SELECTION.anchor_text, 90)}”`;
    const ta = $("#comment-composer-text");
    ta.value = "";
    c.hidden = false;
    b.hidden = true;
    ta.focus();
  }

  function closeCommentUI() {
    $("#comment-bubble").hidden = true;
    $("#comment-composer").hidden = true;
    PENDING_SELECTION = null;
  }

  async function submitComment() {
    if (!PENDING_SELECTION) { closeCommentUI(); return; }
    const body = $("#comment-composer-text").value.trim();
    if (!body) { toast("Type a comment first."); return; }
    try {
      const r = await fetch("/api/comments", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...PENDING_SELECTION, body }),
      });
      if (!r.ok) { toast(`Couldn't save comment: ${await r.text()}`); return; }
      toast("Comment added — Claude will see it next turn.");
    } catch (e) {
      toast("Network error saving comment.");
    }
    closeCommentUI();
    const sel = window.getSelection();
    if (sel) sel.removeAllRanges();
    // WS push re-renders with the new highlight + panel entry.
  }

  // Wrap the first occurrence of each open comment's anchor text in a
  // <mark> so the user sees where their notes are pinned. Cross-node
  // selections (rare) degrade gracefully — they just won't get an inline
  // highlight, but still appear in the panel.
  function highlightCommentsIn(rootEl, pageId) {
    const comments = (STATE && STATE.comments ? STATE.comments : [])
      .filter((c) => c.page_id === pageId && c.status === "open" && c.anchor_text);
    for (const c of comments) wrapFirstOccurrence(rootEl, c.anchor_text, c.comment_id);
  }

  function wrapFirstOccurrence(root, text, commentId) {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
    let node;
    while ((node = walker.nextNode())) {
      if (node.parentElement && node.parentElement.closest(".mcq-card, mark.comment-anchor")) continue;
      const idx = node.nodeValue.indexOf(text);
      if (idx === -1) continue;
      const range = document.createRange();
      range.setStart(node, idx);
      range.setEnd(node, idx + text.length);
      const mark = document.createElement("mark");
      mark.className = "comment-anchor";
      mark.dataset.commentId = commentId;
      try {
        range.surroundContents(mark);
        mark.onclick = () => focusCommentItem(commentId);
      } catch (e) {
        // selection spanned element boundaries — skip inline highlight
      }
      return; // first occurrence only
    }
  }

  function renderCommentsPanel() {
    const panel = $("#comments-panel");
    const list = $("#comments-list");
    const comments = (STATE && STATE.comments) ? STATE.comments : [];
    if (!comments.length) { panel.hidden = true; return; }
    const open = comments.filter((c) => c.status === "open");
    panel.hidden = false;
    $("#comments-panel-count").textContent = `${open.length} open`;
    list.innerHTML = "";
    for (const c of comments) {
      const li = document.createElement("li");
      li.className = `comment-item ${c.status}`;
      li.dataset.commentId = c.comment_id;
      const actions = c.status === "open"
        ? `<button data-act="resolve">Resolve</button><button data-act="dismiss">Dismiss</button>`
        : `<span class="comment-resolved-tag">✓ resolved</span><button data-act="dismiss">Remove</button>`;
      li.innerHTML =
        `<div class="comment-anchor-text">“${escapeHtml(truncate(c.anchor_text, 70))}”</div>` +
        `<div class="comment-body">${escapeHtml(c.body)}</div>` +
        `<div class="comment-actions">${actions}</div>`;
      li.querySelector(".comment-anchor-text").onclick = () => scrollToHighlight(c.comment_id);
      const rb = li.querySelector('[data-act="resolve"]');
      if (rb) rb.onclick = () => resolveComment(c.comment_id);
      li.querySelector('[data-act="dismiss"]').onclick = () => dismissComment(c.comment_id);
      list.appendChild(li);
    }
  }

  function scrollToHighlight(commentId) {
    const mark = document.querySelector(`mark.comment-anchor[data-comment-id="${commentId}"]`);
    if (mark) {
      mark.scrollIntoView({ behavior: "smooth", block: "center" });
      mark.classList.add("flash");
      setTimeout(() => mark.classList.remove("flash"), 1200);
    } else {
      toast("This comment's text was edited — it may already be addressed.");
    }
  }

  function focusCommentItem(commentId) {
    const item = document.querySelector(`#comments-list .comment-item[data-comment-id="${commentId}"]`);
    if (item) {
      item.scrollIntoView({ behavior: "smooth", block: "center" });
      item.classList.add("flash");
      setTimeout(() => item.classList.remove("flash"), 1200);
    }
  }

  async function resolveComment(commentId) {
    try {
      const r = await fetch(`/api/comments/${encodeURIComponent(commentId)}/resolve`, { method: "POST" });
      if (!r.ok) toast("Couldn't resolve comment.");
    } catch { toast("Network error."); }
    // WS push re-renders.
  }

  async function dismissComment(commentId) {
    try {
      const r = await fetch(`/api/comments/${encodeURIComponent(commentId)}`, { method: "DELETE" });
      if (!r.ok) toast("Couldn't remove comment.");
    } catch { toast("Network error."); }
    // WS push re-renders.
  }

  function truncate(s, n) {
    s = String(s || "");
    return s.length > n ? s.slice(0, n - 1) + "…" : s;
  }

  // ---- Project hub (Phase 2) ------------------------------------------

  async function loadProjects() {
    let list = [];
    let current_id = null;
    let project = null;
    try {
      const r = await fetch("/api/projects");
      if (r.ok) {
        const data = await r.json();
        list = data.projects || [];
        current_id = data.current_project_id || null;
      }
      if (current_id) {
        const r2 = await fetch("/api/projects/current");
        if (r2.ok) {
          const d2 = await r2.json();
          project = d2.project || null;
          current_id = d2.current_project_id || current_id;
        }
      }
    } catch (e) {
      // Non-fatal — Canvas still works without projects (single-thread,
      // no organisation). Just leave the picker empty.
    }
    PROJECTS_STATE.list = list;
    PROJECTS_STATE.current_id = current_id;
    PROJECTS_STATE.project = project;
    renderProjectPicker();
  }

  function renderProjectPicker() {
    const sel = $("#project-select");
    if (!sel) return;
    sel.innerHTML = "";
    // Placeholder so we always have a "no selection" state.
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = PROJECTS_STATE.list.length
      ? "(pick a project…)"
      : "(no projects yet)";
    sel.appendChild(placeholder);
    for (const p of PROJECTS_STATE.list) {
      const opt = document.createElement("option");
      opt.value = p.id;
      // Show the name; on hover the tooltip carries the kind so
      // users can tell paper/learning/exam apart in the dropdown.
      opt.textContent = p.name;
      opt.title = `${p.kind} · ${p.id}`;
      if (p.id === PROJECTS_STATE.current_id) opt.selected = true;
      sel.appendChild(opt);
    }
  }

  async function openCompanionView(companionId) {
    if (!PROJECTS_STATE.current_id) {
      toast("Pick or create a project first.");
      return;
    }
    PROJECT_VIEW.companion = companionId;
    PROJECT_VIEW.open_artifact = null;
    await refreshCompanionView();
    renderCompanions(STATE?.companions || {}); // re-render to mark active
    renderViewer();
  }

  async function refreshCompanionView() {
    const companionId = PROJECT_VIEW.companion;
    if (!companionId || !PROJECTS_STATE.current_id) return;
    const subdir = COMPANION_TO_SUBDIR[companionId];
    if (!subdir) return;
    const url = `/api/projects/${encodeURIComponent(PROJECTS_STATE.current_id)}/artifacts/${encodeURIComponent(subdir)}`;
    try {
      const r = await fetch(url);
      if (r.ok) {
        const data = await r.json();
        PROJECT_VIEW.artifacts = data.artifacts || [];
      } else {
        PROJECT_VIEW.artifacts = [];
      }
    } catch {
      PROJECT_VIEW.artifacts = [];
    }
    renderCompanionView();
  }

  function renderCompanionView() {
    const companionId = PROJECT_VIEW.companion;
    // The Design companion gets a rich design-system view (tokens + live
    // component preview) instead of a raw artifact list.
    if (companionId === "selran-design") {
      void renderDesignView();
      return;
    }
    const subdir = COMPANION_TO_SUBDIR[companionId] || "?";
    $("#companion-view-title").textContent = companionId;
    const projectLabel = PROJECTS_STATE.project
      ? `${PROJECTS_STATE.project.name} (${PROJECTS_STATE.project.kind})`
      : PROJECTS_STATE.current_id || "(no project)";
    $("#companion-view-subtitle").textContent =
      `Project: ${projectLabel} · subdir: ${subdir}`;

    const ul = $("#companion-artifact-list");
    ul.innerHTML = "";
    if (!PROJECT_VIEW.artifacts.length) {
      const empty = document.createElement("li");
      empty.className = "artifact-list-empty";
      empty.textContent = `Nothing in ${subdir}/ yet — when ${companionId} writes here, you'll see it.`;
      ul.appendChild(empty);
    } else {
      for (const a of PROJECT_VIEW.artifacts) {
        const li = document.createElement("li");
        li.innerHTML =
          `<span class="artifact-name">${escapeHtml(a.name)}</span>` +
          `<span class="artifact-meta">${formatBytes(a.size_bytes)} · ${formatDate(a.modified_at)}</span>`;
        li.onclick = () => openArtifact(a.name);
        ul.appendChild(li);
      }
    }
  }

  async function openArtifact(filename) {
    const companionId = PROJECT_VIEW.companion;
    if (!companionId || !PROJECTS_STATE.current_id) return;
    const subdir = COMPANION_TO_SUBDIR[companionId];
    if (!subdir) return;
    const url = `/api/projects/${encodeURIComponent(PROJECTS_STATE.current_id)}/artifacts/${encodeURIComponent(subdir)}/${encodeURIComponent(filename)}`;
    try {
      const r = await fetch(url);
      if (!r.ok) {
        toast(`Couldn't load ${filename}`);
        return;
      }
      const data = await r.json();
      PROJECT_VIEW.open_artifact = filename;
      $("#artifact-reader-name").textContent = filename;
      $("#artifact-reader-content").textContent = data.content || "(empty)";
      $("#companion-artifact-reader").hidden = false;
    } catch {
      toast(`Couldn't load ${filename}`);
    }
  }

  function formatBytes(n) {
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
    return `${(n / (1024 * 1024)).toFixed(1)} MB`;
  }

  function formatDate(iso) {
    if (!iso) return "";
    try { return new Date(iso).toLocaleDateString(); }
    catch { return iso; }
  }

  // ---- Design system view (the Design companion) ----------------------

  // Only allow hex or bare color keywords into inline style (the tokens come
  // from a local file, but we never interpolate arbitrary strings into CSS).
  function safeColor(v) {
    const s = String(v == null ? "" : v).trim();
    return /^#[0-9a-fA-F]{3,8}$/.test(s) || /^[a-zA-Z]+$/.test(s) ? s : "transparent";
  }

  async function renderDesignView() {
    $("#companion-view-title").textContent = "Design system";
    const sub = $("#companion-view-subtitle");
    const host = $("#companion-artifact-list");
    $("#companion-artifact-reader").hidden = true;
    host.innerHTML = '<li class="artifact-list-empty">Loading design system…</li>';
    let data;
    try {
      const pid = PROJECTS_STATE.current_id || "";
      const r = await fetch(`/api/design/system?project=${encodeURIComponent(pid)}`);
      data = await r.json();
    } catch { data = { ok: false, error: "network" }; }

    if (!data.ok || !data.tokens) {
      sub.textContent = "";
      host.innerHTML =
        '<li class="artifact-list-empty">No design system yet. When the Design skill writes ' +
        "<code>design-system.md</code> into this project's <code>figures/</code>, its tokens " +
        "and a live component preview render here." +
        (data.error ? ' <span class="muted">(' + escapeHtml(data.error) + ")</span>" : "") +
        "</li>";
      return;
    }
    const t = data.tokens;
    sub.textContent =
      data.file +
      (t.direction ? " · direction: " + t.direction : "") +
      (data.variants && data.variants.length > 1 ? " · " + data.variants.length + " variants" : "");
    host.innerHTML = '<li class="design-host">' + renderDesignPanelHtml(t) + "</li>";
  }

  function swatchRow(label, value) {
    return (
      '<div class="ds-swatch"><span class="ds-chip" style="background:' + safeColor(value) + '"></span>' +
      '<span class="ds-key">' + escapeHtml(label) + "</span>" +
      '<span class="ds-val">' + escapeHtml(String(value)) + "</span></div>"
    );
  }

  function renderDesignPanelHtml(t) {
    const color = t.color || {};
    const type = t.type || {};
    const spacing = t.spacing || {};
    const bodyFont = escapeAttr(String(type.body || "system-ui"));

    const colorRows = Object.entries(color)
      .filter(([, v]) => typeof v === "string")
      .map(([k, v]) => swatchRow(k, v))
      .join("");

    const scale = type.scale || {};
    const scaleRows = Object.entries(scale)
      .map(([k, px]) =>
        '<div class="ds-type-row" style="font-size:' + (Number(px) || 16) + "px;font-family:" + bodyFont + '">' +
        escapeHtml(k) + " · " + escapeHtml(String(px)) + "px — The quick brown fox</div>")
      .join("");

    const accent = safeColor(color.accent || "#2563eb");
    const fg = safeColor(color.fg_primary || "#111111");
    const bgSec = safeColor(color.bg_secondary || "#f4f4f5");
    const border = safeColor(color.border || "#e4e4e7");
    const radius = Number((spacing.radius && (spacing.radius.md || spacing.radius.sm)) || 8) || 8;

    const preview =
      '<div class="ds-preview" style="font-family:' + bodyFont + '">' +
        '<button class="ds-btn" style="background:' + accent + ";border-radius:" + radius + 'px">Primary action</button>' +
        '<div class="ds-card" style="background:' + bgSec + ";border:1px solid " + border + ";border-radius:" + radius + "px;color:" + fg + '">' +
          '<div class="ds-card-title">Card title</div>' +
          '<div class="ds-card-body">A sample card rendered with this system’s tokens — surface, border, radius, body type.</div>' +
        "</div>" +
      "</div>";

    const fonts =
      '<div class="ds-fonts">Display: <b>' + escapeHtml(String(type.display || "—")) + "</b> · " +
      "Body: <b>" + escapeHtml(String(type.body || "—")) + "</b> · Mono: <b>" +
      escapeHtml(String(type.mono || "—")) + "</b></div>";
    const darkNote = color.dark ? '<div class="muted small">+ dark-mode variant defined</div>' : "";

    return (
      '<section class="ds-section"><h3>Preview</h3>' + preview + "</section>" +
      '<section class="ds-section"><h3>Color</h3><div class="ds-swatches">' + colorRows + "</div>" + darkNote + "</section>" +
      '<section class="ds-section"><h3>Type</h3>' + fonts + '<div class="ds-type">' + scaleRows + "</div></section>" +
      '<section class="ds-section"><h3>Spacing</h3><div class="muted small">base unit ' +
        escapeHtml(String(spacing.base_unit || "—")) + "px · radius " +
        escapeHtml(JSON.stringify(spacing.radius || {})) + "</div></section>"
    );
  }

  // ---- Templates ------------------------------------------------------

  async function loadTemplates() {
    const sel = $("#template-select");
    if (!sel) return;
    let items = [];
    try {
      const r = await fetch("/api/templates");
      if (r.ok) items = (await r.json()).templates || [];
    } catch (e) { /* writer skill not present — leave dropdown minimal */ }

    // Reset to just the placeholder, then group paper vs grant.
    sel.innerHTML = "";
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = items.length ? "(choose template…)" : "(no templates found)";
    sel.appendChild(placeholder);

    const groups = { paper: [], grant: [] };
    for (const t of items) (groups[t.category] || (groups[t.category] = [])).push(t);
    const labels = { paper: "Paper types", grant: "Grant mechanisms" };
    for (const cat of Object.keys(labels)) {
      if (!groups[cat] || !groups[cat].length) continue;
      const og = document.createElement("optgroup");
      og.label = labels[cat];
      for (const t of groups[cat]) {
        const opt = document.createElement("option");
        opt.value = t.id;
        const guide = t.reporting_guideline ? ` · ${t.reporting_guideline}` : "";
        opt.textContent = `${t.title} (${t.n_sections} sections)`;
        opt.title = `${t.description || t.title}${guide}`;
        og.appendChild(opt);
      }
      sel.appendChild(og);
    }
  }

  // Front door: filter the Template dropdown to the chosen kind and, for a
  // journal paper, flag the Journal control. Reuses the existing template +
  // journal plumbing — picking a template still scaffolds via scaffoldTemplate.
  function selectDocKind(kind) {
    const sel = $("#template-select");
    if (sel) {
      sel.querySelectorAll("optgroup").forEach((og) => {
        const show =
          kind === "report" ||
          (kind === "paper" && /paper/i.test(og.label)) ||
          (kind === "grant" && /grant/i.test(og.label));
        og.hidden = !show;
        og.disabled = !show;
      });
      sel.value = "";
      try { sel.focus(); } catch (e) { /* focus is best-effort */ }
    }
    const journal = document.querySelector(".control-wide");
    if (journal) journal.classList.toggle("fd-emphasize", kind === "paper");

    const hint = $("#frontdoor-hint");
    if (hint) {
      hint.textContent =
        kind === "paper"
          ? "Pick a paper type in the Template dropdown above, then choose the target journal."
          : kind === "grant"
            ? "Pick a grant mechanism in the Template dropdown above."
            : "Pick a template above to scaffold your report.";
    }
    document.querySelectorAll(".frontdoor-card").forEach((c) =>
      c.classList.toggle("active", c.dataset.kind === kind),
    );
  }

  async function scaffoldTemplate(templateId) {
    try {
      const r = await fetch(`/api/templates/${encodeURIComponent(templateId)}/scaffold`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      if (!r.ok) { toast(`Couldn't scaffold template: ${await r.text()}`); return; }
      const d = await r.json();
      const made = d.created.length;
      const skipped = d.skipped.length;
      let msg = `Scaffolded "${d.title}" — ${made} section${made === 1 ? "" : "s"}`;
      if (skipped) msg += ` (${skipped} already existed, left untouched)`;
      toast(msg);
      // WS push re-renders with the new pages + their guidance notes.
    } catch (e) {
      toast("Network error scaffolding template.");
    }
  }

  // ---- Boot ----
  async function boot() {
    await fetchManifest();
    bindControls();
    await loadLocale();
    await loadProjects();
    await loadTemplates();
    connectWS();
  }
  boot();
})();
