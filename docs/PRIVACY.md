# Privacy and isolation

## What reaches a provider

Only what you type in the chat, the reference images you attach, plus what the assistant reads
in its **workspace**:

- the design files (theme, treatments, overrides, custom Typst rules, import mapping)
- **lorem-ipsum twins** of the documents: same structure and styles, every word replaced by a
  lorem word of the same length, digits randomised, images replaced by flat placeholders,
  metadata and embedded objects removed. The engine verifies each twin against its original
  and refuses it if any original word survives; creating a workspace scans the twin text
  again and refuses on any hit.
- neutral names (`doc-01`, element IDs like `t0008`) and table shapes; labels like
  "Table 8 · Page 4"
- results of synthetic renders: page images of the twins and diagnostics with paths
  reduced to workspace-relative ones
- for the import mapping: the twins as Word lays them out (page images of the twin's own
  LibreOffice render), the Word paragraph styles each twin uses with counts and pages, and
  how many blocks each mapping rule matched in each twin. Word's built-in style names are
  shown as they are; a custom style name that contains a word of the original is shown as
  "(hidden style N)". The real documents are converted with a changed mapping only on your
  machine, outside the sandbox (to align element IDs); what a rule removes from them is
  shown only to you

Never: the Word files, their text, real previews, reports, file names, the project folder.

**Reference images** are the exception you make yourself. An image attached in the chat is
sent as it looks: what it shows is not anonymised. Livery decodes it, scales it to at most
2000 px and saves it again as a plain PNG, which drops EXIF, location, author and embedded
profiles, and stores it in the workspace as `references/ref-NN.png`. Its file name is not
passed on. The message box says this while an image is attached.

## Agent sandbox (Bubblewrap)

Each assistant runs inside Bubblewrap with:

- a read-only system (`/usr`, a few `/etc` files for TLS and users), its own `/tmp`, `/proc`, `/dev`
- the workspace at `/work` and its Livery profile under an otherwise empty home
- **no network namespace of the host**: no route to the local engine, other loopback services,
  the LAN, D-Bus, X11/Wayland or any host socket. One way out: a CONNECT proxy run by the app
  on a unix socket, bridged into the sandbox, that only connects to the provider's own hosts
  on port 443 (Anthropic for Claude Code, OpenAI for Codex). Blocked connections show up in the
  chat activity.
- a cleared environment (no inherited variables, so no credentials of the app or the user)
- for Claude Code: no settings files, no MCP servers, no skills or slash commands; tools
  limited to reading and editing files and running `render-preview`. For Codex: no MCP
  servers or notify hooks. Each uses only its Livery profile, never your usual one.

When Bubblewrap is missing or cannot create a sandbox, assistants are disabled and the app
says so. It never falls back to running one unrestricted.

## Render sandbox

Every Typst compile (synthetic and real) and every page-image conversion runs in a second
Bubblewrap sandbox with no network at all, read access only to the fonts and tools, write
access only to that one render folder, and a time limit after which it is killed. A design an
assistant wrote can therefore read nothing but the render folder of the document being
rendered (Typst's `--root`).

## Design validation

An assistant's changes become a proposal only after every value is checked against a closed
schema: lengths and colours by pattern, enumerations by value, fonts from the bundled set,
text no smaller than 6 pt. Custom Typst rules may not read files, import or include anything,
use `hide`, touch the element markers or `sys.inputs`. Files that are not plain regular
files (for example a symlink planted by an agent) are refused.

The import mapping (`mapping.toml`, [MAPPING.md](MAPPING.md)) decides how Word blocks become
headings, paragraphs and tables. It is data from a closed schema, never code. It can remove
blocks, but only on the record: every removal is logged with the rule that made it and listed
in the document's report, a document where rules remove more than a third of the words fails,
and a text guard compares the words of the Word file (read without any mapping) with the
converted document: any word missing without a recorded removal fails the document.

A design that still manages to drop content (for example a show rule that removes tables)
fails the content checks, which run on the rendered PDFs, and cannot be accepted or exported.

## Tests that hold this up

`engine/tests/desktop/` runs a deterministic test agent inside the real sandbox and checks:

- a sentinel phrase and number in a private document appear nowhere in the workspace, the
  prompts the agent received, render responses or agent-side records
- the agent cannot read the project folder, the source documents, the user's `~/.claude`,
  `~/.codex`, `~/.ssh`, `~/.config` or documents; cannot reach a service on the host's
  loopback or the internet; sees no host environment; a symlink it plants is refused
- render sandboxes have no network and are killed at their time limit
- a mapping changed by the agent reaches the twins and the real documents only through a
  proposal; the sentinel stays out of the workspace when the samples are converted again
- the engine API refuses requests without the per-launch token or with a foreign Host header