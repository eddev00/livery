"""Content-integrity checks. Every check returns a CheckResult; FAIL blocks the deliverable."""
from __future__ import annotations

import difflib
import html
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import pandoc_ast as past
from .normalize import tokens, word_tokens
from .tools import require_tools, run

PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"


@dataclass
class CheckResult:
    name: str
    status: str
    summary: str
    details: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status, "summary": self.summary,
                "details": self.details, "data": self.data}


def worst(results: list[CheckResult]) -> str:
    order = {FAIL: 3, WARN: 2, PASS: 1, SKIP: 0}
    return max(results, key=lambda r: order[r.status]).status if results else PASS


# ------------------------------------------------------------------ diffing
def diff_hunks(a: list[str], b: list[str], context: int = 5, max_hunks: int = 20) -> list[str]:
    sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    hunks: list[str] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        before = " ".join(a[max(0, i1 - context):i1])
        after = " ".join(a[i2:i2 + context])
        src = " ".join(a[i1:i2])
        out = " ".join(b[j1:j2])
        hunks.append(f"[{tag}] …{before} «{src}» → «{out}» {after}…")
        if len(hunks) >= max_hunks:
            hunks.append("(more hunks omitted)")
            break
    return hunks


# ---------------------------------------------------------- check 1: text
def check_text_after_conversion(source_text: str, typ_text: str, ignored: list[str]) -> CheckResult:
    src, out = tokens(source_text), tokens(typ_text)
    if ignored:
        return CheckResult(
            "text_after_conversion", FAIL,
            f"pandoc's Typst reader ignored {len(ignored)} element(s) in content.typ; the file contains constructs that cannot be verified",
            details=ignored[:20],
        )
    if src == out:
        return CheckResult("text_after_conversion", PASS, f"{len(src)} tokens identical between source and content.typ",
                           data={"tokens": len(src)})
    src_w, out_w = word_tokens(source_text), word_tokens(typ_text)
    hunks = diff_hunks(src, out)
    status = WARN if src_w == out_w else FAIL
    summary = (f"words identical but punctuation differs in {len(hunks)} place(s)" if status == WARN
               else f"{len(src)} source tokens vs {len(out)} tokens in content.typ, {len(hunks)} differing hunk(s)")
    return CheckResult("text_after_conversion", status, summary, details=hunks,
                       data={"tokens_source": len(src), "tokens_output": len(out)})


# ----------------------------------------------------- check 2: structure
def check_structure(src: past.Structure, out: past.Structure) -> CheckResult:
    problems: list[str] = []
    a, b = src.as_dict(), out.as_dict()
    for key in ("headings", "tables", "table_cells", "images", "list_items", "footnotes", "links", "code_blocks", "block_quotes"):
        if a[key] != b[key]:
            problems.append(f"{key}: source {a[key]} → content.typ {b[key]}")
    # Cell-by-cell text (normalized) in reading order
    if len(src.table_cells) == len(out.table_cells):
        for i, (x, y) in enumerate(zip(src.table_cells, out.table_cells)):
            if tokens(x) != tokens(y):
                problems.append(f"table cell #{i + 1}: «{x.strip()}» → «{y.strip()}»")
    if sorted(src.links) != sorted(out.links):
        problems.append(f"link targets differ: {sorted(set(src.links) ^ set(out.links))}")
    status = FAIL if problems else PASS
    return CheckResult("structure_after_conversion", status,
                       "structure identical" if not problems else f"{len(problems)} structural difference(s)",
                       details=problems, data={"source": a, "output": b})


# ------------------------------------------------------- check 3: PDF text
def pdf_text(pdf: Path) -> str:
    tools = require_tools("pdftotext")
    # Default (reading-order) mode. It silently de-hyphenates words broken at a
    # line end ("Sub-subsection" -> "Subsubsection"), so PDF comparisons use
    # join_hyphens=True on both sides; -raw would glue words across line ends instead.
    r = run([tools["pdftotext"], "-enc", "UTF-8", "-nopgbrk", "-eol", "unix", pdf, "-"])
    return r.stdout


def reconcile(src_tokens: list[str], pdf_tokens: list[str]) -> dict[str, list[str]]:
    """Match source tokens against PDF tokens as multisets, then explain the rest.

    pdftotext output differs from the source in two harmless ways: neighbouring
    words are glued when a cell overflows or spacing is tight ("SED CONSECTETUR"
    -> "SEDCONSECTETUR"), and a word broken at a line end may come back split
    ("dolore-magna" -> "dolore", "magna"; pieces of a word split inside a narrow
    cell may even be interleaved with other cells). When an overflowing word lands
    on a neighbour so that two identical letters coincide, pdftotext keeps only one
    of them (its fake-bold de-duplication): "EIUSMOD" + "DUI" -> "EIUSMODUI". A glued
    token that differs only by such a shared letter is accepted too. All of these are
    recognised and reported; whatever remains is genuinely missing or extra. Only PDF tokens in
    surplus over the source may serve as explanations, so a legitimate token is
    never cannibalised.
    """
    src_c, pdf_c = Counter(src_tokens), Counter(pdf_tokens)
    need = sorted((src_c - pdf_c).elements(), key=len, reverse=True)
    surplus = pdf_c - src_c
    pdf = list(pdf_tokens)          # working copy; "" marks a consumed token
    glued: list[str] = []
    split: list[str] = []
    # Letters peeled off next to a remainder: before[i] is the last letter of the word
    # removed from the start of pdf[i], after[i] the first letter of one removed from its end.
    before: dict[int, str] = {}
    after: dict[int, str] = {}

    def usable(t: str) -> bool:
        return bool(t) and surplus[t] > 0

    def decompose(s: str) -> list[str] | None:
        """s as a concatenation (>= 2 parts) of surplus tokens, adjacent or not."""
        best: dict[int, list[str] | None] = {0: []}
        for i in range(1, len(s) + 1):
            best[i] = None
            for j in range(max(0, i - 60), i):
                if best[j] is None:
                    continue
                piece = s[j:i]
                if surplus[piece] - best[j].count(piece) > 0:
                    best[i] = best[j] + [piece]
                    break
        parts = best[len(s)]
        return parts if parts and len(parts) >= 2 else None

    def explain(s: str) -> bool:
        # (0) an exact twin freed up by an earlier peel
        if surplus[s] > 0:
            surplus[s] -= 1
            pdf[pdf.index(s)] = ""
            return True
        # (a) split into consecutive surplus tokens
        for i, t in enumerate(pdf):
            if not usable(t) or t == s or not s.startswith(t):
                continue
            acc, j = t, i
            while len(acc) < len(s) and j + 1 < len(pdf) and usable(pdf[j + 1]) and s.startswith(acc + pdf[j + 1]):
                j += 1
                acc += pdf[j]
            if acc == s:
                for k in range(i, j + 1):
                    surplus[pdf[k]] -= 1
                    pdf[k] = ""
                split.append(s)
                return True
        # (b) glued: prefix or suffix of a surplus token; the remainder stays available
        for i, t in enumerate(pdf):
            if usable(t) and t != s and (t.startswith(s) or t.endswith(s)):
                if t not in glued:
                    glued.append(t)
                rest = t[len(s):] if t.startswith(s) else t[:-len(s)]
                surplus[t] -= 1
                pdf[i] = rest
                if rest:
                    surplus[rest] += 1
                    if t.startswith(s):
                        before[i] = s[-1]
                    else:
                        after[i] = s[0]
                return True
        # (c) split into non-adjacent surplus tokens
        parts = decompose(s)
        if parts:
            for piece in parts:
                surplus[piece] -= 1
                pdf[pdf.index(piece)] = ""
            split.append(s)
            return True
        # (d) glued with one letter shared with a neighbour already peeled off (see the
        # docstring): "EIUSMODUI" minus "EIUSMOD" leaves "UI", which explains "DUI"
        for i, t in enumerate(pdf):
            if not usable(t) or len(s) < 2:
                continue
            at_start = before.get(i) == s[0] and t.startswith(s[1:])
            if not at_start and not (after.get(i) == s[-1] and t.endswith(s[:-1])):
                continue
            # t is a remainder, so its glued original is already reported
            rest = t[len(s) - 1:] if at_start else t[:len(t) - len(s) + 1]
            surplus[t] -= 1
            pdf[i] = rest
            if rest:
                surplus[rest] += 1
                if at_start:
                    before[i] = s[-1]
                else:
                    after[i] = s[0]
            return True
        return False

    # Peeling leaves remainders that other entries may need: iterate to a fixpoint.
    pending = need
    while True:
        missing = [s for s in pending if not explain(s)]
        if not missing or len(missing) == len(pending):
            break
        pending = missing

    extra = list((+surplus).elements())
    return {"missing": missing, "extra": extra, "glued": glued, "split": split}


def _fmt(words: list[str], limit: int = 40) -> str:
    return ", ".join(f"«{w}»" for w in words[:limit]) + (" …" if len(words) > limit else "")


def check_pdf_text(source_text: str, pdf: Path) -> CheckResult:
    text = pdf_text(pdf)
    if "\ufffd" in text:
        bad = text.count("\ufffd")
        return CheckResult("text_in_pdf", FAIL, f"PDF text contains {bad} replacement character(s) (U+FFFD): glyphs are missing",
                           data={"replacement_chars": bad})
    src, out = tokens(source_text, join_hyphens=True), tokens(text, join_hyphens=True)
    if src == out:
        return CheckResult("text_in_pdf", PASS, f"{len(src)} tokens identical between source and PDF", data={"tokens": len(src)})
    r = reconcile(src, out)
    if not r["missing"] and not r["extra"]:
        if not r["glued"] and not r["split"]:
            return CheckResult("text_in_pdf", PASS,
                               f"all {len(src)} tokens present in PDF (order differs only where PDF reading order differs, e.g. tables)",
                               data={"tokens": len(src), "reordered": True})
        # Every word is there; the text layer only glued neighbours or split a word at a line
        # break. Text running past a cell edge is a layout problem, reported by text_in_styled_pdf.
        rejoined = len(r["glued"]) + len(r["split"])
        return CheckResult("text_in_pdf", PASS,
                           f"all {len(src)} tokens present in PDF ({rejoined} rejoined across line breaks or cells)",
                           details=(["glued in the text layer: " + _fmt(r["glued"])] if r["glued"] else [])
                           + (["split at a line end: " + _fmt(r["split"])] if r["split"] else []),
                           data={"tokens": len(src), "glued": r["glued"][:200], "split": r["split"][:200]})
    # Word-level (punctuation stripped) comparison decides FAIL vs WARN.
    src_w = word_tokens(source_text, join_hyphens=True)
    out_w = word_tokens(text, join_hyphens=True)
    rw = reconcile(src_w, out_w)
    status = FAIL if (rw["missing"] or rw["extra"]) else WARN
    details = []
    if r["missing"]:
        details.append("missing from PDF: " + _fmt(r["missing"]))
    if r["extra"]:
        details.append("extra in PDF: " + _fmt(r["extra"]))
    if r["glued"]:
        details.append("glued (cell overflow?): " + _fmt(r["glued"], 20))
    details.extend(diff_hunks(src, out, max_hunks=10))
    summary = (f"{len(rw['missing'])} word(s) missing and {len(rw['extra'])} extra in the PDF" if status == FAIL
               else f"words all present; {len(r['missing']) + len(r['extra'])} punctuation-only difference(s)")
    return CheckResult("text_in_pdf", status, summary, details=details,
                       data={"missing": r["missing"][:200], "extra": r["extra"][:200], "tokens_source": len(src), "tokens_pdf": len(out)})


def _chrome_tolerant(text: str) -> list[str]:
    """Word tokens with leading/trailing digits removed: footnote markers and
    heading numbers glue themselves to neighbouring words in pdftotext output
    (e.g. "footnote.¹" -> "footnote1"). Applied to both sides; numbers are
    checked strictly by the bare-render check instead."""
    return [w for w in (t.strip("0123456789") for t in word_tokens(text, join_hyphens=True)) if w]


def _present_somewhere(word: str, pdf_tokens: list[str], pdf_set: set[str]) -> bool:
    """Lenient presence: exact token, inside a glued token, or a concatenation of tokens."""
    if word in pdf_set:
        return True
    if any(word in t for t in pdf_set if len(t) > len(word)):
        return True
    ok = {0}
    for i in range(1, len(word) + 1):
        for j in range(max(0, i - 60), i):
            if j in ok and word[j:i] in pdf_set:
                ok.add(i)
                break
    return len(word) in ok


def check_pdf_superset(source_text: str, pdf: Path) -> CheckResult:
    """Every source word must appear somewhere in the styled PDF.

    The styled PDF carries chrome (running title, page numbers, TOC),
    hyphenation and the same extraction artefacts as the bare render, so this
    check is informational (PASS/WARN only): a word counts as present when it
    is found as a token, inside a glued token, or as a concatenation of tokens.
    The strict comparison is text_in_pdf on the bare render."""
    text = pdf_text(pdf)
    src, out = _chrome_tolerant(source_text), _chrome_tolerant(text)
    r = reconcile(src, out)
    pdf_set = set(out)
    absent = [w for w in r["missing"] if not _present_somewhere(w, out, pdf_set)]
    if absent:
        # Hyphenation plus cell overflow can hide a word from the text layer even
        # though it is printed; the bare render (text_in_pdf) is authoritative, so
        # this stays a warning that names the words to eyeball.
        return CheckResult("text_in_styled_pdf", WARN,
                           f"{len(absent)} source word(s) could not be located in the styled PDF text layer "
                           "(hyphenated inside an overflowing cell?); text_in_pdf is authoritative",
                           details=["not located: " + _fmt(absent, 60)], data={"missing": absent[:200]})
    # A "glued" token that is itself a source word is a word rebuilt from hyphenated pieces.
    glued = [g for g in r["glued"] if g not in set(src)]
    if glued:
        # Two words with no gap between them in the styled PDF: a word ran past the edge of its
        # table cell into the next one (or the spacing is too tight to tell them apart).
        spots = _spots_of(pdf, glued)
        pages: dict[str, list[int]] = {}
        for sp in spots:
            if sp["page"] not in pages.setdefault(sp["word"], []):
                pages[sp["word"]].append(sp["page"])
        details = [f"«{w}» on page {', '.join(map(str, pages[w]))}" if pages.get(w) else f"«{w}»"
                   for w in glued[:40]]
        where = sorted({sp["page"] for sp in spots})
        return CheckResult("text_in_styled_pdf", WARN,
                           f"every word present, but {len(glued)} place(s) where text touches the next word, "
                           "usually a word too long for its table column"
                           + (f" (page {', '.join(map(str, where[:12]))}{' …' if len(where) > 12 else ''})" if where else ""),
                           details=details, data={"glued": glued[:200], "pages": where, "spots": spots[:200]})
    return CheckResult("text_in_styled_pdf", PASS, "every source word is present in the styled PDF", data={"words": len(src)})


_BBOX_WORD_RE = re.compile(r'<word xMin="([\d.]+)" yMin="([\d.]+)" xMax="([\d.]+)" yMax="([\d.]+)">([^<]*)</word>')


def _spots_of(pdf: Path, words: list[str]) -> list[dict[str, Any]]:
    """Where each (normalized) glued word is: page and box in PDF points from the top left.
    pdftotext's word boxes split words more finely than its reading-order text, so a glued
    word may be up to three consecutive boxes."""
    tools = require_tools("pdftotext")
    r = run([tools["pdftotext"], "-enc", "UTF-8", "-bbox", pdf, "-"])
    want = set(words)
    out: list[dict[str, Any]] = []
    for page, chunk in enumerate(r.stdout.split("<page ")[1:], start=1):
        boxes = []
        for m in _BBOX_WORD_RE.finditer(chunk):
            x0, y0, x1, y1 = (float(v) for v in m.groups()[:4])
            boxes += [(t, x0, y0, x1, y1) for t in _chrome_tolerant(html.unescape(m.group(5)))]
        for i in range(len(boxes)):
            for n in (1, 2, 3):
                part = boxes[i:i + n]
                w = "".join(b[0] for b in part)
                if len(part) == n and w in want:
                    x0, y0 = min(b[1] for b in part), min(b[2] for b in part)
                    x1, y1 = max(b[3] for b in part), max(b[4] for b in part)
                    out.append({"word": w, "page": page, "x": round(x0, 1), "y": round(y0, 1),
                                "w": round(x1 - x0, 1), "h": round(y1 - y0, 1)})
    return out


# ------------------------------------------------------ render-level checks
def pdf_image_count(pdf: Path) -> int:
    tools = require_tools("pdfimages")
    r = run([tools["pdfimages"], "-list", pdf])
    n = 0
    for line in r.stdout.splitlines()[2:]:
        parts = line.split()
        if len(parts) > 2 and parts[2] == "image":
            n += 1
    return n


def check_images(expected: int, pdf: Path) -> CheckResult:
    if expected == 0:
        return CheckResult("images", SKIP, "source has no images")
    got = pdf_image_count(pdf)
    status = PASS if got >= expected else FAIL
    return CheckResult("images", status, f"{got} image(s) in PDF, {expected} in source", data={"pdf": got, "source": expected})


_PDFFONTS_RE = re.compile(
    r"^(?P<name>.+?)\s+(?P<type>Type 1C?|Type 3|TrueType|CID Type 0C?|CID TrueType|[A-Za-z0-9 ]+?)\s+"
    r"(?P<encoding>\S+)\s+(?P<emb>yes|no)\s+(?P<sub>yes|no)\s+(?P<uni>yes|no)\s+\d+\s+\d+$"
)


def pdf_fonts(pdf: Path) -> list[dict[str, str]]:
    tools = require_tools("pdffonts")
    r = run([tools["pdffonts"], pdf])
    rows = []
    for line in r.stdout.splitlines()[2:]:
        m = _PDFFONTS_RE.match(line.strip())
        if m:
            rows.append(m.groupdict())
    return rows


def _font_family(pdf_name: str) -> str:
    # "ABCDEF+SourceSans3-Bold-Identity-H" -> "SourceSans3"
    name = pdf_name.split("+", 1)[-1]
    return name.split("-", 1)[0].split(",", 1)[0]


def check_fonts(pdf: Path, typst_warnings: list[str], theme_fonts: list[str]) -> CheckResult:
    problems = [w for w in typst_warnings if "unknown font family" in w]
    rows = pdf_fonts(pdf)
    not_embedded = [r["name"] for r in rows if r["emb"] != "yes"]
    if not_embedded:
        problems.append("fonts not embedded: " + ", ".join(not_embedded))
    wanted = {f.replace(" ", "").lower() for f in theme_fonts if f}
    # PDF font names drop spaces and may carry a width suffix ("SourceSans3Cd" for "Source Sans 3"):
    # a family counts as a theme font when either normalized name is a prefix of the other.
    def is_theme_font(family: str) -> bool:
        fam = family.replace(" ", "").lower()
        return any(fam.startswith(w) or w.startswith(fam) for w in wanted)
    fallback = sorted({_font_family(r["name"]) for r in rows if not is_theme_font(_font_family(r["name"]))})
    if problems:
        return CheckResult("fonts", FAIL, "; ".join(problems), details=problems, data={"fonts": [r["name"] for r in rows]})
    if fallback:
        return CheckResult("fonts", WARN,
                           f"fallback font(s) used for glyphs missing in the theme fonts: {', '.join(fallback)}",
                           details=["Characters such as arrows or symbols are not in the theme fonts; Typst borrowed them "
                                    "from: " + ", ".join(fallback) + ". Add a font that has them to template/assets/fonts "
                                    "and list it in [fonts] to control the look."],
                           data={"fonts": [r["name"] for r in rows], "fallback": fallback})
    return CheckResult("fonts", PASS, "all fonts embedded, no fallback, no font warnings",
                       data={"fonts": [r["name"] for r in rows]})


def check_typst_warnings(warnings: list[str]) -> CheckResult:
    other = [w for w in warnings if "unknown font family" not in w]
    if not other:
        return CheckResult("typst_warnings", PASS, "no compiler warnings")
    return CheckResult("typst_warnings", WARN, f"{len(other)} compiler warning(s)", details=other)
