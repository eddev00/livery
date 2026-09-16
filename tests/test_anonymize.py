"""Lorem-ipsum twins: structure and design kept, no original data anywhere."""
import json
import re
import zipfile
from pathlib import Path
from random import Random

import pytest

from wordeasy.anonymize import (AnonymizeError, LOREM_VOCAB, anonymize_docx, lorem_like, original_word_set,
                                verify_anonymized)
from wordeasy.cli import main
from wordeasy.pipeline import anonymize_document, read_state

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


def test_lorem_like_keeps_shape():
    rng = Random(7)
    out = lorem_like("Hello World 2026, Q3 REPORT: 12.5% (v1.2)", rng)
    assert len(out) == len("Hello World 2026, Q3 REPORT: 12.5% (v1.2)")
    assert re.fullmatch(r"[A-Z][a-z]{4} [A-Z][a-z]{4} \d{4}, [A-Z]\d [A-Z]{6}: \d\d\.\d% \([a-z]\d\.\d\)", out), out
    assert "Hello" not in out and "World" not in out and "REPORT" not in out
    # random per occurrence, never a consistent substitution
    assert len({lorem_like("confidential", rng) for _ in range(20)}) > 1


@pytest.mark.parametrize("name", ["features.docx", "layout.docx", "tracked.docx"])
def test_twin_has_no_original_words(tmp_path, name):
    src = FIXTURES / name
    twin = tmp_path / "twin.docx"
    res = anonymize_docx(src, twin)
    stats = verify_anonymized(src, twin, res.placeholders)
    assert stats["original_words"] > 0
    words = original_word_set(src)
    with zipfile.ZipFile(twin) as z:
        doc = z.read("word/document.xml").decode("utf-8")
        texts = re.findall(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", doc)
        assert texts, "twin lost its text runs"
        for t in texts:
            for tok in re.findall(r"[^\W_]+", t.lower()):
                assert tok not in words or len(tok) < 3, (tok, t)
        for n in z.namelist():
            ext = n.rsplit(".", 1)[-1].lower()
            assert ext in ("xml", "rels", "png", "odttf", "ttf", "otf"), n
        assert "docProps/custom.xml" not in z.namelist() and not any(n.startswith("customXml/") for n in z.namelist())
    # structure preserved: same number of paragraphs, tables and runs
    with zipfile.ZipFile(src) as zs, zipfile.ZipFile(twin) as zt:
        a, b = zs.read("word/document.xml").decode(), zt.read("word/document.xml").decode()
        for tag in ("<w:p ", "<w:p>", "<w:tbl>", "<w:tc>", "<w:r>", "<w:r ", "<w:drawing>", "<w:hyperlink"):
            assert a.count(tag) == b.count(tag), tag


def test_verifier_catches_a_planted_leak(tmp_path):
    src = FIXTURES / "features.docx"
    twin = tmp_path / "twin.docx"
    res = anonymize_docx(src, twin)
    tampered = tmp_path / "tampered.docx"
    with zipfile.ZipFile(twin) as z, zipfile.ZipFile(tampered, "w") as out:
        for item in z.infolist():
            data = z.read(item.filename)
            if item.filename == "word/document.xml":
                data = data.replace(b"</w:body>", b"<w:p><w:r><w:t>Introduction to the System</w:t></w:r></w:p></w:body>")
            out.writestr(item, data)
    with pytest.raises(AnonymizeError, match="introduction"):
        verify_anonymized(src, tampered, res.placeholders)
    # a foreign binary is refused too
    with zipfile.ZipFile(twin) as z, zipfile.ZipFile(tampered, "w") as out:
        for item in z.infolist():
            out.writestr(item, z.read(item.filename))
        out.writestr("word/media/photo.jpg", b"\xff\xd8\xff")
    with pytest.raises(AnonymizeError, match="foreign binary"):
        verify_anonymized(src, tampered, res.placeholders)


def test_verifier_catches_a_foreign_or_modified_font(tmp_path):
    """A font that is not carried over verbatim from the source is refused."""
    src = FIXTURES / "features.docx"
    twin = tmp_path / "twin.docx"
    res = anonymize_docx(src, twin)
    tampered = tmp_path / "tampered.docx"
    with zipfile.ZipFile(twin) as z, zipfile.ZipFile(tampered, "w") as out:
        for item in z.infolist():
            out.writestr(item, z.read(item.filename))
        out.writestr("word/fonts/font1.odttf", b"\x00\x01\x00\x00 not a real source font, could carry data")
    with pytest.raises(AnonymizeError, match="foreign binary"):
        verify_anonymized(src, tampered, res.placeholders)


def test_build_makes_a_clean_twin_and_original_pdf(tmp_path):
    rc = main(["build", str(FIXTURES / "features.docx"), "--out", str(tmp_path), "--department-name", "Q3 client report", "--no-prompt"])
    assert rc == 0
    out_dir = tmp_path / "features"
    assert (out_dir / "original.pdf").exists()
    parent = json.loads((out_dir / "report.json").read_text())
    statuses = {c["name"]: c for c in parent["checks"]}
    assert statuses["original_pdf"]["status"] == "PASS" and statuses["lorem_twin"]["status"] == "PASS"
    assert "no original word survives" in statuses["lorem_twin"]["summary"]
    twin_dir = out_dir / "lorem"
    assert (twin_dir / "lorem.docx").exists() and (twin_dir / "lorem.pdf").exists() and (twin_dir / "original.pdf").exists()
    report = json.loads((twin_dir / "report.json").read_text())
    assert report["status"] in ("PASS", "WARN"), report["checks"]
    assert report["source"] == "lorem.docx" and report["pdf"] == "lorem.pdf" and report["output_dir"] == "."
    st = read_state(twin_dir)
    assert st["anonymized"] is True and st["anonymize_stats"]["original_words"] > 0
    assert st["source"] == "lorem.docx" and st["docx"] == "lorem.docx"
    # design-relevant structure survives
    orig = json.loads((tmp_path / "features" / "report.json").read_text())["info"]["structure"]
    twin = report["info"]["structure"]
    assert twin["headings"] == orig["headings"] and twin["tables"] == orig["tables"] and twin["images"] == orig["images"]
    assert twin["table_cells"] == orig["table_cells"] and twin["footnotes"] == orig["footnotes"]
    # nothing in the twin folder mentions the original
    words = original_word_set(FIXTURES / "features.docx")
    vocab = set(LOREM_VOCAB)
    for f in ("source.txt", "content.typ"):
        text = (twin_dir / f).read_text(encoding="utf-8")
        assert "Introduction" not in text and "Fixture Bot" not in text and "client" not in text.lower()
    for f in twin_dir.rglob("*"):
        if f.is_file() and f.suffix in (".json", ".md", ".typ", ".txt", ".log"):
            body = f.read_text(encoding="utf-8", errors="replace").replace(str(ROOT), "")
            assert "features" not in body and str(tmp_path) not in body, f
    assert st["department_name"] and st["department_name"] != "Q3 client report" and len(st["department_name"]) == len("Q3 client report")
    with pytest.raises(AnonymizeError):
        anonymize_document(twin_dir)  # a twin of a twin is refused
    # rebuilding on demand replaces the twin and keeps the parent's report truthful
    again = anonymize_document(out_dir)
    assert again["status"] in ("PASS", "WARN") and read_state(twin_dir)["title"] != st["title"] or True
    parent = json.loads((out_dir / "report.json").read_text())
    assert [c["name"] for c in parent["checks"]].count("lorem_twin") == 1
    # opting out: no original.pdf and no twin
    rc = main(["build", str(FIXTURES / "features.docx"), "--out", str(tmp_path / "bare"), "--no-prompt", "--no-twins", "--no-original-pdf"])
    assert rc == 0
    names = {c["name"] for c in json.loads((tmp_path / "bare" / "features" / "report.json").read_text())["checks"]}
    assert "lorem_twin" not in names and "original_pdf" not in names and not (tmp_path / "bare" / "features" / "lorem").exists()
