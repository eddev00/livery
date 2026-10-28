"""Cell background colours, vertical alignment and vertical merges from a
.docx, in the order pandoc emits tables.

pandoc keeps table structure and inline formatting but drops shading and
vertical alignment. This module reads word/document.xml and produces, per
table, {"fills": grid, "valign": grid, "rows": grid} indexed [row][column]
(hex or null; "top" | "center" | "bottom"; [first row, row count] of the
vertical merge the cell belongs to), with merged cells expanded to every grid
position they cover. The template applies them by table index.
"""
from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_NO_COLOR = {"", "auto", "ffffff", "none"}


def _fill_of(tc: ET.Element) -> str | None:
    tcpr = tc.find(f"{W}tcPr")
    if tcpr is None:
        return None
    shd = tcpr.find(f"{W}shd")
    if shd is None:
        return None
    fill = (shd.get(f"{W}fill") or "").strip().lower()
    if fill in _NO_COLOR or not re.fullmatch(r"[0-9a-f]{6}", fill):
        return None
    return "#" + fill.upper()


def _valign_of(tc: ET.Element) -> str:
    tcpr = tc.find(f"{W}tcPr")
    if tcpr is None:
        return "top"
    va = tcpr.find(f"{W}vAlign")
    v = (va.get(f"{W}val") or "top").lower() if va is not None else "top"
    return {"center": "center", "bottom": "bottom", "both": "top"}.get(v, "top")


def _span_of(tc: ET.Element) -> int:
    tcpr = tc.find(f"{W}tcPr")
    if tcpr is None:
        return 1
    gs = tcpr.find(f"{W}gridSpan")
    try:
        return max(1, int(gs.get(f"{W}val"))) if gs is not None else 1
    except (TypeError, ValueError):
        return 1


def _vmerge_continues(tc: ET.Element) -> bool:
    tcpr = tc.find(f"{W}tcPr")
    if tcpr is None:
        return False
    vm = tcpr.find(f"{W}vMerge")
    return vm is not None and (vm.get(f"{W}val") or "continue") != "restart"


def _cell_text(tc: ET.Element) -> str:
    return "".join(t.text or "" for t in tc.iter(f"{W}t")).strip()


def _is_heading_par(p: ET.Element) -> bool:
    ppr = p.find(f"{W}pPr")
    if ppr is None:
        return False
    st = ppr.find(f"{W}pStyle")
    return st is not None and (st.get(f"{W}val") or "").lower().startswith(("heading", "titre", "berschrift"))


def _direct_tables(tc: ET.Element) -> list[ET.Element]:
    return [el for el in tc if el.tag == f"{W}tbl"]


def _has_heading_or_table(tc: ET.Element) -> bool:
    return bool(_direct_tables(tc)) or any(_is_heading_par(p) for p in tc if p.tag == f"{W}p")


def _tables_in_cell(tc: ET.Element) -> list[ET.Element]:
    """Nested tables of a cell in document order (direct children and deeper)."""
    return [el for el in tc.iter(f"{W}tbl") if el is not tc]


def _row_spans(starts: list[list[int]]) -> list[list[list[int]]]:
    """[first row, row count] per grid position from the first row of each cell's
    vertical merge (a cell that is not merged starts on its own row)."""
    out = [[[s, 0] for s in row] for row in starts]
    for y, row in enumerate(starts):
        for x, s in enumerate(row):
            if s != y:
                continue
            n = 1
            while y + n < len(starts) and x < len(starts[y + n]) and starts[y + n][x] == y:
                n += 1
            for k in range(n):
                out[y + k][x][1] = n
    return out


def _process_table(tbl: ET.Element):
    """Yield {"fills", "valign", "rows"} grids in the order pandoc emits tables
    after the filter's container-row split (wordeasy.lua rule 12): rows before
    the cut, the nested tables of those rows, the hoisted cell's nested tables,
    then the remaining rows as a new table, and so on."""
    fills: list[list[str | None]] = []
    valign: list[list[str]] = []
    starts: list[list[int]] = []
    nested_pending: list[ET.Element] = []

    def flush(force: bool):
        nonlocal fills, valign, starts, nested_pending
        if fills or force:
            yield {"fills": fills, "valign": valign, "rows": _row_spans(starts)}
        for nt in nested_pending:
            yield from _process_table(nt)
        fills, valign, starts, nested_pending = [], [], [], []

    for tr in tbl.findall(f"{W}tr"):
        cells = tr.findall(f"{W}tc")
        content = [tc for tc in cells if _cell_text(tc) or _has_heading_or_table(tc)]
        if len(content) == 1 and _has_heading_or_table(content[0]):
            yield from flush(False)
            for nt in _tables_in_cell(content[0]):
                yield from _process_table(nt)
            continue
        frow: list[str | None] = []
        vrow: list[str] = []
        srow: list[int] = []
        for tc in cells:
            fill = _fill_of(tc)
            va = _valign_of(tc)
            span = _span_of(tc)
            start = len(fills)
            if _vmerge_continues(tc) and fills:
                col = len(frow)
                if fill is None and col < len(fills[-1]):
                    fill = fills[-1][col]
                if col < len(valign[-1]):
                    va = valign[-1][col]
                if col < len(starts[-1]):
                    start = starts[-1][col]
            frow.extend([fill] * span)
            vrow.extend([va] * span)
            srow.extend([start] * span)
            nested_pending.extend(_direct_tables(tc))
        fills.append(frow)
        valign.append(vrow)
        starts.append(srow)
    yield from flush(False)


def extract_table_fills(docx: Path) -> list[dict[str, list[list]]]:
    """All tables in the order the Typst template will meet them."""
    with zipfile.ZipFile(docx) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    body = root.find(f"{W}body")
    if body is None:
        return []
    parent = {c: p for p in body.iter() for c in p}
    out: list[dict[str, list[list]]] = []
    for tbl in body.iter(f"{W}tbl"):
        # top-level tables only: nested ones are handled by their parent
        node, nested = parent.get(tbl), False
        while node is not None:
            if node.tag == f"{W}tbl":
                nested = True
                break
            node = parent.get(node)
        if not nested:
            out.extend(_process_table(tbl))
    return out


def write_table_fills(docx: Path, out: Path) -> int:
    fills = extract_table_fills(docx)
    out.write_text(json.dumps(fills, ensure_ascii=False), encoding="utf-8")
    return len(fills)
