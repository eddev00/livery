"""The sanitized workspace an agent edits, and the synthetic renders it can ask for.

    <workspaces>/<project>/<session>/          mounted at /work in the agent sandbox
        AGENTS.md, CLAUDE.md                   instructions
        design/theme.toml, design.json, custom.typ, mapping.toml   the only files whose changes count
        template/template.typ                  the app's core template, for reference
        samples/doc-NN/                        lorem-ipsum twins: content.typ, media/, elements.json,
                                               styles.json (Word styles used), word/ (the twin as Word
                                               lays it out, PNG pages)
        selection.json                         the elements attached to the conversation
        references/ref-NN.png                  images the user attached as a look to match
        previews/                              synthetic renders (PNG pages, diagnostics)
        bin/render-preview                     asks the app for a synthetic render
        .requests/ .responses/                 that request protocol

Nothing in a workspace comes from a private document except through its lorem twin, whose
anonymization the engine verified when it was built; workspace creation scans the twin
text again against the original's words and refuses on any hit. Reference images are the
exception the user makes knowingly: they are attached in the chat, re-encoded without
metadata and passed on as they look. Documents are named
doc-01, doc-02…; the mapping to real document IDs stays in the app.

The samples are converted with the workspace's mapping.toml. When the agent changes it, the
next render-preview converts the twins (and, locally, the real documents, to align element
IDs) again and rewrites the samples; design.json references move with their elements.
"""
from __future__ import annotations

import difflib
import json
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable

import dataclasses
import io
import tempfile
import zipfile
from html import unescape

from PIL import Image

from ..anonymize import leaked_into_text, original_word_set
from ..convert import ELEMENTS_FILE
from ..docx_fixups import block_pages, body_blocks
from ..tools import find_tool
from . import builder, sandbox
from .builder import DOC_DIR
from .design import Design, DesignError, rekey, resolve_for_document, validate
from .paths import CORE_FILES, NEUTRAL_DIR, NEUTRAL_FONTS, workspaces_dir
from .store import Project, read_json, write_json

RENDER_TIMEOUT = 90
MAX_PREVIEW_PAGES = 4
MAX_WORD_PAGES = 6
REFERENCES_DIR = "references"
MAX_REFERENCE_BYTES = 12 * 1024 * 1024  # an attached image as sent, before re-encoding
MAX_REFERENCE_PIXELS = 40_000_000
REFERENCE_EDGE = 2000  # longest side after re-encoding; enough to read a table's look
_REQ_ID = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


class LeakRefused(RuntimeError):
    """A workspace would have carried words of a private document."""


INSTRUCTIONS = """\
# Livery design workspace

You are restyling documents for Livery. Content is fixed; only the design changes.

## What you may edit (and nothing else counts)

- `design/theme.toml` — the project theme: fonts, colours, page, header/footer, headings,
  body text and default table look. Keep every key; change values.
- `design/design.json` — reusable named **treatments** and per-element **overrides**:
  ```json
  {"version": 1,
   "treatments": {"Data table": {"kind": "table", "style": {"header_fill": "#1F3A5F", "header_color": "#FFFFFF"}}},
   "assignments": {"doc-01/t0008": "Data table"},
   "overrides": {"doc-01/h0004": {"color": "#8A1C1C"}}}
  ```
  Order of precedence: theme → treatment → override.
- `design/custom.typ` — optional Typst show/set rules inside `custom-rules(body)`. It may not
  read files, import or include anything, use `hide`, or touch `sys.inputs`.
- `design/mapping.toml` — how the Word documents are read into headings, paragraphs and
  tables (see "Import mapping" below). Change it when the structure is wrong, not the look.

## Style properties

- heading: font, size, weight, color, italic, align, space_before, space_after, upper
- paragraph: font, size, weight, color, italic, align, space_before, space_after
- table: font_size, inset, header_fill, header_color, header_weight, body_fill, stripe_fill,
  stroke_color, stroke_width, rules (grid | horizontal | header | none),
  columns (proportions, one per column, e.g. [3, 1, 1]), align, header_align

Lengths are strings like "10pt", "4mm", "1.2em". Colours are "#RRGGBB" or a `[colors]` name.
Fonts: Source Sans 3, Libertinus Serif, New Computer Modern, DejaVu Sans Mono.
weight: light, regular, medium, semibold, bold. align: left, center, right, justify.

## Elements

`samples/doc-NN/elements.json` lists every element of a sample document: its ID
(h = heading, p = paragraph, t = table, l = list, f = figure), kind and, for tables, the
shape (columns, header rows, merged header cells). Element references are
`doc-NN/<id>`. `selection.json` lists the elements the user attached to the conversation.
Unless the user asks for a broader change, change only the attached elements; to reuse a
look, define a treatment and assign it.

The samples are lorem-ipsum twins: same structure as the real documents, no real text.

## Import mapping

`design/mapping.toml` holds optional `[settings]` and an ordered list of `[[rule]]`s. The
first rule that matches a block decides; blocks no rule matches convert as before.

```toml
version = 1

[settings]
headings = { "Body Title" = 2 }     # Word style name -> heading level (0: not a heading)
drop_styles = ["Distribution List"] # also: drop_headings, heading_numbers ("parse"|"keep"),
                                    # chapter_words, block_quotes ("unwrap"|"keep"), outline_level_headings

[[rule]]
name = "Leftover page after the cover"
match = { page = 2 }                # Word page the block starts on: 2, [2, 3] or "2-4"; drop only
action = "drop"
expect = "1+"                       # optional: blocks per document ("1", "0-2", "1+")

[[rule]]
name = "Bold numbered titles"
match = { kind = "paragraph", style = "Normal", bold = true, numbered = true }
action = { heading = "from_number" } # or { heading = 2 }

[[rule]]
name = "Container tables"
match = { kind = "table", contains_table = true }
action = { nested_table = "unwrap_outer" }   # or "unwrap_inner"
```

- match keys (all must hold): `page`, `kind` (paragraph, heading, table, list, figure),
  `style` (Word style name or list; plain paragraphs are "Normal", built-in headings
  "Heading 1"…), `level`, `bold`, `numbered` (starts with a typed number), `upper`,
  `in_table`, `contains_table`, `before_first_heading`, `text`, `text_starts`.
- actions: `"drop"`, `"keep"` (skip the built-in rules, e.g. keep a "toc 1" paragraph),
  `{ heading = N | "from_number" }`, `{ paragraph = true }` (headings only),
  `{ nested_table = "unwrap_outer" | "unwrap_inner" }` (tables only).
- The samples are lorem ipsum: `text` and `text_starts` never match them. Match on style,
  shape and position. `samples/doc-NN/styles.json` lists the Word styles each sample uses
  and on which pages; `samples/doc-NN/word/page-N.png` shows the sample as Word lays it out.
- Every removal is recorded and shown to the user; a document where rules remove more than
  a third of the words fails. Rules never change text.
- After changing mapping.toml, run `render-preview`: the samples are converted again,
  `samples/doc-NN/` is rewritten (element IDs can change) and `design/design.json` is
  re-keyed to the new IDs. `previews/result.json` reports each rule's matches per sample and
  any reference that could not be carried (its element was dropped or changed kind).

## Reference images

When the user attaches an image ("make all tables look like this"), it is saved as
`references/ref-NN.png` and named in the message. Open it (your Read tool shows images) and
match its look with the properties above: fills, text colours, rules and strokes, weights,
alignment, spacing. Its text and layout are not content to copy. Fonts outside the list
above are not available: pick the closest one and say so. Compare your `render-preview`
pages with the reference before finishing, and tell the user what could not be matched.

## Previewing

Run `render-preview` (or `render-preview doc-01`) after editing. It validates the design,
renders the samples offline and writes PNG pages to `previews/doc-NN/` plus
`previews/result.json` with any problems. Look at the PNGs before finishing.
The user then renders the change with the real content locally and accepts or discards it.
"""

RENDER_SCRIPT = """\
#!/bin/sh
# Ask Livery for a synthetic preview of the samples with the current design.
id="r$(date +%s%N)"
mkdir -p /work/.requests /work/.responses
printf '{"docs": "%s"}' "${1:-all}" > "/work/.requests/$id.tmp" && mv "/work/.requests/$id.tmp" "/work/.requests/$id.json"
i=0
while [ ! -e "/work/.responses/$id.json" ]; do
  i=$((i+1)); [ $i -gt 1200 ] && { echo "render-preview: no answer from Livery (timed out)"; exit 1; }
  sleep 0.25
done
cat "/work/.responses/$id.json"
echo
"""


def workspace_dir(project: Project, session: str) -> Path:
    return workspaces_dir() / project.id / session


def _main_typ(title: str) -> str:
    return ('#import "/template/template.typ": project\n'
            f'#show: project.with(title: "{title}")\n'
            '#include "content.typ"\n')


def _copy_regular(src: Path, dest: Path) -> None:
    """Copy a tree, regular files only: symlinks and special files are dropped."""
    dest.mkdir(parents=True, exist_ok=True)
    for entry in src.iterdir():
        if entry.is_symlink():
            continue
        if entry.is_dir():
            _copy_regular(entry, dest / entry.name)
        elif entry.is_file():
            shutil.copyfile(entry, dest / entry.name)


def create(project: Project, session: str, docs: list[str], selections: list[dict[str, Any]] | None = None,
           base_rev: str | None = None) -> dict[str, Any]:
    """Build a fresh workspace for `docs` (real document IDs). Returns the private session
    mapping: {"docs": {real id: neutral id}, "dir": ..., "base": revision}."""
    base_rev = base_rev or project.accepted()
    ws = workspace_dir(project, session)
    if ws.exists():
        shutil.rmtree(ws)
    ws.mkdir(parents=True)
    mapping: dict[str, str] = {}
    try:
        skipped = add_samples(project, ws, docs, mapping, project.design(base_rev))
    except LeakRefused:
        shutil.rmtree(ws)
        raise
    base = project.design(base_rev)
    wdesign = base.with_design(rekey(base.design, mapping, keep_unmapped=False))
    wdesign.write(ws / "design")
    (ws / "template").mkdir()
    for name in CORE_FILES:
        shutil.copyfile(NEUTRAL_DIR / name, ws / "template" / name)
    (ws / "AGENTS.md").write_text(INSTRUCTIONS, encoding="utf-8")
    (ws / "CLAUDE.md").write_text(INSTRUCTIONS, encoding="utf-8")
    (ws / "bin").mkdir()
    (ws / "bin" / "render-preview").write_text(RENDER_SCRIPT, encoding="utf-8")
    os.chmod(ws / "bin" / "render-preview", 0o755)
    for d in ("previews", ".requests", ".responses"):
        (ws / d).mkdir()
    write_selection(ws, selections or [], mapping)
    return {"docs": mapping, "dir": str(ws), "base": base_rev, "skipped": skipped}


def add_samples(project: Project, ws: Path, docs: list[str], mapping: dict[str, str],
                design: Design | None = None) -> list[str]:
    """Add the lorem twins of `docs`, converted with `design`'s mapping (by default the
    accepted design's), to the workspace as doc-NN samples, extending `mapping` (real ->
    neutral) in place. Returns the documents that have no usable twin."""
    design = design or project.design(project.accepted())
    skipped: list[str] = []
    for meta in project.documents():
        if meta["id"] not in docs or meta["id"] in mapping:
            continue
        neutral = f"doc-{len(mapping) + 1:02d}"
        if not write_sample(project, ws, meta["id"], meta["current"], neutral, design):
            skipped.append(meta["id"])
            continue
        mapping[meta["id"]] = neutral
    return skipped


def write_sample(project: Project, ws: Path, doc: str, version: int, neutral: str, design: Design) -> bool:
    """(Re)write samples/<neutral>/ from the lorem twin of (doc, version) converted with
    `design`'s mapping. False when the document has no usable twin."""
    conv = builder.ensure_conversion(project, doc, version, design) / DOC_DIR
    twin = conv / "lorem"
    if not (twin / "content.typ").exists():
        return False
    imported_twin = project.import_dir(doc, version) / DOC_DIR / "lorem"
    # Leak gate: the twin's text, element texts and style names against the original's words.
    orig = original_word_set(project.source_dir(doc, version) / "source.docx")
    twin_inv = read_json(twin / ELEMENTS_FILE, [])
    texts = [(twin / "source.txt").read_text(encoding="utf-8") if (twin / "source.txt").exists() else ""]
    texts += [e.get("text", "") for e in twin_inv]
    leaked = leaked_into_text("\n".join(texts), orig)
    if leaked:
        raise LeakRefused(f"{len(leaked)} word(s) of a private document were found in its synthetic twin; "
                          "it was not added to the agent workspace")
    sample = ws / "samples" / neutral
    if sample.exists():
        shutil.rmtree(sample)
    sample.mkdir(parents=True)
    real_inv = read_json(conv / ELEMENTS_FILE, [])
    ids = align_ids(real_inv, twin_inv)
    content = (twin / "content.typ").read_text(encoding="utf-8")
    (sample / "content.typ").write_text(
        _MARKER_ID.sub(lambda m: f'#metadata("{ids.get(m.group(1), m.group(1))}") <we-{m.group(2)}>', content),
        encoding="utf-8")
    if (twin / "media").is_dir():
        _copy_regular(twin / "media", sample / "media")
    write_json(sample / ELEMENTS_FILE, [
        {**{k: v for k, v in e.items() if k in ("kind", "level", "table", "text")}, "id": ids.get(e["id"], e["id"])}
        for e in twin_inv])
    (sample / "main.typ").write_text(_main_typ(f"Sample document {neutral[4:]}"), encoding="utf-8")
    report = read_json(twin / "report.json") or {}
    write_json(sample / "styles.json", style_usage(imported_twin / "lorem.docx", orig)
               | {"mapping_rules": report.get("info", {}).get("mapping_rules", {})})
    word_pages(imported_twin / "original.pdf", sample / "word", ws.parent)
    return True


_STYLE_NAME_RE = re.compile(r'<w:style\b([^>]*)w:styleId="([^"]+)"([^>]*)>.*?<w:name w:val="([^"]*)"', re.S)
_PSTYLE_RE = re.compile(r'<w:pStyle w:val="([^"]+)"')


def style_usage(docx: Path, orig_words: set[str]) -> dict[str, Any]:
    """The Word paragraph styles a (lorem twin) Word file uses: paragraphs, how many of them
    are in tables, and the pages they appear on. The name of a custom style (Word's own
    style names are never private) that holds a word of the original document is replaced
    by "(hidden style N)"."""
    try:
        with zipfile.ZipFile(docx) as z:
            doc = z.read("word/document.xml").decode("utf-8", "replace")
            try:
                styles = z.read("word/styles.xml").decode("utf-8", "replace")
            except KeyError:
                styles = ""
    except (OSError, KeyError, zipfile.BadZipFile):
        return {"pages": 0, "styles": []}
    names, custom = {}, set()
    for before, sid, after, name in _STYLE_NAME_RE.findall(styles):
        names[sid] = unescape(name)
        if re.search(r'w:customStyle="(1|true)"', before + after):
            custom.add(unescape(name))
    blocks = body_blocks(doc)
    pages = block_pages(doc, blocks)
    usage: dict[str, dict[str, Any]] = {}
    for (start, end, tag), page in zip(blocks, pages):
        xml = doc[start:end]
        for p in re.findall(r"<w:p[ >].*?</w:p>", xml, re.S):
            m = _PSTYLE_RE.search(p)
            name = names.get(m.group(1), m.group(1)) if m else "Normal"
            u = usage.setdefault(name, {"style": name, "paragraphs": 0, "in_tables": 0, "pages": []})
            u["paragraphs"] += 1
            u["in_tables"] += tag == "w:tbl"
            if page not in u["pages"] and len(u["pages"]) < 30:
                u["pages"].append(page)
    hidden = 0
    for u in usage.values():
        if u["style"] in custom and leaked_into_text(u["style"], orig_words):
            hidden += 1
            u["style"] = f"(hidden style {hidden})"
    return {"pages": max(pages, default=0), "styles": list(usage.values())}


def word_pages(pdf: Path, dest: Path, scratch_parent: Path) -> None:
    """PNG pages of a (lorem twin) original.pdf, rendered in the offline sandbox."""
    if not pdf.exists():
        return
    dest.mkdir(parents=True, exist_ok=True)
    pdftoppm = str(find_tool("pdftoppm") or "pdftoppm")
    scratch_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=scratch_parent, prefix=".word-") as tmp:
        root = Path(tmp)
        shutil.copyfile(pdf, root / "word.pdf")
        r = subprocess.run(sandbox.render_command(
            [pdftoppm, "-r", "50", "-png", "-l", str(MAX_WORD_PAGES), str(root / "word.pdf"), str(root / "page")],
            root, [], RENDER_TIMEOUT), capture_output=True, text=True)
        if r.returncode != 0:
            return
        for i, p in enumerate(sorted(root.glob("page-*.png"), key=lambda x: int(x.stem.rsplit("-", 1)[1])), 1):
            shutil.copyfile(p, dest / f"page-{i}.png")


def sync_samples(project: Project, ws: Path, docs: dict[str, str], design: Design,
                 refs_from: Design | None) -> list[str]:
    """Rewrite every sample with `design`'s mapping. With `refs_from` (the design whose
    mapping the samples and design/design.json references were made with), the workspace's
    references are carried over to the new element IDs; returns the neutral references that
    could not be carried."""
    current = read_design(ws)
    refs = current.design
    lost: list[str] = []
    for real, neutral in docs.items():
        version = project.document(real)["current"]
        if refs_from is not None:
            old = read_json(builder.ensure_conversion(project, real, version, refs_from) / DOC_DIR / ELEMENTS_FILE, [])
            new = read_json(builder.ensure_conversion(project, real, version, design) / DOC_DIR / ELEMENTS_FILE, [])
            moved, dropped = builder.remap_refs(refs, neutral, old, new)
            refs = builder.replace_doc_refs(refs, neutral, moved)
            lost += dropped
        write_sample(project, ws, real, version, neutral, design)
    if refs_from is not None:
        current.with_design(refs).write(ws / "design")
    return sorted(lost)


_MARKER_ID = re.compile(r'^#metadata\("([hptlfx]\d{4,})"\) <we-([se])>$', re.M)


def _shape(e: dict[str, Any]) -> tuple:
    t = e.get("table") or {}
    return (e["kind"], e.get("level"), t.get("columns"), t.get("header_rows"))


def align_ids(real: list[dict[str, Any]], twin: list[dict[str, Any]]) -> dict[str, str]:
    """Twin element ID -> the real document's ID for the same element.

    The twin is converted from the anonymised Word file, so its elements can differ from the
    real document's where conversion depends on text (a "Contents" heading is dropped from
    the real document but not from its lorem copy). Elements are aligned by structure (kind,
    heading level, table shape); twin elements with no counterpart get an unstyleable x-ID,
    so an agent can never address a real element through a shifted ID."""
    sm = difflib.SequenceMatcher(a=[_shape(e) for e in twin], b=[_shape(e) for e in real], autojunk=False)
    out: dict[str, str] = {}
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        for k in range(i2 - i1):
            tid = twin[i1 + k]["id"]
            out[tid] = real[j1 + k]["id"] if tag == "equal" else f"x{9000 + i1 + k:04d}"
    return out


def write_selection(ws: Path, selections: list[dict[str, Any]], mapping: dict[str, str]) -> list[dict[str, Any]]:
    """selection.json with neutral references; selections of documents outside the
    workspace are dropped. Only IDs, kinds, labels and table shapes: no text."""
    out = []
    for s in selections:
        if s["doc"] not in mapping:
            continue
        entry = {"ref": f"{mapping[s['doc']]}/{s['element']}", "kind": s.get("kind"),
                 "label": f"{mapping[s['doc']]} · {s.get('label', s['element'])}"}
        if s.get("table"):
            entry["table"] = s["table"]
        out.append(entry)
    write_json(ws / "selection.json", out)
    return out


def normalize_reference(data: bytes) -> bytes:
    """An attached image as a plain PNG: decoded, at most REFERENCE_EDGE on its longest side,
    re-encoded without metadata (EXIF, location, author, embedded profiles). Raises ValueError
    for anything that is not a readable PNG, JPEG, WebP or GIF image."""
    if len(data) > MAX_REFERENCE_BYTES:
        raise ValueError("the image is too large (12 MB at most)")
    try:
        with Image.open(io.BytesIO(data)) as im:
            if im.format not in ("PNG", "JPEG", "WEBP", "GIF"):
                raise ValueError("attach a PNG, JPEG, WebP or GIF image")
            if im.width * im.height > MAX_REFERENCE_PIXELS:
                raise ValueError("the image has too many pixels")
            im.seek(0)
            frame = im.convert("RGBA" if im.mode in ("RGBA", "LA", "PA") or "transparency" in im.info else "RGB")
    except (OSError, Image.DecompressionBombError, SyntaxError) as e:
        raise ValueError("the image could not be read") from e
    frame.thumbnail((REFERENCE_EDGE, REFERENCE_EDGE))
    out = io.BytesIO()
    frame.save(out, format="PNG", optimize=True)
    return out.getvalue()


def add_references(ws: Path, images: list[bytes]) -> list[str]:
    """Save normalized reference images in the workspace, numbered on from the earlier
    ones so that "the image from before" stays available. Returns workspace paths."""
    d = ws / REFERENCES_DIR
    d.mkdir(exist_ok=True)
    taken = [int(m.group(1)) for f in d.iterdir() if (m := re.fullmatch(r"ref-(\d+)\.png", f.name))]
    n = max(taken, default=0)
    out = []
    for data in images:
        n += 1
        name = f"{REFERENCES_DIR}/ref-{n:02d}.png"
        (ws / name).write_bytes(data)
        out.append(name)
    return out


def neutral_elements(ws: Path) -> dict[str, str]:
    out = {}
    for sample in sorted((ws / "samples").glob("doc-*")):
        for e in read_json(sample / ELEMENTS_FILE, []):
            out[f"{sample.name}/{e['id']}"] = e["kind"]
    return out


def read_design(ws: Path) -> Design:
    """The workspace's design files, refusing anything that is not a plain regular file. A
    missing mapping.toml is the default mapping."""
    for name in ("theme.toml", "design.json", "custom.typ", "mapping.toml"):
        p = ws / "design" / name
        if name == "mapping.toml" and not p.exists() and not p.is_symlink():
            continue
        if p.is_symlink() or not p.is_file():
            raise DesignError([f"design/{name}: must be a regular file"])
        if p.stat().st_size > 200_000:
            raise DesignError([f"design/{name}: too large"])
    try:
        return Design.load(ws / "design")
    except json.JSONDecodeError as e:
        raise DesignError([f"design/design.json: not valid JSON: {e}"]) from e


# ------------------------------------------------------------------ synthetic renders
def _sanitize(text: str, roots: list[str]) -> str:
    for r in roots:
        text = text.replace(r + "/", "").replace(r, ".")
    return text[:4000]


def render_samples(ws: Path, design: Design, docs: list[str] | None, scratch: Path,
                   out_pdf_dir: Path | None = None) -> dict[str, Any]:
    """Validate `design` and render the samples in the offline sandbox. PNG pages and the
    result go to ws/previews; PDFs optionally to out_pdf_dir. Paths in diagnostics are
    reduced to workspace-relative ones."""
    elements = neutral_elements(ws)
    try:
        validate(design, elements)
    except DesignError as e:
        return {"ok": False, "problems": e.problems, "docs": {}}
    samples = sorted(p.name for p in (ws / "samples").glob("doc-*"))
    if docs:
        samples = [s for s in samples if s in docs]
    if scratch.exists():
        shutil.rmtree(scratch)
    root = scratch / "root"
    (root / "template").mkdir(parents=True)
    for name in CORE_FILES:
        shutil.copyfile(NEUTRAL_DIR / name, root / "template" / name)
    design.write(root / "template")
    result: dict[str, Any] = {"ok": True, "problems": [], "docs": {}}
    typst = str(find_tool("typst") or "typst")
    pdftoppm = str(find_tool("pdftoppm") or "pdftoppm")
    previews = ws / "previews"
    for name in samples:
        src = ws / "samples" / name
        dst = root / "samples" / name
        _copy_regular(src, dst)
        (dst / "main.typ").write_text(_main_typ(f"Sample document {name[4:]}"), encoding="utf-8")
        inv = read_json(dst / ELEMENTS_FILE, [])
        write_json(dst / "design.json", resolve_for_document(design.design, name, inv))
        pdf = dst / "preview.pdf"
        cmd = [typst, "compile", "--root", str(root), "--diagnostic-format", "short",
               "--input", "theme=/template/theme.toml", "--input", f"design=/samples/{name}/design.json",
               "--font-path", str(NEUTRAL_FONTS), "--ignore-system-fonts", str(dst / "main.typ"), str(pdf)]
        r = subprocess.run(sandbox.render_command(cmd, root, [NEUTRAL_FONTS], RENDER_TIMEOUT),
                           capture_output=True, text=True)
        entry: dict[str, Any] = {}
        if r.returncode != 0 or not pdf.exists():
            killed = r.returncode in (124, 137, -9)
            entry["errors"] = ["render timed out"] if killed else \
                [_sanitize(line, [str(root)]) for line in r.stderr.splitlines() if line.strip()][:20]
            result["ok"] = False
        else:
            entry["warnings"] = [_sanitize(line, [str(root)]) for line in r.stderr.splitlines() if "warning" in line][:20]
            out = previews / name
            if out.exists():
                shutil.rmtree(out)
            out.mkdir(parents=True)
            png = subprocess.run(sandbox.render_command(
                [pdftoppm, "-r", "60", "-png", "-l", str(MAX_PREVIEW_PAGES), str(pdf), str(dst / "page")],
                root, [], RENDER_TIMEOUT), capture_output=True, text=True)
            pages = sorted(dst.glob("page-*.png"))
            for p in pages:
                shutil.copyfile(p, out / p.name)
            entry["pages"] = [f"previews/{name}/{p.name}" for p in pages]
            if png.returncode != 0:
                entry["warnings"].append("page images could not be produced")
            if out_pdf_dir is not None:
                out_pdf_dir.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(pdf, out_pdf_dir / f"{name}.pdf")
        entry["mapping_rules"] = (read_json(src / "styles.json") or {}).get("mapping_rules", {})
        result["docs"][name] = entry
    write_json(previews / "result.json", result)
    return result


class RequestWatcher:
    """Answers `render-preview` requests of one workspace until stopped."""

    def __init__(self, ws: Path, scratch: Path, on_render: Callable[[dict[str, Any]], None] | None = None,
                 prepare: Callable[[], dict[str, Any]] | None = None):
        """`prepare` runs before every render (the samples follow a changed mapping.toml); it
        returns {"problems": [...], "notes": [...]} to add to the result."""
        self.ws = ws
        self.scratch = scratch
        self.on_render = on_render
        self.prepare = prepare
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="we-render-requests")

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        req_dir, resp_dir = self.ws / ".requests", self.ws / ".responses"
        while not self._stop.is_set():
            try:
                pending = sorted(p for p in req_dir.glob("*.json") if not p.is_symlink())
            except OSError:
                pending = []
            for req in pending:
                rid = req.stem
                try:
                    body = json.loads(req.read_text()[:2000])
                except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                    body = {}
                req.unlink(missing_ok=True)
                if not _REQ_ID.match(rid):
                    continue
                wanted = body.get("docs") if isinstance(body, dict) else None
                docs = None if wanted in (None, "all") else [str(wanted)]
                try:
                    prepared = self.prepare() if self.prepare else {}
                    if prepared.get("problems"):
                        result = {"ok": False, "problems": prepared["problems"], "docs": {}}
                    else:
                        result = render_samples(self.ws, read_design(self.ws), docs, self.scratch)
                        result["notes"] = prepared.get("notes", [])
                except DesignError as e:
                    result = {"ok": False, "problems": e.problems, "docs": {}}
                except Exception as e:  # noqa: BLE001
                    result = {"ok": False, "problems": [f"render failed: {type(e).__name__}"], "docs": {}}
                summary = {"ok": result["ok"], "problems": result["problems"], "notes": result.get("notes", []),
                           "docs": {k: {"pages": v.get("pages", []), "errors": v.get("errors", []),
                                        "warnings": v.get("warnings", []),
                                        "mapping_rules": v.get("mapping_rules", {})}
                                    for k, v in result["docs"].items()}}
                tmp = resp_dir / f".{rid}.tmp"
                tmp.write_text(json.dumps(summary, indent=1))
                os.replace(tmp, resp_dir / f"{rid}.json")
                if self.on_render:
                    self.on_render(summary)
            self._stop.wait(0.3)

