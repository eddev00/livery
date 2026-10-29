import { useEffect, useState } from "react";
import { api, bridge } from "../api";
import type { AgentStatus, LoginHelp, Treatment } from "../types";

export function Modal({ title, onClose, children, wide }: { title: string; onClose: () => void; children: React.ReactNode; wide?: boolean }) {
  useEffect(() => {
    const k = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  }, [onClose]);
  return (
    <div className="modal-back" onMouseDown={onClose}>
      <div className={`modal ${wide ? "wide" : ""}`} role="dialog" aria-label={title} onMouseDown={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <h2>{title}</h2>
          <button className="icon" aria-label="Close" onClick={onClose}>×</button>
        </div>
        <div className="modal-body">{children}</div>
      </div>
    </div>
  );
}

interface System { tools: Record<string, string>; sandbox: { ok: boolean; message: string; version: string }; logs: string }

/** Agent installation, sign-in into Livery's own profiles, Bubblewrap and bundled tools. */
export function SetupDialog({ onClose, agents, refresh }: { onClose: () => void; agents: AgentStatus[]; refresh: () => Promise<void> }) {
  const [system, setSystem] = useState<System | null>(null);
  const [help, setHelp] = useState<Record<string, LoginHelp>>({});
  const [secret, setSecret] = useState<Record<string, string>>({});
  const [note, setNote] = useState<string | null>(null);
  const [waiting, setWaiting] = useState<Set<string>>(new Set());

  useEffect(() => {
    // while the dialog is open, follow sign-ins finished in the browser
    const t = setInterval(() => void refresh().catch(() => undefined), 3000);
    return () => clearInterval(t);
  }, [refresh]);

  useEffect(() => {
    void api.get<System>("/api/system").then(setSystem);
    for (const a of agents) void api.get<LoginHelp>(`/api/agents/${a.provider}/login`).then((h) => setHelp((x) => ({ ...x, [a.provider]: h })));
  }, [agents]);

  async function connect(a: AgentStatus) {
    try {
      await api.post(`/api/agents/${a.provider}/connect`);
      setWaiting((w) => new Set(w).add(a.provider));
      setNote(null);
    } catch (e) {
      setNote(`Could not start the ${a.title} sign-in: ${(e as Error).message}`);
    }
  }

  async function saveSecret(provider: string, name: string) {
    await api.post(`/api/agents/${provider}/secret`, { name, value: secret[`${provider}:${name}`] ?? "" });
    setSecret((s) => ({ ...s, [`${provider}:${name}`]: "" }));
    setNote("Saved to the Livery profile.");
    await refresh();
  }

  return (
    <Modal title="Settings" onClose={onClose} wide>
      {system && !system.sandbox.ok && (
        <div className="callout bad">
          <strong>Isolation unavailable.</strong> {system.sandbox.message} Assistants stay disabled until Bubblewrap works:
          Livery never runs an assistant without its sandbox.
        </div>
      )}
      {agents.map((a) => (
        <section key={a.provider} className="setup-agent">
          <h3>
            {a.title} <span className="muted">by {a.company}</span>
            <span className={`badge ${a.installed && a.logged_in ? "PASS" : "WARN"}`}>
              {!a.installed ? "not installed" : a.logged_in ? `signed in (${a.auth_method})` : "not signed in"}
            </span>
          </h3>
          {!a.installed ? (
            <p>
              Install {a.title} first ({a.provider === "claude" ? "see docs.claude.com → Claude Code → Setup" : "see developers.openai.com/codex"}),
              then press Refresh. Livery uses the installed command; it does not install agents.
            </p>
          ) : (
            <>
              <p className="muted small">{a.binary} · {a.version}</p>
              <div className="row">
                {a.logged_in ? (
                  <span className="small">Connected{a.account ? ` as ${a.account}` : ""}.</span>
                ) : waiting.has(a.provider) ? (
                  <span className="small">Finish signing in in your browser…</span>
                ) : (
                  <span className="small">Connect your {a.provider === "claude" ? "Claude" : "ChatGPT"} subscription.</span>
                )}
                {help[a.provider]?.connect && (
                  <button className={`btn ${a.logged_in ? "" : "primary"}`} onClick={() => void connect(a)}>
                    {a.logged_in ? "Reconnect" : waiting.has(a.provider) ? "Open browser again" : "Connect"}
                  </button>
                )}
              </div>
              {(help[a.provider]?.secrets.length ?? 0) > 0 && (
                <details className="small">
                  <summary>Use a key or token instead</summary>
                  {help[a.provider]!.secrets.map((s) => (
                    <div className="secret" key={s.name}>
                      <label>{s.label}</label>
                      <input
                        type="password"
                        autoComplete="off"
                        value={secret[`${a.provider}:${s.name}`] ?? ""}
                        onChange={(e) => setSecret((x) => ({ ...x, [`${a.provider}:${s.name}`]: e.target.value }))}
                      />
                      <button className="btn" onClick={() => void saveSecret(a.provider, s.name)}>
                        {secret[`${a.provider}:${s.name}`] ? "Save" : "Clear"}
                      </button>
                    </div>
                  ))}
                </details>
              )}
            </>
          )}
        </section>
      ))}
      {note && <div className="callout">{note}</div>}
      <details className="tools">
        <summary>Bundled tools and sandbox</summary>
        <ul>
          <li>Bubblewrap: {system ? (system.sandbox.ok ? `${system.sandbox.version} — working` : system.sandbox.message) : "…"}</li>
          {system && Object.entries(system.tools).map(([k, v]) => <li key={k}><code>{k}</code> {v}</li>)}
        </ul>
        <p className="small muted">LibreOffice (soffice) is optional: with it, the “Original” view shows each Word file as Word lays it out.</p>
      </details>
      <div className="row small">
        <span className="muted">Logs: <code>{system?.logs ?? "…"}</code> (engine.log, app.log)</span>
        <button className="btn small" onClick={() => void bridge().openLogs()}>Open log folder</button>
      </div>
      <div className="modal-foot">
        <button className="btn" onClick={() => void refresh()}>Refresh</button>
        <button className="btn primary" onClick={onClose}>Done</button>
      </div>
    </Modal>
  );
}

export interface Candidate {
  doc: string; doc_name: string; element: string; label: string; page: number | null;
  match: "match" | "similar"; signature: string; assigned: string | null; is_reference: boolean;
}

/** Review structural matches before a treatment is applied across documents. */
export function MatchDialog({ onClose, candidates, signature, treatments, onApply }: {
  onClose: () => void; candidates: Candidate[]; signature: string; treatments: Record<string, Treatment>;
  onApply: (treatment: string, refs: { doc: string; element: string }[]) => Promise<void>;
}) {
  const tableTreatments = Object.entries(treatments).filter(([, t]) => t.kind === "table").map(([n]) => n);
  const [chosen, setChosen] = useState<string>(tableTreatments[0] ?? "");
  const [checked, setChecked] = useState<Set<string>>(
    () => new Set(candidates.filter((c) => c.match === "match").map((c) => `${c.doc}/${c.element}`)));
  const [busy, setBusy] = useState(false);
  const toggle = (k: string) => setChecked((s) => {
    const n = new Set(s);
    if (n.has(k)) n.delete(k);
    else n.add(k);
    return n;
  });
  return (
    <Modal title="Apply a treatment to matching tables" onClose={onClose} wide>
      <p className="small">
        Tables with the same structure (<code>{signature}</code>). Structure is only a hint: check each table and leave out
        those that serve a different purpose. Similar tables (same columns and header rows, different merged cells) are
        listed unchecked.
      </p>
      {tableTreatments.length === 0 ? (
        <div className="callout">No table treatment yet. Ask the assistant to create one (for example “make this a reusable
          treatment called Data table”), accept it, then come back here.</div>
      ) : (
        <label className="field">Treatment
          <select value={chosen} onChange={(e) => setChosen(e.target.value)}>
            {tableTreatments.map((n) => <option key={n}>{n}</option>)}
          </select>
        </label>
      )}
      <table className="matches">
        <thead><tr><th /><th>Document</th><th>Table</th><th>Match</th><th>Current treatment</th></tr></thead>
        <tbody>
          {candidates.map((c) => {
            const k = `${c.doc}/${c.element}`;
            return (
              <tr key={k} className={c.is_reference ? "ref" : ""}>
                <td><input type="checkbox" checked={checked.has(k)} onChange={() => toggle(k)} aria-label={`Include ${c.label}`} /></td>
                <td>{c.doc_name}</td>
                <td>{c.label}{c.is_reference ? " (selected)" : ""}</td>
                <td><span className={`badge ${c.match === "match" ? "PASS" : "WARN"}`}>{c.match}</span></td>
                <td>{c.assigned ?? "—"}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <div className="modal-foot">
        <button className="btn" onClick={onClose}>Cancel</button>
        <button className="btn primary" disabled={!chosen || checked.size === 0 || busy} onClick={async () => {
          setBusy(true);
          try {
            await onApply(chosen, [...checked].map((k) => ({ doc: k.split("/")[0], element: k.split("/")[1] })));
            onClose();
          } finally {
            setBusy(false);
          }
        }}>
          Propose for {checked.size} table{checked.size === 1 ? "" : "s"}
        </button>
      </div>
    </Modal>
  );
}
