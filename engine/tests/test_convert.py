from pathlib import Path

from wordeasy.convert import _lists_on_own_line, _stretch_table_columns


def test_list_after_bracket_moves_to_own_line(tmp_path):
    f = tmp_path / "c.typ"
    f.write_text("    ], [- #strong[Parent] \\ more\n\n      - nested\n\n    ],\n"
                 "  caption: [- cap item\n\n    - nested cap\n]\n"
                 "#strong[- not a list]\n", encoding="utf-8")
    _lists_on_own_line(f)
    out = f.read_text(encoding="utf-8")
    assert "    ], [\n    - #strong[Parent] \\ more\n" in out
    assert "  caption: [\n  - cap item\n" in out
    assert "#strong[- not a list]" in out  # a bracket right after a word is not a block opener


def test_percent_columns_become_fractions(tmp_path):
    f = tmp_path / "c.typ"
    f.write_text("#table(\n    columns: (25%, 75%),\n    [a], [b],\n)\nwidth: 50% of text\n", encoding="utf-8")
    _stretch_table_columns(f)
    out = f.read_text(encoding="utf-8")
    assert "columns: (25fr, 75fr)" in out and "width: 50% of text" in out
