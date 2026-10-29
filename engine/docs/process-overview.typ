// How livery works — a plain-language overview for non-technical readers.
// Build: typst compile --font-path template/assets/fonts docs/process-overview.typ

#let accent = rgb("#1F4E79")
#let soft = rgb("#DCE6F1")
#let red = rgb("#BC1C20")
#let muted = rgb("#666666")
#let green = rgb("#2E7D32")
#let amber = rgb("#B26A00")

#set page(paper: "a4", margin: (top: 2.2cm, bottom: 2cm, x: 2.2cm),
  footer: context [
    #set text(size: 8.5pt, fill: muted)
    livery — how a document is styled, twinned and kept away from the AI
    #h(1fr)
    #counter(page).display("1 / 1", both: true)
  ])
#set text(font: "Source Sans 3", size: 10.5pt, fill: rgb("#262626"))
#set par(justify: false, leading: 0.6em, spacing: 0.9em)
#show heading.where(level: 1): it => block(above: 1.6em, below: 0.7em)[
  #text(size: 16pt, weight: "bold", fill: accent)[#it.body]
  #v(-0.4em)
  #line(length: 100%, stroke: 0.6pt + accent)
]
#show heading.where(level: 2): it => block(above: 1.1em, below: 0.4em)[
  #text(size: 12pt, weight: "bold", fill: accent)[#it.body]
]
#set list(indent: 0.6em, spacing: 0.5em)
#set enum(indent: 0.6em, spacing: 0.5em)
#set table(stroke: 0.4pt + rgb("#BFBFBF"), inset: 6pt)
#show table.cell.where(y: 0): set text(weight: "bold", fill: white)

#let tag(label, color) = box(fill: color, inset: (x: 5pt, y: 2pt), radius: 2pt,
  text(size: 8pt, weight: "bold", fill: white)[#label])
#let PASS = tag("PASS", green)
#let WARN = tag("WARN", amber)
#let FAIL = tag("FAIL", red)

#let step(n, title, body) = grid(columns: (2em, 1fr), column-gutter: 0.6em, row-gutter: 0pt,
  align(top, circle(radius: 0.8em, fill: accent, text(fill: white, weight: "bold", size: 9.5pt)[#n])),
  [*#title* \ #body])

#let callout(body) = block(fill: soft, inset: 10pt, radius: 3pt, width: 100%, body)

// ---------------------------------------------------------------- title
#block[
  #text(size: 9pt, fill: muted, tracking: 0.08em)[LIVERY · PROCESS OVERVIEW]
  #v(0.2em)
  #text(size: 24pt, weight: "bold", fill: accent)[How a Word file becomes a styled PDF — safely]
  #v(0.3em)
  #text(size: 11pt, fill: muted)[The steps, the checks, the anonymised "lorem twin", and how documents are kept away from the AI assistant. September 2026.]
]

= In one paragraph

livery takes a Word document, keeps every word of it, and lays it out again in the
house template: cover page, table of contents, chapter pages, fonts and colours. It
never trusts itself: after every build it compares the result with the original,
word by word, and refuses to deliver a PDF whose content differs. Alongside the real
PDF it produces a *lorem twin* — a copy with the same shape but nonsense text — so
that layout problems can be shared and fixed without the real content ever leaving
the office.

#callout[
  *Two promises.* (1) The styled PDF says exactly what the Word file says — nothing
  added, nothing lost. (2) Real documents are never seen by the AI coding assistant
  that maintains the tool; it only ever works on lorem twins.
]

= 1. Making the PDF

#step(1, "Convert")[
  The Word file is opened and its content — headings, paragraphs, tables, lists,
  footnotes, links, images — is written out as plain, style-free text. Word's own
  headers, footers, comments and table of contents are set aside on purpose: the
  template provides its own.
]
#step(2, "Style")[
  That plain content is combined with the template (one shared look for all
  documents) and the cover details you enter: department, title, number, version.
  The typesetter produces the finished PDF.
]
#step(3, "Compare with the original")[
  Word also gets rendered as-is (through LibreOffice) into `original.pdf`, so the
  two can be viewed side by side in the built-in viewer.
]
#step(4, "Check, then decide")[
  A series of independent checks (next section) runs automatically. Every check is
  written to a report with evidence. One #FAIL and the document is marked as not
  deliverable.
]

== What you get per document

A folder with: the styled PDF, `original.pdf` for comparison, the report
(`report.md`), the editable content, extracted images, and a `lorem/` sub-folder
holding the twin.

#pagebreak(weak: true)
= 2. The checks — what each one protects

"Content" means every word, number, punctuation mark, table cell, footnote,
caption, list item and link. Spacing, fonts, colours and page breaks are
*presentation* and are allowed to change.

#table(
  columns: (1.15fr, 2fr, 1.05fr),
  fill: (x, y) => if y == 0 { accent } else if calc.odd(y) { soft.lighten(50%) } else { white },
  table.header([Check], [What it compares], [Result when something is off]),
  [Text after conversion], [Every word of the Word file against the converted content, read back independently], [#FAIL on any differing word; punctuation-only differences #WARN],
  [Structure], [Number of headings, tables, cells, images, lists, footnotes, links — and the text of every table cell], [#FAIL if any count or cell differs],
  [Text in the PDF], [Every word of the original against the words extracted from a bare render (no cover, headers or numbering, so only content is compared)], [#FAIL if a word is missing or added; words glued or split by narrow columns are #WARN and listed for a visual look],
  [Text in the styled PDF], [Every original word must be findable somewhere in the final PDF], [#WARN listing anything not found],
  [Images], [Image count in the PDF vs. the source], [#FAIL if fewer],
  [Fonts], [All fonts embedded and known; fallback fonts flagged], [#FAIL if a font is not embedded; #WARN for fallback symbols],
  [Tracked changes], [Unresolved Word revisions], [#FAIL until they are accepted (or the build is told to accept them)],
  [Original render], [LibreOffice produced `original.pdf`], [#WARN if it could not],
  [Lorem twin], [The twin passed its leak check and its own build], [#WARN if the twin was refused],
)

#callout[
  *Reading a report.* #PASS everything matches. #WARN the PDF is fine but something
  deserves a glance (listed with page and words). #FAIL do not deliver; the report
  shows the exact differing words with five words of context on each side.
  Editing the words by hand and re-verifying is reported as #FAIL on purpose;
  layout edits pass.
]

= 3. The lorem twin

The twin is a copy of the Word file that keeps the *design* and the *shape* of the
document but carries *no data*. It is what gets shared when a document misbehaves
in the template and the developers need to see why.

== How it is made

- Every word is replaced by a lorem-ipsum word of the *same length and capitalisation*,
  so line breaks and column widths stay the same.
- Every digit is randomised (a typed heading number such as "2.3" is kept, as it is structure).
- Every picture becomes a flat grey placeholder of the same size.
- Anything that can hold data but not design is *removed*: hidden custom data,
  document properties, thumbnails, embedded files, macros, people, comments' authors.
- Link addresses, image descriptions, author names, bookmarks and cover details are replaced.
- Word style names and fonts are *kept*: they are design, not content.

== How it is checked before it is allowed to exist

The anonymised file is re-opened and searched, everywhere, for any word of the
original — in text, in link addresses, in hidden attributes, in file names. It is
also inspected for any binary part that is not one of our own placeholders, and
embedded fonts must be byte-identical to fonts already in the source (so a font
cannot be altered to smuggle data). *If anything survives, the twin is deleted*
and the document's report says so. Only a twin that passes is then built through
the template and gets its own report.

Nothing in the `lorem/` folder names the original document, so the folder can be
handed over as it is.

= 4. Keeping documents away from the AI assistant

The tool is developed with the help of an AI coding assistant (Claude Code). The
rule that keeps client documents away from it is a *location*, not a setting:

#grid(columns: (1fr, 1fr), column-gutter: 1em, align: top,
  block(fill: soft, inset: 10pt, radius: 3pt)[
    *Live side* — where real documents are \
    A separate user account or machine with *no AI assistant installed*. It only
    pulls the finished tool, runs it on the Word files, and keeps all output in
    its own folders outside the tool's code.
  ],
  block(fill: soft, inset: 10pt, radius: 3pt)[
    *Agent side* — where the tool is built \
    The code, the template and a set of *lorem twins* used as test cases. No real
    document ever exists here. The AI assistant only sees nonsense text with the
    right shape.
  ],
)

== The one bridge between the two sides

When a document breaks the layout, the operator on the live side presses
*Add to edge cases* in the viewer. The tool then:

+ refuses anything that is not a verified twin, and refuses a case name or note
  that contains the original file name or any word of it;
+ copies *only* the twin's Word file, under a neutral name such as
  `case-2026-09-16-a3f9`, together with a one-line note;
+ records those two files alone and, only if explicitly enabled, sends them to the
  developers. Otherwise the tool makes no network connection at all.

On the agent side the test suite first confirms every received file really is a
twin before building it, and the fix is verified against that twin. The real
document is never needed.

== Belt on top of the wall

On any machine where both sides could exist, additional guard-rails limit accidents:

- The assistant's own configuration *denies it read, write and command access* to
  the live folders and to every output folder, and confines it to the tool's own
  directory in a sandbox.
- A commit hook *rejects any Word or PDF file* from entering the code repository
  unless it sits in the test-fixtures folder reserved for twins.
- The viewer is strictly local: it listens only on the machine itself, on a random
  port, with a one-time access token; nothing is uploaded, no outside connections
  are made, and there is no telemetry.

These are safety nets, not the boundary. The boundary is that live documents are
simply not there.

#pagebreak(weak: true)
= 5. Glossary

#table(columns: (1fr, 3fr), fill: (x, y) => if y == 0 { accent } else { white },
  [Term], [Meaning],
  [Template], [The single house style every document is laid out in (cover, fonts, colours, chapter pages).],
  [Report], [The list of every check, its result and evidence, produced with each build.],
  [Lorem twin], [An anonymised copy of a document: same shape, nonsense words, placeholder images.],
  [Edge case], [A twin that exposes a layout problem, kept as a permanent test so the problem cannot come back.],
  [Live side / agent side], [Where real documents live (no AI) versus where the tool is developed (AI allowed, twins only).],
)
