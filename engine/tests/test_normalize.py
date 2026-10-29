from livery.normalize import normalize, tokens, word_tokens


def test_quotes_dashes_ligatures_nbsp():
    src = "“smart” ‘single’ ﬁne 42 kg soft­hyphen well-known en–dash"
    assert tokens(src) == ['"smart"', "'single'", "fine", "42", "kg", "softhyphen", "well", "known", "en", "dash"]


def test_hyphenated_line_break_equals_source():
    assert tokens("a well-known fact") == tokens("a well-\nknown fact")


def test_pandoc_dash_markup_matches_unicode():
    assert normalize("an en--dash and em---dash") == normalize("an en–dash and em—dash")


def test_word_tokens_strip_punctuation_but_keep_case():
    assert word_tokens("Hello, World! (42)") == ["Hello", "World", "42"]


def test_join_hyphens_matches_pdftotext_dehyphenation():
    assert tokens("Sub-subsection well-known", join_hyphens=True) == ["Subsubsection", "wellknown"]
    assert tokens("an en \u2013 dash", join_hyphens=True) == ["an", "en", "dash"]
    # en dashes are never joined: "9.5–10" stays two tokens like pdftotext leaves it
    assert tokens("9.5\u201310 pt", join_hyphens=True) == ["9.5", "10", "pt"]


def test_slash_is_a_boundary_like_a_typst_line_break():
    assert tokens("Caption (Table/Figure) x") == tokens("Caption (Table/\nFigure) x")
    assert tokens("(Table/Figure)", join_hyphens=True) == ["(Table", "Figure)"]
