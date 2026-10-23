"""One tool-only classification call shared by the pickup and menu checks."""

from __future__ import annotations

import asyncio
import json

from livekit.agents import APIConnectOptions
from livekit.agents.llm import LLM, ChatContext, function_tool
from livekit.agents.voice.amd import AMDCategory

# Provider request timeout. Google rejects a deadline under 10s.
CLASSIFIER_REQUEST_TIMEOUT_SECONDS = 10.0
# Total for one check: the first classifier gets its 3s try
# (``amd.CLASSIFIER_ATTEMPT_TIMEOUT_SECONDS``), the next one gets the rest.
# ``amd.ClassifierFailover`` applies it to the house checkers, AMD included;
# ``classify_speech`` applies it to a customer's own LLM too.
CLASSIFY_TIMEOUT_SECONDS = 5.0


async def classify_speech(llm: LLM, prompt: str, *messages: str) -> AMDCategory | None:
    """The category the model saved, or None when it saved none.

    Provider errors, bad labels and timeouts raise. Callers log errors by
    type only, since provider error bodies can hold the transcript. The
    whole check is capped at ``CLASSIFY_TIMEOUT_SECONDS``.
    """

    @function_tool
    async def save_prediction(label: AMDCategory) -> None:
        """Save the speech category."""

    ctx = ChatContext()
    ctx.add_message(role="system", content=prompt)
    for message in messages:
        ctx.add_message(role="user", content=message)
    async with (
        asyncio.timeout(CLASSIFY_TIMEOUT_SECONDS),
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
