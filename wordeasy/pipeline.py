"""Per-document build and verify pipeline, shared by the command line and the local UI.

`build_document` and `verify_document` each return the report dict that was written to
`report.json`. They report progress through an optional callback and never print.

One build of `<doc>.docx` produces, under `out/<doc>/`:

    <doc>.pdf        the styled PDF (Typst)
    original.pdf     the Word file as LibreOffice renders it (the "Live" comparison's left side)
    lorem/           the lorem-ipsum twin: an anonymised copy of the Word file (lorem.docx),
                     its own LibreOffice render (lorem/original.pdf) and its own styled build
                     (lorem/lorem.pdf), verified to contain no word of the original
"""
from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from random import Random
from typing import Any, Callable

from . import pandoc_ast as past
from . import verify as V
from .anonymize import AnonymizeError, anonymize_docx, leaked_into_text, lorem_like, original_word_set, verify_anonymized
from .convert import DEFAULT_THEME, convert, find_soffice, load_theme, office_pdf, preflight, read_typst_ast
from .render import compile_pdf, compute_root, relocate_main, root_relative, template_import_path, write_main
from .report import build_report, write_report
from .tools import REPO_ROOT, tool_versions

STATE_FILE = "wordeasy.json"
ORIGINAL_PDF = "original.pdf"
TWIN_DIR = "lorem"           # the twin lives in out/<doc>/lorem/, its Word file is lorem/lorem.docx
REQUIRED_TOOLS = ("pandoc", "typst", "pdftotext", "pdfimages", "pdffonts")
# Cover answers remembered from one build of the same document to the next. The department
# name is one variable: it is the label above the title and the name under the symbol.
COVER_FIELDS = ("title", "number", "version", "cover", "department_name")

Progress = Callable[[str, str], None]
"""progress(stage, message): stage is one of converting, cover, rendering, checking, original, anonymizing, reporting."""

CoverAsker = Callable[[Path, dict[str, str]], dict[str, str]]
"""ask(source, proposed) -> answers; lets the CLI prompt on a terminal."""


@dataclass
class BuildOptions:
    theme: Path = DEFAULT_THEME
    out_root: Path = field(default_factory=lambda: REPO_ROOT / "out")
    accept_changes: bool = False
    clean: bool = False
    title: str | None = None
    department: str | None = None       # short symbol ("IT"): footer prefix and big glyph on the cover
    number: str | None = None
    version: str | None = None
    department_name: str | None = None  # full name: label above the title and under the symbol
    cover: str | None = None            # name of a [cover.variants.*] table in the theme
    original_pdf: bool = True           # render the Word file with LibreOffice next to the styled PDF
    twins: bool = True                  # build the lorem-ipsum twin (original + generated) under lorem/


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


def _write_state(out_dir: Path, state: dict[str, Any]) -> None:
    (out_dir / STATE_FILE).write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def twin_dir_for(out_dir: Path) -> Path:
    return out_dir / TWIN_DIR


def cover_variants(theme: dict[str, Any]) -> list[dict[str, str]]:
    """The cover variants a theme defines ([cover.variants.<name>]), as name + description."""
    variants = theme.get("cover", {}).get("variants", {})
    return [{"name": name, "description": str(v.get("description", ""))} for name, v in variants.items()]


def check_cover_variant(theme: dict[str, Any], name: str) -> str:
    """Return `name` if the theme defines it (or it is empty: theme default); raise ValueError otherwise."""
    names = [v["name"] for v in cover_variants(theme)]
    if name and name not in names:
        known = ", ".join(names) if names else "none"
        raise ValueError(f"unknown cover variant {name!r}; the theme defines: {known}")
    return name


def default_cover(previous: dict[str, str], theme: dict[str, Any], opts: BuildOptions) -> dict[str, str]:
    """Command-line values win, then the previous build's answers, then the theme defaults."""
    cover = theme.get("cover", {})
    doc = theme.get("document", {})
    # builds made before the label and the department name were merged stored the label separately
    prev_name = previous.get("department_name") or previous.get("label", "")
    return {
        "title": opts.title if opts.title is not None else previous.get("title", ""),
        "number": opts.number if opts.number is not None else previous.get("number", cover.get("number", "")),
        "version": opts.version if opts.version is not None else previous.get("version", doc.get("version", "")),
        "cover": opts.cover if opts.cover is not None else previous.get("cover", cover.get("variant", "")),
        "department_name": opts.department_name if opts.department_name is not None
        else prev_name or doc.get("department_name", ""),
    }


def _write_main_from_state(out_dir: Path, state: dict[str, Any]) -> None:
    theme_path = Path(state["theme"])
    write_main(out_dir, title=state["title"], root=compute_root(out_dir, theme_path), department=state.get("department"),
               number=state.get("number") or None, department_name=state.get("department_name") or None,
               version=state.get("version") or None, cover=state.get("cover") or None)


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
                   theme.get("cover", {}).get("number_font", ""),
                   theme.get("cover", {}).get("dept_symbol_font", "")]
    checks.append(V.check_fonts(pdf, styled.warnings, theme_fonts))
    checks.append(V.check_typst_warnings(styled.warnings))
    return checks, pdf, extra


def _render_original(source: Path, out_dir: Path, progress: Progress) -> V.CheckResult:
    """original.pdf: the Word file as LibreOffice shows it. Missing LibreOffice is a WARN, not an error."""
    progress("original", "rendering the Word file with LibreOffice")
    if find_soffice() is None:
        return V.CheckResult("original_pdf", V.WARN,
                             "LibreOffice (soffice) not found: no original.pdf, the Live view has no left side")
    try:
        office_pdf(source, out_dir / ORIGINAL_PDF, out_dir / "_preflight")
    except Exception as e:  # noqa: BLE001
        return V.CheckResult("original_pdf", V.WARN, "LibreOffice could not render the Word file", details=[str(e)])
    return V.CheckResult("original_pdf", V.PASS, f"{ORIGINAL_PDF} rendered with LibreOffice")


def _twin_check(twin_report: dict[str, Any] | None, twin_state: dict[str, Any] | None,
                error: str | None) -> V.CheckResult:
    if error is not None:
        return V.CheckResult("lorem_twin", V.WARN, "lorem ipsum twin refused: " + error.splitlines()[0],
                             details=error.splitlines()[1:6])
    st = (twin_state or {}).get("anonymize_stats", {})
    twin_status = (twin_report or {}).get("status", "?")
    summary = (f"{TWIN_DIR}/: {st.get('original_words', '?')} distinct words replaced, "
               f"{st.get('placeholders', '?')} image(s) turned into placeholders, {st.get('dropped_parts', 0)} data part(s) "
               f"removed; no original word survives; twin build {twin_status}")
    status = V.WARN if twin_status == V.FAIL else V.PASS
    return V.CheckResult("lorem_twin", status, summary)


def build_document(source: Path, opts: BuildOptions, *, ask_cover: CoverAsker | None = None,
                   progress: Progress | None = None) -> dict[str, Any]:
    """Convert one Word file, render it through the theme, run every check and write the report.

    Also renders the Word file itself to original.pdf and builds the lorem-ipsum twin under
    lorem/ (both optional through BuildOptions). Raises FileNotFoundError when the source is
    missing and propagates tool errors.
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
        previous = {k: prev_state[k] for k in COVER_FIELDS + ("label",) if prev_state.get(k)}
    if out_dir.exists() and opts.clean:
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    progress("converting", "converting with pandoc")
    conv = convert(source, out_dir, theme, accept_changes=opts.accept_changes, title=opts.title)
    if not previous.get("title"):
        previous["title"] = conv.title
    progress("cover", "collecting cover information")
    cover_info = default_cover(previous, theme, opts)
    if ask_cover is not None:
        cover_info = ask_cover(source, cover_info)
    check_cover_variant(theme, cover_info["cover"])
    conv.title = cover_info["title"] or conv.title
    (out_dir / "source.txt").write_text(conv.source_text, encoding="utf-8")
    state = {
        "name": source.stem, "source": str(source), "docx": str(conv.docx), "theme": str(theme_path),
        "title": conv.title,
        "number": cover_info["number"], "department": opts.department,
        "version": cover_info["version"], "department_name": cover_info["department_name"],
        "cover": cover_info["cover"],
        "expected_images": conv.structure.images,
        "template_import": template_import_path(compute_root(out_dir, theme_path)),
        "structure": conv.structure.as_dict(), "table_cells": conv.structure.table_cells,
        "links": conv.structure.links,
    }
    if prev_state and prev_state.get("anonymized"):
        state["anonymized"] = True
        state["anonymize_stats"] = prev_state.get("anonymize_stats", {})
    _write_state(out_dir, state)
    _write_main_from_state(out_dir, state)

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

    if opts.original_pdf:
        checks.append(_render_original(source, out_dir, progress))
    if opts.twins and not state.get("anonymized"):
        twin_report, twin_error = None, None
        try:
            twin_report = _build_twin(out_dir, state, progress)
        except (AnonymizeError, RuntimeError, ValueError, OSError) as e:
            twin_error = str(e)
        checks.append(_twin_check(twin_report, read_state(twin_dir_for(out_dir)), twin_error))

    progress("reporting", "writing the report")
    info = {
        "title": conv.title,
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


COVER_EDITABLE = ("title", "number", "version", "cover", "department", "department_name")


def update_cover(out_dir: Path, fields: dict[str, str | None]) -> dict[str, Any]:
    """Change the cover/metadata fields of a built document and regenerate main.typ.

    Only keys in COVER_EDITABLE are applied. An empty value clears the field so the theme
    default applies again (title keeps its previous value when emptied). The lorem twin, if
    present, gets lorem-ised copies of the new values. The PDF is not recompiled here; run
    verify_document afterwards. Raises ValueError for a cover variant the theme does not define.
    """
    out_dir = out_dir.resolve()
    state = read_state(out_dir)
    if state is None:
        raise FileNotFoundError(f"{out_dir}: no {STATE_FILE}; not a wordeasy output folder")
    if "cover" in fields:
        check_cover_variant(load_theme(Path(state["theme"])), (fields["cover"] or "").strip())
    for key in COVER_EDITABLE:
        if key not in fields:
            continue
        value = (fields[key] or "").strip()
        if key == "title" and not value:
            continue
        # cover fields are always strings; the department pair is None when
        # unset so the theme default applies
        state[key] = value or (None if key in ("department", "department_name") else "")
    state.pop("label", None)
    _write_state(out_dir, state)
    _write_main_from_state(out_dir, state)
    twin_dir = twin_dir_for(out_dir)
    twin_state = read_state(twin_dir)
    if twin_state is not None:
        _lorem_cover(twin_state, state, Random())
        _write_state(twin_dir, twin_state)
        _write_main_from_state(twin_dir, twin_state)
    return state


def _restore_structure(state: dict[str, Any]) -> past.Structure:
    struct = past.Structure()
    s = state["structure"]
    struct.headings.update({int(k): v for k, v in s["headings"].items()})
    struct.tables, struct.images, struct.list_items = s["tables"], s["images"], s["list_items"]
    struct.notes, struct.code_blocks, struct.block_quotes = s["footnotes"], s["code_blocks"], s["block_quotes"]
    struct.table_cells, struct.links = state["table_cells"], state["links"]
    return struct


def verify_document(out_dir: Path, theme: Path | None = None, *, progress: Progress | None = None) -> dict[str, Any]:
    """Recompile an output folder from its (possibly hand-edited) .typ files and re-run every check.

    The lorem twin under lorem/, when present, is recompiled and re-checked the same way so
    both comparison views follow a template or cover change.
    """
    progress = progress or _noop
    out_dir = out_dir.resolve()
    state = read_state(out_dir)
    if state is None:
        raise FileNotFoundError(f"{out_dir}: no {STATE_FILE}; not a wordeasy output folder")
    source_text = (out_dir / "source.txt").read_text(encoding="utf-8")
    struct = _restore_structure(state)
    theme_path = Path(theme).resolve() if theme else Path(state["theme"])
    new_import = relocate_main(out_dir / "main.typ", state.get("template_import", ""), compute_root(out_dir, theme_path))
    if new_import != state.get("template_import"):
        state["template_import"] = new_import
        _write_state(out_dir, state)
    checks, pdf, extra = _render_and_verify(out_dir, state, source_text, struct, state["expected_images"],
                                            theme_path, progress)
    if (out_dir / ORIGINAL_PDF).exists():
        checks.append(V.CheckResult("original_pdf", V.PASS, f"{ORIGINAL_PDF} kept from the build"))
    twin_dir = twin_dir_for(out_dir)
    if read_state(twin_dir) is not None:
        progress("anonymizing", "recompiling the lorem ipsum twin")
        twin_report = verify_document(twin_dir, theme_path, progress=progress)
        _scrub_twin(twin_dir)
        checks.append(_twin_check(twin_report, read_state(twin_dir), None))
    progress("reporting", "writing the report")
    info = {"title": state["title"],
            "structure": state["structure"], "typst_warnings": extra.get("typst_warnings", []), "tools": tool_versions(),
            "note": "re-verified from edited Typst sources"}
    report = build_report(source=out_dir / state["source"], out_dir=out_dir, pdf=pdf, checks=checks, info=info)
    write_report(report, out_dir)
    return report


# ------------------------------------------------------------------ lorem ipsum twin
def _lorem_cover(twin_state: dict[str, Any], state: dict[str, Any], rng: Random) -> None:
    """Give the twin lorem-ised copies of the document's cover fields (the variant is design: kept)."""
    for key in ("title", "number", "version", "department", "department_name"):
        value = state.get(key)
        twin_state[key] = lorem_like(value, rng) if value else value
    twin_state["cover"] = state.get("cover") or ""


def _scrub_twin(twin_dir: Path) -> None:
    """Make every path the twin records relative to its own folder: nothing inside lorem/ may
    name the original document, and the parent folder carries its name."""
    twin_dir = twin_dir.resolve()
    root = compute_root(twin_dir, Path(read_state(twin_dir)["theme"]))
    prefixes = [str(twin_dir), root_relative(twin_dir, root)]

    def scrub(obj: Any) -> Any:
        if isinstance(obj, str):
            for p in prefixes:
                obj = obj.replace(p + "/", "").replace(p, ".")
            return obj
        if isinstance(obj, list):
            return [scrub(x) for x in obj]
        if isinstance(obj, dict):
            return {k: scrub(v) for k, v in obj.items()}
        return obj

    state = scrub(read_state(twin_dir))
    _write_state(twin_dir, state)
    report_file = twin_dir / "report.json"
    if report_file.exists():
        write_report(scrub(json.loads(report_file.read_text(encoding="utf-8"))), twin_dir)
    for name in ("_reader.json", "_reader.log"):  # pandoc's read-back dump: absolute media paths, needed by nothing
        (twin_dir / name).unlink(missing_ok=True)


def _build_twin(out_dir: Path, state: dict[str, Any], progress: Progress) -> dict[str, Any]:
    """Build the lorem-ipsum twin of a built document under out_dir/lorem/.

    The anonymised Word file is verified against the original before it is rendered with
    LibreOffice (lorem/original.pdf) and built through the normal pipeline (lorem/lorem.pdf).
    Nothing in lorem/ refers to the original: paths are relative and every word is replaced.
    Any failure removes the half-made folder and raises.
    """
    source = Path(state["source"])
    if not source.exists():
        raise FileNotFoundError(f"original document not found: {source}")
    theme_path = Path(state["theme"])
    theme = load_theme(theme_path)
    twin_dir = twin_dir_for(out_dir)
    if twin_dir.exists():
        shutil.rmtree(twin_dir)
    twin_dir.mkdir(parents=True)
    twin_docx = twin_dir / f"{TWIN_DIR}.docx"
    rng = Random()
    try:
        with tempfile.TemporaryDirectory(prefix="wordeasy-anon-") as tmp:
            progress("anonymizing", "preparing the original")
            docx, _ = preflight(source, Path(tmp))  # legacy formats go through LibreOffice into the temp dir
            progress("anonymizing", "replacing every word, number and image")
            result = anonymize_docx(docx, twin_docx, heading_style_names=tuple(theme.get("style_map", {}).keys()))
            progress("checking", "verifying that no original word survived")
            stats = verify_anonymized(docx, twin_docx, result.placeholders)
            orig_words = original_word_set(docx)
        twin_cover: dict[str, Any] = {}
        _lorem_cover(twin_cover, state, rng)
        opts = BuildOptions(
            theme=theme_path, out_root=out_dir, accept_changes=True, twins=False, original_pdf=True,
            title=twin_cover["title"] or None, number=twin_cover["number"] or None,
            version=twin_cover["version"] or None, department=twin_cover["department"] or None,
            department_name=twin_cover["department_name"] or None,
            cover=twin_cover["cover"] or None,  # a variant name, not content: the twin keeps the same look
        )
        report = build_document(twin_docx, opts, progress=progress)
        progress("checking", "scanning the extracted text for original words")
        leaked = leaked_into_text((twin_dir / "source.txt").read_text(encoding="utf-8"), orig_words)
        if leaked:
            raise AnonymizeError(f"{len(leaked)} original word(s) reached the extracted text: {', '.join(leaked[:15])}")
        twin_state = read_state(twin_dir) or {}
        twin_state["anonymized"] = True
        twin_state["anonymize_stats"] = {**stats, "dropped_parts": len(result.dropped_parts)}
        _write_state(twin_dir, twin_state)
        _scrub_twin(twin_dir)
        return json.loads((twin_dir / "report.json").read_text(encoding="utf-8"))
    except BaseException:
        shutil.rmtree(twin_dir, ignore_errors=True)  # never leave a half-made twin behind
        raise


def anonymize_document(out_dir: Path, *, progress: Progress | None = None) -> dict[str, Any]:
    """(Re)build the lorem-ipsum twin of a built document; returns the twin's report.

    The twin is normally built together with the document; this rebuilds it on demand, e.g.
    after it was refused or when LibreOffice was installed later.
    """
    progress = progress or _noop
    out_dir = out_dir.resolve()
    state = read_state(out_dir)
    if state is None:
        raise FileNotFoundError(f"{out_dir}: no {STATE_FILE}; not a wordeasy output folder")
    if state.get("anonymized"):
        raise AnonymizeError("this document is already a lorem ipsum twin")
    twin_report = _build_twin(out_dir, state, progress)
    # record the outcome in the document's own report so the check list stays truthful
    report_file = out_dir / "report.json"
    if report_file.exists():
        report = json.loads(report_file.read_text(encoding="utf-8"))
        check = _twin_check(twin_report, read_state(twin_dir_for(out_dir)), None).as_dict()
        report["checks"] = [c for c in report["checks"] if c["name"] != "lorem_twin"] + [check]
        report["status"] = V.worst([V.CheckResult(c["name"], c["status"], c["summary"], details=c.get("details", []))
                                    for c in report["checks"]])
        write_report(report, out_dir)
    return twin_report
