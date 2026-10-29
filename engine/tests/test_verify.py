import json
import shutil
from pathlib import Path

from livery import verify as V
from livery.cli import main


def _report(out_dir: Path) -> dict:
    return json.loads((out_dir / "report.json").read_text(encoding="utf-8"))


def test_diff_hunks_show_context():
    a = "the quick brown fox jumps over the lazy dog".split()
    b = "the quick brown cat jumps over the dog".split()
    hunks = V.diff_hunks(a, b, context=2)
    assert any("«fox» → «cat»" in h for h in hunks)
    assert any("«lazy» → «»" in h for h in hunks)


def test_text_check_punctuation_only_is_warn():
    r = V.check_text_after_conversion("Hello, world.", "Hello world", [])
    assert r.status == V.WARN


def test_text_check_ignored_elements_fail():
    r = V.check_text_after_conversion("same", "same", ["unknown block element foo"])
    assert r.status == V.FAIL


def test_tampered_content_fails(built_features, tmp_path):
    rc, out_dir = built_features
    assert rc == 0, _report(out_dir)
    tampered = tmp_path / "features"
    shutil.copytree(out_dir, tampered)
    typ = tampered / "content.typ"
    text = typ.read_text(encoding="utf-8")
    assert "Second bullet with" in text and "Nested bullet B" in text and "[Gadget]" in text
    text = text.replace("Second bullet with", "Second bullet without")   # changed word
    text = text.replace("Nested bullet B", "")                           # deleted words
    text = text.replace("[Gadget]", "[Gadgets]")                         # edited table cell
    typ.write_text(text, encoding="utf-8")

    rc = main(["verify", str(tampered)])
    assert rc == 1
    rep = _report(tampered)
    assert rep["status"] == "FAIL"
    by = {c["name"]: c for c in rep["checks"]}
    assert by["text_after_conversion"]["status"] == "FAIL"
    joined = "\n".join(by["text_after_conversion"]["details"])
    assert "«with» → «without»" in joined
    assert "«Nested bullet B» → «»" in joined
    assert "«Gadget» → «Gadgets»" in joined
    assert by["structure_after_conversion"]["status"] == "FAIL"
    assert any("table cell" in d for d in by["structure_after_conversion"]["details"])
    # the relocated folder must still compile against the repo template
    assert by["render"]["status"] == "PASS"
    assert by["text_in_pdf"]["status"] == "FAIL"


def test_unedited_folder_reverifies_clean(built_features, tmp_path):
    rc, out_dir = built_features
    copy = tmp_path / "features"
    shutil.copytree(out_dir, copy)
    assert main(["verify", str(copy)]) == 0
    assert _report(copy)["status"] == "PASS"


def test_reconcile_glued_words_sharing_a_letter():
    # pdftotext keeps one of two identical letters that coincide when a cell overflows
    r = V.reconcile(["SCELERISQUE", "EIUSMOD", "DUI", "torquent"], ["SCELERISQUE", "EIUSMODUI", "torquent"])
    assert not r["missing"] and not r["extra"] and r["glued"] == ["EIUSMODUI"]
    # a word that merely lost its first letter is still missing
    r = V.reconcile(["EIUSMOD", "PUI"], ["EIUSMODUI"])
    assert r["missing"] == ["PUI"]
