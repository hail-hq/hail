"""Read long spelled numbers in groups, the way people say phone numbers.

The prompt wraps codes in ``<spell>...</spell>`` (core prompts, Output rules).
A run of digits in one tag is read as one fast stream, so callers ask to hear
it again. A digit-only run longer than ``MAX_GROUP`` becomes groups of three,
the last group two to four digits, one tag each: 2015550134 is read as
201, 555, 0134. Codes with letters or separators are left as written.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterable, AsyncIterator

OPEN, CLOSE = "<spell>", "</spell>"
MAX_GROUP = 4
# An open tag held longer than this is not waited for: speech goes on.
MAX_HELD = 64
_DIGIT_TAG = re.compile(rf"<spell>(\d{{{MAX_GROUP + 1},}})</spell>")


def digit_groups(digits: str) -> list[str]:
    """``digits`` in groups of three; the last group has two to four."""
    if len(digits) <= MAX_GROUP:
        return [digits]
    groups = [digits[i : i + 3] for i in range(0, len(digits), 3)]
    if len(groups[-1]) == 1:
        last = groups.pop()
        groups[-1] += last
    return groups


def group_spelled_digits(text: str) -> str:
    return _DIGIT_TAG.sub(
        lambda m: ", ".join(f"{OPEN}{g}{CLOSE}" for g in digit_groups(m.group(1))),
        text,
    )


def _ready(buf: str) -> int:
    """How much of ``buf`` can go out now: an open tag waits for its close,
    and a tail that may begin a tag waits for the next chunk."""
    opened = buf.rfind(OPEN)
    if opened != -1 and buf.find(CLOSE, opened) == -1:
        return opened if len(buf) - opened <= MAX_HELD else len(buf)
    for size in range(min(len(OPEN) - 1, len(buf)), 0, -1):
        if OPEN.startswith(buf[-size:]):
            return len(buf) - size
    return len(buf)


async def group_spelled_stream(source: AsyncIterable[str]) -> AsyncIterator[str]:
    """Streaming :func:`group_spelled_digits`; plain text is not delayed."""
    buf = ""
    async for chunk in source:
        buf += chunk
        cut = _ready(buf)
        if cut:
            yield group_spelled_digits(buf[:cut])
            buf = buf[cut:]
    if buf:
        yield group_spelled_digits(buf)
