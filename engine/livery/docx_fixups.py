"""Rewrites of the Word XML applied before pandoc: small content-neutral fixes, and the
page rules of a mapping (drop_pages), whose removals are logged and accounted for."""
from __future__ import annotations

import html
import re
import zipfile
from pathlib import Path

_OUTLINE_RE = re.compile(r'<w:outlineLvl w:val="\d"/>')
_PARA_RE = re.compile(r"<w:p[ >].*?</w:p>", re.S)
_STYLE_RE = re.compile(r'<w:pStyle w:val="([^"]+)"/>')


def _is_heading_style(style_id: str | None) -> bool:
    return bool(style_id) and style_id.lower().startswith(("heading", "titre", "berschrift", "title"))


def strip_outline_levels(docx_in: Path, docx_out: Path) -> int:
    """Remove the outline level from paragraphs that are not Heading-styled.

    pandoc promotes any paragraph with an outline level to a heading. Word
    authors set outline levels on bold "title" paragraphs to get them into the
    navigation pane; here only real Heading styles may become headings. The
    paragraph text is untouched. Returns the number of paragraphs changed.
    """
    with zipfile.ZipFile(docx_in) as z:
        doc = z.read("word/document.xml").decode("utf-8")
        changed = 0

        def fix(m: re.Match) -> str:
            nonlocal changed
            p = m.group(0)
            if "<w:outlineLvl" not in p:
                return p
            st = _STYLE_RE.search(p)
            if _is_heading_style(st.group(1) if st else None):
                return p
            changed += 1
            return _OUTLINE_RE.sub("", p)

        new_doc = _PARA_RE.sub(fix, doc)
        if changed == 0:
            return 0
        with zipfile.ZipFile(docx_out, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in z.infolist():
                zout.writestr(item, new_doc.encode("utf-8") if item.filename == "word/document.xml" else z.read(item.filename))
    return changed


_TOKEN_RE = re.compile(r"<[^>]+>|[^<]+")
_TAG_RE = re.compile(r"<(/?)([\w:]+)")
_TEXT_RE = re.compile(r"<w:t(?:\s[^>]*)?>([^<]*)</w:t>")
_SECT_TYPE_RE = re.compile(r'<w:type w:val="(\w+)"')


def block_text(xml: str) -> str:
    """The text of a block of Word XML, one space between paragraphs (table cells included)."""
    paras = [html.unescape("".join(_TEXT_RE.findall(p))) for p in re.findall(r"<w:p[ >].*?</w:p>", xml, re.S)]
    return re.sub(r"\s+", " ", " ".join(p for p in paras if p.strip())).strip()


def body_blocks(doc_xml: str) -> list[tuple[int, int, str]]:
    """The top-level children of <w:body>: (start, end, tag) offsets into the XML."""
    body = re.search(r"<w:body>", doc_xml)
    if body is None:
        return []
    out: list[tuple[int, int, str]] = []
    depth, start, tag = 0, 0, ""
    for m in _TOKEN_RE.finditer(doc_xml, body.end()):
        tok = m.group(0)
        if not tok.startswith("<") or tok.startswith(("<?", "<!")):
            continue
        tm = _TAG_RE.match(tok)
        if tm is None:
            continue
        closing, name = tm.group(1) == "/", tm.group(2)
        if closing:
            if depth == 0:  # </w:body>
                break
            depth -= 1
            if depth == 0:
                out.append((start, m.end(), tag))
        elif tok.endswith("/>"):
            if depth == 0:
                out.append((m.start(), m.end(), name))
        else:
            if depth == 0:
                start, tag = m.start(), name
            depth += 1
    return out


def block_pages(doc_xml: str, blocks: list[tuple[int, int, str]]) -> list[int]:
    """The Word page each top-level block starts on.

    Word records where it broke pages when it last laid the document out
    (<w:lastRenderedPageBreak/>); when the file has those, they decide. Otherwise only
    explicit breaks count: page breaks, "page break before" and section breaks that start a
    new page. A break before a block's first text moves the block itself to the next page."""
    rendered = "<w:lastRenderedPageBreak/>" in doc_xml
    page, pages = 1, []
    for start, end, tag in blocks:
        xml = doc_xml[start:end]
        seen_text, block_start = False, None
        for m in re.finditer(r"<w:t(?:\s[^>]*)?>[^<]*\S[^<]*</w:t>|<w:lastRenderedPageBreak/>|<w:br\b[^>]*/>"
                             r"|<w:pageBreakBefore(?:\s[^>]*)?/>", xml):
            tok = m.group(0)
            if tok.startswith("<w:t"):
                if not seen_text:
                    seen_text, block_start = True, page
                continue
            if rendered:
                is_break = tok.startswith("<w:lastRenderedPageBreak")
            elif tok.startswith("<w:br"):
                is_break = 'w:type="page"' in tok
            else:
                is_break = not re.search(r'w:val="(0|false|off)"', tok)
            if is_break:
                page += 1
        pages.append(block_start if block_start is not None else page)
        if not rendered and tag == "w:p" and "<w:sectPr" in xml:
            st = _SECT_TYPE_RE.search(xml[xml.index("<w:sectPr"):])
            if not st or st.group(1) != "continuous":
                page += 1
    return pages


def drop_pages(docx_in: Path, docx_out: Path, rules: list[tuple[str, set[int]]]) -> list[tuple[str, str]]:
    """Remove the top-level blocks that start on the pages of page rules [(name, pages)].

    The first matching rule takes a block. The section properties at the end of the body
    are never removed. Returns (rule name, block text) for every removed block, in order;
    docx_out is written only when something was removed."""
    with zipfile.ZipFile(docx_in) as z:
        doc = z.read("word/document.xml").decode("utf-8")
        blocks = body_blocks(doc)
        pages = block_pages(doc, blocks)
        removed: list[tuple[str, str]] = []
        cuts: list[tuple[int, int]] = []
        for (start, end, tag), page in zip(blocks, pages):
            if tag == "w:sectPr":
                continue
            for name, wanted in rules:
                if page in wanted:
                    removed.append((name, block_text(doc[start:end])))
                    cuts.append((start, end))
                    break
        if not cuts:
            return []
        parts, pos = [], 0
        for start, end in cuts:
            parts.append(doc[pos:start])
            pos = end
        parts.append(doc[pos:])
        new_doc = "".join(parts)
        with zipfile.ZipFile(docx_out, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in z.infolist():
                zout.writestr(item, new_doc.encode("utf-8") if item.filename == "word/document.xml" else z.read(item.filename))
    return removed
