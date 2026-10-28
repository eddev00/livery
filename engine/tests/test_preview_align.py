"""Word alignment behind the side-by-side viewer: the same document rendered twice, mapped word to word."""
from wordeasy.ui.preview import align_tokens


def test_identical_sequences_map_one_to_one():
    a = "the cat the dog the end".split()
    assert align_tokens(a, list(a)) == [0, 1, 2, 3, 4, 5]


def test_text_only_on_one_side_stays_unmatched():
    a = "hdr a b c hdr d e f".split()
    assert align_tokens(a, "a b c d e f".split()) == [-1, 0, 1, 2, -1, 3, 4, 5]


def test_repeated_phrases_keep_their_order():
    # the same heading appears twice; the second occurrence must map to the second, not the first
    a = "intro heading one text heading one more text end".split()
    b = "cover intro heading one text heading one more text end".split()
    assert align_tokens(a, b) == [1, 2, 3, 4, 5, 6, 7, 8, 9]


def test_result_is_monotonic_and_consistent():
    body = ("lorem ipsum dolor sit amet " * 40).split()
    a = body + ["unique", "tail"]
    b = ["page", "1"] + body + ["page", "2", "unique", "tail"]
    ab = align_tokens(a, b)
    matched = [j for j in ab if j >= 0]
    assert matched == sorted(matched) and len(set(matched)) == len(matched)
    assert all(a[i] == b[j] for i, j in enumerate(ab) if j >= 0)
    assert len(matched) == len(a)
