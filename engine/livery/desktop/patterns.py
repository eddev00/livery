"""Reusing treatments across the batch.

Tables are matched by a structural signature computed locally: column count, header rows
and the merged cells of the header. Structure is only a hint: the user reviews candidate
matches before a treatment is applied, and can assign a treatment to any element by hand
when two tables look alike but serve different purposes. Applying creates a proposal, so
it goes through the same render, review and accept steps as any other design change.
"""
from __future__ import annotations

from typing import Any

from . import builder, proposals
from .store import Conflict, NotFound, Project, read_json


def table_signature(shape: dict[str, Any]) -> str:
    spans = ",".join(f"r{s['row']}c{s['col']}:{s['rowspan']}x{s['colspan']}"
                     for s in sorted(shape.get("header_spans", []), key=lambda s: (s["row"], s["col"])))
    return f"cols{shape['columns']}·head{shape['header_rows']}" + (f"·merge[{spans}]" if spans else "")


def _labels(project: Project, doc: str, version: int) -> dict[str, dict[str, Any]]:
    """Element labels and pages from the accepted render, when there is one."""
    root = builder.render_root(project, doc, version, project.accepted())
    smap = read_json(root / builder.SELECTION_FILE) or {}
    return {e["id"]: e for e in smap.get("elements", [])}


def candidates(project: Project, doc: str, element: str) -> dict[str, Any]:
    """Tables across the project that share the structure of `doc/element`.
    "match": same signature; "similar": same columns and header rows, different merges."""
    meta = project.document(doc)
    inv = {e["id"]: e for e in builder.inventory(project, doc, meta["current"])}
    if element not in inv:
        raise NotFound(f"{doc}/{element}")
    ref_el = inv[element]
    if ref_el["kind"] != "table":
        raise Conflict("pattern matching is available for tables; assign headings and paragraphs by hand")
    sig = table_signature(ref_el["table"])
    assignments = project.design(project.accepted()).design.get("assignments", {})
    out = []
    for d in project.documents():
        labels = _labels(project, d["id"], d["current"])
        for e in builder.inventory(project, d["id"], d["current"]):
            if e["kind"] != "table":
                continue
            s = table_signature(e["table"])
            same_frame = (e["table"]["columns"], e["table"]["header_rows"]) == \
                         (ref_el["table"]["columns"], ref_el["table"]["header_rows"])
            if s != sig and not same_frame:
                continue
            lab = labels.get(e["id"], {})
            out.append({"doc": d["id"], "doc_name": d["name"], "element": e["id"],
                        "label": lab.get("label", e["id"]), "page": (lab.get("rects") or [{}])[0].get("page"),
                        "match": "match" if s == sig else "similar", "signature": s,
                        "assigned": assignments.get(f"{d['id']}/{e['id']}"),
                        "is_reference": d["id"] == doc and e["id"] == element})
    return {"signature": sig, "candidates": out}


def apply(project: Project, treatment: str, refs: list[dict[str, str]], note: str | None = None) -> dict[str, Any]:
    """Propose assigning `treatment` to the reviewed elements `refs` ([{doc, element}])."""
    base_rev = project.accepted()
    base = project.design(base_rev)
    tr = base.design.get("treatments", {}).get(treatment)
    if tr is None:
        raise NotFound(f"treatment {treatment!r}")
    kinds = builder.element_kinds(project)
    assignments = dict(base.design.get("assignments", {}))
    for r in refs:
        key = f"{r['doc']}/{r['element']}"
        if kinds.get(key) != tr["kind"]:
            raise Conflict(f"{key} is not a {tr['kind']}")
        assignments[key] = treatment
    new = base.with_design({**base.design, "assignments": assignments})
    meta = proposals.create(project, new, base=base_rev, origin="batch",
                            note=note or f"Apply “{treatment}” to {len(refs)} element(s)",
                            focus=sorted({r["doc"] for r in refs}))
    if meta is None:
        raise Conflict("those elements already use this treatment")
    return meta


def unassign(project: Project, refs: list[dict[str, str]]) -> dict[str, Any]:
    base_rev = project.accepted()
    base = project.design(base_rev)
    assignments = {k: v for k, v in base.design.get("assignments", {}).items()
                   if k not in {f"{r['doc']}/{r['element']}" for r in refs}}
    new = base.with_design({**base.design, "assignments": assignments})
    meta = proposals.create(project, new, base=base_rev, origin="batch", note=f"Remove treatment from {len(refs)} element(s)",
                            focus=sorted({r["doc"] for r in refs}))
    if meta is None:
        raise Conflict("nothing to remove")
    return meta
