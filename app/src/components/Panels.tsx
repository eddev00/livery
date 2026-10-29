import { useEffect, useState } from "react";
import type { Check, DesignInfo, DocumentInfo, ProjectStats, ProjectSummary, Proposal, Report, Selection, Status } from "../types";
import { pageText, statusLine, verdict, type Spot } from "../checks";

// ------------------------------------------------------------------ project tree
// Paradev-style sidebar: compact project rows with a letter avatar (a chevron on hover) and a
// "+" to import; the project's documents are indented rows underneath with a status dot.
const AVATAR_COLORS = ["#e07850", "#50a878", "#d4a84b", "#7b68ee", "#dc6b6b", "#4fa3c7"];

function avatarColor(id: string): string {
  let h = 0;
  for (const ch of id) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return AVATAR_COLORS[h % AVATAR_COLORS.length];
}

export function ProjectTree({ projects, current, currentDoc, docsOf, expanded, onToggle, onOpenProject, onOpenDoc,
  onImport, onNewProject, onDeleteProject, onExportProject, onExportDoc, onRemoveDoc, onReplaceDoc, onToggleDone }: {
  projects: ProjectSummary[]; current: string | null; currentDoc: string | null;
  docsOf: (projectId: string) => DocumentInfo[] | undefined; expanded: Set<string>;
  onToggle: (projectId: string) => void; onOpenProject: (projectId: string) => void;
  onOpenDoc: (projectId: string, docId: string) => void; onImport: (projectId: string) => void;
  onNewProject: () => void; onDeleteProject: (projectId: string) => void;
  onExportProject: (projectId: string) => void; onExportDoc: (projectId: string, d: DocumentInfo) => void;
  onRemoveDoc: (d: DocumentInfo) => void; onReplaceDoc: (d: DocumentInfo) => void;
  onToggleDone: (projectId: string, d: DocumentInfo) => void;
}) {
  const [menu, setMenu] = useState<{ x: number; y: number; project: ProjectSummary } | null>(null);
  useEffect(() => {
    if (!menu) return;
    const close = () => setMenu(null);
    window.addEventListener("click", close);
    window.addEventListener("keydown", close);
    return () => { window.removeEventListener("click", close); window.removeEventListener("keydown", close); };
  }, [menu]);

  return (
    <section className="tree">
      <div className="group-head">
        <span>Projects</span>
        <button className="group-add" aria-label="New project" title="New project" onClick={onNewProject}>+</button>
      </div>
      {projects.length === 0 && <p className="tree-empty">No projects yet.</p>}
      {projects.map((p) => {
        const open = expanded.has(p.id);
        const docs = docsOf(p.id);
        return (
          <div key={p.id} className="tree-project">
            <div className={`prow ${p.id === current ? "on" : ""}`} role="button" tabIndex={0}
              aria-expanded={open}
              onClick={() => { if (p.id !== current) onOpenProject(p.id); onToggle(p.id); }}
              onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onToggle(p.id); } }}
              onContextMenu={(e) => { e.preventDefault(); setMenu({ x: e.clientX, y: e.clientY, project: p }); }}>
              <span className="pthumb-slot">
                <span className="pthumb" style={{ background: `${avatarColor(p.id)}26`, borderColor: `${avatarColor(p.id)}99`, color: avatarColor(p.id) }}>
                  {p.name.charAt(0).toUpperCase()}
                </span>
                <span className={`pchev ${open ? "open" : ""}`} aria-hidden>›</span>
              </span>
              <span className="pname" title={p.name}>{p.name}</span>
              {p.health.failing > 0 && <span className="pbadge" title={`${p.health.failing} failing`}>{p.health.failing}</span>}
              <button className="padd" aria-label={`Export ${p.name} as PDFs`} title="Export PDFs…"
                disabled={docs !== undefined && docs.length === 0}
                onClick={(e) => { e.stopPropagation(); onExportProject(p.id); }}>⤓</button>
              <button className="padd" aria-label={`Import documents into ${p.name}`} title="Import documents"
                onClick={(e) => { e.stopPropagation(); onImport(p.id); }}>+</button>
            </div>
            {open && (
              <div className="tree-docs">
                {docs === undefined && <div className="drow muted">Loading…</div>}
                {docs && docs.length === 0 && (
                  <button className="drow drow-empty" onClick={() => onImport(p.id)}>Import Word documents…</button>
                )}
                {docs?.map((d) => {
                  const st = docState(d);
                  return (
                    <div key={d.id} className={`drow ${p.id === current && d.id === currentDoc ? "on" : ""}`} role="button" tabIndex={0}
                      onClick={() => onOpenDoc(p.id, d.id)}
                      onKeyDown={(e) => { if (e.key === "Enter") onOpenDoc(p.id, d.id); }}
                      title={st.detail ?? `${d.name} — ${st.label}`}>
                      <span className={`ddot d-${st.cls}`} aria-label={st.label} />
                      <span className="dname">{d.name.replace(/\.docx$/i, "")}</span>
                      {st.cls !== "PASS" && <span className={`dstate d-${st.cls}`}>{st.label}</span>}
                      <span className="dactions">
                        <button aria-label={d.review?.state === "open" ? `Mark ${d.name} done` : `Mark ${d.name} not done`}
                          title={d.review?.state === "open" ? "Mark done" : "Mark not done"}
                          disabled={d.review?.state === "open" && (st.cls === "BUSY" || st.cls === "FAIL")}
                          onClick={(e) => { e.stopPropagation(); onToggleDone(p.id, d); }}>{d.review?.state === "open" ? "✓" : "↩"}</button>
                        <button aria-label={`Export ${d.name} as PDF`} title="Export PDF…" disabled={st.cls === "BUSY" || st.cls === "FAIL"}
                          onClick={(e) => { e.stopPropagation(); onExportDoc(p.id, d); }}>⤓</button>
                        <button aria-label={`Replace ${d.name}`} title="Replace with a new version"
                          onClick={(e) => { e.stopPropagation(); onReplaceDoc(d); }}>⟳</button>
                        <button aria-label={`Remove ${d.name}`} title="Remove from project"
                          onClick={(e) => { e.stopPropagation(); onRemoveDoc(d); }}>×</button>
                      </span>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        );
      })}
      {menu && (
        <div className="menu" style={{ left: menu.x, top: menu.y }} role="menu">
          <button role="menuitem" onClick={() => onImport(menu.project.id)}>Import documents…</button>
          <button role="menuitem" onClick={() => onExportProject(menu.project.id)}>Export PDFs…</button>
          <button role="menuitem" className="danger" onClick={() => onDeleteProject(menu.project.id)}>Delete project…</button>
        </div>
      )}
    </section>
  );
}

function docState(d: DocumentInfo): { label: string; cls: string; detail?: string } {
  const v = d.version;
  const r = d.review;
  if (v.status === "failed") return { label: "import failed", cls: "FAIL", detail: v.error };
  if (v.status !== "imported") return { label: v.status === "importing" ? "importing" : "queued", cls: "BUSY" };
  if (!d.build || d.build.state === "queued" || d.build.state === "running") return { label: "rendering", cls: "BUSY" };
  if (d.build.state === "failed") return { label: "render failed", cls: "FAIL", detail: d.build.error };
  const short: Record<Status, string> = { PASS: "ready", WARN: "check", FAIL: "fix needed" };
  const s = d.report_status ?? "PASS";
  if (r?.state === "recheck") return { label: "re-check", cls: "RECHECK", detail: `${d.name} — ${recheckText(r)}` };
  if (r?.state === "done" || r?.state === "checking") return { label: "done", cls: "DONE", detail: `${d.name} — marked done` };
  return { label: short[s], cls: s, detail: `${d.name} — ${statusLine(s)}` };
}

/** Why a document marked done needs another look, in one sentence. */
export function recheckText(r: DocumentInfo["review"]): string {
  if (r.reason === "version") return "a new version of the Word file was imported after you marked it done.";
  if (r.reason === "unavailable") return "the design changed since you marked it done.";
  const pages = r.pages?.length ? pageText(r.pages) : "its pages";
  const by = r.changes?.length ? ` after ${r.changes.length === 1 ? `“${r.changes[0].note || r.changes[0].kind}”` : `${r.changes.length} design changes`}` : "";
  return `${pages[0].toUpperCase()}${pages.slice(1)} look different${by}.`;
}

// ------------------------------------------------------------------ statistics
export function Stats({ stats }: { stats: ProjectStats | null }) {
  if (!stats || stats.documents === 0) return null;
  const { review: r, verification: v } = stats;
  const done = r.done + r.checking;
  const flagged = stats.rules.filter((x) => x.outside.length > 0);
  return (
    <section className="panel">
      <div className="panel-head"><h3>Progress <span className="count">{done}/{stats.documents} done</span></h3></div>
      <div className="pad stats">
        <div className="stats-bar" role="img" aria-label={`${done} of ${stats.documents} documents done`}>
          <span className="sb-done" style={{ flexGrow: done }} />
          <span className="sb-recheck" style={{ flexGrow: r.recheck }} />
          <span className="sb-open" style={{ flexGrow: r.open }} />
        </div>
        <ul className="stats-list">
          {r.recheck > 0 && <li className="warn-text">{r.recheck} to re-check: {stats.recheck.map((x) => x.name.replace(/\.docx$/i, "")).join(", ")}</li>}
          {r.open > 0 && <li>{r.open} still open</li>}
          {v.FAIL > 0 && <li className="bad-text">{v.FAIL} failing verification</li>}
          {v.WARN > 0 && <li>{v.WARN} with warnings</li>}
          {v.working > 0 && <li className="muted">{v.working} rendering</li>}
          {stats.rules.length > 0 && <li className="muted">{stats.rules.length} import rule{stats.rules.length === 1 ? "" : "s"}</li>}
          {stats.unused_rules.length > 0 && <li className="warn-text" title="These rules match nothing in any document">
            Unused: {stats.unused_rules.join(", ")}</li>}
          {flagged.length > 0 && <li className="warn-text" title="These rules matched more or fewer blocks than expected in some documents">
            Off expectation: {flagged.map((x) => `${x.name} (${x.outside.length} doc${x.outside.length === 1 ? "" : "s"})`).join(", ")}</li>}
        </ul>
      </div>
    </section>
  );
}

// ------------------------------------------------------------------ treatments
export function Treatments({ design, selections, onFindMatches, onAssign }: {
  design: DesignInfo | null; selections: Selection[];
  onFindMatches: (s: Selection) => void; onAssign: (treatment: string, refs: Selection[]) => void;
}) {
  const [assignTo, setAssignTo] = useState("");
  if (!design) return null;
  const counts: Record<string, number> = {};
  for (const name of Object.values(design.assignments)) counts[name] = (counts[name] ?? 0) + 1;
  const table = selections.find((s) => s.kind === "table");
  const names = Object.keys(design.treatments);
  const fitting = names.filter((n) => selections.length > 0 && selections.every((s) => s.kind === design.treatments[n].kind));
  return (
    <section className="panel">
      <div className="panel-head"><h3>Treatments <span className="count">{names.length}</span></h3></div>
      {names.length === 0 && <p className="muted small pad">Reusable looks appear here once the assistant creates one and you
        accept it.</p>}
      <ul className="treatments">
        {names.map((n) => (
          <li key={n}>
            <span className="tr-name">{n}</span>
            <span className="muted small">{design.treatments[n].kind} · {counts[n] ?? 0} element{(counts[n] ?? 0) === 1 ? "" : "s"}</span>
          </li>
        ))}
      </ul>
      <div className="pad stack">
        <button className="btn small" disabled={!table} onClick={() => table && onFindMatches(table)}
          title={table ? "" : "Select a table in the preview first"}>
          Find matching tables…
        </button>
        {fitting.length > 0 && (
          <div className="row">
            <select value={assignTo} onChange={(e) => setAssignTo(e.target.value)} aria-label="Treatment to assign">
              <option value="">Assign to selection…</option>
              {fitting.map((n) => <option key={n}>{n}</option>)}
            </select>
            <button className="btn small" disabled={!assignTo} onClick={() => { onAssign(assignTo, selections); setAssignTo(""); }}>
              Propose
            </button>
          </div>
        )}
      </div>
    </section>
  );
}

// ------------------------------------------------------------------ history
export function History({ design, onUndo, onRemove }: {
  design: DesignInfo | null; onUndo: () => void; onRemove: (rev: string) => void;
}) {
  if (!design) return null;
  const latest = design.history[design.history.length - 1];
  const changes = [...design.changes].reverse();
  return (
    <section className="panel">
      <div className="panel-head">
        <h3>Design <span className="count">{changes.length}</span></h3>
        <button className="btn small" disabled={!design.can_undo} onClick={onUndo}
          title="Go back to the design before the latest change">Undo</button>
      </div>
      {changes.length === 0 && <p className="muted small pad">Accepted changes appear here. Any of them can be removed later.</p>}
      <ul className="treatments changes">
        {changes.map((c) => (
          <li key={c.id} className={c.reverted_by ? "removed" : ""}>
            <div className="change-head">
              <span className="tr-name">{c.note || c.kind}</span>
              {!c.reverted_by && (
                <button className="link small" onClick={() => onRemove(c.id)}
                  title={c.id === latest ? "Undo this change" : "Propose the current design without this change"}>Remove</button>
              )}
            </div>
            <span className="muted small">
              {c.id} · {c.kind} · {c.created.slice(0, 16).replace("T", " ")}
              {c.reverted_by && <> · removed by {c.reverted_by}</>}
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}

// ------------------------------------------------------------------ proposal
export function ProposalBar({ proposal, docs, agentNames, onRender, onAccept, onDiscard, onView }: {
  proposal: Proposal; docs: DocumentInfo[]; agentNames: Record<string, string>;
  onRender: () => void; onAccept: () => void; onDiscard: () => void;
  onView: (mode: "proposal" | "compare" | "synthetic") => void;
}) {
  const name = (id: string) => docs.find((d) => d.id === id)?.name ?? id;
  const impact = Object.entries(proposal.done_impact ?? {});
  const touched = impact.filter(([, i]) => i.state === "changed");
  const unknown = impact.filter(([, i]) => i.state === "pending");
  const states = Object.entries(proposal.real);
  const done = states.filter(([, s]) => s.state === "done").length;
  const failed = states.filter(([, s]) => "report_status" in s && s.report_status === "FAIL");
  const running = states.some(([, s]) => s.state === "running" || s.state === "queued");
  const ready = done === states.length && states.length > 0;
  const who = proposal.origin.startsWith("agent:") ? agentNames[proposal.origin.slice(6)] ?? proposal.origin.slice(6)
    : proposal.origin === "batch" ? "treatment matching" : proposal.origin;
  const scope = proposal.scope.scope === "global"
    ? `whole design (${proposal.scope.files.join(", ")}) — ${proposal.documents.length} document${proposal.documents.length === 1 ? "" : "s"}`
    : `${proposal.scope.refs.length} element${proposal.scope.refs.length === 1 ? "" : "s"} in ${proposal.documents.length} document${proposal.documents.length === 1 ? "" : "s"}`;
  return (
    <div className={`proposal ${proposal.stale ? "stale" : ""}`}>
      <div className="proposal-text">
        <strong>Proposal</strong> from {who} · based on {proposal.base} · {scope}
        {proposal.note && <div className="muted small proposal-note" title={proposal.note}>“{proposal.note}”</div>}
        {proposal.stale && <div className="bad-text small">The accepted design changed since this proposal was made. It can no
          longer be accepted; ask the assistant again or discard it.</div>}
        {proposal.scope.mapping && (
          <div className="warn-text small">
            Changes how your Word files are read in: every document is imported again.
            {(proposal.remap?.dropped.length ?? 0) > 0 && <> {proposal.remap!.dropped.length} styled
              element{proposal.remap!.dropped.length === 1 ? "" : "s"} no longer exist{proposal.remap!.dropped.length === 1 ? "s" : ""} and
              will lose {proposal.remap!.dropped.length === 1 ? "its" : "their"} styling if you accept.</>}
            {running && " Checking which styled elements carry over…"}
          </div>
        )}
        {touched.length > 0 && <div className="warn-text small">
          Changes {touched.length === 1 ? "a document" : `${touched.length} documents`} you marked done:{" "}
          {touched.map(([d, i]) => `${name(d)}${i.pages?.length ? ` (${pageText(i.pages)})` : ""}`).join("; ")}.
          {" "}{touched.length === 1 ? "It" : "They"} will be flagged for a re-check if you accept.</div>}
        {unknown.length > 0 && <div className="muted small">
          Touches {unknown.length} document{unknown.length === 1 ? "" : "s"} you marked done; render with real content to see whether
          {unknown.length === 1 ? " it changes" : " they change"}.</div>}
        {failed.length > 0 && <div className="warn-text small">Verification failed for {failed.map(([d]) => name(d)).join(", ")}. You can
          still accept; check {failed.length === 1 ? "it" : "them"} before exporting.</div>}
        {proposal.synthetic && !proposal.synthetic.ok && <div className="bad-text small">Synthetic preview: {proposal.synthetic.problems.join("; ") || "render errors"}</div>}
      </div>
      <div className="proposal-actions">
        <div className="segmented small">
          <button onClick={() => onView("synthetic")} disabled={!proposal.synthetic}>Synthetic</button>
          <button onClick={() => onView("proposal")} disabled={done === 0}>Real</button>
          <button onClick={() => onView("compare")} disabled={done === 0}>Compare</button>
        </div>
        <button className="btn" onClick={onRender} disabled={running || proposal.stale}>
          {running ? `Rendering ${done}/${states.length}…` : done ? "Render again" : "Render with real content"}
        </button>
        <button className="btn" onClick={onDiscard}>Discard</button>
        <button className="btn primary" onClick={onAccept} disabled={!ready || proposal.stale}
          title={ready ? "" : "Render with real content first"}>
          Accept
        </button>
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ report
const ICON: Record<Status, string> = { PASS: "✓", WARN: "!", FAIL: "✕" };

/** The verdict for the document on screen: can it be exported, and what is worth a look.
 *  The engine's own check list stays one click away under "Technical details". */
export function ReportPanel({ report, onShow }: { report: Report | null; onShow: (s: Spot) => void }) {
  const [open, setOpen] = useState(false);
  const [tech, setTech] = useState(false);
  const [seen, setSeen] = useState<Record<string, number>>({});
  if (!report) return null;
  const v = verdict(report);
  const attention = v.findings.length > 0;
  const expanded = attention || open; // findings always show; a clean report opens on request
  return (
    <div className={`report report-${v.status}`}>
      <button className="report-head" onClick={() => !attention && setOpen(!open)} aria-expanded={expanded}>
        <span className={`verdict-icon ${v.status}`} aria-hidden="true">{ICON[v.status]}</span>
        <span className="verdict-title">{v.title}</span>
        {v.sub && <span className="verdict-sub">{v.sub}</span>}
        {!attention && <span className="verdict-toggle">{open ? "Hide" : "What we checked"}</span>}
      </button>
      {expanded && (
        <div className="report-body">
          {v.findings.map((f) => {
            const next = f.spots.length ? (seen[f.key] ?? 0) % f.spots.length : 0;
            return (
              <div key={f.key} className={`finding ${f.status}`}>
                <div className="finding-text">
                  <div className="finding-title">{f.title}</div>
                  {f.body && <div className="finding-body">{f.body}</div>}
                </div>
                {f.spots.length > 0 && (
                  <button className="btn small" onClick={() => {
                    onShow(f.spots[next]);
                    setSeen((m) => ({ ...m, [f.key]: next + 1 }));
                  }}>
                    {f.spots.length > 1 ? `Show me (${next + 1}/${f.spots.length})` : "Show me"}
                  </button>
                )}
              </div>
            );
          })}
          {v.checked.length > 0 && (
            <details className="checked" open={!attention}>
              <summary>{attention ? "What passed" : "What we checked"}</summary>
              <ul>{v.checked.map((t) => <li key={t}>{t}</li>)}</ul>
            </details>
          )}
          <button className="link small tech-toggle" onClick={() => setTech(!tech)}>
            {tech ? "Hide technical details" : "Technical details"}
          </button>
          {tech && report.checks.map((c: Check) => (
            <div key={c.name} className="check">
              <span className={`badge ${c.status}`}>{c.status}</span>
              <span className="check-name">{c.name.replaceAll("_", " ")}</span>
              <span className="small">{c.summary}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
