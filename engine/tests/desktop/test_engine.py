"""Import, instrumentation, selection maps and content integrity with the desktop template."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from livery.convert import strip_markers
from livery.desktop import builder, sandbox
from livery.desktop.design import Design, DesignError, validate
from livery.desktop.store import read_json
from livery.pipeline import verify_document


def _root(project, key, rev="r0001"):
    doc = project.ids[key]
    return builder.render_root(project, doc, project.document(doc)["current"], rev)


def _smap(project, key, rev="r0001"):
    return read_json(_root(project, key, rev) / builder.SELECTION_FILE)


def _el(smap, element_id):
    return next(e for e in smap["elements"] if e["id"] == element_id)


def _text_in_rect(pdf: Path, rect: dict) -> str:
    """pdftotext of one rectangle (points; pdftotext crops in pixels at -r 72 = points)."""
    r = subprocess.run(["pdftotext", "-r", "72", "-f", str(rect["page"]), "-l", str(rect["page"]),
                        "-x", str(int(rect["x"])), "-y", str(int(rect["y"])), "-W", str(int(rect["w"]) + 1),
                        "-H", str(int(rect["h"]) + 1), "-layout", str(pdf), "-"], capture_output=True, text=True)
    return " ".join(r.stdout.split())


def test_every_block_has_a_stable_unique_id(project):
    smap = _smap(project, "report")
    ids = [e["id"] for e in smap["elements"]]
    assert len(ids) == len(set(ids))
    # the two identical paragraphs (repeated text) are two elements
    assert ids[1].startswith("p") and ids[2].startswith("p")
    inv = builder.inventory(project, project.ids["report"], 1)
    assert inv[1]["text"] == inv[2]["text"] and inv[1]["id"] != inv[2]["id"]
    # the lorem twin has exactly the same IDs and kinds: agents can refer to the same elements
    twin = builder.inventory(project, project.ids["report"], 1, twin=True)
    assert [(e["id"], e["kind"]) for e in twin] == [(e["id"], e["kind"]) for e in inv]


def test_grouped_header_and_long_table_and_spanning_paragraph(project):
    smap = _smap(project, "report")
    grouped = _el(smap, "t0006")
    assert grouped["table"]["header_rows"] == 2 and len(grouped["table"]["header_spans"]) == 3
    long_table = _el(smap, "t0009")
    assert len({r["page"] for r in long_table["rects"]}) >= 2, "a long table crosses pages: one rectangle per page"
    para = next(e for e in smap["elements"] if e["kind"] == "paragraph" and len(e["rects"]) >= 2)
    pages = [r["page"] for r in para["rects"]]
    assert pages == list(range(pages[0], pages[0] + len(pages)))


def test_unsupported_elements_are_reported_not_selectable(project):
    smap = _smap(project, "report")
    kinds = {e["kind"]: e["selectable"] for e in smap["elements"]}
    assert kinds["list"] is False and kinds["figure"] is False
    assert all(e["selectable"] for e in smap["elements"] if e["kind"] in ("heading", "paragraph", "table"))


def test_labels_name_kind_ordinal_and_page(project):
    smap = _smap(project, "report")
    assert _el(smap, "t0006")["label"] == "Table 1 · Page 1"
    assert _el(smap, "t0009")["label"].startswith("Table 2 · Page ")


def test_rectangles_contain_their_element(project):
    root = _root(project, "report")
    pdf = builder.pdf_of(root)
    smap = _smap(project, "report")
    assert "Overview" in _text_in_rect(pdf, _el(smap, "h0001")["rects"][0])
    assert "Quarterly figures" in _text_in_rect(pdf, _el(smap, "t0006")["rects"][0])
    first_ledger_page = _el(smap, "t0009")["rects"][0]
    assert "Entry 1.1" in _text_in_rect(pdf, first_ledger_page)
    # and not the neighbour: the heading rectangle does not contain the table
    assert "Quarterly figures" not in _text_in_rect(pdf, _el(smap, "h0005")["rects"][0])


def test_rectangles_follow_pagination_changes(project, tmp_path):
    """A different theme repaginates; the map comes from that render and still hits."""
    base = project.design("r0001")
    theme = base.theme_text.replace('size = "10.5pt"\nweight = "regular"', 'size = "13pt"\nweight = "regular"') \
                           .replace('margin_top = "2.5cm"', 'margin_top = "6cm"')
    assert theme != base.theme_text
    design = Design(theme, base.design, base.custom_text)
    doc = project.ids["report"]
    root = tmp_path / "repaginated"
    st = builder.render_document(project, doc, 1, "test", design=design, root=root)
    assert st["state"] == "done" and st["report_status"] != "FAIL", st
    old, new = _smap(project, "report"), read_json(root / builder.SELECTION_FILE)
    assert new["pages"] > old["pages"]
    assert [e["id"] for e in new["elements"]] == [e["id"] for e in old["elements"]]
    pdf = builder.pdf_of(root)
    moved = _el(new, "t0006")["rects"][0]
    assert moved["y"] != _el(old, "t0006")["rects"][0]["y"]
    assert "Quarterly figures" in _text_in_rect(pdf, moved)
    assert "Entry 1.1" in _text_in_rect(pdf, _el(new, "t0009")["rects"][0])


def test_markers_do_not_change_the_layout(project, tmp_path):
    root = _root(project, "report")
    work = tmp_path / "plain"
    shutil.copytree(root, work, symlinks=True)
    content = work / builder.DOC_DIR / "content.typ"
    content.write_text(strip_markers(content.read_text(encoding="utf-8")), encoding="utf-8")
    (work / builder.DOC_DIR / "elements.json").write_text("[]")
    report = verify_document(work / builder.DOC_DIR, work / "template" / "theme.toml")
    assert report["status"] != "FAIL"

    def layout(pdf):
        return subprocess.run(["pdftotext", "-layout", str(pdf), "-"], capture_output=True, text=True).stdout
    assert layout(builder.pdf_of(root)) == layout(builder.pdf_of(work))


@pytest.mark.parametrize("edit,check", [
    (lambda t: t.replace("This paragraph repeats.", "This paragraph changed.", 1), "text_after_conversion"),
    (lambda t: t.replace("[100]", "[109]", 1), "text_after_conversion"),
    (lambda t: t.replace("[Region row 2]", "[Region row 9]", 1), "structure_after_conversion"),
])
def test_content_checks_still_catch_tampering(project, tmp_path, edit, check):
    """With the markers in place, changed words, numbers and table cells still FAIL."""
    root = _root(project, "report")
    work = tmp_path / "tampered"
    shutil.copytree(root, work, symlinks=True)
    content = work / builder.DOC_DIR / "content.typ"
    text = content.read_text(encoding="utf-8")
    edited = edit(text)
    assert edited != text
    content.write_text(edited, encoding="utf-8")
    report = verify_document(work / builder.DOC_DIR, work / "template" / "theme.toml")
    failed = {c["name"] for c in report["checks"] if c["status"] == "FAIL"}
    assert check in failed, failed


def test_tampered_markers_fail_the_instrumentation_check(project, tmp_path):
    root = _root(project, "report")
    work = tmp_path / "markers"
    shutil.copytree(root, work, symlinks=True)
    content = work / builder.DOC_DIR / "content.typ"
    text = content.read_text(encoding="utf-8")
    content.write_text(text.replace('#metadata("t0006") <we-s>\n', '', 1), encoding="utf-8")
    inv = read_json(work / builder.DOC_DIR / "elements.json")
    _, check = builder.selection_map(work, inv)
    assert check["status"] == "FAIL"


@pytest.mark.parametrize("custom,failed_check", [
    ("#let custom-rules(body) = {\n  show table: it => []\n  body\n}\n", "text_in_pdf"),
    ("#let custom-rules(body) = {\n  show figure.where(kind: image): it => []\n  body\n}\n", "images"),
])
def test_designs_that_drop_content_fail_verification(project, tmp_path, custom, failed_check):
    base = project.design("r0001")
    design = Design(base.theme_text, base.design, custom)
    validate(design, None)  # these pass validation: only the verifier can catch them
    st = builder.render_document(project, project.ids["report"], 1, "drop", design=design, root=tmp_path / "r")
    assert st["report_status"] == "FAIL"
    report = builder.report_of(tmp_path / "r")
    assert failed_check in {c["name"] for c in report["checks"] if c["status"] == "FAIL"}


@pytest.mark.parametrize("change,problem", [
    (lambda d: d.design.update(overrides={"dx/h0001": {"size": "2pt"}}), "between 6pt and 96pt"),
    (lambda d: d.design.update(overrides={"dx/h0001": {"color": "red; #eval"}}), "invalid value"),
    (lambda d: d.design.update(overrides={"dx/l0012": {"color": "#000000"}}), "only headings, paragraphs and tables"),
    (lambda d: d.design.update(treatments={"T": {"kind": "table", "style": {"columns": "1fr"}}}), "columns"),
    (lambda d: d.design.update(assignments={"dx/t0006": "Missing"}), "unknown treatment"),
    (lambda d: setattr(d, "custom_text", '#let custom-rules(body) = { read("/etc/passwd"); body }'), "file access"),
    (lambda d: setattr(d, "custom_text", '#import "/x.typ": *\n#let custom-rules(body) = body'), "include/import"),
    (lambda d: setattr(d, "custom_text", "#let custom-rules(body) = { hide(body) }"), "hiding content"),
    (lambda d: setattr(d, "custom_text", "#let custom-rules(body) = { set text(size: 0pt); body }"), "zero-size"),
    (lambda d: setattr(d, "theme_text", d.theme_text.replace('heading_numbers = "keep"', 'heading_numbers = "parse"')),
     "[source] is fixed"),
    (lambda d: setattr(d, "theme_text", d.theme_text.replace('body = "Source Sans 3"', 'body = "Comic Sans"')),
     "fonts.body"),
])
def test_design_validation_rejects(project, change, problem):
    d = project.design("r0001")
    change(d)
    with pytest.raises(DesignError) as e:
        validate(d, None)
    assert any(problem in p for p in e.value.problems), e.value.problems


def test_render_sandbox_is_offline_and_time_limited(tmp_path):
    cmd = sandbox.render_command(["/bin/sh", "-c", "cat /etc/hostname; ls /home; getent hosts example.com || echo nonet"],
                                 tmp_path, [])
    r = subprocess.run(cmd, capture_output=True, text=True)
    assert "nonet" in r.stdout
    assert Path.home().name not in r.stdout
    import time
    t = time.time()
    r = subprocess.run(sandbox.render_command(["/bin/sleep", "30"], tmp_path, [], timeout_s=1), capture_output=True)
    assert r.returncode != 0 and time.time() - t < 10


def test_typst_runs_in_the_render_sandbox(project, monkeypatch, tmp_path):
    seen = []
    from livery import render
    orig = render.COMMAND_WRAPPER

    def spy(cmd, root, theme):
        out = orig(cmd, root, theme)
        seen.append(out)
        return out
    monkeypatch.setattr(render, "COMMAND_WRAPPER", spy)
    builder.render_document(project, project.ids["small"], 1, "spy", design=project.design("r0001"), root=tmp_path / "s")
    assert seen and all("--unshare-all" in c for c in seen)


def test_numbers_typed_at_the_start_of_a_cell_stay_text(desktop_home, tmp_path):
    from livery.desktop.store import Project

    from .docs import toc_table_doc
    p = Project.create("Typed numbers")
    doc, v = p.add_source(toc_table_doc(tmp_path / "plan.docx"))
    report = builder.import_document(p, doc, v)
    assert report["status"] != "FAIL", [c for c in report["checks"] if c["status"] == "FAIL"]
    content = (p.import_dir(doc, v) / builder.DOC_DIR / "content.typ").read_text()
    assert "[1\\. Overview]" in content
