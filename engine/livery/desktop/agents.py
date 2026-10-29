"""Agent adapters: Claude Code, Codex, and a deterministic fake for tests.

Every adapter offers the same capabilities: connection status, session creation, message
submission with streamed events, cancellation and session restoration. Provider output is
normalized into these events (dicts with "type"):

    status      {"state": "connecting" | "ready", "detail"}
    text_delta  {"text"}                     streamed assistant text
    text        {"text"}                     a complete assistant message
    tool        {"name", "detail"}           tool activity (file edited, command run)
    approval    {"id", "title", "detail"}    the agent asks for permission
    done        {"result"}                   the turn finished
    error       {"kind", "message", "recoverable"}   kind: auth | quota | cancelled | exited | failed

Agents always run in the agent sandbox (sandbox.agent_command) with their own Livery
profile: Claude Code with CLAUDE_CONFIG_DIR, Codex with CODEX_HOME. They never see the
user's own agent configuration, plugins, hooks or MCP servers.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any, Callable

from . import sandbox
from .logs import log
from .netbridge import ALLOWLISTS, EgressProxy
from .paths import app_home, profiles_dir

Emit = Callable[[dict[str, Any]], None]
LOGIN_TIMEOUT = 600  # seconds a browser sign-in may stay open
_LOGINS: dict[str, subprocess.Popen] = {}
_LOGINS_LOCK = threading.Lock()
SANDBOX_PROFILE = {"claude": f"{sandbox.SANDBOX_HOME}/.livery-claude",
                   "codex": f"{sandbox.SANDBOX_HOME}/.livery-codex",
                   "fake": f"{sandbox.SANDBOX_HOME}/.livery-fake"}


def _is_elf(p: Path) -> bool:
    try:
        with open(p, "rb") as f:
            return f.read(4) == b"\x7fELF"
    except OSError:
        return False


def _find_binary(name: str, extra: list[str]) -> Path | None:
    """The real executable of an installed agent CLI. Wrapper scripts (version managers,
    shims) do not work inside the sandbox, so only native executables count."""
    env = os.environ.get(f"LIVERY_{name.upper()}_BIN")
    candidates = [Path(env)] if env else []
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if d:
            candidates.append(Path(d) / name)
    home = Path.home()
    for pattern in extra:
        candidates += sorted(home.glob(pattern), reverse=True)
    for c in candidates:
        try:
            rp = c.resolve()
        except OSError:
            continue
        if rp.is_file() and os.access(rp, os.X_OK) and _is_elf(rp):
            return rp
    return None


def _classify(message: str) -> tuple[str, bool]:
    m = message.lower()
    if any(k in m for k in ("login", "log in", "not logged", "authentication", "unauthorized", "invalid api key",
                            "401", "oauth", "credentials", "/login")):
        return "auth", True
    if any(k in m for k in ("rate limit", "quota", "usage limit", "429", "credit balance", "exceeded")):
        return "quota", True
    return "failed", True


class AgentSession:
    """One conversation with an agent. `send` starts a turn; events arrive through `emit`."""

    def __init__(self, adapter: "Adapter", workspace: Path, emit: Emit, resume: str | None):
        self.adapter = adapter
        self.workspace = workspace
        self.emit = emit
        self.session_id = resume
        self.busy = False
        self.proxy: EgressProxy | None = None
        self.denied_hosts: list[str] = []

    def _start_proxy(self) -> Path | None:
        allow = ALLOWLISTS.get(self.adapter.provider, ())
        if not allow:
            return None
        # unix socket paths are limited to 108 bytes: a short private folder, not the app home
        runtime = os.environ.get("XDG_RUNTIME_DIR")
        base = runtime if runtime and len(runtime) < 40 and os.path.isdir(runtime) else None
        sock = Path(tempfile.mkdtemp(prefix="we-px-", dir=base)) / "proxy.sock"

        def on_connect(host: str, port: int, ok: bool) -> None:
            if not ok:
                log.info("%s proxy blocked %s:%s", self.adapter.provider, host, port)
            if not ok and f"{host}:{port}" not in self.denied_hosts:  # once per host, not per retry
                self.denied_hosts.append(f"{host}:{port}")
                self.emit({"type": "tool", "name": "network", "detail": f"blocked connection to {host}:{port}"})
        self.proxy = EgressProxy(sock, allow, on_connect)
        self.proxy.start()
        return sock

    def close(self) -> None:
        self.cancel()
        if self.proxy:
            self.proxy.stop()
            shutil.rmtree(self.proxy.socket_path.parent, ignore_errors=True)
            self.proxy = None

    # subclasses
    def send(self, text: str, images: list[str] = ()) -> None:
        """Start a turn. `images` are workspace paths of reference images the prompt names."""
        raise NotImplementedError

    def cancel(self) -> None:
        raise NotImplementedError

    def respond_approval(self, approval_id: str, allow: bool) -> None:
        raise KeyError(approval_id)


class Adapter:
    provider = ""
    title = ""
    company = ""

    def binary(self) -> Path | None:
        raise NotImplementedError

    def profile(self) -> Path:
        p = profiles_dir() / self.provider
        p.mkdir(parents=True, exist_ok=True)
        os.chmod(p, 0o700)
        return p

    def auth_env(self) -> dict[str, str]:
        f = self.profile() / "livery-auth.json"
        try:
            data = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            return {}
        return {k: v for k, v in data.items() if isinstance(v, str)}

    def save_secret(self, env_name: str, value: str) -> None:
        f = self.profile() / "livery-auth.json"
        data = self.auth_env()
        if value:
            data[env_name] = value.strip()
        else:
            data.pop(env_name, None)
        f.write_text(json.dumps(data))
        os.chmod(f, 0o600)

    def sandbox_argv(self, argv: list[str], workspace: Path, proxy: Path | None, env: dict[str, str]) -> list[str]:
        b = self.binary()
        ro = [b.parent] if b else []
        return sandbox.agent_command(argv, workspace=workspace, profile=self.profile(),
                                     profile_mount=SANDBOX_PROFILE[self.provider], proxy_socket=proxy,
                                     env=env, ro=ro)

    def status(self) -> dict[str, Any]:
        raise NotImplementedError

    def login_help(self) -> dict[str, Any]:
        raise NotImplementedError

    def login_argv(self) -> list[str] | None:
        """The official browser sign-in, run outside the sandbox against Livery's profile."""
        return None

    def login_env(self) -> dict[str, str]:
        return {}

    def connect(self) -> None:
        """Start the browser sign-in in the background. The CLI opens the browser and waits for
        the OAuth callback on localhost; status() reports the result once it lands."""
        argv = self.login_argv()
        if argv is None:
            raise RuntimeError(f"{self.title} has no browser sign-in")
        with _LOGINS_LOCK:
            old = _LOGINS.pop(self.provider, None)
            if old and old.poll() is None:
                os.killpg(old.pid, signal.SIGTERM)
            out = open(self.profile() / "livery-login.log", "wb")
            proc = subprocess.Popen(argv, env={**os.environ, **self.login_env()}, stdin=subprocess.DEVNULL,
                                    stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
            out.close()
            _LOGINS[self.provider] = proc
        log.info("%s: browser sign-in started (pid %d)", self.provider, proc.pid)

        def expire() -> None:  # an abandoned sign-in must not hold the callback port forever
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGTERM)
        threading.Timer(LOGIN_TIMEOUT, expire).start()

    def start(self, workspace: Path, emit: Emit, resume: str | None = None) -> AgentSession:
        raise NotImplementedError

    def _base_status(self) -> dict[str, Any]:
        b = self.binary()
        sb = sandbox.check()
        st: dict[str, Any] = {"provider": self.provider, "title": self.title, "company": self.company,
                              "installed": b is not None, "binary": str(b) if b else None,
                              "sandbox": sb.ok, "sandbox_message": sb.message, "profile": str(self.profile())}
        if b is not None:
            r = subprocess.run([str(b), "--version"], capture_output=True, text=True, timeout=20)
            st["version"] = (r.stdout or r.stderr).strip().splitlines()[0] if (r.stdout or r.stderr).strip() else ""
        return st


# ====================================================================== Claude Code
class ClaudeAdapter(Adapter):
    provider = "claude"
    title = "Claude Code"
    company = "Anthropic"

    def binary(self) -> Path | None:
        return _find_binary("claude", [".local/share/mise/installs/claude/*/claude", ".claude/local/claude",
                                       ".local/share/claude/versions/*"])

    def env(self) -> dict[str, str]:
        env = {"CLAUDE_CONFIG_DIR": SANDBOX_PROFILE["claude"], "DISABLE_AUTOUPDATER": "1",
               "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_TELEMETRY": "1"}
        env.update({k: v for k, v in self.auth_env().items() if k in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")})
        return env

    def status(self) -> dict[str, Any]:
        st = self._base_status()
        st["logged_in"] = False
        st["auth_method"] = "none"
        if st["installed"] and st["sandbox"]:
            try:
                r = subprocess.run(self.sandbox_argv([str(self.binary()), "auth", "status", "--json"],
                                                     self._scratch_workspace(), None, self.env()),
                                   capture_output=True, text=True, timeout=30)
                data = json.loads(r.stdout)
                st["logged_in"] = bool(data.get("loggedIn"))
                st["auth_method"] = data.get("authMethod", "none")
                st["account"] = data.get("email")
            except (json.JSONDecodeError, subprocess.TimeoutExpired, OSError) as e:
                st["status_error"] = str(e)[:200]
        if not st["logged_in"] and self.auth_env():
            st["logged_in"] = True
            st["auth_method"] = "api_key" if "ANTHROPIC_API_KEY" in self.auth_env() else "oauth_token"
        return st

    def _scratch_workspace(self) -> Path:
        p = app_home() / "run" / "status-claude"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def login_help(self) -> dict[str, Any]:
        return {"provider": self.provider, "connect": self.binary() is not None,
                "secrets": [{"name": "CLAUDE_CODE_OAUTH_TOKEN", "label": "Long-lived token (setup-token)"},
                            {"name": "ANTHROPIC_API_KEY", "label": "Anthropic API key"}]}

    def login_argv(self) -> list[str] | None:
        b = self.binary()
        return [str(b), "auth", "login", "--claudeai"] if b else None

    def login_env(self) -> dict[str, str]:
        return {"CLAUDE_CONFIG_DIR": str(self.profile())}

    def start(self, workspace: Path, emit: Emit, resume: str | None = None) -> AgentSession:
        return ClaudeSession(self, workspace, emit, resume)


class ClaudeSession(AgentSession):
    """One `claude -p` process per turn; the conversation continues with --resume."""

    def __init__(self, adapter: ClaudeAdapter, workspace: Path, emit: Emit, resume: str | None):
        super().__init__(adapter, workspace, emit, resume)
        self.proc: subprocess.Popen | None = None
        self._cancelled = False

    def send(self, text: str, images: list[str] = ()) -> None:
        # reference images are named in the prompt; Claude Code's Read tool shows them
        if self.busy:
            raise RuntimeError("the agent is still working on the previous message")
        a: ClaudeAdapter = self.adapter  # type: ignore[assignment]
        b = a.binary()
        if b is None:
            raise RuntimeError("Claude Code is not installed")
        first = self.session_id is None
        if first:
            self.session_id = str(uuid.uuid4())
        argv = [str(b), "-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages",
                "--permission-mode", "acceptEdits",
                "--tools", "Read,Edit,Write,Glob,Grep,Bash",
                "--allowedTools", "Read,Edit,Write,Glob,Grep,Bash(render-preview:*),Bash(render-preview)",
                # no settings files at all (the agent could write one into its workspace or profile),
                # no MCP servers, no skills or slash commands
                "--setting-sources", "", "--strict-mcp-config", "--disable-slash-commands"]
        argv += ["--session-id", self.session_id] if first else ["--resume", self.session_id]
        sock = self.proxy.socket_path if self.proxy else self._start_proxy()
        cmd = a.sandbox_argv(argv, self.workspace, sock, a.env())
        self.busy = True
        self._cancelled = False
        self.emit({"type": "status", "state": "connecting", "detail": "starting Claude Code"})
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, start_new_session=True)
        assert self.proc.stdin is not None
        self.proc.stdin.write(text)
        self.proc.stdin.close()
        threading.Thread(target=self._read, args=(self.proc,), daemon=True, name="claude-turn").start()

    def _read(self, proc: subprocess.Popen) -> None:
        assert proc.stdout is not None and proc.stderr is not None
        stderr_lines: list[str] = []
        threading.Thread(target=lambda: stderr_lines.extend(proc.stderr.read().splitlines()[-40:]), daemon=True).start()
        finished = False
        for line in proc.stdout:
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            finished = self._handle(ev) or finished
        rc = proc.wait()
        if proc is not self.proc:
            return  # a later turn owns the session state now
        self.busy = False
        if self._cancelled:
            self.emit({"type": "error", "kind": "cancelled", "message": "Cancelled.", "recoverable": True})
        elif not finished:
            log.warning("%s exited with code %s without a result; stderr tail:\n%s", self.adapter.provider, rc,
                        "\n".join(stderr_lines[-20:]))
            msg = "\n".join(stderr_lines[-6:]) or f"Claude Code exited with code {rc}"
            kind, rec = _classify(msg)
            self.emit({"type": "error", "kind": kind if kind != "failed" else "exited", "message": msg[:1200],
                       "recoverable": rec})

    def _handle(self, ev: dict[str, Any]) -> bool:
        t = ev.get("type")
        if t == "system" and ev.get("subtype") == "init":
            self.emit({"type": "status", "state": "ready", "detail": ev.get("model", "")})
        elif t == "stream_event":
            inner = ev.get("event", {})
            delta = inner.get("delta", {})
            if inner.get("type") == "content_block_delta" and delta.get("type") == "text_delta":
                self.emit({"type": "text_delta", "text": delta.get("text", "")})
        elif t == "assistant":
            for block in ev.get("message", {}).get("content", []):
                if block.get("type") == "text" and block.get("text"):
                    self.emit({"type": "text", "text": block["text"]})
                elif block.get("type") == "tool_use":
                    inp = block.get("input", {}) or {}
                    detail = inp.get("file_path") or inp.get("command") or inp.get("pattern") or inp.get("path") or ""
                    self.emit({"type": "tool", "name": block.get("name", "tool"),
                               "detail": str(detail).replace("/work/", "")[:160]})
        elif t == "result":
            self.busy = False  # before the final event: listeners may start the next turn
            if ev.get("is_error") or ev.get("subtype", "success") != "success":
                msg = str(ev.get("result") or ev.get("subtype") or "the turn failed")
                kind, rec = _classify(msg)
                self.emit({"type": "error", "kind": kind, "message": msg[:1200], "recoverable": rec})
            else:
                self.emit({"type": "done", "result": str(ev.get("result", ""))[:4000]})
            return True
        return False

    def cancel(self) -> None:
        p = self.proc
        if p is not None and p.poll() is None:
            self._cancelled = True
            try:
                os.killpg(p.pid, signal.SIGTERM)
                p.wait(timeout=5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


# ====================================================================== Codex
class CodexAdapter(Adapter):
    provider = "codex"
    title = "Codex"
    company = "OpenAI"

    def binary(self) -> Path | None:
        return _find_binary("codex", [".local/share/mise/installs/codex/*/bin/codex",
                                      ".local/share/mise/installs/codex/*/codex",
                                      ".npm-global/lib/node_modules/@openai/codex/vendor/x86_64-unknown-linux-musl/codex/codex"])

    def env(self) -> dict[str, str]:
        env = {"CODEX_HOME": SANDBOX_PROFILE["codex"], "RUST_LOG": "error"}
        env.update({k: v for k, v in self.auth_env().items() if k == "OPENAI_API_KEY"})
        return env

    def status(self) -> dict[str, Any]:
        st = self._base_status()
        prof = self.profile()
        st["logged_in"] = (prof / "auth.json").exists() or "OPENAI_API_KEY" in self.auth_env()
        st["auth_method"] = "chatgpt" if (prof / "auth.json").exists() else (
            "api_key" if "OPENAI_API_KEY" in self.auth_env() else "none")
        return st

    def login_help(self) -> dict[str, Any]:
        return {"provider": self.provider, "connect": self.binary() is not None,
                "secrets": [{"name": "OPENAI_API_KEY", "label": "OpenAI API key"}]}

    def login_argv(self) -> list[str] | None:
        b = self.binary()
        return [str(b), "login"] if b else None

    def login_env(self) -> dict[str, str]:
        return {"CODEX_HOME": str(self.profile())}

    def start(self, workspace: Path, emit: Emit, resume: str | None = None) -> AgentSession:
        return CodexSession(self, workspace, emit, resume)


class CodexSession(AgentSession):
    """A long-lived `codex app-server` speaking JSON-RPC over stdio."""

    def __init__(self, adapter: CodexAdapter, workspace: Path, emit: Emit, resume: str | None):
        super().__init__(adapter, workspace, emit, resume)
        self.proc: subprocess.Popen | None = None
        self._next = 0
        self._pending: dict[int, tuple[threading.Event, dict[str, Any]]] = {}
        self._approvals: dict[str, Any] = {}
        self.turn_id: str | None = None
        self._lock = threading.Lock()
        self._stderr: list[str] = []
        self._cancelled = False

    # ---------------------------------------------------------------- rpc plumbing
    def _write(self, msg: dict[str, Any]) -> None:
        assert self.proc is not None and self.proc.stdin is not None
        with self._lock:
            self.proc.stdin.write(json.dumps(msg) + "\n")
            self.proc.stdin.flush()

    def _request(self, method: str, params: dict[str, Any], timeout: float = 60) -> dict[str, Any]:
        self._next += 1
        rid = self._next
        done = threading.Event()
        box: dict[str, Any] = {}
        self._pending[rid] = (done, box)
        self._write({"id": rid, "method": method, "params": params})
        if not done.wait(timeout):
            raise RuntimeError(f"Codex did not answer {method}")
        if "error" in box:
            raise RuntimeError(f"{method}: {box['error'].get('message', box['error'])}")
        return box.get("result", {})

    def _ensure_started(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            return
        a: CodexAdapter = self.adapter  # type: ignore[assignment]
        b = a.binary()
        if b is None:
            raise RuntimeError("Codex is not installed")
        sock = self.proxy.socket_path if self.proxy else self._start_proxy()
        # Ignore integrations the agent might write into its own profile between turns.
        overrides = ["-c", "mcp_servers={}", "-c", "notify=[]"]
        cmd = a.sandbox_argv([str(b), "app-server", *overrides], self.workspace, sock, a.env())
        self.emit({"type": "status", "state": "connecting", "detail": "starting Codex"})
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, bufsize=1, start_new_session=True)
        threading.Thread(target=self._read, daemon=True, name="codex-rpc").start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        self._request("initialize", {"clientInfo": {"name": "livery", "title": "Livery", "version": "1.0"},
                                     "capabilities": {"experimentalApi": False, "requestAttestation": False}})
        self._write({"method": "initialized"})
        account = self._request("account/read", {})
        if account.get("requiresOpenaiAuth") and not account.get("account"):
            raise PermissionError("Codex is not signed in to the Livery profile")
        params = {"cwd": "/work", "approvalPolicy": "never", "sandbox": "danger-full-access"}
        if self.session_id:
            try:
                res = self._request("thread/resume", {"threadId": self.session_id, **params})
            except RuntimeError:
                res = self._request("thread/start", params)
        else:
            res = self._request("thread/start", params)
        self.session_id = res["thread"]["id"]
        self.emit({"type": "status", "state": "ready", "detail": res.get("model", "")})

    def _read_stderr(self) -> None:
        assert self.proc is not None and self.proc.stderr is not None
        for line in self.proc.stderr:
            self._stderr = (self._stderr + [line.rstrip()])[-40:]

    def _read(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        for line in self.proc.stdout:
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "id" in msg and ("result" in msg or "error" in msg):
                entry = self._pending.pop(msg["id"], None)
                if entry:
                    entry[1].update(msg)
                    entry[0].set()
            elif "id" in msg and "method" in msg:
                self._server_request(msg)
            elif "method" in msg:
                self._notification(msg["method"], msg.get("params", {}))
        rc = self.proc.wait() if self.proc else -1
        for done, box in list(self._pending.values()):
            box["error"] = {"message": "Codex exited"}
            done.set()
        log.info("codex app-server exited with code %s", rc)
        if self.busy:
            self.busy = False
            log.warning("codex exited during a turn; stderr tail:\n%s", "\n".join(self._stderr[-20:]))
            msg = "\n".join(self._stderr[-6:]) or f"Codex exited with code {rc}"
            kind, rec = _classify(msg)
            self.emit({"type": "error", "kind": "cancelled" if self._cancelled else (kind if kind != "failed" else "exited"),
                       "message": msg[:1200], "recoverable": rec})

    def _server_request(self, msg: dict[str, Any]) -> None:
        method, params = msg["method"], msg.get("params", {})
        if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval",
                      "execCommandApproval", "applyPatchApproval", "item/permissions/requestApproval"):
            aid = str(msg["id"])
            self._approvals[aid] = msg
            detail = params.get("command") or params.get("reason") or ""
            self.emit({"type": "approval", "id": aid, "title": method.split("/")[-1],
                       "detail": str(detail).replace("/work/", "")[:300]})
        else:
            self._write({"id": msg["id"], "error": {"code": -32601, "message": "not supported by Livery"}})

    def respond_approval(self, approval_id: str, allow: bool) -> None:
        msg = self._approvals.pop(approval_id)
        decision = "accept" if allow else "decline"
        if msg["method"] in ("execCommandApproval", "applyPatchApproval"):
            decision = "approved" if allow else "denied"
        self._write({"id": msg["id"], "result": {"decision": decision}})

    def _notification(self, method: str, p: dict[str, Any]) -> None:
        if method == "item/agentMessage/delta":
            self.emit({"type": "text_delta", "text": p.get("delta", "")})
        elif method == "item/completed":
            item = p.get("item", {})
            if item.get("type") == "agentMessage":
                self.emit({"type": "text", "text": item.get("text", "")})
        elif method == "item/started":
            item = p.get("item", {})
            if item.get("type") == "commandExecution":
                self.emit({"type": "tool", "name": "shell", "detail": str(item.get("command", ""))[:160]})
            elif item.get("type") == "fileChange":
                paths = [c.get("path", "") for c in item.get("changes", [])]
                self.emit({"type": "tool", "name": "edit",
                           "detail": ", ".join(x.replace("/work/", "") for x in paths)[:160]})
        elif method == "turn/started":
            self.turn_id = p.get("turn", {}).get("id")
        elif method == "turn/completed":
            turn = p.get("turn", {})
            self.busy = False
            self.turn_id = None
            status = turn.get("status")
            if status == "completed":
                self.emit({"type": "done", "result": ""})
            elif status == "interrupted":
                self.emit({"type": "error", "kind": "cancelled", "message": "Cancelled.", "recoverable": True})
            else:
                err = turn.get("error") or {}
                msg = err.get("message") or json.dumps(err)[:400] or "the turn failed"
                kind, rec = _classify(msg)
                self.emit({"type": "error", "kind": kind, "message": msg, "recoverable": rec})
        elif method == "error":
            err = p.get("error", {})
            if not p.get("willRetry"):
                msg = err.get("message", "error")
                kind, rec = _classify(msg)
                self.emit({"type": "tool", "name": "error", "detail": f"{kind}: {msg[:200]}"})

    def send(self, text: str, images: list[str] = ()) -> None:
        if self.busy:
            raise RuntimeError("the agent is still working on the previous message")
        self._cancelled = False
        self._ensure_started()
        self.busy = True
        items: list[dict[str, Any]] = [{"type": "text", "text": text, "text_elements": []}]
        items += [{"type": "localImage", "path": f"/work/{p}"} for p in images]
        res = self._request("turn/start", {"threadId": self.session_id, "input": items})
        self.turn_id = res.get("turn", {}).get("id")

    def cancel(self) -> None:
        if self.busy and self.turn_id and self.proc and self.proc.poll() is None:
            self._cancelled = True
            try:
                self._request("turn/interrupt", {"threadId": self.session_id, "turnId": self.turn_id}, timeout=10)
            except RuntimeError:
                self.kill()

    def kill(self) -> None:
        p = self.proc
        if p is not None and p.poll() is None:
            try:
                os.killpg(p.pid, signal.SIGTERM)
                p.wait(timeout=5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def close(self) -> None:
        super().close()
        self.kill()


# ====================================================================== fake agent
FAKE_AGENT = Path(__file__).resolve().parent / "fake_agent.py"


class FakeAdapter(Adapter):
    """A deterministic agent for automated tests. It runs in the same agent sandbox as the
    real ones and speaks the Claude Code stream-json format, so it exercises the same paths."""
    provider = "fake"
    title = "Test agent"
    company = "Livery"

    def binary(self) -> Path | None:
        return Path(sys.executable).resolve()

    def env(self) -> dict[str, str]:
        return {"FAKE_PROFILE": SANDBOX_PROFILE["fake"]}

    def status(self) -> dict[str, Any]:
        st = self._base_status()
        st.update(logged_in=True, auth_method="none", version="fake 1")
        return st

    def login_help(self) -> dict[str, Any]:
        return {"provider": self.provider, "connect": False, "secrets": []}

    def start(self, workspace: Path, emit: Emit, resume: str | None = None) -> AgentSession:
        return FakeSession(self, workspace, emit, resume)

    def sandbox_argv(self, argv: list[str], workspace: Path, proxy: Path | None, env: dict[str, str]) -> list[str]:
        ro = [Path(sys.executable).resolve().parent] if getattr(sys, "frozen", False) else \
            [Path(sys.executable).resolve().parent, Path(sys.prefix), Path(sys.base_prefix), FAKE_AGENT.parent]
        if getattr(sys, "frozen", False):  # the packaged fake-agent subcommand checks it
            env = {**env, "LIVERY_ENABLE_FAKE_AGENT": "1"}
        return sandbox.agent_command(argv, workspace=workspace, profile=self.profile(),
                                     profile_mount=SANDBOX_PROFILE["fake"], proxy_socket=proxy, env=env, ro=ro)


class FakeSession(ClaudeSession):
    def send(self, text: str, images: list[str] = ()) -> None:
        if self.busy:
            raise RuntimeError("the agent is still working on the previous message")
        a: FakeAdapter = self.adapter  # type: ignore[assignment]
        if self.session_id is None:
            self.session_id = str(uuid.uuid4())
        if getattr(sys, "frozen", False):  # the packaged engine carries the fake agent as a subcommand
            argv = [str(Path(sys.executable).resolve()), "fake-agent", "--session", self.session_id]
        else:
            argv = [str(Path(sys.executable).resolve()), "-I", str(FAKE_AGENT), "--session", self.session_id]
        cmd = a.sandbox_argv(argv, self.workspace, None, a.env())
        self.busy = True
        self._cancelled = False
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, start_new_session=True)
        assert self.proc.stdin is not None
        self.proc.stdin.write(text)
        self.proc.stdin.close()
        threading.Thread(target=self._read, args=(self.proc,), daemon=True).start()


ADAPTERS: dict[str, Adapter] = {"claude": ClaudeAdapter(), "codex": CodexAdapter(), "fake": FakeAdapter()}


def adapter(provider: str) -> Adapter:
    if provider not in ADAPTERS:
        raise KeyError(provider)
    if provider == "fake" and os.environ.get("LIVERY_ENABLE_FAKE_AGENT") != "1":
        raise KeyError(provider)
    return ADAPTERS[provider]


def providers() -> list[str]:
    out = ["claude", "codex"]
    if os.environ.get("LIVERY_ENABLE_FAKE_AGENT") == "1":
        out.append("fake")
    return out

