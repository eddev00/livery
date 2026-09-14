# wordeasy

Batch-restyle Word documents (`.docx`) through a single Typst template, with
safeguards that refuse to deliver a PDF whose content differs from the source.

```
docx ──pandoc + Lua filter──▶ content.typ ──┐
                                            ├─ main.typ ──typst──▶ <doc>.pdf
template/template.typ + template/theme.toml ┘
                    ▲
        verifier: source text vs content.typ vs PDF text
```

Per input file you get `out/<name>/`:

| File | Purpose |
|---|---|
| `<name>.pdf` | the final styled PDF |
| `content.typ` | the document's content as plain Typst markup, editable |
| `main.typ` | imports the template and includes `content.typ` |
| `media/` | images extracted from the docx |
| `report.md` / `report.json` | every integrity check with evidence |
| `source.txt` | the normalized source text the checks compare against |
| `wordeasy.json` | state needed by `wordeasy verify` |

## Install

Arch Linux:

```
sudo pacman -S typst pandoc-cli poppler     # typst ≥ 0.13, pandoc ≥ 3.6, pdftotext/pdfimages/pdffonts
uv sync
```

Without root: put the `typst` and `pandoc` release binaries into `.tools/bin/`;
they are picked up when the system ones are missing. LibreOffice is only needed
to convert legacy `.doc`/`.odt`/`.rtf` inputs.

## Use

```
uv run wordeasy build docs/*.docx                 # one PDF per file under out/
uv run wordeasy build a.docx --out /some/dir      # elsewhere
uv run wordeasy build a.docx --theme my-theme.toml
uv run wordeasy build a.docx --keep-going         # don't stop at the first FAIL
uv run wordeasy build a.docx --accept-changes     # accept Word tracked changes (see below)
uv run wordeasy verify out/a                      # recompile from (edited) .typ and re-check
uv run wordeasy watch out/a --open                # live preview: PDF recompiles on every save
uv run wordeasy tools                             # show detected tool versions
```

Exit codes: `0` every document PASS/WARN, `1` at least one FAIL, `2` tool or I/O error.

### Editing the Typst by hand

Edit `out/<name>/content.typ` (body) or `out/<name>/main.typ` (cover fields), then
run `wordeasy verify out/<name>`. It recompiles the PDF and re-runs every check
against the original source text, so a change to the words is reported as FAIL on
purpose while layout edits pass. `wordeasy watch out/<name> --open` gives a live
preview: the PDF recompiles whenever a `.typ` file or the theme is saved. A folder
can be moved anywhere; the template import path is fixed up automatically.

### Styling

Everything visual lives in `template/theme.toml` (fonts, colors, margins, chapter
page, table look, cover, TOC) and `template/template.typ` (the `set`/`show` rules).
`content.typ` never contains styling, so re-theming never touches content.
Custom Word paragraph styles are mapped to heading levels in `[style_map]`.

## Content safeguards

"Content" is every word, number, punctuation mark, table cell, footnote,
caption, list item and link text. Whitespace, empty paragraphs, fonts, colors,
page breaks and hard line breaks are presentation and may change.

| Check | Compares | FAIL when |
|---|---|---|
| `text_after_conversion` | source docx text vs `content.typ` read back by pandoc | any token differs, or pandoc's reader had to ignore an element |
| `structure_after_conversion` | heading/table/cell/image/list/footnote/link counts, every table cell text, link targets | any count or cell differs |
| `render` | | Typst fails to compile |
| `text_in_pdf` | source text vs `pdftotext` of a bare render (no cover/TOC/header/footer/numbering, footnotes inline, hyphenation and ligatures off, wide word spacing) | any word missing or added; order differences alone (tables, PDF reading order) pass. Words that pdftotext glued together (text overflowing a narrow cell) or split at a line end are WARN and listed, so the cells can be checked visually |
| `text_in_styled_pdf` | every source word present somewhere in the final PDF (lenient: chrome, hyphenation and extraction artefacts tolerated) | never; WARN lists words it could not locate, `text_in_pdf` is authoritative |
| `images` | image count in PDF vs source | fewer images |
| `fonts` | `pdffonts` + Typst warnings | a font is not embedded or unknown; WARN when a fallback font supplied glyphs the theme fonts lack (arrows, symbols) |
| `tracked_changes` | | the docx has unresolved tracked changes and `--accept-changes` was not given |
| `typst_warnings`, `pandoc_warnings` | | never; reported as WARN |

Punctuation-only differences are WARN. Diffs are reported as hunks with five
words of context on each side, in `report.md` and on the console.

Dropped by design and listed in the report: Word headers/footers (the template
provides its own), comments, and Word's own table of contents (paragraphs in
`toc N` styles plus the styles/heading texts listed under `[source]` in
`theme.toml`); the template regenerates the TOC. Document title/author/date go to the cover page.

## Development

```
uv run python tests/make_fixtures.py   # regenerate tests/fixtures/*.docx
uv run pytest
```

`tests/test_verify.py::test_tampered_content_fails` edits a generated
`content.typ` (one changed word, one deleted phrase, one edited table cell) and
asserts the verifier reports exactly those three hunks.
