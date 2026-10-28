"""report.json / report.md writers and console summary."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table

from .verify import FAIL, PASS, SKIP, WARN, CheckResult, worst

console = Console()
_ICON = {PASS: "[green]PASS[/]", WARN: "[yellow]WARN[/]", FAIL: "[bold red]FAIL[/]", SKIP: "[dim]SKIP[/]"}


def build_report(*, source: Path, out_dir: Path, pdf: Path | None, checks: list[CheckResult],
                 info: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": str(source),
        "output_dir": str(out_dir),
        "pdf": str(pdf) if pdf else None,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": worst(checks),
        "checks": [c.as_dict() for c in checks],
        "info": info,
    }


def write_report(report: dict[str, Any], out_dir: Path) -> tuple[Path, Path]:
    j = out_dir / "report.json"
    j.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    m = out_dir / "report.md"
    m.write_text(render_markdown(report), encoding="utf-8")
    return j, m


def render_markdown(r: dict[str, Any]) -> str:
    lines = [f"# wordeasy report — {Path(r['source']).name}", "",
             f"**Status: {r['status']}**  ", f"Source: `{r['source']}`  ", f"PDF: `{r['pdf']}`  ",
             f"Generated: {r['generated_at']}", "", "## Checks", "",
             "| Check | Status | Summary |", "|---|---|---|"]
    for c in r["checks"]:
        lines.append(f"| {c['name']} | **{c['status']}** | {c['summary']} |")
    for c in r["checks"]:
        if c["details"]:
            lines += ["", f"### {c['name']} ({c['status']})", ""]
            lines += [f"- {d}" for d in c["details"]]
    info = r.get("info", {})
    lines += ["", "## Source document", ""]
    for k in ("title", "converted_from"):
        if info.get(k):
            lines.append(f"- {k}: {info[k]}")
    if info.get("tracked_changes"):
        tc = info["tracked_changes"]
        lines += ["", "### Tracked changes (accepted during conversion)", ""]
        lines += [f"- inserted: «{t}»" for t in tc.get("insertions", [])]
        lines += [f"- deleted: «{t}»" for t in tc.get("deletions", [])]
    if info.get("comments"):
        lines += ["", "### Comments (dropped by design)", ""] + [f"- {t}" for t in info["comments"]]
    if info.get("headings"):
        lines += ["", "### Heading structure (typed number → level → regenerated number)", "",
                  "| kind | typed | level | regenerated | text |", "|---|---|---|---|---|"]
        for h in info["headings"]:
            lines.append(f"| {h['kind']} | {h['typed']} | {h['level']} | {h['expected']} | {h['text']} |")
    if info.get("dropped_paragraphs"):
        lines += ["", "### Dropped by design (Word table of contents, empty headings, hand-typed heading numbers)", ""]
        lines += [f"- {t}" for t in info["dropped_paragraphs"]]
    if info.get("smartart"):
        lines += ["", "### SmartArt diagrams (rendered as pictures; text inside is not verifiable)", ""]
        lines += [f"- diagram {d['index']}: " + " | ".join(d["text"]) for d in info["smartart"]]
    if info.get("dropped_headers_footers"):
        lines += ["", "### Source header/footer text (dropped by design, template provides its own)", ""]
        lines += [f"- {t}" for t in info["dropped_headers_footers"]]
    if info.get("structure"):
        lines += ["", "### Structure", "", "```", json.dumps(info["structure"], indent=2), "```"]
    if info.get("typst_special_chars"):
        lines += ["", f"Typst-special characters in source (escaped by pandoc): `{info['typst_special_chars']}`"]
    if info.get("tools"):
        lines += ["", "## Tools", ""] + [f"- {k}: {v}" for k, v in info["tools"].items()]
    return "\n".join(lines) + "\n"


def print_summary(reports: list[dict[str, Any]]) -> None:
    t = Table(title="wordeasy", show_lines=False)
    t.add_column("Document")
    t.add_column("Status")
    t.add_column("Checks")
    t.add_column("PDF")
    for r in reports:
        checks = "  ".join(f"{c['name'].replace('_', ' ')}: {_ICON[c['status']]}" for c in r["checks"])
        t.add_row(Path(r["source"]).name, _ICON[r["status"]], checks, r["pdf"] or "-")
    console.print(t)
    for r in reports:
        for c in r["checks"]:
            if c["status"] in (FAIL, WARN) and c["details"]:
                console.print(f"[bold]{Path(r['source']).name} › {c['name']}[/] — {c['summary']}")
                for d in c["details"][:8]:
                    console.print(f"   {d}", markup=False, highlight=False)
