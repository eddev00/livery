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
from .docx_fixups import drop_pages, strip_outline_levels
from .mapping import MappingError, pages_of, split_rules, validate_rules
from .tools import FILTER_DIR, TEMPLATE_DIR, find_tool, require_tools, run

LUA_FILTER = FILTER_DIR / "livery.lua"
DEFAULT_THEME = TEMPLATE_DIR / "theme.toml"
CONVERTIBLE = {".doc", ".odt", ".rtf", ".docm", ".dotx", ".dot"}
ELEMENTS_FILE = "elements.json"
# The element markers the filter's instrumentation pass writes, one per line, exactly this shape.
MARKER_RE = re.compile(r'^#metadata\("[hptlfx]\d{4,}"\) <we-[se]>\n', re.M)


def load_theme(path: Path) -> dict[str, Any]:
    with open(path, "rb") as f:
        return tomllib.load(f)


@dataclass
class ConvertResult:
    docx: Path
    content_typ: Path
    converted_from: str | None
    title: str
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
    raw_text: str = ""                                       # the Word file's text, read without any mapping
    rule_counts: dict[str, int] = field(default_factory=dict)  # mapping rule name -> blocks matched


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
    """`columns: (25%, 75%)` -> `columns: (25fr, 75fr)`: same proportions, full text width.
    A single column keeps a trailing comma, `(50fr,)`: without it the parentheses are only
    grouping, and pandoc's Typst reader cannot count the columns when reading it back."""
    text = content_typ.read_text(encoding="utf-8")

    def stretch(m: re.Match[str]) -> str:
        inner = m.group(2).replace("%", "fr")
        if "," not in inner:
            inner += ","
        return m.group(1) + inner + m.group(3)
    text = _COLUMNS_RE.sub(stretch, text)
    content_typ.write_text(text, encoding="utf-8")


_TYPED_NUMBER_RE = re.compile(r"(?<=\[)(\d+)\.(?=[ \t])")


def _escape_typed_numbers(content_typ: Path) -> None:
    """`[1. Business Overview]` -> `[1\\. Business Overview]`.

    pandoc writes real numbered lists as `+ item`, but leaves a number typed at the start of a
    table cell or other content block unescaped, and Typst then reads `1. ` as a list marker:
    the typed number would become list numbering and drop out of the text. Escaping the dot
    keeps it as text. Layout only; the read-back sees exactly the source text again."""
    text = content_typ.read_text(encoding="utf-8")
    content_typ.write_text(_TYPED_NUMBER_RE.sub(lambda m: m.group(1) + "\\.", text), encoding="utf-8")


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
        if len(parts) == 6:
            kind, typed, level, expected, prefix, text = parts
            out.append({"kind": kind, "typed": typed, "level": int(level), "expected": expected, "prefix": prefix,
                        "text": text.strip()})
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

    rules = theme.get("rule", [])
    problems = validate_rules(rules)
    if problems:
        raise MappingError(problems)
    page_rules, block_rules = split_rules(rules)
    # The words of the Word file as pandoc reads it without the filter: what the mapping
    # may restructure but never lose (pipeline's mapping_text check).
    r_raw = run([pandoc, docx, "-f", "docx", "-t", "json", "--track-changes=accept"], cwd=out_dir)
    raw_buf: list[str] = []
    past._walk_blocks(json.loads(r_raw.stdout)["blocks"], raw_buf, [])
    raw_text = "".join(raw_buf)
    rule_counts = {r["name"]: 0 for r in rules}
    page_dropped: list[str] = []
    if page_rules:
        paged = out_dir / "_preflight" / (docx.stem + "-pages.docx")
        paged.parent.mkdir(parents=True, exist_ok=True)
        removed = drop_pages(docx, paged, [(r["name"], pages_of(r["match"]["page"])) for r in page_rules])
        for name, text in removed:
            rule_counts[name] += 1
            page_dropped.append(f"[rule:{name}] {text}")
        if removed:
            docx = paged

    style_map = {k: int(v) for k, v in theme.get("style_map", {}).items()}
    drop_log = out_dir / "_dropped.tsv"
    heading_log = out_dir / "_headings.tsv"
    rule_log = out_dir / "_rules.json"
    for f in (drop_log, heading_log, rule_log):
        if f.exists():
            f.unlink()
    src = theme.get("source", {})
    env = {
        "LIVERY_STYLE_MAP": json.dumps(style_map),
        "LIVERY_TEXT_WIDTH_IN": str(theme.get("source", {}).get("text_width_in", 6.5)),
        "LIVERY_DROP_STYLES": json.dumps(list(theme.get("source", {}).get("drop_styles", []))),
        "LIVERY_DROP_HEADINGS": json.dumps(list(theme.get("source", {}).get("drop_headings", []))),
        "LIVERY_DROP_LOG": str(drop_log),
        "LIVERY_TABLE_COLUMNS": _filter_column_mode(theme),
        "LIVERY_TABLE_ALIGN": str(theme.get("tables", {}).get("align", "left")),
        "LIVERY_BLOCK_QUOTES": str(theme.get("source", {}).get("block_quotes", "unwrap")),
        "LIVERY_HEADING_NUMBERS": str(src.get("heading_numbers", "parse")),
        "LIVERY_CHAPTER_WORDS": json.dumps(list(src.get("chapter_words", []))),
        "LIVERY_HEADING_LOG": str(heading_log),
        "LIVERY_RULES": json.dumps(block_rules),
        "LIVERY_RULE_LOG": str(rule_log),
    }
    common = [
        pandoc, docx, "-f", "docx+styles", "--wrap=none",
        "--lua-filter", LUA_FILTER,
        "--track-changes=accept",
    ]

    # 1. content.typ (media extracted next to it, referenced relatively). With [desktop]
    # instrument = true every block is bracketed by invisible element markers and the
    # element inventory is written next to it (see the filter's third pass).
    content_typ = out_dir / "content.typ"
    typ_env = dict(env)
    if theme.get("desktop", {}).get("instrument", False):
        typ_env["LIVERY_INSTRUMENT"] = str(out_dir / ELEMENTS_FILE)
    r_typ = run(common + ["-t", "typst", "--extract-media=.", "-o", content_typ], cwd=out_dir, env=typ_env)
    if theme.get("tables", {}).get("column_widths", "fill") == "fill":
        _stretch_table_columns(content_typ)
    _lists_on_own_line(content_typ)
    _escape_typed_numbers(content_typ)

    dropped_paragraphs = page_dropped + _read_drop_log(drop_log)
    if rule_log.exists():
        for name, n in json.loads(rule_log.read_text(encoding="utf-8") or "{}").items():
            rule_counts[name] = rule_counts.get(name, 0) + int(n)
        rule_log.unlink()
    write_table_fills(docx, out_dir / "table-fills.json")
    headings = _read_heading_log(heading_log)

    # 2. JSON AST of the source, after the same filter, for text + structure
    # (logs not passed again: the same lines would be logged twice)
    r_json = run(common + ["-t", "json"], cwd=out_dir, env={**env, "LIVERY_DROP_LOG": "", "LIVERY_HEADING_LOG": "", "LIVERY_RULE_LOG": ""})
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
        raw_text=raw_text,
        rule_counts=rule_counts,
        pandoc_stderr=(r_typ.stderr + r_json.stderr).strip(),
        typst_special_chars=_special_char_census(source_text),
    )


def strip_markers(text: str) -> str:
    """content.typ without the element markers of the instrumentation pass."""
    return MARKER_RE.sub("", text)


def read_typst_ast(content_typ: Path, log_path: Path) -> tuple[dict[str, Any], list[str]]:
    """Parse a Typst file back with pandoc's typst reader. Returns (ast, ignored-element messages)."""
    tools = require_tools("pandoc")
    text = content_typ.read_text(encoding="utf-8")
    stripped = strip_markers(text)
    if stripped != text:
        # Element markers are layout-neutral metadata; pandoc's reader would report them as
        # ignored elements. Read back exactly the content without them.
        content_typ = content_typ.with_name("_readback.typ")
        content_typ.write_text(stripped, encoding="utf-8")
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
