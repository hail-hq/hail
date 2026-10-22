"""One tool-only classification call shared by the pickup and menu checks."""

from __future__ import annotations

import json

from livekit.agents import APIConnectOptions
from livekit.agents.llm import LLM, ChatContext, function_tool
from livekit.agents.voice.amd import AMDCategory


async def classify_speech(llm: LLM, prompt: str, *messages: str) -> AMDCategory | None:
    """The category the model saved, or None when it saved none.

    Provider errors and bad labels raise. Callers bound the wait and log
    errors by type only, since provider error bodies can hold the transcript.
    """

    @function_tool
    async def save_prediction(label: AMDCategory) -> None:
        """Save the speech category."""

    ctx = ChatContext()
    ctx.add_message(role="system", content=prompt)
    for message in messages:
        ctx.add_message(role="user", content=message)
    async with llm.chat(
        chat_ctx=ctx,
        tools=[save_prediction],
        tool_choice="required",
        # Google requires a >=10s provider deadline.
        conn_options=APIConnectOptions(max_retry=0, timeout=10.0),
    ) as stream:
        response = await stream.collect()
    for call in response.tool_calls:
        if call.name == "save_prediction":
            return AMDCategory(json.loads(call.arguments)["label"])
    return None
