"""Opt-in model checks: pytest --run-voice-evals tests/test_clock_and_name_behavior.py.

Uses the configured Bedrock conversation model and AWS credentials. No phone
calls are placed and no tools execute. Checks that the agent uses the date and
time section, and that it writes no comma before a name.
"""

import os
import re
from datetime import datetime, timezone

import anthropic
import pytest
from hailhq.core import prompts

pytestmark = pytest.mark.voice_eval

FRONT_DESK = (
    "You answer the phone for Northgate Dental. We are open 9am to 5pm, "
    "London time, Monday to Friday. Take the caller's name first."
)


def _instructions(started_at: datetime) -> str:
    clock = prompts.clock_section(
        started_at,
        inbound=True,
        business_number="+442079460000",
        other_number="+447911123456",
    )
    return prompts.build_voice_instructions(FRONT_DESK, "inbound", other_party=clock)


async def _reply(system: str, caller: str) -> str:
    async with anthropic.AsyncAnthropicBedrock(
        aws_region=os.environ["ANTHROPIC_BEDROCK_REGION"], max_retries=0
    ) as client:
        response = await client.messages.create(
            model=os.environ["ANTHROPIC_MODEL"],
            max_tokens=300,
            system=system,
            messages=[
                {"role": "user", "content": "Hello?"},
                {
                    "role": "assistant",
                    "content": "Hi, Northgate Dental, you're speaking to our AI "
                    "assistant. Can I take your name?",
                },
                {"role": "user", "content": caller},
            ],
        )
    return " ".join(block.text for block in response.content if block.type == "text")


async def _speech(system: str, caller: str) -> str:
    """A reply with words: the model now and then returns an empty turn."""
    return await _reply(system, caller) or await _reply(system, caller)


@pytest.mark.parametrize(
    "started_at,is_open",
    [
        # Wednesday 20:05 in London: closed.
        pytest.param(datetime(2026, 11, 18, 20, 5, tzinfo=timezone.utc), False),
        # Wednesday 10:00 in London: open.
        pytest.param(datetime(2026, 11, 18, 10, 0, tzinfo=timezone.utc), True),
    ],
)
async def test_agent_knows_whether_the_office_is_open(started_at, is_open) -> None:
    speech = await _speech(
        _instructions(started_at),
        "It's Sam. Is the office open right now? Start your answer with yes or no.",
    )
    # The caller asks for yes or no: the answer leads the reply.
    answer = re.search(r"\b(yes|no)\b", speech, re.IGNORECASE)
    assert answer is not None, speech
    assert (answer.group(1).lower() == "yes") == is_open, speech


@pytest.mark.parametrize("attempt", range(3))
async def test_no_comma_before_the_callers_name(attempt) -> None:
    speech = await _speech(
        _instructions(datetime(2026, 11, 18, 10, 0, tzinfo=timezone.utc)),
        "Yeah, this is Sam Lee. I'd like to book a checkup.",
    )
    assert "Sam" in speech, speech
    assert not re.search(r",\s+Sam\b", speech), speech
