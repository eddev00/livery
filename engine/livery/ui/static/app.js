/* Anti-Rdiim local UI. Vanilla JS, hash routing, no build step. */
(() => {
  "use strict";
  const $ = (sel, root = document) => root.querySelector(sel);
  const view = $("#view");
  const CHECK_ORDER = { FAIL: 0, WARN: 1, PASS: 2, SKIP: 3 };
  let STATE = null;          // /api/state
  let pending = [];          // documents chosen for the next build: { path, meta: {cover fields} }
  let lastMeta = {};         // cover fields of the last submitted batch, offered to newly added rows
  let lastForm = null;       // remembered form values
  let live = null;           // EventSource for the job being watched in the top bar

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
    menu: `<svg viewBox="0 0 24 24" fill="none"><path d="M4 7h16M4 12h16M4 17h16"/></svg>`,
    minus: `<svg viewBox="0 0 24 24" fill="none"><path d="M5 12h14"/></svg>`,
    power: `<svg viewBox="0 0 24 24" fill="none"><path d="M12 3v9"/><path d="M6.3 7.3a8 8 0 1 0 11.4 0"/></svg>`,
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
    $("#topnav").innerHTML = NAV.map(([k, h, l, icon]) => `<a class="nav-item ${k === active ? "active" : ""}" href="${h}" title="${l}">${icon}<span>${l}</span></a>`).join("");
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

  // Cover fields every document carries on its own; the same keys the Cover tab edits later.
  const META = [["cover", "Variant"], ["department_name", "Department name"], ["department", "Symbol"], ["number", "Number"], ["title", "Title"], ["version", "Version"]];
  const metaHint = { department_name: "label above the title", department: "IT", number: "", title: "from the document", version: "V1" };

  function renderNew() {
    renderNav("new");
    const f = lastForm || {};
    const field = (name, label, hint = "", value = "") => `<div class="field"><label>${label}${hint ? ` <span class="hint">${hint}</span>` : ""}</label><input type="text" name="${name}" value="${esc(value)}"></div>`;
    view.className = "page";
    view.innerHTML = pageHead("Pipeline", "New build", "Each Word file becomes one styled PDF plus a report, with its own cover data. Any content drift between source and PDF is a FAIL.")
      + `<div class="card flush"><div class="card-title-row" style="padding:18px 18px 0"><h2>Documents</h2><div class="actions"><button class="btn small" id="copydown" title="Copy the first row's cover fields into every other row">Copy first row to all</button><button class="btn small" id="browse">${I.folder}Add files…</button></div></div>
          <div class="table-wrap" id="files"></div></div>
        <div class="card" style="margin-top:14px"><div class="card-title-row"><h2>Options</h2></div>
          <form id="form" autocomplete="off"><div class="fields">
            <div class="two-col">${field("out_root", "Output folder", "", f.out_root || STATE?.out_root || "")}
            <div class="muted small" style="align-self:end">Styled with <code>${esc((STATE?.template || "").replace(STATE.repo_root + "/", ""))}</code>. Edit the template, then use “Rebuild &amp; re-verify” on a document to see the change.</div></div>
            <label class="check-row"><input type="checkbox" name="accept_changes" ${f.accept_changes ? "checked" : ""}><div>Accept tracked changes <span>— otherwise unresolved changes are a FAIL</span></div></label>
            <label class="check-row"><input type="checkbox" name="clean" ${f.clean ? "checked" : ""}><div>Clean rebuild <span>— delete the document's output folder first</span></div></label>
            <div class="form-foot"><button class="btn primary" type="submit" id="run">Build</button><span class="muted small" id="runhint"></span></div>
          </div></form></div>`;
    const variants = STATE?.cover_variants || [];
    const cell = (i, k, v) => k === "cover"
      ? `<select data-i="${i}" data-k="cover"><option value="">theme default</option>${variants.map((o) => `<option value="${esc(o.name)}" ${o.name === v ? "selected" : ""}>${esc(o.name)}</option>`).join("")}</select>`
      : `<input type="text" data-i="${i}" data-k="${k}" value="${esc(v || "")}" placeholder="${esc(metaHint[k] || "")}">`;
    const renderFiles = () => {
      const box = $("#files");
      box.innerHTML = pending.length
        ? `<table class="doctable"><thead><tr><th>File</th>${META.map(([, l]) => `<th>${l}</th>`).join("")}<th></th></tr></thead><tbody>
            ${pending.map((d, i) => `<tr><td class="doc-name"><strong title="${esc(d.path)}"><em>${esc(basename(d.path))}</em></strong><span>${esc(dirname(d.path))}</span></td>
              ${META.map(([k]) => `<td>${cell(i, k, d.meta[k])}</td>`).join("")}
              <td><button class="row-action danger" data-remove="${i}" title="Remove from this batch">${I.trash}</button></td></tr>`).join("")}</tbody></table>`
        : `<div class="empty">No documents yet. Use Add files… to pick one or more Word documents; each gets its own row of cover data.</div>`;
      box.querySelectorAll("[data-remove]").forEach((b) => b.addEventListener("click", () => { pending.splice(+b.dataset.remove, 1); renderFiles(); }));
      box.querySelectorAll("[data-k]").forEach((el) => el.addEventListener("input", () => { pending[+el.dataset.i].meta[el.dataset.k] = el.value; }));
      $("#runhint").textContent = pending.length ? `${pending.length} document${pending.length > 1 ? "s" : ""} in this batch` : "";
      $("#copydown").disabled = pending.length < 2;
    };
    renderFiles();
    $("#browse").addEventListener("click", () => openBrowser((chosen) => {
      chosen.forEach((path) => !pending.some((d) => d.path === path) && pending.push({ path, meta: { ...lastMeta } }));
      renderFiles();
    }));
    $("#copydown").addEventListener("click", () => { const first = pending[0]?.meta || {}; pending.forEach((d, i) => { if (i) d.meta = { ...first }; }); renderFiles(); });
    $("#form").addEventListener("submit", async (e) => {
      e.preventDefault();
      if (!pending.length) { toast("Choose at least one Word file", true); return; }
      const fd = new FormData(e.target);
      const options = Object.fromEntries(fd.entries());
      options.accept_changes = fd.has("accept_changes"); options.clean = fd.has("clean");
      lastForm = options; lastMeta = { ...pending[0].meta };
      $("#run").disabled = true;
      try {
        const job = await postJSON("/api/jobs", { docs: pending.map((d) => ({ path: d.path, ...d.meta })), options });
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

  // ---------------------------------------------------------------- side-by-side viewer
  // Each pane renders its PDF as page images with an invisible, selectable word layer on top
  // (boxes from pdftotext). The server aligns the two word sequences once, so word i of one
  // pane maps to its counterpart in the other: selecting anything scrolls the other pane to the
  // transformed element and highlights it. Pages are rendered on demand as they scroll in.

  class PdfView {
    constructor(el, docId, rel) {
      this.el = el; this.docId = docId; this.rel = rel; this.words = []; this.partner = null; this.map = null; this.hits = [];
      this.zoom = 1; this.dpi = 120;
      this.status = el.closest(".pane").querySelector(".pane-status");
      this.zoomLabel = el.closest(".pane").querySelector(".zoom-level");
      this.ready = this.load().catch((err) => { el.innerHTML = `<div class="nopdf">${esc(err.message)}</div>`; throw err; });
    }
    async load() {
      const q = `file=${encodeURIComponent(this.rel)}`;
      const data = await api(`/api/docs/${this.docId}/pages?${q}`);
      const html = [];
      let i = 0;
      data.pages.forEach((pg, n) => {
        const spans = pg.words.map(([x0, y0, x1, y1, t]) => {
          this.words.push(t.trim()); const k = i++;
          return `<span data-i="${k}" style="left:${(x0 / pg.w * 100).toFixed(3)}%;top:${(y0 / pg.h * 100).toFixed(3)}%;width:${((x1 - x0) / pg.w * 100).toFixed(3)}%;height:${((y1 - y0) / pg.h * 100).toFixed(3)}%;font-size:${((y1 - y0) / pg.w * 100).toFixed(3)}cqw">${esc(t)}</span>`;
        }).join(" ");
        html.push(`<div class="pg" data-n="${n + 1}" style="aspect-ratio:${pg.w} / ${pg.h}"><img alt="page ${n + 1}" draggable="false"><div class="words">${spans}</div></div>`);
      });
      this.el.innerHTML = html.join("");
      this.pages = this.el.querySelectorAll(".pg");
      this.src = (n) => `/api/docs/${this.docId}/page?${q}&n=${n}&dpi=${this.dpi}`;
      const io = new IntersectionObserver((entries) => entries.forEach((e) => {
        if (!e.isIntersecting) return;
        const img = e.target.querySelector("img");
        if (!img.src) img.src = this.src(e.target.dataset.n);
        io.unobserve(e.target);
      }), { root: this.el, rootMargin: "150% 0px" });
      this.pages.forEach((pg) => io.observe(pg));
      this.el.addEventListener("scroll", () => this.showPage(), { passive: true });
      this.el.addEventListener("wheel", (e) => {
        if (!e.ctrlKey && !e.metaKey) return;
        e.preventDefault();
        this.setZoom(this.zoom * (e.deltaY < 0 ? 1.1 : 1 / 1.1), e);
      }, { passive: false });
      const pane = this.el.closest(".pane");
      pane.querySelector("[data-zoom='in']").addEventListener("click", () => this.setZoom(this.zoom * 1.25));
      pane.querySelector("[data-zoom='out']").addEventListener("click", () => this.setZoom(this.zoom / 1.25));
      pane.querySelector("[data-zoom='fit']").addEventListener("click", () => this.setZoom(1));
      this.el.pdfView = this;
      this.showPage();
    }
    // Zoom relative to the pane width (1 = fit width). Keeps the point under the cursor, or the
    // middle of the pane, in place; zoomed-in panes swap to sharper renders of the loaded pages.
    setZoom(z, at) {
      z = Math.min(4, Math.max(0.5, Math.round(z * 100) / 100));
      if (z === this.zoom) return;
      const box = this.el.getBoundingClientRect();
      const ax = at ? at.clientX - box.left : this.el.clientWidth / 2, ay = at ? at.clientY - box.top : this.el.clientHeight / 2;
      const ratio = z / this.zoom;
      const sx = this.el.scrollLeft, sy = this.el.scrollTop;
      this.zoom = z;
      this.el.style.setProperty("--zoom", z);
      this.el.scrollTo({ left: (sx + ax) * ratio - ax, top: (sy + ay) * ratio - ay });
      this.el.classList.toggle("zoomed", z > 1);
      if (this.zoomLabel) this.zoomLabel.textContent = `${Math.round(z * 100)}%`;
      const dpi = z > 1.4 ? 200 : 120;
      if (dpi !== this.dpi) { this.dpi = dpi; this.el.querySelectorAll("img[src]").forEach((img) => { img.src = this.src(img.closest(".pg").dataset.n); }); }
      this.showPage();
    }
    showPage() {
      if (!this.status) return;
      const top = this.el.getBoundingClientRect().top + this.el.clientHeight / 2;
      let cur = 1;
      for (const pg of this.pages) { if (pg.getBoundingClientRect().top <= top) cur = +pg.dataset.n; else break; }
      this.status.textContent = `${cur} / ${this.pages.length}`;
    }
    selectedRange() {
      const sel = window.getSelection();
      if (!sel || sel.isCollapsed || sel.rangeCount === 0) return null;
      const range = sel.getRangeAt(0);
      if (!this.el.contains(range.commonAncestorContainer)) return null;
      const spans = [...this.el.querySelectorAll(".words span")].filter((sp) => range.intersectsNode(sp));
      if (!spans.length) return null;
      return [+spans[0].dataset.i, +spans[spans.length - 1].dataset.i];
    }
    onSelect() {
      const r = this.selectedRange();
      if (!r || !this.partner || !this.map) return;
      // First and last selected words that have a counterpart; a selection made only of text the
      // other side lacks (a running header, a page number) falls back to the nearest mapped word.
      let j0 = -1, j1 = -1;
      for (let k = r[0]; k <= r[1] && j0 < 0; k++) j0 = this.map[k];
      for (let k = r[1]; k >= r[0] && j1 < 0; k--) j1 = this.map[k];
      if (j0 < 0) {
        let k = r[0];
        while (k >= 0 && this.map[k] < 0) k--;
        if (k < 0) { k = r[1]; while (k < this.map.length && this.map[k] < 0) k++; }
        if (k < 0 || k >= this.map.length) { this.partner.mark([]); this.partner.note("no counterpart"); return; }
        j0 = j1 = this.map[k];
        this.partner.show(j0, j1, "nearest mapped word: the selection has no counterpart");
        return;
      }
      const unmapped = [];
      for (let k = r[0]; k <= r[1]; k++) if (this.words[k] && this.map[k] < 0) unmapped.push(k);
      this.partner.show(j0, j1, unmapped.length ? `${unmapped.length} selected word${unmapped.length > 1 ? "s have" : " has"} no counterpart` : "");
    }
    // Highlight words j0..j1 and scroll them into the middle of the pane.
    show(j0, j1, msg) {
      const spans = [];
      for (let k = j0; k <= j1; k++) spans.push(this.el.querySelector(`.words span[data-i="${k}"]`));
      this.mark(spans);
      this.note(msg);
      const first = spans[0];
      if (!first) return;
      const y = first.getBoundingClientRect().top - this.el.getBoundingClientRect().top + this.el.scrollTop;
      this.el.scrollTo({ top: Math.max(0, y - this.el.clientHeight / 2), behavior: "smooth" });
    }
    mark(spans) { this.hits.forEach((sp) => sp.classList.remove("hit")); this.hits = spans.filter(Boolean); this.hits.forEach((sp) => sp.classList.add("hit")); }
    note(msg) { const n = this.el.closest(".pane").querySelector(".pane-note"); if (n) { n.textContent = msg; n.hidden = !msg; } }
  }

  // A drag may end outside the pane it started in, so selections are picked up at document level.
  const syncSelection = () => document.querySelectorAll(".pdfview").forEach((el) => el.pdfView?.onSelect());
  document.addEventListener("mouseup", () => setTimeout(syncSelection, 0));
  document.addEventListener("keyup", (e) => { if (e.shiftKey) syncSelection(); });

  function compareHTML(left, right) {
    const zoom = `<span class="zoom"><button class="row-action" data-zoom="out" title="Zoom out (Ctrl + wheel)">${I.minus}</button><button class="zoom-level" data-zoom="fit" title="Fit width">100%</button><button class="row-action" data-zoom="in" title="Zoom in (Ctrl + wheel)">${I.plus}</button></span>`;
    const pane = (p) => `<div class="pane"><div class="pane-head"><strong>${esc(p.title)}</strong><span>${esc(p.sub || "")}</span><em class="pane-note" hidden></em><b class="pane-status"></b>${p.rel ? zoom : ""}${p.url ? `<a class="row-action" href="${p.url}" target="_blank" rel="noopener" title="Open in a new tab">${I.open}</a>` : ""}</div>
      ${p.rel ? `<div class="pdfview" data-rel="${esc(p.rel)}"><div class="nopdf">Loading…</div></div>` : `<div class="nopdf">${p.missing || "No PDF."}</div>`}</div>`;
    return `<div class="compare">${pane(left)}${pane(right)}</div>`;
  }
  // Builds the two viewers of a compare block once, the first time its tab is shown, and links them.
  function mountCompare(sec, docId) {
    const els = [...sec.querySelectorAll(".pdfview[data-rel]:not([data-mounted])")];
    if (!els.length) return;
    const views = els.map((el) => { el.dataset.mounted = "1"; return new PdfView(el, docId, el.dataset.rel); });
    if (views.length !== 2) return;
    views[0].partner = views[1]; views[1].partner = views[0];
    Promise.all(views.map((v) => v.ready))
      .then(() => api(`/api/docs/${docId}/align?a=${encodeURIComponent(views[0].rel)}&b=${encodeURIComponent(views[1].rel)}`))
      .then((m) => { views[0].map = m.ab; views[1].map = m.ba; })
      .catch((err) => views.forEach((v) => v.note(`alignment failed: ${err.message}`)));
  }

  // The burger menu on document pages (the top bar is hidden there); closes on outside click / Esc.
  const toggleMenu = (open) => { const m = $("#menu-list"); if (!m) return; m.hidden = !open; $("#menu").setAttribute("aria-expanded", String(open)); };
  document.addEventListener("click", (e) => { const m = $("#menu-list"); if (m && !m.hidden && !m.contains(e.target)) toggleMenu(false); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") toggleMenu(false); });

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
    view.innerHTML = `<div class="page-head compact"><div class="doc-line"><span class="eyebrow">${doc.anonymized ? "Lorem ipsum twin" : "Document"}</span>
        <h1>${esc(doc.name)} ${chip(doc.status)}${doc.anonymized ? chip("lorem", "lorem ipsum") : ""}</h1>
        <p>${doc.anonymized ? "Same structure and design, every word, number and image replaced. Verified against the original: safe to share." : `built ${esc(fmtTime(doc.generated_at))}`}</p></div><div class="actions">
        ${pdfURL ? `<a class="btn" href="${pdfURL}" target="_blank" rel="noopener">${I.open}Open PDF</a>` : ""}
         <button class="btn primary" id="reverify" title="Recompile the PDF (and the lorem twin) with the current template and run every check again">Rebuild &amp; re-verify</button>
         <div class="menu-wrap"><button class="btn icon" id="menu" title="Menu" aria-haspopup="true" aria-expanded="false">${I.menu}</button>
           <div class="menu" id="menu-list" hidden>${NAV.map(([, h, l, icon]) => `<a class="nav-item" href="${h}">${icon}<span>${l}</span></a>`).join("")}
             <div class="menu-sep"></div><button class="nav-item" id="menu-quit" title="Stop the local server">${I.power}<span>Quit</span></button>
             <div class="menu-foot"><span class="dot"></span>Local only · <code>${esc(location.host)}</code></div></div></div></div></div>`
      + `<div class="tabs" role="tablist">${DOC_TABS.map(([k, l]) => `<button role="tab" class="tab ${k === tab ? "active" : ""}" data-tab="${k}">${l}${k === "lorem" && twin ? chip(twin.status) : ""}${k === "report" && r ? `<span class="count">${checks.filter((c) => c.status !== "PASS").length || ""}</span>` : ""}</button>`).join("")}</div>`
      + `<section class="tabbody" data-tab="live" ${tab === "live" ? "" : "hidden"}>${compareHTML(
            { title: "Original", sub: "the Word file as LibreOffice renders it", url: file(doc.original_pdf), rel: doc.original_pdf, missing: "No original.pdf: LibreOffice was not found at build time. Install it and rebuild." },
            { title: "Generated", sub: "styled through the template", url: pdfURL, rel: doc.pdf, missing: "No PDF: the render failed. See the report." })}</section>`
      + `<section class="tabbody" data-tab="lorem" ${tab === "lorem" ? "" : "hidden"}>
          ${doc.anonymized ? `<div class="card empty">This document is itself a lorem ipsum twin.</div>`
            : `<div class="tabnote">${twin ? `${chip(twin.status)}<span class="note">${esc(twinCheck?.summary || "Lorem ipsum twin: same structure and design, no real data.")}</span>` : `${chip("WARN", "missing")}<span class="note">${esc(twinCheck?.summary || "No lorem twin has been built for this document yet.")}</span>`}
              <button class="btn small ${twin ? "" : "primary"}" id="anonymize" title="Build the twin again: every word, number and image replaced, verified to contain no word of the original">${twin ? "Rebuild twin" : "Build twin"}</button>
              <button class="btn small" id="edgecase" ${twin && !doc.state?.edge_case ? "" : "disabled"} title="Copy the twin's Word file into the code repository's tests/fixtures/edge-cases/ so the developers can reproduce and fix this case without ever seeing the document">${doc.state?.edge_case ? `Forwarded as ${esc(doc.state.edge_case)}` : "Add to edge cases"}</button></div>`
            + compareHTML(
              { title: "Lorem original", sub: "the anonymised Word file as LibreOffice renders it", url: file(twin?.original_pdf), rel: twin?.original_pdf, missing: twin ? "No lorem/original.pdf: LibreOffice was not found at build time." : "No twin yet." },
              { title: "Lorem generated", sub: "the anonymised file styled through the template", url: file(twin?.pdf), rel: twin?.pdf, missing: twin ? "No PDF: the twin's render failed." : "No twin yet." })}</section>`
      + `<section class="tabbody" data-tab="cover" ${tab === "cover" ? "" : "hidden"}><div class="split">${coverHTML(doc.state || {})}
          <div class="pdfpane">${pdfURL ? `<iframe data-src="${pdfURL}#page=1&view=Fit&navpanes=0" title="Cover"></iframe>` : `<div class="nopdf">No PDF: the render failed. See the report.</div>`}</div></div></section>`
      + `<section class="tabbody" data-tab="report" ${tab === "report" ? "" : "hidden"}><div class="report">${r ? `${checks.map(checkHTML).join("")}<div style="height:6px"></div>${infoHTML(r.info || {}, doc.state)}` : `<div class="card empty">No report yet.</div>`}</div></section>`;

    const showTab = (k) => {
      view.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === k));
      view.querySelectorAll(".tabbody").forEach((sec) => {
        sec.hidden = sec.dataset.tab !== k;
        if (!sec.hidden) { sec.querySelectorAll("iframe[data-src]").forEach((f) => { f.src = f.dataset.src; f.removeAttribute("data-src"); }); mountCompare(sec, id); }
      });
      const h = `#/doc/${id}${k === "live" ? "" : "/" + k}`;
      if (location.hash !== h) history.replaceState(null, "", h);
    };
    view.querySelectorAll(".tab").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
    showTab(tab);

    $("#menu").addEventListener("click", (e) => { e.stopPropagation(); toggleMenu($("#menu-list").hidden); });
    $("#menu-quit").addEventListener("click", quit);
    $("#reverify").addEventListener("click", () => startVerify(id));
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
    document.body.innerHTML = `<div class="empty" style="padding-top:22vh"><div class="eyebrow">Anti-Rdiim</div><h1 style="font-size:40px;letter-spacing:-.05em;color:var(--ink)">Stopped</h1><p style="margin-top:10px">Run <code>uv run livery ui</code> to start it again.</p></div>`;
  }
  async function route() {
    const h = location.hash || "#/";
    document.body.classList.toggle("no-topbar", h.startsWith("#/doc/"));
    try {
      if (!STATE) { STATE = await api("/api/state"); $("#top-host").textContent = location.host; }
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
