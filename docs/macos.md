# Install and run on a MacBook

Ten minutes, all from Terminal. Works on Apple Silicon and Intel.

## 1. Homebrew

If `brew` is not installed yet:

```
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

Follow the two "Next steps" lines it prints at the end (they add `brew` to your
shell), then open a new Terminal window.

## 2. Tools

```
brew install git uv typst pandoc poppler
brew install --cask libreoffice
```

| Tool | Why |
|---|---|
| `uv` | installs Python 3.12 and the project's packages, runs the commands |
| `typst` | compiles the styled PDF |
| `pandoc` | reads the Word file |
| `poppler` | `pdftotext`, `pdfimages`, `pdffonts` for the content checks |
| LibreOffice | renders `original.pdf` (the Word file as Word shows it) and converts `.doc`/`.odt`/`.rtf`. Optional: without it the build still succeeds and reports the missing original as WARN |

## 3. Get the repository

```
cd ~
git clone https://github.com/eddev00/word-easy.git
cd word-easy
uv sync
```

If the clone asks for credentials, the repository is private: run `brew install gh`,
then `gh auth login` (choose GitHub.com, HTTPS, log in with the browser) and clone
again.

Turn on the repository's git hooks once per clone (they refuse to commit any Word
file or PDF outside `tests/fixtures/`):

```
git config core.hooksPath .githooks
```

## 4. Let wordeasy find LibreOffice

macOS does not put LibreOffice on the PATH. Link it into the folder wordeasy
checks for tools:

```
mkdir -p .tools/bin
ln -sf /Applications/LibreOffice.app/Contents/MacOS/soffice .tools/bin/soffice
```

Check that everything is found:

```
uv run wordeasy tools
```

Every line should show a version. `soffice` missing means step 4 did not work;
anything else missing means step 2 did not finish.

## 5. Run the server

```
mkdir -p ~/wordeasy-live/in ~/wordeasy-live/out
uv run wordeasy ui --out ~/wordeasy-live/out
```

The browser opens on `http://127.0.0.1:<port>/?token=…`. The server listens on this
Mac only and stops with Ctrl-C or the Quit button. Put the Word files to convert in
`~/wordeasy-live/in/` and pick them with "New build"; the files stay where they are.

Keep the documents in `~/wordeasy-live/`, never inside the `word-easy` folder:
that folder is the code, and it is the only thing that may be shared with a coding
agent. See [live-side.md](live-side.md) for the reasoning and the edge-case flow.

To let the "Add to edge cases" button push its commit to GitHub, start with
`--push` (needs the `gh auth login` from step 3):

```
uv run wordeasy ui --out ~/wordeasy-live/out --push
```

## 6. Update later

```
cd ~/word-easy
git pull
uv sync
```

## Command line instead of the UI

```
uv run wordeasy build ~/wordeasy-live/in/*.docx --out ~/wordeasy-live/out
open ~/wordeasy-live/out/<name>/<name>.pdf
```

## If something fails

- `uv: command not found` right after step 2: open a new Terminal window.
- "LibreOffice conversion produced no file": quit the LibreOffice application if it
  is open; the headless converter cannot run next to the GUI.
- Fonts: the template's fonts ship in `template/assets/fonts/`, nothing to install.
- A macOS "cannot be opened because the developer cannot be verified" dialog for
  `typst` or `pandoc` does not occur with Homebrew builds; if you see one, remove
  the binary you downloaded by hand and use the Homebrew one.
