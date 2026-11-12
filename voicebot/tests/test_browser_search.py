"""Browser tools keep search and continuation parameters intact."""

import uuid
from unittest.mock import AsyncMock

import pytest
from hailhq.core.config import settings
from hailhq.voicebot import browser_tools


@pytest.mark.asyncio
async def test_browser_forwards_search_and_both_continuation_offsets(monkeypatch):
    monkeypatch.setattr(settings, "hail_internal_secret", "test")
    monkeypatch.setattr(browser_tools, "wrap_tool", lambda spec, ctx: spec)
    post = AsyncMock(return_value={"quoted_untrusted_content": "remaining content"})
    monkeypatch.setattr(browser_tools.BrowserApiClient, "post", post)
    tools, _ = browser_tools.build_browser_tools(
        uuid.uuid4(), uuid.uuid4(), {"inboundEmailReady": True}
    )
    spec = next(s for s in tools if s.name == "read_browser_inbox")
    args = {
        "email_id": str(uuid.uuid4()),
        "query": "receipt",
        "offset": 6000,
        "text_offset": 24000,
        "page": 4,
        "limit": 2,
    }
    await spec.execute(None, args)
    payload = post.await_args.args[1]
    for key, value in args.items():
        assert payload.get(key) == value
        assert key in spec.parameters["properties"]
