"""send_dtmf: RFC 4733 codes, validation, ordering, and missing handle."""

from __future__ import annotations

import uuid

import pytest
from hailhq.core.agent_tools.send_dtmf import (
    DTMF_CODES,
    MAX_DIGITS,
    SPEC,
    normalize_digits,
)
from hailhq.core.agent_tools.spec import ToolContext


@pytest.fixture(autouse=True)
def _no_pacing(monkeypatch):
    """Collapse the 0.3s inter-digit delay so the suite stays fast."""
    import hailhq.core.agent_tools.send_dtmf as mod

    monkeypatch.setattr(mod, "DIGIT_DELAY_SECONDS", 0)


def _ctx(send_dtmf):
    return ToolContext(
        call_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        api=None,
        hangup=None,
        send_dtmf=send_dtmf,
    )


def test_rfc4733_code_mapping() -> None:
    expected = {
        "0": 0,
        "1": 1,
        "2": 2,
        "3": 3,
        "4": 4,
        "5": 5,
        "6": 6,
        "7": 7,
        "8": 8,
        "9": 9,
        "*": 10,
        "#": 11,
        "A": 12,
        "B": 13,
        "C": 14,
        "D": 15,
    }
    assert DTMF_CODES == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("123#", "123#"),
        ("*0", "*0"),
        ("  42  ", "42"),
        ("abcd", "ABCD"),
    ],
)
def test_normalize_accepts_legal_digits(raw: str, expected: str) -> None:
    assert normalize_digits(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "12 34",
        "1,2",
        "hello",
        "1" * (MAX_DIGITS + 1),
        None,
        123,
    ],
)
def test_normalize_rejects_illegal_input(raw: object) -> None:
    assert normalize_digits(raw) is None


async def test_publishes_digits_in_order() -> None:
    pressed: list[str] = []

    async def _send(digit: str) -> None:
        pressed.append(digit)

    spoken = await SPEC.execute(_ctx(_send), {"digits": "4321#"})

    assert pressed == ["4", "3", "2", "1", "#"]
    assert isinstance(spoken, str) and spoken


async def test_lowercase_letters_are_upcased_before_publishing() -> None:
    pressed: list[str] = []

    async def _send(digit: str) -> None:
        pressed.append(digit)

    await SPEC.execute(_ctx(_send), {"digits": "a*d"})

    assert pressed == ["A", "*", "D"]


@pytest.mark.parametrize("digits", ["", "12 34", "1" * (MAX_DIGITS + 1), None])
async def test_rejects_return_speakable_text_without_publishing(digits: object) -> None:
    pressed: list[str] = []

    async def _send(digit: str) -> None:  # pragma: no cover — must not run
        pressed.append(digit)

    spoken = await SPEC.execute(_ctx(_send), {"digits": digits})

    assert pressed == []
    assert isinstance(spoken, str) and spoken
    # A speakable sentence, not a stack trace or a schema error.
    assert "Error" not in spoken and "Traceback" not in spoken


async def test_missing_handle_returns_speakable_line() -> None:
    spoken = await SPEC.execute(_ctx(None), {"digits": "1"})
    assert isinstance(spoken, str) and spoken


async def test_is_available_is_unconditional() -> None:
    assert await SPEC.is_available(uuid.uuid4(), None) is True  # type: ignore[arg-type]
