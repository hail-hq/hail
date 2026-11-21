"""Search words and pieces for the knowledge base, in every language.

``terms`` is the one tokenizer both keyword searches use: the in-call BM25
(``index``) and the SQL search (``search``), from the words stored in
``knowledge_pieces.terms``. So the two never disagree.

* Scripts written without spaces (Chinese, Japanese, Thai, Lao, Khmer,
  Myanmar) give overlapping character pairs ("bigrams"): a word of any
  length is found by its pairs, with no dictionary.
* Other scripts give their words, lowercased (``casefold``). Latin words
  also lose their accents, so "garantia" finds "garantía".
* IDs and serial numbers: speech-to-text writes "NS-BAT-48V-100" as
  "NS BAT 48V 100" or "nsbat 48 v 100". A run of short parts with a digit
  among them (or joined by dashes, like "Wi-Fi") also gives its parts joined, lowercased, with no spaces,
  dashes, dots, slashes or underscores (``compact_id``), so every spelling
  meets the same word.

``chunk_blocks`` cuts prose into pieces of about 300 to 500 tokens on
headings and paragraphs, with about 50 tokens of overlap, keeping the page
each piece starts on.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass

import regex

__all__ = [
    "MAX_PIECE_TOKENS",
    "OVERLAP_TOKENS",
    "TARGET_PIECE_TOKENS",
    "Block",
    "Chunk",
    "chunk_blocks",
    "compact_id",
    "query_terms",
    "terms",
]

TARGET_PIECE_TOKENS = 400
MAX_PIECE_TOKENS = 500
OVERLAP_TOKENS = 50
# A heading starts a new piece once the current one has this many tokens.
_MIN_TOKENS_BEFORE_HEADING = 200
# A heading repeated in front of a piece is cut to this many characters.
_HEADING_CHARS = 120
# Longer words are cut: Postgres refuses a lexeme over 2047 bytes, and no
# question names such a word anyway.
_MAX_WORD_CHARS = 100

_NO_SPACE = r"\p{Han}\p{Hiragana}\p{Katakana}\p{Thai}\p{Lao}\p{Khmer}\p{Myanmar}"
_TOKEN = regex.compile(rf"(?P<run>[{_NO_SPACE}]+)|(?P<word>[\p{{L}}\p{{N}}\p{{M}}]+)")
_LATIN = regex.compile(r"[\p{Latin}\p{N}\p{M}]+")
# The parts of an ID: letters and digits, apart by at most one of these.
_ID_PART = regex.compile(r"[\p{L}\p{N}]+")
_ID_SEPARATORS = frozenset(" -./_")
_ID_PART_MAX = 10
_ID_PARTS_MAX = 6
_ID_MAX = 32
# A query keeps at most this many distinct words (the SQL search runs one
# lookup per word).
MAX_QUERY_TERMS = 24


def _fold(word: str) -> str:
    if _LATIN.fullmatch(word):
        word = "".join(
            c
            for c in unicodedata.normalize("NFD", word)
            if not unicodedata.combining(c)
        )
    return word


def compact_id(text: str) -> str:
    """``text`` lowercased with spaces, dashes, dots, slashes and underscores
    removed: "NS-BAT 48V/100" is "nsbat48v100"."""
    return "".join(_ID_PART.findall(unicodedata.normalize("NFKC", text).casefold()))


def _id_terms(text: str) -> Iterable[str]:
    parts = list(_ID_PART.finditer(text))
    group: list[str] = []
    # gaps[i] is the separator before group[i + 1].
    gaps: list[str] = []
    for i, part in enumerate(parts):
        word = part.group()
        if len(word) > _ID_PART_MAX:
            yield from _id_runs(group, gaps)
            group, gaps = [], []
            continue
        if group:
            gap = text[parts[i - 1].end() : part.start()]
            if len(gap) != 1 or gap not in _ID_SEPARATORS:
                yield from _id_runs(group, gaps)
                group, gaps = [], []
            else:
                gaps.append(gap)
        group.append(word)
    yield from _id_runs(group, gaps)


def _id_runs(group: list[str], gaps: list[str]) -> Iterable[str]:
    """Every run of 2 to 6 neighbouring parts, joined, that has a digit or
    is joined by dashes, dots, slashes or underscores only ("Wi-Fi" gives
    "wifi", so "wifi" finds it)."""
    for start in range(len(group)):
        for end in range(start + 2, min(len(group), start + _ID_PARTS_MAX) + 1):
            joined = "".join(group[start:end])
            if len(joined) > _ID_MAX:
                break
            if any(c.isdigit() for c in joined) or " " not in gaps[start : end - 1]:
                yield joined


def terms(text: str) -> list[str]:
    """The search words of ``text``, in order, repeats kept (BM25 counts
    them). Every word is letters, digits and marks only."""
    text = unicodedata.normalize("NFKC", text).casefold()
    out: list[str] = []
    for match in _TOKEN.finditer(text):
        run = match.group("run")
        if run is not None:
            if len(run) == 1:
                out.append(run)
            else:
                out.extend(run[i : i + 2] for i in range(len(run) - 1))
        else:
            out.append(_fold(match.group("word"))[:_MAX_WORD_CHARS])
    out.extend(_id_terms(text))
    return out


def query_terms(text: str) -> list[str]:
    """The distinct search words of a question, at most ``MAX_QUERY_TERMS``,
    in order of first use."""
    return list(dict.fromkeys(terms(text)))[:MAX_QUERY_TERMS]


@dataclass(frozen=True)
class Block:
    """A paragraph (or heading) of a decoded document."""

    text: str
    page: int | None = None
    heading: bool = False


@dataclass(frozen=True)
class Chunk:
    """One piece to store: its text and where it starts."""

    text: str
    page: int | None = None
    csv_row: int | None = None


_SENTENCE_END = regex.compile(r"(?<=[.!?;:。！？；])\s+|(?<=[。！？])")


def _split_long(text: str, max_tokens: int, count: Callable[[str], int]) -> list[str]:
    """``text`` in parts of at most ``max_tokens``: by sentences, then by
    words, then by characters (scripts with no spaces)."""
    if count(text) <= max_tokens:
        return [text]
    parts: list[str] = []
    for sentence in (s for s in _SENTENCE_END.split(text) if s and s.strip()):
        if count(sentence) <= max_tokens:
            parts.append(sentence)
            continue
        words = sentence.split()
        if len(words) > 1:
            # Word counts add up (the tokenizers split on spaces first), so
            # each word is counted once: linear in the sentence's length.
            current: list[str] = []
            size = 0
            for word in words:
                cost = count(word)
                if cost > max_tokens:
                    if current:
                        parts.append(" ".join(current))
                        current, size = [], 0
                    parts.extend(_split_long(word, max_tokens, count))
                    continue
                if current and size + cost > max_tokens:
                    parts.append(" ".join(current))
                    current, size = [], 0
                current.append(word)
                size += cost
            if current:
                parts.append(" ".join(current))
            continue
        # One very long word or a run with no spaces: halve it.
        if len(sentence) < 2:
            parts.append(sentence)
            continue
        mid = len(sentence) // 2
        parts.extend(_split_long(sentence[:mid], max_tokens, count))
        parts.extend(_split_long(sentence[mid:], max_tokens, count))
    return parts


def _tail(text: str, max_tokens: int, count: Callable[[str], int]) -> str:
    """The end of ``text`` with at most ``max_tokens``, starting on a word
    where there are spaces."""
    if count(text) <= max_tokens:
        return text
    low, high = 0, len(text)
    while low < high:
        mid = (low + high) // 2
        if count(text[mid:]) <= max_tokens:
            high = mid
        else:
            low = mid + 1
    tail = text[low:]
    space = tail.find(" ")
    if 0 <= space < len(tail) // 2:
        tail = tail[space + 1 :]
    return tail


def chunk_blocks(
    blocks: Iterable[Block],
    count_tokens: Callable[[str], int],
    *,
    max_pieces: int | None = None,
    target: int = TARGET_PIECE_TOKENS,
    max_tokens: int = MAX_PIECE_TOKENS,
    overlap: int = OVERLAP_TOKENS,
) -> list[Chunk]:
    """Cut paragraphs into pieces of about ``target`` tokens, at most
    ``max_tokens``. A heading starts a new piece once the current one is
    long enough; a piece that does not start with its heading gets the
    last heading first, so it reads on its own. Each new piece repeats about
    ``overlap`` tokens of the end of the one before. Stops after
    ``max_pieces + 1`` pieces (the caller then refuses the file)."""
    chunks: list[Chunk] = []
    current: list[tuple[str, int | None]] = []
    tokens = 0
    # How many entries of ``current`` are the overlap carried from the piece
    # before (0 or 1): a piece of only that is never stored.
    carried = 0
    heading: str | None = None
    current_heading: str | None = None
    # Tokens the heading adds in front of a piece: counted in its budget, so
    # the stored piece (heading included) stays within ``max_tokens``.
    heading_cost = 0

    def set_heading(value: str | None) -> None:
        nonlocal current_heading, heading_cost
        current_heading = value
        heading_cost = count_tokens(value) + 1 if value else 0

    def flush(*, keep_overlap: bool) -> None:
        nonlocal current, tokens, carried
        body = "\n".join(t for t, _ in current).strip()
        if body:
            page = next((p for _, p in current if p is not None), None)
            text = body
            if current_heading and not body.startswith(current_heading):
                text = f"{current_heading}\n{body}"
            chunks.append(Chunk(text=text, page=page))
        carry = _tail(current[-1][0], overlap, count_tokens) if keep_overlap else ""
        last_page = current[-1][1] if current else None
        current = [(carry, last_page)] if carry else []
        carried = len(current)
        tokens = count_tokens(carry) if carry else 0
        set_heading(heading)

    for block in blocks:
        text = " ".join(block.text.split()) if not block.heading else block.text.strip()
        if not text:
            continue
        if block.heading:
            if tokens >= _MIN_TOKENS_BEFORE_HEADING and len(current) > carried:
                flush(keep_overlap=False)
            heading = text[:_HEADING_CHARS]
            if len(current) <= carried:
                current, carried, tokens = [], 0, 0
                set_heading(heading)
        budget = max_tokens - overlap - heading_cost
        for part in _split_long(text, max(budget, 1), count_tokens):
            size = count_tokens(part)
            if len(current) > carried and tokens + size + heading_cost > max_tokens:
                flush(keep_overlap=True)
                if max_pieces is not None and len(chunks) > max_pieces:
                    return chunks
            current.append((part, block.page))
            tokens += size
            if tokens + heading_cost >= target and not block.heading:
                flush(keep_overlap=True)
                if max_pieces is not None and len(chunks) > max_pieces:
                    return chunks
    if len(current) > carried:
        flush(keep_overlap=False)
    return chunks
