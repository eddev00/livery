"""Local web UI for wordeasy.

Security model (the UI is strictly local):
  * the server binds to 127.0.0.1 only; it is unreachable from the LAN whatever the firewall says
  * every request must carry a session cookie holding a random per-launch token; the token is
    handed over once through the URL printed at start-up, then lives in an HttpOnly cookie
  * the Host header must be the loopback address the server listens on (blocks DNS rebinding)
  * state-changing requests must come from the page itself (Origin / Sec-Fetch-Site checks)
  * files are served only from registered wordeasy output folders, by whitelisted extension
  * no CORS, no outbound network, no telemetry; the process ends when the terminal or the Quit
    button says so
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import shutil
import socket
import tomllib
import threading
import time
import uuid
import webbrowser
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse
from starlette.middleware.base import BaseHTTPMiddleware

from .. import verify as V
from ..convert import DEFAULT_THEME, load_theme
from ..pipeline import COVER_EDITABLE, ORIGINAL_PDF, STATE_FILE, BuildOptions, anonymize_document, build_document, cover_variants, out_dir_for, read_state, twin_dir_for, update_cover, verify_document
from ..tools import REPO_ROOT, TEMPLATE_DIR, tool_versions

STATIC_DIR = Path(__file__).parent / "static"
COOKIE = "wordeasy_session"
SERVABLE = {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".md", ".json", ".txt", ".log"}
DOC_EXTS = {".docx", ".docm", ".doc", ".odt", ".rtf"}
STAGES = ("queued", "converting", "cover", "rendering", "checking", "original", "anonymizing", "reporting", "done", "error")


# ------------------------------------------------------------------ registry of output folders
class Registry:
    """Maps short ids to wordeasy output folders. Only registered folders are ever served."""

    def __init__(self, out_root: Path):
        self.out_root = out_root
        self._dirs: dict[str, Path] = {}
        self._lock = threading.Lock()

    @staticmethod
    def id_for(out_dir: Path) -> str:
        return hashlib.sha1(str(out_dir.resolve()).encode("utf-8")).hexdigest()[:12]

    def register(self, out_dir: Path) -> str:
        out_dir = out_dir.resolve()
        doc_id = self.id_for(out_dir)
        with self._lock:
            self._dirs[doc_id] = out_dir
        return doc_id

    def forget(self, doc_id: str) -> None:
        with self._lock:
            self._dirs.pop(doc_id, None)

    def get(self, doc_id: str) -> Path:
        with self._lock:
            d = self._dirs.get(doc_id)
        if d is None:
            self.scan()  # a deep link on a fresh server: register whatever is under out_root
            with self._lock:
                d = self._dirs.get(doc_id)
        if d is None or not (d / STATE_FILE).exists():
            raise HTTPException(404, "unknown document")
        return d

    def scan(self) -> list[dict[str, Any]]:
        docs: list[dict[str, Any]] = []
        if self.out_root.is_dir():
            for d in sorted(self.out_root.iterdir()):
                if d.is_dir() and (d / STATE_FILE).exists():
                    docs.append(self.summary(self.register(d)))
        docs.sort(key=lambda d: d.get("generated_at") or "", reverse=True)
        return docs

    def summary(self, doc_id: str) -> dict[str, Any]:
        d = self.get(doc_id)
        return {"id": doc_id, **_folder_summary(d)}


def _folder_summary(d: Path) -> dict[str, Any]:
    """What the UI needs to know about one output folder (a document or its lorem twin)."""
    state = read_state(d) or {}
    report = _read_json(d / "report.json")
    pdf = d / f"{state.get('name', d.name)}.pdf"
    return {
        "name": state.get("name", d.name), "title": state.get("title", ""),
        "source": state.get("source", ""), "out_dir": str(d),
        "status": report.get("status") if report else None,
        "generated_at": report.get("generated_at") if report else None,
        "pdf": pdf.name if pdf.exists() else None,
        "original_pdf": ORIGINAL_PDF if (d / ORIGINAL_PDF).exists() else None,
        "anonymized": bool(state.get("anonymized")),
        "checks": {c["name"]: c["status"] for c in report.get("checks", [])} if report else {},
    }


def _twin_summary(d: Path) -> dict[str, Any] | None:
    """The lorem twin under <doc>/lorem/, with its files addressed relative to the document folder."""
    twin = twin_dir_for(d)
    if read_state(twin) is None:
        return None
    t = _folder_summary(twin)
    t["pdf"] = f"{twin.name}/{t['pdf']}" if t["pdf"] else None
    t["original_pdf"] = f"{twin.name}/{t['original_pdf']}" if t["original_pdf"] else None
    t["report"] = _read_json(twin / "report.json")
    return t


def _read_json(p: Path) -> dict[str, Any] | None:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


# ------------------------------------------------------------------ jobs
@dataclass
class DocTask:
    source: str
    name: str
    stage: str = "queued"
    message: str = ""
    status: str | None = None
    doc_id: str | None = None
    error: str | None = None
    started: float | None = None
    finished: float | None = None


@dataclass
class Job:
    id: str
    kind: str  # build | verify
    docs: list[DocTask]
    options: dict[str, Any]
    created: float = field(default_factory=time.time)
    state: str = "queued"  # queued | running | done
    events: list[dict[str, Any]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def emit(self, kind: str, **data: Any) -> None:
        with self.lock:
            self.events.append({"seq": len(self.events), "t": time.time(), "type": kind, **data})

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {"id": self.id, "kind": self.kind, "state": self.state, "created": self.created,
                    "options": self.options, "docs": [asdict(d) for d in self.docs],
                    "summary": {s: sum(1 for d in self.docs if d.status == s) for s in (V.PASS, V.WARN, V.FAIL)}
                    | {"error": sum(1 for d in self.docs if d.error)}}

    def events_since(self, seq: int) -> list[dict[str, Any]]:
        with self.lock:
            return self.events[seq:]


class Runner:
    """Runs jobs one at a time on a background thread; typst and pandoc are CPU bound anyway."""

    def __init__(self, registry: Registry):
        self.registry = registry
        self.jobs: dict[str, Job] = {}
        self._queue: list[Job] = []
        self._cv = threading.Condition()
        self._thread = threading.Thread(target=self._loop, name="wordeasy-runner", daemon=True)
        self._thread.start()

    def submit(self, job: Job) -> Job:
        with self._cv:
            self.jobs[job.id] = job
            self._queue.append(job)
            self._cv.notify()
        return job

    def _loop(self) -> None:
        while True:
            with self._cv:
                while not self._queue:
                    self._cv.wait()
                job = self._queue.pop(0)
            self._run(job)

    def _run(self, job: Job) -> None:
        job.state = "running"
        job.emit("job", state="running")
        for i, task in enumerate(job.docs):
            task.started = time.time()

            def progress(stage: str, message: str, _i: int = i, _t: DocTask = task) -> None:
                _t.stage, _t.message = stage, message
                job.emit("doc", index=_i, stage=stage, message=message)

            try:
                if job.kind == "build":
                    opts = BuildOptions(**job.options["build"])
                    report = build_document(Path(task.source), opts, progress=progress)
                    task.doc_id = self.registry.register(Path(report["output_dir"]))
                elif job.kind == "anonymize":
                    report = anonymize_document(Path(task.source), progress=progress)
                    task.doc_id = self.registry.register(Path(task.source))  # the twin is part of the document
                else:
                    report = verify_document(Path(task.source), progress=progress)
                    task.doc_id = self.registry.register(Path(task.source))
                task.status = report["status"]
                task.stage, task.message = "done", f"{report['status']}"
            except Exception as e:  # noqa: BLE001
                task.error = str(e)
                task.stage, task.message = "error", str(e)
            task.finished = time.time()
            job.emit("doc", index=i, stage=task.stage, message=task.message, status=task.status,
                     doc_id=task.doc_id, error=task.error)
        job.state = "done"
        job.emit("job", state="done", summary=job.snapshot()["summary"])


# ------------------------------------------------------------------ security middleware
class LocalOnly(BaseHTTPMiddleware):
    def __init__(self, app: FastAPI, token: str, hosts: set[str]):
        super().__init__(app)
        self.token = token
        self.hosts = hosts
        self.origins = {f"http://{h}" for h in hosts}

    async def dispatch(self, request: Request, call_next):
        host = request.headers.get("host", "")
        if host not in self.hosts:
            return PlainTextResponse("wordeasy: unexpected Host header", status_code=421)
        if request.url.path == "/" and request.query_params.get("token"):
            if hmac.compare_digest(request.query_params["token"], self.token):
                resp = RedirectResponse("/", status_code=303)
                resp.set_cookie(COOKIE, self.token, httponly=True, samesite="strict", path="/")
                return resp
            return PlainTextResponse("wordeasy: invalid token", status_code=403)
        cookie = request.cookies.get(COOKIE, "")
        if not hmac.compare_digest(cookie, self.token):
            return PlainTextResponse("wordeasy: not signed in. Open the URL printed in the terminal.", status_code=403)
        if request.method not in ("GET", "HEAD"):
            origin = request.headers.get("origin")
            site = request.headers.get("sec-fetch-site")
            if (origin is not None and origin not in self.origins) or (site not in (None, "same-origin", "none")):
                return PlainTextResponse("wordeasy: cross-site request refused", status_code=403)
        resp = await call_next(request)
        resp.headers["Content-Security-Policy"] = ("default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                                                   "frame-src 'self'; object-src 'self'; base-uri 'none'; form-action 'self'")
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["X-Frame-Options"] = "SAMEORIGIN"
        if request.url.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp


# ------------------------------------------------------------------ app
def create_app(*, token: str, out_root: Path, hosts: set[str]) -> FastAPI:
    app = FastAPI(title="wordeasy", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(LocalOnly, token=token, hosts=hosts)
    registry = Registry(out_root)
    runner = Runner(registry)
    app.state.registry, app.state.runner, app.state.server = registry, runner, None

    def static(name: str, media_type: str) -> Response:
        return FileResponse(STATIC_DIR / name, media_type=media_type, headers={"Cache-Control": "no-cache"})

    @app.get("/")
    def index() -> Response:
        return static("index.html", "text/html; charset=utf-8")

    @app.get("/app.js")
    def app_js() -> Response:
        return static("app.js", "text/javascript; charset=utf-8")

    @app.get("/style.css")
    def style_css() -> Response:
        return static("style.css", "text/css; charset=utf-8")

    @app.get("/api/state")
    def api_state() -> dict[str, Any]:
        return {
            "out_root": str(out_root), "repo_root": str(REPO_ROOT), "home": str(Path.home()),
            "samples": str(REPO_ROOT / "samples"),
            "template": str(DEFAULT_THEME.resolve()),
            "cover_variants": cover_variants(load_theme(DEFAULT_THEME)),  # re-read: the theme is editable in the UI
            "tools": tool_versions(),
            "jobs": [j.snapshot() for j in sorted(runner.jobs.values(), key=lambda j: j.created, reverse=True)][:20],
        }

    @app.get("/api/docs")
    def api_docs() -> list[dict[str, Any]]:
        return registry.scan()

    @app.get("/api/docs/{doc_id}")
    def api_doc(doc_id: str) -> dict[str, Any]:
        d = registry.get(doc_id)
        return {
            **registry.summary(doc_id),
            "report": _read_json(d / "report.json"),
            "state": read_state(d),
            "lorem": _twin_summary(d),
            "files": sorted(p.name for p in d.iterdir() if p.is_file() and p.suffix.lower() in SERVABLE),
        }

    def _verify_job(doc_id: str, d: Path) -> dict[str, Any]:
        state = read_state(d) or {}
        job = Job(id=uuid.uuid4().hex[:10], kind="verify", options={"doc_id": doc_id},
                  docs=[DocTask(source=str(d), name=state.get("name", d.name), doc_id=doc_id)])
        runner.submit(job)
        return job.snapshot()

    @app.post("/api/docs/{doc_id}/verify")
    def api_verify(doc_id: str) -> dict[str, Any]:
        return _verify_job(doc_id, registry.get(doc_id))

    @app.delete("/api/docs/{doc_id}")
    def api_delete(doc_id: str) -> dict[str, Any]:
        """Delete a document's whole output folder. The original Word file is never touched."""
        d = registry.get(doc_id)
        busy = [j for j in runner.jobs.values() if j.state != "done"
                and any(t.doc_id == doc_id or t.source == str(d) for t in j.docs)]
        if busy:
            raise HTTPException(409, "a job is still working on this document")
        shutil.rmtree(d)
        registry.forget(doc_id)
        return {"ok": True, "deleted": str(d)}

    @app.post("/api/docs/{doc_id}/anonymize")
    def api_anonymize(doc_id: str) -> dict[str, Any]:
        """Queue a (re)build of the document's lorem-ipsum twin under lorem/ (same design, no real data)."""
        d = registry.get(doc_id)
        state = read_state(d) or {}
        if state.get("anonymized"):
            raise HTTPException(400, "this document is already a lorem ipsum twin")
        job = Job(id=uuid.uuid4().hex[:10], kind="anonymize", options={"doc_id": doc_id},
                  docs=[DocTask(source=str(d), name=state.get("name", d.name))])
        runner.submit(job)
        return job.snapshot()

    @app.post("/api/docs/{doc_id}/cover")
    async def api_cover(doc_id: str, request: Request) -> dict[str, Any]:
        """Change cover/metadata fields, regenerate main.typ, then rebuild and re-verify."""
        d = registry.get(doc_id)
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(400, "expected a JSON object")
        fields = {k: (v if isinstance(v, str) else "") for k, v in body.items() if k in COVER_EDITABLE}
        try:
            update_cover(d, fields)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return _verify_job(doc_id, d)

    @app.post("/api/jobs")
    async def api_new_job(request: Request) -> dict[str, Any]:
        body = await request.json()
        files = [Path(f).expanduser() for f in body.get("files", []) if isinstance(f, str) and f.strip()]
        if not files:
            raise HTTPException(400, "no files given")
        missing = [str(f) for f in files if not f.is_file()]
        if missing:
            raise HTTPException(400, f"not found: {', '.join(missing)}")
        bad = [str(f) for f in files if f.suffix.lower() not in DOC_EXTS]
        if bad:
            raise HTTPException(400, f"not a Word document: {', '.join(bad)}")
        o = body.get("options", {}) or {}
        root = Path(o.get("out_root") or out_root).expanduser().resolve()

        def text(key: str) -> str | None:
            v = o.get(key)
            return v.strip() if isinstance(v, str) and v.strip() else None

        opts = BuildOptions(
            theme=DEFAULT_THEME.resolve(), out_root=root,
            accept_changes=bool(o.get("accept_changes")), clean=bool(o.get("clean")),
            title=text("title"), author=text("author"), date=text("date"), department=text("department"),
            number=text("number"), version=text("version"),
            department_name=text("department_name"), cover=text("cover"),
        )
        job = Job(id=uuid.uuid4().hex[:10], kind="build",
                  options={"build": {k: (str(v) if isinstance(v, Path) else v) for k, v in asdict(opts).items()}},
                  docs=[DocTask(source=str(f.resolve()), name=f.stem,
                                doc_id=registry.id_for(out_dir_for(f.resolve(), root))) for f in files])
        runner.submit(job)
        return job.snapshot()

    @app.get("/api/jobs")
    def api_jobs() -> list[dict[str, Any]]:
        return [j.snapshot() for j in sorted(runner.jobs.values(), key=lambda j: j.created, reverse=True)]

    @app.get("/api/jobs/{job_id}")
    def api_job(job_id: str) -> dict[str, Any]:
        job = runner.jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "unknown job")
        return job.snapshot()

    @app.get("/api/jobs/{job_id}/events")
    async def api_job_events(job_id: str, request: Request) -> StreamingResponse:
        job = runner.jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "unknown job")

        async def stream():
            seq = 0
            yield f"event: snapshot\ndata: {json.dumps(job.snapshot())}\n\n"
            while True:
                for ev in job.events_since(seq):
                    seq = ev["seq"] + 1
                    yield f"id: {ev['seq']}\nevent: {ev['type']}\ndata: {json.dumps(ev)}\n\n"
                if job.state == "done" and seq >= len(job.events):
                    yield f"event: end\ndata: {json.dumps(job.snapshot())}\n\n"
                    return
                if await request.is_disconnected():
                    return
                await asyncio.sleep(0.25)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    # ---- template editor: the .toml and .typ files directly under template/, edited as free text
    TEMPLATE_EXTS = {".toml", ".typ"}

    def _template_file(name: str) -> Path:
        if "/" in name or "\\" in name or name.startswith(".") or Path(name).suffix not in TEMPLATE_EXTS:
            raise HTTPException(404, "unknown template file")
        f = (TEMPLATE_DIR / name).resolve()
        if f.parent != TEMPLATE_DIR.resolve() or not f.is_file():
            raise HTTPException(404, "unknown template file")
        return f

    @app.get("/api/template")
    def api_template() -> dict[str, Any]:
        files = sorted(f for f in TEMPLATE_DIR.iterdir() if f.is_file() and f.suffix in TEMPLATE_EXTS and not f.name.startswith("."))
        return {"dir": str(TEMPLATE_DIR), "files": [{"name": f.name, "size": f.stat().st_size, "mtime": f.stat().st_mtime} for f in files]}

    @app.get("/api/template/{name}")
    def api_template_read(name: str) -> Response:
        return PlainTextResponse(_template_file(name).read_text(encoding="utf-8"))

    @app.put("/api/template/{name}")
    async def api_template_write(name: str, request: Request) -> dict[str, Any]:
        f = _template_file(name)
        text = (await request.body()).decode("utf-8")
        if f.suffix == ".toml":
            try:
                tomllib.loads(text)
            except tomllib.TOMLDecodeError as e:
                raise HTTPException(400, f"not valid TOML: {e}")
        f.write_text(text, encoding="utf-8")
        return {"ok": True, "name": f.name, "bytes": len(text.encode("utf-8")), "mtime": f.stat().st_mtime}

    @app.post("/api/template/rebuild")
    def api_template_rebuild() -> dict[str, Any]:
        """Recompile and re-check every document under the output root with the current template."""
        docs = registry.scan()
        if not docs:
            raise HTTPException(400, "no documents to rebuild")
        job = Job(id=uuid.uuid4().hex[:10], kind="verify", options={"all": True},
                  docs=[DocTask(source=d["out_dir"], name=d["name"], doc_id=d["id"]) for d in docs])
        runner.submit(job)
        return job.snapshot()

    @app.get("/api/fs")
    def api_fs(path: str = "") -> dict[str, Any]:
        """List one directory: sub-directories and Word documents. Used by the file picker."""
        p = Path(path).expanduser() if path else Path.home()
        try:
            p = p.resolve(strict=True)
        except OSError:
            raise HTTPException(404, "no such directory")
        if not p.is_dir():
            p = p.parent
        dirs, files = [], []
        try:
            for child in sorted(p.iterdir(), key=lambda c: c.name.lower()):
                if child.name.startswith("."):
                    continue
                try:
                    if child.is_dir():
                        dirs.append(child.name)
                    elif child.is_file() and child.suffix.lower() in DOC_EXTS:
                        files.append({"name": child.name, "size": child.stat().st_size})
                except OSError:
                    continue
        except PermissionError:
            raise HTTPException(403, "permission denied")
        return {"path": str(p), "parent": str(p.parent) if p.parent != p else None, "dirs": dirs, "files": files}

    @app.get("/files/{doc_id}/{rel:path}")
    def api_file(doc_id: str, rel: str) -> Response:
        d = registry.get(doc_id)
        target = (d / rel).resolve()
        if d not in target.parents or not target.is_file() or target.suffix.lower() not in SERVABLE:
            raise HTTPException(404, "no such file")
        media = {".pdf": "application/pdf", ".md": "text/plain; charset=utf-8", ".txt": "text/plain; charset=utf-8",
                 ".log": "text/plain; charset=utf-8", ".json": "application/json"}.get(target.suffix.lower())
        return FileResponse(target, media_type=media, headers={"Cache-Control": "no-cache"})

    @app.post("/api/quit")
    def api_quit() -> dict[str, Any]:
        server = app.state.server
        if server is not None:
            server.should_exit = True
        return {"ok": True}

    @app.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    return app


# ------------------------------------------------------------------ entry point
def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def serve(*, out_root: Path, port: int = 0, open_browser: bool = True) -> int:
    import uvicorn

    port = port or free_port()
    token = secrets.token_urlsafe(32)
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    app = create_app(token=token, out_root=out_root, hosts=hosts)
    url = f"http://127.0.0.1:{port}/?token={token}"
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    app.state.server = server
    print(f"wordeasy UI listening on 127.0.0.1:{port} (loopback only)\nopen: {url}\nCtrl-C or the Quit button stops it.", flush=True)
    if open_browser and not os.environ.get("WORDEASY_NO_BROWSER"):
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    server.run()
    return 0
