"""Forward a lorem-ipsum twin to the code repository as an edge case.

The live side (where real documents are built) never shares a document with the agent side
(where the code is developed). What crosses is one file: the twin's Word file, copied into
``tests/fixtures/edge-cases/<slug>.docx`` next to a short note, committed and, on request,
pushed. The test suite on the agent side builds every file in that folder.

Guards, in order:
  * only a file that carries the anonymiser's markers is accepted (``is_twin_docx``)
  * the slug and the note may not contain the original file name or any word of it, since
    the output folder is named after the source document and that name is often the most
    identifying thing about it
  * only the two new files are staged and committed, whatever else the checkout holds
"""
from __future__ import annotations

import datetime as _dt
import re
import secrets
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Any, Iterable

from .tools import REPO_ROOT

EDGE_CASES_DIR = REPO_ROOT / "tests" / "fixtures" / "edge-cases"
DEFAULT_BRANCH = "edge-cases"
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,40}$")
_WORDS = re.compile(r"[^\W\d_]{4,}")


def is_twin_docx(path: Path) -> bool:
    """True when the package properties carry the anonymiser's markers."""
    try:
        with zipfile.ZipFile(path) as z:
            names = {n.lower(): n for n in z.namelist()}
            app, core = names.get("docprops/app.xml"), names.get("docprops/core.xml")
            if app and b"<Application>Anonymized</Application>" in z.read(app):
                return True
            if core and b"<dc:creator>Anonymous</dc:creator>" in z.read(core):
                return True
    except (OSError, zipfile.BadZipFile):
        return False
    return False


def default_slug() -> str:
    return f"case-{_dt.date.today().isoformat()}-{secrets.token_hex(2)}"


def forbidden_words(source_name: str) -> set[str]:
    """The source file name, normalised, plus every word of four letters or more in it."""
    stem = Path(source_name).stem.lower()
    norm = re.sub(r"[^a-z0-9]+", "-", stem).strip("-")
    words = {w.lower() for w in _WORDS.findall(stem)}
    return {w for w in words | {norm} if w}


def _mentions(text: str, forbidden: Iterable[str]) -> str | None:
    low = text.lower()
    tokens = set(_WORDS.findall(low)) | {re.sub(r"[^a-z0-9]+", "-", low).strip("-")}
    for f in sorted(forbidden, key=len, reverse=True):
        if f in tokens or (len(f) >= 8 and f in low):
            return f
    return None


def validate_slug(slug: str, forbidden: Iterable[str] = ()) -> str:
    slug = (slug or "").strip().lower()
    if not SLUG_RE.match(slug):
        raise ValueError("slug: 3 to 41 characters, lowercase letters, digits and dashes, starting with a letter or digit")
    hit = _mentions(slug, forbidden)
    if hit:
        raise ValueError(f"slug must not be derived from the source file name (contains “{hit}”)")
    return slug


def validate_note(note: str, forbidden: Iterable[str] = ()) -> str:
    note = (note or "").strip()
    if len(note) > 2000:
        raise ValueError("note: at most 2000 characters")
    hit = _mentions(note, forbidden)
    if hit:
        raise ValueError(f"note must not mention the source file name (contains “{hit}”)")
    return note


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def git_toplevel(path: Path) -> Path | None:
    r = _git(path, "rev-parse", "--show-toplevel")
    return Path(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else None


def add_edge_case(twin_docx: Path, *, slug: str, note: str, source_name: str, dest_dir: Path = EDGE_CASES_DIR,
                  twin_report: dict[str, Any] | None = None, push: bool = False, branch: str = DEFAULT_BRANCH) -> dict[str, Any]:
    """Copy the twin and a note into dest_dir, commit both, push when asked. Raises ValueError on a refused input."""
    forbidden = forbidden_words(source_name)
    slug = validate_slug(slug, forbidden)
    note = validate_note(note, forbidden)
    twin_docx = Path(twin_docx)
    if not twin_docx.is_file():
        raise ValueError("no lorem twin: build the twin first")
    if not is_twin_docx(twin_docx):
        raise ValueError("refused: this file does not carry the anonymiser's markers, so it is not a lorem twin")
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    docx_out, note_out = dest_dir / f"{slug}.docx", dest_dir / f"{slug}.md"
    if docx_out.exists() or note_out.exists():
        raise FileExistsError(f"an edge case named “{slug}” already exists")

    shutil.copyfile(twin_docx, docx_out)
    lines = [f"# {slug}", "", note or "(no note)", ""]
    if twin_report:
        bad = [c["name"] for c in twin_report.get("checks", []) if c.get("status") in ("FAIL", "WARN")]
        lines += [f"Twin build: {twin_report.get('status', '?')}" + (f"; checks not PASS: {', '.join(bad)}" if bad else ""), ""]
    lines += ["Build it with: uv run pytest tests/test_edge_cases.py -q", ""]
    note_out.write_text("\n".join(lines), encoding="utf-8")

    result: dict[str, Any] = {"slug": slug, "docx": str(docx_out), "note": str(note_out), "dir": str(dest_dir),
                              "committed": False, "commit": None, "pushed": False, "branch": branch, "message": ""}
    repo = git_toplevel(dest_dir)
    if repo is None:
        result["message"] = "copied; the folder is not inside a git checkout, so nothing was committed"
        return result
    result["repo"] = str(repo)
    rel = [str(p.resolve().relative_to(repo.resolve())) for p in (docx_out, note_out)]
    add = _git(repo, "add", "--", *rel)
    if add.returncode != 0:
        result["message"] = f"copied; git add failed: {add.stderr.strip()}"
        return result
    commit = _git(repo, "commit", "-q", "-m", f"Edge case: {slug}\n\n{note}".rstrip(), "--", *rel)
    if commit.returncode != 0:
        result["message"] = f"copied and staged; git commit failed: {(commit.stderr or commit.stdout).strip()}"
        return result
    result["committed"] = True
    result["commit"] = _git(repo, "rev-parse", "--short", "HEAD").stdout.strip()
    result["message"] = f"committed {result['commit']}"
    if push:
        pushed = _git(repo, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")
        if pushed.returncode == 0:
            result["pushed"] = True
            result["message"] += f", pushed to origin/{branch}"
        else:
            result["message"] += f"; push failed: {pushed.stderr.strip() or 'unknown error'}"
    else:
        result["message"] += f"; push it with: git push origin HEAD:{branch}"
    return result
