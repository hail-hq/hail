import inspect
from datetime import timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from hailhq.core import carrier_routing, number_offers
from hailhq.core.carrier_offer import CarrierOffer
from hailhq.core.carrier_routing import CARRIERS, DIDWW, TELNYX, TWILIO, carrier
from hailhq.core.config import settings
from hailhq.core.providers.voice import CarrierNotConfigured


@pytest.fixture(autouse=True)
def trunks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "livekit_twilio_sip_outbound_trunk_id", "ST_twilio")
    monkeypatch.setattr(settings, "livekit_telnyx_sip_outbound_trunk_id", "ST_telnyx")
    monkeypatch.setattr(settings, "telnyx_sip_username", "hail-sip")
    monkeypatch.setattr(settings, "livekit_didww_sip_outbound_trunk_id", "ST_didww")


def test_each_carrier_uses_its_own_trunk() -> None:
    assert carrier_routing.voice_route("twilio") == ("ST_twilio", {})
    assert carrier_routing.voice_route("telnyx") == (
        "ST_telnyx",
        {"X-Telnyx-Username": "hail-sip"},
    )
    assert carrier_routing.voice_route("didww") == ("ST_didww", {})


@pytest.mark.parametrize(
    ("provider", "setting"),
    [
        ("twilio", "livekit_twilio_sip_outbound_trunk_id"),
        ("telnyx", "livekit_telnyx_sip_outbound_trunk_id"),
        ("telnyx", "telnyx_sip_username"),
        ("didww", "livekit_didww_sip_outbound_trunk_id"),
    ],
)
def test_missing_trunk_is_an_error(
    monkeypatch: pytest.MonkeyPatch, provider: str, setting: str
) -> None:
    monkeypatch.setattr(settings, setting, "")
    with pytest.raises(ValueError, match=setting.upper()):
        carrier_routing.voice_route(provider)


def test_unknown_carrier_is_an_error() -> None:
    with pytest.raises(ValueError, match="Unsupported"):
        carrier_routing.voice_route("vonage")


def test_didww_sells_no_sms_through_hail() -> None:
    with pytest.raises(ValueError, match="cannot send SMS"):
        carrier_routing.sms_route("didww")
    with pytest.raises(ValueError, match="cannot send SMS"):
        carrier_routing.sms_status_path("didww")
    assert carrier_routing.sms_status_path("twilio") == "sms/status"
    assert carrier_routing.sms_status_path("telnyx") == "sms/telnyx"


def test_inbound_trunk_is_shared_unless_overridden(monkeypatch) -> None:
    # config.py folds LIVEKIT_SIP_INBOUND_TRUNK_ID into the per-carrier
    # attributes at load time; the registry reads the folded values.
    for name in ("twilio", "telnyx", "didww"):
        monkeypatch.setattr(settings, f"livekit_{name}_sip_inbound_trunk_id", "ST_in")
    assert carrier_routing.inbound_trunk("twilio") == "ST_in"
    assert carrier_routing.inbound_trunk("telnyx") == "ST_in"
    assert carrier_routing.inbound_trunk("didww") == "ST_in"
    monkeypatch.setattr(settings, "livekit_didww_sip_inbound_trunk_id", "ST_in_dw")
    assert carrier_routing.inbound_trunk("didww") == "ST_in_dw"


def test_missing_inbound_trunk_names_the_shared_key(monkeypatch) -> None:
    monkeypatch.setattr(settings, "livekit_twilio_sip_inbound_trunk_id", "")
    with pytest.raises(ValueError, match="LIVEKIT_SIP_INBOUND_TRUNK_ID"):
        carrier_routing.inbound_trunk("twilio")


def test_every_carrier_has_inbound_hooks() -> None:
    for entry in carrier_routing.CARRIERS.values():
        assert callable(entry.inbound_trunk)
        assert callable(entry.attach_inbound)
        assert callable(entry.detach_inbound)


def test_pending_timeout_per_carrier() -> None:
    assert carrier(TWILIO).pending_timeout == timedelta(hours=2)
    assert carrier(TELNYX).pending_timeout == timedelta(hours=2)
    assert carrier(DIDWW).pending_timeout == timedelta(days=7)


def test_poll_interval_per_carrier() -> None:
    assert carrier(TWILIO).poll_interval == timedelta(seconds=15)
    assert carrier(TELNYX).poll_interval == timedelta(seconds=15)
    assert carrier(DIDWW).poll_interval == timedelta(minutes=15)


def test_didww_purchase_completes_later() -> None:
    assert carrier(DIDWW).async_orders is True


# -- one interface for every carrier ---------------------------------------

VOICE = "hailhq.core.providers.voice"


def _offer(provider: str) -> CarrierOffer:
    return CarrierOffer(
        provider=provider,
        e164="+351300000001",
        country_code="PT",
        number_type="national",
        capabilities=["voice"],
        monthly_cents=350,
        setup_cents=350,
        readiness="ready",
        verification_id="ver-1",
        address_id="addr-1",
    )


@pytest.mark.parametrize("name", list(CARRIERS))
def test_every_carrier_has_the_whole_interface(name: str) -> None:
    entry = carrier(name)
    for fn in (entry.offers, entry.place_order, entry.order_outcome, entry.release):
        assert inspect.iscoroutinefunction(fn)


def test_every_listed_carrier_is_asked_for_offers() -> None:
    assert number_offers.PROVIDERS == tuple(CARRIERS)
    assert tuple(CARRIERS) == ("twilio", "telnyx", "didww")


@pytest.mark.parametrize(
    ("name", "inner", "args"),
    [
        (TWILIO, "twilio.twilio_offers", {}),
        (TELNYX, "telnyx.telnyx_offers", {}),
        (DIDWW, "didww.didww_offers", {}),
    ],
)
async def test_offers_reach_the_carrier(monkeypatch, name, inner, args) -> None:
    found = AsyncMock(return_value=[_offer(name)])
    monkeypatch.setattr(f"{VOICE}.{inner}", found)
    org = uuid4()
    offers = await carrier(name).offers(
        org, "PT", "national", ["voice"], "+351300000001"
    )
    assert offers == [_offer(name)]
    assert found.await_args.args[:4] == (org, "PT", "national", ["voice"])
    assert found.await_args.kwargs["e164"] == "+351300000001"
    await carrier(name).offers(org, "US", "local", ["voice"], None, "480")
    assert found.await_args.kwargs["area_code"] == "480"
    await carrier(name).offers(
        org, "US", "local", ["voice"], None, None, "Phoenix", "AZ"
    )
    assert found.await_args.kwargs["city"] == "Phoenix"
    assert found.await_args.kwargs["region"] == "AZ"


async def test_place_order_reaches_each_carrier(monkeypatch) -> None:
    number_id = uuid4()
    twilio = AsyncMock(return_value="PN1")
    telnyx = AsyncMock(return_value="order-t")
    didww = AsyncMock(return_value="order-d")
    monkeypatch.setattr(f"{VOICE}.twilio.purchase_ordered_number", twilio)
    monkeypatch.setattr(f"{VOICE}.telnyx.place_number_order", telnyx)
    monkeypatch.setattr(f"{VOICE}.didww.place_didww_order", didww)
    assert await carrier(TWILIO).place_order(number_id, _offer(TWILIO)) == "PN1"
    assert await carrier(TELNYX).place_order(number_id, _offer(TELNYX)) == "order-t"
    assert await carrier(DIDWW).place_order(number_id, _offer(DIDWW)) == "order-d"
    twilio.assert_awaited_once_with("+351300000001", number_id, "ver-1")
    telnyx.assert_awaited_once_with(number_id, "+351300000001", "ver-1", ["voice"])
    didww.assert_awaited_once_with(number_id, "+351300000001", "addr-1")


async def test_order_outcome_reaches_each_carrier(monkeypatch) -> None:
    number_id = uuid4()
    telnyx = AsyncMock(return_value=("pending", None, "order-t"))
    didww = AsyncMock(return_value=("pending", "did-1", "order-d"))
    monkeypatch.setattr(f"{VOICE}.telnyx.telnyx_order_outcome", telnyx)
    monkeypatch.setattr(f"{VOICE}.didww.didww_order_outcome", didww)
    e164 = "+351300000001"
    assert await carrier(TELNYX).order_outcome(
        e164, number_id, "order-t", _offer(TELNYX)
    ) == ("pending", None, "order-t")
    assert await carrier(DIDWW).order_outcome(
        e164, number_id, "order-d", _offer(DIDWW)
    ) == ("pending", "did-1", "order-d")
    telnyx.assert_awaited_once_with(e164, number_id, "order-t")
    didww.assert_awaited_once_with(e164, number_id, "order-d", "addr-1")


@pytest.mark.parametrize(
    ("sid", "outcome"),
    [("PN1", ("active", "PN1", None)), (None, ("missing", None, None))],
)
async def test_twilio_order_outcome(monkeypatch, sid, outcome) -> None:
    number_id = uuid4()
    find = AsyncMock(return_value=sid)
    monkeypatch.setattr(f"{VOICE}.twilio.find_ordered_number", find)
    assert (
        await carrier(TWILIO).order_outcome(
            "+351300000001", number_id, None, _offer(TWILIO)
        )
        == outcome
    )
    find.assert_awaited_once_with("+351300000001", number_id)


async def test_release_reaches_each_carrier(monkeypatch) -> None:
    for name, inner in (
        (TWILIO, "twilio.release_twilio_number"),
        (TELNYX, "telnyx.release_telnyx_number"),
        (DIDWW, "didww.release_didww_number"),
    ):
        release = AsyncMock()
        monkeypatch.setattr(f"{VOICE}.{inner}", release)
        await carrier(name).release("res-1")
        release.assert_awaited_once_with("res-1")


async def test_twilio_release_without_credentials(monkeypatch) -> None:
    monkeypatch.setattr(settings, "twilio_account_sid", "")
    monkeypatch.setattr(settings, "twilio_auth_token", "")
    with pytest.raises(CarrierNotConfigured):
        await carrier(TWILIO).release("PN1")


async def test_only_didww_takes_a_registration_back(monkeypatch) -> None:
    assert carrier(TWILIO).revoke_registration is None
    assert carrier(TELNYX).revoke_registration is None
    revoke = AsyncMock(return_value="Document is blurry")
    monkeypatch.setattr(f"{VOICE}.didww.revoke_registration", revoke)
    org = uuid4()
    assert await carrier(DIDWW).revoke_registration(_offer(DIDWW), org) == (
        "Document is blurry"
    )
    revoke.assert_awaited_once_with("addr-1", org, "PT", "national")


def test_first_listed_carrier_wins_a_tie() -> None:
    ranked = number_offers.rank_offers([_offer(DIDWW), _offer(TELNYX), _offer(TWILIO)])
    assert ranked[0].provider == "twilio"


def test_each_sms_carrier_builds_its_own_client(monkeypatch) -> None:
    from hailhq.core.providers.sms.telnyx import TelnyxSmsProvider
    from hailhq.core.providers.sms.twilio import LazyTwilioSmsProvider

    monkeypatch.setattr(settings, "telnyx_api_key", "key")
    monkeypatch.setattr(settings, "telnyx_public_key", "public")
    twilio_sms = carrier_routing.sms_route(TWILIO)
    assert isinstance(twilio_sms, LazyTwilioSmsProvider)
    # One Twilio client per process.
    assert carrier_routing.sms_route(TWILIO) is twilio_sms
    assert isinstance(carrier_routing.sms_route(TELNYX), TelnyxSmsProvider)


def _http(status: int) -> Exception:
    from hailhq.core.providers.voice.base import CarrierRequestError

    return CarrierRequestError(status)


def _wrapped(cause: Exception) -> Exception:
    """A plug-in error raised from ``cause``."""
    try:
        try:
            raise cause
        except Exception as exc:
            raise RuntimeError("plug-in") from exc
    except RuntimeError as outer:
        return outer


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (_http(404), "gone"),
        (_http(410), "gone"),
        (_http(500), "outage"),
        (_http(503), "outage"),
        (_http(401), "outage"),
        (_http(408), "outage"),
        (_http(425), "outage"),
        (_http(429), "outage"),
        (_http(403), "refused"),
        (_http(409), "refused"),
        (_http(400), "final"),
        (_http(422), "final"),
        (_wrapped(_http(409)), "refused"),
        (TimeoutError(), "outage"),
        (ConnectionResetError(), "outage"),
        (_wrapped(TimeoutError()), "outage"),
        (CarrierNotConfigured("x"), "outage"),
    ],
)
def test_failure_kind(error, kind) -> None:
    assert carrier_routing.failure_kind(error) == kind


def test_failure_kind_network_errors_of_each_carrier_sdk() -> None:
    import httpx
    import requests

    request = httpx.Request("DELETE", "https://example.test")
    for error in (
        httpx.ConnectError("x", request=request),
        httpx.ReadTimeout("x", request=request),
        requests.ConnectionError("x"),
        requests.Timeout("x"),
    ):
        assert carrier_routing.failure_kind(error, unknown="refused") == "outage"


def test_failure_kind_of_an_error_with_no_carrier_answer() -> None:
    """A bug (KeyError) is ``unknown``: a discard step counts it refused,
    other callers retry it."""
    assert carrier_routing.failure_kind(KeyError("x")) == "outage"
    assert carrier_routing.failure_kind(KeyError("x"), unknown="refused") == "refused"


def test_escaped_discard_error() -> None:
    """An error a discard raised outside its steps, as discard raises it."""
    from hailhq.core.providers.verification.base import (
        DiscardRefused,
        VerificationProviderError,
        escaped_discard_error,
    )

    assert escaped_discard_error(_http(404)) is None
    outage = escaped_discard_error(TimeoutError())
    assert type(outage) is VerificationProviderError
    assert isinstance(escaped_discard_error(_http(409)), DiscardRefused)
    assert isinstance(escaped_discard_error(KeyError("x")), DiscardRefused)
    own = VerificationProviderError("x")
    assert escaped_discard_error(own) is own
