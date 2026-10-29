import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError, bridge } from "./api";
import { Chat, COMMANDS } from "./components/Chat";
import { MAX_IMAGES, prepareImage } from "./images";
import { MatchDialog, SetupDialog, type Candidate } from "./components/Dialogs";
import { History, ProjectTree, ProposalBar, recheckText, ReportPanel, Stats, Treatments } from "./components/Panels";
import { PdfView } from "./components/PdfView";
import type { Spot } from "./checks";
import type {
  AgentStatus, ChatEntry, DesignInfo, DocumentInfo, Job, MapElement, ProjectStats, ProjectSummary, Proposal, RefImage, Report, Selection, SelectionMap,
} from "./types";

// "approved": a document marked done, as approved (left) next to the accepted design (right)
// "vs-original": the Word file as LibreOffice lays it out (left) next to the accepted design (right)
type View = "accepted" | "original" | "vs-original" | "synthetic" | "proposal" | "compare" | "approved";
type Toast = { id: number; text: string; bad?: boolean; action?: { label: string; run: () => void } };

const CHAT_DEFAULT = 390, CHAT_MIN = 300;
// Leave the left sidebar (320px) and a usable document pane.
const clampChat = (w: number) => Math.round(Math.min(Math.max(w, CHAT_MIN), Math.max(CHAT_MIN, window.innerWidth - 320 - 360)));

export function App() {
  const [engine, setEngine] = useState<{ ready: boolean; error: string | null }>({ ready: false, error: null });
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [projectId, setProjectId] = useState<string | null>(() => localStorage.getItem("we:project"));
  const [, setProjectName] = useState("");
  const [docs, setDocs] = useState<DocumentInfo[]>([]);
  const [docId, setDocId] = useState<string | null>(null);
  const [design, setDesign] = useState<DesignInfo | null>(null);
  const [proposals, setProposals] = useState<Proposal[]>([]);
  const [stats, setStats] = useState<ProjectStats | null>(null);
  const [view, setView] = useState<View>("accepted");
  const [zoom, setZoom] = useState<number | "fit">("fit");
  const [selections, setSelections] = useState<Selection[]>([]);
  const [images, setImages] = useState<RefImage[]>([]);
  const [agents, setAgents] = useState<AgentStatus[]>([]);
  const [provider, setProvider] = useState(() => localStorage.getItem("we:provider") ?? "claude");
  const [entries, setEntries] = useState<ChatEntry[]>([]);
  const [streaming, setStreaming] = useState("");
  const [busy, setBusy] = useState(false);
  const [phase, setPhase] = useState<string | null>(null);
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [setupOpen, setSetupOpen] = useState(false);
  const [matches, setMatches] = useState<{ signature: string; candidates: Candidate[] } | null>(null);
  const [newProject, setNewProject] = useState(false);
  const [sandboxOk, setSandboxOk] = useState(true);
  const [chatWidth, setChatWidth] = useState(() => clampChat(Number(localStorage.getItem("we:chatWidth")) || CHAT_DEFAULT));

  useEffect(() => { localStorage.setItem("we:chatWidth", String(chatWidth)); }, [chatWidth]);
  const [sidebarOpen, setSidebarOpen] = useState(() => localStorage.getItem("we:sidebar") !== "closed");
  useEffect(() => { localStorage.setItem("we:sidebar", sidebarOpen ? "open" : "closed"); }, [sidebarOpen]);
  useEffect(() => {
    const k = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && !e.shiftKey && !e.altKey && e.key.toLowerCase() === "b") { e.preventDefault(); setSidebarOpen((o) => !o); }
    };
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  }, []);
  useEffect(() => {
    const onResize = () => setChatWidth((w) => clampChat(w));
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  const startChatResize = (e: React.PointerEvent<HTMLDivElement>) => {
    e.preventDefault();
    const startX = e.clientX, startW = chatWidth;
    const move = (ev: PointerEvent) => setChatWidth(clampChat(startW + startX - ev.clientX));
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      document.body.classList.remove("resizing");
    };
    document.body.classList.add("resizing");
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  };
  const keyChatResize = (e: React.KeyboardEvent<HTMLDivElement>) => {
    const step = e.shiftKey ? 64 : 16;
    if (e.key === "ArrowLeft") setChatWidth((w) => clampChat(w + step));
    else if (e.key === "ArrowRight") setChatWidth((w) => clampChat(w - step));
    else return;
    e.preventDefault();
  };

  const toast = useCallback((text: string, bad = false, action?: Toast["action"]) => {
    const id = Date.now() + Math.random();
    setToasts((t) => [...t, { id, text, bad, action }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), action ? 12000 : 6000);
  }, []);
  const fail = useCallback((e: unknown) => toast(e instanceof ApiError ? e.message : String(e), true), [toast]);

  // ---------------------------------------------------------------- engine & system
  useEffect(() => {
    const poll = async () => {
      const info = (await bridge().engineInfo()) as { ready: boolean; error: string | null };
      setEngine(info);
      return info.ready || !!info.error;
    };
    void poll();
    const off = bridge().onEvent((ev) => {
      const e = ev as { type: string; state?: string; error?: string };
      if (e.type === "engine") setEngine({ ready: e.state === "ready", error: e.error ?? null });
    });
    const t = setInterval(async () => (await poll()) && clearInterval(t), 1000);
    return () => { off(); clearInterval(t); };
  }, []);

  const loadAgents = useCallback(async () => {
    const a = await api.get<AgentStatus[]>("/api/agents");
    setAgents(a);
    // A remembered provider may no longer be offered (e.g. the test agent).
    setProvider((cur) => (a.length && !a.some((x) => x.provider === cur) ? a[0].provider : cur));
    setSandboxOk(a.every((x) => x.sandbox));
  }, []);

  // Sign-in happens in a terminal: pick up the new status when the user comes back.
  useEffect(() => {
    if (!engine.ready) return;
    const onFocus = () => void loadAgents().catch(() => undefined);
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, [engine.ready, loadAgents]);

  useEffect(() => {
    if (!engine.ready) return;
    void api.get<ProjectSummary[]>("/api/projects").then((ps) => {
      setProjects(ps);
      if (projectId && !ps.some((p) => p.id === projectId)) setProjectId(ps[0]?.id ?? null);
      if (!projectId && ps[0]) setProjectId(ps[0].id);
      if (ps.length === 0) setNewProject(true);
    }).catch(fail);
    void loadAgents().catch(fail);
  }, [engine.ready]); // eslint-disable-line react-hooks/exhaustive-deps

  // ---------------------------------------------------------------- project data
  const base = projectId ? `/api/projects/${projectId}` : null;
  const loadDocs = useCallback(async () => {
    if (!base) return;
    setDocs(await api.get<DocumentInfo[]>(`${base}/documents`));
    setStats(await api.get<ProjectStats>(`${base}/stats`).catch(() => null));
  }, [base]);
  const loadDesign = useCallback(async () => base && setDesign(await api.get<DesignInfo>(`${base}/design`)), [base]);
  const loadProposals = useCallback(async () => base && setProposals(await api.get<Proposal[]>(`${base}/proposals`)), [base]);
  const loadChat = useCallback(async () => {
    if (!base) return;
    const hist = await api.get<ChatEntry[]>(`${base}/chat/${provider}/history`);
    setEntries(hist.filter((h) => h.role !== "activity"));
    const st = await api.get<{ busy: boolean; active: string | null }>(`${base}/chat`);
    setBusy(st.busy && st.active === provider);
  }, [base, provider]);

  useEffect(() => {
    if (!engine.ready || !projectId) return;
    localStorage.setItem("we:project", projectId);
    setSelections([]);
    setImages([]);
    setView("accepted");
    void api.get<{ name: string }>(`/api/projects/${projectId}`).then((p) => setProjectName(p.name)).catch(fail);
    void Promise.all([loadDocs(), loadDesign(), loadProposals()]).catch(fail);
    void bridge().subscribe(projectId);
  }, [engine.ready, projectId]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    localStorage.setItem("we:provider", provider);
    if (engine.ready && projectId) void loadChat().catch(fail);
  }, [provider, engine.ready, projectId]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!docId && docs.length) setDocId(docs[0].id);
    if (docId && docs.length && !docs.some((d) => d.id === docId)) setDocId(docs[0].id);
  }, [docs, docId]);

  // ---------------------------------------------------------------- live events
  const refreshTimer = useRef<number | null>(null);
  const [previewTick, setPreviewTick] = useState(0);
  const scheduleRefresh = useCallback(() => {
    if (refreshTimer.current) return;
    refreshTimer.current = window.setTimeout(() => {
      refreshTimer.current = null;
      void loadDocs().catch(() => undefined);
      void loadProposals().catch(() => undefined);
      void api.get<ProjectSummary[]>("/api/projects").then(setProjects).catch(() => undefined);
    }, 400);
  }, [loadDocs, loadProposals]);

  useEffect(() => {
    return bridge().onEvent((raw) => {
      const ev = raw as { type: string; project?: string; [k: string]: unknown };
      if (ev.project && ev.project !== projectId) return;
      switch (ev.type) {
        case "job": {
          const job = ev.job as Job;
          if (job.kind === "export" && job.state === "done") {
            const r = job.result as { dest: string; summary: { exported: number; total: number; failed: number }; documents: { name: string }[] };
            const text = r.summary.total === 1
              ? (r.summary.exported ? `Exported “${r.documents[0].name}”.` : `“${r.documents[0].name}” was not exported — see the report.`)
              : `Exported ${r.summary.exported} of ${r.summary.total} documents${r.summary.failed ? ` — ${r.summary.failed} not exported, see the report` : ""}.`;
            toast(text, r.summary.failed > 0, { label: "Show folder", run: () => void bridge().showFolder(r.dest) });
          } else if (job.kind === "export" && job.state === "failed") toast(`Export failed: ${job.error}`, true);
          if (job.state === "done" || job.state === "failed") scheduleRefresh();
          break;
        }
        case "build":
        case "documents":
        case "document":
          scheduleRefresh();
          setPreviewTick((t) => t + 1);
          break;
        case "proposal":
          scheduleRefresh();
          setPreviewTick((t) => t + 1);
          break;
        case "design":
          void loadDesign();
          scheduleRefresh();
          setPreviewTick((t) => t + 1);
          break;
        case "agent":
          if (ev.provider === provider) onAgentEvent(ev.event as Record<string, unknown>);
          break;
      }
    });
  }); // re-subscribes each render so handlers see current state

  function onAgentEvent(e: Record<string, unknown>) {
    switch (e.type) {
      case "turn":
        if (e.state === "started") { setBusy(true); setPhase(null); setStreaming(""); }
        if (e.state === "finished") {
          setBusy(false);
          setPhase(null);
          void loadProposals();
          if (e.proposal) { toast("The assistant proposed a design change. Review it above the preview."); setView("synthetic"); }
        }
        break;
      case "status":
        break;
      case "text_delta":
        setStreaming((s) => s + String(e.text));
        break;
      case "text":
        setStreaming("");
        if (String(e.text).trim()) setEntries((x) => [...x, { role: "agent", text: String(e.text) }]);
        break;
      case "tool":
        // what the agent runs stays out of the chat; the status line just keeps going
        if (String(e.detail ?? "").includes("render-preview")) setPhase("previewing");
        break;
      case "approval":
        setEntries((x) => [...x, { role: "approval", text: `${e.title}: ${e.detail}`, id: String(e.id) }]);
        break;
      case "done":
        setPhase("finishing");
        break;
      case "preview":
        break;
      case "error":
        setStreaming("");
        if (e.kind !== "invalid_design") { setBusy(false); setPhase(null); }
        setEntries((x) => [...x, { role: "error", text: String(e.message), kind: String(e.kind) }]);
        if (e.kind === "auth") void loadAgents();
        break;
    }
  }

  // ---------------------------------------------------------------- preview data
  const doc = docs.find((d) => d.id === docId) ?? null;
  const pending = proposals.find((p) => p.status === "pending") ?? null;
  const [pdfA, setPdfA] = useState<Uint8Array | null>(null);
  const [pdfB, setPdfB] = useState<Uint8Array | null>(null);
  const [mapA, setMapA] = useState<SelectionMap | null>(null);
  const [mapB, setMapB] = useState<SelectionMap | null>(null);
  const [report, setReport] = useState<Report | null>(null);
  // "Show me" from the report: the spot to scroll to and flash; seq makes repeats re-trigger.
  const [focus, setFocus] = useState<{ spot: Spot; seq: number } | null>(null);

  const canCompareApproved = doc?.review?.state === "recheck" && doc.review.reason === "design";
  const effectiveView: View = (!pending && ["proposal", "compare", "synthetic"].includes(view)) || (view === "approved" && !canCompareApproved)
    || (view === "vs-original" && !doc?.has_original) ? "accepted" : view;
  const twoPanes = effectiveView === "compare" || effectiveView === "approved" || effectiveView === "vs-original";
  const buildKey = `${doc?.build?.state}-${doc?.build?.revision}-${pending?.id}-${JSON.stringify(pending?.real?.[docId ?? ""] ?? null)}-${doc?.review?.state}`;

  useEffect(() => {
    if (!base || !doc) { setPdfA(null); setPdfB(null); setMapA(null); setMapB(null); setReport(null); return; }
    let cancelled = false;
    const d = `${base}/documents/${doc.id}`;
    const q = pending ? `proposal=${pending.id}` : "";
    (async () => {
      const accepted = async () => {
        const [pdf, map] = await Promise.all([api.pdf(`${d}/pdf`), api.get<SelectionMap>(`${d}/selection`).catch(() => null)]);
        return { pdf, map };
      };
      const proposed = async () => {
        const [pdf, map] = await Promise.all([api.pdf(`${d}/pdf?${q}`), api.get<SelectionMap>(`${d}/selection?${q}`).catch(() => null)]);
        return { pdf, map };
      };
      let a: { pdf: Uint8Array | null; map: SelectionMap | null } = { pdf: null, map: null };
      let b: typeof a = { pdf: null, map: null };
      let rep: Report | null = null;
      if (effectiveView === "accepted") {
        a = await accepted();
        rep = (await api.get<{ report: Report | null }>(`${d}/report`).catch(() => ({ report: null }))).report;
      } else if (effectiveView === "original") {
        a = { pdf: await api.pdf(`${d}/pdf?kind=original`), map: null };
      } else if (effectiveView === "vs-original") {
        [a, b] = await Promise.all([api.pdf(`${d}/pdf?kind=original`).then((pdf) => ({ pdf, map: null })), accepted()]);
        rep = (await api.get<{ report: Report | null }>(`${d}/report`).catch(() => ({ report: null }))).report;
      } else if (effectiveView === "synthetic") {
        a = { pdf: await api.pdf(`${d}/pdf?kind=synthetic&${q}`), map: null };
      } else if (effectiveView === "approved") {
        const q = `rev=${doc.review.approved!.revision}`;
        const [pdf, map] = await Promise.all([api.pdf(`${d}/pdf?${q}`), api.get<SelectionMap>(`${d}/selection?${q}`).catch(() => null)]);
        [a, b] = [{ pdf, map }, await accepted()];
        rep = (await api.get<{ report: Report | null }>(`${d}/report`).catch(() => ({ report: null }))).report;
      } else if (effectiveView === "proposal") {
        a = await proposed();
        rep = (await api.get<{ report: Report | null }>(`${d}/report?${q}`).catch(() => ({ report: null }))).report;
      } else {
        [a, b] = await Promise.all([accepted(), proposed()]);
        rep = (await api.get<{ report: Report | null }>(`${d}/report?${q}`).catch(() => ({ report: null }))).report;
      }
      if (cancelled) return;
      setPdfA(a.pdf); setMapA(a.map); setPdfB(b.pdf); setMapB(b.map); setReport(rep); setFocus(null);
    })().catch(() => undefined);
    return () => { cancelled = true; };
  }, [base, doc?.id, effectiveView, buildKey, previewTick]); // eslint-disable-line react-hooks/exhaustive-deps

  const selectedIds = useMemo(() => new Set(selections.filter((s) => s.doc === docId).map((s) => s.element)), [selections, docId]);

  function select(el: MapElement, additive: boolean) {
    if (!docId) return;
    const s: Selection = { doc: docId, element: el.id, label: `${el.label}`, kind: el.kind };
    setSelections((cur) => {
      const has = cur.some((x) => x.doc === docId && x.element === el.id);
      if (additive) return has ? cur.filter((x) => !(x.doc === docId && x.element === el.id)) : [...cur, s];
      return has && cur.length === 1 ? [] : [s];
    });
  }

  // ---------------------------------------------------------------- actions
  async function toggleDone(pid: string, d: DocumentInfo, done = d.review?.state === "open") {
    try {
      await api.post(`/api/projects/${pid}/documents/${d.id}/done`, { done });
      if (pid === projectId) { await loadDocs(); if (view === "approved") setView("accepted"); }
      else setOtherDocs((x) => ({ ...x, [pid]: (x[pid] ?? []).map((y) => (y.id === d.id ? { ...y, review: done ? { state: "done" } : { state: "open" } } : y)) }));
    } catch (e) { fail(e); }
  }

  async function importDocs(into: string | null = projectId) {
    if (!into) return;
    const paths = (await bridge().pickDocx()) as string[];
    if (!paths.length) return;
    const res = await api.post<{ path: string; error?: string }[]>(`/api/projects/${into}/documents`, { paths });
    const bad = res.filter((r) => r.error);
    if (bad.length) toast(`${bad.length} file(s) not imported: ${bad[0].error}`, true);
    setExpanded((x) => new Set(x).add(into));
    if (into !== projectId) setProjectId(into);
    else await loadDocs();
  }

  // sidebar tree: which projects are open, and the documents of open projects other than the current one
  const [expanded, setExpanded] = useState<Set<string>>(() => {
    try { return new Set(JSON.parse(localStorage.getItem("we:expanded") ?? "[]") as string[]); } catch { return new Set(); }
  });
  useEffect(() => { try { localStorage.setItem("we:expanded", JSON.stringify([...expanded])); } catch { /* optional */ } }, [expanded]);
  useEffect(() => { if (projectId) setExpanded((x) => (x.has(projectId) ? x : new Set(x).add(projectId))); }, [projectId]);
  const [otherDocs, setOtherDocs] = useState<Record<string, DocumentInfo[]>>({});
  useEffect(() => {
    if (!engine.ready) return;
    for (const id of expanded) {
      if (id !== projectId && projects.some((p) => p.id === id)) {
        void api.get<DocumentInfo[]>(`/api/projects/${id}/documents`)
          .then((d) => setOtherDocs((x) => ({ ...x, [id]: d }))).catch(() => undefined);
      }
    }
  }, [expanded, projects, projectId, engine.ready]);
  const pendingDoc = useRef<string | null>(null);
  useEffect(() => {
    if (pendingDoc.current && docs.some((d) => d.id === pendingDoc.current)) {
      setDocId(pendingDoc.current);
      pendingDoc.current = null;
    }
  }, [docs]);

  async function send(text: string): Promise<boolean> {
    if (!base) return false;
    // Show the message immediately; take it back out if the post fails.
    const entry: ChatEntry = { role: "user", text, attachments: selections.map((s) => s.label), images: images.map((i) => i.url) };
    setEntries((x) => [...x, entry]);
    try {
      await api.post(`${base}/chat/${provider}/messages`, {
        text, selections: selections.map(({ doc, element }) => ({ doc, element })), images: images.map((i) => i.data),
      });
      setImages([]); // sent once; the assistant keeps them in its workspace
      setBusy(true);
      setPhase(null);
      return true;
    } catch (e) {
      setEntries((x) => x.filter((y) => y !== entry));
      if (e instanceof ApiError && e.status === 401) {
        setEntries((x) => [...x, { role: "error", text: e.message, kind: "auth" }]);
        void loadAgents();
      } else fail(e);
      return false;
    }
  }

  async function addImages(files: File[]) {
    const room = MAX_IMAGES - images.length;
    if (files.length > room) toast(`Attach at most ${MAX_IMAGES} images to a message.`, true);
    const ready: RefImage[] = [];
    for (const f of files.slice(0, Math.max(room, 0))) {
      try {
        ready.push(await prepareImage(f));
      } catch {
        toast(`${f.name || "The image"} could not be read.`, true);
      }
    }
    if (ready.length) setImages((x) => [...x, ...ready].slice(0, MAX_IMAGES));
  }

  async function runCommand(name: string): Promise<boolean> {
    const notice = (text: string) => setEntries((x) => [...x, { role: "notice", text }]);
    switch (name) {
      case "clear":
        if (!base) return false;
        try {
          await api.post(`${base}/chat/${provider}/clear`);
        } catch (e) { fail(e); return false; }
        setEntries([]); setStreaming(""); setPhase(null);
        return true;
      case "setup":
        setSetupOpen(true);
        return true;
      case "help":
        notice(COMMANDS.map((c) => `/${c.name} — ${c.hint}`).join("\n"));
        return true;
      default:
        notice(`Unknown command /${name}. Type /help to see the commands.`);
        return true;
    }
  }

  /** Export a whole project, or one of its documents (`doc`). */
  async function exportPdfs(pid: string, doc?: DocumentInfo) {
    const folder = (await bridge().pickFolder(doc ? `Export “${doc.name}” to…` : "Export verified PDFs to…")) as string | null;
    if (!folder) return;
    try {
      await api.post(`/api/projects/${pid}/export`, { path: folder, ...(doc ? { docs: [doc.id] } : {}) });
      toast(doc ? `Exporting “${doc.name}”… it is verified first.` : "Exporting… each document is verified; failures do not stop the others.");
    } catch (e) { fail(e); }
  }

  async function createProject(name: string) {
    const p = await api.post<{ id: string; name: string }>("/api/projects", { name });
    setProjects(await api.get<ProjectSummary[]>("/api/projects"));
    setProjectId(p.id);
    setNewProject(false);
  }

  async function deleteProjects(ids: string[]) {
    const chosen = projects.filter((x) => ids.includes(x.id));
    if (!chosen.length) return;
    const what = chosen.length === 1 ? `the project “${chosen[0].name}”` : `${chosen.length} projects`;
    if (!confirm(`Delete ${what}?\n\nImported documents, design history and chats are removed from this computer. `
      + "Your original Word files and exported PDFs are not touched. This cannot be undone.")) return;
    for (const p of chosen) {
      try {
        await api.del(`/api/projects/${p.id}`);
      } catch (e) { fail(e); }
    }
    const rest = await api.get<ProjectSummary[]>("/api/projects");
    setProjects(rest);
    toast(`Deleted ${what}.`);
    if (!projectId || !ids.includes(projectId)) return; // the open project stays open
    setDocs([]); setDesign(null); setProposals([]); setEntries([]); setProjectName("");
    setProjectId(rest[0]?.id ?? null);
    if (!rest.length) { localStorage.removeItem("we:project"); setNewProject(true); }
  }

  const current = agents.find((a) => a.provider === provider);
  const disabledReason = !engine.ready ? "Starting the engine…"
    : !sandboxOk ? "Assistants are disabled: Bubblewrap isolation is unavailable (see Settings)."
    : current && !current.installed ? `${current.title} is not installed (see Settings).`
    : current && !current.logged_in ? `Sign in to ${current.title} first (see Settings).`
    : docs.length === 0 ? "Import documents first." : null;

  // ---------------------------------------------------------------- render
  if (!engine.ready) {
    return (
      <div className="splash">
        <h1>Livery</h1>
        {engine.error ? (
          <>
            <p className="bad-text pre">{engine.error}</p>
            <button className="btn primary" onClick={async () => setEngine((await bridge().restartEngine()) as typeof engine)}>Try again</button>
          </>
        ) : <p className="muted">Starting the local engine…</p>}
      </div>
    );
  }

  return (
    <div className="app">
      <main className={`workspace${sidebarOpen ? "" : " no-sidebar"}`} style={{ "--chat-w": `${chatWidth}px` } as React.CSSProperties}>
        {sidebarOpen && <aside className="left">
          <div className="sidebar-head">
            <div className="brand">Livery</div>
            <button className="icon-btn" onClick={() => setSidebarOpen(false)} title="Hide sidebar (Ctrl+B)" aria-label="Hide sidebar">
              <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="1.8"
                strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="4" width="18" height="16" rx="2" /><path d="M9 4v16" /></svg>
            </button>
          </div>
          <ProjectTree projects={projects} current={projectId} currentDoc={docId}
            docsOf={(id) => (id === projectId ? docs : otherDocs[id])} expanded={expanded}
            onToggle={(id) => setExpanded((x) => { const n = new Set(x); if (n.has(id) && id === projectId) n.delete(id); else n.add(id); return n; })}
            onOpenProject={(id) => setProjectId(id)}
            onOpenDoc={(pid, did) => { if (pid === projectId) setDocId(did); else { pendingDoc.current = did; setProjectId(pid); } }}
            onImport={(id) => void importDocs(id).catch(fail)} onNewProject={() => setNewProject(true)}
            onDeleteProject={(id) => void deleteProjects([id])}
            onExportProject={(id) => void exportPdfs(id)}
            onExportDoc={(pid, d) => void exportPdfs(pid, d)}
            onToggleDone={(pid, d) => void toggleDone(pid, d)}
            onRemoveDoc={async (d) => { if (confirm(`Remove “${d.name}” from the project?`)) { await api.del(`${base}/documents/${d.id}`).catch(fail); void loadDocs(); } }}
            onReplaceDoc={async (d) => {
              const paths = (await bridge().pickDocx()) as string[];
              if (paths[0]) { await api.post(`${base}/documents/${d.id}/replace`, { path: paths[0] }).catch(fail); void loadDocs(); }
            }} />
          <Stats stats={stats} />
          <Treatments design={design} selections={selections}
            onFindMatches={async (s) => {
              try { setMatches(await api.post(`${base}/treatments/candidates`, { doc: s.doc, element: s.element })); } catch (e) { fail(e); }
            }}
            onAssign={async (t, refs) => {
              try {
                await api.post(`${base}/treatments/apply`, { treatment: t, refs: refs.map(({ doc, element }) => ({ doc, element })) });
                await loadProposals();
                toast("Proposal created. Render it with real content, then accept.");
              } catch (e) { fail(e); }
            }} />
          <History design={design} onUndo={async () => { await api.post(`${base}/design/undo`).catch(fail); void loadDesign(); }}
            onRemove={async (rev) => {
              try {
                const r = await api.post<{ proposal: Proposal | null }>(`${base}/design/changes/${rev}/remove`);
                if (r.proposal) { await loadProposals(); toast("Proposal created without that change. Render it with real content, then accept."); }
              } catch (e) { fail(e); }
              void loadDesign();
            }} />
          <div className="sidebar-foot">
            <button className="settings-btn" onClick={() => setSetupOpen(true)}>
              <svg viewBox="0 0 24 24" width="15" height="15" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="1.8"
                strokeLinecap="round" strokeLinejoin="round">
                <path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z" />
                <circle cx="12" cy="12" r="3" />
              </svg>
              Settings
            </button>
            <button className="repo-link" title="github.com/eddev00/livery"
              onClick={() => void bridge().openExternal("https://github.com/eddev00/livery")}>
              <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" fill="currentColor">
                <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z" />
              </svg>
              GitHub
            </button>
            {!sandboxOk && <button className="badge FAIL" onClick={() => setSetupOpen(true)}>Isolation unavailable</button>}
          </div>
        </aside>}

        <section className="center">
          {pending && (
            <ProposalBar proposal={pending} docs={docs} agentNames={Object.fromEntries(agents.map((a) => [a.provider, a.title]))}
              onRender={async () => { await api.post(`${base}/proposals/${pending.id}/render`, {}).catch(fail); void loadProposals(); }}
              onAccept={async () => {
                const touched = Object.entries(pending.done_impact ?? {}).filter(([, i]) => i.state === "changed");
                if (touched.length && !confirm(`This change alters ${touched.length === 1 ? "a document" : `${touched.length} documents`} you marked done:\n\n`
                  + touched.map(([id]) => `• ${docs.find((x) => x.id === id)?.name ?? id}`).join("\n")
                  + `\n\nAccept anyway? ${touched.length === 1 ? "It" : "They"} will be flagged for a re-check.`)) return;
                try {
                  const r = await api.post<{ failed?: string[] }>(`${base}/proposals/${pending.id}/accept`);
                  setView("accepted");
                  const n = r.failed?.length ?? 0;
                  toast(n ? `Accepted. ${n} document${n === 1 ? "" : "s"} failed verification with this design; check ${n === 1 ? "it" : "them"} before exporting.`
                    : "Accepted. The new design is now the project design.");
                }
                catch (e) { fail(e); }
                void loadProposals(); void loadDesign();
              }}
              onDiscard={async () => { await api.post(`${base}/proposals/${pending.id}/discard`).catch(fail); setView("accepted"); void loadProposals(); }}
              onView={(m) => setView(m)} />
          )}
          <div className="preview-bar">
            {!sidebarOpen && (
              <button className="icon-btn" onClick={() => setSidebarOpen(true)} title="Show sidebar (Ctrl+B)" aria-label="Show sidebar">
                <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="1.8"
                strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="4" width="18" height="16" rx="2" /><path d="M9 4v16" /></svg>
              </button>
            )}
            <div className="segmented small">
              <button className={effectiveView === "accepted" ? "on" : ""} onClick={() => setView("accepted")}>Accepted</button>
              <button className={effectiveView === "original" ? "on" : ""} disabled={!doc?.has_original} onClick={() => setView("original")}
                title={doc?.has_original ? "The Word file as LibreOffice lays it out" : "Install LibreOffice to see the original layout"}>Original</button>
              <button className={effectiveView === "vs-original" ? "on" : ""} disabled={!doc?.has_original} onClick={() => setView("vs-original")}
                title={doc?.has_original ? "Original (left) next to Accepted (right)" : "Install LibreOffice to see the original layout"}>Side by side</button>
              {pending && <>
                <button className={effectiveView === "synthetic" ? "on" : ""} onClick={() => setView("synthetic")}>Synthetic</button>
                <button className={effectiveView === "proposal" ? "on" : ""} onClick={() => setView("proposal")}>Proposed</button>
                <button className={effectiveView === "compare" ? "on" : ""} onClick={() => setView("compare")}>Compare</button>
              </>}
            </div>
            <div className="doc-title">{doc?.name ?? ""}</div>
            {doc && (
              <button className={`btn small done-toggle ${doc.review?.state === "open" ? "" : "on"}`}
                disabled={doc.review?.state === "open" && (doc.build?.state !== "done" || doc.report_status === "FAIL")}
                title={doc.review?.state === "open" ? "Mark this document done: you will be told if a later change alters it"
                  : `Marked done with ${doc.review?.approved?.revision ?? "the design"} — click to reopen`}
                onClick={() => void toggleDone(projectId!, doc)}>
                {doc.review?.state === "open" ? "Mark done" : doc.review?.state === "recheck" ? "Done · re-check" : "✓ Done"}
              </button>
            )}
            <div className="zoom">
              <button className="icon" aria-label="Zoom out" onClick={() => setZoom((z) => Math.max(0.4, +((z === "fit" ? 1 : z) - 0.1).toFixed(2)))}>−</button>
              <button className={`link small zoom-label ${zoom === "fit" ? "on" : ""}`} onClick={() => setZoom("fit")} title="Fit to width">
                {zoom === "fit" ? "Fit" : `${Math.round(zoom * 100)}%`}
              </button>
              <button className="icon" aria-label="Zoom in" onClick={() => setZoom((z) => Math.min(2.5, +((z === "fit" ? 1 : z) + 0.1).toFixed(2)))}>+</button>
            </div>
          </div>
          {doc?.review?.state === "recheck" && (
            <div className="recheck">
              <span>Marked done, but {recheckText(doc.review).replace(/^./, (c) => c.toLowerCase())}</span>
              <span className="recheck-actions">
                {canCompareApproved && (effectiveView === "approved"
                  ? <button className="btn small" onClick={() => setView("accepted")}>Close comparison</button>
                  : <button className="btn small" onClick={() => setView("approved")}>Compare with approved</button>)}
                <button className="btn small" disabled={doc.build?.state !== "done" || doc.report_status === "FAIL"}
                  onClick={() => void toggleDone(projectId!, doc, true)}>Looks right — mark done</button>
              </span>
            </div>
          )}
          {effectiveView === "synthetic" && <div className="hint">Synthetic preview: lorem-ipsum twin of this document with the proposed design — what the assistant
            saw. Render with real content to check the actual pages.</div>}
          <div className={`preview ${twoPanes ? "compare" : ""}`}>
            <div className="preview-pane">
              {effectiveView === "compare" && <div className="pane-label">Accepted</div>}
              {effectiveView === "vs-original" && <div className="pane-label">Original</div>}
              {effectiveView === "approved" && <div className="pane-label">As approved ({doc?.review?.approved?.revision})</div>}
              <PdfView data={pdfA} map={effectiveView === "original" || effectiveView === "vs-original" || effectiveView === "synthetic" || effectiveView === "approved" ? null : mapA} zoom={zoom} selected={selectedIds}
                focus={effectiveView === "accepted" || effectiveView === "proposal" ? focus : null}
                onSelect={select} onUnsupported={(el) => toast(`${el.label}: ${el.kind}s can't be attached yet. Select a heading, paragraph or table.`)}
                emptyText={!doc ? "No document selected." : effectiveView === "original" || effectiveView === "vs-original" ? "No original rendering (LibreOffice not installed or failed)."
                  : effectiveView === "synthetic" ? "No synthetic preview for this document in this proposal."
                  : effectiveView === "proposal" ? "Not rendered with real content yet." : "Rendering…"} />
            </div>
            {twoPanes && (
              <div className="preview-pane">
                <div className="pane-label">{effectiveView === "approved" ? "Now" : effectiveView === "vs-original" ? "Accepted" : "Proposed"}</div>
                <PdfView data={pdfB} map={mapB} zoom={zoom} selected={selectedIds} onSelect={select} focus={focus}
                  emptyText={effectiveView === "compare" ? "Not rendered with real content yet." : "Rendering…"} />
              </div>
            )}
          </div>
          <ReportPanel report={report} onShow={(spot) => setFocus({ spot, seq: Date.now() })} />
        </section>

        <aside className="right">
          <div className="resize-handle" role="separator" aria-orientation="vertical" aria-label="Resize agent panel"
            aria-valuenow={chatWidth} aria-valuemin={CHAT_MIN} tabIndex={0} title="Drag to resize · double-click to reset"
            onPointerDown={startChatResize} onKeyDown={keyChatResize} onDoubleClick={() => setChatWidth(clampChat(CHAT_DEFAULT))} />
          <Chat providers={agents} provider={provider}
            onProvider={(p) => { setProvider(p); setStreaming(""); }}
            entries={entries} streaming={streaming} busy={busy} phase={phase}
            attachments={selections} onRemoveAttachment={(s) => setSelections((x) => x.filter((y) => !(y.doc === s.doc && y.element === s.element)))}
            images={images} onAddImages={(f) => void addImages(f)} onRemoveImage={(id) => setImages((x) => x.filter((i) => i.id !== id))}
            onSend={send} onCancel={() => void api.post(`${base}/chat/cancel`).catch(fail)}
            onApproval={(id, allow) => void api.post(`${base}/chat/approvals/${id}`, { allow }).catch(fail)}
            onSetup={() => setSetupOpen(true)} onCommand={runCommand} disabledReason={disabledReason} />
        </aside>
      </main>

      {setupOpen && <SetupDialog agents={agents} onClose={() => setSetupOpen(false)} refresh={loadAgents} />}
      {matches && design && (
        <MatchDialog candidates={matches.candidates} signature={matches.signature} treatments={design.treatments}
          onClose={() => setMatches(null)}
          onApply={async (t, refs) => {
            try {
              await api.post(`${base}/treatments/apply`, { treatment: t, refs });
              await loadProposals();
              toast(`Proposal: “${t}” on ${refs.length} table(s). Render it with real content, then accept.`);
            } catch (e) { fail(e); }
          }} />
      )}
      {newProject && <NewProject onCreate={createProject} onClose={projects.length ? () => setNewProject(false) : undefined} />}
      <div className="toasts" role="status">
        {toasts.map((t) => (
          <div key={t.id} className={`toast ${t.bad ? "bad" : ""}`}>
            {t.text}
            {t.action && <button className="link" onClick={t.action.run}>{t.action.label}</button>}
          </div>
        ))}
      </div>
    </div>
  );
}

function NewProject({ onCreate, onClose }: { onCreate: (name: string) => Promise<void>; onClose?: () => void }) {
  const [name, setName] = useState("");
  useEffect(() => {
    if (!onClose) return;
    const k = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  }, [onClose]);
  return (
    <div className="modal-back" onMouseDown={onClose}>
      <form className="modal compact" role="dialog" aria-label="New project" onMouseDown={(e) => e.stopPropagation()}
        onSubmit={(e) => { e.preventDefault(); if (name.trim()) void onCreate(name); }}>
        <label className="field">Project name
          <input autoFocus value={name} onChange={(e) => setName(e.target.value)} />
        </label>
        <div className="modal-foot">
          {onClose && <button type="button" className="btn" onClick={onClose}>Cancel</button>}
          <button type="submit" className="btn primary" disabled={!name.trim()}>Create</button>
        </div>
      </form>
    </div>
  );
}
