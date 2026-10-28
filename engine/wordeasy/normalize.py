"""Text normalization shared by every content check.

The goal is to make two renderings of the *same words* compare equal while
keeping every difference in the words themselves visible.
"""
from __future__ import annotations

import re
import unicodedata

_QUOTES = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "…": "...",
}
_DROP = {"­", "​", "‌", "‍", "⁠", "﻿"}
# Any dash-like character is a token boundary: "well-known" and a hyphenated
# line break "well-\nknown" both become ["well", "known"].
_DASHES = "-‐‑‒–—―−"
# "/" is a line-break opportunity in Typst ("Table/Figure" may wrap after the
# slash), so it is a token boundary on both sides as well.
_SPLIT_RE = re.compile(rf"[\s/{re.escape(_DASHES)}]+")
_PUNCT_RE = re.compile(r"[^\w]+", re.UNICODE)


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = "".join(ch for ch in text if ch not in _DROP)
    for k, v in _QUOTES.items():
        text = text.replace(k, v)
    # Pandoc's plain/typst round-trip may emit -- and --- for dashes.
    text = text.replace("---", "—").replace("--", "–")
    return text


# pdftotext re-joins only ASCII-style hyphens broken at a line end; en/em dashes
# stay as they are. join_hyphens mirrors that: delete hyphens, split on dashes.
_HYPHENS = "-\u2010\u2011"
_HYPHEN_RE = re.compile(f"[{re.escape(_HYPHENS)}]")
_WS_DASH_RE = re.compile(rf"[\s/{re.escape(_DASHES.replace('-', '').replace(chr(0x2010), '').replace(chr(0x2011), ''))}]+")


def tokens(text: str, *, join_hyphens: bool = False) -> list[str]:
    """Split into tokens. Dashes are token boundaries, so "well-known" and a
    hyphenated line break "well-\nknown" agree. With join_hyphens=True dashes
    are deleted instead ("well-known" -> "wellknown"): needed when comparing
    against pdftotext output, which de-hyphenates line-end breaks itself."""
    text = normalize(text)
    if join_hyphens:
        return [t for t in _WS_DASH_RE.split(_HYPHEN_RE.sub("", text)) if t]
    return [t for t in _SPLIT_RE.split(text) if t]


def strip_punct(tok: str) -> str:
    return _PUNCT_RE.sub("", tok)


def word_tokens(text: str, *, join_hyphens: bool = False) -> list[str]:
    """Tokens with punctuation removed (empty tokens dropped)."""
    return [w for w in (strip_punct(t) for t in tokens(text, join_hyphens=join_hyphens)) if w]
