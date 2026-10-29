import { useEffect, useRef, useState } from "react";
import { IMAGE_ACCEPT, imageFiles, MAX_IMAGES } from "../images";
import type { AgentStatus, ChatEntry, RefImage, Selection } from "../types";
import { AgentIcon } from "./AgentIcon";

interface Props {
  providers: AgentStatus[];
  provider: string;
  onProvider: (p: string) => void;
  entries: ChatEntry[];
  streaming: string;
  busy: boolean;
  phase: string | null;
  attachments: Selection[];
  onRemoveAttachment: (s: Selection) => void;
  images: RefImage[];
  onAddImages: (files: File[]) => void;
  onRemoveImage: (id: string) => void;
  onSend: (text: string) => Promise<boolean>;
  onCancel: () => void;
  onApproval: (id: string, allow: boolean) => void;
  onSetup: () => void;
  onCommand: (name: string) => Promise<boolean>;
  disabledReason: string | null;
}

export const COMMANDS = [
  { name: "clear", hint: "Start a new conversation" },
  { name: "setup", hint: "Set up and sign in to assistants" },
  { name: "help", hint: "Show the available commands" },
] as const;

export function Chat(p: Props) {
  const [text, setText] = useState("");
  const list = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  const picker = useRef<HTMLInputElement>(null);
  const [dropping, setDropping] = useState(false);

  useEffect(() => {
    list.current?.scrollTo({ top: list.current.scrollHeight });
  }, [p.entries.length, p.streaming]);

  // Grow with the text, up to a cap; Shift+Enter adds a line.
  useEffect(() => {
    const el = input.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight + el.offsetHeight - el.clientHeight, 200)}px`;
  }, [text]);

  // Clear right away; the post can take a moment. Restore the text if it failed
  // and nothing new was typed meanwhile.
  const sending = useRef(false);
  async function send(value = text) {
    if (!value.trim() || p.busy || p.disabledReason || sending.current) return;
    const sent = value;
    sending.current = true;
    setText("");
    try {
      const cmd = parseCommand(sent);
      const ok = cmd ? await p.onCommand(cmd) : await p.onSend(sent);
      if (!ok) setText((t) => t || sent);
    } finally {
      sending.current = false;
    }
  }

  // "/" opens the command menu while the first word is being typed.
  const [menuIndex, setMenuIndex] = useState(0);
  const [menuClosed, setMenuClosed] = useState(false);
  const query = /^\/\S*$/.test(text) ? text.slice(1).toLowerCase() : null;
  const menu = query === null || menuClosed ? [] : COMMANDS.filter((c) => c.name.startsWith(query));
  useEffect(() => { setMenuIndex(0); setMenuClosed(false); }, [query]);

  const current = p.providers.find((a) => a.provider === p.provider);
  const needsSignIn = current && (!current.installed || !current.logged_in);
  const canAttach = !p.disabledReason && p.images.length < MAX_IMAGES;

  return (
    <div className="chat">
      <div className="chat-head">
        <div className="segmented" role="tablist" aria-label="Assistant">
          {p.providers.map((a) => (
            <button
              key={a.provider}
              role="tab"
              aria-selected={a.provider === p.provider}
              className={a.provider === p.provider ? "on" : ""}
              disabled={p.busy}
              onClick={() => p.onProvider(a.provider)}
            >
              <AgentIcon provider={a.provider} />
              {a.title}
            </button>
          ))}
        </div>
      </div>

      {needsSignIn ? (
        <div className="chat-signin">
          <AgentIcon provider={current.provider} size={32} />
          <p>{current.installed ? `Sign in to ${current.title} to start.` : `${current.title} is not installed yet.`}</p>
          <button className="btn primary" onClick={p.onSetup}>{current.installed ? "Sign in…" : "Set up…"}</button>
        </div>
      ) : (
      <div className="chat-list" ref={list}>
        {p.entries.map((e, i) => (
          <Entry key={i} e={e} onApproval={p.onApproval} onSetup={p.onSetup} />
        ))}
        {p.streaming && <div className="msg msg-agent streaming">{p.streaming}</div>}
        {p.busy && <Working phase={p.phase} />}
      </div>
      )}

      <div className={`composer${dropping ? " dropping" : ""}`}
        onDragOver={(e) => {
          if (!canAttach || !Array.from(e.dataTransfer.items).some((i) => i.kind === "file")) return;
          e.preventDefault();
          setDropping(true);
        }}
        onDragLeave={(e) => { if (!e.currentTarget.contains(e.relatedTarget as Node)) setDropping(false); }}
        onDrop={(e) => {
          setDropping(false);
          if (!canAttach) return;
          e.preventDefault();
          const files = imageFiles(e.dataTransfer.files);
          if (files.length) p.onAddImages(files);
        }}>
        {(p.attachments.length > 0 || p.images.length > 0) && (
          <div className="chips">
            {p.attachments.map((s) => (
              <span className="chip" key={`${s.doc}/${s.element}`}>
                {s.label}
                <button aria-label={`Remove ${s.label}`} onClick={() => p.onRemoveAttachment(s)}>×</button>
              </span>
            ))}
            {p.images.map((img, i) => (
              <span className="chip chip-image" key={img.id}>
                <img src={img.url} alt="" />
                Image {i + 1}
                <button aria-label={`Remove image ${i + 1}`} onClick={() => p.onRemoveImage(img.id)}>×</button>
              </span>
            ))}
          </div>
        )}
        {p.images.length > 0 && (
          <div className="composer-note">
            Images go to {current?.company ?? "the assistant's provider"} as they are, unlike your documents. Don't attach
            pictures of confidential pages.
          </div>
        )}
        <div className="composer-field">
        {menu.length > 0 && (
          <div className="command-menu" role="listbox" aria-label="Commands">
            {menu.map((c, i) => (
              <button key={c.name} role="option" aria-selected={i === menuIndex}
                className={i === menuIndex ? "on" : ""}
                onMouseEnter={() => setMenuIndex(i)}
                onMouseDown={(e) => { e.preventDefault(); void send(`/${c.name}`); }}>
                <span className="command-name">/{c.name}</span>
                <span className="command-hint">{c.hint}</span>
              </button>
            ))}
          </div>
        )}
        <textarea
          ref={input}
          value={text}
          placeholder={p.disabledReason ?? (p.busy ? "Esc to stop" : "Describe a change, or / for commands")}
          disabled={!!p.disabledReason}
          onChange={(e) => setText(e.target.value)}
          onPaste={(e) => {
            const files = imageFiles(e.clipboardData.files);
            if (!files.length || !canAttach) return;
            e.preventDefault();
            p.onAddImages(files);
          }}
          onKeyDown={(e) => {
            if (menu.length > 0) {
              const pick = menu[Math.min(menuIndex, menu.length - 1)];
              if (e.key === "ArrowDown" || e.key === "ArrowUp") {
                e.preventDefault();
                const d = e.key === "ArrowDown" ? 1 : -1;
                setMenuIndex((i) => (i + d + menu.length) % menu.length);
                return;
              }
              if (e.key === "Tab") { e.preventDefault(); setText(`/${pick.name}`); return; }
              if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault();
                void send(`/${pick.name}`);
                return;
              }
              if (e.key === "Escape") { e.preventDefault(); setMenuClosed(true); return; }
            }
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              void send();
            } else if (e.key === "Escape" && p.busy) {
              e.preventDefault();
              p.onCancel();
            }
          }}
          rows={1}
        />
        <input ref={picker} type="file" accept={IMAGE_ACCEPT} multiple hidden
          onChange={(e) => { p.onAddImages(imageFiles(e.target.files)); e.target.value = ""; }} />
        <button
          className="composer-attach"
          aria-label="Attach a reference image"
          title={p.images.length >= MAX_IMAGES ? `At most ${MAX_IMAGES} images` : "Attach a reference image (or paste or drop one)"}
          disabled={!canAttach}
          onClick={() => picker.current?.click()}
        >
          <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" fill="none" stroke="currentColor"
            strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
            <rect x="2" y="3" width="12" height="10" rx="1.5" /><circle cx="6" cy="6.5" r="1.2" /><path d="m3 12 3.5-3.5 2.5 2.5 1.5-1.5L13.5 12" />
          </svg>
        </button>
        <button
          className={`composer-key${text.trim() || p.busy ? " ready" : ""}`}
          aria-label={p.busy ? "Stop (Esc)" : "Send (Enter)"}
          title={p.busy ? "Stop (Esc)" : "Send (Enter)"}
          disabled={!!p.disabledReason || (!p.busy && !text.trim())}
          onClick={() => (p.busy ? p.onCancel() : void send())}
        >
          {p.busy ? (
            <svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true"><rect x="3.5" y="3.5" width="9" height="9" rx="1.5" fill="currentColor" /></svg>
          ) : (
            <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" fill="none" stroke="currentColor"
              strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
              <path d="M13 3v5.5a2 2 0 0 1-2 2H3.5" /><path d="M6.5 7.5 3.5 10.5l3 3" />
            </svg>
          )}
        </button>
        </div>
      </div>
    </div>
  );
}

function Entry({ e, onApproval, onSetup }: { e: ChatEntry; onApproval: (id: string, allow: boolean) => void; onSetup: () => void }) {
  if (e.role === "activity") return null;
  if (e.role === "notice") return <div className="msg msg-notice pre">{e.text}</div>;
  if (e.role === "approval")
    return (
      <div className="msg msg-approval">
        <div>The assistant asks for permission: {e.text}</div>
        <div className="row">
          <button className="btn" onClick={() => onApproval(e.id!, false)}>Decline</button>
          <button className="btn primary" onClick={() => onApproval(e.id!, true)}>Allow</button>
        </div>
      </div>
    );
  if (e.role === "error")
    return (
      <div className="msg msg-error">
        <div className="pre">{e.text}</div>
        {e.kind === "auth" && <button className="btn" onClick={onSetup}>Sign in…</button>}
      </div>
    );
  return (
    <div className={`msg msg-${e.role}`}>
      {e.attachments && e.attachments.length > 0 && <div className="msg-attachments">{e.attachments.join(" · ")}</div>}
      {e.images && e.images.length > 0 && (
        <div className="msg-images">{e.images.map((src, i) => <img key={i} src={src} alt={`Image ${i + 1}`} />)}</div>
      )}
      <div className="pre">{e.text}</div>
    </div>
  );
}

/** A lone `/word` is a command (known or not); anything else is an ordinary message. */
function parseCommand(text: string): string | null {
  const m = /^\/([\w-]+)$/.exec(text.trim());
  return m ? m[1].toLowerCase() : null;
}

const GLYPHS = ["·", "✢", "✳", "✶", "✻", "✽", "✻", "✶", "✳", "✢"];
const VERBS = ["Composing", "Pondering", "Sketching", "Arranging", "Tinkering", "Considering", "Typesetting",
  "Balancing", "Refining", "Kerning", "Harmonizing", "Polishing"];
const PHASE_VERBS: Record<string, string> = { previewing: "Previewing", finishing: "Wrapping up" };

/** The assistant is working: one quiet animated line, never the commands it runs. */
function Working({ phase }: { phase: string | null }) {
  const [tick, setTick] = useState(0);
  const [verb, setVerb] = useState(() => Math.floor(Math.random() * VERBS.length));
  const [started] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setTick((x) => x + 1), 120);
    const v = setInterval(() => setVerb((x) => (x + 1 + Math.floor(Math.random() * 3)) % VERBS.length), 3200);
    return () => { clearInterval(t); clearInterval(v); };
  }, []);
  const secs = Math.floor((Date.now() - started) / 1000);
  const word = (phase && PHASE_VERBS[phase]) || VERBS[verb];
  return (
    <div className="working" role="status" aria-live="polite">
      <span className="working-glyph">{GLYPHS[tick % GLYPHS.length]}</span>
      <span className="working-verb">{word}…</span>
      {secs >= 3 && <span className="working-time">{secs}s</span>}
    </div>
  );
}
