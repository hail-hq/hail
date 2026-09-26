# DIDWW Purchase Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Customers get DIDWW offers in `POST /numbers/quotes`, register through the existing verification wizard, and `POST /numbers` buys, registers and activates the DIDWW number.

**Architecture:** DIDWW becomes a third carrier in the quote flow (`didww_offers`), an async-order carrier in `number_orders` (order → DID → address verification → active), and a verification plug-in that turns DIDWW `address_requirements` into the neutral wizard model. All DIDWW HTTP goes through the `didww` SDK's `DidwwClient` low-level methods (`get/post/patch/delete/upload_encrypted_file`) with plain JSON:API dicts, wrapped in `asyncio.to_thread`.

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy async, pydantic v2, `didww` SDK 3.1 (MIT; deps `requests`, `jsonapi-requests` BSD, `cryptography`), `responses` for HTTP mocks, pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-09-26-didww-purchase-design.md`

## Global Constraints

- DIDWW API version header `X-DIDWW-API-Version: 2026-04-16` (the SDK sets it; never override).
- Base URLs: production `https://api.didww.com/v3`, sandbox `https://sandbox-api.didww.com/v3`. Tests always use the sandbox URL.
- `provider` value for DIDWW is the string `"didww"` (`carrier_routing.DIDWW`).
- DIDWW offers advertise `capabilities=["voice"]` only, never `sms`.
- Only `did_groups` whose `features` contain `"voice_out"` produce offers.
- Prices in USD cents through `hailhq.core.carrier_offer.cents`; a zero or missing price means no offer.
- `PENDING` order wait for DIDWW is 7 days; Twilio and Telnyx stay at 2 hours.
- On DIDWW registration rejection: refund the monthly fee only; the setup debit stays.
- Hail stores DIDWW ids and states only. Files are encrypted in memory to DIDWW's public keys and uploaded once with the fixed name `document.<ext>`.
- No customer names, organization ids or real phone numbers in code, tests, docs or commits.
- Python: `ruff` + `black`; run `uv sync --all-packages --all-extras` at the repo root, never inside a subpackage.
- Every route change: regenerate `openapi/openapi.yaml`, run `pnpm exec prettier --write openapi/openapi.yaml`, then `cd cli && make codegen`.
- Commits: Conventional Commits, no AI attribution trailers.

## Review Focus

1. A DIDWW offer for a number whose `did_group` lists `voice_out` but no metered SKU (`channels_included_count == 0`) must be skipped, not priced at zero → Task 3 `test_offers_skip_group_without_metered_sku`.
2. The reconciler must never create a second `address_verification` for the same DID after a crash between POST and the metadata write → Task 4 `test_outcome_reuses_existing_verification`.
3. A DIDWW order whose `GET /orders/{id}` returns 429 or 5xx must stay `pending` (retried), not fail the purchase → Task 4 `test_outcome_transient_error_raises` and Task 5 `test_lookup_error_keeps_pending_before_timeout`.
4. A Twilio or Telnyx pending order must still time out at 2 hours after `pending_timeout` is introduced → Task 5 `test_non_didww_timeout_unchanged`.
5. Validation errors from DIDWW (422 with `/data` pointers) must reach the wizard as problems and leave nothing behind at DIDWW → Task 7 `test_create_draft_validation_failure_discards_everything`.

---

### Task 1: Dependency, settings, client factory

**Files:**
- Modify: `core/pyproject.toml` (dependencies list)
- Modify: `core/hailhq/core/config.py:47-52` (Carriers block)
- Modify: `.env.example:124-131` (after the Telnyx block)
- Create: `core/hailhq/core/providers/voice/didww.py`
- Test: `core/tests/providers/test_didww_client.py`

**Interfaces:**
- Produces: `didww_client() -> didww.client.DidwwClient` (raises `CarrierNotConfigured` when `settings.didww_api_key` is empty); `DIDWW_TEST_BASE = "https://sandbox-api.didww.com/v3"` is what tests target; `carrier_status(exc: DidwwApiError) -> int`.

- [ ] **Step 1: Add the dependency and settings**

`core/pyproject.toml`, in `dependencies`, after `"phonenumbers>=9.0.39",`:

```toml
    "didww>=3.1",
```

`core/hailhq/core/config.py`, after `telnyx_public_key: str = ""`:

```python
    # DIDWW: numbers, orders and end-user registration through API v3.
    # Empty key = DIDWW offers are hidden and its verification plug-in is off.
    didww_api_key: str = ""
    # "production" or "sandbox" (https://sandbox-api.didww.com/v3).
    didww_environment: str = "production"
```

`.env.example`, after the `TELNYX_PUBLIC_KEY=` line and its blank line:

```
# Optional third carrier. Leave the key empty to hide DIDWW inventory.
# Voice: DIDWW outbound trunk + LiveKit trunk LIVEKIT_DIDWW_SIP_OUTBOUND_TRUNK_ID.
# Numbers and end-user registration go through API v3 (my.didww.com -> API).
# DIDWW_ENVIRONMENT=sandbox points every call at sandbox-api.didww.com.
DIDWW_API_KEY=
DIDWW_ENVIRONMENT=production

```

Then run at the repo root: `uv sync --all-packages --all-extras`.

- [ ] **Step 2: Write the failing tests**

`core/tests/providers/test_didww_client.py`:

```python
import pytest
from didww.configuration import Environment
from didww.exceptions import DidwwApiError
from hailhq.core.config import settings
from hailhq.core.providers.voice import CarrierNotConfigured
from hailhq.core.providers.voice.didww import carrier_status, didww_client


def test_client_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "didww_api_key", "")
    with pytest.raises(CarrierNotConfigured):
        didww_client()


@pytest.mark.parametrize(
    ("env", "base"),
    [("production", Environment.PRODUCTION.value), ("sandbox", Environment.SANDBOX.value)],
)
def test_client_picks_environment(monkeypatch: pytest.MonkeyPatch, env, base) -> None:
    monkeypatch.setattr(settings, "didww_api_key", "k")
    monkeypatch.setattr(settings, "didww_environment", env)
    assert didww_client().base_url == base


def test_client_rejects_unknown_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "didww_api_key", "k")
    monkeypatch.setattr(settings, "didww_environment", "staging")
    with pytest.raises(CarrierNotConfigured):
        didww_client()


def test_carrier_status_defaults_to_502() -> None:
    assert carrier_status(DidwwApiError([{"title": "x"}], status_code=422)) == 422
    assert carrier_status(DidwwApiError([{"title": "x"}])) == 502
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest core/tests/providers/test_didww_client.py -v`
Expected: FAIL with `ModuleNotFoundError: hailhq.core.providers.voice.didww`

- [ ] **Step 4: Write the module**

`core/hailhq/core/providers/voice/didww.py`:

```python
"""DIDWW carrier: offers, orders, registration outcome, release.

All HTTP goes through the ``didww`` SDK's low-level client with plain
JSON:API dicts, so tests mock at the ``requests`` boundary (``responses``)
and SDK drift shows up as test failures. The SDK is sync; every public
function here is ``async`` and runs its call in ``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
import logging
from functools import lru_cache
from typing import Any, Literal
from uuid import UUID

from didww.client import DidwwClient
from didww.configuration import Environment
from didww.exceptions import DidwwApiError
from hailhq.core.carrier_offer import CarrierOffer, cents
from hailhq.core.config import settings
from hailhq.core.providers.voice.base import CarrierNotConfigured, CarrierRequestError
from hailhq.core.schemas import NumberType

logger = logging.getLogger(__name__)

PROVIDER = "didww"

_ENVIRONMENTS = {
    "production": Environment.PRODUCTION,
    "sandbox": Environment.SANDBOX,
}


def didww_client() -> DidwwClient:
    """A client for the configured environment. ``CarrierNotConfigured``
    when the key is missing or the environment name is unknown."""
    if not settings.didww_api_key:
        raise CarrierNotConfigured("DIDWW is not configured (DIDWW_API_KEY)")
    env = _ENVIRONMENTS.get(settings.didww_environment)
    if env is None:
        raise CarrierNotConfigured(
            "DIDWW_ENVIRONMENT must be 'production' or 'sandbox'"
        )
    return DidwwClient(api_key=settings.didww_api_key, environment=env)


def carrier_status(exc: DidwwApiError) -> int:
    """HTTP status of a DIDWW error; 502 when the SDK did not record one."""
    return exc.status_code or 502
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest core/tests/providers/test_didww_client.py -v`
Expected: 5 passed

- [ ] **Step 6: Commit**

```bash
git add core/pyproject.toml uv.lock core/hailhq/core/config.py .env.example core/hailhq/core/providers/voice/didww.py core/tests/providers/test_didww_client.py
git commit -m "feat(didww): SDK dependency, settings and client factory"
```

---

### Task 2: Per-carrier pending timeout and DIDWW as an async-order carrier

**Files:**
- Modify: `core/hailhq/core/carrier_routing.py:60-80` (`Carrier` dataclass and `CARRIERS`)
- Modify: `api/hailhq/api/number_orders.py:45,205-235` (`PENDING_ORDER_TIMEOUT` uses)
- Test: `core/tests/test_carrier_routing.py`, `api/tests/test_telnyx_orders.py`

**Interfaces:**
- Produces: `Carrier.pending_timeout: timedelta`; `carrier("didww").async_orders is True`; `reconcile_order` reads `carrier(number.provider).pending_timeout`.
- Note: `carrier_outcome` still routes DIDWW to Telnyx after this task; Task 5 fixes the dispatch. No DIDWW number can exist as `pending` before Task 5, so nothing breaks in between.

- [ ] **Step 1: Write the failing tests**

Append to `core/tests/test_carrier_routing.py`:

```python
from datetime import timedelta

from hailhq.core.carrier_routing import DIDWW, TELNYX, TWILIO, carrier


def test_pending_timeout_per_carrier() -> None:
    assert carrier(TWILIO).pending_timeout == timedelta(hours=2)
    assert carrier(TELNYX).pending_timeout == timedelta(hours=2)
    assert carrier(DIDWW).pending_timeout == timedelta(days=7)


def test_didww_orders_complete_later() -> None:
    assert carrier(DIDWW).async_orders is True
```

Append to `api/tests/test_telnyx_orders.py`:

```python
async def test_non_didww_timeout_unchanged(async_session, org_and_key, monkeypatch):
    org, _, _ = org_and_key
    row, offer = await seed_quote(async_session, org)
    await _stub_order(monkeypatch, offer, {"data": {"id": str(uuid4())}})
    number = await buy(async_session, org, row)
    monkeypatch.setattr(
        "hailhq.api.number_orders.carrier_outcome",
        AsyncMock(return_value=("pending", None, None)),
    )
    await async_session.execute(
        text("UPDATE phone_numbers SET created_at = :t WHERE id = :id"),
        {"t": datetime.now(timezone.utc) - timedelta(hours=2, minutes=1), "id": number.id},
    )
    await async_session.commit()
    await async_session.refresh(number)
    await reconcile_order(async_session, number, force=True)
    assert number.provisioning_state == "failed"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest core/tests/test_carrier_routing.py -v -k "pending_timeout or complete_later"`
Expected: FAIL with `AttributeError: 'Carrier' object has no attribute 'pending_timeout'`

- [ ] **Step 3: Add the field and use it**

`core/hailhq/core/carrier_routing.py`: add `from datetime import timedelta` to the imports, then change the dataclass and registry:

```python
@dataclass(frozen=True)
class Carrier:
    voice_route: Callable[[], VoiceRoute]
    # None when Hail sends no SMS through this carrier.
    sms_route: Callable[[SmsProvider], SmsProvider] | None
    # Path under the API URL that receives this carrier's message status.
    sms_status_path: str | None
    # True when a purchase is accepted first and completes later.
    async_orders: bool
    # How long a pending order may wait for the carrier before Hail fails
    # it and refunds the hold. DIDWW registers the end user after the
    # purchase, which takes days; the others answer within minutes.
    pending_timeout: timedelta = timedelta(hours=2)


CARRIERS: dict[str, Carrier] = {
    TWILIO: Carrier(_twilio_voice, _twilio_sms, "sms/status", async_orders=False),
    TELNYX: Carrier(_telnyx_voice, _telnyx_sms, "sms/telnyx", async_orders=True),
    # Outbound voice only. Orders complete after DIDWW approves the
    # end-user registration: docs/public/self-host/didww.md.
    DIDWW: Carrier(
        _didww_voice, None, None, async_orders=True, pending_timeout=timedelta(days=7)
    ),
}
```

`api/hailhq/api/number_orders.py`: keep `PENDING_ORDER_TIMEOUT` (tests import it) and inside `reconcile_order` replace the three `PENDING_ORDER_TIMEOUT` reads with a local:

```python
    timeout = carrier(number.provider).pending_timeout
```

placed right after `org = number.organization_id`, and use `timeout` in:

```python
    if lookup_error is not None and (
        datetime.now(timezone.utc) - number.created_at <= timeout
    ):
```

```python
    unfound = (
        state == "missing"
        and datetime.now(timezone.utc) - number.created_at > timeout
    )
```

```python
    elif (
        state == "pending"
        and datetime.now(timezone.utc) - number.created_at > timeout
    ):
```

Update the comment above `PENDING_ORDER_TIMEOUT` to say it is the default for `Carrier.pending_timeout`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest core/tests/test_carrier_routing.py -v && uv run pytest api/tests/test_telnyx_orders.py -v -k "timeout"`
Expected: all pass, including the pre-existing timeout tests

- [ ] **Step 5: Commit**

```bash
git add core/hailhq/core/carrier_routing.py api/hailhq/api/number_orders.py core/tests/test_carrier_routing.py api/tests/test_telnyx_orders.py
git commit -m "feat(carriers): per-carrier pending order timeout; DIDWW orders are async"
```

---

### Task 3: `didww_offers` discovery

**Files:**
- Modify: `core/hailhq/core/providers/voice/didww.py`
- Test: `core/tests/providers/test_didww_offers.py`

**Interfaces:**
- Consumes: `didww_client()`, `CarrierOffer`, `cents`.
- Produces: `async didww_offers(org: UUID, country: str, kind: NumberType, capabilities: list[str], e164: str | None = None) -> list[CarrierOffer]`; `approved_address_ref(org: UUID, country: str, kind: str) -> str` returning `f"hail:{org}:{country}:{kind}"` (the address `external_reference_id` the verification plug-in stamps on approval, Task 7); `lookup_ids(client, country) -> tuple[str, dict[str, str]]` (country id, `{hail number_type: did_group_type id}`).

DIDWW calls used (all `GET`):
- `countries?filter[iso]=PT` → `data[0].id`
- `did_group_types` → `data[].attributes.name` in `Local`, `National`, `Mobile`, `Toll-free`
- `available_dids?filter[country.id]=…&filter[did_group_type.id]=…&filter[did_group.features]=voice_out&include=did_group,did_group.stock_keeping_units,did_group.address_requirement&page[size]=3` (plus `filter[number_contains]=<digits>` when `e164` is given)
- `addresses?filter[external_reference_id]=hail:<org>:<CC>:<type>` → approved registration handle

- [ ] **Step 1: Write the failing tests**

`core/tests/providers/test_didww_offers.py`:

```python
"""``didww_offers`` against DIDWW's JSON:API, mocked with ``responses``."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
import responses
from hailhq.core.config import settings
from hailhq.core.providers.voice import didww as mod
from hailhq.core.providers.voice.didww import didww_offers

BASE = "https://sandbox-api.didww.com/v3"
ORG = uuid4()
COUNTRY_ID = "c0000000-0000-0000-0000-000000000001"
NATIONAL_ID = "t0000000-0000-0000-0000-000000000002"
GROUP_ID = "g0000000-0000-0000-0000-000000000003"
SKU_ID = "s0000000-0000-0000-0000-000000000004"
REQ_ID = "r0000000-0000-0000-0000-000000000005"


@pytest.fixture(autouse=True)
def _configured(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "didww_api_key", "test-key")
    monkeypatch.setattr(settings, "didww_environment", "sandbox")
    mod.lookup_ids.cache_clear()


def _static():
    responses.add(
        responses.GET,
        f"{BASE}/countries",
        json={"data": [{"id": COUNTRY_ID, "type": "countries", "attributes": {"iso": "PT"}}]},
    )
    responses.add(
        responses.GET,
        f"{BASE}/did_group_types",
        json={
            "data": [
                {"id": "t-local", "type": "did_group_types", "attributes": {"name": "Local"}},
                {"id": NATIONAL_ID, "type": "did_group_types", "attributes": {"name": "National"}},
            ]
        },
    )


def _inventory(*, features=("voice_in", "voice_out"), skus=None, needs_registration=True, numbers=("351300000001",)):
    if skus is None:
        skus = [{"id": SKU_ID, "type": "stock_keeping_units", "attributes": {"setup_price": "3.5", "monthly_price": "3.5", "channels_included_count": 0}}]
    included = [
        {
            "id": GROUP_ID,
            "type": "did_groups",
            "attributes": {"features": list(features), "area_name": "Portugal"},
            "meta": {"needs_registration": needs_registration},
            "relationships": {
                "stock_keeping_units": {"data": [{"id": s["id"], "type": "stock_keeping_units"} for s in skus]},
                "address_requirement": {"data": {"id": REQ_ID, "type": "address_requirements"}},
            },
        },
        *skus,
        {
            "id": REQ_ID,
            "type": "address_requirements",
            "attributes": {"personal_proof_qty": 1, "business_proof_qty": 1, "address_proof_qty": 0},
        },
    ]
    responses.add(
        responses.GET,
        f"{BASE}/available_dids",
        json={
            "data": [
                {
                    "id": f"a-{n}",
                    "type": "available_dids",
                    "attributes": {"number": n},
                    "relationships": {"did_group": {"data": {"id": GROUP_ID, "type": "did_groups"}}},
                }
                for n in numbers
            ],
            "included": included,
        },
    )


def _no_address():
    responses.add(responses.GET, f"{BASE}/addresses", json={"data": []})


@responses.activate
async def test_offers_verification_required_with_documents():
    _static()
    _inventory()
    _no_address()
    offers = await didww_offers(ORG, "PT", "national", ["voice"])
    assert len(offers) == 1
    o = offers[0]
    assert o.provider == "didww"
    assert o.e164 == "+351300000001"
    assert o.capabilities == ["voice"]
    assert (o.monthly_cents, o.setup_cents) == (350, 350)
    assert o.readiness == "verification_required"
    assert o.regulatory_friction == "documents"
    assert o.verification_id is None
    query = responses.calls[2].request.url
    assert "filter%5Bdid_group.features%5D=voice_out" in query
    assert f"filter%5Bdid_group_type.id%5D={NATIONAL_ID}" in query


@responses.activate
async def test_offers_ready_with_approved_address():
    _static()
    _inventory()
    responses.add(
        responses.GET,
        f"{BASE}/addresses",
        json={"data": [{"id": "addr-1", "type": "addresses", "attributes": {"external_reference_id": f"hail:{ORG}:PT:national"}}]},
    )
    (o,) = await didww_offers(ORG, "PT", "national", ["voice"])
    assert o.readiness == "ready"
    assert o.verification_id == "addr-1"
    assert o.address_id == "addr-1"
    assert "filter%5Bexternal_reference_id%5D=hail%3A" in responses.calls[3].request.url


@responses.activate
async def test_offers_ready_when_no_registration_needed():
    _static()
    _inventory(needs_registration=False)
    (o,) = await didww_offers(ORG, "PT", "national", ["voice"])
    assert o.readiness == "ready" and o.regulatory_friction == "none"
    assert len(responses.calls) == 3  # no address lookup


@responses.activate
async def test_offers_skip_group_without_metered_sku():
    _static()
    _inventory(skus=[{"id": SKU_ID, "type": "stock_keeping_units", "attributes": {"setup_price": "3.5", "monthly_price": "3.5", "channels_included_count": 2}}])
    assert await didww_offers(ORG, "PT", "national", ["voice"]) == []


@responses.activate
async def test_offers_skip_group_without_voice_out():
    _static()
    _inventory(features=("voice_in",))
    assert await didww_offers(ORG, "PT", "national", ["voice"]) == []


@responses.activate
async def test_sms_request_yields_nothing():
    _static()
    assert await didww_offers(ORG, "PT", "national", ["voice", "sms"]) == []
    assert len(responses.calls) == 0


@responses.activate
async def test_unknown_country_yields_nothing():
    responses.add(responses.GET, f"{BASE}/countries", json={"data": []})
    assert await didww_offers(ORG, "XX", "national", ["voice"]) == []


async def test_unconfigured_yields_nothing(monkeypatch):
    monkeypatch.setattr(settings, "didww_api_key", "")
    assert await didww_offers(ORG, "PT", "national", ["voice"]) == []


@responses.activate
async def test_e164_filter_and_exact_match():
    _static()
    _inventory(numbers=("351300000001", "351300000002"))
    _no_address()
    offers = await didww_offers(ORG, "PT", "national", ["voice"], e164="+351300000002")
    assert [o.e164 for o in offers] == ["+351300000002"]
    assert "filter%5Bnumber_contains%5D=351300000002" in responses.calls[2].request.url


@responses.activate
@pytest.mark.parametrize("status", [401, 429, 500])
async def test_errors_propagate(status):
    responses.add(responses.GET, f"{BASE}/countries", status=status, json={"errors": [{"title": "x"}]})
    with pytest.raises(Exception):
        await didww_offers(ORG, "PT", "national", ["voice"])
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest core/tests/providers/test_didww_offers.py -v`
Expected: FAIL with `ImportError: cannot import name 'didww_offers'`

- [ ] **Step 3: Implement discovery**

Append to `core/hailhq/core/providers/voice/didww.py`:

```python
_TYPE_NAMES = {
    "Local": "local",
    "National": "national",
    "Mobile": "mobile",
    "Toll-free": "toll_free",
}


def approved_address_ref(org: UUID, country: str, kind: str) -> str:
    """``external_reference_id`` the verification plug-in stamps on an
    address whose papers passed validation and a superadmin approved."""
    return f"hail:{org}:{country}:{kind}"


@lru_cache(maxsize=64)
def lookup_ids(country: str) -> tuple[str | None, dict[str, str]]:
    """(country id, {hail number type: DIDWW group type id}). Cached per
    process; these ids never change."""
    client = didww_client()
    countries = client.get("countries", params={"filter[iso]": country})["data"]
    country_id = countries[0]["id"] if countries else None
    types = {
        _TYPE_NAMES[t["attributes"]["name"]]: t["id"]
        for t in client.get("did_group_types")["data"]
        if t["attributes"]["name"] in _TYPE_NAMES
    }
    return country_id, types


def _by_id(included: list[dict]) -> dict[tuple[str, str], dict]:
    return {(r["type"], r["id"]): r for r in included}


def _metered_sku(group: dict, index: dict) -> dict | None:
    for ref in group["relationships"].get("stock_keeping_units", {}).get("data", []):
        sku = index.get(("stock_keeping_units", ref["id"]))
        if sku and sku["attributes"].get("channels_included_count") == 0:
            return sku
    return None


def _friction(requirement: dict | None) -> Literal["information", "documents"]:
    attrs = (requirement or {}).get("attributes", {})
    qty = (
        attrs.get("personal_proof_qty", 0)
        + attrs.get("business_proof_qty", 0)
        + attrs.get("address_proof_qty", 0)
    )
    return "documents" if qty > 0 else "information"


def _offers_sync(
    org: UUID, country: str, kind: NumberType, e164: str | None
) -> list[CarrierOffer]:
    client = didww_client()
    country_id, types = lookup_ids(country)
    type_id = types.get(kind)
    if not country_id or not type_id:
        return []
    params = {
        "filter[country.id]": country_id,
        "filter[did_group_type.id]": type_id,
        "filter[did_group.features]": "voice_out",
        "include": "did_group,did_group.stock_keeping_units,did_group.address_requirement",
        "page[size]": 3,
    }
    if e164:
        params["filter[number_contains]"] = e164.lstrip("+")
    body = client.get("available_dids", params=params)
    index = _by_id(body.get("included", []))
    approved: dict | None = None
    approved_checked = False
    results: list[CarrierOffer] = []
    for did in body["data"]:
        number = "+" + did["attributes"]["number"]
        if e164 and number != e164:
            continue
        group = index.get(("did_groups", did["relationships"]["did_group"]["data"]["id"]))
        if not group or "voice_out" not in group["attributes"].get("features", []):
            continue
        sku = _metered_sku(group, index)
        if sku is None:
            continue
        monthly = cents(sku["attributes"]["monthly_price"])
        if monthly <= 0:
            continue
        needs_registration = bool(group.get("meta", {}).get("needs_registration"))
        if needs_registration and not approved_checked:
            approved_checked = True
            found = client.get(
                "addresses",
                params={"filter[external_reference_id]": approved_address_ref(org, country, kind)},
            )["data"]
            approved = found[0] if found else None
        req_ref = group["relationships"].get("address_requirement", {}).get("data")
        requirement = index.get(("address_requirements", req_ref["id"])) if req_ref else None
        ready = not needs_registration or approved is not None
        results.append(
            CarrierOffer(
                provider=PROVIDER,
                e164=number,
                country_code=country,
                number_type=kind,
                capabilities=["voice"],
                monthly_cents=monthly,
                setup_cents=cents(sku["attributes"].get("setup_price") or 0),
                readiness="ready" if ready else "verification_required",
                regulatory_friction="none" if ready else _friction(requirement),
                requirements=[] if ready else ["End-user registration (identity and address)"],
                verification_id=approved["id"] if approved else None,
                address_id=approved["id"] if approved else None,
            )
        )
        break  # One offer per carrier, like Telnyx.
    return results


async def didww_offers(
    org: UUID,
    country: str,
    kind: NumberType,
    capabilities: list[str],
    e164: str | None = None,
) -> list[CarrierOffer]:
    """Live DIDWW inventory for one country and number type. Voice only:
    an ``sms`` request never gets a DIDWW offer."""
    if not settings.didww_api_key or "sms" in capabilities:
        return []
    return await asyncio.to_thread(_offers_sync, org, country, kind, e164)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest core/tests/providers/test_didww_offers.py -v`
Expected: 11 passed. If `responses` complains about unmatched query strings, it is because `responses.add` matches the path only by default; the tests read `responses.calls[n].request.url` to check params, so no matcher is needed.

- [ ] **Step 5: Commit**

```bash
git add core/hailhq/core/providers/voice/didww.py core/tests/providers/test_didww_offers.py
git commit -m "feat(didww): live offers from available DIDs"
```

---

### Task 4: Orders, outcome and termination at DIDWW

**Files:**
- Modify: `core/hailhq/core/providers/voice/didww.py`
- Test: `core/tests/providers/test_didww_orders.py`

**Interfaces:**
- Produces:
  - `async place_didww_order(number_id: UUID, e164: str, address_id: str | None) -> str` — order id; raises `CarrierRequestError(status)` on a DIDWW error.
  - `OrderState = Literal["active", "failed", "pending", "missing", "rejected_registration"]`
  - `async didww_order_outcome(e164: str, number_id: UUID, order_id: str | None, address_id: str | None) -> tuple[OrderState, str | None, str | None]` — (state, DID id, order id).
  - `async terminate_did(did_id: str) -> None`; `async release_didww_number(resource_id: str) -> None` (raises `CarrierNotConfigured` when unconfigured).

DIDWW calls:
- order: `GET available_dids?filter[number_contains]=<digits>&include=did_group.stock_keeping_units` → exact number's `available_did_id` + metered `sku_id`; `POST orders` with `{"data":{"type":"orders","attributes":{"allow_back_ordering":false,"external_reference_id":"<number_id>","items":[{"type":"did_order_items","attributes":{"available_did_id":…,"sku_id":…}}]}}}`
- outcome: `GET orders/{id}` (or `GET orders?filter[external_reference_id]=<number_id>` when the id was lost); `GET dids?filter[order.id]=<id>&include=address_verification`; `POST address_verifications` with `{"data":{"type":"address_verifications","attributes":{"service_description": <address description or "">},"relationships":{"address":{"data":{"id":address_id,"type":"addresses"}},"dids":{"data":[{"id":did_id,"type":"dids"}]}}}}`; `GET address_verifications/{id}`
- terminate: `PATCH dids/{id}` with `{"data":{"id":…,"type":"dids","attributes":{"terminated":true}}}`

- [ ] **Step 1: Write the failing tests**

`core/tests/providers/test_didww_orders.py`:

```python
from __future__ import annotations

import json
from uuid import uuid4

import pytest
import responses
from hailhq.core.config import settings
from hailhq.core.providers.voice import CarrierNotConfigured, CarrierRequestError
from hailhq.core.providers.voice.didww import (
    didww_order_outcome,
    place_didww_order,
    release_didww_number,
    terminate_did,
)

BASE = "https://sandbox-api.didww.com/v3"
NUMBER = uuid4()
E164 = "+351300000001"
ORDER = "o0000000-0000-0000-0000-000000000001"
DID = "d0000000-0000-0000-0000-000000000002"
ADDR = "a0000000-0000-0000-0000-000000000003"
VER = "v0000000-0000-0000-0000-000000000004"


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(settings, "didww_api_key", "test-key")
    monkeypatch.setattr(settings, "didww_environment", "sandbox")


def _inventory():
    responses.add(
        responses.GET,
        f"{BASE}/available_dids",
        json={
            "data": [{"id": "avail-1", "type": "available_dids", "attributes": {"number": "351300000001"}, "relationships": {"did_group": {"data": {"id": "g1", "type": "did_groups"}}}}],
            "included": [
                {"id": "g1", "type": "did_groups", "attributes": {"features": ["voice_out"]}, "relationships": {"stock_keeping_units": {"data": [{"id": "sku-1", "type": "stock_keeping_units"}]}}},
                {"id": "sku-1", "type": "stock_keeping_units", "attributes": {"channels_included_count": 0, "monthly_price": "3.5", "setup_price": "3.5"}},
            ],
        },
    )


def _did(awaiting: bool, verification: str | None):
    responses.add(
        responses.GET,
        f"{BASE}/dids",
        json={
            "data": [
                {
                    "id": DID,
                    "type": "dids",
                    "attributes": {"number": "351300000001", "awaiting_registration": awaiting},
                    "relationships": {"address_verification": {"data": {"id": verification, "type": "address_verifications"} if verification else None}},
                }
            ]
        },
    )


def _order(status):
    responses.add(responses.GET, f"{BASE}/orders/{ORDER}", json={"data": {"id": ORDER, "type": "orders", "attributes": {"status": status}}})


@responses.activate
async def test_place_order_uses_exact_number_and_metered_sku():
    _inventory()
    responses.add(responses.POST, f"{BASE}/orders", status=201, json={"data": {"id": ORDER, "type": "orders", "attributes": {"status": "pending"}}})
    assert await place_didww_order(NUMBER, E164, ADDR) == ORDER
    sent = json.loads(responses.calls[1].request.body)
    item = sent["data"]["attributes"]["items"][0]["attributes"]
    assert item == {"available_did_id": "avail-1", "sku_id": "sku-1"}
    assert sent["data"]["attributes"]["external_reference_id"] == str(NUMBER)
    assert sent["data"]["attributes"]["allow_back_ordering"] is False


@responses.activate
async def test_place_order_number_gone_is_a_409():
    responses.add(responses.GET, f"{BASE}/available_dids", json={"data": [], "included": []})
    with pytest.raises(CarrierRequestError) as exc:
        await place_didww_order(NUMBER, E164, ADDR)
    assert exc.value.status == 409


@responses.activate
async def test_place_order_carrier_error_is_carrier_request_error():
    _inventory()
    responses.add(responses.POST, f"{BASE}/orders", status=422, json={"errors": [{"title": "insufficient funds"}]})
    with pytest.raises(CarrierRequestError) as exc:
        await place_didww_order(NUMBER, E164, ADDR)
    assert exc.value.status == 422


@responses.activate
async def test_outcome_pending_order():
    _order("pending")
    assert await didww_order_outcome(E164, NUMBER, ORDER, ADDR) == ("pending", None, ORDER)


@responses.activate
async def test_outcome_canceled_order_is_failed():
    _order("canceled")
    assert await didww_order_outcome(E164, NUMBER, ORDER, ADDR) == ("failed", None, ORDER)


@responses.activate
async def test_outcome_completed_and_registered_is_active():
    _order("completed")
    _did(awaiting=False, verification=None)
    assert await didww_order_outcome(E164, NUMBER, ORDER, ADDR) == ("active", DID, ORDER)
    assert f"filter%5Border.id%5D={ORDER}" in responses.calls[1].request.url


@responses.activate
async def test_outcome_creates_verification_once_when_awaiting():
    _order("completed")
    _did(awaiting=True, verification=None)
    responses.add(responses.GET, f"{BASE}/addresses/{ADDR}", json={"data": {"id": ADDR, "type": "addresses", "attributes": {"description": "Customer support line"}}})
    responses.add(responses.POST, f"{BASE}/address_verifications", status=201, json={"data": {"id": VER, "type": "address_verifications", "attributes": {"status": "pending"}}})
    assert await didww_order_outcome(E164, NUMBER, ORDER, ADDR) == ("pending", None, ORDER)
    sent = json.loads(responses.calls[3].request.body)
    assert sent["data"]["relationships"]["dids"]["data"] == [{"id": DID, "type": "dids"}]
    assert sent["data"]["relationships"]["address"]["data"] == {"id": ADDR, "type": "addresses"}
    assert sent["data"]["attributes"]["service_description"] == "Customer support line"


@responses.activate
async def test_outcome_reuses_existing_verification():
    _order("completed")
    _did(awaiting=True, verification=VER)
    responses.add(responses.GET, f"{BASE}/address_verifications/{VER}", json={"data": {"id": VER, "type": "address_verifications", "attributes": {"status": "pending"}}})
    assert await didww_order_outcome(E164, NUMBER, ORDER, ADDR) == ("pending", None, ORDER)
    assert not any(c.request.method == "POST" for c in responses.calls)


@responses.activate
async def test_outcome_approved_verification_is_active():
    _order("completed")
    _did(awaiting=True, verification=VER)
    responses.add(responses.GET, f"{BASE}/address_verifications/{VER}", json={"data": {"id": VER, "type": "address_verifications", "attributes": {"status": "approved"}}})
    assert await didww_order_outcome(E164, NUMBER, ORDER, ADDR) == ("active", DID, ORDER)


@responses.activate
async def test_outcome_rejected_verification():
    _order("completed")
    _did(awaiting=True, verification=VER)
    responses.add(responses.GET, f"{BASE}/address_verifications/{VER}", json={"data": {"id": VER, "type": "address_verifications", "attributes": {"status": "rejected", "reject_reasons": ["blurry"]}}})
    assert await didww_order_outcome(E164, NUMBER, ORDER, ADDR) == ("rejected_registration", DID, ORDER)


@responses.activate
async def test_outcome_awaiting_without_address_stays_pending():
    _order("completed")
    _did(awaiting=True, verification=None)
    assert await didww_order_outcome(E164, NUMBER, ORDER, None) == ("pending", None, ORDER)
    assert len(responses.calls) == 2


@responses.activate
async def test_outcome_recovers_lost_order_id_by_reference():
    responses.add(responses.GET, f"{BASE}/orders", json={"data": [{"id": ORDER, "type": "orders", "attributes": {"status": "pending", "external_reference_id": str(NUMBER)}}]})
    assert await didww_order_outcome(E164, NUMBER, None, ADDR) == ("pending", None, ORDER)
    assert f"filter%5Bexternal_reference_id%5D={NUMBER}" in responses.calls[0].request.url


@responses.activate
async def test_outcome_missing_when_no_order_by_reference():
    responses.add(responses.GET, f"{BASE}/orders", json={"data": []})
    assert await didww_order_outcome(E164, NUMBER, None, ADDR) == ("missing", None, None)


@responses.activate
@pytest.mark.parametrize("status", [429, 500, 503])
async def test_outcome_transient_error_raises(status):
    responses.add(responses.GET, f"{BASE}/orders/{ORDER}", status=status, json={"errors": [{"title": "x"}]})
    with pytest.raises(Exception):
        await didww_order_outcome(E164, NUMBER, ORDER, ADDR)


@responses.activate
async def test_terminate_did_patches_terminated():
    responses.add(responses.PATCH, f"{BASE}/dids/{DID}", json={"data": {"id": DID, "type": "dids", "attributes": {"terminated": True}}})
    await terminate_did(DID)
    sent = json.loads(responses.calls[0].request.body)
    assert sent["data"]["attributes"] == {"terminated": True}


async def test_release_unconfigured(monkeypatch):
    monkeypatch.setattr(settings, "didww_api_key", "")
    with pytest.raises(CarrierNotConfigured):
        await release_didww_number(DID)


@responses.activate
async def test_release_tolerates_404():
    responses.add(responses.PATCH, f"{BASE}/dids/{DID}", status=404, json={"errors": [{"title": "not found"}]})
    await release_didww_number(DID)  # already gone at the carrier
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest core/tests/providers/test_didww_orders.py -v`
Expected: FAIL with `ImportError: cannot import name 'didww_order_outcome'`

- [ ] **Step 3: Implement orders, outcome and termination**

Append to `core/hailhq/core/providers/voice/didww.py`:

```python
OrderState = Literal["active", "failed", "pending", "missing", "rejected_registration"]


def _find_available(client: DidwwClient, e164: str) -> tuple[str, str]:
    """(available_did id, metered sku id) for one exact number, or
    ``CarrierRequestError(409)`` when it is gone."""
    body = client.get(
        "available_dids",
        params={
            "filter[number_contains]": e164.lstrip("+"),
            "include": "did_group,did_group.stock_keeping_units",
            "page[size]": 10,
        },
    )
    index = _by_id(body.get("included", []))
    for did in body["data"]:
        if "+" + did["attributes"]["number"] != e164:
            continue
        group = index.get(("did_groups", did["relationships"]["did_group"]["data"]["id"]))
        sku = _metered_sku(group, index) if group else None
        if sku is not None:
            return did["id"], sku["id"]
    raise CarrierRequestError(409)


def _place_order_sync(number_id: UUID, e164: str) -> str:
    client = didww_client()
    try:
        available_id, sku_id = _find_available(client, e164)
        order = client.post(
            "orders",
            {
                "data": {
                    "type": "orders",
                    "attributes": {
                        "allow_back_ordering": False,
                        "external_reference_id": str(number_id),
                        "items": [
                            {
                                "type": "did_order_items",
                                "attributes": {
                                    "available_did_id": available_id,
                                    "sku_id": sku_id,
                                },
                            }
                        ],
                    },
                }
            },
        )
    except DidwwApiError as exc:
        raise CarrierRequestError(carrier_status(exc)) from exc
    return order["data"]["id"]


async def place_didww_order(number_id: UUID, e164: str, address_id: str | None) -> str:
    """Order one exact number once; returns the DIDWW order id. Never
    retried. ``address_id`` is not sent: DIDWW links the registration to
    the DID after the order (see ``didww_order_outcome``)."""
    return await asyncio.to_thread(_place_order_sync, number_id, e164)


def _load_order(client: DidwwClient, number_id: UUID, order_id: str | None) -> dict | None:
    if order_id:
        return client.get(f"orders/{order_id}")["data"]
    # A crash after POST may lose the response. Recover by our reference.
    found = client.get(
        "orders", params={"filter[external_reference_id]": str(number_id), "page[size]": 10}
    )["data"]
    matches = [o for o in found if o["attributes"].get("external_reference_id") == str(number_id)]
    return matches[0] if len(matches) == 1 else None


def _ensure_verification(client: DidwwClient, did: dict, address_id: str) -> dict:
    """The DID's address verification, created once when missing."""
    rel = did["relationships"].get("address_verification", {}).get("data")
    if rel:
        return client.get(f"address_verifications/{rel['id']}")["data"]
    address = client.get(f"addresses/{address_id}")["data"]
    return client.post(
        "address_verifications",
        {
            "data": {
                "type": "address_verifications",
                "attributes": {
                    "service_description": address["attributes"].get("description") or ""
                },
                "relationships": {
                    "address": {"data": {"id": address_id, "type": "addresses"}},
                    "dids": {"data": [{"id": did["id"], "type": "dids"}]},
                },
            }
        },
    )["data"]


def _outcome_sync(
    e164: str, number_id: UUID, order_id: str | None, address_id: str | None
) -> tuple[OrderState, str | None, str | None]:
    client = didww_client()
    order = _load_order(client, number_id, order_id)
    if order is None:
        return "missing", None, None
    order_id = order["id"]
    status = order["attributes"]["status"]
    if status == "canceled":
        return "failed", None, order_id
    if status != "completed":
        return "pending", None, order_id
    dids = client.get(
        "dids",
        params={"filter[order.id]": order_id, "include": "address_verification", "page[size]": 10},
    )["data"]
    did = next((d for d in dids if "+" + d["attributes"]["number"] == e164), None)
    if did is None:
        return "pending", None, order_id
    if not did["attributes"].get("awaiting_registration"):
        return "active", did["id"], order_id
    if not address_id:
        # Bought without an approved registration: nothing to file.
        return "pending", None, order_id
    verification = _ensure_verification(client, did, address_id)
    vstatus = verification["attributes"]["status"]
    if vstatus == "approved":
        return "active", did["id"], order_id
    if vstatus == "rejected":
        return "rejected_registration", did["id"], order_id
    return "pending", None, order_id


async def didww_order_outcome(
    e164: str, number_id: UUID, order_id: str | None, address_id: str | None
) -> tuple[OrderState, str | None, str | None]:
    """Ask DIDWW what happened to an order. Holds no DB lock. Files the
    end-user registration once the DID exists. Errors propagate so the
    reconciler retries until the carrier's ``pending_timeout``."""
    return await asyncio.to_thread(_outcome_sync, e164, number_id, order_id, address_id)


def _terminate_sync(did_id: str) -> None:
    didww_client().patch(
        f"dids/{did_id}",
        {"data": {"id": did_id, "type": "dids", "attributes": {"terminated": True}}},
    )


async def terminate_did(did_id: str) -> None:
    """Stop renewal at the end of the billing cycle. DIDWW does not refund."""
    await asyncio.to_thread(_terminate_sync, did_id)


async def release_didww_number(resource_id: str) -> None:
    """Release an owned DIDWW number. ``CarrierNotConfigured`` when the key
    is missing; a 404 means it is already gone and is tolerated."""
    try:
        await terminate_did(resource_id)
    except DidwwApiError as exc:
        if carrier_status(exc) == 404:
            logger.warning(
                "didww terminate of %s returned 404; treating as already released",
                resource_id,
            )
            return
        raise
```

`terminate_did` calls `didww_client()` inside the thread, so the unconfigured case raises `CarrierNotConfigured` from `release_didww_number` as the test expects.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest core/tests/providers/test_didww_orders.py -v`
Expected: 19 passed

- [ ] **Step 5: Commit**

```bash
git add core/hailhq/core/providers/voice/didww.py core/tests/providers/test_didww_orders.py
git commit -m "feat(didww): orders, registration outcome and termination"
```

---

### Task 5: Wire DIDWW into the purchase flow

**Files:**
- Modify: `core/hailhq/core/carrier_offer.py:14` (`provider` Literal)
- Modify: `core/hailhq/core/schemas.py:524,563` (`provider` Literals)
- Modify: `core/hailhq/core/number_offers.py:53-71` (`PROVIDERS`, `searches`)
- Modify: `api/hailhq/api/number_orders.py` (`carrier_outcome`, `acquire_offer` async branch, `finish_order`, `reconcile_order`)
- Modify: `api/hailhq/api/routes/numbers.py:199-211,487-515` (`_RELEASERS`, quotes route catalog gate)
- Test: `api/tests/test_didww_orders.py`, `core/tests/test_number_offers.py` (create)

**Interfaces:**
- Consumes: `place_didww_order`, `didww_order_outcome`, `release_didww_number`, `didww_offers` (Tasks 3–4), `carrier(...).pending_timeout` (Task 2).
- Produces: `finish_order(..., keep_setup: bool = False)`; `carrier_outcome` returns the `OrderState` union; `PROVIDERS == ("twilio", "telnyx", "didww")`.

- [ ] **Step 1: Write the failing tests**

`core/tests/test_number_offers.py`:

```python
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from hailhq.core import number_offers


async def test_discover_asks_didww(monkeypatch):
    calls = {}
    for name in ("twilio", "telnyx", "didww"):
        calls[name] = AsyncMock(return_value=[])
    monkeypatch.setattr(number_offers, "twilio_offers", calls["twilio"])
    monkeypatch.setattr(number_offers, "telnyx_offers", calls["telnyx"])
    monkeypatch.setattr(number_offers, "didww_offers", calls["didww"])
    offers, unavailable = await number_offers.discover_offers(uuid4(), "PT", "national", ["voice"])
    assert offers == [] and unavailable == []
    calls["didww"].assert_awaited_once()
    assert number_offers.PROVIDERS == ("twilio", "telnyx", "didww")


async def test_discover_reports_didww_outage(monkeypatch):
    monkeypatch.setattr(number_offers, "twilio_offers", AsyncMock(return_value=[]))
    monkeypatch.setattr(number_offers, "telnyx_offers", AsyncMock(return_value=[]))
    monkeypatch.setattr(number_offers, "didww_offers", AsyncMock(side_effect=RuntimeError("down")))
    _, unavailable = await number_offers.discover_offers(uuid4(), "PT", "national", ["voice"])
    assert unavailable == ["didww"]
```

`api/tests/test_didww_orders.py`:

```python
"""DIDWW purchases through ``acquire_offer`` and ``reconcile_order``. The
carrier functions are mocked; their HTTP is covered in core tests."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from hailhq.api.number_orders import acquire_offer, reconcile_order
from hailhq.core.billing import get_balance_cents
from hailhq.core.models import AccountCredit, NumberOffer, PhoneNumber
from hailhq.core.number_offers import CarrierOffer
from hailhq.core.providers.voice import CarrierRequestError
from sqlalchemy import select, text

ORDER = "o0000000-0000-0000-0000-000000000001"
DID = "d0000000-0000-0000-0000-000000000002"


async def seed_quote(db, org, *, address_id="addr-1", monthly=350, setup=350):
    offer = CarrierOffer(
        provider="didww",
        e164="+351300000001",
        country_code="PT",
        number_type="national",
        capabilities=["voice"],
        monthly_cents=monthly,
        setup_cents=setup,
        readiness="ready",
        verification_id=address_id,
        address_id=address_id,
    )
    row = NumberOffer(
        organization_id=org,
        offer=offer.model_dump(mode="json"),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    db.add(row)
    await db.commit()
    return row, offer


async def buy(db, org, quote, monkeypatch, offer):
    monkeypatch.setattr(
        "hailhq.api.number_orders.discover_offers",
        AsyncMock(return_value=([offer], [])),
    )
    return await acquire_offer(
        db, org, quote.id, country="PT", kind="national", provider="auto", billed=True
    )


async def _age(db, number, delta):
    await db.execute(
        text("UPDATE phone_numbers SET created_at = :t WHERE id = :id"),
        {"t": datetime.now(timezone.utc) - delta, "id": number.id},
    )
    await db.commit()
    await db.refresh(number)


async def test_didww_purchase_places_order_and_waits(async_session, org_and_key, monkeypatch):
    org, _, _ = org_and_key
    row, offer = await seed_quote(async_session, org)
    place = AsyncMock(return_value=ORDER)
    monkeypatch.setattr("hailhq.api.number_orders.place_didww_order", place)
    monkeypatch.setattr(
        "hailhq.api.number_orders.didww_order_outcome",
        AsyncMock(return_value=("pending", None, ORDER)),
    )
    number = await buy(async_session, org, row, monkeypatch, offer)
    assert number.provisioning_state == "pending"
    assert number.provider == "didww"
    assert number.provisioning_metadata["order_id"] == ORDER
    place.assert_awaited_once_with(number.id, "+351300000001", "addr-1")
    assert await get_balance_cents(async_session, org) == 100000 - 700


async def test_didww_purchase_is_not_catalog_gated(async_session, org_and_key, monkeypatch):
    """PT/national is not in the test catalog; DIDWW carries its own price."""
    org, _, _ = org_and_key
    row, offer = await seed_quote(async_session, org)
    monkeypatch.setattr("hailhq.api.number_orders.place_didww_order", AsyncMock(return_value=ORDER))
    monkeypatch.setattr(
        "hailhq.api.number_orders.didww_order_outcome",
        AsyncMock(return_value=("pending", None, ORDER)),
    )
    number = await buy(async_session, org, row, monkeypatch, offer)
    assert number.provisioning_state == "pending"


async def test_didww_registration_approved_activates(async_session, org_and_key, monkeypatch):
    org, _, _ = org_and_key
    row, offer = await seed_quote(async_session, org)
    monkeypatch.setattr("hailhq.api.number_orders.place_didww_order", AsyncMock(return_value=ORDER))
    outcome = AsyncMock(return_value=("pending", None, ORDER))
    monkeypatch.setattr("hailhq.api.number_orders.didww_order_outcome", outcome)
    number = await buy(async_session, org, row, monkeypatch, offer)
    outcome.return_value = ("active", DID, ORDER)
    await reconcile_order(async_session, number, force=True)
    assert number.provisioning_state == "active"
    assert number.provider_resource_id == DID
    outcome.assert_awaited_with("+351300000001", number.id, ORDER, "addr-1")
    assert await get_balance_cents(async_session, org) == 100000 - 700


async def test_didww_registration_rejected_refunds_monthly_only(async_session, org_and_key, monkeypatch):
    org, _, _ = org_and_key
    row, offer = await seed_quote(async_session, org)
    monkeypatch.setattr("hailhq.api.number_orders.place_didww_order", AsyncMock(return_value=ORDER))
    outcome = AsyncMock(return_value=("pending", None, ORDER))
    monkeypatch.setattr("hailhq.api.number_orders.didww_order_outcome", outcome)
    terminate = AsyncMock()
    monkeypatch.setattr("hailhq.api.number_orders.terminate_did", terminate)
    number = await buy(async_session, org, row, monkeypatch, offer)
    outcome.return_value = ("rejected_registration", DID, ORDER)
    await reconcile_order(async_session, number, force=True)
    assert number.provisioning_state == "failed"
    assert "registration" in number.provisioning_metadata["failure_reason"]
    terminate.assert_awaited_once_with(DID)
    assert await get_balance_cents(async_session, org) == 100000 - 350
    setup = (
        await async_session.execute(
            select(AccountCredit).where(AccountCredit.ref == f"number_setup:{number.id}")
        )
    ).scalar_one()
    assert setup.amount_cents == -350
    # A second pass changes nothing.
    await reconcile_order(async_session, number, force=True)
    assert await get_balance_cents(async_session, org) == 100000 - 350


async def test_didww_pending_survives_three_days(async_session, org_and_key, monkeypatch):
    org, _, _ = org_and_key
    row, offer = await seed_quote(async_session, org)
    monkeypatch.setattr("hailhq.api.number_orders.place_didww_order", AsyncMock(return_value=ORDER))
    monkeypatch.setattr(
        "hailhq.api.number_orders.didww_order_outcome",
        AsyncMock(return_value=("pending", None, ORDER)),
    )
    number = await buy(async_session, org, row, monkeypatch, offer)
    await _age(async_session, number, timedelta(days=3))
    await reconcile_order(async_session, number, force=True)
    assert number.provisioning_state == "pending"
    await _age(async_session, number, timedelta(days=7, minutes=1))
    await reconcile_order(async_session, number, force=True)
    assert number.provisioning_state == "failed"
    assert await get_balance_cents(async_session, org) == 100000


async def test_lookup_error_keeps_pending_before_timeout(async_session, org_and_key, monkeypatch):
    org, _, _ = org_and_key
    row, offer = await seed_quote(async_session, org)
    monkeypatch.setattr("hailhq.api.number_orders.place_didww_order", AsyncMock(return_value=ORDER))
    outcome = AsyncMock(return_value=("pending", None, ORDER))
    monkeypatch.setattr("hailhq.api.number_orders.didww_order_outcome", outcome)
    number = await buy(async_session, org, row, monkeypatch, offer)
    outcome.side_effect = RuntimeError("429")
    with pytest.raises(RuntimeError):
        await reconcile_order(async_session, number, force=True)
    await async_session.refresh(number)
    assert number.provisioning_state == "pending"


async def test_didww_order_rejected_at_carrier_refunds_all(async_session, org_and_key, monkeypatch):
    org, _, _ = org_and_key
    row, offer = await seed_quote(async_session, org)
    monkeypatch.setattr(
        "hailhq.api.number_orders.place_didww_order",
        AsyncMock(side_effect=CarrierRequestError(422)),
    )
    number = await buy(async_session, org, row, monkeypatch, offer)
    assert number.provisioning_state == "failed"
    assert await get_balance_cents(async_session, org) == 100000
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest core/tests/test_number_offers.py api/tests/test_didww_orders.py -v`
Expected: FAIL (`AttributeError: module has no attribute 'didww_offers'`, then `ValidationError` on `provider="didww"`)

- [ ] **Step 3: Widen the provider literals and the discovery registry**

`core/hailhq/core/carrier_offer.py`:

```python
    provider: Literal["twilio", "telnyx", "didww"] = Field(
        description="Carrier supplying this exact number."
    )
```

`core/hailhq/core/schemas.py`, both request models:

```python
    provider: Literal["auto", "twilio", "telnyx", "didww"] = Field(
```

and update the two descriptions to name `didww` next to `twilio` and `telnyx`.

`core/hailhq/core/number_offers.py`:

```python
from hailhq.core.providers.voice.didww import didww_offers
```

```python
PROVIDERS = ("twilio", "telnyx", "didww")
```

```python
    searches = {
        "twilio": lambda: twilio_offers(org, country, kind, capabilities, e164=e164),
        "telnyx": lambda: telnyx_offers(
            org, country, kind, capabilities, get_http_client(), e164=e164
        ),
        "didww": lambda: didww_offers(org, country, kind, capabilities, e164=e164),
    }
```

Add `"didww_offers"` to `__all__`.

- [ ] **Step 4: Dispatch orders and outcomes by carrier; partial refund**

`api/hailhq/api/number_orders.py` imports:

```python
from hailhq.core.carrier_routing import DIDWW, carrier
from hailhq.core.providers.voice.didww import (
    didww_order_outcome,
    place_didww_order,
    terminate_did,
)
```

`finish_order` gains `keep_setup: bool = False`; inside the `if meta["billed"]:` block, the setup debit is added when the order succeeded **or** `keep_setup`:

```python
        if not failed:
            db.add(
                credit(
                    number,
                    -offer.monthly_cents,
                    monthly_fee_ref(number.organization_id, number.id, now),
                    "monthly_fee",
                )
            )
        if offer.setup_cents and (not failed or keep_setup):
            db.add(
                credit(
                    number,
                    -offer.setup_cents,
                    f"number_setup:{number.id}",
                    "number_setup",
                )
            )
```

`carrier_outcome`:

```python
async def carrier_outcome(
    number: PhoneNumber,
) -> tuple[
    Literal["active", "failed", "pending", "missing", "rejected_registration"],
    str | None,
    str | None,
]:
    """Ask the carrier what happened to this order. Holds no DB lock.

    Returns (state, owned resource id, carrier order id). ``missing`` means
    the carrier has no record of the order; it is never treated as
    permission to submit another paid purchase. ``rejected_registration``
    (DIDWW) means the number exists but the end-user registration failed.
    """
    meta = number.provisioning_metadata
    if number.provider == DIDWW:
        return await didww_order_outcome(
            number.e164,
            number.id,
            meta.get("order_id"),
            meta.get("offer", {}).get("address_id"),
        )
    if carrier(number.provider).async_orders:
        return await telnyx_order_outcome(number.e164, number.id, meta.get("order_id"))
    sid = await find_ordered_number(number.e164, number.id)
    return ("active", sid, None) if sid else ("missing", None, None)
```

`acquire_offer`, the async branch:

```python
        if carrier(offer.provider).async_orders:
            if offer.provider == DIDWW:
                order_id = await place_didww_order(
                    number.id, offer.e164, offer.address_id
                )
            else:
                order_id = await place_number_order(
                    number.id, offer.e164, offer.verification_id, offer.capabilities
                )
```

`acquire_offer`, the catalog gate: replace `catalog_capabilities(country, kind)` with

```python
    if offer.provider != DIDWW:
        # DIDWW offers carry their own live price; the catalog lists Twilio's.
        catalog_capabilities(country, kind)
```

`reconcile_order`, after the `state == "failed"` branch:

```python
    elif state == "rejected_registration":
        # The number exists at the carrier but the end-user registration was
        # refused. Stop its renewal and give the monthly fee back; the setup
        # fee stays (the carrier billed it and does not refund).
        if resource_id:
            try:
                await terminate_did(resource_id)
            except Exception:
                logger.error(
                    "Could not terminate rejected DIDWW number; release it by hand: "
                    "number=%s did=%s",
                    number.id,
                    resource_id,
                    exc_info=True,
                )
        await finish_order(
            db,
            number,
            resource_id=None,
            failed=True,
            keep_setup=True,
            reason="the carrier rejected the end-user registration",
        )
```

- [ ] **Step 5: Quotes route and release**

`api/hailhq/api/routes/numbers.py` imports: add `DIDWW` from `hailhq.core.carrier_routing`, `release_didww_number` from `hailhq.core.providers.voice.didww`, and `settings` from `hailhq.core.config` if not already imported.

```python
async def _release_didww(number: PhoneNumber, provider: VoiceProvider) -> None:
    try:
        await release_didww_number(number.provider_resource_id)
    except CarrierNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


_RELEASERS = {TELNYX: _release_telnyx, TWILIO: _release_twilio, DIDWW: _release_didww}
```

Quotes route: the catalog decides the kinds to search for Twilio and Telnyx; DIDWW is searched for every kind when it is configured.

```python
    providers = PROVIDERS if body.provider == "auto" else [body.provider]
    didww_on = DIDWW in providers and bool(settings.didww_api_key)
    if body.number_type:
        if not didww_on:
            catalog_capabilities(body.country_code, body.number_type)
        kinds = [body.number_type]
    else:
        kinds = [
            k
            for k in ("local", "mobile", "national", "toll_free")
            if didww_on or telephony_catalog.capabilities(body.country_code, k) is not None
        ]
        if not kinds:
            raise unprocessable(
                f"we don't offer numbers in {body.country_code} yet",
                loc=["body", "country_code"],
            )
```

(Move the `providers = ...` line above the kinds block; delete its old position.)

- [ ] **Step 6: Run the tests**

Run: `uv run pytest core/tests/test_number_offers.py -v && uv run pytest api/tests/test_didww_orders.py api/tests/test_telnyx_orders.py api/tests/test_numbers_api.py -v`
Expected: all pass. `test_quote_api_returns_live_recommendation_and_persists_org_scope` in `test_telnyx_orders.py` mocks `discover_offers`, so the new DIDWW search does not reach the network.

- [ ] **Step 7: Regenerate OpenAPI and the CLI client**

```bash
cd api && uv run python -c "import json,yaml; from hailhq.api.main import app; print(yaml.safe_dump(app.openapi(), sort_keys=False))" > ../openapi/openapi.yaml && cd ..
pnpm exec prettier --write openapi/openapi.yaml
cd cli && make codegen && cd ..
```

If the repo has a dedicated regen script (check `package.json` scripts and `api/pyproject.toml` for `openapi`), use that instead of the inline python. `git diff --stat openapi/openapi.yaml` must show only the `provider` enum lines and descriptions.

- [ ] **Step 8: Commit**

```bash
git add core/hailhq/core/carrier_offer.py core/hailhq/core/schemas.py core/hailhq/core/number_offers.py api/hailhq/api/number_orders.py api/hailhq/api/routes/numbers.py core/tests/test_number_offers.py api/tests/test_didww_orders.py openapi/openapi.yaml cli/internal/client/client.gen.go
git commit -m "feat(numbers): DIDWW offers, orders and registration in the purchase flow"
```

---

### Task 6: Spec amendment — approval is stamped on the DIDWW address

**Files:**
- Modify: `docs/superpowers/specs/2026-09-26-didww-purchase-design.md` (Verification plug-in section, Discovery section)

The spec says `submit()` is a no-op and the approved-verification lookup moves to core. During planning it turned out `discover_offers` has no database session, and every other carrier finds its own approval at the carrier. So:

- `submit(refs)` stamps the DIDWW address `external_reference_id` = `hail:<org>:<CC>:<type>` (`PATCH /addresses/{id}`). Before that the address carries `hail-draft:<org>:<CC>:<type>`.
- `status(refs)` reads the address: `hail:` prefix → `approved`, else `draft`.
- `didww_offers` looks up `GET /addresses?filter[external_reference_id]=hail:<org>:<CC>:<type>` (Task 3). No lookup moves to core. `api/routes/verifications.py` is untouched.

- [ ] **Step 1: Edit the spec**

In "Verification plug-in", replace the `check/submit/status/purchase_handle` bullet with:

```
- `check(refs)`: re-run the validation. `submit(refs)`: `PATCH /addresses/{address_id}` setting `external_reference_id` to `hail:<org>:<country>:<number_type>` (the draft carries `hail-draft:…`). `status(refs)`: `approved` when the address carries the `hail:` reference, else `draft`. `purchase_handle(refs)`: `{identity_id, address_id}`. `discard(refs)`: delete proofs, files, address, and the identity when this draft created it; best effort.
```

In "Discovery", replace the last bullet (the lookup moving to core) with:

```
- The approved registration is found at DIDWW, like the other carriers find theirs: `GET /addresses?filter[external_reference_id]=hail:<org>:<country>:<number_type>`. Nothing moves out of `api/routes/verifications.py`.
```

In "Code touch points", delete the line about `api/hailhq/api/routes/verifications.py`.

- [ ] **Step 2: Commit**

```bash
git add docs/superpowers/specs/2026-09-26-didww-purchase-design.md
git commit -m "docs(specs): DIDWW approval is stamped on the address reference"
```

---

### Task 7: DIDWW verification plug-in

**Files:**
- Create: `core/hailhq/core/providers/verification/didww.py`
- Modify: `core/hailhq/core/providers/verification/__init__.py:81-89` (`_register_builtin`)
- Test: `core/tests/providers/test_didww_verification.py`

**Interfaces:**
- Consumes: `didww_client`, `lookup_ids`, `approved_address_ref` (Tasks 1, 3); base models from `hailhq.core.providers.verification.base`.
- Produces: `class DidwwVerificationProvider(VerificationProvider)` with `name = "didww"`; registered as `"didww"`; factory raises `ValueError` when `settings.didww_api_key` is empty (the registry treats that as "not configured").

Field names (keys the wizard sends, DIDWW identity attributes): `first_name`, `last_name`, `birth_date`, `id_number`, `personal_tax_id`, `phone_number`, `company_name`, `company_reg_number`, `vat_id`, plus `service_description` (stored in the address `description`).

- [ ] **Step 1: Write the failing tests**

`core/tests/providers/test_didww_verification.py`:

```python
"""``DidwwVerificationProvider`` against DIDWW's JSON:API (``responses``)."""

from __future__ import annotations

import json

import pytest
import responses
from hailhq.core.config import settings
from hailhq.core.providers.verification import (
    Address,
    DocumentInput,
    UnsupportedSubjectType,
    UploadedFile,
    get_verification_provider,
)
from hailhq.core.providers.verification.didww import DidwwVerificationProvider
from hailhq.core.providers.voice import didww as voice_mod

BASE = "https://sandbox-api.didww.com/v3"
ORG = "11111111-2222-3333-4444-555555555555"
COUNTRY_ID = "c0000000-0000-0000-0000-000000000001"
NATIONAL_ID = "t0000000-0000-0000-0000-000000000002"
REQ_ID = "r0000000-0000-0000-0000-000000000005"
PEM = (
    "-----BEGIN PUBLIC KEY-----\n"
    "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAu1SU1LfVLPHCozMxH2Mo\n"
    "4lgOEePzNm0tRgeLezV6ffAt0gunVTLw7onLRnrq0/IzW7yWR7QkrmBL7jTKEn5u\n"
    "+qKhbwKfBstIs+bMY2Zkp18gnTxKLxoS2tFczGkPLPgizskuemMghRniWaoLcyeh\n"
    "kd3qqGElvW/VDL5AaWTg0nLVkjRo9z+40RQzuVaE8AkAFmxZzow3x+VJYKdjykkJ\n"
    "0iT9wCS0DRTXu269V264Vf/3jvredZiKRkgwlL9xNAwxXFg0x/XFw005UWVRIkdg\n"
    "cKWTjpBP2dPwVZ4WWC+9aGVd+Gyn1o0CLelf4rEjGoXbAAEgAqeGUxrcIlbjXfbc\n"
    "mwIDAQAB\n"
    "-----END PUBLIC KEY-----"
)


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(settings, "didww_api_key", "test-key")
    monkeypatch.setattr(settings, "didww_environment", "sandbox")
    voice_mod.lookup_ids.cache_clear()


@pytest.fixture()
def provider() -> DidwwVerificationProvider:
    return DidwwVerificationProvider()


def _static():
    responses.add(responses.GET, f"{BASE}/countries", json={"data": [{"id": COUNTRY_ID, "type": "countries", "attributes": {"iso": "PT"}}]})
    responses.add(responses.GET, f"{BASE}/did_group_types", json={"data": [{"id": NATIONAL_ID, "type": "did_group_types", "attributes": {"name": "National"}}]})


def _requirement(*, identity_type="any", personal_qty=1, address_qty=1, service_description=False, area="country"):
    responses.add(
        responses.GET,
        f"{BASE}/address_requirements",
        json={
            "data": [
                {
                    "id": REQ_ID,
                    "type": "address_requirements",
                    "attributes": {
                        "identity_type": identity_type,
                        "personal_area_level": "world_wide",
                        "business_area_level": "world_wide",
                        "address_area_level": area,
                        "personal_proof_qty": personal_qty,
                        "business_proof_qty": 1,
                        "address_proof_qty": address_qty,
                        "personal_mandatory_fields": ["birth_date", "id_number"],
                        "business_mandatory_fields": ["company_reg_number"],
                        "service_description_required": service_description,
                        "restriction_message": "Local presence required.",
                    },
                    "relationships": {
                        "personal_proof_types": {"data": [{"id": "pt-passport", "type": "proof_types"}]},
                        "business_proof_types": {"data": [{"id": "pt-reg", "type": "proof_types"}]},
                        "address_proof_types": {"data": [{"id": "pt-bill", "type": "proof_types"}]},
                    },
                }
            ],
            "included": [
                {"id": "pt-passport", "type": "proof_types", "attributes": {"name": "Passport", "entity_type": "Personal"}},
                {"id": "pt-reg", "type": "proof_types", "attributes": {"name": "Company registration", "entity_type": "Business"}},
                {"id": "pt-bill", "type": "proof_types", "attributes": {"name": "Utility bill", "entity_type": "Address"}},
            ],
        },
    )


@responses.activate
async def test_requirements_person(provider):
    _static()
    _requirement(service_description=True)
    req = await provider.requirements("PT", "national", "person")
    assert req.required is True and req.provider == "didww"
    assert [f.name for f in req.fields] == ["first_name", "last_name", "birth_date", "id_number", "service_description"]
    assert [s.name for s in req.documents] == ["identity_proof_1", "address_proof_1"]
    assert req.documents[0].options[0].key == "pt-passport"
    assert req.documents[0].options[0].label == "Passport"
    assert req.documents[1].options[0].needs_address is True
    assert req.address_required is True
    assert req.subject_types == ("business", "person")
    url = responses.calls[2].request.url
    assert f"filter%5Bcountry.id%5D={COUNTRY_ID}" in url and f"filter%5Bdid_group_type.id%5D={NATIONAL_ID}" in url


@responses.activate
async def test_requirements_business(provider):
    _static()
    _requirement()
    req = await provider.requirements("PT", "national", "business")
    assert [f.name for f in req.fields] == ["company_name", "company_reg_number"]
    assert req.documents[0].options[0].key == "pt-reg"


@responses.activate
async def test_requirements_rejects_disallowed_subject(provider):
    _static()
    _requirement(identity_type="business")
    with pytest.raises(UnsupportedSubjectType) as exc:
        await provider.requirements("PT", "national", "person")
    assert exc.value.allowed == ("business",)


@responses.activate
async def test_requirements_none_needed(provider):
    _static()
    responses.add(responses.GET, f"{BASE}/address_requirements", json={"data": [], "included": []})
    req = await provider.requirements("PT", "national", "person")
    assert req.required is False and req.fields == () and req.documents == ()


def _draft_endpoints(*, validation_status=201, validation_body=None):
    responses.add(responses.GET, f"{BASE}/identities", json={"data": []})
    responses.add(responses.POST, f"{BASE}/identities", status=201, json={"data": {"id": "id-1", "type": "identities"}})
    responses.add(responses.POST, f"{BASE}/addresses", status=201, json={"data": {"id": "addr-1", "type": "addresses"}})
    responses.add(responses.GET, f"{BASE}/public_keys", json={"data": [{"id": "k1", "type": "public_keys", "attributes": {"key": PEM}}, {"id": "k2", "type": "public_keys", "attributes": {"key": PEM}}]})
    responses.add(responses.POST, f"{BASE}/encrypted_files", status=201, json={"data": {"id": "file-1", "type": "encrypted_files"}})
    responses.add(responses.POST, f"{BASE}/encrypted_files", status=201, json={"data": {"id": "file-2", "type": "encrypted_files"}})
    responses.add(responses.POST, f"{BASE}/proofs", status=201, json={"data": {"id": "proof-1", "type": "proofs"}})
    responses.add(responses.POST, f"{BASE}/proofs", status=201, json={"data": {"id": "proof-2", "type": "proofs"}})
    responses.add(
        responses.POST,
        f"{BASE}/address_requirement_validations",
        status=validation_status,
        json=validation_body or {"data": {"id": "val-1", "type": "address_requirement_validations"}},
    )


def _inputs():
    fields = {"first_name": "Ana", "last_name": "Silva", "birth_date": "1990-01-02", "id_number": "12345678", "service_description": "Support line"}
    address = Address(customer_name="Ana Silva", street="Rua A 1", city="Lisboa", region="Lisboa", postal_code="1000-001", country_code="PT")
    pdf = UploadedFile(filename="passport.pdf", content_type="application/pdf", data=b"%PDF-1.4 test")
    documents = {
        "identity_proof_1": DocumentInput(option="pt-passport", file=pdf),
        "address_proof_1": DocumentInput(option="pt-bill", file=pdf),
    }
    return fields, address, documents


@responses.activate
async def test_create_draft_happy_path(provider):
    _static()
    _requirement(service_description=True)
    req = await provider.requirements("PT", "national", "person")
    _draft_endpoints()
    fields, address, documents = _inputs()
    result = await provider.create_draft(
        organization_id=ORG, contact_email="ops@hail.test", requirements=req,
        fields=fields, address=address, documents=documents,
    )
    assert result.problems == []
    assert result.refs == {
        "organization_id": ORG,
        "identity_id": "id-1", "identity_created": True, "address_id": "addr-1",
        "proof_ids": ["proof-1", "proof-2"], "file_ids": ["file-1", "file-2"],
        "requirement_id": REQ_ID, "country_code": "PT", "number_type": "national",
    }
    bodies = {c.request.url.split("/v3/")[1].split("?")[0]: c.request for c in responses.calls}
    identity = json.loads(bodies["identities"].body)["data"]
    assert identity["attributes"]["identity_type"] == "personal"
    assert identity["attributes"]["first_name"] == "Ana"
    assert identity["attributes"]["external_reference_id"] == f"hail-{ORG}"
    assert identity["attributes"]["contact_email"] == "ops@hail.test"
    assert identity["relationships"]["country"]["data"] == {"id": COUNTRY_ID, "type": "countries"}
    addr = json.loads(bodies["addresses"].body)["data"]
    assert addr["attributes"] == {"address": "Rua A 1", "city_name": "Lisboa", "postal_code": "1000-001", "description": "Support line", "external_reference_id": f"hail-draft:{ORG}:PT:national"}
    assert addr["relationships"]["identity"]["data"] == {"id": "id-1", "type": "identities"}
    upload = next(c.request for c in responses.calls if c.request.url.endswith("/encrypted_files"))
    assert b"document.pdf" in upload.body and b"%PDF-1.4 test" not in upload.body
    proofs = [json.loads(c.request.body)["data"] for c in responses.calls if c.request.url.endswith("/proofs")]
    assert proofs[0]["relationships"]["entity"]["data"] == {"id": "id-1", "type": "identities"}
    assert proofs[0]["relationships"]["proof_type"]["data"] == {"id": "pt-passport", "type": "proof_types"}
    assert proofs[1]["relationships"]["entity"]["data"] == {"id": "addr-1", "type": "addresses"}
    validation = json.loads(bodies["address_requirement_validations"].body)["data"]["relationships"]
    assert validation["address_requirement"]["data"]["id"] == REQ_ID


@responses.activate
async def test_create_draft_validation_failure_discards_everything(provider):
    _static()
    _requirement()
    req = await provider.requirements("PT", "national", "person")
    _draft_endpoints(validation_status=422, validation_body={"errors": [{"title": "Address in Portugal required", "detail": "Address in Portugal required", "source": {"pointer": "/data"}}]})
    for path in ("proofs/proof-1", "proofs/proof-2", "encrypted_files/file-1", "encrypted_files/file-2", "addresses/addr-1", "identities/id-1"):
        responses.add(responses.DELETE, f"{BASE}/{path}", status=204)
    fields, address, documents = _inputs()
    result = await provider.create_draft(
        organization_id=ORG, contact_email="ops@hail.test", requirements=req,
        fields=fields, address=address, documents=documents,
    )
    assert result.refs == {}
    assert [p.message for p in result.problems] == ["Address in Portugal required"]
    deleted = sorted(c.request.url.split("/v3/")[1] for c in responses.calls if c.request.method == "DELETE")
    assert deleted == ["addresses/addr-1", "encrypted_files/file-1", "encrypted_files/file-2", "identities/id-1", "proofs/proof-1", "proofs/proof-2"]


@responses.activate
async def test_create_draft_reuses_existing_identity(provider):
    _static()
    _requirement()
    req = await provider.requirements("PT", "national", "person")
    responses.add(responses.GET, f"{BASE}/identities", json={"data": [{"id": "id-old", "type": "identities", "attributes": {"external_reference_id": f"hail-{ORG}", "identity_type": "personal"}, "relationships": {"country": {"data": {"id": COUNTRY_ID, "type": "countries"}}}}]})
    responses.add(responses.POST, f"{BASE}/addresses", status=201, json={"data": {"id": "addr-1", "type": "addresses"}})
    responses.add(responses.GET, f"{BASE}/public_keys", json={"data": [{"id": "k1", "type": "public_keys", "attributes": {"key": PEM}}, {"id": "k2", "type": "public_keys", "attributes": {"key": PEM}}]})
    responses.add(responses.POST, f"{BASE}/encrypted_files", status=201, json={"data": {"id": "file-1", "type": "encrypted_files"}})
    responses.add(responses.POST, f"{BASE}/encrypted_files", status=201, json={"data": {"id": "file-2", "type": "encrypted_files"}})
    responses.add(responses.POST, f"{BASE}/proofs", status=201, json={"data": {"id": "proof-1", "type": "proofs"}})
    responses.add(responses.POST, f"{BASE}/proofs", status=201, json={"data": {"id": "proof-2", "type": "proofs"}})
    responses.add(responses.POST, f"{BASE}/address_requirement_validations", status=201, json={"data": {"id": "val-1", "type": "address_requirement_validations"}})
    fields, address, documents = _inputs()
    result = await provider.create_draft(
        organization_id=ORG, contact_email="ops@hail.test", requirements=req,
        fields=fields, address=address, documents=documents,
    )
    assert result.refs["identity_id"] == "id-old" and result.refs["identity_created"] is False
    assert not any(c.request.method == "POST" and c.request.url.endswith("/identities") for c in responses.calls)


@responses.activate
async def test_submit_stamps_approved_reference(provider):
    responses.add(responses.PATCH, f"{BASE}/addresses/addr-1", json={"data": {"id": "addr-1", "type": "addresses"}})
    await provider.submit({"address_id": "addr-1", "organization_id": ORG, "country_code": "PT", "number_type": "national"})
    sent = json.loads(responses.calls[0].request.body)["data"]
    assert sent == {"id": "addr-1", "type": "addresses", "attributes": {"external_reference_id": f"hail:{ORG}:PT:national"}}


@responses.activate
@pytest.mark.parametrize(("ref", "state"), [(f"hail:{ORG}:PT:national", "approved"), (f"hail-draft:{ORG}:PT:national", "draft")])
async def test_status_reads_reference(provider, ref, state):
    responses.add(responses.GET, f"{BASE}/addresses/addr-1", json={"data": {"id": "addr-1", "type": "addresses", "attributes": {"external_reference_id": ref}}})
    assert (await provider.status({"address_id": "addr-1"})).state == state


async def test_purchase_handle(provider):
    assert await provider.purchase_handle({"identity_id": "id-1", "address_id": "addr-1", "proof_ids": []}) == {"identity_id": "id-1", "address_id": "addr-1"}


@responses.activate
async def test_check_reruns_validation(provider):
    responses.add(responses.POST, f"{BASE}/address_requirement_validations", status=422, json={"errors": [{"title": "Identity id number missing", "detail": "Identity id number missing"}]})
    problems = await provider.check({"identity_id": "id-1", "address_id": "addr-1", "requirement_id": REQ_ID})
    assert [p.message for p in problems] == ["Identity id number missing"]


@responses.activate
async def test_discard_keeps_reused_identity(provider):
    for path in ("proofs/proof-1", "encrypted_files/file-1", "addresses/addr-1"):
        responses.add(responses.DELETE, f"{BASE}/{path}", status=204)
    await provider.discard({"identity_id": "id-old", "identity_created": False, "address_id": "addr-1", "proof_ids": ["proof-1"], "file_ids": ["file-1"]})
    assert not any(c.request.url.endswith("/identities/id-old") for c in responses.calls)


def test_registered_when_configured():
    assert isinstance(get_verification_provider("didww"), DidwwVerificationProvider)


def test_not_registered_without_key(monkeypatch):
    from hailhq.core.providers.verification import _INSTANCES
    monkeypatch.setattr(settings, "didww_api_key", "")
    _INSTANCES.pop("didww", None)
    assert get_verification_provider("didww") is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest core/tests/providers/test_didww_verification.py -v`
Expected: FAIL with `ModuleNotFoundError: hailhq.core.providers.verification.didww`

- [ ] **Step 3: Write the plug-in**

`core/hailhq/core/providers/verification/didww.py`:

```python
"""DIDWW plug-in: end-user registration through DIDWW API v3.

This is the only module that knows DIDWW's words for it: identities,
addresses, proofs, encrypted files, address requirements. DIDWW checks the
papers before purchase (``address_requirement_validations``) and approves
them after the DID is bought (``address_verifications``, filed by the
order reconciler in ``providers/voice/didww.py``).
"""

from __future__ import annotations

import asyncio
import logging

from didww.encrypt import Encrypt
from didww.exceptions import DidwwApiError
from hailhq.core.config import settings
from hailhq.core.providers.verification.base import (
    Address,
    DocumentInput,
    DocumentOption,
    DocumentSlot,
    DraftResult,
    FieldSpec,
    Problem,
    ProviderStatus,
    Requirements,
    SubjectType,
    UnsupportedSubjectType,
    VerificationProvider,
    VerificationProviderError,
)
from hailhq.core.providers.voice.didww import (
    approved_address_ref,
    carrier_status,
    didww_client,
    lookup_ids,
)

logger = logging.getLogger(__name__)

_SUBJECT_TO_DIDWW = {"person": "personal", "business": "business"}
_EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "application/pdf": "pdf"}

_BASE_FIELDS: dict[str, tuple[str, ...]] = {
    "person": ("first_name", "last_name"),
    "business": ("company_name",),
}
_LABELS = {
    "first_name": "First name",
    "last_name": "Last name",
    "birth_date": "Date of birth (YYYY-MM-DD)",
    "id_number": "ID document number",
    "personal_tax_id": "Personal tax number",
    "phone_number": "Phone number",
    "company_name": "Company name",
    "company_reg_number": "Company registration number",
    "vat_id": "VAT number",
    "service_description": "What the number is used for",
}
_KINDS = {"phone_number": "phone"}
# Identity attributes DIDWW accepts; everything else stays in Hail's form only.
_IDENTITY_ATTRS = frozenset(_LABELS) - {"service_description"}


def draft_address_ref(org: str, country: str, kind: str) -> str:
    return f"hail-draft:{org}:{country}:{kind}"


def _field(name: str) -> FieldSpec:
    return FieldSpec(name=name, label=_LABELS.get(name, name.replace("_", " ").capitalize()), kind=_KINDS.get(name, "text"))  # type: ignore[arg-type]


def _slots(prefix: str, qty: int, proof_types: list[dict], *, needs_address: bool) -> list[DocumentSlot]:
    options = tuple(
        DocumentOption(key=p["id"], label=p["attributes"]["name"], needs_address=needs_address)
        for p in proof_types
    )
    if not options:
        return []
    return [
        DocumentSlot(
            name=f"{prefix}_{i + 1}",
            label=("Proof of address" if needs_address else "Proof of identity") + (f" {i + 1}" if qty > 1 else ""),
            options=options,
        )
        for i in range(qty)
    ]


def _problems(exc: DidwwApiError) -> list[Problem]:
    return [
        Problem(field="", message=e.get("detail") or e.get("title") or "Not accepted by the carrier.")
        for e in exc.errors
    ] or [Problem(field="", message="Not accepted by the carrier.")]


class DidwwVerificationProvider(VerificationProvider):
    name = "didww"

    def __init__(self) -> None:
        if not settings.didww_api_key:
            raise ValueError("DIDWW_API_KEY is not set")
        self._client = didww_client()

    # -- requirements ----------------------------------------------------

    async def requirements(
        self, country_code: str, number_type: str, subject_type: SubjectType
    ) -> Requirements:
        return await asyncio.to_thread(self._requirements_sync, country_code, number_type, subject_type)

    def _requirement_row(self, country_code: str, number_type: str) -> tuple[dict | None, dict]:
        country_id, types = lookup_ids(country_code)
        type_id = types.get(number_type)
        if not country_id or not type_id:
            return None, {}
        body = self._client.get(
            "address_requirements",
            params={
                "filter[country.id]": country_id,
                "filter[did_group_type.id]": type_id,
                "include": "personal_proof_types,business_proof_types,address_proof_types",
            },
        )
        rows = body.get("data", [])
        index = {(r["type"], r["id"]): r for r in body.get("included", [])}
        return (rows[0] if rows else None), index

    def _requirements_sync(self, country_code: str, number_type: str, subject_type: SubjectType) -> Requirements:
        row, index = self._requirement_row(country_code, number_type)
        if row is None:
            return Requirements(provider=self.name, country_code=country_code, number_type=number_type, subject_type=subject_type, required=False)
        attrs = row["attributes"]
        identity_type = attrs.get("identity_type", "any")
        offered: tuple[SubjectType, ...] = ("business", "person") if identity_type == "any" else (("person",) if identity_type == "personal" else ("business",))
        if subject_type not in offered:
            raise UnsupportedSubjectType(
                f"{subject_type} is not accepted for {number_type} numbers in {country_code}",
                allowed=offered,
            )
        side = _SUBJECT_TO_DIDWW[subject_type]
        names = list(_BASE_FIELDS[subject_type])
        for name in attrs.get(f"{side}_mandatory_fields", []):
            if name not in names:
                names.append(name)
        if attrs.get("service_description_required"):
            names.append("service_description")
        proof_types = lambda rel: [index[("proof_types", r["id"])] for r in row["relationships"].get(rel, {}).get("data", []) if ("proof_types", r["id"]) in index]
        documents = [
            *_slots("identity_proof", attrs.get(f"{side}_proof_qty", 0), proof_types(f"{side}_proof_types"), needs_address=False),
            *_slots("address_proof", attrs.get("address_proof_qty", 0), proof_types("address_proof_types"), needs_address=True),
        ]
        return Requirements(
            provider=self.name,
            country_code=country_code,
            number_type=number_type,
            subject_type=subject_type,
            required=True,
            fields=tuple(_field(n) for n in names),
            documents=tuple(documents),
            address_required=True,
            subject_types=offered,
        )

    # -- draft -----------------------------------------------------------

    async def create_draft(
        self,
        *,
        organization_id: str,
        contact_email: str,
        requirements: Requirements,
        fields: dict[str, str],
        address: Address | None,
        documents: dict[str, DocumentInput],
    ) -> DraftResult:
        return await asyncio.to_thread(
            self._create_draft_sync, organization_id, contact_email, requirements, fields, address, documents
        )

    def _find_identity(self, organization_id: str, country_id: str, identity_type: str) -> str | None:
        found = self._client.get(
            "identities",
            params={"filter[external_reference_id]": f"hail-{organization_id}", "page[size]": 50},
        )["data"]
        for i in found:
            same_country = i.get("relationships", {}).get("country", {}).get("data", {}).get("id") == country_id
            if same_country and i["attributes"].get("identity_type") == identity_type:
                return i["id"]
        return None

    def _create_draft_sync(
        self,
        organization_id: str,
        contact_email: str,
        requirements: Requirements,
        fields: dict[str, str],
        address: Address | None,
        documents: dict[str, DocumentInput],
    ) -> DraftResult:
        if address is None:
            return DraftResult(refs={}, problems=[Problem(field="address", message="An address is required.")])
        row, _ = self._requirement_row(requirements.country_code, requirements.number_type)
        if row is None:
            raise VerificationProviderError("DIDWW requirement not found")
        country_id, _ = lookup_ids(requirements.country_code)
        clean = {k: v.strip() for k, v in fields.items() if v and v.strip()}
        identity_type = _SUBJECT_TO_DIDWW[requirements.subject_type]
        refs: dict = {
            "organization_id": organization_id,
            "country_code": requirements.country_code,
            "number_type": requirements.number_type,
            "requirement_id": row["id"],
            "proof_ids": [],
            "file_ids": [],
        }
        try:
            existing = self._find_identity(organization_id, country_id, identity_type)
            if existing:
                refs["identity_id"], refs["identity_created"] = existing, False
            else:
                identity = self._client.post(
                    "identities",
                    {
                        "data": {
                            "type": "identities",
                            "attributes": {
                                **{k: v for k, v in clean.items() if k in _IDENTITY_ATTRS},
                                "identity_type": identity_type,
                                "external_reference_id": f"hail-{organization_id}",
                                "contact_email": contact_email,
                            },
                            "relationships": {"country": {"data": {"id": country_id, "type": "countries"}}},
                        }
                    },
                )["data"]
                refs["identity_id"], refs["identity_created"] = identity["id"], True
            addr = self._client.post(
                "addresses",
                {
                    "data": {
                        "type": "addresses",
                        "attributes": {
                            "address": address.street,
                            "city_name": address.city,
                            "postal_code": address.postal_code,
                            "description": clean.get("service_description", ""),
                            "external_reference_id": draft_address_ref(organization_id, requirements.country_code, requirements.number_type),
                        },
                        "relationships": {
                            "country": {"data": {"id": country_id, "type": "countries"}},
                            "identity": {"data": {"id": refs["identity_id"], "type": "identities"}},
                        },
                    }
                },
            )["data"]
            refs["address_id"] = addr["id"]
            keys = [k["attributes"]["key"] for k in self._client.get("public_keys")["data"]]
            fingerprint = Encrypt.calculate_fingerprint(keys)
            for slot in requirements.documents:
                doc = documents.get(slot.name)
                if doc is None or doc.file is None:
                    continue
                option = next((o for o in slot.options if o.key == doc.option), None)
                if option is None:
                    self._discard_sync(refs)
                    return DraftResult(refs={}, problems=[Problem(field=slot.name, message="Pick one of the offered document types.")])
                ext = _EXTENSIONS.get(doc.file.content_type, "bin")
                file_id = self._client.upload_encrypted_file(
                    fingerprint, Encrypt.encrypt_with_keys(doc.file.data, keys), filename=f"document.{ext}"
                )
                refs["file_ids"].append(file_id)
                entity = (
                    {"id": refs["address_id"], "type": "addresses"}
                    if option.needs_address
                    else {"id": refs["identity_id"], "type": "identities"}
                )
                proof = self._client.post(
                    "proofs",
                    {
                        "data": {
                            "type": "proofs",
                            "relationships": {
                                "proof_type": {"data": {"id": option.key, "type": "proof_types"}},
                                "entity": {"data": entity},
                                "files": {"data": [{"id": file_id, "type": "encrypted_files"}]},
                            },
                        }
                    },
                )["data"]
                refs["proof_ids"].append(proof["id"])
            problems = self._validate(refs)
        except DidwwApiError as exc:
            self._discard_sync(refs)
            if carrier_status(exc) in (400, 422):
                return DraftResult(refs={}, problems=_problems(exc))
            raise VerificationProviderError("DIDWW request failed") from exc
        except Exception:
            self._discard_sync(refs)
            raise
        if problems:
            self._discard_sync(refs)
            return DraftResult(refs={}, problems=problems)
        return DraftResult(refs=refs)

    def _validate(self, refs: dict) -> list[Problem]:
        try:
            self._client.post(
                "address_requirement_validations",
                {
                    "data": {
                        "type": "address_requirement_validations",
                        "relationships": {
                            "address_requirement": {"data": {"id": refs["requirement_id"], "type": "address_requirements"}},
                            "identity": {"data": {"id": refs["identity_id"], "type": "identities"}},
                            "address": {"data": {"id": refs["address_id"], "type": "addresses"}},
                        },
                    }
                },
            )
        except DidwwApiError as exc:
            if carrier_status(exc) == 422:
                return _problems(exc)
            raise
        return []

    # -- lifecycle -------------------------------------------------------

    async def check(self, refs: dict) -> list[Problem]:
        try:
            return await asyncio.to_thread(self._validate, refs)
        except DidwwApiError as exc:
            raise VerificationProviderError("DIDWW request failed") from exc

    async def submit(self, refs: dict) -> None:
        """Approval is stamped on the address: discovery finds it by this
        reference and treats the organization as registered."""
        ref = approved_address_ref(refs["organization_id"], refs["country_code"], refs["number_type"])

        def run() -> None:
            self._client.patch(
                f"addresses/{refs['address_id']}",
                {"data": {"id": refs["address_id"], "type": "addresses", "attributes": {"external_reference_id": ref}}},
            )

        try:
            await asyncio.to_thread(run)
        except DidwwApiError as exc:
            raise VerificationProviderError("DIDWW request failed") from exc

    async def status(self, refs: dict) -> ProviderStatus:
        def run() -> ProviderStatus:
            addr = self._client.get(f"addresses/{refs['address_id']}")["data"]
            ref = addr["attributes"].get("external_reference_id") or ""
            return ProviderStatus(state="approved" if ref.startswith("hail:") else "draft")

        try:
            return await asyncio.to_thread(run)
        except DidwwApiError as exc:
            raise VerificationProviderError("DIDWW request failed") from exc

    async def purchase_handle(self, refs: dict) -> dict:
        return {"identity_id": refs["identity_id"], "address_id": refs["address_id"]}

    async def discard(self, refs: dict) -> None:
        await asyncio.to_thread(self._discard_sync, refs)

    def _discard_sync(self, refs: dict) -> None:
        paths = [f"proofs/{p}" for p in refs.get("proof_ids", [])]
        paths += [f"encrypted_files/{f}" for f in refs.get("file_ids", [])]
        if refs.get("address_id"):
            paths.append(f"addresses/{refs['address_id']}")
        if refs.get("identity_id") and refs.get("identity_created"):
            paths.append(f"identities/{refs['identity_id']}")
        for path in paths:
            try:
                self._client.delete(path)
            except Exception:
                logger.warning("didww discard of %s failed", path, exc_info=True)
```

`core/hailhq/core/providers/verification/__init__.py`, in `_register_builtin`:

```python
def _register_builtin() -> None:
    from hailhq.core.providers.verification.didww import DidwwVerificationProvider
    from hailhq.core.providers.verification.twilio import (
        TwilioVerificationProvider,
    )

    register_verification_provider("twilio", TwilioVerificationProvider)
    register_verification_provider("didww", DidwwVerificationProvider)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest core/tests/providers/test_didww_verification.py -v`
Expected: 16 passed

- [ ] **Step 5: Run the full core and api suites**

Run: `uv run pytest core/tests -q && uv run pytest api/tests -q`
Expected: all pass. `api/tests/test_verifications_api.py` uses a fake registry, so the new plug-in does not change it.

- [ ] **Step 6: Commit**

```bash
git add core/hailhq/core/providers/verification/didww.py core/hailhq/core/providers/verification/__init__.py core/tests/providers/test_didww_verification.py
git commit -m "feat(didww): verification plug-in for end-user registration"
```

---

### Task 8: Docs, env, changelog

**Files:**
- Modify: `docs/public/self-host/didww.md` (intro and section 4)
- Modify: `CHANGELOG.md` (Unreleased)
- Modify: local `.env` (not committed): add `DIDWW_API_KEY=` and `DIDWW_ENVIRONMENT=production` in the same position as `.env.example`.

- [ ] **Step 1: Rewrite the intro and section 4 of `docs/public/self-host/didww.md`**

Replace the paragraph starting "Not supported on DIDWW yet" with:

````
Buying goes through the normal quote flow once `DIDWW_API_KEY` is set. Not
supported on DIDWW: SMS (offers are voice only) and inbound calls.
````

Replace section "## 4. Register the number in Hail" with:

````
## 4. API key

my.didww.com → **API** → create a key. Put it in `.env`:

```
DIDWW_API_KEY=<key>
DIDWW_ENVIRONMENT=production
```

`DIDWW_ENVIRONMENT=sandbox` points every call at `sandbox-api.didww.com`
(sandbox key from the Sandbox User Panel → API → DIDWW API 3). Restart `api`.

## 5. Buying a number

```bash
curl -X POST "$HAIL_API_URL/numbers/quotes" -H "Authorization: Bearer $HAIL_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"country_code":"PT","number_type":"national","capabilities":["voice"]}'
# → offers[].provider == "didww", readiness "ready" or "verification_required"
curl -X POST "$HAIL_API_URL/numbers" -H "Authorization: Bearer $HAIL_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"country_code":"PT","number_type":"national","quote_id":"<quote_id>"}'
```

Order of events for a country that needs end-user registration:

1. `readiness: verification_required` → the customer fills `/verifications`
   (console wizard). Hail creates the identity, address and proofs at DIDWW and
   validates them (`address_requirement_validations`). A superadmin approves.
2. `POST /numbers` reserves setup + first month, orders the DID
   (`provisioning_state: pending`).
3. The reconciler files the registration (`address_verifications`) once the
   DID exists and polls it. DIDWW approves in 1–3 days → `active`.
4. Rejected → `failed`, the DID is terminated, the monthly fee is refunded,
   the setup fee stays. A pending order is failed and refunded after 7 days.

Schemas: [`openapi/openapi.yaml`](../../../openapi/openapi.yaml). Code:
[`providers/voice/didww.py`](../../../core/hailhq/core/providers/voice/didww.py),
[`providers/verification/didww.py`](../../../core/hailhq/core/providers/verification/didww.py),
[`number_orders.py`](../../../api/hailhq/api/number_orders.py).
````

Keep the last two paragraphs about `carrier_route_failed`.

- [ ] **Step 2: Changelog**

Under the Unreleased section of `CHANGELOG.md` (create the section if missing, matching the file's existing style):

```
- DIDWW numbers can be quoted and bought through `POST /numbers/quotes` and
  `POST /numbers`; end-user registration runs through `/verifications`
  (`provider=didww`). New settings `DIDWW_API_KEY`, `DIDWW_ENVIRONMENT`.
```

- [ ] **Step 3: Prettier and commit**

```bash
pnpm exec prettier --write docs/public/self-host/didww.md CHANGELOG.md
git add docs/public/self-host/didww.md CHANGELOG.md
git commit -m "docs(didww): buying numbers through Hail"
```

---

### Task 9: Sandbox smoke test (manual)

**Files:** none committed. Needs a DIDWW **sandbox** API key (Sandbox User Panel → API → DIDWW API 3). With only a production key, run step 1 (read-only) and stop.

- [ ] **Step 1: Quotes against DIDWW (read-only, safe on production)**

Set `DIDWW_API_KEY` and `DIDWW_ENVIRONMENT` in `.env`, start the API locally, then:

```bash
curl -s -X POST "http://localhost:8080/numbers/quotes" -H "Authorization: Bearer $HAIL_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"country_code":"PT","number_type":"national","capabilities":["voice"],"provider":"didww"}' | python3 -m json.tool
```

Expected: one offer with `provider: didww`, `monthly_cents: 350`, `setup_cents: 350`, `readiness: verification_required`, `regulatory_friction: documents`.

- [ ] **Step 2: Requirements**

```bash
curl -s "http://localhost:8080/verifications/requirements?country_code=PT&number_type=national&subject_type=person&provider=didww" -H "Authorization: Bearer $HAIL_API_KEY" | python3 -m json.tool
```

Expected: `required: true`, fields include `first_name`, `last_name`, documents `identity_proof_1` and `address_proof_1`.

- [ ] **Step 3: Full flow (sandbox only)**

With `DIDWW_ENVIRONMENT=sandbox`: submit a verification with test documents through the console wizard, approve it as superadmin, buy the quote, then watch `GET /numbers/{id}` go `pending → active` (the sandbox may approve registrations at once or never; if it stays pending, check `provisioning_metadata.order_id` in the DIDWW sandbox panel). Record what the sandbox did in the PR description.

- [ ] **Step 4: Push and open the PR**

```bash
git push -u origin feat/didww-purchase
gh pr create --base main --title "feat(didww): quote, buy and register DIDWW numbers" --body-file <(printf '%s\n' "Implements docs/superpowers/specs/2026-09-26-didww-purchase-design.md." "" "See the spec for the flow. Smoke test results: <fill in from Task 9>.")
```

## Self-review notes

- Spec coverage: quotes (T3, T5), registration validation and approval (T7), order → verification → active (T4, T5), 7-day timeout (T2), rejection partial refund (T5), release (T4, T5), settings/env (T1), catalog gate per carrier (T5), docs (T8), sandbox smoke (T9). Spec amended in T6 for where approval is recorded.
- Names used across tasks: `didww_client`, `carrier_status`, `lookup_ids`, `approved_address_ref`, `didww_offers`, `place_didww_order`, `didww_order_outcome`, `terminate_did`, `release_didww_number`, `OrderState`, `Carrier.pending_timeout`, `finish_order(keep_setup=)`, `DidwwVerificationProvider`, `draft_address_ref`.
- Not built on purpose: DID reservations, DIDWW SMS, inbound trunks, order/verification callbacks (polling is enough at this volume).
