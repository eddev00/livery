// Livery desktop: main process.
//
// Starts the local engine (Python/FastAPI) as a child process, hands it a fresh access
// token on stdin and talks to it over 127.0.0.1. The renderer never sees the token or the
// port: it is a sandboxed page with context isolation that reaches the engine only through
// the narrow preload API below, which this process checks and forwards.
import { app, BrowserWindow, clipboard, dialog, ipcMain, Menu, session, shell, type WebContents } from "electron";
import { spawn, type ChildProcess } from "node:child_process";
import { randomBytes } from "node:crypto";
import { appendFileSync, existsSync, mkdirSync, renameSync, statSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import readline from "node:readline";

// ------------------------------------------------------------------ log file
// <app home>/logs/app.log, next to the engine's engine.log. Rotated at 2 MB.
export function logsDir(): string {
  const home = process.env.LIVERY_HOME
    ?? path.join(process.env.XDG_DATA_HOME ?? path.join(os.homedir(), ".local", "share"), "livery");
  return path.join(home, "logs");
}

function appLog(level: string, message: string): void {
  try {
    const dir = logsDir();
    mkdirSync(dir, { recursive: true });
    const file = path.join(dir, "app.log");
    if (existsSync(file) && statSync(file).size > 2_000_000) renameSync(file, file + ".1");
    const stamp = new Date().toISOString().replace("T", " ").slice(0, 23);
    appendFileSync(file, message.split("\n").map((l) => `${stamp} ${level.padEnd(7)} ${l}`).join("\n") + "\n");
  } catch {
    /* logging must never break the app */
  }
}

type Engine = { proc: ChildProcess; port: number | null; token: string; error: string | null; log: string[] };

let engine: Engine | null = null;
let win: BrowserWindow | null = null;
const subscriptions = new Map<string, AbortController>();

// ------------------------------------------------------------------ engine process
function engineCommand(): { cmd: string; args: string[]; cwd: string; env: NodeJS.ProcessEnv } {
  const env: NodeJS.ProcessEnv = { ...process.env, LIVERY_IGNORE_SYSTEM_FONTS: "1" };
  for (const k of Object.keys(env)) if (k.startsWith("ELECTRON_")) delete env[k];
  if (app.isPackaged) {
    const res = process.resourcesPath;
    env.LIVERY_TOOLS_DIR = path.join(res, "tools", "bin");
    return { cmd: path.join(res, "engine", "livery-engine"), args: ["serve"], cwd: res, env };
  }
  if (process.env.LIVERY_ENGINE_CMD) {
    const [cmd, ...args] = JSON.parse(process.env.LIVERY_ENGINE_CMD) as string[];
    return { cmd, args, cwd: process.cwd(), env };
  }
  const engineDir = path.resolve(app.getAppPath(), "..", "engine");
  return { cmd: "uv", args: ["run", "--project", engineDir, "python", "-m", "livery.desktop", "serve"], cwd: engineDir, env };
}

function startEngine(): Promise<Engine> {
  const { cmd, args, cwd, env } = engineCommand();
  const token = randomBytes(32).toString("hex");
  appLog("INFO", `starting engine: ${cmd} ${args.join(" ")}`);
  const proc = spawn(cmd, args, { cwd, env, stdio: ["pipe", "pipe", "pipe"] });
  const e: Engine = { proc, port: null, token, error: null, log: [] };
  engine = e;
  proc.stdin!.write(token + "\n"); // stdin stays open: the engine exits when it closes
  proc.stderr!.on("data", (d: Buffer) => {
    for (const line of d.toString().split("\n").filter(Boolean)) appLog("ENGINE", line);
    e.log.push(...d.toString().split("\n").filter(Boolean));
    e.log = e.log.slice(-200);
  });
  return new Promise((resolve) => {
    const rl = readline.createInterface({ input: proc.stdout! });
    const timer = setTimeout(() => {
      e.error = "The engine did not start within 60 seconds.\n" + e.log.slice(-10).join("\n");
      resolve(e);
    }, 60_000);
    rl.once("line", (line) => {
      clearTimeout(timer);
      try {
        e.port = (JSON.parse(line) as { port: number }).port;
        appLog("INFO", `engine ready on port ${e.port}`);
      } catch {
        e.error = "Unexpected engine output: " + line;
        appLog("ERROR", e.error);
      }
      resolve(e);
      rl.on("line", (more) => appLog("ENGINE", more));
    });
    proc.on("error", (err) => {
      clearTimeout(timer);
      appLog("ERROR", `could not start the engine (${cmd}): ${err.message}`);
      e.error = `Could not start the engine (${cmd}): ${err.message}`;
      resolve(e);
    });
    proc.on("exit", (code, signal) => {
      clearTimeout(timer);
      appLog(quitting ? "INFO" : "ERROR", `engine exited (code ${code}, signal ${signal})`);
      if (e.port === null && !e.error) e.error = `The engine exited (code ${code}).\n` + e.log.slice(-10).join("\n");
      else if (!quitting) {
        e.error = `The engine stopped unexpectedly (code ${code}).`;
        win?.webContents.send("engine:event", { type: "engine", state: "stopped", error: e.error });
      }
      resolve(e);
    });
  });
}

let quitting = false;
function stopEngine(): void {
  quitting = true;
  for (const c of subscriptions.values()) c.abort();
  const p = engine?.proc;
  if (!p || p.exitCode !== null) return;
  p.stdin?.end(); // asks the engine to shut down (and stop every agent it started)
  const t = setTimeout(() => p.kill("SIGTERM"), 4000);
  p.once("exit", () => clearTimeout(t));
}

async function call(method: string, apiPath: string, body?: unknown): Promise<{ status: number; data: unknown }> {
  if (!engine || engine.port === null) return { status: 503, data: { detail: engine?.error ?? "engine not running" } };
  const res = await fetch(`http://127.0.0.1:${engine.port}${apiPath}`, {
    method,
    headers: { Authorization: `Bearer ${engine.token}`, ...(body !== undefined ? { "Content-Type": "application/json" } : {}) },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  let data: unknown = text;
  try {
    data = JSON.parse(text);
  } catch {
    /* plain text */
  }
  return { status: res.status, data };
}

async function subscribe(projectId: string, target: WebContents): Promise<void> {
  subscriptions.get(projectId)?.abort();
  const ctl = new AbortController();
  subscriptions.set(projectId, ctl);
  while (!ctl.signal.aborted && engine?.port) {
    try {
      const res = await fetch(`http://127.0.0.1:${engine.port}/api/projects/${projectId}/events`, {
        headers: { Authorization: `Bearer ${engine.token}` },
        signal: ctl.signal,
      });
      const reader = res.body!.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let i: number;
        while ((i = buf.indexOf("\n\n")) >= 0) {
          const chunk = buf.slice(0, i);
          buf = buf.slice(i + 2);
          for (const line of chunk.split("\n")) {
            if (line.startsWith("data: ") && !target.isDestroyed()) target.send("engine:event", JSON.parse(line.slice(6)));
          }
        }
      }
    } catch {
      /* reconnect below */
    }
    if (!ctl.signal.aborted) await new Promise((r) => setTimeout(r, 1500));
  }
}

// ------------------------------------------------------------------ IPC
function str(v: unknown, max = 4096): string {
  if (typeof v !== "string" || v.length > max) throw new Error("invalid argument");
  return v;
}

function registerIpc(): void {
  ipcMain.handle("engine:info", () => ({ ready: !!engine?.port, error: engine?.error ?? null, log: engine?.log.slice(-20) ?? [] }));
  ipcMain.handle("engine:restart", async () => {
    stopEngine();
    quitting = false;
    await startEngine();
    return { ready: !!engine?.port, error: engine?.error ?? null };
  });
  ipcMain.handle("api", async (_e, method: unknown, apiPath: unknown, body: unknown) => {
    const m = str(method, 8);
    const p = str(apiPath, 2048);
    if (!["GET", "POST", "PATCH", "DELETE"].includes(m) || !p.startsWith("/api/") || p.includes("..")) {
      return { status: 400, data: { detail: "invalid request" } };
    }
    try {
      const r = await call(m, p, body);
      if (r.status >= 500) appLog("ERROR", `${m} ${p} -> ${r.status}: ${JSON.stringify(r.data).slice(0, 500)}`);
      return r;
    } catch (err) {
      appLog("ERROR", `${m} ${p} failed: ${String(err)}`);
      return { status: 503, data: { detail: String(err) } };
    }
  });
  ipcMain.handle("api:pdf", async (_e, apiPath: unknown) => {
    const p = str(apiPath, 2048);
    if (!p.startsWith("/api/projects/") || !engine?.port) return null;
    const res = await fetch(`http://127.0.0.1:${engine.port}${p}`, { headers: { Authorization: `Bearer ${engine.token}` } });
    if (!res.ok) return null;
    return new Uint8Array(await res.arrayBuffer());
  });
  ipcMain.handle("events:subscribe", (e, projectId: unknown) => {
    const id = str(projectId, 40);
    if (!/^p[0-9a-f]+$/.test(id)) return false;
    for (const [k, c] of subscriptions) if (k !== id) c.abort();
    void subscribe(id, e.sender);
    return true;
  });
  ipcMain.handle("dialog:docx", async () => {
    const r = await dialog.showOpenDialog(win!, {
      title: "Import Word documents",
      properties: ["openFile", "multiSelections"],
      filters: [{ name: "Word documents", extensions: ["docx"] }],
    });
    return r.canceled ? [] : r.filePaths;
  });
  ipcMain.handle("dialog:folder", async (_e, title: unknown) => {
    const r = await dialog.showOpenDialog(win!, { title: str(title ?? "Choose a folder", 200), properties: ["openDirectory", "createDirectory"] });
    return r.canceled ? null : r.filePaths[0];
  });
  ipcMain.handle("shell:open", (_e, url: unknown) => {
    const u = new URL(str(url, 2048));
    if (u.protocol !== "https:") return false;
    void shell.openExternal(u.toString());
    return true;
  });
  ipcMain.handle("logs:open", () => {
    void shell.openPath(logsDir());
    return logsDir();
  });
  ipcMain.handle("shell:show", (_e, folder: unknown) => {
    void shell.openPath(str(folder, 4096));
    return true;
  });
  ipcMain.handle("clipboard:write", (_e, text: unknown) => {
    clipboard.writeText(str(text, 10_000));
    return true;
  });
}

// ------------------------------------------------------------------ window
const SMOKE = process.env.LIVERY_SMOKE; // folder for screenshots: run the end-to-end smoke test and quit

function createWindow(): void {
  win = new BrowserWindow({
    show: !SMOKE,
    paintWhenInitiallyHidden: true,
    width: 1480,
    height: 920,
    minWidth: 1100,
    minHeight: 680,
    title: "Livery",
    backgroundColor: "#f4f5f7",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      sandbox: true,
      contextIsolation: true,
      nodeIntegration: false,
      webviewTag: false,
      spellcheck: false,
    },
  });
  win.webContents.on("will-navigate", (e) => e.preventDefault());
  win.webContents.on("console-message", (e) => {
    if (e.level === "error" || e.level === "warning") appLog(e.level === "error" ? "UI-ERR" : "UI-WARN", `${e.message} (${e.sourceId}:${e.lineNumber})`);
  });
  win.webContents.on("render-process-gone", (_e, d) => appLog("ERROR", `renderer gone: ${d.reason} (exit ${d.exitCode})`));
  win.webContents.on("preload-error", (_e, p, err) => appLog("ERROR", `preload ${p}: ${err.message}`));
  win.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  const index = path.join(__dirname, "..", "renderer", "index.html");
  if (!existsSync(index)) throw new Error("renderer not built: run npm run build");
  void win.loadFile(index);
}

process.on("uncaughtException", (err) => appLog("ERROR", `uncaught: ${err.stack ?? err}`));
process.on("unhandledRejection", (err) => appLog("ERROR", `unhandled rejection: ${String(err)}`));

app.whenReady().then(async () => {
  Menu.setApplicationMenu(null); // no File / Edit / View / Window bar
  appLog("INFO", `Livery ${app.getVersion()} starting (electron ${process.versions.electron}, ${app.isPackaged ? "packaged" : "dev"})`);
  session.defaultSession.setPermissionRequestHandler((_wc, _perm, cb) => cb(false));
  session.defaultSession.webRequest.onBeforeRequest((details, cb) => {
    // The page may load its own files and nothing else.
    cb({ cancel: !(details.url.startsWith("file://") || details.url.startsWith("devtools://") || details.url.startsWith("blob:") || details.url.startsWith("data:")) });
  });
  registerIpc();
  createWindow();
  await startEngine();
  win?.webContents.send("engine:event", { type: "engine", state: engine?.port ? "ready" : "failed", error: engine?.error });
  if (SMOKE) void smoke(SMOKE).then((code) => { stopEngine(); setTimeout(() => app.exit(code), 1500); });
});

// ------------------------------------------------------------------ smoke test
// Creates a project, imports LIVERY_SMOKE_DOCS (":"-separated), waits until every document
// is rendered and verified, and saves screenshots of the real window. Exit code 0 on success.
async function smoke(out: string): Promise<number> {
  const { mkdirSync, writeFileSync } = await import("node:fs");
  mkdirSync(out, { recursive: true });
  const log = (m: string) => console.log(`[smoke] ${m}`);
  const shot = async (name: string) => {
    const img = await win!.webContents.capturePage();
    writeFileSync(path.join(out, name), img.toPNG());
    log(`screenshot ${name}`);
  };
  win!.webContents.on("console-message", (e) => log(`console: ${e.message}`));
  try {
    if (!engine?.port) throw new Error(engine?.error ?? "engine not running");
    const sys = await call("GET", "/api/system");
    log(`system ${JSON.stringify(sys.data)}`);
    const p = (await call("POST", "/api/projects", { name: "Smoke test" })).data as { id: string };
    const docs = (process.env.LIVERY_SMOKE_DOCS ?? "").split(":").filter(Boolean);
    const imported = await call("POST", `/api/projects/${p.id}/documents`, { paths: docs });
    log(`imported ${JSON.stringify(imported.data)}`);
    const deadline = Date.now() + 600_000;
    let list: { name: string; version: { status: string; error?: string }; build: { state: string } | null; report_status: string | null }[] = [];
    while (Date.now() < deadline) {
      list = (await call("GET", `/api/projects/${p.id}/documents`)).data as typeof list;
      const pending = list.filter((d) => d.version.status !== "failed" && d.build?.state !== "done" && d.build?.state !== "failed");
      const accepted = (imported.data as { error?: string }[]).filter((r) => !r.error).length;
      if (list.length === accepted && pending.length === 0) break;
      await new Promise((r) => setTimeout(r, 1000));
    }
    for (const d of list) log(`${d.name}: import=${d.version.status} build=${d.build?.state} report=${d.report_status} ${d.version.error ?? ""}`);
    await win!.webContents.executeJavaScript(`localStorage.setItem("we:project", ${JSON.stringify(p.id)})`);
    win!.webContents.reload(); // the page itself may not navigate (will-navigate is blocked)
    await new Promise((r) => setTimeout(r, 6000));
    log("page " + JSON.stringify(await win!.webContents.executeJavaScript(
      `({ ls: localStorage.getItem("we:project"), sel: document.querySelector(".project-select")?.value, docs: document.querySelectorAll(".docs li").length, opts: [...document.querySelectorAll(".project-select option")].map(o => o.value) })`)));
    await shot("01-workspace.png");
    // the report's "Show me", when the document has something to look at
    if (await win!.webContents.executeJavaScript(
      `(() => { const b = document.querySelector('.finding .btn'); if (!b) return false; b.click(); return true; })()`)) {
      await new Promise((r) => setTimeout(r, 1500));
      await shot("01-show-me.png");
    }
    // click the first table overlay, if any, and capture the selection state
    const clicked = await win!.webContents.executeJavaScript(
      `(() => { const el = document.querySelector('.el-table') || document.querySelector('.el'); if (!el) return false; el.click(); return true; })()`);
    await new Promise((r) => setTimeout(r, 800));
    await shot("02-selection.png");
    let ok = list.length === docs.length && list.every((d) => d.build?.state === "done" && d.report_status !== "FAIL") && clicked;
    if (ok && process.env.LIVERY_SMOKE_AGENT) ok = await smokeAgent(p.id, process.env.LIVERY_SMOKE_AGENT, out, log, shot);
    log(ok ? "PASS" : "FAIL");
    return ok ? 0 : 1;
  } catch (e) {
    log(`error ${String(e)} ${String((e as { cause?: unknown }).cause ?? "")} ${engine?.log.slice(-5).join(" / ")}`);
    try { await shot("error.png"); } catch { /* window gone */ }
    return 1;
  }
}

app.on("before-quit", stopEngine);
app.on("window-all-closed", () => app.quit());
process.on("exit", () => engine?.proc.kill("SIGTERM"));

// The design loop through the UI: send a request with the selected table attached, review the
// proposal, render it with real content, compare, accept, then export.
async function smokeAgent(projectId: string, provider: string, out: string, log: (m: string) => void,
                          shot: (n: string) => Promise<void>): Promise<boolean> {
  const js = (code: string) => win!.webContents.executeJavaScript(code);
  const clickText = (sel: string, text: string) => js(`(() => { const b = [...document.querySelectorAll(${JSON.stringify(sel)})].find(x => x.textContent.trim().startsWith(${JSON.stringify(text)})); if (!b || b.disabled) return false; b.click(); return true; })()`);
  const wait = async (what: string, fn: () => Promise<boolean>, ms = 240_000) => {
    const end = Date.now() + ms;
    while (Date.now() < end) {
      if (await fn()) return true;
      await new Promise((r) => setTimeout(r, 1000));
    }
    log(`timed out waiting for ${what}`);
    return false;
  };
  const proposals = async () => (await call("GET", `/api/projects/${projectId}/proposals`)).data as
    { id: string; status: string; real: Record<string, { state: string }> }[];
  const title = (await call("GET", "/api/agents")).data as { provider: string; title: string }[];
  const tab = title.find((a) => a.provider === provider)?.title ?? provider;
  if (!(await clickText(".chat-head .segmented button", tab))) { log("agent tab not found"); return false; }
  await new Promise((r) => setTimeout(r, 1500));
  await js(`(() => { const ta = document.querySelector('.composer textarea');
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set.call(ta, 'Give this table header the colour #AA3311 with white text');
    ta.dispatchEvent(new Event('input', { bubbles: true })); })()`);
  await new Promise((r) => setTimeout(r, 300));
  await js(`document.querySelector('.composer textarea').dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))`);
  if (!(await wait("a proposal", async () => (await proposals()).some((p) => p.status === "pending")))) return false;
  await new Promise((r) => setTimeout(r, 2500));
  await shot("03-proposal-synthetic.png");
  if (!(await clickText(".proposal-actions button", "Render with real content"))) { log("render button missing"); return false; }
  if (!(await wait("real renders", async () => {
    const p = (await proposals()).find((x) => x.status === "pending");
    return !!p && Object.values(p.real).every((r) => r.state === "done");
  }))) return false;
  await new Promise((r) => setTimeout(r, 1500));
  await clickText(".proposal-actions .segmented button", "Compare");
  await new Promise((r) => setTimeout(r, 3000));
  await shot("04-compare.png");
  const before = ((await call("GET", `/api/projects/${projectId}/design`)).data as { accepted: string }).accepted;
  if (!(await clickText(".proposal-actions button", "Accept"))) { log("accept disabled"); await shot("error-accept.png"); return false; }
  if (!(await wait("acceptance", async () => ((await call("GET", `/api/projects/${projectId}/design`)).data as { accepted: string }).accepted !== before))) return false;
  await new Promise((r) => setTimeout(r, 3000));
  await shot("05-accepted.png");
  const dest = path.join(out, "export");
  await call("POST", `/api/projects/${projectId}/export`, { path: dest });
  const exported = await wait("export", async () => {
    const jobs = (await call("GET", `/api/projects/${projectId}/jobs`)).data as { kind: string; state: string }[];
    return jobs.some((j) => j.kind === "export" && j.state === "done");
  });
  log(`export ${exported ? "done" : "missing"} → ${dest}`);
  return exported;
}
