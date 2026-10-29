"""Import and render documents for the desktop app.

Import (once per document version): the engine's `build_document` converts the immutable
source copy with the neutral template. That produces content.typ with element markers, the
element inventory, the LibreOffice original.pdf and the lorem-ipsum twin (the synthetic
document agents work with).

Conversions with another mapping (per document version and mapping): a revision whose
mapping.toml is not the default one needs its own conversion of the source, made on demand
under imports/<doc>/v<n>/mapped/<mapping key>/, with the lorem twin converted again from the
anonymised Word file of the import. Element IDs differ between conversions; references are
carried from one to the other by aligning the two inventories (remap_refs).

Render (per document version and design revision): the converted content is copied into a
fresh render root next to a project-local template holding that revision's design, and
the engine's `verify_document` compiles and re-checks it. The selection map is read from
that same render, so overlays always match the PDF the user is looking at. Every Typst
run happens in the offline render sandbox.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
import zipfile
from difflib import SequenceMatcher
from html import unescape
from pathlib import Path
from typing import Any, Callable

from .. import render as engine_render
from ..convert import ELEMENTS_FILE, MARKER_RE, find_soffice, strip_markers
from ..anonymize import AnonymizeError
from ..pipeline import BuildOptions, build_document, convert_twin_with, verify_document
from ..render import compute_root, typst_command
from ..report import write_report
from ..verify import CheckResult, worst
from . import sandbox
from .design import Design, resolve_for_document
from .paths import CORE_FILES, NEUTRAL_DIR, NEUTRAL_FONTS
from .logs import log
from .store import Project, read_json, write_json

Progress = Callable[[str, str], None]
DOC_DIR = "doc"          # inside a render root: the document
TEMPLATE_DIR = "template"
SELECTION_FILE = "selection.json"
STATUS_FILE = "status.json"
REMAP_FILE = "remap.json"  # in a render root: the document's references carried to this mapping
# Bumped when the template or the checks change, so finished renders made before are redone.
RENDER_FORMAT = 3
RENDER_TIMEOUT = int(os.environ.get("LIVERY_RENDER_TIMEOUT", "180"))

KIND_LABELS = {"heading": "Heading", "paragraph": "Paragraph", "table": "Table",
               "list": "List", "figure": "Figure", "other": "Block"}
SELECTABLE = {"heading", "paragraph", "table"}


def _sandboxed_typst(cmd: list[Any], root: Path, theme_path: Path) -> list[Any]:
    return sandbox.render_command([str(c) for c in cmd], root, [NEUTRAL_FONTS], RENDER_TIMEOUT)


def install_render_sandbox() -> None:
    """Every Typst compile of the engine now runs in the offline render sandbox."""
    engine_render.COMMAND_WRAPPER = _sandboxed_typst


# ------------------------------------------------------------------ template roots
def write_template(folder: Path, design: Design) -> Path:
    """A project-local template: the app's core template, the revision's design files and
    the app's fonts. Returns the theme path."""
    folder.mkdir(parents=True, exist_ok=True)
    for name in CORE_FILES:
        shutil.copyfile(NEUTRAL_DIR / name, folder / name)
    design.write(folder)
    assets = folder / "assets"
    if not assets.exists():
        assets.symlink_to(NEUTRAL_DIR / "assets", target_is_directory=True)
    return folder / "theme.toml"


def _docx_title(path: Path) -> str | None:
    try:
        with zipfile.ZipFile(path) as z:
            core = z.read("docProps/core.xml").decode("utf-8", errors="replace")
    except (KeyError, OSError, zipfile.BadZipFile):
        return None
    m = re.search(r"<dc:title>([^<]*)</dc:title>", core)
    return unescape(m.group(1)).strip() if m and m.group(1).strip() else None


# ------------------------------------------------------------------ import
def import_document(project: Project, doc: str, version: int, progress: Progress | None = None) -> dict[str, Any]:
    """Convert one imported version with the neutral design. Returns the engine report."""
    progress = progress or (lambda s, m: None)
    src = project.source_dir(doc, version) / "source.docx"
    root = project.import_dir(doc, version)
    if root.exists():
        shutil.rmtree(root)
    theme = write_template(root / TEMPLATE_DIR, project.design("r0001"))
    meta = project.document(doc)
    title = _docx_title(src) or Path(meta["name"]).stem
    opts = BuildOptions(theme=theme, out_root=root, title=title, original_pdf=find_soffice() is not None, twins=True)
    report = build_document(src, opts, progress=progress)
    # build_document names the output folder after the file: <root>/source/
    out = root / "source"
    if out.exists():
        os.replace(out, root / DOC_DIR)
    for folder in (root / DOC_DIR, root / DOC_DIR / "lorem"):
        if (folder / ELEMENTS_FILE).exists():
            write_json(folder / ELEMENTS_FILE, normalize_inventory(read_json(folder / ELEMENTS_FILE, [])))
    inventory = read_json(root / DOC_DIR / ELEMENTS_FILE, [])
    project.set_version_status(doc, version, status="imported", report_status=report["status"],
                               elements=len(inventory), title=title)
    return report


def normalize_inventory(inv: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """pandoc's JSON encoder writes an empty Lua list as {}: make list fields lists again."""
    for e in inv:
        t = e.get("table")
        if t is not None:
            for key in ("header_spans", "widths"):
                if not isinstance(t.get(key), list):
                    t[key] = []
    return inv


MAPPED_DIR = "mapped"


def conversion_root(project: Project, doc: str, version: int, key: str) -> Path:
    """The folder holding DOC_DIR for (doc, version) converted with mapping `key`."""
    root = project.import_dir(doc, version)
    return root / MAPPED_DIR / key if key else root


def ensure_conversion(project: Project, doc: str, version: int, design: Design,
                      progress: Progress | None = None) -> Path:
    """conversion_root for `design`'s mapping, converting the source with it first when that
    has not been done. The default mapping's conversion is the import itself."""
    key = design.mapping_key()
    root = conversion_root(project, doc, version, key)
    if not key:
        return root
    with _lock_for(root):
        if (root / DOC_DIR / "report.json").exists():
            return root
        imported = project.import_dir(doc, version) / DOC_DIR
        if not (imported / "report.json").exists():
            raise RuntimeError(f"{doc} v{version} is not imported yet")
        mapping = design.mapping()
        tmp = root.with_name(f".{root.name}.tmp")
        if tmp.exists():
            shutil.rmtree(tmp)
        theme = write_template(tmp / TEMPLATE_DIR, project.design("r0001"))
        title = (read_json(imported / "livery.json") or {}).get("title")
        build_document(project.source_dir(doc, version) / "source.docx",
                       BuildOptions(theme=theme, out_root=tmp, title=title, original_pdf=False, twins=False,
                                    mapping=mapping, render=False), progress=progress)
        os.replace(tmp / "source", tmp / DOC_DIR)
        twin_docx = imported / "lorem" / "lorem.docx"
        if twin_docx.exists():
            try:
                convert_twin_with(tmp / DOC_DIR, twin_docx, mapping, progress=progress)
            except (AnonymizeError, RuntimeError, ValueError, OSError) as e:
                log.warning("twin of %s v%d with mapping %s refused: %s", doc, version, key, str(e)[:200])
        for folder in (tmp / DOC_DIR, tmp / DOC_DIR / "lorem"):
            if (folder / ELEMENTS_FILE).exists():
                write_json(folder / ELEMENTS_FILE, normalize_inventory(read_json(folder / ELEMENTS_FILE, [])))
        root.parent.mkdir(parents=True, exist_ok=True)
        if root.exists():
            shutil.rmtree(root)
        os.replace(tmp, root)
    return root


def inventory(project: Project, doc: str, version: int, twin: bool = False,
              design: Design | None = None) -> list[dict[str, Any]]:
    """The element inventory of (doc, version) as converted with `design`'s mapping (by
    default the accepted design's). Empty when that conversion has not been made."""
    key = (design or project.design(project.accepted())).mapping_key()
    d = conversion_root(project, doc, version, key) / DOC_DIR
    if twin:
        d = d / "lorem"
    return read_json(d / ELEMENTS_FILE, [])


def element_kinds(project: Project, design: Design | None = None) -> dict[str, str]:
    """Every element reference "<doc>/<element>" of the current versions, with its kind."""
    design = design or project.design(project.accepted())
    out = {}
    for meta in project.documents():
        for e in inventory(project, meta["id"], meta["current"], design=design):
            out[f"{meta['id']}/{e['id']}"] = e["kind"]
    return out


def align_elements(old: list[dict[str, Any]], new: list[dict[str, Any]]) -> dict[str, str]:
    """Element ID in `old` -> ID of the same element in `new`, for two conversions of one
    source: same kind and text, or same kind at the same place in a run of changed elements
    (a heading whose typed number is now kept). Elements that were dropped, or that changed
    kind, have no counterpart."""
    sm = SequenceMatcher(a=[(e["kind"], e.get("text", "")) for e in old],
                         b=[(e["kind"], e.get("text", "")) for e in new], autojunk=False)
    out: dict[str, str] = {}
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal" or (tag == "replace" and i2 - i1 == j2 - j1):
            for k in range(i2 - i1):
                if old[i1 + k]["kind"] == new[j1 + k]["kind"]:
                    out[old[i1 + k]["id"]] = new[j1 + k]["id"]
    return out


def remap_refs(design: dict[str, Any], doc: str, old: list[dict[str, Any]],
               new: list[dict[str, Any]]) -> tuple[dict[str, Any], list[str]]:
    """The assignments and overrides of `doc` moved from the `old` conversion's element IDs to
    the `new` one's: ({"assignments", "overrides"}, references that could not be carried)."""
    ids = align_elements(old, new)
    prefix = doc + "/"
    out: dict[str, Any] = {"assignments": {}, "overrides": {}}
    dropped = []
    for key in ("assignments", "overrides"):
        for ref, v in design.get(key, {}).items():
            if not ref.startswith(prefix):
                continue
            el = ref[len(prefix):]
            if el in ids:
                out[key][prefix + ids[el]] = v
            else:
                dropped.append(ref)
    return out, sorted(set(dropped))


def replace_doc_refs(design: dict[str, Any], doc: str, refs: dict[str, Any]) -> dict[str, Any]:
    """`design` with the assignments and overrides of `doc` replaced by `refs`."""
    prefix = doc + "/"
    out = dict(design)
    for key in ("assignments", "overrides"):
        out[key] = {**{r: v for r, v in design.get(key, {}).items() if not r.startswith(prefix)}, **refs.get(key, {})}
    return out


# ------------------------------------------------------------------ render
def _copy_content(src: Path, dest: Path) -> None:
    """The converted document without the previews and the twin: a render needs only content."""
    def ignore(folder: str, names: list[str]) -> set[str]:
        skip = {"lorem", "_preflight", "original.pdf"} if Path(folder) == src else set()
        return skip | {n for n in names if n.endswith(".pdf")}
    shutil.copytree(src, dest, ignore=ignore, symlinks=False)


def render_root(project: Project, doc: str, version: int, rev: str) -> Path:
    return project.build_dir(doc, version, rev)


_root_locks: dict[str, threading.Lock] = {}
_root_locks_guard = threading.Lock()


def _lock_for(root: Path) -> threading.Lock:
    with _root_locks_guard:
        return _root_locks.setdefault(str(root), threading.Lock())


def render_once(project: Project, doc: str, version: int, rev: str, progress: Progress | None = None) -> dict[str, Any]:
    """The finished render of (doc, version, rev), rendering it only if there is none yet.
    Concurrent callers wait for one render instead of starting their own."""
    root = render_root(project, doc, version, rev)
    with _lock_for(root):
        st = build_status_at(root)
        if st and st.get("state") == "done" and st.get("format") == RENDER_FORMAT:
            return st
        return _render(project, doc, version, rev, project.design(rev), root, progress)


def render_document(project: Project, doc: str, version: int, rev: str, design: Design | None = None,
                    root: Path | None = None, progress: Progress | None = None,
                    refs_from: Design | None = None) -> dict[str, Any]:
    """Render one document version with one design (by default revision `rev`) into `root`
    (by default the revision's build folder). Writes status.json, the report and the
    selection map; returns the status. With `refs_from`, the design's references to this
    document were made with that design's mapping: they are carried over to `design`'s
    conversion first, and what was carried (or lost) is written to REMAP_FILE."""
    root = root or render_root(project, doc, version, rev)
    with _lock_for(root):
        return _render(project, doc, version, rev, design or project.design(rev), root, progress, refs_from)


def _render(project: Project, doc: str, version: int, rev: str, design: Design, root: Path,
            progress: Progress | None, refs_from: Design | None = None) -> dict[str, Any]:
    status_file = root / STATUS_FILE
    started = time.time()
    tmp = root.with_name(f".{root.name}.tmp")
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    write_json(tmp / STATUS_FILE, {"state": "running", "revision": rev, "started": started})
    try:
        conv = ensure_conversion(project, doc, version, design, progress)
        theme = write_template(tmp / TEMPLATE_DIR, design)
        _copy_content(conv / DOC_DIR, tmp / DOC_DIR)
        inv = read_json(tmp / DOC_DIR / ELEMENTS_FILE, [])
        doc_design = design.design
        if refs_from is not None and refs_from.mapping_key() != design.mapping_key():
            old = read_json(ensure_conversion(project, doc, version, refs_from, progress) / DOC_DIR / ELEMENTS_FILE, [])
            refs, dropped = remap_refs(design.design, doc, old, inv)
            doc_design = replace_doc_refs(design.design, doc, refs)
            write_json(tmp / REMAP_FILE, {**refs, "dropped": dropped})
        write_json(tmp / DOC_DIR / "design.json", resolve_for_document(doc_design, doc, inv))
        report = verify_document(tmp / DOC_DIR, theme, progress=progress)
        _carry_import_checks(report, conv / DOC_DIR / "report.json")
        status: dict[str, Any] = {"state": "done", "format": RENDER_FORMAT, "revision": rev, "started": started, "finished": time.time(),
                                  "report_status": report["status"]}
        if report.get("pdf"):
            smap, check = selection_map(tmp, inv)
            write_json(tmp / SELECTION_FILE, smap)
            report["checks"].append(check)
            report["status"] = worst([CheckResult(c["name"], c["status"], c["summary"]) for c in report["checks"]])
            write_report(report, tmp / DOC_DIR)
            status["report_status"] = report["status"]
        else:
            status["error"] = "render failed"
            write_report(report, tmp / DOC_DIR)
        write_json(tmp / STATUS_FILE, status)
    except Exception as e:  # noqa: BLE001 - recorded per document, never stops a batch
        log.exception("render %s v%d %s failed", doc, version, rev)
        status = {"state": "failed", "revision": rev, "started": started, "finished": time.time(),
                  "error": f"{type(e).__name__}: {e}"}
        write_json(tmp / STATUS_FILE, status)
    if root.exists():
        shutil.rmtree(root)
    root.parent.mkdir(parents=True, exist_ok=True)
    os.replace(tmp, root)
    return status


# Checks only the import (conversion) runs. They describe the source document, not the design,
# so they hold for every revision: a document with unresolved tracked changes stays FAIL.
IMPORT_CHECKS = ("tracked_changes", "heading_numbering", "dropped_paragraphs", "outline_levels", "smartart",
                 "pandoc_warnings", "original_pdf", "lorem_twin", "mapping_rules", "mapping_text")


def _carry_import_checks(report: dict[str, Any], import_report: Path) -> None:
    imported = read_json(import_report) or {}
    have = {c["name"] for c in report["checks"]}
    carried = [c for c in imported.get("checks", []) if c["name"] in IMPORT_CHECKS and c["name"] not in have]
    report["checks"].extend(carried)
    report["status"] = worst([CheckResult(c["name"], c["status"], c["summary"]) for c in report["checks"]])


def build_status_at(root: Path) -> dict[str, Any] | None:
    return read_json(root / STATUS_FILE)


def build_status(project: Project, doc: str, version: int, rev: str) -> dict[str, Any] | None:
    return build_status_at(render_root(project, doc, version, rev))


def report_of(root: Path) -> dict[str, Any] | None:
    return read_json(root / DOC_DIR / "report.json")


def pdf_of(root: Path) -> Path | None:
    st = read_json(root / DOC_DIR / "livery.json")
    if not st:
        return None
    pdf = root / DOC_DIR / f"{st['name']}.pdf"
    return pdf if pdf.exists() else None


# ------------------------------------------------------------------ selection map
def _eval_map(root: Path) -> dict[str, Any]:
    doc = root / DOC_DIR
    main = doc / "main.typ"
    theme = root / TEMPLATE_DIR / "theme.toml"
    cmd = typst_command("compile", main, doc / "_map.pdf", root=compute_root(doc, theme), theme_path=theme)
    # typst eval takes the same world options as compile: swap the subcommand and the paths
    opts = [str(c) for c in cmd[2:-2]]
    ev = [str(cmd[0]), "eval", "query(<we-map>).first().value", "--in", str(main)] + opts
    if engine_render.COMMAND_WRAPPER is not None:
        ev = [str(c) for c in engine_render.COMMAND_WRAPPER(ev, compute_root(doc, theme), theme)]
    r = subprocess.run(ev, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("selection map query failed: " + r.stderr.strip()[:400])
    import json
    return json.loads(r.stdout)


def rectangles(raw: dict[str, Any]) -> dict[str, list[dict[str, float]]]:
    """Element ID -> one rectangle per page it occupies (points, origin top-left).
    An element that spans pages gets several rectangles sharing its ID."""
    area = raw["area"]
    ends = {e["id"]: e for e in raw["ends"]}
    out: dict[str, list[dict[str, float]]] = {}
    for s in raw["starts"]:
        e = ends.get(s["id"])
        if e is None:
            continue
        rects = []
        for page in range(s["page"], e["page"] + 1):
            top = s["y"] if page == s["page"] else area["top"]
            bottom = e["y"] if page == e["page"] else area["bottom"]
            if bottom - top >= 1:
                rects.append({"page": page, "x": area["left"], "y": top,
                              "w": area["right"] - area["left"], "h": bottom - top})
        out[s["id"]] = rects
    return out


def selection_map(root: Path, inv: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    """The selection map of a finished render, and the instrumentation check.

    The check is FAIL when the markers in content.typ, the inventory and the rendered markers
    disagree: then no element can be attributed reliably, and the export is blocked."""
    content = (root / DOC_DIR / "content.typ").read_text(encoding="utf-8")
    markers = MARKER_RE.findall(content)
    stripped = strip_markers(content)
    problems = []
    if len(markers) != 2 * len(inv):
        problems.append(f"{len(markers)} markers in content.typ for {len(inv)} elements")
    if "<we-" in stripped:
        problems.append("content.typ contains marker text outside the generated markers")
    ids = [e["id"] for e in inv]
    if len(set(ids)) != len(ids):
        problems.append("duplicate element IDs")
    raw = _eval_map(root)
    started = [s["id"] for s in raw["starts"]]
    ended = [e["id"] for e in raw["ends"]]
    if started != ids or ended != ids:
        problems.append("rendered markers do not match the element inventory")
    rects = rectangles(raw)
    counters: dict[str, int] = {}
    elements = []
    unmapped = []
    for e in inv:
        counters[e["kind"]] = counters.get(e["kind"], 0) + 1
        r = rects.get(e["id"], [])
        page = r[0]["page"] if r else None
        label = f"{KIND_LABELS.get(e['kind'], 'Block')} {counters[e['kind']]}" + (f" · Page {page}" if page else "")
        entry = {"id": e["id"], "kind": e["kind"], "label": label, "rects": r,
                 "selectable": e["kind"] in SELECTABLE and bool(r)}
        if e["kind"] == "heading":
            entry["level"] = e.get("level")
        if e["kind"] == "table":
            entry["table"] = e.get("table")
        if not r:
            unmapped.append(e["id"])
        elements.append(entry)
    smap = {"page": raw["page"], "pages": raw["pages"], "area": raw["area"], "elements": elements,
            "unmapped": unmapped}
    if problems:
        check = {"name": "instrumentation", "status": "FAIL", "summary": "; ".join(problems), "details": problems}
    else:
        summary = f"{len(inv)} element(s) mapped from the render"
        if unmapped:
            summary += f"; {len(unmapped)} produced no visible area (empty or dropped by layout)"
        check = {"name": "instrumentation", "status": "PASS", "summary": summary,
                 "details": [f"unmapped: {', '.join(unmapped[:30])}"] if unmapped else []}
    return smap, check
