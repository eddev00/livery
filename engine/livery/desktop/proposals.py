"""Design proposals: a candidate revision waiting for review.

Every proposal records the accepted revision it started from. It is rendered with the
real content locally (never sent anywhere), and accepted atomically only when those renders
passed verification and the accepted revision has not moved on in the meantime; otherwise
it is stale. Discarding or undoing never touches source documents.

A proposal that changes the mapping converts every document again. Its references to the
documents in meta["remap"]["docs"] were made with the base revision's mapping: each real
render carries them over to the new conversion (builder.remap_refs), and accepting stores
the carried references; those that could not be carried are listed in meta["remap"]["dropped"].

    proposals/<id>/meta.json     base, status, origin, change scope, render states
    proposals/<id>/design/       the proposed theme.toml, design.json, custom.typ
    proposals/<id>/synthetic/    synthetic (lorem twin) renders, doc-NN.pdf
    proposals/<id>/real/<doc>/   real-content render roots, like builds/
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from . import builder, events, jobs, review
from .design import Design, DesignError, change_scope, revert, validate
from .store import Conflict, NotFound, Project, new_id, now, read_json, write_json

ACTIVE = ("pending",)


def _meta_file(project: Project, pid: str) -> Path:
    return project.proposal_dir(pid) / "meta.json"


def get(project: Project, pid: str) -> dict[str, Any]:
    meta = read_json(_meta_file(project, pid))
    if meta is None:
        raise NotFound(pid)
    meta["real"] = {doc: builder.build_status_at(real_root(project, pid, doc)) or {"state": "not rendered"}
                    for doc in meta.get("documents", [])}
    meta["stale"] = meta["status"] == "pending" and meta["base"] != project.accepted()
    if meta["status"] == "pending":
        meta["done_impact"] = review.proposal_impact(project, meta)
    if meta.get("remap") and meta["status"] == "pending":
        lost = []
        for doc in meta["remap"]["docs"]:
            lost += (read_json(real_root(project, pid, doc) / builder.REMAP_FILE) or {}).get("dropped", [])
        meta["remap"]["dropped"] = sorted(lost)
    return meta


def list_all(project: Project) -> list[dict[str, Any]]:
    d = project.root / "proposals"
    if not d.is_dir():
        return []
    out = [get(project, p.name) for p in d.iterdir() if (p / "meta.json").exists()]
    return sorted(out, key=lambda m: m["created"], reverse=True)


def design_of(project: Project, pid: str) -> Design:
    return Design.load(project.proposal_dir(pid) / "design")


def real_root(project: Project, pid: str, doc: str) -> Path:
    return project.proposal_dir(pid) / "real" / doc


def _affected_documents(project: Project, scope: dict[str, Any], extra: list[str]) -> list[str]:
    all_docs = [d["id"] for d in project.documents()]
    docs = all_docs if scope["scope"] == "global" else [d for d in all_docs if d in scope["documents"]]
    return docs + [d for d in extra if d in all_docs and d not in docs]


def create(project: Project, design: Design, *, base: str, origin: str, note: str,
           focus: list[str] | None = None, pid: str | None = None,
           remap_docs: list[str] | None = None, reverts: str | None = None) -> dict[str, Any] | None:
    """Store (or replace, with `pid`) a proposal. Validates the design against the project's
    elements first. Returns None when the design equals its base.

    When the mapping changes, `remap_docs` are the documents whose references were made with
    the base mapping (default: all); the others' were already made with the new one. Their
    references are checked once they are carried over, when the proposal is accepted."""
    base_design = project.design(base)
    scope = change_scope(base_design, design)
    validate(design, None if scope["mapping"] else builder.element_kinds(project, base_design))
    if not scope["files"]:
        return None
    all_docs = [d["id"] for d in project.documents()]
    remap = {"docs": [d for d in (all_docs if remap_docs is None else remap_docs) if d in all_docs],
             "dropped": []} if scope["mapping"] else None
    with project.lock:
        pid = pid or new_id("q")
        d = project.proposal_dir(pid)
        old = read_json(d / "meta.json")
        if d.exists():
            shutil.rmtree(d)
        design.write(d / "design")
        meta = {"id": pid, "base": base, "status": "pending", "origin": origin, "note": note,
                "created": (old or {}).get("created", now()), "updated": now(), "scope": scope,
                "documents": _affected_documents(project, scope, focus or []), "focus": focus or [], "remap": remap}
        if reverts:
            meta["reverts"] = reverts
        write_json(d / "meta.json", meta)
    events.publish(project.id, {"type": "proposal", "proposal": pid, "status": "pending"})
    return get(project, pid)


def render_real(project: Project, pid: str, docs: list[str] | None = None) -> list[dict[str, Any]]:
    """Queue local real-content renders of the proposal for its documents (or `docs`)."""
    meta = get(project, pid)
    if meta["status"] != "pending":
        raise Conflict(f"proposal is {meta['status']}")
    design = design_of(project, pid)
    base = project.design(meta["base"])
    remap_docs = set((meta.get("remap") or {}).get("docs", []))
    queued = []
    for doc in docs or meta["documents"]:
        version = project.document(doc)["current"]

        def fn(progress, doc=doc, version=version):
            st = builder.render_document(project, doc, version, pid, design=design,
                                         root=real_root(project, pid, doc), progress=progress,
                                         refs_from=base if doc in remap_docs else None)
            events.publish(project.id, {"type": "proposal", "proposal": pid, "doc": doc, "status": st})
            return st
        queued.append(jobs.submit(project, "proposal-render", f"proposal:{project.id}:{pid}:{doc}", fn,
                                  project=project.id, doc=doc, proposal=pid))
    return queued


def accept(project: Project, pid: str) -> dict[str, Any]:
    """Make the proposal the accepted design. Refused while renders are missing, and when the
    accepted revision changed since the proposal was made (stale). Documents that fail
    verification with the new design do not block it: they are returned as `failed` so the
    user is warned, and their own export checks still apply."""
    with project.lock:
        meta = get(project, pid)
        if meta["status"] != "pending":
            raise Conflict(f"proposal is {meta['status']}")
        if meta["stale"]:
            _set_status(project, pid, "stale")
            raise Conflict("the accepted design changed since this proposal was made; it is stale")
        missing = [d for d, st in meta["real"].items() if st.get("state") != "done"]
        if missing:
            raise Conflict(f"render the proposal with real content first ({len(missing)} document(s) not rendered)")
        failed = [d for d, st in meta["real"].items() if st.get("report_status") == "FAIL"]
        design = design_of(project, pid)
        extra: dict[str, Any] = {}
        if meta.get("remap"):
            design = _carried(project, meta, design)
            validate(design, builder.element_kinds(project, design))
            extra["remap"] = meta["remap"]
        rev = project.add_revision(design, parent=meta["base"], kind=meta["origin"].split(":")[0],
                                   note=meta["note"], expect_accepted=meta["base"],
                                   reverts=meta.get("reverts"))
        _set_status(project, pid, "accepted", revision=rev, **extra)
    _carry_builds(project, meta, rev)
    events.publish(project.id, {"type": "design", "accepted": rev})
    return {"revision": rev, "failed": failed}


def _carried(project: Project, meta: dict[str, Any], design: Design) -> Design:
    """The proposal's design with the references its real renders carried to the new mapping."""
    d = design.design
    for doc in meta["remap"]["docs"]:
        remap = read_json(real_root(project, meta["id"], doc) / builder.REMAP_FILE)
        if remap is None:
            raise Conflict(f"render the proposal with real content first ({doc} has not been converted with the new mapping)")
        d = builder.replace_doc_refs(d, doc, remap)
    return design.with_design(d)


def _carry_builds(project: Project, meta: dict[str, Any], rev: str) -> None:
    """Reuse what is already rendered for the new revision: the proposal's real renders, and
    for an element-scoped change the base revision's builds of untouched documents."""
    for d in project.documents():
        doc, version = d["id"], d["current"]
        target = builder.render_root(project, doc, version, rev)
        src = real_root(project, meta["id"], doc)
        base_build = builder.render_root(project, doc, version, meta["base"])
        if (src / builder.STATUS_FILE).exists():
            shutil.copytree(src, target, symlinks=True, dirs_exist_ok=True)
        elif meta["scope"]["scope"] == "elements" and doc not in meta["documents"] and \
                (read_json(base_build / builder.STATUS_FILE) or {}).get("state") == "done":
            shutil.copytree(base_build, target, symlinks=True, dirs_exist_ok=True)
        else:
            jobs.queue_render(project, doc, version, rev)
            continue
        st = read_json(target / builder.STATUS_FILE) or {}
        st["revision"] = rev
        write_json(target / builder.STATUS_FILE, st)


def discard(project: Project, pid: str) -> None:
    with project.lock:
        meta = get(project, pid)
        if meta["status"] != "pending":
            raise Conflict(f"proposal is {meta['status']}")
        _set_status(project, pid, "discarded")
    events.publish(project.id, {"type": "proposal", "proposal": pid, "status": "discarded"})


def _set_status(project: Project, pid: str, status: str, **extra: Any) -> None:
    f = _meta_file(project, pid)
    meta = read_json(f)
    meta.update(status=status, updated=now(), **extra)
    write_json(f, meta)


def undo(project: Project) -> dict[str, Any]:
    rev = project.undo()
    for d in project.documents():
        jobs.ensure_rendered(project, d["id"], d["current"], rev)
    events.publish(project.id, {"type": "design", "accepted": rev})
    return {"revision": rev}


def remove_change(project: Project, rev: str) -> dict[str, Any]:
    """Remove one accepted change. The latest one is undone at once (its parent was already
    verified); an earlier one becomes a proposal: the accepted design without that change,
    rendered and accepted like any other. Refused when later changes depend on it."""
    st = project.state()
    if rev not in st["history"][1:]:
        raise NotFound(rev)
    change = next(c for c in project.changes() if c["id"] == rev)
    if change["reverted_by"]:
        raise Conflict(f"{rev} was already removed by {change['reverted_by']}")
    if rev == st["history"][-1]:
        return {**undo(project), "proposal": None}
    parent = st["history"][st["history"].index(rev) - 1]
    current = st["accepted"]
    try:
        design = revert(project.design(parent), project.design(rev), project.design(current))
        meta = create(project, design, base=current, origin="revert", reverts=rev,
                      note=f"Remove {rev}" + (f": {change['note']}" if change["note"] else ""))
    except DesignError as e:
        raise Conflict(f"{rev} cannot be removed on its own: " + "; ".join(e.problems)) from e
    if meta is None:
        raise Conflict(f"{rev} no longer changes the design")
    return {"revision": current, "proposal": meta}
