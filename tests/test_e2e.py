import json
import subprocess
from pathlib import Path

import pytest

from wordeasy.cli import main

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


def _report(out_dir: Path) -> dict:
    return json.loads((out_dir / "report.json").read_text(encoding="utf-8"))


def _statuses(rep: dict) -> dict[str, str]:
    return {c["name"]: c["status"] for c in rep["checks"]}


def test_features_build_passes(built_features):
    rc, out_dir = built_features
    rep = _report(out_dir)
    assert rc == 0, rep
    assert rep["status"] == "PASS"
    st = _statuses(rep)
    for name in ("text_after_conversion", "structure_after_conversion", "render", "text_in_pdf",
                 "text_in_styled_pdf", "images", "fonts"):
        assert st[name] == "PASS", (name, rep)
    for f in ("content.typ", "main.typ", "features.pdf", "source.txt", "report.md", "wordeasy.json"):
        assert (out_dir / f).exists(), f
    assert list((out_dir / "media").glob("*.png"))
    # structure captured from the source
    s = rep["info"]["structure"]
    assert s["headings"] == {"1": 3, "2": 2, "3": 1}
    assert s["tables"] == 1 and s["table_cells"] == 16 and s["images"] == 1 and s["footnotes"] == 1
    # custom "Chapter Title" style became a level-1 heading and the doc title came from metadata
    assert rep["info"]["title"] == "Feature Coverage Document"
    typ = (out_dir / "content.typ").read_text(encoding="utf-8")
    assert "= Introduction to the System" in typ and "= Second Chapter" in typ
    assert "<" not in typ.split("\n")[0]  # no <label> on headings


def test_layout_build_passes(tmp_path):
    rc = main(["build", str(FIXTURES / "layout.docx"), "--out", str(tmp_path)])
    rep = _report(tmp_path / "layout")
    assert rc == 0 and rep["status"] == "WARN", rep
    assert rep["info"]["dropped_headers_footers"]  # Word header/footer text is reported
    typ = (tmp_path / "layout" / "content.typ").read_text(encoding="utf-8")
    assert "rowspan: 2" in typ and "colspan: 3" in typ
    # cell shading is read from the docx: header row blue, one body cell yellow, merged cells expanded
    fills = json.loads((tmp_path / "layout" / "table-fills.json").read_text(encoding="utf-8"))
    assert fills[0]["fills"][0] == ["#1F4E79", "#1F4E79", "#1F4E79"]
    assert fills[0]["fills"][2] == [None, None, "#FFF2CC"] and fills[0]["fills"][3] == [None, None, None]
    assert fills[0]["valign"][0] == ["top", "top", "top"]
    # vertical merge extent per cell: the merged cell covers rows 1-2, its neighbours one row each
    assert fills[0]["rows"][1] == [[1, 2], [1, 1], [1, 1]] and fills[0]["rows"][2] == [[1, 2], [2, 1], [2, 1]]
    assert fills[0]["rows"][3] == [[3, 1], [3, 1], [3, 1]]
    assert "= Styled Chapter Heading" in typ
    # Word's own TOC is dropped and reported; the internal link survives as text
    dropped = rep["info"]["dropped_paragraphs"]
    assert len(dropped) == 3 and any("Long table" in d for d in dropped)
    assert _statuses(rep)["dropped_paragraphs"] == "WARN"
    assert "Table of Contents" not in typ and "the long table section" in typ and "#link(<" not in typ


def test_tracked_changes_fail_unless_accepted(tmp_path):
    rc = main(["build", str(FIXTURES / "tracked.docx"), "--out", str(tmp_path / "a")])
    rep = _report(tmp_path / "a" / "tracked")
    assert rc == 1 and rep["status"] == "FAIL"
    assert _statuses(rep)["tracked_changes"] == "FAIL"
    assert rep["info"]["comments"] == ["A commented sentence."]

    rc = main(["build", str(FIXTURES / "tracked.docx"), "--out", str(tmp_path / "b"), "--accept-changes"])
    rep = _report(tmp_path / "b" / "tracked")
    assert rc == 0 and rep["status"] == "WARN"
    assert rep["info"]["tracked_changes"]["insertions"] == ["This text was inserted"]


def test_theme_change_is_content_neutral(built_features, tmp_path):
    """A different theme changes the PDF but not a single check outcome."""
    rc, out_dir = built_features
    theme = tmp_path / "theme.toml"
    src = (ROOT / "template" / "theme.toml").read_text(encoding="utf-8")
    theme.write_text(src.replace('accent = "#1F4E79"', 'accent = "#8B0000"').replace("zebra = true", "zebra = false"),
                     encoding="utf-8")
    rc = main(["build", str(FIXTURES / "features.docx"), "--out", str(tmp_path / "out"), "--theme", str(theme)])
    rep = _report(tmp_path / "out" / "features")
    assert rc == 0 and rep["status"] == "PASS", rep
    a = (out_dir / "features.pdf").read_bytes()
    b = (tmp_path / "out" / "features" / "features.pdf").read_bytes()
    assert a != b


def test_cli_entrypoint_runs():
    r = subprocess.run(["uv", "run", "wordeasy", "tools"], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0 and "pandoc" in r.stdout


def test_typed_heading_numbers_define_structure(tmp_path):
    rc = main(["build", str(FIXTURES / "numbered.docx"), "--out", str(tmp_path)])
    rep = _report(tmp_path / "numbered")
    assert rc == 0, rep
    st = _statuses(rep)
    assert st["text_after_conversion"] == "PASS" and st["text_in_pdf"] == "PASS"
    # levels come from the typed numbers, numbers are stripped, front matter is unnumbered
    typ = (tmp_path / "numbered" / "content.typ").read_text(encoding="utf-8")
    assert "#heading(level: 1, numbering: none)[Preface]" in typ
    assert "= Alpha chapter" in typ and "== Alpha first section" in typ
    assert "== Alpha second section" in typ and "=== Deep one" in typ
    assert "= Gamma chapter" in typ and "1.2.1" not in typ
    assert rep["info"]["structure"]["headings"] == {"1": 3, "2": 2, "3": 1}
    # the skipped number is reported, not silently renumbered
    assert st["heading_numbering"] == "WARN"
    hn = next(c for c in rep["checks"] if c["name"] == "heading_numbering")
    assert any("typed «3» → regenerated «2»" in d for d in hn["details"])


def test_chapter_words_shift_plain_numbers_down(tmp_path):
    """'Section 01. X' declares the chapter; '1. Intro' below it is level 2."""
    rc = main(["build", str(FIXTURES / "layout.docx"), "--out", str(tmp_path)])
    assert rc == 0
