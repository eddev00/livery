"""Every lorem-ipsum twin under tests/fixtures/edge-cases/ must be a real twin and must build.

Workflow: the live side copies out/<doc>/lorem/lorem.docx of a misbehaving document into
tests/fixtures/edge-cases/<slug>.docx; the fix is made here until the build passes, and the
fixture stays as a regression test. See docs/live-side.md.
"""
import json
from pathlib import Path

import pytest

from wordeasy.cli import main
from wordeasy.edge_cases import is_twin_docx

EDGE_CASES = Path(__file__).resolve().parent / "fixtures" / "edge-cases"
DOCS = sorted(EDGE_CASES.glob("*.docx"))


@pytest.mark.parametrize("docx", DOCS, ids=[d.stem for d in DOCS])
def test_edge_case_is_a_twin(docx: Path):
    assert is_twin_docx(docx), (
        f"{docx.name} is not a lorem-ipsum twin. Only out/<doc>/lorem/lorem.docx files belong here; "
        "never a live document."
    )


@pytest.mark.parametrize("docx", DOCS, ids=[d.stem for d in DOCS])
def test_edge_case_builds(docx: Path, tmp_path: Path):
    rc = main(["build", str(docx), "--out", str(tmp_path), "--no-prompt", "--no-twins", "--no-original-pdf",
               "--accept-changes"])
    report = json.loads((tmp_path / docx.stem / "report.json").read_text(encoding="utf-8"))
    failed = [c["name"] for c in report["checks"] if c["status"] == "FAIL"]
    assert rc == 0 and not failed, (docx.name, failed, report)
