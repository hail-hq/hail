"""Search words, ID spellings and chunking for the knowledge base."""

from __future__ import annotations

from hailhq.core.knowledge.text import (
    Block,
    chunk_blocks,
    compact_id,
    query_terms,
    terms,
)


def _words(text: str) -> int:
    return len(text.split())


def test_latin_words_lowercase_without_accents() -> None:
    assert terms("Garantía DÉCADE") == ["garantia", "decade"]


def test_scripts_without_spaces_give_bigrams() -> None:
    assert terms("保修期") == ["保修", "修期"]
    assert terms("ภาษาไทย")[:2] == ["ภา", "าษ"]
    assert "保修" in query_terms("逆变器的保修期是多久")


def test_devanagari_keeps_its_vowel_signs() -> None:
    assert terms("वारंटी") == ["वारंटी"]


def test_id_spellings_meet_on_one_word() -> None:
    doc = set(terms("Use the battery NS-BAT-48V-100 only."))
    for spoken in (
        "NS BAT 48V 100",
        "ns bat 48 v 100",
        "nsbat48v100",
        "NS.BAT/48V_100",
        "NSBAT 48 V 100",
    ):
        assert "nsbat48v100" in set(terms(spoken)), spoken
    assert "nsbat48v100" in doc
    assert "nsbat48v100" in set(terms("NS BAT 48V 100"))
    assert "nsbat48v100" in set(terms("ns bat 48 v 100"))
    assert "e204" in set(terms("Error E-204 shows")) & set(terms("what is e 204"))
    assert compact_id("ns-bat 48V/100") == "nsbat48v100"


def test_query_terms_are_distinct_and_capped() -> None:
    words = query_terms(" ".join(f"w{i} w{i}" for i in range(100)))
    assert len(words) == len(set(words)) == 24


def test_chunks_start_new_piece_on_heading_and_keep_pages() -> None:
    para = " ".join(["word"] * 150)
    blocks = [
        Block("Intro", page=1, heading=True),
        Block(para, page=1),
        Block(para, page=2),
        Block("Battery", page=3, heading=True),
        Block("The battery stores energy.", page=3),
    ]
    chunks = chunk_blocks(blocks, _words)
    assert chunks[0].page == 1 and chunks[0].text.startswith("Intro")
    assert chunks[-1].text.startswith("Battery")
    assert chunks[-1].page == 3
    assert all(_words(c.text) <= 500 for c in chunks)


def test_long_text_is_cut_with_overlap() -> None:
    words = [f"w{i}" for i in range(1200)]
    chunks = chunk_blocks([Block(" ".join(words), page=5)], _words)
    assert len(chunks) >= 3
    assert all(_words(c.text) <= 500 for c in chunks)
    first, second = chunks[0].text.split(), chunks[1].text.split()
    # The second piece starts with the end of the first.
    assert second[0] in first and first.index(second[0]) > len(first) - 60
    assert {c.page for c in chunks} == {5}


def test_text_with_no_spaces_is_cut_too() -> None:
    chunks = chunk_blocks([Block("保" * 3000)], lambda t: len(t))
    assert chunks and all(len(c.text) <= 500 for c in chunks)


def test_chunking_stops_past_max_pieces() -> None:
    para = " ".join(["word"] * 450)
    chunks = chunk_blocks([Block(para) for _ in range(50)], _words, max_pieces=3)
    assert len(chunks) == 4


def test_piece_gets_its_section_heading() -> None:
    para = " ".join(["word"] * 300)
    chunks = chunk_blocks(
        [Block("Warranty", heading=True), Block(para), Block(para)], _words
    )
    assert len(chunks) >= 2
    assert all(c.text.startswith("Warranty") for c in chunks)


def test_dash_joins_stay_small() -> None:
    words = set(terms("state-of-the-art and/or e.g Wi-Fi"))
    assert "wifi" in words
    assert "stateoftheart" not in words and "andor" not in words and "eg" not in words


def test_a_longer_heading_after_a_flush_still_fits() -> None:
    blocks = [
        Block(" ".join(["intro"] * 50)),
        Block(" ".join(["head"] * 30), heading=True),
        Block(" ".join(["word"] * 3000)),
    ]
    chunks = chunk_blocks(blocks, _words)
    assert all(_words(c.text) <= 500 for c in chunks), [_words(c.text) for c in chunks]
