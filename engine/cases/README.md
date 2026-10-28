# cases/ — verified lorem twins only

This folder is the **only** place real-document work is allowed to be shared, shown to
Claude, or committed. It holds lorem-ipsum twins: anonymised copies where every word,
number and image has been replaced and a verifier has confirmed no word of the original
survives.

## The boundary

- **Real inputs and outputs live outside this repo.** Point the pipeline at an external
  folder: `wordeasy build <file> --out /path/outside/repo`. Never build real data into
  the repo's `out/` (that path is deny-listed in `.claude/settings.json`).
- **Real and twin files share the same names.** In a build, `out/<doc>/source.txt` is the
  REAL extracted text; `out/<doc>/lorem/source.txt` is the twin's. The only thing telling
  them apart is the `lorem/` path segment, so a wrong copy is easy. Do not hand-copy from
  a real `out/<doc>` folder — use the script below, which copies only the twin.

## Exporting a twin here

```
scripts/export-twin.sh /path/outside/repo/<doc>   [name]
```

The script refuses unless the twin recorded a clean verification, and copies only the
`lorem/` subtree. When you want Claude to fix a generation rule, point it at files under
`cases/<name>/`, never at the external real folders.

## Rule of thumb

If a path does not contain `lorem/` (or live under `cases/`), assume it is real data and
keep it away from the chat.
