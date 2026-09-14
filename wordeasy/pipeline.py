"""Per-document build and verify pipeline, shared by the command line and the local UI.

`build_document` and `verify_document` each return the report dict that was written to
`report.json`. They report progress through an optional callback and never print.
"""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import pandoc_ast as past
from . import verify as V
from .convert import DEFAULT_THEME, convert, load_theme, read_typst_ast
from .render import compile_pdf, compute_root, relocate_main, template_import_path, write_main
from .report import build_report, write_report
from .tools import REPO_ROOT, tool_versions

STATE_FILE = "wordeasy.json"
REQUIRED_TOOLS = ("pandoc", "typst", "pdftotext", "pdfimages", "pdffonts")
COVER_FIELDS = ("label", "title", "number", "version")

Progress = Callable[[str, str], None]
"""progress(stage, message): stage is one of converting, cover, rendering, checking, reporting."""

CoverAsker = Callable[[Path, dict[str, str]], dict[str, str]]
"""ask(source, proposed) -> answers; lets the CLI prompt on a terminal."""


@dataclass
class BuildOptions:
    theme: Path = DEFAULT_THEME
    out_root: Path = field(default_factory=lambda: REPO_ROOT / "out")
    accept_changes: bool = False
    clean: bool = False
    title: str | None = None
    author: str | None = None
    date: str | None = None
    department: str | None = None
    number: str | None = None
    label: str | None = None
    version: str | None = None
    department_name: str | None = None


def out_dir_for(source: Path, out_root: Path) -> Path:
    return out_root / source.stem


def _noop(_stage: str, _message: str) -> None:
    return None


def read_state(out_dir: Path) -> dict[str, Any] | None:
    f = out_dir / STATE_FILE
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def default_cover(previous: dict[str, str], theme: dict[str, Any], opts: BuildOptions) -> dict[str, str]:
    """Command-line values win, then the previous build's answers, then the theme defaults."""
    cover = theme.get("cover", {})
    doc = theme.get("document", {})
    return {
        "label": opts.label if opts.label is not None else previous.get("label", cover.get("label", "")),
        "title": opts.title if opts.title is not None else previous.get("title", ""),
        "number": opts.number if opts.number is not None else previous.get("number", cover.get("number", "")),
        "version": opts.version if opts.version is not None else previous.get("version", doc.get("version", "")),
    }


def _render_and_verify(out_dir: Path, state: dict[str, Any], source_text: str, source_struct: past.Structure,
                       expected_images: int, theme_path: Path,
                       progress: Progress) -> tuple[list[V.CheckResult], Path | None, dict[str, Any]]:
    root = compute_root(out_dir, theme_path)
    main = out_dir / "main.typ"
    pdf = out_dir / f"{state['name']}.pdf"
    bare = out_dir / f"{state['name']}.verify.pdf"
    checks: list[V.CheckResult] = []
    extra: dict[str, Any] = {}

    # Check 1 + 2: read content.typ back with pandoc
    progress("checking", "reading content.typ back with pandoc")
    typ_ast, ignored = read_typst_ast(out_dir / "content.typ", out_dir / "_reader.log")
    typ_text = past.stringify_blocks(typ_ast["blocks"])
    checks.append(V.check_text_after_conversion(source_text, typ_text, ignored))
    checks.append(V.check_structure(source_struct, past.structure_of(typ_ast["blocks"])))

    # Styled render
    progress("rendering", "compiling the styled PDF")
    styled = compile_pdf(main, pdf, root=root, theme_path=theme_path)
    extra["typst_warnings"] = styled.warnings
    if not styled.ok:
        checks.append(V.CheckResult("render", V.FAIL, "typst compile failed", details=styled.errors))
        return checks, None, extra
    checks.append(V.CheckResult("render", V.PASS, "typst compile succeeded"))

    # Bare render for the PDF text check
    progress("checking", "compiling the bare verification PDF and comparing text")
    bare_res = compile_pdf(main, bare, root=root, theme_path=theme_path, verify=True)
    if not bare_res.ok:
        checks.append(V.CheckResult("text_in_pdf", V.FAIL, "verification render failed", details=bare_res.errors))
    else:
        checks.append(V.check_pdf_text(source_text, bare))
    checks.append(V.check_pdf_superset(source_text, pdf))
    checks.append(V.check_images(expected_images, pdf))
    theme = load_theme(theme_path)
    theme_fonts = [theme["fonts"]["body"], theme["fonts"]["heading"], theme["fonts"]["mono"],
                   theme.get("cover", {}).get("number_font", "")]
    checks.append(V.check_fonts(pdf, styled.warnings, theme_fonts))
    checks.append(V.check_typst_warnings(styled.warnings))
    return checks, pdf, extra


def build_document(source: Path, opts: BuildOptions, *, ask_cover: CoverAsker | None = None,
                   progress: Progress | None = None) -> dict[str, Any]:
    """Convert one Word file, render it through the theme, run every check and write the report.

    Raises FileNotFoundError when the source is missing and propagates tool errors.
    """
    progress = progress or _noop
    source = source.resolve()
    if not source.exists():
        raise FileNotFoundError(f"not found: {source}")
    theme_path = Path(opts.theme).resolve()
    theme = load_theme(theme_path)
    out_root = Path(opts.out_root).resolve()
    out_dir = out_dir_for(source, out_root)

    previous: dict[str, str] = {}
    prev_state = read_state(out_dir)
    if prev_state:
        previous = {k: prev_state[k] for k in COVER_FIELDS if prev_state.get(k)}
    if out_dir.exists() and opts.clean:
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    progress("converting", "converting with pandoc")
    conv = convert(source, out_dir, theme, accept_changes=opts.accept_changes,
                   title=opts.title, author=opts.author, date=opts.date)
    if not previous.get("title"):
        previous["title"] = conv.title
    progress("cover", "collecting cover information")
    cover_info = default_cover(previous, theme, opts)
    if ask_cover is not None:
        cover_info = ask_cover(source, cover_info)
    conv.title = cover_info["title"] or conv.title
    (out_dir / "source.txt").write_text(conv.source_text, encoding="utf-8")
    state = {
        "name": source.stem, "source": str(source), "docx": str(conv.docx), "theme": str(theme_path),
        "title": conv.title, "author": conv.author, "date": conv.date,
        "label": cover_info["label"], "number": cover_info["number"], "department": opts.department,
        "version": cover_info["version"], "department_name": opts.department_name,
        "expected_images": conv.structure.images,
        "template_import": template_import_path(compute_root(out_dir, theme_path)),
        "structure": conv.structure.as_dict(), "table_cells": conv.structure.table_cells,
        "links": conv.structure.links,
    }
    (out_dir / STATE_FILE).write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    root = compute_root(out_dir, theme_path)
    write_main(out_dir, title=conv.title, author=conv.author, date=conv.date, root=root, department=opts.department,
               number=cover_info["number"] or None, label=cover_info["label"] or None,
               department_name=opts.department_name, version=cover_info["version"] or None)

    checks, pdf, extra = _render_and_verify(out_dir, state, conv.source_text, conv.structure,
                                            conv.structure.images, theme_path, progress)
    if conv.revisions.has_tracked_changes:
        n = len(conv.revisions.insertions) + len(conv.revisions.deletions)
        if opts.accept_changes:
            checks.append(V.CheckResult("tracked_changes", V.WARN, f"{n} tracked change(s) accepted (--accept-changes)"))
        else:
            checks.append(V.CheckResult(
                "tracked_changes", V.FAIL,
                f"source contains {n} tracked change(s); accepting them is a content decision. "
                "Resolve them in Word or re-run with --accept-changes",
                details=[f"inserted: «{t}»" for t in conv.revisions.insertions]
                + [f"deleted: «{t}»" for t in conv.revisions.deletions]))
    if conv.headings:
        parsed = [h for h in conv.headings if h["kind"] in ("chapter", "number")]
        mismatched = [h for h in parsed if h["typed"].lstrip("0") != h["expected"] and h["typed"] != h["expected"]]
        front = [h for h in conv.headings if h["kind"] == "front-matter"]
        summary = (f"{len(parsed)} heading number(s) parsed from the text and regenerated"
                   + (f", {len(front)} front-matter heading(s) left unnumbered" if front else ""))
        if mismatched:
            checks.append(V.CheckResult(
                "heading_numbering", V.WARN,
                summary + f"; {len(mismatched)} typed number(s) differ from the regenerated ones",
                details=[f"typed «{h['typed']}» → regenerated «{h['expected']}» (level {h['level']}): {h['text']}"
                         for h in mismatched[:20]]))
        else:
            checks.append(V.CheckResult("heading_numbering", V.PASS, summary + "; all match"))
    if conv.dropped_paragraphs:
        checks.append(V.CheckResult("dropped_paragraphs", V.WARN,
                                    f"{len(conv.dropped_paragraphs)} paragraph(s) dropped by design "
                                    "(Word table of contents, empty headings, hand-typed heading numbers)",
                                    details=conv.dropped_paragraphs[:12]))
    if conv.outline_stripped:
        checks.append(V.CheckResult("outline_levels", V.WARN,
                                    f"{conv.outline_stripped} non-heading paragraph(s) carried a Word outline level; "
                                    "kept as ordinary paragraphs (theme [source] outline_level_headings)"))
    if conv.smartart:
        ok = [d for d in conv.smartart if str(d.png)]
        failed = [d for d in conv.smartart if not str(d.png)]
        details = [f"diagram {d.index}: " + " | ".join(d.text) for d in conv.smartart]
        checks.append(V.CheckResult(
            "smartart", V.FAIL if failed else V.WARN,
            f"{len(ok)} SmartArt diagram(s) rendered as pictures via LibreOffice; their text cannot be verified"
            + (f"; {len(failed)} could not be rendered" if failed else ""),
            details=details))
    if conv.pandoc_stderr:
        checks.append(V.CheckResult("pandoc_warnings", V.WARN, "pandoc reported warnings",
                                    details=conv.pandoc_stderr.splitlines()[:20]))
    progress("reporting", "writing the report")
    info = {
        "title": conv.title, "author": conv.author, "date": conv.date,
        "converted_from": conv.converted_from,
        "tracked_changes": conv.revisions.as_dict() if conv.revisions.has_tracked_changes else None,
        "comments": conv.revisions.comments,
        "dropped_headers_footers": conv.dropped_headers_footers,
        "dropped_paragraphs": conv.dropped_paragraphs,
        "smartart": [{"index": d.index, "text": d.text} for d in conv.smartart],
        "headings": conv.headings,
        "structure": conv.structure.as_dict(),
        "typst_special_chars": conv.typst_special_chars,
        "typst_warnings": extra.get("typst_warnings", []),
        "tools": tool_versions(),
    }
    report = build_report(source=source, out_dir=out_dir, pdf=pdf, checks=checks, info=info)
    write_report(report, out_dir)
    return report


def verify_document(out_dir: Path, theme: Path | None = None, *, progress: Progress | None = None) -> dict[str, Any]:
    """Recompile an output folder from its (possibly hand-edited) .typ files and re-run every check."""
    progress = progress or _noop
    out_dir = out_dir.resolve()
    state = read_state(out_dir)
    if state is None:
        raise FileNotFoundError(f"{out_dir}: no {STATE_FILE}; not a wordeasy output folder")
    source_text = (out_dir / "source.txt").read_text(encoding="utf-8")
    struct = past.Structure()
    s = state["structure"]
    struct.headings.update({int(k): v for k, v in s["headings"].items()})
    struct.tables, struct.images, struct.list_items = s["tables"], s["images"], s["list_items"]
    struct.notes, struct.code_blocks, struct.block_quotes = s["footnotes"], s["code_blocks"], s["block_quotes"]
    struct.table_cells, struct.links = state["table_cells"], state["links"]
    theme_path = Path(theme).resolve() if theme else Path(state["theme"])
    new_import = relocate_main(out_dir / "main.typ", state.get("template_import", ""), compute_root(out_dir, theme_path))
    if new_import != state.get("template_import"):
        state["template_import"] = new_import
        (out_dir / STATE_FILE).write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    checks, pdf, extra = _render_and_verify(out_dir, state, source_text, struct, state["expected_images"],
                                            theme_path, progress)
    progress("reporting", "writing the report")
    info = {"title": state["title"], "author": state["author"], "date": state["date"],
            "structure": s, "typst_warnings": extra.get("typst_warnings", []), "tools": tool_versions(),
            "note": "re-verified from edited Typst sources"}
    report = build_report(source=Path(state["source"]), out_dir=out_dir, pdf=pdf, checks=checks, info=info)
    write_report(report, out_dir)
    return report
