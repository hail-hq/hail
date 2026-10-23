"""One tool-only classification call shared by the pickup and menu checks."""

from __future__ import annotations

import asyncio
import json

from livekit.agents import APIConnectOptions
from livekit.agents.llm import LLM, ChatContext, function_tool
from livekit.agents.voice.amd import AMDCategory

# One try per classifier in ``amd.ClassifierFailover``, no retries.
CLASSIFIER_ATTEMPT_TIMEOUT_SECONDS = 3.0
# Provider request timeout. Google rejects a deadline under 10s.
CLASSIFIER_REQUEST_TIMEOUT_SECONDS = 10.0
# Time to open each classifier's stream, on top of its try.
STREAM_SETUP_MARGIN_SECONDS = 0.5


async def classify_speech(llm: LLM, prompt: str, *messages: str) -> AMDCategory | None:
    """The category the model saved, or None when it saved none.

    Provider errors, bad labels and timeouts raise. Callers log errors by
    type only, since provider error bodies can hold the transcript.

    A ``ClassifierFailover`` bounds the wait with its one chain deadline.
    Any other LLM (a customer's own) is capped at its request timeout here.
    """

    @function_tool
    async def save_prediction(label: AMDCategory) -> None:
        """Save the speech category."""

    ctx = ChatContext()
    ctx.add_message(role="system", content=prompt)
    for message in messages:
        ctx.add_message(role="user", content=message)
    # A ClassifierFailover has a ``deadline`` and enforces it itself.
    limit = None if hasattr(llm, "deadline") else CLASSIFIER_REQUEST_TIMEOUT_SECONDS
    async with (
        asyncio.timeout(limit),
        llm.chat(
            chat_ctx=ctx,
            tools=[save_prediction],
            tool_choice="required",
            conn_options=APIConnectOptions(
                max_retry=0, timeout=CLASSIFIER_REQUEST_TIMEOUT_SECONDS
            ),
        ) as stream,
    ):
        response = await stream.collect()
    for call in response.tool_calls:
        if call.name == "save_prediction":
            return AMDCategory(json.loads(call.arguments)["label"])
    return None
