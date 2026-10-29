# Import mapping

How a Word document becomes headings, paragraphs and tables is part of a project's design,
not fixed app code. Each design revision carries a `mapping.toml` next to `theme.toml`,
`design.json` and `custom.typ`. The assistant edits it in its workspace like the other
design files, previews the result on the lorem twins, and the change becomes a proposal
that is rendered with the real documents and accepted (or discarded, or undone) like any
other design change.

The mapping is data, never code: settings and an ordered list of rules from a closed
schema. Nothing an agent writes runs on a private document.

## The file

```toml
version = 1

[settings]                       # the conversion settings, all optional
headings = { "Titre 1" = 1, "Body Title" = 2 }   # Word style name -> heading level
drop_styles = ["Distribution List"]              # paragraphs in these styles are removed
drop_headings = ["Contents"]                     # headings with exactly this text are removed
heading_numbers = "keep"                         # "parse": derive levels from typed numbers
chapter_words = ["Section", "Chapter"]
block_quotes = "unwrap"                          # or "keep"
outline_level_headings = false                   # Word outline levels on non-heading styles

[[rule]]
name = "Cover page leftovers"
match = { page = 2 }
action = "drop"
expect = "0-1"                   # how many blocks per document this rule should match

[[rule]]
name = "Bold numbered titles"
match = { style = "Normal", bold = true, numbered = true }
action = { heading = "from_number" }

[[rule]]
name = "Container tables"
match = { kind = "table", contains_table = true }
action = { nested_table = "unwrap_outer" }
```

Settings left out keep the app defaults (`theme.toml [source]` and `[style_map]`).

### Matching

Every key of `match` must hold (AND). Rules are tried in order; the first rule that matches
a block decides what happens to it, before the built-in conversion rules.

| key | applies to | meaning |
|---|---|---|
| `page` | any top-level block | the Word page the block starts on: `2`, `[2, 3]` or `"2-4"`. Pages come from Word's own page-break records (`lastRenderedPageBreak`), or from explicit page and section breaks when the file has none |
| `kind` | any | `paragraph`, `heading`, `table`, `list`, `figure` |
| `style` | paragraph, heading | the Word paragraph style name, or a list of names (case-insensitive). Unstyled paragraphs are `Normal`, built-in headings `Heading 1`… |
| `level` | heading | heading level 1-6 |
| `bold` | paragraph, heading | every word is bold |
| `numbered` | paragraph, heading | the text starts with a typed number (`1.`, `2.3`, `Section 4`) |
| `upper` | paragraph, heading | the text is all capitals |
| `in_table` | any | `true`: only inside table cells; `false`: only outside |
| `contains_table` | table | a table cell holds another table |
| `before_first_heading` | any | the block comes before the document's first heading |
| `text`, `text_starts` | paragraph, heading | exact text or text prefix (case-insensitive), one string or a list |

`page` is matched in the Word file before conversion and only supports `action = "drop"`.
`text` and `text_starts` compare words: they never match the lorem twins, so a rule that
uses them is previewed only with the real documents, locally.

### Actions

| action | for | effect |
|---|---|---|
| `"drop"` | any | remove the block (a declared removal, see below) |
| `"keep"` | any | leave the block as it is in Word, skipping built-in rules (e.g. keep a `toc 1` paragraph) |
| `{ heading = N }` | paragraph, heading | make it a heading of level N (1-6) |
| `{ heading = "from_number" }` | paragraph, heading | level from the typed number: `2.3 Scope` is level 2 |
| `{ paragraph = true }` | heading | make it an ordinary paragraph |
| `{ nested_table = "unwrap_outer" }` | table | replace the outer table by its cells' contents, in reading order |
| `{ nested_table = "unwrap_inner" }` | table | replace each inner table by its cells' contents, inside the outer cell |

`expect` (optional) states how many blocks a rule should match per document: `"1"`,
`"0-2"`, `"1+"`. A document outside that range gets a warning, shown before accepting.

## Content guarantees

Content still never changes silently:

1. **Declared removals.** Every block a rule drops is logged with the rule's name. The
   report of each document lists, per rule, how many blocks it matched and the removed
   text (locally, in the app; the agent only ever sees counts for the twins).
2. **Text guard.** The conversion compares the words of the Word file (read without any
   mapping) with the words of the converted document. Every missing word must be
   explained by a logged removal, a typed heading number the template regenerates, or a
   chapter word. Anything else is a FAIL and blocks the export: a rule can restructure a
   document, never lose text by accident.
3. **Runaway rules.** A document where drop rules removed more than a third of its words
   fails; a rule outside its `expect` range warns.
4. The existing checks (read-back, PDF text, structure, images) run unchanged on the result.

## Element IDs across mappings

Element IDs (`h0004`, `p0012`, …) number the converted blocks, so a mapping change can
shift them. When a revision's mapping differs from the one the design's references were
made with, the references are carried over by aligning the two conversions of each
document (same kind, same text). A reference whose element was dropped or changed kind
(a paragraph that became a heading) cannot be carried: it is removed and listed in the
proposal, so the user sees which styles were lost.

## Where conversions live

```
imports/<doc>/v<n>/doc/                    the conversion with the default mapping (as before)
imports/<doc>/v<n>/mapped/<hash>/doc/      one conversion per other mapping, with its own lorem/
```

A mapping is identified by the hash of its parsed content, so revisions that share a
mapping share one conversion. `original.pdf` (the Word rendering) does not depend on the
mapping and stays in the default conversion.

## Limits

- Pages come from Word's records of its own layout. A file saved by a tool that writes no
  page-break records has only its explicit page and section breaks.
- `text` and `text_starts` need real words, which the assistant only knows when you type
  them in the chat.
- The command-line template applies Word's cell colours by table index; a rule that drops or
  unwraps a table shifts them for the tables after it. The desktop template does not use them.
- A case no match or action can express needs an engine change: a new match key or action,
  available to every project from then on.

## The assistant's side

The workspace has `design/mapping.toml` and, per sample, `styles.json` (the Word paragraph
styles the sample uses, with counts and the pages they appear on) and `previews/doc-NN/word-*.png`
(the lorem twin as Word lays it out, so pages such as a leftover page after the cover are
visible). After the assistant changes `mapping.toml`, `render-preview` converts the twins
again with the new mapping, rewrites `samples/doc-NN/` and re-keys `design/design.json`;
`previews/result.json` reports each rule's match counts per sample and any reference that
could not be carried.

The twins are converted from the anonymised Word files the engine verified when the
document was imported; the real documents are converted only on the user's machine, outside
the agent sandbox, to align IDs and for the real renders.
