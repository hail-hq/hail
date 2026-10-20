"""Anthropic on Amazon Bedrock: the client the voicebot hands its Anthropic
LLM when ``ANTHROPIC_BEDROCK_REGION`` is set."""

from __future__ import annotations

from typing import Any

import anthropic

__all__ = ["bedrock_client", "is_bedrock_client"]


def bedrock_client(region: str) -> anthropic.AsyncAnthropicBedrock:
    """A Bedrock client signed with the AWS credential chain. No retries: the
    voicebot's fallback chain moves to the next model instead."""
    return anthropic.AsyncAnthropicBedrock(aws_region=region, max_retries=0)


def is_bedrock_client(client: Any) -> bool:
    return isinstance(client, anthropic.AsyncAnthropicBedrock)
