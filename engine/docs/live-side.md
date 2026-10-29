# Live side and agent side

Two places, one code repository. The rule that keeps client documents away from
any AI coding agent is a **location**, not a permission setting: live documents are
never inside this repository, on the agent's machine or under the agent's user.

```
agent side (this repo, Claude Code / Codex allowed)      live side (no agent installed)
──────────────────────────────────────────────────       ───────────────────────────────────
livery/  template/  tests/fixtures/edge-cases/*.docx   ~/livery-live/repo/   (git pull only)
                       ▲                                  ~/livery-live/in/     (Word files)
                       │ one human copy step              ~/livery-live/out/    (build output)
                       └───────── lorem.docx ─────────────┘
```

## Live side

A separate OS user or a separate machine. No Claude Code, Codex, or browser AI on it.

```
sudo useradd -m livery-live                # or use another machine
sudo -iu livery-live
git clone <repo-url> ~/livery-live/repo    # pull only, never edit here
mkdir -p ~/livery-live/in ~/livery-live/out
cd ~/livery-live/repo && uv sync
```

Build and browse with the output root outside the checkout:

```
uv run livery build ~/livery-live/in/*.docx --out ~/livery-live/out
uv run livery ui --out ~/livery-live/out
```

To pick up a fix: `git pull` in `~/livery-live/repo`, then rebuild.

## Forwarding an edge case

From the UI, on the live side: open the document, Lorem tab, **Add to edge cases**.
The dialog asks for a neutral case name (a default like `case-2026-09-16-a3f9` is
proposed) and a one-line note on what is wrong. The server then:

1. refuses anything that is not a verified twin (the anonymiser's markers must be
   in the file), and refuses a name or note that contains the source file name or
   any word of it, since the output folder is named after the original document;
2. copies **only** `lorem/lorem.docx` to `tests/fixtures/edge-cases/<name>.docx` in
   the live checkout, writes `<name>.md` with the note and the twin's own check
   results next to it;
3. stages and commits those two files alone, whatever else the checkout holds;
4. pushes to `origin/edge-cases` when the UI was started with `--push`. Without
   the flag the UI stays free of outbound network calls and the operator runs
   `git push origin HEAD:edge-cases` by hand.

```
uv run livery ui --out ~/livery-live/out --push
```

By hand, the same thing is: check the twin reproduces the problem (its own
`report.md` and `lorem.pdf` under `out/<doc>/lorem/`), copy `lorem.docx` and
nothing else, commit, push. If the twin does not reproduce it, describe the case in
words and build a synthetic docx on the agent side instead.

On the agent side, `git pull origin edge-cases`, then
`uv run pytest tests/test_edge_cases.py` reproduces the failure. The suite first
asserts each file really is a twin and refuses to build anything else. Fix the
code, keep the fixture as a regression test, merge into `main`; the live side
pulls `main` and rebuilds. The pre-commit hook rejects any docx or PDF outside
`tests/fixtures/`.

## Building a twin on the agent side

`out/` is denied to the agent, so twins build into `tests/fixtures/_build/`, which is
gitignored and readable:

```
uv run pytest tests/test_edge_cases.py -q                     # builds every twin, fails on any FAIL check
uv run livery build tests/fixtures/edge-cases/<slug>.docx --out tests/fixtures/_build --no-twins --no-original-pdf --no-prompt
uv run livery verify tests/fixtures/_build/<slug>           # after editing the template or content.typ
uv run livery ui --out tests/fixtures/_build                # human side-by-side look (drop --no-original-pdf for that)
```

`--no-twins` because a twin of a twin is wasted work. Everything under `_build/` is
derived from the twin, so the agent may read all of it.

## What the agent side may still see

The twin keeps shape, not content: heading and table structure, word lengths,
digit-run lengths, custom style names, fonts. If a document's structure or a style
name is identifying on its own, do not forward its twin; reproduce the case from a
synthetic document.

## Belt on top of the wall

`.claude/settings.json` (committed) denies the Read, Edit, Write and Bash tools on
`~/livery-live/` and on `out/`, blocks Read outside the working directory, and
turns on the Bash sandbox with those paths unreadable. Codex's default
workspace-write sandbox confines the filesystem to the checkout on its own. These
rules limit accidents on a machine where both sides exist; they are not the
boundary. The boundary is that live documents are not there.

Enable the versioned hooks once per clone on the agent side:

```
git config core.hooksPath .githooks
```
