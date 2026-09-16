"""wordeasy command line: build Word documents into styled Typst + PDF, verify content integrity."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from rich.prompt import Prompt

from . import verify as V
from .convert import DEFAULT_THEME, load_theme
from .pipeline import REQUIRED_TOOLS, BuildOptions, build_document, cover_variants, out_dir_for, verify_document
from .report import console, print_summary
from .tools import REPO_ROOT, ToolMissing, require_tools, tool_versions

EXIT_OK, EXIT_FAIL, EXIT_ERROR = 0, 1, 2


def _cover_prompter(args: argparse.Namespace):
    """Return a callback that asks for the cover fields on a terminal; None when not interactive."""
    if not (sys.stdin.isatty() and not args.no_prompt):
        return None

    variants = [v["name"] for v in cover_variants(load_theme(Path(args.theme).resolve()))]

    def ask(source: Path, proposed: dict[str, str]) -> dict[str, str]:
        answers = dict(proposed)
        console.print(f"[bold]Cover information for {source.name}[/] (Enter keeps the value in brackets)")
        if args.cover is None and variants:
            answers["cover"] = Prompt.ask("  Cover variant", choices=variants, default=answers["cover"] or variants[0], show_default=True)
        if args.department_name is None:
            answers["department_name"] = Prompt.ask("  Department name (label above the title)", default=answers["department_name"], show_default=True)
        if args.title is None:
            answers["title"] = Prompt.ask("  Title", default=answers["title"], show_default=True)
        if args.number is None:
            answers["number"] = Prompt.ask("  Cover number", default=answers["number"], show_default=True)
        if args.version is None:
            answers["version"] = Prompt.ask("  Version (e.g. V1, MAR 2026)", default=answers["version"], show_default=True)
        return answers

    return ask


def cmd_build(args: argparse.Namespace) -> int:
    try:
        require_tools(*REQUIRED_TOOLS)
    except ToolMissing as e:
        console.print(f"[red]{e}[/]")
        return EXIT_ERROR
    opts = BuildOptions(
        theme=Path(args.theme).resolve(), out_root=Path(args.out).resolve(),
        accept_changes=args.accept_changes, clean=args.clean,
        title=args.title, department=args.department,
        number=args.number, version=args.version, department_name=args.department_name,
        cover=args.cover, original_pdf=not args.no_original_pdf, twins=not args.no_twins,
    )
    ask = _cover_prompter(args)
    reports: list[dict[str, Any]] = []
    had_error = False

    for src in args.files:
        source = Path(src).resolve()
        if not source.exists():
            console.print(f"[red]not found: {source}[/]")
            had_error = True
            if not args.keep_going:
                break
            continue
        console.print(f"[bold]{source.name}[/] → {out_dir_for(source, opts.out_root)}")
        try:
            reports.append(build_document(source, opts, ask_cover=ask))
        except Exception as e:  # noqa: BLE001
            had_error = True
            console.print(f"[red]error while processing {source.name}: {e}[/]")
            if args.debug:
                raise
            if not args.keep_going:
                break
        if reports and reports[-1]["status"] == V.FAIL and not args.keep_going:
            break

    if reports:
        print_summary(reports)
    if had_error:
        return EXIT_ERROR
    return EXIT_FAIL if any(r["status"] == V.FAIL for r in reports) else EXIT_OK


def cmd_verify(args: argparse.Namespace) -> int:
    try:
        require_tools(*REQUIRED_TOOLS)
    except ToolMissing as e:
        console.print(f"[red]{e}[/]")
        return EXIT_ERROR
    reports: list[dict[str, Any]] = []
    theme = Path(args.theme).resolve() if args.theme else None
    for d in args.dirs:
        out_dir = Path(d).resolve()
        console.print(f"[bold]{out_dir.name}[/] (recompile + verify) ← {out_dir}")
        try:
            reports.append(verify_document(out_dir, theme))
        except FileNotFoundError as e:
            console.print(f"[red]{e}[/]")
            return EXIT_ERROR
    print_summary(reports)
    return EXIT_FAIL if any(r["status"] == V.FAIL for r in reports) else EXIT_OK


def cmd_tools(_: argparse.Namespace) -> int:
    for k, v in tool_versions().items():
        console.print(f"{k:10s} {v}")
    return EXIT_OK


def cmd_ui(args: argparse.Namespace) -> int:
    from .ui.server import serve

    try:
        require_tools(*REQUIRED_TOOLS)
    except ToolMissing as e:
        console.print(f"[red]{e}[/]")
        return EXIT_ERROR
    return serve(out_root=Path(args.out).resolve(), port=args.port, open_browser=not args.no_browser,
                 edge_cases_dir=Path(args.edge_cases_dir).resolve() if args.edge_cases_dir else None, push=args.push)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="wordeasy", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="convert .docx files to styled Typst + PDF (plus original.pdf and a lorem ipsum twin) and verify content")
    b.add_argument("files", nargs="+", help=".docx (or .doc/.odt/.rtf via LibreOffice) files")
    b.add_argument("--out", default=str(REPO_ROOT / "out"), help="output root (default: ./out)")
    b.add_argument("--theme", default=str(DEFAULT_THEME), help="theme.toml to use")
    b.add_argument("--keep-going", action="store_true", help="continue with the next file after a FAIL")
    b.add_argument("--clean", action="store_true", help="delete the document's output folder before building")
    b.add_argument("--accept-changes", action="store_true", help="accept tracked changes instead of failing")
    b.add_argument("--title", help="document title (default from the Word metadata, then the file name)")
    b.add_argument("--department", help="bold prefix before the title in the footer (default from theme [document])")
    b.add_argument("--number", help="big number on the cover page (default from theme [cover])")
    b.add_argument("--cover", help="cover variant, one of the [cover.variants.*] names in the theme (default from theme [cover])")
    b.add_argument("--version", help="version text on the cover, e.g. 'V1, MAR 2026'")
    b.add_argument("--department-name", help="department name: the label above the title and the name under the symbol on the cover (default from theme [document])")
    b.add_argument("--no-original-pdf", action="store_true", help="skip original.pdf (the Word file rendered by LibreOffice)")
    b.add_argument("--no-twins", action="store_true", help="skip the lorem ipsum twin under lorem/")
    b.add_argument("--no-prompt", action="store_true", help="never ask for cover information interactively")
    b.add_argument("--debug", action="store_true")
    b.set_defaults(func=cmd_build)

    v = sub.add_parser("verify", help="recompile an output folder from its .typ files and re-run all checks")
    v.add_argument("dirs", nargs="+", help="output folder(s), e.g. out/mydoc")
    v.add_argument("--theme", default=None)
    v.set_defaults(func=cmd_verify)

    t = sub.add_parser("tools", help="show detected tool versions")
    t.set_defaults(func=cmd_tools)

    u = sub.add_parser("ui", help="open the local web UI (loopback only, one-time access token)")
    u.add_argument("--out", default=str(REPO_ROOT / "out"), help="output root to build into and browse (default: ./out)")
    u.add_argument("--port", type=int, default=0, help="port on 127.0.0.1 (default: a free random port)")
    u.add_argument("--no-browser", action="store_true", help="print the URL instead of opening a browser")
    u.add_argument("--edge-cases-dir", default=None, help="where \"Add to edge cases\" copies a lorem twin (default: tests/fixtures/edge-cases in this checkout)")
    u.add_argument("--push", action="store_true", help="after committing an edge case, push it to origin/edge-cases (the only outbound network call the UI ever makes)")
    u.set_defaults(func=cmd_ui)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
