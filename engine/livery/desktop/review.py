"""Review state: the documents the user marked done, and whether later design changes made
them look different.

Marking a document done records the document version and the design revision it was
approved with (documents[<doc>]["done"] in project.json). A done document stays done while
the accepted design renders it exactly like the approved one: the two PDFs are compared
page by page as small grayscale rasters, so a change that does not touch the document (a
rule that matches nothing in it, a treatment used elsewhere) never flags it. Otherwise it
needs a re-check: its pages changed, or a new version of the Word file was imported.

    builds/<doc>/v<n>/<rev>/pages.json   page fingerprints of that render's PDF (a cache)
"""
from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path
from typing import Any

from ..mapping import parse_expect
from ..tools import require_tools, run
from . import builder
from .store import Conflict, NotFound, Project, now, read_json, write_json

PAGES_FILE = "pages.json"
FINGERPRINT_DPI = 30  # coarse enough to be quick, fine enough to see a changed colour or line


def fingerprints(root: Path) -> list[str] | None:
    """One hash per page of the render's PDF, cached next to it; None without a PDF."""
    pdf = builder.pdf_of(root)
    if pdf is None:
        return None
    stamp = pdf.stat().st_mtime_ns
    cached = read_json(root / PAGES_FILE)
    if cached and cached.get("pdf") == pdf.name and cached.get("mtime") == stamp and cached.get("dpi") == FINGERPRINT_DPI:
        return cached["pages"]
    tools = require_tools("pdftoppm")
    with tempfile.TemporaryDirectory(prefix="livery-pages-") as tmp:
        r = run([tools["pdftoppm"], "-r", str(FINGERPRINT_DPI), "-gray", pdf, Path(tmp) / "p"], check=False)
        if not r.ok:
            raise RuntimeError(f"pdftoppm failed: {r.stderr.strip()}")
        # pdftoppm pads page numbers to the page count's width, so names sort in page order
        pages = [hashlib.sha256(f.read_bytes()).hexdigest()[:16] for f in sorted(Path(tmp).glob("p-*.pgm"))]
    write_json(root / PAGES_FILE, {"pdf": pdf.name, "mtime": stamp, "dpi": FINGERPRINT_DPI, "pages": pages})
    return pages


def compare(before: Path, after: Path) -> dict[str, Any]:
    """How the render in `after` differs from the one in `before`: "same", "changed" (with
    the 1-based pages of `after` that differ, and both page counts), or "unavailable"."""
    try:
        a, b = fingerprints(before), fingerprints(after)
    except RuntimeError:
        return {"state": "unavailable"}
    if a is None or b is None:
        return {"state": "unavailable"}
    pages = [i + 1 for i in range(len(b)) if i >= len(a) or a[i] != b[i]]
    if not pages and len(a) == len(b):
        return {"state": "same"}
    return {"state": "changed", "pages": pages, "before": len(a), "after": len(b)}


def mark(project: Project, doc: str, done: bool) -> dict[str, Any]:
    """Mark a document done with the accepted design, or clear the mark. Only a finished
    render that passed verification (PASS or WARN) can be marked done."""
    with project.lock:
        st = project.state()
        meta = st["documents"].get(doc)
        if meta is None:
            raise NotFound(doc)
        if not done:
            meta.pop("done", None)
        else:
            rev = st["accepted"]
            build = builder.build_status(project, doc, meta["current"], rev) or {}
            if build.get("state") != "done":
                raise Conflict("wait for the document to finish rendering before marking it done")
            if build.get("report_status") == "FAIL":
                raise Conflict("this document fails verification; fix it before marking it done")
            meta["done"] = {"revision": rev, "version": meta["current"], "at": now()}
        project.save(st)
    return state_of(project, meta, st["accepted"], st["history"])


def _changes_since(project: Project, rev: str, history: list[str]) -> list[dict[str, Any]]:
    """The accepted changes made after `rev`, oldest first (empty when `rev` was undone)."""
    if rev not in history:
        return []
    later = set(history[history.index(rev) + 1:])
    return [{"id": c["id"], "note": c["note"], "kind": c["kind"]} for c in project.changes() if c["id"] in later]


def state_of(project: Project, meta: dict[str, Any], accepted: str, history: list[str]) -> dict[str, Any]:
    """The review state of one document with the accepted design:
    "open" (not marked done), "done", "checking" (the accepted render is not finished yet),
    or "recheck" with a reason: "version" (a new Word file), "design" (its pages changed;
    `pages` lists them) or "unavailable" (the approved render is gone)."""
    done = meta.get("done")
    if not done:
        return {"state": "open"}
    out: dict[str, Any] = {"approved": done}
    if done["version"] != meta["current"]:
        return {**out, "state": "recheck", "reason": "version"}
    if done["revision"] == accepted:
        return {**out, "state": "done"}
    now_root = builder.render_root(project, meta["id"], meta["current"], accepted)
    if (builder.build_status_at(now_root) or {}).get("state") != "done":
        return {**out, "state": "checking"}
    diff = compare(builder.render_root(project, meta["id"], done["version"], done["revision"]), now_root)
    if diff["state"] == "same":
        return {**out, "state": "done"}
    reason = "design" if diff["state"] == "changed" else "unavailable"
    return {**out, "state": "recheck", "reason": reason, "pages": diff.get("pages", []),
            "changes": _changes_since(project, done["revision"], history)}


def proposal_impact(project: Project, meta: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """For each done document a pending proposal touches: would accepting it change the
    document's pages? "pending" until the proposal is rendered with real content."""
    from .proposals import real_root
    out = {}
    for doc in meta.get("documents", []):
        try:
            d = project.document(doc)
        except NotFound:
            continue
        done = d.get("done")
        if not done or done["version"] != d["current"]:
            continue
        real = real_root(project, meta["id"], doc)
        if (builder.build_status_at(real) or {}).get("state") != "done":
            out[doc] = {"state": "pending"}
            continue
        diff = compare(builder.render_root(project, doc, done["version"], done["revision"]), real)
        out[doc] = diff if diff["state"] != "unavailable" else {"state": "changed", "pages": []}
    return out


def stats(project: Project) -> dict[str, Any]:
    """Project statistics: review and verification counts and what each mapping rule matched
    across the documents. Reads what is already rendered; queues nothing."""
    st = project.state()
    accepted, history = st["accepted"], st["history"]
    design = project.design(accepted)
    review = {"open": 0, "done": 0, "checking": 0, "recheck": 0}
    verification = {"PASS": 0, "WARN": 0, "FAIL": 0, "working": 0}
    recheck = []
    rules = [{"name": r["name"], "expect": r.get("expect"), "matched": 0, "documents": 0, "outside": []}
             for r in design.mapping().get("rule", [])]
    key = design.mapping_key()
    counted = 0
    for meta in project.documents():
        doc, version = meta["id"], meta["current"]
        ver = next((v for v in meta["versions"] if v["v"] == version), {})
        build = builder.build_status(project, doc, version, accepted) or {}
        if ver.get("status") == "failed" or build.get("state") == "failed":
            verification["FAIL"] += 1
        elif ver.get("status") != "imported" or build.get("state") != "done":
            verification["working"] += 1
        else:
            verification[build.get("report_status") or "PASS"] += 1
        rs = state_of(project, meta, accepted, history)
        review[rs["state"]] += 1
        if rs["state"] == "recheck":
            recheck.append({"doc": doc, "name": meta["name"], "reason": rs["reason"], "pages": rs.get("pages", [])})
        counts = (read_json(builder.conversion_root(project, doc, version, key) / builder.DOC_DIR / "report.json")
                  or {}).get("info", {}).get("mapping_rules")
        if counts is None:
            continue
        counted += 1
        for r in rules:
            n = counts.get(r["name"], 0)
            r["matched"] += n
            r["documents"] += n > 0
            if r["expect"]:
                lo, hi = parse_expect(r["expect"])
                if n < lo or (hi is not None and n > hi):
                    r["outside"].append(doc)
    return {"documents": sum(review.values()), "review": review, "recheck": recheck, "verification": verification,
            "rules": rules, "unused_rules": [r["name"] for r in rules if counted and r["matched"] == 0]}
