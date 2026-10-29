"""Background work: imports, renders and exports, each document independently.

One failing document never stops the others: every job records its own outcome. Jobs are
de-duplicated by key, so asking twice for the same render runs it once.
"""
from __future__ import annotations

import os
import shutil
import threading
import time
import traceback
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable

from . import builder, events
from .logs import log
from .store import Project, new_id, read_json

_pool = ThreadPoolExecutor(max_workers=max(2, min(4, (os.cpu_count() or 2) // 2)), thread_name_prefix="we-job")
_jobs: dict[str, dict[str, Any]] = {}
_by_key: dict[str, Future] = {}
_lock = threading.Lock()


def submit(proj: Project, kind: str, key: str, fn: Callable[[Callable[[str, str], None]], Any],
           **info: Any) -> dict[str, Any]:
    """Run fn(progress) in the background unless a job with the same key is still active.
    `info` (project, doc, revision…) is stored on the job record."""
    with _lock:
        fut = _by_key.get(key)
        if fut is not None and not fut.done():
            running = [j for j in _jobs.values() if j["key"] == key and j["state"] in ("queued", "running")]
            if running:
                return running[-1]
        job = {"id": new_id("j"), "kind": kind, "key": key, "state": "queued", "message": "", **info}
        _jobs[job["id"]] = job
    events.publish(proj.id, {"type": "job", "job": dict(job)})

    def progress(stage: str, message: str) -> None:
        job["message"] = f"{stage}: {message}"
        events.publish(proj.id, {"type": "job", "job": dict(job)})

    def run() -> Any:
        log.info("job %s %s started %s", job["id"], kind, {k: v for k, v in info.items() if k != "project"})
        t0 = time.monotonic()
        job["state"] = "running"
        events.publish(proj.id, {"type": "job", "job": dict(job)})
        try:
            result = fn(progress)
            job["state"] = "done"
            log.info("job %s %s done in %.1fs", job["id"], kind, time.monotonic() - t0)
            job["result"] = result if isinstance(result, (dict, list, str, int, type(None))) else None
            return result
        except Exception as e:  # noqa: BLE001
            job["state"] = "failed"
            job["error"] = f"{type(e).__name__}: {e}"
            job["trace"] = traceback.format_exc(limit=6)
            log.error("job %s %s failed after %.1fs: %s\n%s", job["id"], kind, time.monotonic() - t0, job["error"], job["trace"])
            raise
        finally:
            events.publish(proj.id, {"type": "job", "job": {k: v for k, v in job.items() if k != "trace"}})

    with _lock:
        _by_key[key] = _pool.submit(run)
    return job


def wait(job: dict[str, Any], timeout: float | None = None) -> Any:
    fut = _by_key.get(job["key"])
    return fut.result(timeout) if fut else None


def list_jobs(project_id: str | None = None) -> list[dict[str, Any]]:
    return [{k: v for k, v in j.items() if k != "trace"} for j in _jobs.values()
            if project_id is None or j.get("project") == project_id]


# ------------------------------------------------------------------ document jobs
def queue_render(project: Project, doc: str, version: int, rev: str) -> dict[str, Any]:
    def fn(progress):
        st = builder.render_once(project, doc, version, rev, progress=progress)
        events.publish(project.id, {"type": "build", "doc": doc, "version": version, "revision": rev, "status": st})
        if st["state"] == "failed":
            raise RuntimeError(st.get("error", "render failed"))
        return st
    return submit(project, "render", f"render:{project.id}:{doc}:{version}:{rev}", fn,
                  project=project.id, doc=doc, version=version, revision=rev)


def queue_import(project: Project, doc: str, version: int) -> dict[str, Any]:
    def fn(progress):
        project.set_version_status(doc, version, status="importing")
        events.publish(project.id, {"type": "document", "doc": doc})
        try:
            report = builder.import_document(project, doc, version, progress=progress)
        except Exception as e:
            project.set_version_status(doc, version, status="failed", error=f"{type(e).__name__}: {e}")
            events.publish(project.id, {"type": "document", "doc": doc})
            raise
        events.publish(project.id, {"type": "document", "doc": doc})
        queue_render(project, doc, version, project.accepted())
        return {"status": report["status"]}
    return submit(project, "import", f"import:{project.id}:{doc}:{version}", fn,
                  project=project.id, doc=doc, version=version)


def ensure_rendered(project: Project, doc: str, version: int, rev: str) -> dict[str, Any] | None:
    """The build status of (doc, version, rev); queues the render when there is none yet."""
    st = builder.build_status(project, doc, version, rev)
    meta = project.document(doc)
    ver = next((v for v in meta["versions"] if v["v"] == version), None)
    if ver and ver.get("status") == "imported":
        if st is None:
            queue_render(project, doc, version, rev)
            return {"state": "queued", "revision": rev}
        if st.get("state") == "done" and st.get("format") != builder.RENDER_FORMAT:
            queue_render(project, doc, version, rev)  # made with an older template: redo, keep showing it meanwhile
    return st


def recover(project: Project) -> list[str]:
    """After a restart: clear half-written render roots and retry interrupted imports."""
    notes = []
    builds = project.root / "builds"
    if builds.is_dir():
        for tmp in builds.glob("*/*/.*.tmp"):
            shutil.rmtree(tmp, ignore_errors=True)
            notes.append(f"removed interrupted render {tmp.parent.parent.name}/{tmp.name}")
    for meta in project.documents():
        for v in meta["versions"]:
            if v.get("status") in ("pending", "importing"):
                project.set_version_status(meta["id"], v["v"], status="pending", interrupted=True)
                queue_import(project, meta["id"], v["v"])
                notes.append(f"re-importing {meta['id']} v{v['v']}")
        st = read_json(builder.render_root(project, meta["id"], meta["current"], project.accepted()) / builder.STATUS_FILE)
        if st and st.get("state") in ("running", "queued"):
            queue_render(project, meta["id"], meta["current"], project.accepted())
    return notes
