"""Local tests for reading saved agent voice settings on the call path."""

from uuid import uuid4

import pytest
from hailhq.api.routes.calls import apply_agent_defaults
from hailhq.core.models import Agent
from hailhq.core.schemas import CallCreate


@pytest.mark.parametrize(
    ("voice_config", "voice_id"),
    [
        ({"language": "it", "voice_id": "legacy"}, "legacy"),
        ({"language": "it", "voice_id": "legacy", "voice_provider": None}, "legacy"),
        (
            {
                "language": "it",
                "scoped_voice_id": "selected",
                "voice_provider": "cartesia",
            },
            None,
        ),
    ],
)
def test_saved_agent_voice_settings_keep_outbound_calls_compatible(
    voice_config: dict, voice_id: str | None
) -> None:
    agent = Agent(system_prompt="Help the caller.", voice_config=voice_config.copy())
    request = CallCreate(to="+14155550123", agent_id=uuid4(), recipient_consent=True)

    body = apply_agent_defaults(request, agent)

    assert body.voice_config.language == "it"
    assert body.voice_config.voice_id == voice_id
    assert agent.voice_config == voice_config
