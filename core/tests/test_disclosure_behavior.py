"""Opt-in spoken settings checks; enable with pytest --run-voice-evals."""

import os

import anthropic
import pytest
from hailhq.core import prompts
from hailhq.core.agent_tools import hail_help

pytestmark = pytest.mark.voice_eval


@pytest.mark.parametrize(
    "question,expected",
    [
        (
            (
                "Will you always announce you are AI? How do I switch that off "
                "for my saved Hail agent? Give me the exact control label."
            ),
            ("agents", "first line", "say this first on every call", "save"),
        ),
        (
            (
                "I am setting up a one-off call through my connected assistant. "
                "How do I disable the automatic AI announcement for just that call?"
            ),
            ("false",),
        ),
        (
            "Where do I edit the workspace AI introduction wording in Hail?",
            ("calls", "call settings", "ai line", "save"),
        ),
    ],
    ids=["saved-agent-toggle", "one-off-override", "workspace-wording"],
)
async def test_disclosure_settings_answer(question, expected) -> None:
    spec = hail_help.SPEC
    messages = [{"role": "user", "content": question}]
    async with anthropic.AsyncAnthropicBedrock(
        aws_region=os.environ["ANTHROPIC_BEDROCK_REGION"], max_retries=0
    ) as client:
        kwargs = {
            "model": os.environ["ANTHROPIC_MODEL"],
            "max_tokens": 500,
            "system": prompts.build_voice_instructions(
                "Answer the other person's Hail settings questions.", "inbound"
            ),
            "tools": [
                {
                    "name": spec.name,
                    "description": spec.description,
                    "input_schema": spec.parameters,
                }
            ],
        }
        response = await client.messages.create(messages=messages, **kwargs)
        calls = [block for block in response.content if block.type == "tool_use"]
        assert calls, "The agent must look up current settings before answering"
        assert all(block.name == spec.name for block in calls)
        messages.extend(
            [
                {"role": "assistant", "content": response.content},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": await hail_help._execute(None, {}),
                        }
                        for block in calls
                    ],
                },
            ]
        )
        answer = await client.messages.create(messages=messages, **kwargs)
    speech = " ".join(block.text for block in answer.content if block.type == "text")
    for term in expected:
        assert term in speech.lower(), speech
