# case-01

if table cell is in yellow color (any color shade) i want the text color to be black.

Twin build: FAIL; checks not PASS: text_in_pdf, text_in_styled_pdf, fonts, heading_numbering, dropped_paragraphs, outline_levels, smartart

Build it with: uv run pytest tests/test_edge_cases.py -q
