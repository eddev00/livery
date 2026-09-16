/* Anti-Rdiim local UI. Vanilla JS, hash routing, no build step. */
(() => {
  "use strict";
  const $ = (sel, root = document) => root.querySelector(sel);
  const view = $("#view");
  const CHECK_ORDER = { FAIL: 0, WARN: 1, PASS: 2, SKIP: 3 };
  let STATE = null;          // /api/state
  let pending = [];          // files chosen for the next build
  let lastForm = null;       // remembered form values
  let live = null;           // EventSource for the job being watched in the sidebar

  // ---------------------------------------------------------------- helpers
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const human = (name) => name.replace(/_/g, " ");
  const basename = (p) => p.split(/[\\/]/).pop();
  const dirname = (p) => p.slice(0, Math.max(0, p.length - basename(p).length - 1));
  const fmtTime = (iso) => { if (!iso) return ""; const d = new Date(iso); return d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }); };
  const fmtSecs = (s) => (s < 60 ? `${s.toFixed(1)} s` : `${Math.floor(s / 60)} min ${Math.round(s % 60)} s`);
  const fmtBytes = (n) => (n > 1048576 ? `${(n / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`);
  const I = {
    docs: `<svg viewBox="0 0 24 24" fill="none"><path d="M7 3h7l5 5v13H7z"/><path d="M14 3v5h5"/><path d="M10 13h6M10 17h6"/></svg>`,
    plus: `<svg viewBox="0 0 24 24" fill="none"><path d="M12 5v14M5 12h14"/></svg>`,
    jobs: `<svg viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/></svg>`,
    open: `<svg viewBox="0 0 24 24" fill="none"><path d="M14 5h5v5"/><path d="M19 5l-8 8"/><path d="M17 13v6H5V7h6"/></svg>`,
    trash: `<svg viewBox="0 0 24 24" fill="none"><path d="M5 7h14"/><path d="M9 7V4h6v3"/><path d="M7 7l1 13h8l1-13"/></svg>`,
    folder: `<svg viewBox="0 0 24 24" fill="none"><path d="M3 6h6l2 2h10v11H3z"/></svg>`,
    up: `<svg viewBox="0 0 24 24" fill="none"><path d="M12 19V6"/><path d="m6 12 6-6 6 6"/></svg>`,
    file: `<svg viewBox="0 0 24 24" fill="none"><path d="M7 3h7l5 5v13H7z"/><path d="M14 3v5h5"/></svg>`,
    brush: `<svg viewBox="0 0 24 24" fill="none"><path d="M4 20c2-1 2.5-3 3-5l9.5-9.5a2.1 2.1 0 0 1 3 3L10 18c-2 .5-4 1-6 2z"/><path d="M14 7l3 3"/></svg>`,
  };

  async function api(path, opts = {}) {
    const r = await fetch(path, { credentials: "same-origin", ...opts });
    const ct = r.headers.get("content-type") || "";
    const body = ct.includes("json") ? await r.json() : await r.text();
    if (!r.ok) throw new Error((body && body.error) || (typeof body === "string" ? body : r.statusText));
    return body;
  }
  const postJSON = (path, data) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) });
  async function deleteDoc(id, name) {
    if (!confirm(`Delete the output folder of “${name}”?\n\nThe PDF, report and Typst files are removed. The original Word file is not touched.`)) return false;
    try { await api(`/api/docs/${id}`, { method: "DELETE" }); toast(`Deleted ${name}`); return true; }
    catch (err) { toast(err.message, true); return false; }
  }

  let toastTimer;
  function toast(msg, isError = false) {
    const t = $("#toast");
    t.textContent = msg; t.className = "toast show" + (isError ? " error" : "");
    clearTimeout(toastTimer); toastTimer = setTimeout(() => (t.className = "toast"), isError ? 6000 : 2800);
  }
  const chip = (s, label) => `<span class="chip ${esc(s || "")}">${esc(label || s || "—")}</span>`;
  const pageHead = (eyebrow, title, desc, actions = "") =>
    `<div class="page-head"><div><div class="eyebrow">${eyebrow}</div><h1>${title}</h1>${desc ? `<p>${desc}</p>` : ""}</div><div class="actions">${actions}</div></div>`;

  // ---------------------------------------------------------------- navigation
  const NAV = [["home", "#/", "Documents", I.docs], ["new", "#/new", "New build", I.plus], ["template", "#/template", "Template", I.brush]];
  function renderNav(active) {
    $("#sidenav").innerHTML = NAV.map(([k, h, l, icon]) => `<a class="nav-item ${k === active ? "active" : ""}" href="${h}">${icon}${l}</a>`).join("");
    $("#mobilenav").innerHTML = NAV.map(([k, h, l, icon]) => `<button class="${k === active ? "active" : ""}" data-go="${h}">${icon}${l}</button>`).join("")
      + `<button id="mquit">${I.jobs}Quit</button>`;
    $("#mobilenav").style.gridTemplateColumns = `repeat(${NAV.length + 1}, 1fr)`;
    $("#mobilenav").querySelectorAll("[data-go]").forEach((b) => b.addEventListener("click", () => (location.hash = b.dataset.go)));
    $("#mquit").addEventListener("click", quit);
  }

  function watchTopbar(jobId) {
    if (live) live.close();
    const a = $("#activity");
    live = new EventSource(`/api/jobs/${jobId}/events`);
    const update = (snap) => {
      const running = snap.docs.find((d) => !["done", "error", "queued"].includes(d.stage));
      const done = snap.docs.filter((d) => d.stage === "done" || d.stage === "error").length;
      a.hidden = snap.state === "done";
      a.innerHTML = `<a href="#/job/${snap.id}">${running ? `${esc(running.name)} · ${esc(running.stage)}` : "working"} ${done}/${snap.docs.length}</a>`;
    };
    live.addEventListener("snapshot", (e) => update(JSON.parse(e.data)));
    live.addEventListener("doc", () => api(`/api/jobs/${jobId}`).then(update));
    live.addEventListener("end", (e) => { update(JSON.parse(e.data)); live.close(); live = null;
      const s = JSON.parse(e.data).summary; toast(`Finished: ${s.PASS} pass, ${s.WARN} warn, ${s.FAIL} fail${s.error ? `, ${s.error} error` : ""}`, s.FAIL > 0 || s.error > 0);
      if (location.hash === "#/" || location.hash === "") route(); });
    live.onerror = () => { live.close(); live = null; a.hidden = true; };
  }

  // ---------------------------------------------------------------- home
  async function renderHome() {
    renderNav("home");
    const [docs, state] = await Promise.all([api("/api/docs"), api("/api/state")]);
    STATE = state;
    const runningJobs = state.jobs.filter((j) => j.state !== "done");
    if (runningJobs.length && !live) watchTopbar(runningJobs[0].id);
    const n = { all: docs.length, pass: docs.filter((d) => d.status === "PASS").length, fail: docs.filter((d) => d.status === "FAIL").length, twins: docs.filter((d) => d.anonymized).length };
    view.className = "page";
    view.innerHTML = pageHead("Library", "Documents", `Every Word file restyled through the template, checked word for word. Output folder <code>${esc(state.out_root)}</code>.`,
        `<a class="btn primary" href="#/new">${I.plus}New build</a>`)
      + `<div class="grid metric-grid">
          <div class="card metric"><span>Documents</span><strong>${n.all}</strong><small>${n.twins} lorem ipsum twin${n.twins === 1 ? "" : "s"}</small></div>
          <div class="card metric"><span>Passing</span><strong>${n.pass}</strong><small>every check green</small></div>
          <div class="card metric"><span>Failing</span><strong>${n.fail}</strong><small>content drift or render error</small></div>
        </div>`
      + (runningJobs.length ? `<div class="card flush" style="margin-bottom:14px"><div class="card-title-row" style="padding:16px 18px 0"><h2>In progress</h2></div>${runningJobs.map(jobRows).join("")}</div>` : "")
      + `<div class="card flush">${docs.length ? `<div class="list">
          <div class="list-row head"><span>Document</span><span>Status</span><span>Checks</span><span>Built</span><span></span></div>
          ${docs.map((d) => `<div class="list-row link" data-id="${d.id}">
            <div class="doc-name"><strong><em>${esc(d.name)}</em>${d.anonymized ? chip("lorem", "lorem ipsum") : ""}</strong><span>${esc(d.title || "")}</span></div>
            <div>${chip(d.status)}</div>
            <div class="dots" title="${esc(Object.entries(d.checks).map(([k, v]) => `${human(k)}: ${v}`).join("\n"))}">${Object.values(d.checks).map((s) => `<i class="${s}"></i>`).join("")}</div>
            <div class="muted small">${esc(fmtTime(d.generated_at))}</div>
            <div class="row-actions">${d.pdf ? `<a class="row-action" href="/files/${d.id}/${encodeURIComponent(d.pdf)}" target="_blank" rel="noopener" title="Open PDF" data-stop>${I.open}</a>` : ""}<button class="row-action danger" data-del="${d.id}" data-name="${esc(d.name)}" title="Delete output folder" data-stop>${I.trash}</button></div>
          </div>`).join("")}</div>`
        : `<div class="empty">No documents built yet.<br><a class="btn primary" href="#/new">${I.plus}Build your first document</a></div>`}</div>`;
    view.querySelectorAll(".list-row.link").forEach((tr) => tr.addEventListener("click", (e) => { if (e.target.closest("[data-stop]")) return; location.hash = `#/doc/${tr.dataset.id}`; }));
    view.querySelectorAll("[data-del]").forEach((b) => b.addEventListener("click", async () => { if (await deleteDoc(b.dataset.del, b.dataset.name)) renderHome(); }));
  }

  // ---------------------------------------------------------------- new build
  // Cover variant picker: the [cover.variants.*] tables of the theme; "" = theme default.
  function variantSelect(value, blankLabel) {
    const variants = STATE?.cover_variants || [];
    if (!variants.length) return "";
    const opts = [`<option value="" ${value ? "" : "selected"}>${esc(blankLabel)}</option>`]
      .concat(variants.map((v) => `<option value="${esc(v.name)}" ${v.name === value ? "selected" : ""}>${esc(v.name)}${v.description ? ` — ${esc(v.description)}` : ""}</option>`));
    return `<div class="field"><label>Cover variant</label><select name="cover">${opts.join("")}</select></div>`;
  }

  function renderNew() {
    renderNav("new");
    const f = lastForm || {};
    const field = (name, label, hint = "", value = "") => `<div class="field"><label>${label}${hint ? ` <span class="hint">${hint}</span>` : ""}</label><input type="text" name="${name}" value="${esc(value)}"></div>`;
    view.className = "page";
    view.innerHTML = pageHead("Pipeline", "New build", "Each Word file becomes one styled PDF plus a report. Any content drift between source and PDF is a FAIL.")
      + `<div class="grid two">
        <div class="card"><div class="card-title-row"><h2>Files</h2><button class="btn small" id="browse">${I.folder}Browse…</button></div>
          <div class="filelist" id="files"></div></div>
        <div class="card"><div class="card-title-row"><h2>Options</h2></div>
          <form id="form" autocomplete="off"><div class="fields">
            ${field("out_root", "Output folder", "", f.out_root || STATE?.out_root || "")}
            <div class="muted small">Styled with <code>${esc((STATE?.template || "").replace(STATE.repo_root + "/", ""))}</code>. Edit the template, then use “Rebuild &amp; re-verify” on a document to see the change.</div>
            <label class="check-row"><input type="checkbox" name="accept_changes" ${f.accept_changes ? "checked" : ""}><div>Accept tracked changes <span>— otherwise unresolved changes are a FAIL</span></div></label>
            <label class="check-row"><input type="checkbox" name="clean" ${f.clean ? "checked" : ""}><div>Clean rebuild <span>— delete the document's output folder first</span></div></label>
            <details class="group" ${f.department_name || f.title || f.number || f.version || f.cover ? "open" : ""}><summary>Cover page</summary>
              <div class="two-col">${variantSelect(f.cover || "", "previous build, then the theme default")}${field("department_name", "Department name", "label above the title", f.department_name)}${field("number", "Cover number", "", f.number)}${field("title", "Title", "blank: from the document", f.title)}${field("version", "Version", "e.g. V1, MAR 2026", f.version)}</div>
              <p class="muted small" style="margin-top:12px">Blank fields keep the previous build's answers, then the theme defaults. Values apply to every file in this batch.</p>
            </details>
            <details class="group" ${f.department ? "open" : ""}><summary>Department symbol</summary>
              <div class="two-col">${field("department", "Department symbol", "e.g. IT: footer prefix, big glyph on the cover", f.department)}</div>
            </details>
            <div class="form-foot"><button class="btn primary" type="submit" id="run">Build</button><span class="muted small" id="runhint"></span></div>
          </div></form></div>
      </div>`;
    const renderFiles = () => {
      const box = $("#files");
      box.innerHTML = pending.length ? pending.map((p, i) => `<div class="file-item"><div><strong title="${esc(p)}">${esc(basename(p))}</strong><span>${esc(dirname(p))}</span></div><button class="row-action danger" data-i="${i}" title="Remove">${I.trash}</button></div>`).join("")
                                     : `<div class="empty">No files yet. Use Browse… to pick Word documents.</div>`;
      box.querySelectorAll("[data-i]").forEach((b) => b.addEventListener("click", () => { pending.splice(+b.dataset.i, 1); renderFiles(); }));
      $("#runhint").textContent = pending.length ? `${pending.length} file${pending.length > 1 ? "s" : ""} selected` : "";
    };
    renderFiles();
    $("#browse").addEventListener("click", () => openBrowser((chosen) => { chosen.forEach((p) => !pending.includes(p) && pending.push(p)); renderFiles(); }));
    $("#form").addEventListener("submit", async (e) => {
      e.preventDefault();
      if (!pending.length) { toast("Choose at least one Word file", true); return; }
      const fd = new FormData(e.target);
      const options = Object.fromEntries(fd.entries());
      options.accept_changes = fd.has("accept_changes"); options.clean = fd.has("clean");
      lastForm = options;
      $("#run").disabled = true;
      try {
        const job = await postJSON("/api/jobs", { files: pending, options });
        pending = [];
        watchTopbar(job.id);
        location.hash = `#/job/${job.id}`;
      } catch (err) { toast(err.message, true); $("#run").disabled = false; }
    });
  }

  // ---------------------------------------------------------------- file browser modal
  async function openBrowser(onDone) {
    const modal = $("#modal");
    const selected = new Set();
    let current = STATE?.samples || STATE?.home;
    modal.hidden = false;
    modal.innerHTML = `<div class="dialog" role="dialog" aria-label="Choose Word files">
      <div class="head"><div class="crumbs" id="crumbs"></div><button class="btn icon" id="close" title="Close">✕</button></div>
      <div class="quick">${[["Samples", STATE.samples], ["Project", STATE.repo_root], ["Home", STATE.home]].map(([n, p]) => `<button class="filter" data-go="${esc(p)}">${n}</button>`).join("")}</div>
      <div class="browser" id="browser"></div>
      <div class="foot"><span class="muted small" id="count"></span><span style="flex:1"></span><button class="btn" id="cancel">Cancel</button><button class="btn primary" id="add" disabled>Add selected</button></div></div>`;
    const close = () => { modal.hidden = true; modal.innerHTML = ""; };
    $("#close", modal).onclick = $("#cancel", modal).onclick = close;
    modal.addEventListener("click", (e) => { if (e.target === modal) close(); });
    $("#add", modal).onclick = () => { onDone([...selected]); close(); };
    modal.querySelectorAll("[data-go]").forEach((b) => b.addEventListener("click", () => load(b.dataset.go)));
    const refreshCount = () => { $("#count", modal).textContent = selected.size ? `${selected.size} selected` : ""; $("#add", modal).disabled = !selected.size; };
    async function load(path) {
      let data;
      try { data = await api(`/api/fs?path=${encodeURIComponent(path)}`); } catch (err) { toast(err.message, true); return; }
      current = data.path;
      const parts = data.path.split("/").filter(Boolean);
      $("#crumbs", modal).innerHTML = `<a href="#" data-p="/">/</a>` + parts.map((p, i) => `<span class="sep">/</span><a href="#" data-p="${esc("/" + parts.slice(0, i + 1).join("/"))}">${esc(p)}</a>`).join("");
      $("#crumbs", modal).querySelectorAll("a").forEach((a) => a.addEventListener("click", (e) => { e.preventDefault(); load(a.dataset.p); }));
      const b = $("#browser", modal);
      b.innerHTML = (data.parent ? `<div class="row dir" data-d="${esc(data.parent)}">${I.up}<span class="muted">Up one level</span></div>` : "")
        + data.dirs.map((d) => `<div class="row dir" data-d="${esc(data.path + "/" + d)}">${I.folder}${esc(d)}</div>`).join("")
        + data.files.map((f) => { const full = data.path + "/" + f.name; return `<label class="row file"><input type="checkbox" data-f="${esc(full)}" ${selected.has(full) ? "checked" : ""}>${I.file}<span>${esc(f.name)}</span><span class="size">${fmtBytes(f.size)}</span></label>`; }).join("")
        + (!data.dirs.length && !data.files.length ? `<div class="empty">Empty folder</div>` : "");
      b.querySelectorAll(".dir").forEach((d) => d.addEventListener("click", () => load(d.dataset.d)));
      b.querySelectorAll("input").forEach((c) => c.addEventListener("change", () => { c.checked ? selected.add(c.dataset.f) : selected.delete(c.dataset.f); refreshCount(); }));
    }
    load(current);
  }

  // ---------------------------------------------------------------- template editor
  let templateDraft = null;  // { name, saved, current } kept across re-renders of the same page
  async function renderTemplate(name) {
    renderNav("template");
    let info;
    try { info = await api("/api/template"); } catch (err) { view.innerHTML = `<div class="empty">${esc(err.message)}</div>`; return; }
    const files = info.files;
    if (!files.length) { view.innerHTML = `<div class="empty">No template files found in <code>${esc(info.dir)}</code>.</div>`; return; }
    const active = files.find((f) => f.name === name) ? name : (files.find((f) => f.name === "theme.toml") ? "theme.toml" : files[0].name);
    if (templateDraft && templateDraft.name !== active && templateDraft.current !== templateDraft.saved
        && !confirm(`Discard unsaved changes to ${templateDraft.name}?`)) { location.hash = `#/template/${templateDraft.name}`; return; }
    let text;
    try { text = await api(`/api/template/${encodeURIComponent(active)}`); } catch (err) { view.innerHTML = `<div class="empty">${esc(err.message)}</div>`; return; }
    if (!templateDraft || templateDraft.name !== active || templateDraft.saved !== text) templateDraft = { name: active, saved: text, current: text };
    view.className = "page wide";
    view.innerHTML = pageHead("Styling", "Template", `Free-text editing of the files in <code>${esc(info.dir)}</code>. Content never lives here, only looks: fonts, colours, margins, cover, table style and the Typst rules.`,
        `<button class="btn" id="revert" disabled>Revert</button><button class="btn" id="save" disabled>Save</button><button class="btn primary" id="saveall" title="Save, then recompile and re-check every document with the new template">Save &amp; rebuild all</button>`)
      + `<div class="filters" style="display:flex;gap:8px;margin-bottom:14px">${files.map((f) => `<a class="filter ${f.name === active ? "active" : ""}" href="#/template/${encodeURIComponent(f.name)}">${esc(f.name)}</a>`).join("")}
           <span class="muted small" style="margin-left:auto;align-self:center" id="editstatus"></span></div>
         <div class="card flush"><textarea class="editor" id="editor" spellcheck="false" wrap="off"></textarea></div>`;
    const ed = $("#editor"); ed.value = templateDraft.current;
    const status = $("#editstatus");
    const refresh = () => {
      templateDraft.current = ed.value;
      const dirty = ed.value !== templateDraft.saved;
      $("#save").disabled = !dirty; $("#revert").disabled = !dirty;
      status.textContent = dirty ? "unsaved changes" : `saved · ${ed.value.split("\n").length} lines`;
    };
    refresh();
    ed.addEventListener("input", refresh);
    ed.addEventListener("keydown", (e) => {
      if (e.key === "Tab") { e.preventDefault(); const s = ed.selectionStart; ed.setRangeText("  ", s, ed.selectionEnd, "end"); refresh(); }
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") { e.preventDefault(); save(); }
    });
    $("#revert").addEventListener("click", () => { ed.value = templateDraft.saved; refresh(); });
    async function save() {
      if (ed.value === templateDraft.saved) return true;
      try {
        await api(`/api/template/${encodeURIComponent(active)}`, { method: "PUT", headers: { "Content-Type": "text/plain; charset=utf-8" }, body: ed.value });
        templateDraft.saved = ed.value; refresh(); toast(`Saved ${active}`); return true;
      } catch (err) { toast(err.message, true); return false; }
    }
    $("#save").addEventListener("click", save);
    $("#saveall").addEventListener("click", async () => {
      if (!(await save())) return;
      try { const job = await postJSON("/api/template/rebuild", {}); watchTopbar(job.id); location.hash = `#/job/${job.id}`; }
      catch (err) { toast(err.message, true); }
    });
  }

  // ---------------------------------------------------------------- job view
  function jobRows(snap) {
    return `<div class="list">${snap.docs.map((d) => {
      const secs = d.started ? ((d.finished || Date.now() / 1000) - d.started) : 0;
      const cls = d.stage === "done" ? "done" : d.stage === "error" ? "error" : d.stage === "queued" ? "" : "running";
      return `<div class="list-row ${d.doc_id && d.stage === "done" ? "link" : ""}" data-id="${d.doc_id || ""}" data-tab="${snap.kind === "anonymize" ? "lorem" : ""}">
        <div class="doc-name"><strong><em>${esc(d.name)}</em></strong><span>${esc(snap.kind === "anonymize" && d.stage === "done" ? "twin built, click to open" : d.source)}</span></div>
        <div>${d.error ? chip("error") : d.status ? chip(d.status) : ""}</div>
        <div class="stage ${cls}">${esc(d.stage)}${d.message && d.stage !== "done" ? ` <span class="muted">· ${esc(d.message)}</span>` : ""}</div>
        <div class="muted small mono">${secs ? fmtSecs(secs) : ""}</div>
        <div></div></div>`;
    }).join("")}</div>`;
  }

  async function renderJob(id) {
    renderNav(null);
    let snap;
    try { snap = await api(`/api/jobs/${id}`); } catch (err) { view.innerHTML = `<div class="empty">${esc(err.message)}</div>`; return; }
    view.className = "page";
    const draw = (s) => {
      const done = s.docs.filter((d) => d.stage === "done" || d.stage === "error").length;
      const title = s.kind === "verify" ? (s.options.all ? "Rebuild all" : "Rebuild") : s.kind === "anonymize" ? "Lorem ipsum twin" : "Build";
      view.innerHTML = pageHead("Job", `${title} <span style="color:var(--muted)">${s.state === "done" ? "finished" : "in progress"}</span>`,
          `${s.docs.length} document${s.docs.length > 1 ? "s" : ""} · ${s.summary.PASS} pass, ${s.summary.WARN} warn, ${s.summary.FAIL} fail${s.summary.error ? `, ${s.summary.error} error` : ""}`,
          s.state === "done" ? `<a class="btn" href="#/">Documents</a><a class="btn primary" href="#/new">${I.plus}New build</a>` : "")
        + `<div class="progress" style="margin:-8px 0 14px"><i style="width:${(100 * done) / s.docs.length}%"></i></div>`
        + `<div class="card flush">${jobRows(s)}</div>`
        + (s.docs.some((d) => d.error) ? `<div class="card" style="margin-top:14px"><div class="card-title-row"><h2>Errors</h2></div>${s.docs.filter((d) => d.error).map((d) => `<div class="section-title">${esc(d.name)}</div><pre class="plain">${esc(d.error)}</pre>`).join("")}</div>` : "");
      view.querySelectorAll(".list-row.link").forEach((tr) => tr.addEventListener("click", () => (location.hash = `#/doc/${tr.dataset.id}${tr.dataset.tab ? "/" + tr.dataset.tab : ""}`)));
    };
    draw(snap);
    if (snap.state === "done") return;
    const es = new EventSource(`/api/jobs/${id}/events`);
    const refresh = () => api(`/api/jobs/${id}`).then((s) => { if (location.hash === `#/job/${id}`) draw(s); });
    es.addEventListener("doc", refresh);
    es.addEventListener("job", refresh);
    es.addEventListener("end", (e) => { es.close(); draw(JSON.parse(e.data)); });
    es.onerror = () => es.close();
    const tick = setInterval(() => { if (location.hash !== `#/job/${id}`) { clearInterval(tick); es.close(); } else if (snap.state !== "done") refresh(); }, 2000);
  }

  // ---------------------------------------------------------------- edge case dialog
  async function openEdgeCaseDialog(id) {
    if (!STATE) STATE = await api("/api/state");
    const ec = STATE.edge_cases || {};
    const modal = $("#modal");
    modal.hidden = false;
    modal.innerHTML = `<div class="dialog" role="dialog" aria-label="Add to edge cases" style="width:min(580px,100%)">
      <div class="head"><strong>Add to edge cases</strong><span style="flex:1"></span><button class="btn icon" id="close" title="Close">✕</button></div>
      <div class="fields" style="padding:16px 18px">
        <p class="muted small" style="margin:0">Copies only the lorem twin's Word file (not one word, number or image of the original) into <code>${esc(ec.dir || "")}</code> and commits it${ec.push ? `, then pushes it to <code>origin/${esc(ec.branch || "edge-cases")}</code>` : ""}. The name and the note are the only free text that travels with it: do not type the document's name, people, or anything from its content.</p>
        <div class="field"><label>Case name <span class="hint">lowercase letters, digits and dashes</span></label><input id="ec-slug" value="${esc(ec.default_slug || "")}" spellcheck="false" autocomplete="off"></div>
        <div class="field"><label>What is wrong <span class="hint">in neutral words, e.g. “header row of the second table is dropped”</span></label><textarea id="ec-note" rows="3"></textarea></div>
      </div>
      <div class="foot"><span class="muted small" id="ec-msg"></span><span style="flex:1"></span><button class="btn" id="cancel">Cancel</button><button class="btn primary" id="ec-send">Add to edge cases</button></div></div>`;
    const close = () => { modal.hidden = true; modal.innerHTML = ""; };
    $("#close", modal).onclick = $("#cancel", modal).onclick = close;
    modal.onclick = (e) => { if (e.target === modal) close(); };
    $("#ec-send", modal).onclick = async () => {
      const btn = $("#ec-send", modal);
      btn.disabled = true; $("#ec-msg", modal).textContent = ec.push ? "Committing and pushing…" : "Committing…";
      try {
        const r = await postJSON(`/api/docs/${id}/edge-case`, { slug: $("#ec-slug", modal).value, note: $("#ec-note", modal).value });
        close(); toast(`Edge case ${r.slug}: ${r.message}`); renderDoc(id, "lorem");
      } catch (err) { $("#ec-msg", modal).textContent = err.message; btn.disabled = false; }
    };
    $("#ec-slug", modal).focus();
  }

  // ---------------------------------------------------------------- document view
  const HUNK = /^\[(\w+)\] …(.*?) «(.*)» → «(.*)» (.*?)…$/s;
  function detailHTML(d) {
    const m = HUNK.exec(d);
    if (!m) return `<li>${esc(d)}</li>`;
    return `<li class="hunk"><span class="tag">${esc(m[1])}</span><span class="ctx">${esc(m[2])}</span> <del>${esc(m[3])}</del> <ins>${esc(m[4])}</ins> <span class="ctx">${esc(m[5])}</span></li>`;
  }
  function checkHTML(c) {
    const open = c.status !== "PASS" && c.details.length;
    return `<details class="check" ${open ? "open" : ""}><summary>${chip(c.status)}<span class="name">${esc(human(c.name))}</span><span class="sum">${esc(c.summary)}</span>${c.details.length ? `<span class="chev">+</span>` : ""}</summary>
      ${c.details.length ? `<div class="details"><ul>${c.details.map(detailHTML).join("")}</ul></div>` : ""}</details>`;
  }
  function infoHTML(info, state) {
    const s = info.structure || {};
    const stats = [["headings", Object.values(s.headings || {}).reduce((a, b) => a + b, 0)], ["tables", s.tables], ["table cells", s.table_cells], ["images", s.images], ["list items", s.list_items], ["footnotes", s.footnotes], ["links", s.links]]
      .filter(([, v]) => v !== undefined).map(([k, v]) => `<span class="stat"><b>${v}</b>${k}</span>`).join("");
    const kv = [["Title", info.title], ["Converted from", info.converted_from], ["Cover variant", state?.cover], ["Department name", state?.department_name], ["Cover number", state?.number], ["Version", state?.version], ["Note", info.note]]
      .filter(([, v]) => v).map(([k, v]) => `<dt>${k}</dt><dd>${esc(v)}</dd>`).join("");
    const list = (title, items, fmt = esc) => items && items.length ? `<div class="section-title">${title}</div><ul class="small" style="margin:0;padding-left:16px">${items.map((x) => `<li>${fmt(x)}</li>`).join("")}</ul>` : "";
    return `<div class="card">
      <div class="section-title">Source document</div><dl class="kv">${kv}</dl>
      <div class="section-title">Structure</div><div class="stats">${stats}</div>
      ${info.tracked_changes ? list("Tracked changes accepted", [...(info.tracked_changes.insertions || []).map((t) => `inserted: «${t}»`), ...(info.tracked_changes.deletions || []).map((t) => `deleted: «${t}»`)]) : ""}
      ${list("Comments dropped by design", info.comments)}
      ${list("Header and footer text dropped by design", info.dropped_headers_footers)}
      ${list("Paragraphs dropped by design", info.dropped_paragraphs)}
      ${info.headings && info.headings.length ? `<div class="section-title">Headings</div><div style="overflow:auto"><table class="minitable"><thead><tr><th>kind</th><th>typed</th><th>level</th><th>regenerated</th><th>text</th></tr></thead><tbody>${info.headings.map((h) => `<tr><td>${esc(h.kind)}</td><td class="mono">${esc(h.typed)}</td><td>${esc(h.level)}</td><td class="mono">${esc(h.expected)}</td><td>${esc(h.text)}</td></tr>`).join("")}</tbody></table></div>` : ""}
      ${list("SmartArt diagrams", info.smartart, (d) => `diagram ${d.index}: ${esc((d.text || []).join(" | "))}`)}
      ${info.tools ? `<div class="section-title">Tools</div><dl class="kv small">${Object.entries(info.tools).map(([k, v]) => `<dt>${esc(k)}</dt><dd class="mono" style="font-weight:400">${esc(v)}</dd>`).join("")}</dl>` : ""}
    </div>`;
  }
  function coverHTML(st) {
    const f = (name, label, hint = "") => `<div class="field"><label>${label}${hint ? ` <span class="hint">${hint}</span>` : ""}</label><input type="text" name="${name}" value="${esc(st[name] || "")}"></div>`;
    return `<div class="card"><div class="card-title-row"><h2>Cover &amp; metadata</h2></div>
      <form id="coverform" autocomplete="off">
        <div class="two-col">${variantSelect(st.cover || "", "theme default")}${f("department_name", "Department name", "label above the title and name under the symbol")}${f("title", "Title")}${f("number", "Cover number")}${f("version", "Version", "e.g. V1, MAR 2026")}${f("department", "Department symbol", "e.g. IT: footer prefix, big glyph")}</div>
        <div class="form-foot"><button class="btn primary" type="submit" id="coversave">Save &amp; rebuild</button><span class="muted small">Empty fields fall back to the theme defaults. Content is untouched; every check runs again, and the lorem twin gets lorem-ised copies of the new values.</span></div>
      </form></div>`;
  }

  // Two PDFs side by side; the iframes get their src only when the tab is first shown.
  function compareHTML(left, right) {
    const pane = (p) => `<div class="pane"><div class="pane-head"><strong>${esc(p.title)}</strong><span>${esc(p.sub || "")}</span>${p.url ? `<a class="row-action" href="${p.url}" target="_blank" rel="noopener" title="Open in a new tab">${I.open}</a>` : ""}</div>
      ${p.url ? `<iframe data-src="${p.url}#view=FitH&navpanes=0" title="${esc(p.title)}"></iframe>` : `<div class="nopdf">${p.missing || "No PDF."}</div>`}</div>`;
    return `<div class="compare">${pane(left)}${pane(right)}</div>`;
  }

  const DOC_TABS = [["live", "Live"], ["lorem", "Lorem"], ["cover", "Cover &amp; metadata"], ["report", "Report"]];
  async function renderDoc(id, tab) {
    renderNav("home");
    let doc;
    try { doc = await api(`/api/docs/${id}`); } catch (err) { view.innerHTML = `<div class="empty">${esc(err.message)}</div>`; return; }
    const r = doc.report;
    const checks = (r?.checks || []).slice().sort((a, b) => CHECK_ORDER[a.status] - CHECK_ORDER[b.status]);
    const file = (rel) => (rel ? `/files/${id}/${rel.split("/").map(encodeURIComponent).join("/")}` : null);
    const pdfURL = file(doc.pdf);
    const twin = doc.lorem;
    const twinCheck = (r?.checks || []).find((c) => c.name === "lorem_twin");
    if (!DOC_TABS.some(([k]) => k === tab)) tab = "live";
    view.className = "page wide";
    view.innerHTML = pageHead(doc.anonymized ? "Lorem ipsum twin" : "Document",
        `${esc(doc.name)} ${chip(doc.status)}${doc.anonymized ? chip("lorem", "lorem ipsum") : ""}`,
        doc.anonymized ? "Same structure and design, every word, number and image replaced. Verified against the original: safe to share." : `${esc(doc.source)} · built ${esc(fmtTime(doc.generated_at))}`,
        `${pdfURL ? `<a class="btn" href="${pdfURL}" target="_blank" rel="noopener">${I.open}Open PDF</a>` : ""}
         <button class="btn primary" id="reverify" title="Recompile the PDF (and the lorem twin) with the current template and run every check again">Rebuild &amp; re-verify</button>
         <button class="btn icon danger" id="delete" title="Delete this output folder">${I.trash}</button>`)
      + `<div class="tabs" role="tablist">${DOC_TABS.map(([k, l]) => `<button role="tab" class="tab ${k === tab ? "active" : ""}" data-tab="${k}">${l}${k === "lorem" && twin ? chip(twin.status) : ""}${k === "report" && r ? `<span class="count">${checks.filter((c) => c.status !== "PASS").length || ""}</span>` : ""}</button>`).join("")}</div>`
      + `<section class="tabbody" data-tab="live" ${tab === "live" ? "" : "hidden"}>${compareHTML(
            { title: "Original", sub: "the Word file as LibreOffice renders it", url: file(doc.original_pdf), missing: "No original.pdf: LibreOffice was not found at build time. Install it and rebuild." },
            { title: "Generated", sub: "styled through the template", url: pdfURL, missing: "No PDF: the render failed. See the report." })}</section>`
      + `<section class="tabbody" data-tab="lorem" ${tab === "lorem" ? "" : "hidden"}>
          ${doc.anonymized ? `<div class="card empty">This document is itself a lorem ipsum twin.</div>`
            : `<div class="tabnote">${twin ? `${chip(twin.status)}<span class="note">${esc(twinCheck?.summary || "Lorem ipsum twin: same structure and design, no real data.")}</span>` : `${chip("WARN", "missing")}<span class="note">${esc(twinCheck?.summary || "No lorem twin has been built for this document yet.")}</span>`}
              <button class="btn small ${twin ? "" : "primary"}" id="anonymize" title="Build the twin again: every word, number and image replaced, verified to contain no word of the original">${twin ? "Rebuild twin" : "Build twin"}</button>
              <button class="btn small" id="edgecase" ${twin && !doc.state?.edge_case ? "" : "disabled"} title="Copy the twin's Word file into the code repository's tests/fixtures/edge-cases/ so the developers can reproduce and fix this case without ever seeing the document">${doc.state?.edge_case ? `Forwarded as ${esc(doc.state.edge_case)}` : "Add to edge cases"}</button></div>`
            + compareHTML(
              { title: "Lorem original", sub: "the anonymised Word file as LibreOffice renders it", url: file(twin?.original_pdf), missing: twin ? "No lorem/original.pdf: LibreOffice was not found at build time." : "No twin yet." },
              { title: "Lorem generated", sub: "the anonymised file styled through the template", url: file(twin?.pdf), missing: twin ? "No PDF: the twin's render failed." : "No twin yet." })}</section>`
      + `<section class="tabbody" data-tab="cover" ${tab === "cover" ? "" : "hidden"}><div class="split">${coverHTML(doc.state || {})}
          <div class="pdfpane">${pdfURL ? `<iframe data-src="${pdfURL}#page=1&view=Fit&navpanes=0" title="Cover"></iframe>` : `<div class="nopdf">No PDF: the render failed. See the report.</div>`}</div></div></section>`
      + `<section class="tabbody" data-tab="report" ${tab === "report" ? "" : "hidden"}><div class="report">${r ? `${checks.map(checkHTML).join("")}<div style="height:6px"></div>${infoHTML(r.info || {}, doc.state)}` : `<div class="card empty">No report yet.</div>`}</div></section>`;

    const showTab = (k) => {
      view.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === k));
      view.querySelectorAll(".tabbody").forEach((sec) => {
        sec.hidden = sec.dataset.tab !== k;
        if (!sec.hidden) sec.querySelectorAll("iframe[data-src]").forEach((f) => { f.src = f.dataset.src; f.removeAttribute("data-src"); });
      });
      const h = `#/doc/${id}${k === "live" ? "" : "/" + k}`;
      if (location.hash !== h) history.replaceState(null, "", h);
    };
    view.querySelectorAll(".tab").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
    showTab(tab);

    $("#reverify").addEventListener("click", () => startVerify(id));
    $("#delete").addEventListener("click", async () => { if (await deleteDoc(id, doc.name)) location.hash = "#/"; });
    $("#edgecase")?.addEventListener("click", () => openEdgeCaseDialog(id));
    $("#anonymize")?.addEventListener("click", async () => {
      $("#anonymize").disabled = true;
      try { const job = await postJSON(`/api/docs/${id}/anonymize`, {}); watchTopbar(job.id); toast("Building the lorem ipsum twin"); followJob(job.id, id); }
      catch (err) { toast(err.message, true); $("#anonymize").disabled = false; }
    });
    const form = $("#coverform");
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fields = Object.fromEntries(new FormData(form).entries());
      $("#coversave").disabled = true;
      try {
        const job = await postJSON(`/api/docs/${id}/cover`, fields);
        watchTopbar(job.id);
        toast("Rebuilding with the new cover data");
        followJob(job.id, id);
      } catch (err) { toast(err.message, true); $("#coversave").disabled = false; }
    });
  }

  // Re-render the document page (keeping its tab) when a job on it finishes.
  function followJob(jobId, docId) {
    const es = new EventSource(`/api/jobs/${jobId}/events`);
    es.addEventListener("end", () => { es.close(); if (location.hash.startsWith(`#/doc/${docId}`)) route(); });
    es.onerror = () => es.close();
  }

  async function startVerify(id) {
    try {
      const job = await postJSON(`/api/docs/${id}/verify`, {});
      watchTopbar(job.id);
      toast("Rebuilding with the current template");
      followJob(job.id, id);
    } catch (err) { toast(err.message, true); }
  }

  // ---------------------------------------------------------------- router
  async function quit() {
    if (!confirm("Stop the local Anti-Rdiim server? Running jobs will be interrupted.")) return;
    try { await postJSON("/api/quit", {}); } catch (_) { /* server is gone */ }
    document.body.innerHTML = `<div class="empty" style="padding-top:22vh"><div class="eyebrow">Anti-Rdiim</div><h1 style="font-size:40px;letter-spacing:-.05em;color:var(--ink)">Stopped</h1><p style="margin-top:10px">Run <code>uv run wordeasy ui</code> to start it again.</p></div>`;
  }
  async function route() {
    const h = location.hash || "#/";
    try {
      if (!STATE) { STATE = await api("/api/state"); $("#side-host").textContent = location.host; }
      let m;
      if (h === "#/") await renderHome();
      else if (h === "#/new") renderNew();
      else if ((m = /^#\/template(?:\/([^/]+))?$/.exec(h))) await renderTemplate(m[1] ? decodeURIComponent(m[1]) : "");
      else if ((m = /^#\/job\/([\w-]+)$/.exec(h))) await renderJob(m[1]);
      else if ((m = /^#\/doc\/([\w-]+)(?:\/(\w+))?$/.exec(h))) await renderDoc(m[1], m[2] || "live");
      else location.hash = "#/";
    } catch (err) {
      view.innerHTML = `<div class="empty">${esc(err.message)}<br><span class="small">Is the Anti-Rdiim server still running?</span></div>`;
    }
    window.scrollTo(0, 0);
  }
  window.addEventListener("hashchange", route);
  $("#quit").addEventListener("click", quit);
  route();
})();
