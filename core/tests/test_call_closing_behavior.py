"""Opt-in model checks: pytest --run-voice-evals tests/test_call_closing_behavior.py.

Uses the configured Bedrock conversation model and AWS credentials. No phone
calls are placed and no tools execute. Unlike prompt snapshots, these checks
exercise the model's decision to keep talking or hang up.
"""

import os

import anthropic
import pytest
from hailhq.core import prompts
from hailhq.core.agent_tools.end_call import SPEC

pytestmark = pytest.mark.voice_eval

TEST_CALL = (
    "You are Alex, Sam Taylor's AI assistant, making a short test call to "
    "Sam himself to check the new phone line. Ask him how the voice and audio "
    "sound, listen to his answer, thank him and end the call. Keep the whole "
    "call under 90 seconds."
)


@pytest.mark.parametrize(
    "direction,instructions,opening,reply,should_end,ask_question",
    [
        pytest.param(
            None,
            TEST_CALL,
            "How do my voice and audio sound?",
            "Sounds pretty good. Will you always introduce yourself as an AI "
            "assistant when you call?",
            False,
            True,
            id="test-follow-up",
        ),
        pytest.param(
            None,
            TEST_CALL,
            "How do my voice and audio sound?",
            "The audio sounds good.",
            False,
            True,
            id="feedback-is-not-goodbye",
        ),
        pytest.param(
            None,
            TEST_CALL,
            "Anything else you would like to test?",
            "Yes, can you speak a little slower?",
            False,
            False,
            id="continue-testing",
        ),
        pytest.param(
            None,
            TEST_CALL,
            "Anything else you would like to test?",
            "No, that is all. Goodbye.",
            True,
            False,
            id="test-finished",
        ),
        pytest.param(
            None,
            TEST_CALL,
            "How do my voice and audio sound?",
            "I have to go now. Goodbye.",
            True,
            False,
            id="stop-without-closing-question",
        ),
        pytest.param(
            None,
            "Call the dentist to book Sam a checkup tomorrow at ten.",
            "Can you book Sam a checkup tomorrow at ten?",
            "Yes, Sam is booked for tomorrow at ten. The booking is confirmed.",
            True,
            False,
            id="business-task-complete",
        ),
        pytest.param(
            None,
            "Call the dentist to book Sam a checkup tomorrow at ten. "
            "Sam has not given us his date of birth.",
            "Can you book Sam a checkup tomorrow at ten?",
            "The booking is confirmed. Can you also give me Sam's date of birth?",
            False,
            False,
            id="business-follow-up",
        ),
        pytest.param(
            "inbound",
            "Answer questions about our opening hours: nine to five.",
            "We are open from nine to five.",
            "Thank you.",
            False,
            True,
            id="inbound-thanks-is-not-goodbye",
        ),
    ],
)
async def test_call_closing_decision(
    direction, instructions, opening, reply, should_end, ask_question
) -> None:
    async with anthropic.AsyncAnthropicBedrock(
        aws_region=os.environ["ANTHROPIC_BEDROCK_REGION"], max_retries=0
    ) as client:
        response = await client.messages.create(
            model=os.environ["ANTHROPIC_MODEL"],
            max_tokens=350,
            system=prompts.build_voice_instructions(instructions, direction),
            messages=[
                {"role": "assistant", "content": opening},
                {"role": "user", "content": reply},
            ],
            tools=[
                {
                    "name": SPEC.name,
                    "description": SPEC.description,
                    "input_schema": SPEC.parameters,
                }
            ],
        )
    calls = [block.name for block in response.content if block.type == "tool_use"]
    speech = " ".join(block.text for block in response.content if block.type == "text")
    assert ("end_call" in calls) == should_end, speech
    if ask_question:
        assert "?" in speech, f"Expected an invitation to continue: {speech}"
