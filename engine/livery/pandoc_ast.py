"""Helpers over the pandoc JSON AST: text extraction and structural counts."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable

Node = dict[str, Any]

# Inline node types whose content is a list of inlines under "c" (or c[1]).
_INLINE_WRAPPERS = {"Emph", "Strong", "Strikeout", "Superscript", "Subscript", "SmallCaps", "Underline"}


def _walk_inlines(inlines: Iterable[Node], out: list[str], notes: list[str] | None) -> None:
    for n in inlines:
        t = n.get("t")
        c = n.get("c")
        if t == "Str":
            out.append(c)
        elif t in ("Space", "SoftBreak", "LineBreak"):
            out.append(" ")
        elif t in _INLINE_WRAPPERS:
            _walk_inlines(c, out, notes)
        elif t == "Quoted":
            _walk_inlines(c[1], out, notes)
        elif t == "Cite":
            _walk_inlines(c[1], out, notes)
        elif t == "Code":
            out.append(c[1])
        elif t == "Math":
            out.append(" " + c[1] + " ")
        elif t == "Link":
            # link text is content; the target is checked separately
            _walk_inlines(c[1], out, notes)
        elif t == "Image":
            pass  # alt text is never rendered; the image itself is counted separately
        elif t == "Span":
            _walk_inlines(c[1], out, notes)
        elif t == "Note":
            buf: list[str] = []
            _walk_blocks(c, buf, notes)
            note_text = "".join(buf)
            if notes is not None:
                notes.append(note_text)
            out.append(" " + note_text + " ")
        elif t == "RawInline":
            pass
        else:  # unknown inline: be conservative, keep any strings inside
            if isinstance(c, list):
                _walk_any(c, out, notes)


def _walk_any(obj: Any, out: list[str], notes: list[str] | None) -> None:
    if isinstance(obj, dict) and "t" in obj:
        t = obj["t"]
        if t in {"Str", "Space", "SoftBreak", "LineBreak", "Code", "Math", "Note", "Link", "Image",
                 "Span", "Quoted", "Cite", "RawInline"} or t in _INLINE_WRAPPERS:
            _walk_inlines([obj], out, notes)
        else:
            _walk_blocks([obj], out, notes)
    elif isinstance(obj, list):
        for x in obj:
            _walk_any(x, out, notes)


def _walk_blocks(blocks: Iterable[Node], out: list[str], notes: list[str] | None) -> None:
    for b in blocks:
        t = b.get("t")
        c = b.get("c")
        if t in ("Para", "Plain"):
            _walk_inlines(c, out, notes)
            out.append("\n")
        elif t == "Header":
            _walk_inlines(c[2], out, notes)
            out.append("\n")
        elif t == "LineBlock":
            for line in c:
                _walk_inlines(line, out, notes)
                out.append("\n")
        elif t == "CodeBlock":
            out.append(c[1] + "\n")
        elif t == "RawBlock":
            pass
        elif t == "BlockQuote":
            _walk_blocks(c, out, notes)
        elif t in ("BulletList", "OrderedList"):
            items = c if t == "BulletList" else c[1]
            for item in items:
                _walk_blocks(item, out, notes)
        elif t == "DefinitionList":
            for term, defs in c:
                _walk_inlines(term, out, notes)
                out.append("\n")
                for d in defs:
                    _walk_blocks(d, out, notes)
        elif t == "Div":
            _walk_blocks(c[1], out, notes)
        elif t == "Figure":
            # c = [attr, caption, blocks]; caption = [short, blocks]
            _walk_blocks(c[2], out, notes)
            _walk_blocks(c[1][1], out, notes)
        elif t == "Table":
            for cell in iter_table_cells(b):
                _walk_blocks(cell_blocks(cell), out, notes)
                out.append("\n")
            _walk_blocks(c[1][1], out, notes)  # caption
        elif t == "HorizontalRule":
            pass
        else:
            if isinstance(c, list):
                _walk_any(c, out, notes)


def stringify_blocks(blocks: list[Node]) -> str:
    out: list[str] = []
    _walk_blocks(blocks, out, None)
    return "".join(out)


def stringify_inlines(inlines: list[Node]) -> str:
    out: list[str] = []
    _walk_inlines(inlines, out, None)
    return "".join(out)


def meta_string(meta: Node, key: str) -> str:
    v = meta.get(key)
    if not v:
        return ""
    t, c = v.get("t"), v.get("c")
    if t == "MetaInlines":
        return stringify_inlines(c).strip()
    if t == "MetaString":
        return c.strip()
    if t == "MetaBlocks":
        return stringify_blocks(c).strip()
    if t == "MetaList":
        parts = []
        for item in c:
            if item.get("t") == "MetaInlines":
                parts.append(stringify_inlines(item["c"]).strip())
            elif item.get("t") == "MetaString":
                parts.append(item["c"].strip())
        return ", ".join(p for p in parts if p)
    return ""


# ------------------------------------------------------------------ tables
def iter_table_cells(table: Node):
    """Yield cells of a pandoc Table in reading order (head, bodies, foot)."""
    c = table["c"]
    head = c[3]  # [attr, rows]
    bodies = c[4]  # list of [attr, rowheadcols, headrows, bodyrows]
    foot = c[5]  # [attr, rows]
    for row in head[1]:
        yield from row[1]
    for body in bodies:
        for row in body[2]:
            yield from row[1]
        for row in body[3]:
            yield from row[1]
    for row in foot[1]:
        yield from row[1]


def cell_blocks(cell: list) -> list[Node]:
    # Cell = [attr, alignment, rowspan, colspan, blocks]
    return cell[4]


# ------------------------------------------------------------------ counts
@dataclass
class Structure:
    headings: Counter = field(default_factory=Counter)  # level -> count
    tables: int = 0
    table_cells: list[str] = field(default_factory=list)  # stringified, reading order
    images: int = 0
    image_targets: list[str] = field(default_factory=list)
    list_items: int = 0
    notes: int = 0
    links: list[str] = field(default_factory=list)
    code_blocks: int = 0
    block_quotes: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "headings": {str(k): v for k, v in sorted(self.headings.items())},
            "tables": self.tables,
            "table_cells": len(self.table_cells),
            "images": self.images,
            "list_items": self.list_items,
            "footnotes": self.notes,
            "links": len(self.links),
            "code_blocks": self.code_blocks,
            "block_quotes": self.block_quotes,
        }


def _scan(obj: Any, s: Structure) -> None:
    if isinstance(obj, dict) and "t" in obj:
        t, c = obj["t"], obj.get("c")
        if t == "Header":
            s.headings[c[0]] += 1
            _scan(c[2], s)
        elif t == "Table":
            s.tables += 1
            for cell in iter_table_cells(obj):
                s.table_cells.append(stringify_blocks(cell_blocks(cell)))
                _scan(cell_blocks(cell), s)
            _scan(c[1], s)  # caption
        elif t == "Image":
            s.images += 1
            s.image_targets.append(c[2][0])
            _scan(c[1], s)
        elif t == "Link":
            s.links.append(c[2][0])
            _scan(c[1], s)
        elif t == "BulletList":
            s.list_items += len(c)
            _scan(c, s)
        elif t == "OrderedList":
            s.list_items += len(c[1])
            _scan(c[1], s)
        elif t == "Note":
            s.notes += 1
            _scan(c, s)
        elif t == "CodeBlock":
            s.code_blocks += 1
        elif t == "BlockQuote":
            s.block_quotes += 1
            _scan(c, s)
        else:
            _scan(c, s)
    elif isinstance(obj, list):
        for x in obj:
            _scan(x, s)


def structure_of(blocks: list[Node]) -> Structure:
    s = Structure()
    _scan(blocks, s)
    return s


# ------------------------------------------------------- revisions/comments
@dataclass
class Revisions:
    insertions: list[str] = field(default_factory=list)
    deletions: list[str] = field(default_factory=list)
    comments: list[str] = field(default_factory=list)

    @property
    def has_tracked_changes(self) -> bool:
        return bool(self.insertions or self.deletions)

    def as_dict(self) -> dict[str, Any]:
        return {"insertions": self.insertions, "deletions": self.deletions, "comments": self.comments}


def _scan_revisions(obj: Any, r: Revisions) -> None:
    if isinstance(obj, dict) and "t" in obj:
        t, c = obj["t"], obj.get("c")
        if t == "Span":
            classes = c[0][1]
            attrs = dict(c[0][2])
            text = stringify_inlines(c[1]).strip()
            if "insertion" in classes:
                r.insertions.append(text)
            elif "deletion" in classes:
                r.deletions.append(text)
            elif "comment-start" in classes:
                r.comments.append(text or f"(comment by {attrs.get('author', '?')})")
        _scan_revisions(c, r)
    elif isinstance(obj, list):
        for x in obj:
            _scan_revisions(x, r)


def revisions_of(blocks: list[Node]) -> Revisions:
    r = Revisions()
    _scan_revisions(blocks, r)
    return r
