"""Locate external tools (pandoc, typst, poppler) and run them."""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LOCAL_BIN = REPO_ROOT / ".tools" / "bin"
TEMPLATE_DIR = REPO_ROOT / "template"
FILTER_DIR = REPO_ROOT / "filters"

INSTALL_HINT = (
    "Install with:  sudo pacman -S typst pandoc-cli poppler\n"
    "or drop release binaries into .tools/bin/ (see README)."
)


class ToolMissing(RuntimeError):
    pass


def find_tool(name: str) -> Path | None:
    # A packaged build (the desktop AppImage) ships its own tools and names their folder here;
    # they win over whatever the host has installed.
    bundled = os.environ.get("LIVERY_TOOLS_DIR")
    if bundled:
        p = Path(bundled) / name
        if p.is_file() and os.access(p, os.X_OK):
            return p
    local = LOCAL_BIN / name
    found = shutil.which(name)
    if found:
        return Path(found)
    if local.is_file() and os.access(local, os.X_OK):
        return local
    return None


def require_tools(*names: str) -> dict[str, Path]:
    found: dict[str, Path] = {}
    missing: list[str] = []
    for n in names:
        p = find_tool(n)
        if p is None:
            missing.append(n)
        else:
            found[n] = p
    if missing:
        raise ToolMissing(f"missing tool(s): {', '.join(missing)}\n{INSTALL_HINT}")
    return found


@dataclass
class RunResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def run(cmd: list[str | Path], *, cwd: Path | None = None, env: dict[str, str] | None = None,
        input_text: str | None = None, check: bool = True) -> RunResult:
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    proc = subprocess.run(
        [str(c) for c in cmd],
        cwd=str(cwd) if cwd else None,
        env=full_env,
        input=input_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    res = RunResult(proc.returncode, proc.stdout, proc.stderr)
    if check and not res.ok:
        raise RuntimeError(f"command failed ({proc.returncode}): {' '.join(str(c) for c in cmd)}\n{res.stderr}")
    return res


def tool_versions() -> dict[str, str]:
    out: dict[str, str] = {}
    for n in ("pandoc", "typst", "pdftotext", "pdfimages", "pdffonts", "soffice"):
        p = find_tool(n)
        if p is None:
            out[n] = "missing"
            continue
        try:
            flag = "-v" if n.startswith("pdf") else "--version"
            r = run([p, flag], check=False)
            first = (r.stdout or r.stderr).strip().splitlines()[0] if (r.stdout or r.stderr).strip() else "?"
            out[n] = f"{first}  ({p})"
        except Exception as e:  # pragma: no cover
            out[n] = f"error: {e}"
    return out
