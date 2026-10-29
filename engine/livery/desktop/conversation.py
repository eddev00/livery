"""The design conversation of a project: one active agent session at a time.

Conversations are kept separately per provider (chats/<provider>.jsonl and their own agent
session). Switching providers ends the running session and starts the other provider in a
fresh workspace built from the accepted design, with a short sanitized project summary.

After every turn the workspace design is compared with the revision it started from; a
change becomes (or updates) the session's pending proposal, rendered from the synthetic
samples at once. Real-content renders happen locally when the user asks for them.

The samples are converted with the workspace's mapping.toml (rec["samples_mapping"] is the
mapping they were made with, and the one design/design.json's references belong to). A
changed mapping regenerates them before the next preview and after the turn, carrying the
references along; the proposal then holds the workspace documents' references for the new
mapping and every other document's for the base mapping, which its real renders carry over.
"""
from __future__ import annotations

import dataclasses
import json
import shutil
import threading
from pathlib import Path
from typing import Any

from . import events, proposals, workspace
from .agents import AgentSession, adapter
from .design import Design, DesignError, merge_workspace, rekey
from ..mapping import MappingError
from .logs import log
from .store import Conflict, Project, new_id, now, read_json, write_json

MAX_SAMPLES = 12
# Bumped when workspaces built by older versions must be rebuilt (2: twin IDs aligned to real IDs,
# 3: mapping.toml, styles.json and Word pages).
WORKSPACE_FORMAT = 3
_active: dict[str, "Conversation"] = {}
_guard = threading.Lock()


def _state_file(project: Project) -> Path:
    return project.root / "chats" / "state.json"


def _chat_file(project: Project, provider: str) -> Path:
    return project.root / "chats" / f"{provider}.jsonl"


def record(project: Project, provider: str, entry: dict[str, Any]) -> None:
    """Append to the sanitized chat record: messages and short activity lines, never tool
    output or file contents."""
    f = _chat_file(project, provider)
    f.parent.mkdir(parents=True, exist_ok=True)
    with open(f, "a", encoding="utf-8") as out:
        out.write(json.dumps({"ts": now(), **entry}, ensure_ascii=False) + "\n")


def history(project: Project, provider: str, limit: int = 400) -> list[dict[str, Any]]:
    f = _chat_file(project, provider)
    if not f.exists():
        return []
    lines = f.read_text(encoding="utf-8").splitlines()[-limit:]
    return [json.loads(x) for x in lines if x.strip()]


def summary(project: Project, mapping: dict[str, str]) -> str:
    st = project.state()
    d = project.design(st["accepted"]).design
    treatments = ", ".join(f"“{n}” ({t.get('kind')})" for n, t in d.get("treatments", {}).items()) or "none yet"
    return (f"[Project summary] {len(st['order'])} document(s); {len(mapping)} sample(s) in this workspace "
            f"({', '.join(sorted(mapping.values()))}). Accepted design revision {st['accepted']}. "
            f"Treatments defined: {treatments}. Earlier conversations happened with another assistant; "
            "the design files reflect everything accepted so far.\n\n")


class Conversation:
    def __init__(self, project: Project, provider: str):
        self.project = project
        self.provider = provider
        self.adapter = adapter(provider)
        self.session: AgentSession | None = None
        self.watcher: workspace.RequestWatcher | None = None
        self.turn_text: list[str] = []
        self.lock = threading.Lock()
        states = read_json(_state_file(project), {"sessions": {}})
        self.rec: dict[str, Any] | None = states["sessions"].get(provider)

    # ---------------------------------------------------------------- persistence
    def _save(self) -> None:
        with self.project.lock:
            states = read_json(_state_file(self.project), {"sessions": {}})
            states["sessions"][self.provider] = self.rec
            states["active"] = self.provider
            write_json(_state_file(self.project), states)

    @property
    def ws(self) -> Path:
        assert self.rec is not None
        return Path(self.rec["dir"])

    # ---------------------------------------------------------------- lifecycle
    def ensure(self, docs: list[str]) -> None:
        """Have a workspace and an agent session; restore the previous one when possible."""
        all_docs = [d["id"] for d in self.project.documents()]
        if self.rec is not None and self.rec.get("format") != WORKSPACE_FORMAT:
            log.info("agent %s %s: rebuilding an outdated workspace", self.provider, self.project.id)
            self.rec = None
        if self.rec is None or not Path(self.rec["dir"]).is_dir():
            key = new_id("s")
            wanted = docs + [d for d in all_docs if d not in docs]
            info = workspace.create(self.project, key, wanted[:max(MAX_SAMPLES, len(docs))])
            self.rec = {"key": key, "dir": info["dir"], "docs": info["docs"], "base": info["base"],
                        "agent_session": None, "proposal": None, "created": now(), "needs_summary": True,
                        "format": WORKSPACE_FORMAT,
                        "samples_mapping": self.project.design(info["base"]).mapping_text}
            self._save()
        missing = [d for d in docs if d not in self.rec["docs"]]
        if missing:
            workspace.add_samples(self.project, self.ws, missing, self.rec["docs"], self._samples_design())
            self._sync_design(force=True)
            self._save()
        if self.session is None:
            def emit(ev: dict[str, Any]) -> None:
                self._on_event(ev)
            self.session = self.adapter.start(self.ws, emit, resume=self.rec.get("agent_session"))
            self.watcher = workspace.RequestWatcher(self.ws, self.ws.parent / f".render-{self.rec['key']}",
                                                    on_render=lambda r: self._publish({"type": "preview", "ok": r["ok"]}),
                                                    prepare=self._follow_mapping)
            self.watcher.start()

    def _sync_design(self, force: bool = False) -> None:
        """Keep the workspace design on the right base: the pending proposal of this session,
        or else the accepted design (after an accept, discard or undo elsewhere)."""
        assert self.rec is not None
        pid = self.rec.get("proposal")
        pending = None
        if pid:
            try:
                meta = proposals.get(self.project, pid)
                pending = meta if meta["status"] == "pending" and not meta["stale"] else None
            except KeyError:
                pending = None
        if pending is None:
            self.rec["proposal"] = None
        accepted = self.project.accepted()
        if pending is None and (force or self.rec["base"] != accepted or self._workspace_differs(accepted)):
            self._write_design(self.project.design(accepted))
            self.rec["base"] = accepted
        elif pending is not None and force:
            self._write_design(proposals.design_of(self.project, pid))

    def _samples_design(self) -> Design:
        """A design with the mapping the samples were made with (for its conversions)."""
        assert self.rec is not None
        base = self.project.design(self.rec["base"])
        return dataclasses.replace(base, mapping_text=self.rec.get("samples_mapping", base.mapping_text))

    def _follow_mapping(self) -> dict[str, Any]:
        """Bring the samples to the workspace's mapping.toml when it changed, carrying the
        workspace references along. Returns problems (an invalid mapping) and notes."""
        assert self.rec is not None
        try:
            current = workspace.read_design(self.ws)
            key = current.mapping_key()
        except MappingError as e:
            return {"problems": e.problems}
        except DesignError as e:
            return {"problems": e.problems}
        old = self._samples_design()
        if key == old.mapping_key():
            return {}
        log.info("agent %s %s: converting the samples with a changed mapping", self.provider, self.project.id)
        lost = workspace.sync_samples(self.project, self.ws, self.rec["docs"], current, refs_from=old)
        self.rec["samples_mapping"] = current.mapping_text
        self._save()
        notes = ["The samples were converted again with the new mapping.toml; element IDs may have changed "
                 "and design/design.json was re-keyed."]
        if lost:
            notes.append("These references could not be carried (their element was dropped or changed kind) "
                         "and were removed: " + ", ".join(lost[:40]))
        return {"notes": notes}

    def _workspace_differs(self, rev: str) -> bool:
        try:
            current = workspace.read_design(self.ws)
        except DesignError:
            return True
        base = self.project.design(rev)
        expected = rekey(base.design, self.rec["docs"], keep_unmapped=False)
        return (current.theme_text, current.custom_text, current.mapping_text, current.design) != \
            (base.theme_text, base.custom_text, base.mapping_text, expected)

    def _write_design(self, d: Design) -> None:
        """Put `d` in the workspace. Its references belong to its own mapping, so the samples
        are converted with that mapping first when theirs differs."""
        target = self.ws / "design"
        if target.exists():
            shutil.rmtree(target)
        d.with_design(rekey(d.design, self.rec["docs"], keep_unmapped=False)).write(target)
        if d.mapping_key() != self._samples_design().mapping_key():
            workspace.sync_samples(self.project, self.ws, self.rec["docs"], d, refs_from=None)
            self.rec["samples_mapping"] = d.mapping_text

    def close(self) -> None:
        if self.watcher:
            self.watcher.stop()
        if self.session:
            self.session.close()
        self.session = None

    # ---------------------------------------------------------------- turns
    def send(self, text: str, selections: list[dict[str, Any]], images: list[bytes] = ()) -> None:
        """`images` are reference images already normalized (workspace.normalize_reference)."""
        with self.lock:
            if self.session is not None and self.session.busy:
                raise Conflict("the agent is still working on the previous message")
            docs = list(dict.fromkeys(s["doc"] for s in selections))
            self.ensure(docs)
            assert self.rec is not None and self.session is not None
            self._sync_design()
            sel = workspace.write_selection(self.ws, selections, self.rec["docs"])
            prompt = text.strip()
            if sel:
                lines = []
                for s in sel:
                    shape = ""
                    if s.get("table"):
                        t = s["table"]
                        shape = (f" — {t['columns']} columns, {t['header_rows']} header row(s)"
                                 + (f", {len(t['header_spans'])} merged header cell(s)" if t.get("header_spans") else ""))
                    lines.append(f"- {s['ref']} ({s['label']}){shape}")
                prompt += "\n\nAttached elements (see selection.json):\n" + "\n".join(lines)
            refs = workspace.add_references(self.ws, list(images))
            if refs:
                prompt += ("\n\nAttached reference images (the look to match; open them before you start):\n"
                           + "\n".join(f"- {r}" for r in refs))
            if self.rec.get("needs_summary"):
                prompt = summary(self.project, self.rec["docs"]) + prompt
            self.turn_text = []
            self.turn_focus = docs
            self.turn_note = text.strip()[:120]
            record(self.project, self.provider, {"role": "user", "text": text.strip(),
                                                 "attachments": [s.get("label", s["element"]) for s in selections]
                                                 + [f"Image {int(Path(r).stem[4:])}" for r in refs]})
            log.info("agent %s %s: turn started (%d chars, %d attachment(s), %d image(s))", self.provider,
                     self.project.id, len(text), len(selections), len(refs))
            self._publish({"type": "turn", "state": "started"})
            self.session.send(prompt, refs)
            self.rec["agent_session"] = self.session.session_id
            self.rec["needs_summary"] = False
            self._save()

    def cancel(self) -> None:
        if self.session:
            self.session.cancel()

    def _publish(self, ev: dict[str, Any]) -> None:
        events.publish(self.project.id, {"type": "agent", "provider": self.provider, "event": ev})

    def _on_event(self, ev: dict[str, Any]) -> None:
        self._publish(ev)
        if ev["type"] in ("status", "tool", "approval", "done", "error"):
            # no chat text in the log: event type and short details only
            detail = ev.get("kind") or ev.get("state") or ev.get("name") or ""
            extra = ev.get("detail") or (ev.get("message", "")[:300] if ev["type"] == "error" else "")
            (log.warning if ev["type"] == "error" else log.info)(
                "agent %s %s: %s %s %s", self.provider, self.project.id, ev["type"], detail, str(extra)[:300])
        t = ev["type"]
        if t == "text":
            self.turn_text.append(ev["text"])
        elif t == "tool":
            record(self.project, self.provider, {"role": "activity", "text": f"{ev['name']}: {ev.get('detail', '')}"[:200]})
        elif t == "approval":
            record(self.project, self.provider, {"role": "activity", "text": f"approval requested: {ev['title']}"})
        elif t in ("done", "error"):
            if self.turn_text:
                record(self.project, self.provider, {"role": "agent", "text": "\n\n".join(self.turn_text)})
            if t == "error":
                record(self.project, self.provider, {"role": "error", "text": f"{ev['kind']}: {ev['message']}"[:600]})
            if self.rec is not None and self.session is not None:
                self.rec["agent_session"] = self.session.session_id
                self._save()
            threading.Thread(target=self._after_turn, daemon=True).start()

    def _after_turn(self) -> None:
        """Turn the workspace design into a proposal, or report why it cannot be one."""
        assert self.rec is not None
        try:
            followed = self._follow_mapping()
            if followed.get("problems"):
                raise DesignError(followed["problems"])
            wdesign = workspace.read_design(self.ws)
            reverse = {v: k for k, v in self.rec["docs"].items()}
            real = wdesign.with_design(rekey(wdesign.design, reverse, keep_unmapped=False))
            base = self.project.design(self.rec["base"])
            real.design = merge_workspace(base.design, real.design, set(self.rec["docs"]))
            # the workspace documents' references now belong to the workspace mapping; every
            # other document's still to the base one
            remap_docs = [d["id"] for d in self.project.documents() if d["id"] not in self.rec["docs"]]
            # keep updating this session's proposal while it is pending; otherwise start a new one
            pid = self.rec.get("proposal")
            if pid and (read_json(self.project.proposal_dir(pid) / "meta.json") or {}).get("status") != "pending":
                pid = None
            meta = proposals.create(self.project, real, base=self.rec["base"], origin=f"agent:{self.provider}",
                                    note=getattr(self, "turn_note", ""), focus=getattr(self, "turn_focus", []),
                                    pid=pid, remap_docs=remap_docs)
        except DesignError as e:
            log.warning("agent %s %s: invalid design: %s", self.provider, self.project.id, "; ".join(e.problems[:8]))
            self._publish({"type": "error", "kind": "invalid_design", "recoverable": True,
                           "message": "The agent's design changes are invalid and were not turned into a proposal:\n- "
                                      + "\n- ".join(e.problems[:12])})
            record(self.project, self.provider, {"role": "error", "text": "invalid design: " + "; ".join(e.problems[:6])})
            self._publish({"type": "turn", "state": "finished", "proposal": None})
            return
        except Exception as e:  # noqa: BLE001
            log.exception("agent %s %s: proposal failed", self.provider, self.project.id)
            self._publish({"type": "error", "kind": "failed", "recoverable": True, "message": f"proposal failed: {e}"})
            self._publish({"type": "turn", "state": "finished", "proposal": None})
            return
        if meta is None:
            self._publish({"type": "turn", "state": "finished", "proposal": None})
            return
        self.rec["proposal"] = meta["id"]
        self._save()
        pdir = self.project.proposal_dir(meta["id"])
        result = workspace.render_samples(self.ws, workspace.read_design(self.ws), None,
                                          self.ws.parent / f".render-{self.rec['key']}", out_pdf_dir=pdir / "synthetic")
        mapping = {v: k for k, v in self.rec["docs"].items()}
        write_json(pdir / "synthetic.json", {"ok": result["ok"], "problems": result["problems"],
                                             "docs": {mapping.get(n, n): {"sample": n, **v} for n, v in result["docs"].items()}})
        log.info("agent %s %s: proposal %s (synthetic preview ok=%s)", self.provider, self.project.id, meta["id"], result["ok"])
        self._publish({"type": "turn", "state": "finished", "proposal": meta["id"]})


def active(project: Project) -> Conversation | None:
    return _active.get(project.id)


def get(project: Project, provider: str) -> Conversation:
    """The project's conversation with `provider`, switching away from another provider."""
    with _guard:
        conv = _active.get(project.id)
        if conv is not None and conv.provider == provider:
            return conv
        if conv is not None:
            if conv.session is not None and conv.session.busy:
                raise Conflict("finish or cancel the current conversation before switching assistants")
            conv.close()
        conv = Conversation(project, provider)
        if conv.rec is not None and conv.rec.get("base") != project.accepted() and not conv.rec.get("proposal"):
            # coming back to a provider after work elsewhere: continue in a fresh workspace
            conv.rec = None
        _active[project.id] = conv
        return conv


def clear(project: Project, provider: str) -> None:
    """Start over with `provider`: end its agent session and archive its chat record. The
    workspace and any pending proposal stay; the next message starts a fresh agent session
    with the project summary."""
    with _guard:
        conv = _active.get(project.id)
        if conv is not None and conv.provider == provider:
            if conv.session is not None and conv.session.busy:
                raise Conflict("finish or cancel the current message before clearing the conversation")
            conv.close()
            del _active[project.id]
        with project.lock:
            states = read_json(_state_file(project), {"sessions": {}})
            rec = states["sessions"].get(provider)
            if rec:
                rec["agent_session"] = None
                rec["needs_summary"] = True
                write_json(_state_file(project), states)
        f = _chat_file(project, provider)
        if f.exists():
            archive = f.parent / "archive"
            archive.mkdir(exist_ok=True)
            f.rename(archive / f"{provider}-{new_id('c')}.jsonl")
    log.info("agent %s %s: conversation cleared", provider, project.id)


def drop(project: Project) -> None:
    """Close the project's conversation (the project is being deleted)."""
    with _guard:
        conv = _active.get(project.id)
        if conv is not None:
            if conv.session is not None and conv.session.busy:
                raise Conflict("finish or cancel the current conversation before deleting the project")
            conv.close()
            del _active[project.id]


def close_all() -> None:
    with _guard:
        for conv in _active.values():
            conv.close()
        _active.clear()
