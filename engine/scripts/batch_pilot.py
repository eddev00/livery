"""The pilot's batch run on 50 documents, without the UI.

    uv run python scripts/batch_pilot.py [--docs 50] [--keep]

Builds a mixed batch (generated reports and supplier sheets with grouped and plain tables,
the test fixtures and recorded edge cases, and two corrupt files), then runs the production
workflow: import and verify every document, define a table treatment, find the tables that
share the selected table's structure, apply the treatment to the reviewed matches, render the
proposal with real content, accept it and export. Every document gets its own status; the
corrupt files and the document with unresolved tracked changes must fail without stopping the
others. Prints timings and the export summary.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--docs", type=int, default=50)
    ap.add_argument("--keep", action="store_true", help="keep the temporary project folder")
    a = ap.parse_args()
    home = Path(tempfile.mkdtemp(prefix="we-batch-"))
    os.environ.update(LIVERY_HOME=str(home), LIVERY_IGNORE_SYSTEM_FONTS="1")

    from desktop.docs import broken_doc, report_doc, small_doc
    from livery.desktop import builder, export, jobs, patterns, proposals
    from livery.desktop.design import Design
    from livery.desktop.store import Project

    builder.install_render_sandbox()
    src = home / "incoming"
    src.mkdir()
    files: list[Path] = []
    fixtures = sorted((ROOT / "tests" / "fixtures").glob("*.docx")) + \
        sorted((ROOT / "tests" / "fixtures" / "edge-cases").glob("*.docx"))
    for f in fixtures:
        files.append(Path(shutil.copy(f, src / f.name)))
    files += [broken_doc(src / "corrupt-1.docx"), broken_doc(src / "corrupt-2.docx")]
    i = 0
    while len(files) < a.docs:
        i += 1
        if i % 3 == 0:
            files.append(report_doc(src / f"Operations report {i:02d}.docx", sentinel=i % 2 == 0,
                                    title=f"Operations report {i}"))
        else:
            files.append(small_doc(src / f"Supplier sheet {i:02d}.docx", title=f"Supplier sheet {i}", grouped=i % 4 != 1))

    p = Project.create("Batch pilot")
    t0 = time.time()
    queued = []
    for f in files:
        doc, v = p.add_source(f)
        queued.append(jobs.queue_import(p, doc, v))
    for j in queued:
        try:
            jobs.wait(j, timeout=1800)
        except Exception:  # noqa: BLE001 - recorded per document
            pass
    for d in p.documents():  # imports queue their renders; wait for those too
        if d["versions"][-1].get("status") == "imported":
            builder.render_once(p, d["id"], d["current"], p.accepted())
    t_import = time.time() - t0
    statuses = {}
    for d in p.documents():
        ver = d["versions"][-1]
        st = builder.build_status(p, d["id"], d["current"], p.accepted()) or {}
        statuses[d["name"]] = ver["status"] if ver["status"] != "imported" else st.get("report_status", st.get("state"))
    print(f"imported and verified {len(files)} documents in {t_import:.0f}s "
          f"({t_import / len(files):.1f}s per document, {jobs._pool._max_workers} workers)")
    print("  statuses:", json.dumps({s: list(statuses.values()).count(s) for s in sorted(set(statuses.values()))}))
    for name, s in statuses.items():
        if s in ("FAIL", "failed"):
            print(f"  {s}: {name}")

    # A treatment, then the matching tables across the batch.
    t1 = time.time()
    ref_doc = next(d for d in p.documents() if d["name"].startswith("Operations report"))
    base = p.design(p.accepted())
    d = Design(base.theme_text, json.loads(json.dumps(base.design)), base.custom_text)
    d.design["treatments"]["Grouped figures"] = {"kind": "table", "style": {
        "header_fill": "#1F3A5F", "header_color": "#FFFFFF", "rules": "horizontal", "stripe_fill": "#F2F4F7"}}
    meta = proposals.create(p, d, base=p.accepted(), origin="manual", note="Define Grouped figures")
    for j in proposals.render_real(p, meta["id"], [ref_doc["id"]]):
        jobs.wait(j, timeout=600)
    proposals.accept(p, meta["id"])
    grouped = next(e for e in builder.inventory(p, ref_doc["id"], ref_doc["current"])
                   if e["kind"] == "table" and e["table"]["header_rows"] == 2)
    found = patterns.candidates(p, ref_doc["id"], grouped["id"])
    matches = [c for c in found["candidates"] if c["match"] == "match"]
    similar = [c for c in found["candidates"] if c["match"] == "similar"]
    print(f"signature {found['signature']}: {len(matches)} matching table(s), {len(similar)} similar (left out)")
    meta = patterns.apply(p, "Grouped figures", [{"doc": c["doc"], "element": c["element"]} for c in matches])
    for j in proposals.render_real(p, meta["id"]):
        try:
            jobs.wait(j, timeout=1800)
        except Exception:  # noqa: BLE001
            pass
    real = proposals.get(p, meta["id"])["real"]
    print(f"proposal rendered for {len(real)} document(s): "
          + json.dumps({s: [r.get("report_status") for r in real.values()].count(s) for s in ("PASS", "WARN", "FAIL")}))
    rev = proposals.accept(p, meta["id"])["revision"]
    t_apply = time.time() - t1
    print(f"accepted {rev} in {t_apply:.0f}s (treatment, matching, real renders)")

    t2 = time.time()
    out = home / "export"
    result = export.export(p, out)
    print(f"exported in {time.time() - t2:.0f}s: {json.dumps(result['summary'])}")
    failed = [r for r in result["documents"] if not r["exported"]]
    for r in failed:
        print(f"  not exported: {r['name']}: {(r['failures'] or ['?'])[0][:110]}")
    # every document that failed import or verification is reported and not exported; all others are
    must_fail = {name for name, s in statuses.items() if s in ("FAIL", "failed")}
    ok = {r["name"] for r in failed} == must_fail and {"corrupt-1.docx", "corrupt-2.docx", "tracked.docx"} <= must_fail \
        and result["summary"]["exported"] == len(files) - len(must_fail)
    print("RESULT", "PASS" if ok else "FAIL", f"(report: {out / 'livery-report.md'})")
    if not a.keep:
        shutil.rmtree(home, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
