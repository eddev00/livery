"""Export: verified PDFs of every document with the accepted design, and a batch report.

Each document gets its own status. A document whose render failed or whose integrity
checks FAIL is not exported; the others are. Warnings are listed in the report.
"""
from __future__ import annotations

import re
import shutil
import time
from pathlib import Path
from typing import Any, Callable

from . import builder
from .store import Project, write_json


def _safe_name(name: str, taken: set[str]) -> str:
    stem = re.sub(r"[^\w\-. ()]+", "_", Path(name).stem).strip(" .") or "document"
    candidate, i = stem, 2
    while candidate.lower() in taken:
        candidate = f"{stem} ({i})"
        i += 1
    taken.add(candidate.lower())
    return candidate + ".pdf"


def export(project: Project, dest: Path, progress: Callable[[str, str], None] | None = None,
           only: list[str] | None = None) -> dict[str, Any]:
    """Export every document, or just the ids in ``only``."""
    progress = progress or (lambda s, m: None)
    dest = Path(dest).expanduser().resolve()
    dest.mkdir(parents=True, exist_ok=True)
    rev = project.accepted()
    rows: list[dict[str, Any]] = []
    taken: set[str] = set()
    docs = [d for d in project.documents() if only is None or d["id"] in only]
    for i, d in enumerate(docs, 1):
        progress("exporting", f"{i}/{len(docs)} {d['name']}")
        ver = next((v for v in d["versions"] if v["v"] == d["current"]), {})
        row: dict[str, Any] = {"doc": d["id"], "name": d["name"], "version": d["current"]}
        try:
            if ver.get("status") != "imported":
                raise RuntimeError(f"import {ver.get('status', 'missing')}: {ver.get('error', '')}".strip(": "))
            st = builder.render_once(project, d["id"], d["current"], rev)
            root = builder.render_root(project, d["id"], d["current"], rev)
            report = builder.report_of(root) or {}
            row["status"] = report.get("status", "FAIL") if st.get("state") == "done" else "FAIL"
            row["warnings"] = [f"{c['name']}: {c['summary']}" for c in report.get("checks", []) if c["status"] == "WARN"]
            row["failures"] = [f"{c['name']}: {c['summary']}" for c in report.get("checks", []) if c["status"] == "FAIL"]
            if st.get("state") != "done":
                row["failures"].append(st.get("error", "render failed"))
            pdf = builder.pdf_of(root)
            if row["status"] in ("PASS", "WARN") and pdf is not None:
                out = _safe_name(d["name"], taken)
                shutil.copyfile(pdf, dest / out)
                row["pdf"] = out
                row["exported"] = True
            else:
                row["exported"] = False
        except Exception as e:  # noqa: BLE001 - one document never stops the batch
            row.update(status="FAIL", exported=False, warnings=[], failures=[f"{type(e).__name__}: {e}"])
        rows.append(row)
    summary = {"exported": sum(r["exported"] for r in rows), "failed": sum(not r["exported"] for r in rows),
               "warnings": sum(bool(r["warnings"]) for r in rows), "total": len(rows)}
    report = {"project": project.state()["name"], "revision": rev, "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
              "summary": summary, "documents": rows}
    write_json(dest / "livery-report.json", report)
    (dest / "livery-report.md").write_text(_markdown(report), encoding="utf-8")
    return {"dest": str(dest), **report}


def _markdown(r: dict[str, Any]) -> str:
    s = r["summary"]
    lines = [f"# Livery export — {r['project']}", "",
             f"Design revision {r['revision']}, {r['generated_at']}.", "",
             f"**{s['exported']} of {s['total']} exported**, {s['failed']} not exported, "
             f"{s['warnings']} with warnings.", "", "| Document | Status | PDF |", "|---|---|---|"]
    for d in r["documents"]:
        lines.append(f"| {d['name']} | {d['status']} | {d.get('pdf') or '—'} |")
    for d in r["documents"]:
        if d["failures"] or d["warnings"]:
            lines += ["", f"## {d['name']} — {d['status']}"]
            lines += [f"- FAIL {x}" for x in d["failures"]] + [f"- WARN {x}" for x in d["warnings"]]
    return "\n".join(lines) + "\n"
