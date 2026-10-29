# Architecture

```
Electron main ──spawn, token on stdin──▶ engine (python -m livery.desktop serve / packaged
   │  narrow preload API (IPC)              livery-engine) ── FastAPI on 127.0.0.1:<random>
   ▼                                          │
sandboxed renderer (React, pdf.js)            ├─ store: projects, revisions, proposals (JSON, atomic)
                                              ├─ builder: import (engine build_document) and renders
                                              │  per revision (engine verify_document) + selection map
                                              ├─ jobs: per-document background work, de-duplicated
                                              ├─ conversation → agents (Claude Code stream-json,
                                              │  Codex app-server JSON-RPC, fake) in the agent sandbox
                                              ├─ workspace: sanitized /work, render-preview requests
                                              └─ patterns, proposals, export
```

## Engine changes (engine/livery)

- **Project-local templates.** The template is the folder holding the theme when it has a
  `template.typ`; Typst's `--root` is the common folder of template, theme and output, so a
  render root contains exactly one document and one design. The repository location no
  longer matters.
- **Element instrumentation.** A third pass of the Lua filter (enabled by `[desktop] instrument`)
  gives every top-level block an ID (`h0004`, `p0012`, `t0008`, …), brackets it with two
  invisible `#metadata` markers and writes the inventory (kind, heading level, table shape:
  columns, header rows, merged header cells, body rows). The markers add no text; the read-back
  check strips them (only lines of exactly the generated form) before comparing. IDs are stable
  for an import version because content never changes; a replaced document is a new version.
- `LIVERY_TOOLS_DIR` for bundled tools, `render.COMMAND_WRAPPER` to run Typst in a sandbox,
  `design.json` as a Typst input, `LIVERY_IGNORE_SYSTEM_FONTS` for reproducible output.
- Fix: single-column nested tables (`columns: (50fr,)`) can be read back.

## Neutral template (engine/livery/desktop/neutral)

Source Sans 3 (OFL) plus Typst's embedded fonts. The template tracks the current element from
the markers (show rules on the marker labels update a state), resolves each heading,
paragraph and table style as theme → treatment → override, and publishes `<we-map>`: every
marker's page and position plus the text area. The selection map turns that into one
rectangle per page per element, from the actual render. The verification render keeps the
design rules, so a design that hides content fails the text check.

## Design revisions

A revision is `theme.toml`, `design.json` (treatments, assignments and overrides keyed by
`<document>/<element>`) and `custom.typ`. Proposals record their base revision; accepting
re-checks that base under the project lock and adds the revision atomically. Builds of
documents a change did not touch are carried over to the new revision.

## Import mapping

Each revision's `mapping.toml` (settings and rules, [MAPPING.md](MAPPING.md)) decides the
conversion. The import is the conversion with the default mapping; a revision with another
mapping gets its own conversion of each document on demand (`builder.ensure_conversion`,
`imports/<doc>/v<n>/mapped/<key>/`), with the lorem twin converted again from the import's
verified anonymised Word file. Element IDs differ between conversions: a mapping proposal's
real renders carry the design's references over by aligning the two inventories (kind and
text), and accepting stores the carried references. In the engine, page rules are applied to
the Word XML before pandoc (`docx_fixups.drop_pages`) and the other rules in the Lua filter's
first pass; the `mapping_rules` and `mapping_text` import checks account for every removed word.

## Agents

Common adapter: status, session start/restore, send, streamed normalized events (text,
tool activity, approvals, done, errors classified as auth/quota/cancelled/exited), cancel.

- Claude Code: one `claude -p --output-format stream-json --include-partial-messages` per turn,
  `--session-id` then `--resume`; profile via `CLAUDE_CONFIG_DIR`.
- Codex: a long-lived `codex app-server` (JSON-RPC over stdio): `initialize`, `account/read`,
  `thread/start|resume`, `turn/start`, `turn/interrupt`, notifications for deltas, items and
  turn completion, server requests for approvals; profile via `CODEX_HOME`.

After each turn the workspace design is validated, re-keyed from `doc-NN` to real document
IDs, merged with the base and stored as the session's proposal, and the twins are rendered
for the synthetic preview. When the agent changed `mapping.toml`, the samples are converted
again first (before `render-preview` and after the turn) and the workspace references are
carried to the new element IDs.

## Packaging

`packaging/build-engine.sh` runs in Ubuntu 22.04: PyInstaller (python-build-standalone 3.12)
for the engine, static Pandoc and Typst, Poppler with its libraries behind small wrappers.
electron-builder packs those as resources of the AppImage. `packaging/test-clean.sh` runs the
built-in smoke test in a fresh Ubuntu 24.04 container with only Bubblewrap and Xvfb added.
