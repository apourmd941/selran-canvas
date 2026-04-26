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
      "bio-research-pubmed": "bio-research:pubmed",
    };
    for (const [k, label] of Object.entries(labels)) {
      const li = document.createElement("li");
      if (comps[k]) li.classList.add("installed");
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
    if (!STATE.pages || !STATE.pages.length) {
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
    let body = renderMarkdownWithCitations(page.content_md, CITEPROC, STATE.references || []);
    body = injectMcqs(body, mcqs);
    html += body;
    $("#page-view").innerHTML = html;
    bindMcqHandlers();
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
        ${body}
      </section>`;
    }).join("");
    $("#manuscript-view").innerHTML = sections;
    bindMcqHandlers();
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
  }

  // ---- Boot ----
  async function boot() {
    await fetchManifest();
    bindControls();
    await loadLocale();
    connectWS();
  }
  boot();
})();
