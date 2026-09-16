"""Produce a lorem-ipsum twin of a Word document: same structure and design, no real data.

The twin is built at the OOXML level so that everything the pipeline derives from it
(Typst content, PDF, reports, extracted images) is derived from fake data only:

  * every text node in every XML part becomes lorem ipsum, word by word, keeping each
    word's length and capitalisation so line breaks and table widths stay similar
  * digits are randomised (a typed heading number at the start of a heading is kept)
  * every image is replaced by a flat placeholder of the same pixel size
  * parts that can carry data but not design are removed: custom XML, custom
    properties, thumbnails, embedded objects, macros, ActiveX, people, alt chunks
  * external link targets, alt texts, authors, bookmarks, field arguments, content
    control names and core/app properties are replaced

`verify_anonymized` then re-reads both packages and refuses the twin if any word from
the original survives anywhere (text nodes, sensitive attributes, link targets, file
names), if any binary part other than our own placeholders remains, or if an embedded
font is not byte-identical to a font already in the source (so a font cannot be added or
edited to smuggle data through). Intrinsic font metadata carried over unchanged from the
source is out of scope: fonts are shared design assets, not this document's content.
"""
from __future__ import annotations

import hashlib
import io
import posixpath
import re
import secrets
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from random import Random
from xml.sax.saxutils import escape, unescape

from PIL import Image, ImageDraw

LOREM_VOCAB = sorted(set("""
lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod tempor incididunt ut labore et
dolore magna aliqua enim ad minim veniam quis nostrud exercitation ullamco laboris nisi aliquip ex ea
commodo consequat duis aute irure in reprehenderit voluptate velit esse cillum fugiat nulla pariatur
excepteur sint occaecat cupidatat non proident sunt culpa qui officia deserunt mollit anim id est
laborum vestibulum pellentesque sollicitudin condimentum ullamcorper suspendisse scelerisque vivamus
fermentum curabitur phasellus tincidunt hendrerit malesuada ultricies efficitur facilisis praesent
maecenas aenean integer vehicula porttitor elementum dignissim venenatis pulvinar faucibus interdum
rhoncus sagittis ornare blandit lacinia luctus mattis varius cursus gravida auctor tortor risus purus
urna arcu eros nibh quam diam odio leo mi a ac eu ne at nec vel per sem nam eget vitae justo mauris
felis metus massa augue lectus ante ligula nunc orci dui dapibus placerat convallis imperdiet posuere
fringilla vulputate sodales suscipit pharetra tristique volutpat accumsan bibendum molestie lobortis
consequatur donec etiam morbi fusce proin cras nullam quisque aliquam habitant senectus netus fames
turpis egestas primis cubilia curae sociosqu litora torquent conubia nostra inceptos himenaeos
""".split()))
_BY_LEN: dict[int, list[str]] = {}
for _w in LOREM_VOCAB:
    _BY_LEN.setdefault(len(_w), []).append(_w)
VOCAB_SET = set(LOREM_VOCAB)

FIELD_KEYWORDS = set("""
ADDRESSBLOCK ADVANCE ASK AUTHOR AUTONUM AUTONUMLGL AUTONUMOUT AUTOTEXT AUTOTEXTLIST BARCODE BIBLIOGRAPHY
CITATION COMMENTS COMPARE CREATEDATE DATABASE DATE DOCPROPERTY DOCVARIABLE EDITTIME EQ FILENAME FILESIZE
FILLIN GOTOBUTTON GREETINGLINE HYPERLINK IF INCLUDEPICTURE INCLUDETEXT INDEX INFO KEYWORDS LASTSAVEDBY LINK
LISTNUM MACROBUTTON MERGEFIELD MERGEREC MERGESEQ NEXT NEXTIF NOTEREF NUMCHARS NUMPAGES NUMWORDS PAGE PAGEREF
PRINT PRINTDATE PRIVATE QUOTE RD REF REVNUM SAVEDATE SECTION SECTIONPAGES SEQ SET SKIPIF STYLEREF SUBJECT
SYMBOL TA TC TEMPLATE TIME TITLE TOA TOC USERADDRESS USERINITIALS USERNAME XE MERGEFORMAT CHARFORMAT ARABIC
ROMAN ALPHABETIC UPPER LOWER FIRSTCAP CAPS ORDINAL CARDTEXT DOLLARTEXT HEX ORDTEXT
""".split())
SAFE_WORDS = {"anonymous", "anonymized", "image", "example", "com", "https", "http", "sheet"} | {k.lower() for k in FIELD_KEYWORDS}
TEXT_KEEP_ELEMENTS = {"formatCode", "created", "modified"}   # chart number formats and package timestamps: structure, not data
STRUCTURAL_BOOKMARK = re.compile(r"^_(Toc|Ref|Hlk|GoBack)", re.I)
HEADING_NUMBER = re.compile(r"^\d{1,2}(\.\d{1,2}){0,3}$")
ALLOWED_BINARY = {".png", ".odttf", ".ttf", ".otf"}
FONT_EXTS = {".odttf", ".ttf", ".otf"}   # kept verbatim for design; must be identical to a font already in the source
KEEP_TEXT_EXTS = {".xml", ".rels"}

_XML_TOKEN = re.compile(r"<!--.*?-->|<!\[CDATA\[.*?\]\]>|<\?.*?\?>|<[^>]*>|[^<]+", re.S)
_TAG_NAME = re.compile(r"<(/?)([\w.:-]+)")
_ATTR = re.compile(r"""(\s)([\w.:-]+)=(["'])(.*?)\3""", re.S)
_WORD_PARTS = re.compile(r"[^\W\d_]+|\d+|[\W_]+")
_TOKENS = re.compile(r"[^\W\d_]+|\d+")  # letter runs and digit runs separately, like the replacement
_QUOTED = re.compile(r'"([^"]*)"')
_STYLE_DEF = re.compile(r'<w:style\b[^>]*w:styleId="([^"]+)"[^>]*>.*?<w:name w:val="([^"]*)"', re.S)


class AnonymizeError(RuntimeError):
    pass


# ------------------------------------------------------------------ words
def lorem_word(length: int, rng: Random) -> str:
    if length <= 0:
        return ""
    if length in _BY_LEN:
        return rng.choice(_BY_LEN[length])
    out = ""
    while len(out) < length:
        out += rng.choice(LOREM_VOCAB)
    return out[:length]


def lorem_like(text: str, rng: Random) -> str:
    """Replace every letter run with a lorem word of the same length and case shape and
    every digit run with random digits; punctuation and whitespace are kept."""
    parts = []
    for m in _WORD_PARTS.finditer(text):
        s = m.group(0)
        if s[0].isdigit():
            digits = "".join(str(rng.randrange(10)) for _ in s)
            if len(s) > 1 and s[0] != "0" and digits[0] == "0":
                digits = str(rng.randrange(1, 10)) + digits[1:]
            parts.append(digits)
        elif s[0].isalpha():
            w = lorem_word(len(s), rng)
            if s.isupper() and len(s) > 1:
                w = w.upper()
            elif s[0].isupper():
                w = w[0].upper() + w[1:]
            parts.append(w)
        else:
            parts.append(s)
    return "".join(parts)


def _field_code(instr: str, rng: Random, bookmarks: "dict[str, str]") -> str:
    """Keep the field keyword and switches, replace every argument."""
    def quoted(m: re.Match) -> str:
        inner = m.group(1)
        if re.match(r"^\s*(https?:|ftp:|mailto:|file:|www\.|\\\\)", inner, re.I):
            return f'"https://example.com/{lorem_word(6, rng)}"'
        return f'"{lorem_like(inner, rng)}"'
    instr = _QUOTED.sub(quoted, instr)
    out = []
    for tok in re.split(r"(\s+)", instr):
        if not tok or tok.isspace() or tok.startswith("\\") or tok.startswith('"') or tok.upper() in FIELD_KEYWORDS:
            out.append(tok)
        elif tok.startswith("_") and STRUCTURAL_BOOKMARK.match(tok):
            out.append(tok)
        elif tok in bookmarks:
            out.append(bookmarks[tok])
        else:
            out.append(lorem_like(tok, rng))
    return "".join(out)


# ------------------------------------------------------------------ XML rewriting
@dataclass
class _Ctx:
    rng: Random
    heading_styles: set[str]
    bookmarks: dict[str, str]
    salt: str
    image_counter: int = 0
    stack: list[str] = field(default_factory=list)
    in_heading: bool = False
    first_word_pending: bool = False

    def bookmark(self, name: str) -> str:
        if not name or STRUCTURAL_BOOKMARK.match(name):
            return name
        if name not in self.bookmarks:
            self.bookmarks[name] = "bm" + hashlib.sha256((self.salt + name).encode()).hexdigest()[:10]
        return self.bookmarks[name]


def _local(name: str) -> str:
    return name.rsplit(":", 1)[-1]


def _rewrite_attrs(tag: str, ctx: _Ctx) -> str:
    m = _TAG_NAME.match(tag)
    if not m:
        return tag
    elem = _local(m.group(2))

    def repl(am: re.Match) -> str:
        space, name, q, val = am.group(1), am.group(2), am.group(3), am.group(4)
        local = _local(name)
        new = None
        if local in ("author", "initials"):
            new = "Anonymous" if local == "author" else "AN"
        elif local == "date" and elem in ("ins", "del", "comment", "rPrChange", "pPrChange", "tblPrChange", "trPrChange",
                                          "tcPrChange", "sectPrChange", "numberingChange", "cellIns", "cellDel", "moveFrom", "moveTo"):
            new = "2000-01-01T00:00:00Z"
        elif elem in ("docPr", "cNvPr") and local in ("name", "descr", "title"):
            if local == "name":
                ctx.image_counter += 1
                new = f"Image {ctx.image_counter}"
            else:
                new = lorem_like(unescape(val), ctx.rng) if val.strip() else val
        elif elem == "bookmarkStart" and local == "name":
            new = ctx.bookmark(unescape(val))
        elif elem == "hyperlink" and local == "anchor":
            new = ctx.bookmark(unescape(val))
        elif local == "tooltip":
            new = lorem_like(unescape(val), ctx.rng)
        elif elem in ("alias", "tag", "tblCaption", "tblDescription") and local == "val":
            new = lorem_like(unescape(val), ctx.rng)
        elif elem == "listItem" and local in ("displayText", "value"):
            new = lorem_like(unescape(val), ctx.rng)
        elif elem == "fldSimple" and local == "instr":
            new = _field_code(unescape(val), ctx.rng, ctx.bookmarks)
        if new is None:
            return am.group(0)
        return f"{space}{name}={q}{escape(new, {q: '&quot;' if q == '"' else '&apos;'})}{q}"

    return _ATTR.sub(repl, tag)


def _rewrite_text(text: str, ctx: _Ctx) -> str:
    parent = ctx.stack[-1] if ctx.stack else ""
    if parent in TEXT_KEEP_ELEMENTS:
        return text
    raw = unescape(text)
    if parent == "instrText":
        return escape(_field_code(raw, ctx.rng, ctx.bookmarks))
    if parent in ("t", "delText") and ctx.first_word_pending:
        m = re.search(r"[^\W_]+", raw)
        if m:
            ctx.first_word_pending = False
            if ctx.in_heading and HEADING_NUMBER.match(raw[m.start():].split()[0] if raw[m.start():].split() else ""):
                first = raw[m.start():].split()[0]
                head = raw[: m.start() + len(first)]
                return escape(head + lorem_like(raw[m.start() + len(first):], ctx.rng))
    return escape(lorem_like(raw, ctx.rng))


def rewrite_xml(xml: str, ctx: _Ctx) -> str:
    out: list[str] = []
    for m in _XML_TOKEN.finditer(xml):
        tok = m.group(0)
        if tok.startswith("<!--"):
            continue  # comments may carry anything; they are never part of the document
        if tok.startswith("<![CDATA["):
            out.append("<![CDATA[" + lorem_like(tok[9:-3], ctx.rng) + "]]>")
            continue
        if tok.startswith("<?"):
            out.append(tok)
            continue
        if tok.startswith("<"):
            tm = _TAG_NAME.match(tok)
            if not tm:
                out.append(tok)
                continue
            closing, name = tm.group(1) == "/", tm.group(2)
            local = _local(name)
            self_closing = tok.endswith("/>")
            if closing:
                if ctx.stack and ctx.stack[-1] == local:
                    ctx.stack.pop()
                out.append(tok)
                continue
            if local == "p":
                ctx.in_heading, ctx.first_word_pending = False, True
            elif local == "pStyle" and "pPr" in ctx.stack and "p" in ctx.stack:
                am = re.search(r'w:val=(["\'])(.*?)\1', tok)
                if am and unescape(am.group(2)) in ctx.heading_styles:
                    ctx.in_heading = True
            elif local == "outlineLvl" and ctx.stack and ctx.stack[-1] == "pPr":
                ctx.in_heading = True
            out.append(_rewrite_attrs(tok, ctx))
            if not self_closing:
                ctx.stack.append(local)
            continue
        if tok.strip():
            out.append(_rewrite_text(tok, ctx))
        else:
            out.append(tok)
    return "".join(out)


# ------------------------------------------------------------------ package handling
def _owner_of_rels(part: str) -> str:
    d, base = posixpath.split(part)
    if base == ".rels" and d == "_rels":
        return ""
    assert d.endswith("_rels")
    return posixpath.normpath(posixpath.join(d[: -len("_rels")], base[: -len(".rels")]))


def _resolve(owner: str, target: str) -> str:
    if target.startswith("/"):
        return posixpath.normpath(target[1:])
    return posixpath.normpath(posixpath.join(posixpath.dirname(owner), target))


def _ext(name: str) -> str:
    """Extension of a package part; `_rels/.rels` is a .rels file, not a dotfile."""
    base = posixpath.basename(name).lower()
    return ".rels" if base.endswith(".rels") else posixpath.splitext(base)[1]


def _is_dropped(name: str) -> bool:
    lower = name.lower()
    if lower.startswith(("customxml/", "word/embeddings/", "word/activex/", "docprops/thumbnail")):
        return True
    if lower in ("docprops/custom.xml", "word/vbadata.xml", "word/people.xml"):
        return True
    if "/media/" in lower or lower.startswith("media/"):
        return False  # replaced, not dropped
    ext = _ext(lower)
    if ext in KEEP_TEXT_EXTS:
        return "afchunk" in lower or "/afchunks/" in lower
    return ext not in ALLOWED_BINARY


def placeholder_png(width: int, height: int) -> bytes:
    img = Image.new("RGB", (max(1, width), max(1, height)), (225, 225, 225))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, width - 1, height - 1], outline=(175, 175, 175), width=max(1, min(width, height) // 120))
    d.line([0, 0, width - 1, height - 1], fill=(200, 200, 200), width=max(1, min(width, height) // 160))
    d.line([0, height - 1, width - 1, 0], fill=(200, 200, 200), width=max(1, min(width, height) // 160))
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _image_size(data: bytes) -> tuple[int, int]:
    try:
        with Image.open(io.BytesIO(data)) as im:
            w, h = im.size
            return (min(w, 4000), min(h, 4000))
    except Exception:  # noqa: BLE001  (emf, wmf, svg, broken)
        return (800, 600)


@dataclass
class AnonymizeResult:
    output: Path
    placeholders: dict[str, str]        # new media path -> sha256 of the bytes we wrote
    dropped_parts: list[str]
    replaced_parts: list[str]


def anonymize_docx(src: Path, dst: Path, *, heading_style_names: tuple[str, ...] = (), seed: int | None = None) -> AnonymizeResult:
    rng = Random(seed if seed is not None else secrets.randbits(64))
    salt = secrets.token_hex(8)
    with zipfile.ZipFile(src) as z:
        names = z.namelist()
        try:
            styles = z.read("word/styles.xml").decode("utf-8", "replace")
        except KeyError:
            styles = ""
        wanted = {n.lower() for n in heading_style_names}
        heading_ids = {sid for sid, nm in _STYLE_DEF.findall(styles)
                       if nm.lower().startswith(("heading", "titre", "berschrift", "title")) or nm.lower() in wanted}
        ctx = _Ctx(rng=rng, heading_styles=heading_ids, bookmarks={}, salt=salt)

        dropped = {n for n in names if _is_dropped(n)}
        for n in names:  # rels of dropped parts go too
            if n.endswith(".rels") and "_rels/" in n and n not in dropped:
                try:
                    if _owner_of_rels(n) in dropped:
                        dropped.add(n)
                except AssertionError:
                    pass
        media_map: dict[str, tuple[str, bytes]] = {}
        placeholders: dict[str, str] = {}
        media_index = 0
        for n in names:
            if n in dropped or not ("/media/" in n.lower() or n.lower().startswith("media/")):
                continue
            media_index += 1
            new_name = posixpath.join(posixpath.dirname(n), f"image{media_index}.png")
            data = placeholder_png(*_image_size(z.read(n)))
            media_map[n] = (new_name, data)
            placeholders[new_name] = hashlib.sha256(data).hexdigest()

        # bookmark names must be mapped before hyperlinks/fields reference them: pre-scan
        for n in names:
            if n.endswith(".xml") and n not in dropped:
                for bm in re.findall(r'<w:bookmarkStart\b[^>]*w:name=(["\'])(.*?)\1', z.read(n).decode("utf-8", "replace")):
                    ctx.bookmark(unescape(bm[1]))

        replaced: list[str] = []
        dst.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as out:
            for item in z.infolist():
                n = item.filename
                if n in dropped:
                    continue
                if n in media_map:
                    new_name, data = media_map[n]
                    out.writestr(new_name, data)
                    continue
                data = z.read(n)
                lower = n.lower()
                if lower == "[content_types].xml":
                    xml = data.decode("utf-8")
                    xml = re.sub(r'<Override\b[^>]*PartName="([^"]+)"[^>]*/>',
                                 lambda m: "" if m.group(1).lstrip("/") in dropped else m.group(0), xml)
                    xml = xml.replace("wordprocessingml.document.macroEnabled.main+xml", "wordprocessingml.document.main+xml")
                    xml = xml.replace("wordprocessingml.template.macroEnabledTemplate.main+xml", "wordprocessingml.template.main+xml")
                    if 'Extension="png"' not in xml:
                        xml = xml.replace("<Default ", '<Default Extension="png" ContentType="image/png"/><Default ', 1)
                    data = xml.encode("utf-8")
                elif lower == "docprops/core.xml":
                    title = re.search(r"<dc:title>(.*?)</dc:title>", data.decode("utf-8", "replace"), re.S)
                    t = lorem_like(unescape(title.group(1)), rng) if title and title.group(1).strip() else ""
                    data = (
                        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
                        'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
                        'xmlns:dcmitype="http://purl.org/dc/dcmitype/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
                        f"<dc:title>{escape(t)}</dc:title><dc:creator>Anonymous</dc:creator><cp:lastModifiedBy>Anonymous</cp:lastModifiedBy>"
                        '<dcterms:created xsi:type="dcterms:W3CDTF">2000-01-01T00:00:00Z</dcterms:created>'
                        '<dcterms:modified xsi:type="dcterms:W3CDTF">2000-01-01T00:00:00Z</dcterms:modified></cp:coreProperties>'
                    ).encode("utf-8")
                elif lower == "docprops/app.xml":
                    data = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                            '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" '
                            'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
                            "<Application>Anonymized</Application></Properties>").encode("utf-8")
                elif lower.endswith(".rels"):
                    owner = _owner_of_rels(n)
                    xml = data.decode("utf-8")
                    counter = 0

                    def rel(m: re.Match) -> str:
                        nonlocal counter
                        tag = m.group(0)
                        tm = re.search(r'Target=(["\'])(.*?)\1', tag)
                        if not tm:
                            return tag
                        target = unescape(tm.group(2))
                        if re.search(r'TargetMode=(["\'])External\1', tag):
                            counter += 1
                            return tag.replace(tm.group(0), f'Target="https://example.com/{counter}"')
                        resolved = _resolve(owner, target)
                        if resolved in dropped:
                            return ""
                        if resolved in media_map:
                            new_rel = posixpath.relpath(media_map[resolved][0], posixpath.dirname(owner) or ".")
                            return tag.replace(tm.group(0), f'Target="{new_rel}"')
                        return tag
                    data = re.sub(r"<Relationship\b[^>]*/>", rel, xml).encode("utf-8")
                elif lower.endswith(".xml"):
                    xml = data.decode("utf-8")
                    if lower == "word/settings.xml":
                        xml = re.sub(r"<w:docVars>.*?</w:docVars>|<w:mailMerge>.*?</w:mailMerge>|<w:attachedTemplate\b[^>]*/>", "", xml, flags=re.S)
                    if lower == "word/document.xml":
                        xml = re.sub(r"<w:altChunk\b[^>]*/>|<w:altChunk\b.*?</w:altChunk>", "", xml, flags=re.S)
                    if "/charts/" in lower:
                        xml = re.sub(r"<c:externalData\b[^>]*/>|<c:externalData\b.*?</c:externalData>", "", xml, flags=re.S)
                    ctx.stack.clear()
                    ctx.in_heading, ctx.first_word_pending = False, False
                    xml = rewrite_xml(xml, ctx)
                    data = xml.encode("utf-8")
                    replaced.append(n)
                out.writestr(n, data)
    return AnonymizeResult(output=dst, placeholders=placeholders, dropped_parts=sorted(dropped), replaced_parts=replaced)


# ------------------------------------------------------------------ verification
_SENSITIVE_ATTRS = {("docPr", "name"), ("docPr", "descr"), ("docPr", "title"), ("cNvPr", "name"), ("cNvPr", "descr"),
                    ("cNvPr", "title"), ("bookmarkStart", "name"), ("hyperlink", "anchor"), ("hyperlink", "tooltip"),
                    ("alias", "val"), ("tag", "val"), ("tblCaption", "val"), ("tblDescription", "val"),
                    ("listItem", "displayText"), ("listItem", "value"), ("fldSimple", "instr"), ("docVar", "name"),
                    ("docVar", "val"), ("attachedTemplate", "id")}


def _tokens(text: str) -> set[str]:
    # >= 3: construction (lorem_like) already replaces letter runs of every length, so short
    # words do not leak by omission. Comparing 1-2 char tokens only produces false positives,
    # because synthetic twin tokens (bookmark hashes like bm3be7f, the "AN" initials
    # placeholder) collide with common two-letter words in the original and would refuse a
    # clean twin. Coverage of short values in attributes is governed by _SENSITIVE_ATTRS, not
    # by this threshold.
    return {t.lower() for t in _TOKENS.findall(text) if len(t) >= 3}


def _collect(z: zipfile.ZipFile, *, numbers_from_text: bool) -> tuple[set[str], set[str]]:
    """Return (word tokens, digit tokens) of the content-bearing places of a package."""
    words: set[str] = set()
    digits: set[str] = set()

    def add(text: str, allow_numbers: bool = True) -> None:
        for t in _tokens(text):
            if t.isdigit():
                if allow_numbers:
                    digits.add(t)
            elif t not in VOCAB_SET and t not in SAFE_WORDS:
                words.add(t)

    for n in z.namelist():
        lower = n.lower()
        ext = _ext(lower)
        if ext not in KEEP_TEXT_EXTS:
            add(posixpath.splitext(posixpath.basename(n))[0])  # media file names can carry data; extensions cannot
            continue
        xml = z.read(n).decode("utf-8", "replace")
        stack: list[str] = []
        for m in _XML_TOKEN.finditer(xml):
            tok = m.group(0)
            if tok.startswith("<!--"):
                add(tok)
            elif tok.startswith("<![CDATA["):
                add(tok[9:-3])
            elif tok.startswith("<?"):
                continue
            elif tok.startswith("<"):
                tm = _TAG_NAME.match(tok)
                if not tm:
                    continue
                local = _local(tm.group(2))
                if tm.group(1) == "/":
                    if stack and stack[-1] == local:
                        stack.pop()
                    continue
                for am in _ATTR.finditer(tok):
                    aname, val = _local(am.group(2)), unescape(am.group(4))
                    if (local, aname) in _SENSITIVE_ATTRS or aname in ("author", "initials"):
                        if local == "bookmarkStart" and STRUCTURAL_BOOKMARK.match(val):
                            continue
                        if local == "hyperlink" and aname == "anchor" and STRUCTURAL_BOOKMARK.match(val):
                            continue
                        add(val)
                    elif ext == ".rels" and aname == "Target" and re.search(r'TargetMode=(["\'])External\1', tok):
                        add(val)
                    elif lower == "docprops/custom.xml" and aname == "name":
                        add(val)  # custom property names are user data; values are text nodes
                    elif lower == "word/people.xml" and not am.group(2).startswith("xmlns"):
                        add(val)
                if not tok.endswith("/>"):
                    stack.append(local)
            elif tok.strip():
                parent = stack[-1] if stack else ""
                if parent in TEXT_KEEP_ELEMENTS:
                    continue
                add(unescape(tok), allow_numbers=numbers_from_text)
    return words, digits


def verify_anonymized(original: Path, twin: Path, placeholders: dict[str, str]) -> dict[str, int]:
    """Raise AnonymizeError if any original word survives in the twin or a foreign binary remains."""
    with zipfile.ZipFile(original) as zo, zipfile.ZipFile(twin) as zt:
        orig_words, orig_digits = _collect(zo, numbers_from_text=True)
        twin_words, twin_digits = _collect(zt, numbers_from_text=False)  # text-node numbers are randomised by construction
        leaked_words = sorted(orig_words & twin_words)
        leaked_digits = sorted(orig_digits & twin_digits)
        orig_fonts = {hashlib.sha256(zo.read(n)).hexdigest() for n in zo.namelist() if _ext(n) in FONT_EXTS}
        foreign: list[str] = []
        for n in zt.namelist():
            ext = _ext(n)
            if ext in KEEP_TEXT_EXTS:
                continue
            if n in placeholders:
                if hashlib.sha256(zt.read(n)).hexdigest() != placeholders[n]:
                    foreign.append(n)
            elif ext not in ALLOWED_BINARY:
                foreign.append(n)
            elif ext in FONT_EXTS:
                if hashlib.sha256(zt.read(n)).hexdigest() not in orig_fonts:
                    foreign.append(n)  # a font not carried over verbatim from the source (added or modified)
            elif ext == ".png":
                foreign.append(n)  # a png we did not generate
    problems = []
    if leaked_words:
        problems.append(f"{len(leaked_words)} original word(s) survived: {', '.join(leaked_words[:15])}")
    if leaked_digits:
        problems.append(f"{len(leaked_digits)} original number(s) survived outside text: {', '.join(leaked_digits[:15])}")
    if foreign:
        problems.append(f"foreign binary part(s) remain: {', '.join(foreign[:10])}")
    if problems:
        raise AnonymizeError("; ".join(problems))
    return {"original_words": len(orig_words), "twin_words": len(twin_words), "placeholders": len(placeholders)}


def original_word_set(original: Path) -> set[str]:
    with zipfile.ZipFile(original) as zo:
        return _collect(zo, numbers_from_text=True)[0]


def leaked_into_text(text: str, orig_words: set[str]) -> list[str]:
    """Words of the original that appear in a derived text file (source.txt); empty when clean."""
    return sorted({t for t in _tokens(text) if not t.isdigit()} & orig_words)
