"""register/unregister: carrier hook, then LiveKit trunk, then the stamp."""

from __future__ import annotations

import dataclasses
import uuid
from unittest.mock import AsyncMock

import pytest
from hailhq.core import carrier_routing, inbound_routing
from hailhq.core.config import settings
from hailhq.core.models import PhoneNumber
from hailhq.core.providers.voice.base import CarrierNotConfigured


@pytest.fixture()
def hooks(monkeypatch: pytest.MonkeyPatch) -> dict[str, AsyncMock]:
    monkeypatch.setattr(settings, "livekit_twilio_sip_inbound_trunk_id", "ST_in_tw")
    attach, detach = AsyncMock(), AsyncMock()
    monkeypatch.setitem(
        carrier_routing.CARRIERS,
        "twilio",
        dataclasses.replace(
            carrier_routing.CARRIERS["twilio"],
            attach_inbound=attach,
            detach_inbound=detach,
        ),
    )
    return {"attach": attach, "detach": detach}


def _number() -> PhoneNumber:
    return PhoneNumber(
        organization_id=uuid.uuid4(),
        e164="+14155550100",
        country_code="US",
        number_type="local",
        provider="twilio",
        provider_resource_id="PN1",
        provisioning_state="active",
    )


async def test_register_attaches_then_adds_then_stamps(async_session, hooks) -> None:
    lk = AsyncMock()
    number = _number()
    async_session.add(number)
    await async_session.flush()

    await inbound_routing.register(async_session, lk, number)

    hooks["attach"].assert_awaited_once_with("PN1", "+14155550100")
    lk.add_inbound_number.assert_awaited_once_with("ST_in_tw", "+14155550100")
    assert number.inbound_registered_at is not None

    # Idempotent: a second call touches nothing.
    await inbound_routing.register(async_session, lk, number)
    assert hooks["attach"].await_count == 1
    assert lk.add_inbound_number.await_count == 1


async def test_unregister_removes_then_detaches_then_clears(
    async_session, hooks
) -> None:
    lk = AsyncMock()
    number = _number()
    async_session.add(number)
    await async_session.flush()
    await inbound_routing.register(async_session, lk, number)

    await inbound_routing.unregister(async_session, lk, number)

    lk.remove_inbound_number.assert_awaited_once_with("ST_in_tw", "+14155550100")
    hooks["detach"].assert_awaited_once_with("PN1", "+14155550100")
    assert number.inbound_registered_at is None
    await inbound_routing.unregister(async_session, lk, number)
    assert hooks["detach"].await_count == 1


async def test_carrier_failure_leaves_number_unregistered(async_session, hooks) -> None:
    hooks["attach"].side_effect = CarrierNotConfigured(
        "TWILIO_SIP_TRUNK_SID is not set"
    )
    lk = AsyncMock()
    number = _number()
    async_session.add(number)
    await async_session.flush()

    with pytest.raises(inbound_routing.InboundRoutingError) as exc_info:
        await inbound_routing.register(async_session, lk, number)
    assert exc_info.value.stage == "carrier_attach"
    assert exc_info.value.config is True
    lk.add_inbound_number.assert_not_awaited()
    assert number.inbound_registered_at is None


async def test_livekit_failure_reports_its_stage(async_session, hooks) -> None:
    lk = AsyncMock()
    lk.add_inbound_number.side_effect = RuntimeError("twirp 500")
    number = _number()
    async_session.add(number)
    await async_session.flush()

    with pytest.raises(inbound_routing.InboundRoutingError) as exc_info:
        await inbound_routing.register(async_session, lk, number)
    assert exc_info.value.stage == "livekit_add"
    assert exc_info.value.config is False
    assert number.inbound_registered_at is None


async def test_missing_trunk_is_a_config_error(
    async_session, hooks, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "livekit_twilio_sip_inbound_trunk_id", "")
    monkeypatch.setattr(settings, "livekit_sip_inbound_trunk_id", "")
    number = _number()
    async_session.add(number)
    await async_session.flush()
    with pytest.raises(inbound_routing.InboundRoutingError) as exc_info:
        await inbound_routing.register(async_session, AsyncMock(), number)
    assert exc_info.value.config is True
    hooks["attach"].assert_not_awaited()


async def test_livekit_failure_undoes_the_carrier_attach(async_session, hooks) -> None:
    lk = AsyncMock()
    lk.add_inbound_number.side_effect = RuntimeError("twirp 500")
    number = _number()
    async_session.add(number)
    await async_session.flush()
    with pytest.raises(inbound_routing.InboundRoutingError):
        await inbound_routing.register(async_session, lk, number)
    hooks["detach"].assert_awaited_once_with("PN1", "+14155550100")
