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
| `original.pdf` | the Word file as LibreOffice renders it, for side-by-side comparison |
| `lorem/` | the lorem ipsum twin: `lorem.docx` (anonymised copy), `lorem/original.pdf` (its LibreOffice render) and `lorem/lorem.pdf` (its styled build), plus its own report |
| `content.typ` | the document's content as plain Typst markup, editable |
| `main.typ` | imports the template and includes `content.typ` |
| `media/` | images extracted from the docx |
| `report.md` / `report.json` | every integrity check with evidence |
| `source.txt` | the normalized source text the checks compare against |
| `wordeasy.json` | state needed by `wordeasy verify` |

## Install

macOS: follow [docs/macos.md](docs/macos.md) (Homebrew, LibreOffice link, first run).

Arch Linux:

```
sudo pacman -S typst pandoc-cli poppler     # typst ≥ 0.13, pandoc ≥ 3.6, pdftotext/pdfimages/pdffonts
uv sync
```

Without root: put the `typst` and `pandoc` release binaries into `.tools/bin/`;
they are picked up when the system ones are missing. LibreOffice renders
`original.pdf` (the Word file as it looks in Word) and converts legacy
`.doc`/`.odt`/`.rtf`/`.docm` inputs; without it the build still succeeds and
reports the missing original as WARN.

## Where the Word files live

Live documents never sit inside this repository. The build and the UI take an output
root with `--out`; on the live side both point at a folder outside the checkout, on a
user or machine where no AI coding agent runs. Edge cases travel the other way as
lorem-ipsum twins under `tests/fixtures/edge-cases/`, which the test suite builds and
which the pre-commit hook (`git config core.hooksPath .githooks`) is the only place
it lets a document file be committed. The full procedure is in
[docs/live-side.md](docs/live-side.md).

## Use

```
uv run wordeasy build docs/*.docx                 # one PDF per file under out/
uv run wordeasy build a.docx --out /some/dir      # elsewhere
uv run wordeasy build a.docx --theme my-theme.toml
uv run wordeasy build a.docx --keep-going         # don't stop at the first FAIL
uv run wordeasy build a.docx --accept-changes     # accept Word tracked changes (see below)
uv run wordeasy build a.docx --cover large        # cover variant: "small" (default) or "large" red band
uv run wordeasy build a.docx --department-name "Finance"   # label above the title and name under the symbol (one variable)
uv run wordeasy build a.docx --no-twins --no-original-pdf  # styled PDF only: skip lorem/ and original.pdf
uv run wordeasy verify out/a                      # recompile from (edited) .typ and re-check
uv run wordeasy watch out/a --open                # live preview: PDF recompiles on every save
uv run wordeasy tools                             # show detected tool versions
```

### Local web UI (Anti-Rdiim)

```
uv run wordeasy ui                                # opens http://127.0.0.1:<random port>/?token=… in the browser
uv run wordeasy ui --out /some/dir --no-browser   # browse another output root; print the URL only
```

The UI is a thin layer over the same `build` and `verify` code: pick Word files
with the folder browser (files stay where they are, nothing is uploaded), fill in
the cover fields, watch each document move through converting, rendering and
checking. Every output folder under `--out` is listed for reopening. A document
page has four tabs:

- **Live** — the original (`original.pdf`, LibreOffice's render of the Word file)
  on the left, the generated styled PDF on the right.
- **Lorem** — the same pair for the lorem ipsum twin: the anonymised Word file
  rendered by LibreOffice, and the anonymised file styled through the template.
  "Rebuild twin" builds the twin again on demand.
- **Cover & metadata** — the generation data (cover variant, department name,
  title, number, version, department symbol). "Save & rebuild"
  regenerates `main.typ`, gives the twin lorem-ised copies of the new values and
  recompiles and re-checks both.
- **Report** — every integrity check with diff hunks highlighted, plus the
  structure and the tools used.

The Lorem tab's **Add to edge cases** copies the twin's Word file (and nothing else)
into `tests/fixtures/edge-cases/` of the checkout with a neutral name and a short
note, commits the two files, and pushes them to `origin/edge-cases` when the UI was
started with `--push`. That is how a misbehaving document reaches the developers
without the document itself ever leaving the live side; see
[docs/live-side.md](docs/live-side.md).

The UI always styles with `template/`; after editing the template, "Rebuild &
re-verify" on a document recompiles it (and its twin) and runs every check again.

**Template editor.** The Template page edits the `.toml` and `.typ` files in
`template/` as free text (Ctrl-S saves; TOML is validated before it is written).
"Save & rebuild all" recompiles and re-checks every document with the new look.

**Lorem ipsum twin.** Every build also produces a copy that keeps the structure
and design but carries no data: every word is replaced by a lorem word of the
same length and case, digits are randomised, every image becomes a flat
placeholder of the same size, and parts that can hold data but not design
(custom XML and properties, thumbnails, embedded objects, macros, ActiveX,
people, alt chunks) are removed; link targets, alt texts, authors, bookmarks,
field arguments and core properties are replaced. Before the copy is built, the
anonymised Word file is re-read and compared with the original: if any word of the
original survives anywhere, or any binary other than the generated placeholders
remains, the twin is refused and deleted, and the document's report says so in
its `lorem_twin` check. The twin lives in `out/<name>/lorem/`; nothing inside
that folder names the original (paths are relative, cover fields are
lorem-ised), so the folder can be shared as is. Word style names and embedded
fonts are kept, since they are design. Also available from Python as
`wordeasy.pipeline.anonymize_document(out_dir)`, and skipped with `--no-twins`.

It is strictly local:

- binds to `127.0.0.1` only, on a random free port; nothing listens on the LAN
- a random per-launch token is handed over once through the start-up URL and
  then kept in an HttpOnly, SameSite=Strict cookie; every request needs it
- the `Host` header must match the loopback address (blocks DNS rebinding) and
  state-changing requests must originate from the page itself
- files are served only from registered wordeasy output folders, by extension
- no CORS, no outbound network, no telemetry; Ctrl-C or the Quit button ends it.
  The one exception is opt-in: `--push` lets "Add to edge cases" run `git push`

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

The cover page comes in variants, one `[cover.variants.<name>]` table each
(`small`: short red band, `large`: taller band with room for a long title). A
variant overrides any `[cover]` key, usually just `background`. The variant is
chosen per document (`--cover`, the terminal prompt, or the cover picker in the
UI's setup form and document page) and defaults to `[cover] variant`. The
department name (`[document] department_name`, `--department-name`) is one
variable used twice on the cover: as the label above the title and under the
department symbol.
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
| `original_pdf` | | never; WARN when LibreOffice is missing or could not render the Word file |
| `lorem_twin` | the anonymised Word file vs the original (leak verifier), then the twin's own build | never; WARN when the twin was refused or its own build FAILs |
| `typst_warnings`, `pandoc_warnings` | | never; reported as WARN |

Punctuation-only differences are WARN. Diffs are reported as hunks with five
words of context on each side, in `report.md` and on the console.

Dropped by design and listed in the report: Word headers/footers (the template
provides its own), comments, and Word's own table of contents (paragraphs in
`toc N` styles plus the styles/heading texts listed under `[source]` in
`theme.toml`); the template regenerates the TOC. The document title goes to the cover page.

## Development

```
uv run python tests/make_fixtures.py   # regenerate tests/fixtures/*.docx
uv run pytest
```

`tests/test_verify.py::test_tampered_content_fails` edits a generated
`content.typ` (one changed word, one deleted phrase, one edited table cell) and
asserts the verifier reports exactly those three hunks.
