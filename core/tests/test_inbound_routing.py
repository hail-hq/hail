"""register/unregister: carrier hook, then LiveKit trunk, then the stamp."""

from __future__ import annotations

import dataclasses
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from hailhq.core import carrier_routing, inbound_routing
from hailhq.core.config import settings
from hailhq.core.models import PhoneNumber
from hailhq.core.providers.voice.base import CarrierNotConfigured
from livekit.api.twirp_client import TwirpError


@pytest.fixture()
def hooks(monkeypatch: pytest.MonkeyPatch) -> dict[str, AsyncMock]:
    monkeypatch.setattr(settings, "livekit_twilio_sip_inbound_trunk_id", "ST_in_tw")
    monkeypatch.setattr(settings, "livekit_sip_inbound_trunk_id", "")
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


def _conflict() -> TwirpError:
    return TwirpError("invalid_argument", "Conflicting inbound SIP Trunks", status=400)


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


async def test_unregister_detaches_then_removes_then_clears(
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


async def test_unregister_detaches_at_the_carrier_before_livekit(
    async_session, hooks
) -> None:
    order: list[str] = []
    lk = AsyncMock()
    number = _number()
    async_session.add(number)
    await async_session.flush()
    await inbound_routing.register(async_session, lk, number)
    hooks["detach"].side_effect = lambda *a: order.append("carrier")
    lk.remove_inbound_number.side_effect = lambda *a: order.append("livekit")
    await inbound_routing.unregister(async_session, lk, number)
    assert order == ["carrier", "livekit"]


async def test_failed_carrier_detach_leaves_livekit_and_row_untouched(
    async_session, hooks
) -> None:
    lk = AsyncMock()
    number = _number()
    async_session.add(number)
    await async_session.flush()
    await inbound_routing.register(async_session, lk, number)
    hooks["detach"].side_effect = RuntimeError("carrier 500")
    with pytest.raises(inbound_routing.InboundRoutingError) as exc_info:
        await inbound_routing.unregister(async_session, lk, number)
    assert exc_info.value.stage == "carrier_detach"
    lk.remove_inbound_number.assert_not_awaited()
    assert number.inbound_registered_at is not None


async def test_failed_livekit_remove_keeps_the_row_registered(
    async_session, hooks
) -> None:
    lk = AsyncMock()
    number = _number()
    async_session.add(number)
    await async_session.flush()
    await inbound_routing.register(async_session, lk, number)
    lk.remove_inbound_number.side_effect = RuntimeError("twirp 500")
    with pytest.raises(inbound_routing.InboundRoutingError) as exc_info:
        await inbound_routing.unregister(async_session, lk, number)
    assert exc_info.value.stage == "livekit_remove"
    assert number.inbound_registered_at is not None


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


async def test_unregister_also_clears_the_shared_trunk(
    async_session, hooks, monkeypatch
) -> None:
    """Cutover: a number registered before its carrier had its own trunk is
    still on the shared one; leaving it there would block its next register."""
    monkeypatch.setattr(settings, "livekit_sip_inbound_trunk_id", "ST_shared")
    lk = AsyncMock()
    number = _number()
    number.inbound_registered_at = datetime.now(timezone.utc)
    async_session.add(number)
    await async_session.flush()

    await inbound_routing.unregister(async_session, lk, number)

    assert [c.args for c in lk.remove_inbound_number.await_args_list] == [
        ("ST_in_tw", "+14155550100"),
        ("ST_shared", "+14155550100"),
    ]
    assert number.inbound_registered_at is None


async def test_shared_trunk_failure_does_not_block_unregister(
    async_session, hooks, monkeypatch, caplog
) -> None:
    """The carrier is already detached: the number is unregistered. The
    leftover entry is logged as an error and removed when a later register
    is refused for it."""
    monkeypatch.setattr(settings, "livekit_sip_inbound_trunk_id", "ST_shared")
    lk = AsyncMock()
    lk.remove_inbound_number.side_effect = [None, RuntimeError("timeout")]
    number = _number()
    number.inbound_registered_at = datetime.now(timezone.utc)
    async_session.add(number)
    await async_session.flush()

    with caplog.at_level("ERROR", logger="hailhq.core.inbound_routing"):
        await inbound_routing.unregister(async_session, lk, number)
    assert number.inbound_registered_at is None
    [record] = [r for r in caplog.records if r.levelname == "ERROR"]
    message = record.getMessage()
    assert "+14155550100: still listed on the shared inbound trunk ST_shared" in (
        message
    )
    assert "sip_trunks.py move --carrier twilio --yes" in message

    lk.remove_inbound_number.reset_mock(side_effect=True)
    lk.add_inbound_number.reset_mock()
    lk.add_inbound_number.side_effect = [_conflict(), None, None]
    await inbound_routing.register(async_session, lk, number)
    lk.remove_inbound_number.assert_awaited_once_with("ST_shared", "+14155550100")
    assert lk.add_inbound_number.await_args_list[1].args == (
        "ST_shared",
        inbound_routing.SHARED_TRUNK_PLACEHOLDER,
    )
    assert lk.add_inbound_number.await_count == 3
    assert number.inbound_registered_at is not None


async def test_register_leaves_the_shared_trunk_alone_when_the_add_works(
    async_session, hooks, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "livekit_sip_inbound_trunk_id", "ST_shared")
    lk = AsyncMock()
    number = _number()
    async_session.add(number)
    await async_session.flush()

    await inbound_routing.register(async_session, lk, number)

    lk.remove_inbound_number.assert_not_awaited()
    lk.add_inbound_number.assert_awaited_once_with("ST_in_tw", "+14155550100")


async def test_register_after_the_shared_trunk_is_deleted(
    async_session, hooks, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "livekit_sip_inbound_trunk_id", "ST_shared")
    lk = AsyncMock()
    gone = TwirpError("not_found", "trunk not found", status=404)
    # Carrier add refused, placeholder add on the deleted shared trunk, retry.
    lk.add_inbound_number.side_effect = [_conflict(), gone, None]
    lk.remove_inbound_number.side_effect = gone
    number = _number()
    async_session.add(number)
    await async_session.flush()

    await inbound_routing.register(async_session, lk, number)

    assert number.inbound_registered_at is not None


async def test_register_fails_when_the_shared_trunk_cannot_let_go(
    async_session, hooks, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "livekit_sip_inbound_trunk_id", "ST_shared")
    lk = AsyncMock()
    lk.add_inbound_number.side_effect = _conflict()
    lk.remove_inbound_number.side_effect = RuntimeError("timeout")
    number = _number()
    async_session.add(number)
    await async_session.flush()

    with pytest.raises(inbound_routing.InboundRoutingError) as err:
        await inbound_routing.register(async_session, lk, number)

    assert err.value.stage == "livekit_add"
    hooks["detach"].assert_awaited_once()
    assert number.inbound_registered_at is None


async def test_deleted_shared_trunk_does_not_block_unregister(
    async_session, hooks, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "livekit_sip_inbound_trunk_id", "ST_shared")
    lk = AsyncMock()
    lk.remove_inbound_number.side_effect = [
        None,
        TwirpError("not_found", "trunk not found", status=404),
    ]
    number = _number()
    number.inbound_registered_at = datetime.now(timezone.utc)
    async_session.add(number)
    await async_session.flush()

    await inbound_routing.unregister(async_session, lk, number)

    assert number.inbound_registered_at is None


async def test_unregister_skips_the_shared_trunk_when_it_is_the_carrier_trunk(
    async_session, hooks, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "livekit_sip_inbound_trunk_id", "ST_in_tw")
    lk = AsyncMock()
    number = _number()
    number.inbound_registered_at = datetime.now(timezone.utc)
    async_session.add(number)
    await async_session.flush()

    await inbound_routing.unregister(async_session, lk, number)

    lk.remove_inbound_number.assert_awaited_once_with("ST_in_tw", "+14155550100")


async def test_register_does_not_touch_the_shared_trunk_on_other_errors(
    async_session, hooks, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "livekit_sip_inbound_trunk_id", "ST_shared")
    lk = AsyncMock()
    lk.add_inbound_number.side_effect = TimeoutError()
    number = _number()
    async_session.add(number)
    await async_session.flush()

    with pytest.raises(inbound_routing.InboundRoutingError):
        await inbound_routing.register(async_session, lk, number)

    lk.remove_inbound_number.assert_not_awaited()
    assert lk.add_inbound_number.await_count == 1


async def test_shared_trunk_keeps_its_placeholder_before_a_number_leaves(
    async_session, hooks, monkeypatch
) -> None:
    """A trunk with no numbers matches every dialed number, so the shared
    trunk gets its placeholder before the last number leaves it."""
    monkeypatch.setattr(settings, "livekit_sip_inbound_trunk_id", "ST_shared")
    lk = AsyncMock()
    number = _number()
    number.inbound_registered_at = datetime.now(timezone.utc)
    async_session.add(number)
    await async_session.flush()

    await inbound_routing.unregister(async_session, lk, number)

    lk.add_inbound_number.assert_awaited_once_with(
        "ST_shared", inbound_routing.SHARED_TRUNK_PLACEHOLDER
    )


async def test_other_invalid_argument_does_not_touch_the_shared_trunk(
    async_session, hooks, monkeypatch, caplog
) -> None:
    monkeypatch.setattr(settings, "livekit_sip_inbound_trunk_id", "ST_shared")
    lk = AsyncMock()
    refused = TwirpError("invalid_argument", "bad number", status=400)
    lk.add_inbound_number.side_effect = refused
    lk.inbound_trunk_numbers.return_value = [
        inbound_routing.SHARED_TRUNK_PLACEHOLDER,
        "+14155550199",
    ]
    number = _number()
    async_session.add(number)
    await async_session.flush()

    with (
        caplog.at_level("ERROR", logger="hailhq.core.inbound_routing"),
        pytest.raises(inbound_routing.InboundRoutingError) as err,
    ):
        await inbound_routing.register(async_session, lk, number)

    assert err.value.__cause__ is refused
    lk.inbound_trunk_numbers.assert_awaited_once_with("ST_shared")
    lk.remove_inbound_number.assert_not_awaited()
    assert lk.add_inbound_number.await_count == 1
    [record] = [r for r in caplog.records if r.levelname == "ERROR"]
    assert "invalid_argument: bad number" in record.getMessage()


async def test_conflict_with_new_wording_is_found_by_the_shared_trunk_list(
    async_session, hooks, monkeypatch
) -> None:
    """LiveKit may reword the conflict message: a number the shared trunk
    lists is still a leftover to remove."""
    monkeypatch.setattr(settings, "livekit_sip_inbound_trunk_id", "ST_shared")
    lk = AsyncMock()
    lk.add_inbound_number.side_effect = [
        TwirpError("invalid_argument", "number is on trunk ST_shared", status=400),
        None,  # the shared placeholder
        None,  # the retry
    ]
    lk.inbound_trunk_numbers.return_value = [
        inbound_routing.SHARED_TRUNK_PLACEHOLDER,
        "+14155550100",
    ]
    number = _number()
    async_session.add(number)
    await async_session.flush()

    await inbound_routing.register(async_session, lk, number)

    lk.inbound_trunk_numbers.assert_awaited_once_with("ST_shared")
    lk.remove_inbound_number.assert_awaited_once_with("ST_shared", "+14155550100")
    assert lk.add_inbound_number.await_args_list[-1].args == (
        "ST_in_tw",
        "+14155550100",
    )
    assert number.inbound_registered_at is not None


async def test_known_conflict_text_needs_no_shared_trunk_read(
    async_session, hooks, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "livekit_sip_inbound_trunk_id", "ST_shared")
    lk = AsyncMock()
    lk.add_inbound_number.side_effect = [_conflict(), None, None]
    number = _number()
    async_session.add(number)
    await async_session.flush()

    await inbound_routing.register(async_session, lk, number)

    lk.inbound_trunk_numbers.assert_not_awaited()
    assert number.inbound_registered_at is not None


async def test_unreadable_shared_trunk_keeps_the_original_error(
    async_session, hooks, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "livekit_sip_inbound_trunk_id", "ST_shared")
    lk = AsyncMock()
    refused = TwirpError("invalid_argument", "something else", status=400)
    lk.add_inbound_number.side_effect = refused
    lk.inbound_trunk_numbers.side_effect = LookupError("no LiveKit inbound trunk")
    number = _number()
    async_session.add(number)
    await async_session.flush()

    with pytest.raises(inbound_routing.InboundRoutingError) as err:
        await inbound_routing.register(async_session, lk, number)

    assert err.value.__cause__ is refused
    lk.remove_inbound_number.assert_not_awaited()
