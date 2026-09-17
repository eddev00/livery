"""Page previews for the side-by-side viewer: word boxes and lazily rendered page images.

Both come from poppler, which the pipeline already requires: ``pdftotext -bbox-layout`` gives
every word with its box in PDF points, ``pdftoppm`` renders one page to PNG on demand. Renders
are cached under ``<doc>/.preview/`` and keyed by the PDF's mtime, so a rebuild invalidates them
and deleting the document folder removes them.
"""
from __future__ import annotations

import bisect
import difflib
import re
import threading
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from ..tools import require_tools, run

CACHE_DIR = ".preview"
DPI = 120          # fits a half-screen pane
DPIS = (DPI, 200)  # the second one for zoomed-in panes
_XHTML = "{http://www.w3.org/1999/xhtml}"
_pages_cache: dict[tuple[str, int], dict[str, Any]] = {}
_align_cache: dict[tuple[tuple[str, int], tuple[str, int]], dict[str, list[int]]] = {}
_render_lock = threading.Lock()


def _key(pdf: Path) -> tuple[str, int]:
    return str(pdf), pdf.stat().st_mtime_ns


def pages(pdf: Path) -> dict[str, Any]:
    """Page sizes (points) and words as ``[x0, y0, x1, y1, text]`` in reading order."""
    key = _key(pdf)
    if key in _pages_cache:
        return _pages_cache[key]
    tools = require_tools("pdftotext")
    r = run([tools["pdftotext"], "-bbox-layout", pdf, "-"])
    if not r.ok:
        raise RuntimeError(f"pdftotext failed: {r.stderr.strip()}")
    out: list[dict[str, Any]] = []
    for page in ET.fromstring(r.stdout).iter(f"{_XHTML}page"):
        words = [[round(float(w.get("xMin")), 2), round(float(w.get("yMin")), 2),
                  round(float(w.get("xMax")), 2), round(float(w.get("yMax")), 2), w.text or ""]
                 for w in page.iter(f"{_XHTML}word")]
        out.append({"w": float(page.get("width")), "h": float(page.get("height")), "words": words})
    result = {"pages": out}
    _pages_cache.clear()  # one document at a time is plenty; the parse is cheap
    _pages_cache[key] = result
    return result


def page_png(pdf: Path, n: int, dpi: int = DPI) -> Path:
    """Render page ``n`` (1-based) at ``dpi`` to PNG, or return the cached render."""
    if dpi not in DPIS:
        raise ValueError(f"dpi must be one of {DPIS}")
    cache = pdf.parent / CACHE_DIR
    stem = f"{pdf.stem}-{pdf.stat().st_mtime_ns}"
    target = cache / f"{stem}-p{n}@{dpi}.png"
    if target.is_file():
        return target
    tools = require_tools("pdftoppm")
    with _render_lock:
        if target.is_file():
            return target
        cache.mkdir(exist_ok=True)
        for stale in cache.glob(f"{pdf.stem}-*-p*.png"):  # renders of an older build of this PDF
            if not stale.name.startswith(stem):
                stale.unlink(missing_ok=True)
        r = run([tools["pdftoppm"], "-r", str(dpi), "-png", "-f", str(n), "-l", str(n), "-singlefile", pdf, target.with_suffix("")])
        if not r.ok or not target.is_file():
            raise RuntimeError(f"pdftoppm failed: {r.stderr.strip()}")
    return target


# ------------------------------------------------------------------ word alignment
_NON_WORD = re.compile(r"[^\w]+", re.UNICODE)


def _norm(word: str) -> str:
    return _NON_WORD.sub("", word.lower())


def _lis(pairs: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Longest chain of anchors increasing on both sides (pairs come sorted by the first side)."""
    tails: list[int] = []
    back: list[int] = []
    tail_idx: list[int] = []
    for n, (_, j) in enumerate(pairs):
        k = bisect.bisect_left(tails, j)
        if k == len(tails):
            tails.append(j); tail_idx.append(n)
        else:
            tails[k] = j; tail_idx[k] = n
        back.append(tail_idx[k - 1] if k else -1)
    out: list[tuple[int, int]] = []
    n = tail_idx[-1] if tail_idx else -1
    while n >= 0:
        out.append(pairs[n]); n = back[n]
    out.reverse()
    return out


def align_tokens(a: list[str], b: list[str]) -> list[int]:
    """For every token of ``a`` the index of its counterpart in ``b`` (-1 when it has none).

    Patience-style: n-grams that occur exactly once on both sides of a range are anchors, the
    longest monotonic chain of anchors is kept and extended word by word, and the gaps between
    them are aligned the same way with ever shorter n-grams. Text that only exists on one side
    (running headers, page numbers, a table of contents) simply stays unmatched.
    """
    ab = [-1] * len(a)

    def anchors(a0: int, a1: int, b0: int, b1: int, k: int) -> list[tuple[int, int]]:
        def unique(seq: list[str], lo: int, hi: int) -> dict[tuple[str, ...], int]:
            seen: dict[tuple[str, ...], int] = {}
            for i in range(lo, hi - k + 1):
                key = tuple(seq[i:i + k])
                if "" in key:
                    continue
                seen[key] = -1 if key in seen else i
            return {key: i for key, i in seen.items() if i >= 0}
        ua, ub = unique(a, a0, a1), unique(b, b0, b1)
        return _lis(sorted((i, ub[key]) for key, i in ua.items() if key in ub))

    def fill(a0: int, a1: int, b0: int, b1: int) -> None:
        """Last resort for a gap without unique anchors (a page of repeated words, say): a plain
        diff when the gap is small enough, else a walk inwards from the gap's anchored ends."""
        if (a1 - a0) * (b1 - b0) <= 4_000_000:
            sm = difflib.SequenceMatcher(None, a[a0:a1], b[b0:b1], autojunk=False)
            for i, j, size in sm.get_matching_blocks():
                for u in range(size):
                    ab[a0 + i + u] = b0 + j + u
            return
        s, t = a0, b0
        while s < a1 and t < b1 and a[s] == b[t]:
            ab[s] = t; s += 1; t += 1
        e, f = a1, b1
        while e > s and f > t and a[e - 1] == b[f - 1]:
            e -= 1; f -= 1; ab[e] = f

    stack = [(0, len(a), 0, len(b), 4)]
    while stack:
        a0, a1, b0, b1, k = stack.pop()
        if a0 >= a1 or b0 >= b1:
            continue
        chain = anchors(a0, a1, b0, b1, k) if k >= 1 else []
        if not chain:
            if k > 1:
                stack.append((a0, a1, b0, b1, k // 2))
            else:
                fill(a0, a1, b0, b1)
            continue
        prev_a, prev_b = a0, b0
        for n, (i, j) in enumerate(chain):
            if i < prev_a or j < prev_b:  # swallowed by the previous anchor's extension
                continue
            ni, nj = next(((x, y) for x, y in chain[n + 1:] if x >= i + k and y >= j + k), (a1, b1))
            # extend the anchor backwards and forwards while the words keep agreeing
            s, t = i, j
            while s > prev_a and t > prev_b and a[s - 1] == b[t - 1]:
                s -= 1; t -= 1
            e, f = i + k, j + k
            while e < ni and f < nj and a[e] == b[f]:
                e += 1; f += 1
            for u in range(e - s):
                ab[s + u] = t + u
            if s > prev_a and t > prev_b:
                stack.append((prev_a, s, prev_b, t, k))
            prev_a, prev_b = e, f
        if prev_a < a1 and prev_b < b1:
            stack.append((prev_a, a1, prev_b, b1, k))
    return ab


def align(pdf_a: Path, pdf_b: Path) -> dict[str, list[int]]:
    """Word-index maps both ways between two PDFs of the same document."""
    key = (_key(pdf_a), _key(pdf_b))
    if key in _align_cache:
        return _align_cache[key]
    a = [_norm(w[4]) for pg in pages(pdf_a)["pages"] for w in pg["words"]]
    b = [_norm(w[4]) for pg in pages(pdf_b)["pages"] for w in pg["words"]]
    ab = align_tokens(a, b)
    ba = [-1] * len(b)
    for i, j in enumerate(ab):
        if j >= 0:
            ba[j] = i
    result = {"ab": ab, "ba": ba}
    _align_cache.clear()
    _align_cache[key] = result
    return result
