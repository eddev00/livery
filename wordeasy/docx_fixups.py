"""Small, content-neutral rewrites of the Word XML applied before pandoc."""
from __future__ import annotations

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
