"""The local HTTP API of the desktop engine.

Bound to 127.0.0.1 on a random port. Every request carries the per-launch token the app
handed over on stdin (`Authorization: Bearer <token>`); the Host header must be the loopback
address. No CORS, no static files, no outbound network. Agent-facing operations are not
here: agents only see their workspace (see workspace.py).
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import hmac
import json
import logging
import os
import queue
import shutil
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from ..tools import tool_versions
from . import agents, builder, conversation, events, export, jobs, logs, patterns, proposals, review, sandbox, workspace
from .design import DesignError
from .paths import workspaces_dir
from .store import Conflict, NotFound, Project, list_projects, read_json


class NameBody(BaseModel):
    name: str


class PathsBody(BaseModel):
    paths: list[str]


class PathBody(BaseModel):
    path: str


class ExportBody(BaseModel):
    path: str
    docs: list[str] | None = None


class Ref(BaseModel):
    doc: str
    element: str


MAX_IMAGES = 4


class MessageBody(BaseModel):
    text: str
    selections: list[Ref] = []
    images: list[str] = []  # base64 reference images (PNG, JPEG, WebP or GIF)


class ApplyBody(BaseModel):
    treatment: str
    refs: list[Ref]


class RefsBody(BaseModel):
    refs: list[Ref]


class SecretBody(BaseModel):
    name: str
    value: str


class AllowBody(BaseModel):
    allow: bool


class DocsBody(BaseModel):
    docs: list[str] | None = None


class DoneBody(BaseModel):
    done: bool


def _project(pid: str) -> Project:
    try:
        return Project.open(pid)
    except NotFound:
        raise HTTPException(404, "project not found") from None


def _doc(project: Project, doc: str) -> dict[str, Any]:
    try:
        return project.document(doc)
    except NotFound:
        raise HTTPException(404, "document not found") from None


def create_app(token: str) -> FastAPI:
    app = FastAPI(title="Livery engine", docs_url=None, redoc_url=None, openapi_url=None)

    allowed_hosts = ("127.0.0.1",) + (("testserver",) if os.environ.get("LIVERY_TESTING") == "1" else ())

    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = request.headers.get("host", "").rsplit(":", 1)[0] if ":" in request.headers.get("host", "") \
            else request.headers.get("host", "")
        if host not in allowed_hosts:
            return JSONResponse({"detail": "bad host"}, status_code=421)
        auth = request.headers.get("authorization", "")
        supplied = auth[7:] if auth.startswith("Bearer ") else ""
        if not hmac.compare_digest(supplied.encode(), token.encode()):
            logs.log.warning("refused request without a valid token: %s %s", request.method, request.url.path)
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        started = time.monotonic()
        response = await call_next(request)
        if not request.url.path.endswith("/events"):
            ms = (time.monotonic() - started) * 1000
            level = logging.WARNING if response.status_code >= 500 else logging.INFO
            logs.log.log(level, "%s %s -> %d (%.0f ms)", request.method, request.url.path, response.status_code, ms)
        return response

    def handler(status: int):
        async def handle(request: Request, exc: Exception):
            logs.log.info("%s %s: %s %s", request.method, request.url.path, type(exc).__name__, str(exc)[:300])
            body: dict[str, Any] = {"detail": str(exc).strip("'\"")}
            if isinstance(exc, DesignError):
                body["problems"] = exc.problems
            return JSONResponse(body, status_code=status)
        return handle

    app.add_exception_handler(NotFound, handler(404))
    app.add_exception_handler(Conflict, handler(409))
    app.add_exception_handler(DesignError, handler(422))
    app.add_exception_handler(ValueError, handler(400))
    app.add_exception_handler(FileNotFoundError, handler(400))
    app.add_exception_handler(sandbox.SandboxUnavailable, handler(503))
    app.add_exception_handler(workspace.LeakRefused, handler(409))

    # ---------------------------------------------------------------- system
    @app.get("/api/health")
    def health():
        return {"ok": True}

    @app.get("/api/system")
    def system():
        sb = sandbox.check()
        return {"tools": tool_versions(), "sandbox": {"ok": sb.ok, "message": sb.message, "version": sb.version},
                "providers": agents.providers(), "logs": str(logs.logs_dir())}

    @app.get("/api/agents")
    def agent_status():
        out = []
        for p in agents.providers():
            try:
                out.append(agents.adapter(p).status())
            except Exception as e:  # noqa: BLE001
                out.append({"provider": p, "installed": False, "error": str(e)})
        return out

    @app.get("/api/agents/{provider}/login")
    def agent_login(provider: str):
        try:
            return agents.adapter(provider).login_help()
        except KeyError:
            raise HTTPException(404, "unknown provider") from None

    @app.post("/api/agents/{provider}/connect")
    def agent_connect(provider: str):
        try:
            agents.adapter(provider).connect()
        except KeyError:
            raise HTTPException(404, "unknown provider") from None
        except (RuntimeError, OSError) as e:
            raise HTTPException(400, str(e)) from None
        return {"ok": True}

    @app.post("/api/agents/{provider}/secret")
    def agent_secret(provider: str, body: SecretBody):
        a = agents.adapter(provider)
        allowed = {s["name"] for s in a.login_help()["secrets"]}
        if body.name not in allowed:
            raise HTTPException(400, "unknown secret")
        a.save_secret(body.name, body.value)
        return a.status()

    # ---------------------------------------------------------------- projects
    @app.get("/api/projects")
    def projects():
        return list_projects()

    @app.post("/api/projects")
    def create_project(body: NameBody):
        p = Project.create(body.name)
        return p.state()

    @app.get("/api/projects/{pid}")
    def project(pid: str):
        return _project(pid).state()

    @app.patch("/api/projects/{pid}")
    def rename(pid: str, body: NameBody):
        p = _project(pid)
        with p.lock:
            st = p.state()
            st["name"] = body.name.strip() or st["name"]
            p.save(st)
        return st

    @app.delete("/api/projects/{pid}")
    def delete_project(pid: str):
        p = _project(pid)
        with p.lock:
            if any(j["state"] in ("queued", "running") for j in jobs.list_jobs(p.id)):
                raise Conflict("wait for the running imports and renders to finish before deleting the project")
            conversation.drop(p)
            shutil.rmtree(workspaces_dir() / p.id, ignore_errors=True)
            p.delete()
        return {"ok": True}

    # ---------------------------------------------------------------- documents
    @app.post("/api/projects/{pid}/documents")
    def import_docs(pid: str, body: PathsBody):
        p = _project(pid)
        out = []
        for path in body.paths:
            try:
                doc, version = p.add_source(Path(path))
                jobs.queue_import(p, doc, version)
                out.append({"path": path, "doc": doc, "version": version})
            except (ValueError, FileNotFoundError) as e:
                out.append({"path": path, "error": str(e)})
        events.publish(p.id, {"type": "documents"})
        return out

    @app.post("/api/projects/{pid}/documents/{doc}/replace")
    def replace_doc(pid: str, doc: str, body: PathBody):
        p = _project(pid)
        _doc(p, doc)
        d, version = p.add_source(Path(body.path), replace=doc)
        jobs.queue_import(p, d, version)
        events.publish(p.id, {"type": "documents"})
        return {"doc": d, "version": version}

    @app.delete("/api/projects/{pid}/documents/{doc}")
    def remove_doc(pid: str, doc: str):
        p = _project(pid)
        p.remove_document(doc)
        events.publish(p.id, {"type": "documents"})
        return {"ok": True}

    @app.post("/api/projects/{pid}/documents/{doc}/done")
    def mark_done(pid: str, doc: str, body: DoneBody):
        p = _project(pid)
        _doc(p, doc)
        r = review.mark(p, doc, body.done)
        events.publish(p.id, {"type": "documents"})
        return r

    @app.get("/api/projects/{pid}/stats")
    def stats(pid: str):
        return review.stats(_project(pid))

    @app.get("/api/projects/{pid}/documents")
    def documents(pid: str):
        p = _project(pid)
        st = p.state()
        rev = st["accepted"]
        out = []
        for d in p.documents():
            ver = next((v for v in d["versions"] if v["v"] == d["current"]), {})
            build = jobs.ensure_rendered(p, d["id"], d["current"], rev) if ver.get("status") == "imported" else None
            root = builder.render_root(p, d["id"], d["current"], rev)
            report = builder.report_of(root) if build and build.get("state") == "done" else None
            orig = p.import_dir(d["id"], d["current"]) / builder.DOC_DIR / "original.pdf"
            out.append({**d, "version": ver, "build": build, "report_status": (report or {}).get("status"),
                        "has_original": orig.exists(), "review": review.state_of(p, d, rev, st["history"]),
                        "has_twin": (p.import_dir(d["id"], d["current"]) / builder.DOC_DIR / "lorem" / "content.typ").exists()})
        return out

    def _root_for(p: Project, doc: str, rev: str | None, proposal: str | None) -> Path:
        d = _doc(p, doc)
        if proposal:
            return proposals.real_root(p, proposal, doc)
        return builder.render_root(p, doc, d["current"], rev or p.accepted())

    @app.get("/api/projects/{pid}/documents/{doc}/pdf")
    def pdf(pid: str, doc: str, kind: str = "styled", rev: str | None = None, proposal: str | None = None):
        p = _project(pid)
        d = _doc(p, doc)
        if kind == "original":
            f = p.import_dir(doc, d["current"]) / builder.DOC_DIR / "original.pdf"
        elif kind == "synthetic":
            if not proposal:
                raise HTTPException(400, "synthetic previews belong to a proposal")
            sj = read_json(p.proposal_dir(proposal) / "synthetic.json") or {}
            sample = (sj.get("docs", {}).get(doc) or {}).get("sample")
            f = p.proposal_dir(proposal) / "synthetic" / f"{sample}.pdf" if sample else Path("/nonexistent")
        else:
            f = builder.pdf_of(_root_for(p, doc, rev, proposal)) or Path("/nonexistent")
        if not f.exists():
            raise HTTPException(404, "no PDF yet")
        return FileResponse(f, media_type="application/pdf", headers={"Cache-Control": "no-store"})

    @app.get("/api/projects/{pid}/documents/{doc}/selection")
    def selection(pid: str, doc: str, rev: str | None = None, proposal: str | None = None):
        p = _project(pid)
        smap = read_json(_root_for(p, doc, rev, proposal) / builder.SELECTION_FILE)
        if smap is None:
            raise HTTPException(404, "no selection map yet")
        return smap

    @app.get("/api/projects/{pid}/documents/{doc}/report")
    def report(pid: str, doc: str, rev: str | None = None, proposal: str | None = None):
        p = _project(pid)
        root = _root_for(p, doc, rev, proposal)
        r = builder.report_of(root)
        st = builder.build_status_at(root)
        if r is None and st is None:
            raise HTTPException(404, "not rendered yet")
        return {"status": st, "report": r}

    @app.get("/api/projects/{pid}/documents/{doc}/elements")
    def elements(pid: str, doc: str):
        p = _project(pid)
        d = _doc(p, doc)
        return builder.inventory(p, doc, d["current"])

    # ---------------------------------------------------------------- design
    @app.get("/api/projects/{pid}/design")
    def design(pid: str):
        p = _project(pid)
        st = p.state()
        d = p.design(st["accepted"])
        return {"accepted": st["accepted"], "history": st["history"], "revisions": st["revisions"],
                "treatments": d.design.get("treatments", {}), "assignments": d.design.get("assignments", {}),
                "overrides": d.design.get("overrides", {}), "can_undo": len(st["history"]) > 1,
                "changes": p.changes()}

    @app.get("/api/projects/{pid}/design/{rev}/files")
    def design_files(pid: str, rev: str):
        return _project(pid).design(rev).files()

    @app.post("/api/projects/{pid}/design/undo")
    def undo(pid: str):
        return proposals.undo(_project(pid))

    @app.post("/api/projects/{pid}/design/changes/{rev}/remove")
    def remove_change(pid: str, rev: str):
        return proposals.remove_change(_project(pid), rev)

    # ---------------------------------------------------------------- proposals
    @app.get("/api/projects/{pid}/proposals")
    def proposal_list(pid: str):
        return proposals.list_all(_project(pid))

    @app.get("/api/projects/{pid}/proposals/{qid}")
    def proposal(pid: str, qid: str):
        p = _project(pid)
        meta = proposals.get(p, qid)
        meta["synthetic"] = read_json(p.proposal_dir(qid) / "synthetic.json")
        meta["files"] = proposals.design_of(p, qid).files()
        return meta

    @app.post("/api/projects/{pid}/proposals/{qid}/render")
    def proposal_render(pid: str, qid: str, body: DocsBody):
        return proposals.render_real(_project(pid), qid, body.docs)

    @app.post("/api/projects/{pid}/proposals/{qid}/accept")
    def proposal_accept(pid: str, qid: str):
        return proposals.accept(_project(pid), qid)

    @app.post("/api/projects/{pid}/proposals/{qid}/discard")
    def proposal_discard(pid: str, qid: str):
        proposals.discard(_project(pid), qid)
        return {"ok": True}

    # ---------------------------------------------------------------- treatments
    @app.post("/api/projects/{pid}/treatments/candidates")
    def tr_candidates(pid: str, body: Ref):
        return patterns.candidates(_project(pid), body.doc, body.element)

    @app.post("/api/projects/{pid}/treatments/apply")
    def tr_apply(pid: str, body: ApplyBody):
        return patterns.apply(_project(pid), body.treatment, [r.model_dump() for r in body.refs])

    @app.post("/api/projects/{pid}/treatments/unassign")
    def tr_unassign(pid: str, body: RefsBody):
        return patterns.unassign(_project(pid), [r.model_dump() for r in body.refs])

    # ---------------------------------------------------------------- chat
    def _resolve_selections(p: Project, refs: list[Ref]) -> list[dict[str, Any]]:
        """Attach only elements the accepted render actually mapped; never a guess."""
        out = []
        rev = p.accepted()
        for r in refs:
            d = _doc(p, r.doc)
            smap = read_json(builder.render_root(p, r.doc, d["current"], rev) / builder.SELECTION_FILE) or {}
            el = next((e for e in smap.get("elements", []) if e["id"] == r.element), None)
            if el is None or not el.get("selectable"):
                raise Conflict(f"{r.element} is not a selectable element of this document "
                               "(unsupported kind or not mapped in the current render)")
            out.append({"doc": r.doc, "element": r.element, "kind": el["kind"], "label": el["label"],
                        "table": el.get("table")})
        return out

    def _decode_images(data: list[str]) -> list[bytes]:
        if len(data) > MAX_IMAGES:
            raise HTTPException(400, f"attach at most {MAX_IMAGES} images to a message")
        out = []
        for i, b64 in enumerate(data, 1):
            try:
                raw = base64.b64decode(b64, validate=True)
            except binascii.Error:
                raise HTTPException(400, f"image {i} is not valid base64") from None
            try:
                out.append(workspace.normalize_reference(raw))
            except ValueError as e:
                raise HTTPException(400, f"image {i}: {e}") from None
        return out

    @app.get("/api/projects/{pid}/chat")
    def chat_state(pid: str):
        p = _project(pid)
        conv = conversation.active(p)
        st = read_json(p.root / "chats" / "state.json", {"sessions": {}})
        return {"active": conv.provider if conv else st.get("active"),
                "busy": bool(conv and conv.session and conv.session.busy),
                "proposal": (conv.rec or {}).get("proposal") if conv else None,
                "sessions": {k: {"created": v.get("created"), "proposal": v.get("proposal")}
                             for k, v in st["sessions"].items() if v}}

    @app.get("/api/projects/{pid}/chat/{provider}/history")
    def chat_history(pid: str, provider: str):
        return conversation.history(_project(pid), provider)

    @app.post("/api/projects/{pid}/chat/{provider}/messages")
    def chat_send(pid: str, provider: str, body: MessageBody):
        p = _project(pid)
        if not body.text.strip():
            raise HTTPException(400, "empty message")
        try:
            a = agents.adapter(provider)
        except KeyError:
            raise HTTPException(404, "unknown provider") from None
        sandbox.require()  # fail closed: never an unrestricted agent
        if a.binary() is None:
            raise HTTPException(503, f"{a.title} is not installed")
        sels = _resolve_selections(p, body.selections)
        images = _decode_images(body.images)
        conv = conversation.get(p, provider)
        try:
            conv.send(body.text, sels, images)
        except PermissionError as e:
            raise HTTPException(401, str(e)) from None
        return {"ok": True}

    @app.post("/api/projects/{pid}/chat/{provider}/clear")
    def chat_clear(pid: str, provider: str):
        try:
            agents.adapter(provider)
        except KeyError:
            raise HTTPException(404, "unknown provider") from None
        conversation.clear(_project(pid), provider)
        return {"ok": True}

    @app.post("/api/projects/{pid}/chat/cancel")
    def chat_cancel(pid: str):
        conv = conversation.active(_project(pid))
        if conv:
            conv.cancel()
        return {"ok": True}

    @app.post("/api/projects/{pid}/chat/approvals/{aid}")
    def chat_approval(pid: str, aid: str, body: AllowBody):
        conv = conversation.active(_project(pid))
        if not conv or not conv.session:
            raise HTTPException(404, "no active session")
        conv.session.respond_approval(aid, body.allow)
        return {"ok": True}

    # ---------------------------------------------------------------- export & jobs
    @app.post("/api/projects/{pid}/export")
    def do_export(pid: str, body: ExportBody):
        p = _project(pid)
        key = f"export:{p.id}" if body.docs is None else f"export:{p.id}:{','.join(sorted(body.docs))}"
        return jobs.submit(p, "export", key, lambda progress: export.export(p, Path(body.path), progress, body.docs),
                           project=p.id)

    @app.get("/api/projects/{pid}/jobs")
    def project_jobs(pid: str):
        return jobs.list_jobs(pid)

    @app.get("/api/projects/{pid}/events")
    async def stream(pid: str, request: Request):
        p = _project(pid)
        q = events.subscribe(p.id)

        async def gen():
            try:
                yield "retry: 2000\n\n"
                while not await request.is_disconnected():
                    try:
                        ev = await asyncio.to_thread(q.get, True, 15)
                        yield f"data: {json.dumps(ev, default=str)}\n\n"
                    except queue.Empty:
                        yield ": keepalive\n\n"
            finally:
                events.unsubscribe(p.id, q)
        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})

    return app
