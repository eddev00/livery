# Livery Desktop

A Linux desktop app for restyling batches of Word documents with an AI design assistant.
Import `.docx` files, select a heading, paragraph or table in the preview, describe the change
in the chat, review the proposed design against your real content, and apply the look across
every matching document. Export is PDF only, and only documents that pass the content
checks are exported.

Content never changes. Every PDF is checked word by word against its Word source (the engine's
integrity checks). Your documents never leave the computer: the assistant
(Claude Code or Codex) works in a sandbox on lorem-ipsum copies.

```
app/        Electron + React + TypeScript (Vite): the three-pane workspace
engine/     the Livery Python engine (conversion, integrity checks, lorem twins) and its desktop
            layer in engine/livery/desktop/: local API, projects, design revisions,
            selection maps, agent adapters, sandboxes
packaging/  AppImage build (engine and tools built in an Ubuntu 22.04 container) and the
            clean-system test
docs/       pilot setup, the designer's workflow, privacy and isolation
```

- **Install and set up the pilot:** [docs/SETUP.md](docs/SETUP.md)
- **Use it:** [docs/WORKFLOW.md](docs/WORKFLOW.md)
- **What stays private, and how:** [docs/PRIVACY.md](docs/PRIVACY.md)
- **How it is built:** [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- **How documents are read in (per-project mapping rules):** [docs/MAPPING.md](docs/MAPPING.md)

## Build the AppImage

Needs Docker and Node.js 20 or newer.

```
packaging/build-appimage.sh          # -> dist/Livery-1.0.0-x86_64.AppImage
packaging/test-clean.sh              # installs it in a fresh ubuntu:24.04 container and runs the
                                     # end-to-end smoke test (import, verify, select, agent loop
                                     # with the test agent in the sandbox, accept, export)
```

## Develop

```
cd engine && uv sync                 # Python engine, tests need pandoc, typst, poppler, bwrap
uv run pytest                        # engine tests + tests/desktop (fake agent in the real sandbox)
uv run python scripts/batch_pilot.py # the 50-document batch run

cd app && npm install
npm run dev                          # builds and starts Electron with the engine from ../engine
```

`LIVERY_HOME` moves the data folder (default `~/.local/share/livery`).
`LIVERY_ENABLE_FAKE_AGENT=1` adds the deterministic test agent next to Claude Code and Codex.
`LIVERY_SMOKE=<folder>` with `LIVERY_SMOKE_DOCS=a.docx:b.docx` runs the app's built-in
end-to-end smoke test and exits (add `LIVERY_SMOKE_AGENT=fake` for the agent loop).
