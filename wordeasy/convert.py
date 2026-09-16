"""docx -> content.typ (+ media) via pandoc, plus everything the verifier needs
to know about the source document."""
from __future__ import annotations

import json
import re
import tempfile
import tomllib
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import pandoc_ast as past
from .table_fills import write_table_fills
from .smartart import has_smartart, replace_smartart
from .docx_fixups import strip_outline_levels
from .tools import FILTER_DIR, TEMPLATE_DIR, find_tool, require_tools, run

LUA_FILTER = FILTER_DIR / "wordeasy.lua"
DEFAULT_THEME = TEMPLATE_DIR / "theme.toml"
CONVERTIBLE = {".doc", ".odt", ".rtf", ".docm", ".dotx", ".dot"}


def load_theme(path: Path) -> dict[str, Any]:
    with open(path, "rb") as f:
        return tomllib.load(f)


@dataclass
class ConvertResult:
    docx: Path
    content_typ: Path
    converted_from: str | None
    title: str
    author: str
    date: str
    source_ast: dict[str, Any]
    source_text: str
    source_notes: list[str]
    structure: past.Structure
    revisions: past.Revisions
    dropped_headers_footers: list[str]
    dropped_paragraphs: list[str]
    headings: list[dict[str, str]]
    pandoc_stderr: str
    typst_special_chars: dict[str, int] = field(default_factory=dict)
    smartart: list = field(default_factory=list)
    outline_stripped: int = 0


def find_soffice() -> Path | None:
    return find_tool("soffice") or find_tool("libreoffice")


def _soffice_convert(path: Path, work_dir: Path, target: str) -> Path:
    """Run LibreOffice headless: convert `path` to `target` (a filter name such as docx or pdf) into work_dir."""
    soffice = find_soffice()
    if soffice is None:
        raise RuntimeError(f"{path.name}: LibreOffice (soffice) is required to convert {path.suffix} to .{target}")
    work_dir.mkdir(parents=True, exist_ok=True)
    # A private profile: otherwise the request is handed to an already running
    # LibreOffice GUI (e.g. with the same file open) and silently produces nothing.
    profile = work_dir / "lo-profile"
    profile.mkdir(exist_ok=True)
    run([soffice, f"-env:UserInstallation={profile.resolve().as_uri()}", "--headless",
         "--convert-to", target, "--outdir", work_dir, path])
    out = work_dir / (path.stem + "." + target)
    if not out.exists():
        raise RuntimeError(f"{path.name}: LibreOffice conversion produced no file")
    return out


def preflight(path: Path, work_dir: Path) -> tuple[Path, str | None]:
    """Return a .docx path, converting legacy formats with LibreOffice."""
    if path.suffix.lower() == ".docx":
        return path, None
    if path.suffix.lower() not in CONVERTIBLE:
        raise ValueError(f"unsupported input format: {path.suffix} ({path})")
    return _soffice_convert(path, work_dir, "docx"), path.suffix.lower()


def office_pdf(path: Path, pdf: Path, work_dir: Path) -> Path:
    """Render a Word file to PDF with LibreOffice, as it would look in Word: the "original" side
    of the UI's comparison views. Raises RuntimeError when LibreOffice is missing."""
    out = _soffice_convert(path, work_dir, "pdf")
    pdf.parent.mkdir(parents=True, exist_ok=True)
    out.replace(pdf)
    return pdf


def _header_footer_text(docx: Path) -> list[str]:
    texts: list[str] = []
    with zipfile.ZipFile(docx) as z:
        for name in sorted(z.namelist()):
            if re.fullmatch(r"word/(header|footer)\d*\.xml", name):
                xml = z.read(name).decode("utf-8", errors="replace")
                parts = re.findall(r"<w:t(?:\s[^>]*)?>([^<]*)</w:t>", xml)
                t = "".join(parts).strip()
                if t:
                    texts.append(f"{Path(name).stem}: {t}")
    return texts


def _filter_column_mode(theme: dict[str, Any]) -> str:
    """The Lua filter knows "auto" (drop widths) and "word" (keep them); "fill" keeps them
    and converts the percentages to fractions afterwards."""
    mode = theme.get("tables", {}).get("column_widths", "fill")
    return "auto" if mode == "auto" else "word"


_COLUMNS_RE = re.compile(r"(columns: \()([^)]*%[^)]*)(\))")


def _stretch_table_columns(content_typ: Path) -> None:
    """`columns: (25%, 75%)` -> `columns: (25fr, 75fr)`: same proportions, full text width."""
    text = content_typ.read_text(encoding="utf-8")
    text = _COLUMNS_RE.sub(lambda m: m.group(1) + m.group(2).replace("%", "fr") + m.group(3), text)
    content_typ.write_text(text, encoding="utf-8")


_LIST_AFTER_BRACKET_RE = re.compile(r"^(\s*)(.*?)(?<![\w\]])\[([-+]) ", re.M)


def _lists_on_own_line(content_typ: Path) -> None:
    """`[- item` -> `[` newline `- item`.

    pandoc opens a content block (table cell, figure caption) with the first
    list item on the same line as the bracket. Typst then treats the following
    indented items as siblings instead of nested items. Moving the first item to
    its own line, indented like the block, restores the nesting. Layout only.
    """
    text = content_typ.read_text(encoding="utf-8")
    text = _LIST_AFTER_BRACKET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}[\n{m.group(1)}{m.group(3)} ", text)
    content_typ.write_text(text, encoding="utf-8")


def _read_drop_log(path: Path) -> list[str]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        style, _, text = line.partition("\t")
        out.append(f"[{style}] {text.strip()}")
    path.unlink()
    return out


def _read_heading_log(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) == 5:
            kind, typed, level, expected, text = parts
            out.append({"kind": kind, "typed": typed, "level": int(level), "expected": expected, "text": text.strip()})
    path.unlink()
    return out


def _special_char_census(text: str) -> dict[str, int]:
    return {ch: text.count(ch) for ch in "#*_@<>$\\~[]`" if text.count(ch)}


def convert(
    source: Path,
    out_dir: Path,
    theme: dict[str, Any],
    *,
    accept_changes: bool = False,
    title: str | None = None,
    author: str | None = None,
    date: str | None = None,
) -> ConvertResult:
    tools = require_tools("pandoc")
    pandoc = tools["pandoc"]
    out_dir.mkdir(parents=True, exist_ok=True)

    docx, converted_from = preflight(source, out_dir / "_preflight")
    outline_stripped = 0
    if not theme.get("source", {}).get("outline_level_headings", False):
        fixed = out_dir / "_preflight" / (docx.stem + "-outline.docx")
        fixed.parent.mkdir(parents=True, exist_ok=True)
        outline_stripped = strip_outline_levels(docx, fixed)
        if outline_stripped:
            docx = fixed
    smartart = []
    if has_smartart(docx):
        rendered = out_dir / "_preflight" / (docx.stem + "-smartart.docx")
        smartart = replace_smartart(docx, rendered, out_dir / "_preflight" / "smartart")
        docx = rendered

    style_map = {k: int(v) for k, v in theme.get("style_map", {}).items()}
    drop_log = out_dir / "_dropped.tsv"
    heading_log = out_dir / "_headings.tsv"
    for f in (drop_log, heading_log):
        if f.exists():
            f.unlink()
    src = theme.get("source", {})
    env = {
        "WORDEASY_STYLE_MAP": json.dumps(style_map),
        "WORDEASY_TEXT_WIDTH_IN": str(theme.get("source", {}).get("text_width_in", 6.5)),
        "WORDEASY_DROP_STYLES": json.dumps(list(theme.get("source", {}).get("drop_styles", []))),
        "WORDEASY_DROP_HEADINGS": json.dumps(list(theme.get("source", {}).get("drop_headings", []))),
        "WORDEASY_DROP_LOG": str(drop_log),
        "WORDEASY_TABLE_COLUMNS": _filter_column_mode(theme),
        "WORDEASY_TABLE_ALIGN": str(theme.get("tables", {}).get("align", "left")),
        "WORDEASY_BLOCK_QUOTES": str(theme.get("source", {}).get("block_quotes", "unwrap")),
        "WORDEASY_HEADING_NUMBERS": str(src.get("heading_numbers", "parse")),
        "WORDEASY_CHAPTER_WORDS": json.dumps(list(src.get("chapter_words", []))),
        "WORDEASY_HEADING_LOG": str(heading_log),
    }
    common = [
        pandoc, docx, "-f", "docx+styles", "--wrap=none",
        "--lua-filter", LUA_FILTER,
        "--track-changes=accept",
    ]

    # 1. content.typ (media extracted next to it, referenced relatively)
    content_typ = out_dir / "content.typ"
    r_typ = run(common + ["-t", "typst", "--extract-media=.", "-o", content_typ], cwd=out_dir, env=env)
    if theme.get("tables", {}).get("column_widths", "fill") == "fill":
        _stretch_table_columns(content_typ)
    _lists_on_own_line(content_typ)

    dropped_paragraphs = _read_drop_log(drop_log)
    write_table_fills(docx, out_dir / "table-fills.json")
    headings = _read_heading_log(heading_log)

    # 2. JSON AST of the source, after the same filter, for text + structure
    # (logs not passed again: the same lines would be logged twice)
    r_json = run(common + ["-t", "json"], cwd=out_dir, env={**env, "WORDEASY_DROP_LOG": "", "WORDEASY_HEADING_LOG": ""})
    ast = json.loads(r_json.stdout)
    blocks = ast["blocks"]
    meta = ast.get("meta", {})

    notes: list[str] = []
    buf: list[str] = []
    past._walk_blocks(blocks, buf, notes)
    source_text = "".join(buf)

    # 3. revisions / comments (independent of --track-changes=accept)
    r_all = run([pandoc, docx, "-f", "docx", "-t", "json", "--track-changes=all"], cwd=out_dir)
    revisions = past.revisions_of(json.loads(r_all.stdout)["blocks"])
    if revisions.has_tracked_changes and not accept_changes:
        # Leave the decision to the caller; conversion output is still produced
        # (with changes accepted) so the report can show what was accepted.
        pass

    return ConvertResult(
        docx=docx,
        content_typ=content_typ,
        converted_from=converted_from,
        title=title or past.meta_string(meta, "title") or source.stem,
        author=author or past.meta_string(meta, "author"),
        date=date or past.meta_string(meta, "date"),
        source_ast=ast,
        source_text=source_text,
        source_notes=notes,
        structure=past.structure_of(blocks),
        revisions=revisions,
        dropped_headers_footers=_header_footer_text(docx),
        dropped_paragraphs=dropped_paragraphs,
        smartart=smartart,
        outline_stripped=outline_stripped,
        headings=headings,
        pandoc_stderr=(r_typ.stderr + r_json.stderr).strip(),
        typst_special_chars=_special_char_census(source_text),
    )


def read_typst_ast(content_typ: Path, log_path: Path) -> tuple[dict[str, Any], list[str]]:
    """Parse a Typst file back with pandoc's typst reader. Returns (ast, ignored-element messages)."""
    tools = require_tools("pandoc")
    run([tools["pandoc"], content_typ, "-f", "typst", "-t", "json", f"--log={log_path}", "-o", log_path.with_suffix(".json")],
        cwd=content_typ.parent)
    ast = json.loads(log_path.with_suffix(".json").read_text(encoding="utf-8"))
    ignored: list[str] = []
    try:
        for entry in json.loads(log_path.read_text(encoding="utf-8")):
            if entry.get("type") == "IgnoredElement" or "ignor" in entry.get("type", "").lower():
                ignored.append(entry.get("message") or json.dumps(entry))
    except (json.JSONDecodeError, OSError):
        pass
    return ast, ignored
