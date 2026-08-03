# SMS Numbers & Sender ID Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A generic, cross-channel number-provisioning API (acquire a dedicated number, attach it to a Twilio Messaging Service for SMS), an org-level custom Sender ID setting, and Sender ID resolution wired into outbound sends so international destinations in no-registration corridors can send via alphanumeric ID without requiring a dedicated number.

**Architecture:** `POST /numbers` reuses `VoiceProvider.acquire_number` (already generic — it isn't voice-specific in behavior) to purchase a number and persist a `PhoneNumber` row. A new `POST /numbers/{id}/enable-sms` validates the number's _physical_ capabilities (fixed at Twilio purchase time — cannot be toggled after the fact, a corrected assumption from the design spec, see Global Constraints) and creates/attaches a per-org Twilio Messaging Service. A new `sender_id.py` module classifies a destination E.164 into one of four Sender ID outcomes and is wired into the existing `POST /sms` route's from-resolution logic, making the dedicated-number requirement conditional on destination rather than universal.

**Tech Stack:** FastAPI + SQLAlchemy 2 (async) + Alembic, Twilio's Messaging Service API (`client.messaging.v1.services`), reusing `TwilioVoiceProvider.acquire_number`.

## Global Constraints

- **This phase assumes both SMS Outbound Core and SMS Inbound & Compliance are already merged.**
- **Corrected assumption from the design spec**: Twilio phone number capabilities (`voice_enabled`/`sms_enabled`/`mms_enabled`) are fixed by the carrier network at the time of purchase and **cannot be toggled via API afterward**. The spec's `PATCH /numbers/{id}/capabilities` is therefore replaced with `POST /numbers/{id}/enable-sms`, which validates the number's already-physically-present `sms` capability (stored in `PhoneNumber.capabilities`, populated from Twilio's own response at purchase time) rather than attempting to grant a capability the carrier didn't provision. If a number lacks physical SMS capability, the response is a 422 telling the caller to acquire a new number — there is no way to add it to an existing one.
- **No `PATCH /numbers/{id}/capabilities` generic endpoint** in this phase — only the specific `enable-sms` action, since that's the only concrete capability-activation flow needed right now (matches the spec's "capability toggle" intent without pretending a general capability-mutation API is technically meaningful for Twilio-backed numbers).
- **Dedicated-number requirement becomes conditional on destination**, not universal. `POST /sms` (from Phase 1) always required a dedicated number for any destination. This phase changes that: US/Canada destinations still always require one (Decision 6, unchanged); destinations in a Sender-ID-eligible corridor (no pre-registration required) can send via the resolved alphanumeric Sender ID with **no dedicated number needed at all** — this is the whole point of Sender ID being "outbound-only, international-only, no inbound path."
- **Only researched corridors get real Sender ID classification** (US, Canada, UK, Germany, Australia, India — per the design spec's research). Any other destination conservatively falls back to requiring the dedicated number, since its local Sender ID rules haven't been researched. This is a deliberate, stated fallback, not an oversight.
- **`SmsSenderIdentity` is a new small table** keyed by `organization_id` (hail's DB doesn't own the `Organization` table itself — it lives in hail-website's better-auth schema — so this follows the same `organization_id`-without-FK convention as `OrgClosure`).
- **Migrations continue the chain** — the current head on `main` is **`0027`** (chain: `0025_sms.py` → `0026_suppressions_sms_channel.py` → `0027_sms_events.py`). This plan's new migrations therefore start at **`0028`** (`down_revision="0027"`). Confirm the head is still `0027` via `ls api/migrations/versions/ | sort | tail -3` at implementation time and bump every migration number below if it has advanced further.

---

## File Structure

```
core/hailhq/core/providers/sms/base.py       # + ensure_messaging_service, attach_number on SmsProvider
core/hailhq/core/providers/sms/twilio.py     # + Twilio Messaging Service API calls
core/hailhq/core/sender_id.py                # new — corridor classification + resolution
core/hailhq/core/models.py                   # + messaging_service_sid on PhoneNumber, + SmsSenderIdentity
core/hailhq/core/schemas.py                  # + PhoneNumberResponse/ListResponse, NumberAcquire, SenderIdConfig

api/migrations/versions/0028_phone_number_messaging_service.py   # new column (head is 0027)
api/migrations/versions/0029_sms_sender_identities.py            # new table
api/hailhq/api/routes/numbers.py             # new — generic /numbers resource (NOTE: api/hailhq/api/numbers.py already exists as the resolve_org_number helper; different path, no collision — do not overwrite it)
api/hailhq/api/routes/sms.py                 # modified — conditional from-resolution via sender_id.py, sender-id GET/PATCH
api/hailhq/api/main.py                       # + numbers router registration

core/tests/providers/test_twilio_sms.py      # + messaging service tests
core/tests/test_sender_id.py                 # new
api/tests/test_numbers_api.py                # new
api/tests/test_sms_sender_id_api.py          # new
api/tests/test_sms_api.py                    # + conditional-resolution regression tests

cli/internal/cmd/number.go                   # new
cli/internal/cmd/sms.go                      # + sender-id subcommand
sdk/hail/models.py, sdk/hail/client.py       # + numbers resource, sender_id methods
```

---

### Task 1: `ensure_messaging_service`/`attach_number` on `SmsProvider`

**Files:**

- Modify: `core/hailhq/core/providers/sms/base.py`
- Modify: `core/hailhq/core/providers/sms/twilio.py`
- Test: `core/tests/providers/test_twilio_sms.py` (append)

**Interfaces:**

- Produces: `SmsProvider.ensure_messaging_service(organization_id: UUID, existing_sid: str | None) -> str` (returns the Messaging Service SID, creating one if `existing_sid` is None); `SmsProvider.attach_number(messaging_service_sid: str, provider_resource_id: str) -> None`.

- [ ] **Step 1: Write the failing test**

```python
# core/tests/providers/test_twilio_sms.py — append
import uuid


@responses.activate
async def test_ensure_messaging_service_creates_when_none_exists(provider: TwilioSmsProvider) -> None:
    org_id = uuid.uuid4()
    responses.add(
        responses.POST,
        f"{API_BASE}/../../v1/Services".replace("2010-04-01/Accounts/ACtest1234567890abcdef1234567890ab/..", "messaging"),
        json={"sid": "MG_test_service", "friendly_name": f"hail-org-{org_id}"},
        status=201,
    )
    # NOTE: messaging.v1.services lives under https://messaging.twilio.com,
    # not the 2010-04-01 Accounts base — verify the real base URL against
    # the installed twilio SDK's Domain definition before finalizing this
    # mock URL; adjust the `responses.add` target to match exactly.
    sid = await provider.ensure_messaging_service(organization_id=org_id, existing_sid=None)
    assert sid == "MG_test_service"


async def test_ensure_messaging_service_returns_existing_without_api_call(
    provider: TwilioSmsProvider,
) -> None:
    org_id = uuid.uuid4()
    sid = await provider.ensure_messaging_service(organization_id=org_id, existing_sid="MG_already_have_one")
    assert sid == "MG_already_have_one"


@responses.activate
async def test_attach_number_calls_phone_numbers_create(provider: TwilioSmsProvider) -> None:
    responses.add(
        responses.POST,
        "https://messaging.twilio.com/v1/Services/MG_test_service/PhoneNumbers",
        json={"sid": "PN_link", "phone_number_sid": "PN1234567890abcdef1234567890abcd"},
        status=201,
    )
    await provider.attach_number(
        messaging_service_sid="MG_test_service",
        provider_resource_id="PN1234567890abcdef1234567890abcd",
    )
    sent_body = parse_qs(responses.calls[0].request.body)
    assert sent_body == {"PhoneNumberSid": ["PN1234567890abcdef1234567890abcd"]}
```

**Note before running**: the exact base URL for Twilio's Messaging Service API is `https://messaging.twilio.com/v1/Services` (confirmed structurally from the installed SDK's `twilio/rest/messaging/v1/__init__.py`/`service/__init__.py`) — fix the first test's mock URL to that exact literal rather than the placeholder string-substitution shown above (written deliberately awkwardly to force verification rather than copy-paste a possibly-wrong guess).

- [ ] **Step 2: Run test to verify it fails**

Run: `cd core && uv run pytest tests/providers/test_twilio_sms.py -v -k messaging`
Expected: FAIL with `AttributeError: 'TwilioSmsProvider' object has no attribute 'ensure_messaging_service'`

- [ ] **Step 3: Add the abstract methods**

In `core/hailhq/core/providers/sms/base.py`, add to `SmsProvider`:

```python
    @abstractmethod
    async def ensure_messaging_service(
        self, organization_id: UUID, existing_sid: str | None
    ) -> str:
        """Return a Messaging Service SID for this org — the existing one
        if provided, otherwise create a new one and return its SID."""

    @abstractmethod
    async def attach_number(self, messaging_service_sid: str, provider_resource_id: str) -> None:
        """Attach a purchased phone number (by its Twilio SID) to a
        Messaging Service's sender pool."""
```

Add `from uuid import UUID` to the imports if not already present.

- [ ] **Step 4: Implement in `TwilioSmsProvider`**

```python
    async def ensure_messaging_service(
        self, organization_id: UUID, existing_sid: str | None
    ) -> str:
        if existing_sid is not None:
            return existing_sid
        service = await asyncio.to_thread(
            self._client.messaging.v1.services.create,
            friendly_name=f"hail-org-{organization_id}",
        )
        return service.sid

    async def attach_number(self, messaging_service_sid: str, provider_resource_id: str) -> None:
        await asyncio.to_thread(
            self._client.messaging.v1.services(messaging_service_sid).phone_numbers.create,
            phone_number_sid=provider_resource_id,
        )
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd core && uv run pytest tests/providers/test_twilio_sms.py -v`
Expected: all passed (existing Phase 1 tests + 3 new)

- [ ] **Step 6: Commit**

```bash
git add core/hailhq/core/providers/sms/base.py core/hailhq/core/providers/sms/twilio.py core/tests/providers/test_twilio_sms.py
git commit -m "feat(core): add Twilio Messaging Service creation and number attachment"
```

---

### Task 2: `PhoneNumber.messaging_service_sid` + generic `/numbers` API (acquire, list, get)

**Files:**

- Modify: `core/hailhq/core/models.py`
- Modify: `core/hailhq/core/schemas.py`
- Create: `api/migrations/versions/0028_phone_number_messaging_service.py`
- Create: `api/hailhq/api/routes/numbers.py` (NOTE: `api/hailhq/api/numbers.py` already exists as the `resolve_org_number` read-side helper — this is a _different_ path under `routes/`; do not confuse or overwrite it)
- Modify: `api/hailhq/api/main.py`
- Test: `api/tests/test_numbers_api.py`

**Interfaces:**

- Produces: `PhoneNumberResponse`, `PhoneNumberListResponse`, `NumberAcquireRequest` schemas; `POST /numbers`, `GET /numbers`, `GET /numbers/{id}` routes.

- [ ] **Step 1: Write the failing test**

```python
# api/tests/test_numbers_api.py
"""Tests for POST/GET /numbers — generic cross-channel number provisioning."""

from __future__ import annotations

import uuid


async def test_acquire_number_requires_auth(client) -> None:
    resp = await client.post("/numbers", json={"country_code": "US", "number_type": "local"})
    assert resp.status_code == 401


async def test_acquire_number_happy_path(client, org_and_key, voice_provider_mock) -> None:
    _, _, plaintext = org_and_key
    resp = await client.post(
        "/numbers",
        json={"country_code": "US", "number_type": "local"},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["e164"]
    assert body["is_dedicated"] is True
    assert "sms" in body["capabilities"] or "voice" in body["capabilities"]


async def test_get_number_not_found(client, org_and_key) -> None:
    _, _, plaintext = org_and_key
    resp = await client.get(f"/numbers/{uuid.uuid4()}", headers={"Authorization": f"Bearer {plaintext}"})
    assert resp.status_code == 404


async def test_list_numbers_scoped_to_org(client, async_session, org_and_key) -> None:
    from hailhq.core.models import PhoneNumber

    org_id, _, plaintext = org_and_key
    async_session.add(
        PhoneNumber(
            organization_id=org_id, e164="+14155551111", country_code="US", number_type="local",
            provider_resource_id="PN_a", provisioning_state="active",
        )
    )
    await async_session.commit()

    resp = await client.get("/numbers", headers={"Authorization": f"Bearer {plaintext}"})
    assert resp.status_code == 200
    assert len(resp.json()["items"]) == 1
```

Note: this test file assumes a `voice_provider_mock` fixture exists in `conftest.py` already (check — if `POST /numbers` uses the `VoiceProvider` interface for acquisition, confirm whether `livekit_mock`/an existing voice-provider-adjacent fixture already covers `acquire_number`, or add a new `voice_provider_mock` fixture in `conftest.py` mirroring `sms_mock`'s shape but for `VoiceProvider`, with `mock.acquire_number.return_value = ProviderNumber(provider_resource_id="PN_test", e164="+14155550001", country_code="US", capabilities=["voice","sms"], number_type="local")`).

- [ ] **Step 2: Run test to verify it fails**

Run: `cd api && uv run pytest tests/test_numbers_api.py -v`
Expected: FAIL — 404 on nonexistent routes / missing fixture.

- [ ] **Step 3: Add the `messaging_service_sid` column**

In `core/hailhq/core/models.py`'s `PhoneNumber` class, add after `provisioning_metadata`:

```python
    messaging_service_sid: Mapped[str | None] = mapped_column(Text, nullable=True)
```

```python
# api/migrations/versions/0028_phone_number_messaging_service.py
"""Add messaging_service_sid to phone_numbers.

Revision ID: 0028
Revises: 0027
"""

from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0028"
down_revision: Union[str, None] = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("phone_numbers", sa.Column("messaging_service_sid", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("phone_numbers", "messaging_service_sid")
```

Run: `cd api && uv run alembic upgrade head` — expect `Running upgrade 0027 -> 0028`.

- [ ] **Step 4: Add schemas**

In `core/hailhq/core/schemas.py`, after the Sms-related schemas:

```python
class NumberAcquireRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    country_code: str = Field(min_length=2, max_length=2)
    number_type: NumberType = "local"


class PhoneNumberResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    e164: str
    country_code: str
    number_type: str
    capabilities: list[str]
    provisioning_state: str
    is_dedicated: bool = Field(validation_alias="is_pool", serialization_alias="is_dedicated")

    @field_validator("is_dedicated", mode="before")
    @classmethod
    def _invert_is_pool(cls, v: bool) -> bool:
        return not v


class PhoneNumberListResponse(BaseModel):
    items: list[PhoneNumberResponse]
    next_cursor: str | None = None
```

Note: the `is_dedicated`/`is_pool` inversion via `validation_alias` is a genuine Pydantic v2 pattern but double-check it round-trips correctly with `from_attributes=True` reading off the ORM object's `is_pool` attribute during implementation — if the alias-plus-validator combination doesn't behave as expected against a real SQLAlchemy instance, the simpler fallback is a plain computed field: drop the alias, keep the model's own `is_pool: bool` field name, and expose the friendlier name only at the API-consumer documentation level (field description). Verify with a real test before committing to the alias approach.

- [ ] **Step 5: Write the route**

```python
# api/hailhq/api/routes/numbers.py
"""Routes for generic, cross-channel dedicated-number provisioning.

Not SMS-specific: a dedicated PhoneNumber is a shared resource across
voice, SMS, and (later) MMS. This module only covers acquisition and
listing; SMS-specific activation (`POST /numbers/{id}/enable-sms`) lives
in `routes/sms.py` since it's SMS-shaped (Messaging Service attachment),
not because numbers themselves are SMS-only.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import status as http_status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.api.deps import Principal, get_current_principal
from hailhq.api.pagination import fetch_cursor_page
from hailhq.core.db import get_session
from hailhq.core.models import PhoneNumber
from hailhq.core.providers.voice import VoiceProvider
from hailhq.core.schemas import (
    NumberAcquireRequest,
    PhoneNumberListResponse,
    PhoneNumberResponse,
)

router = APIRouter(prefix="/numbers", tags=["numbers"])

_DEFAULT_LIST_LIMIT = 50
_MAX_LIST_LIMIT = 200

# Reuses the calls.py get_livekit-style lazy-singleton pattern for the
# voice provider used to acquire a physical number (acquire_number is a
# carrier-numbers concern, not a call-dialing concern, per
# providers/voice/base.py's own docstring).
_voice_provider_singleton: VoiceProvider | None = None


def get_voice_provider() -> VoiceProvider:
    global _voice_provider_singleton
    if _voice_provider_singleton is None:
        from hailhq.core.providers.voice import TwilioVoiceProvider

        _voice_provider_singleton = TwilioVoiceProvider()
    return _voice_provider_singleton


@router.post("", response_model=PhoneNumberResponse, status_code=http_status.HTTP_201_CREATED)
async def acquire_number(
    body: NumberAcquireRequest,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    provider: Annotated[VoiceProvider, Depends(get_voice_provider)],
) -> PhoneNumberResponse:
    try:
        acquired = await provider.acquire_number(
            country_code=body.country_code,
            number_type=body.number_type,
            capabilities=["voice", "sms"],
        )
    except LookupError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc

    number = PhoneNumber(
        organization_id=principal.organization_id,
        e164=acquired.e164,
        country_code=acquired.country_code,
        number_type=acquired.number_type,
        capabilities=acquired.capabilities,
        provider_resource_id=acquired.provider_resource_id,
        provisioning_state="active",
        is_pool=False,
    )
    db.add(number)
    await db.commit()
    await db.refresh(number)
    return PhoneNumberResponse.model_validate(number)


@router.get("/{number_id}", response_model=PhoneNumberResponse)
async def get_number(
    number_id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> PhoneNumberResponse:
    stmt = select(PhoneNumber).where(
        PhoneNumber.id == number_id, PhoneNumber.organization_id == principal.organization_id
    )
    number = (await db.execute(stmt)).scalar_one_or_none()
    if number is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="number not found")
    return PhoneNumberResponse.model_validate(number)


@router.get("", response_model=PhoneNumberListResponse)
async def list_numbers(
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    cursor: str | None = Query(default=None),
    limit: int = Query(default=_DEFAULT_LIST_LIMIT, ge=1, le=_MAX_LIST_LIMIT),
) -> PhoneNumberListResponse:
    stmt = select(PhoneNumber).where(
        PhoneNumber.organization_id == principal.organization_id,
        PhoneNumber.is_pool.is_(False),
    )
    rows, next_cursor = await fetch_cursor_page(
        db, stmt, PhoneNumber.created_at, PhoneNumber.id, cursor=cursor, limit=limit, newest_first=True
    )
    return PhoneNumberListResponse(
        items=[PhoneNumberResponse.model_validate(r) for r in rows],
        next_cursor=next_cursor,
    )


__all__ = ["router", "get_voice_provider"]
```

Register in `main.py`: add `from hailhq.api.routes import numbers as numbers_routes` and `app.include_router(numbers_routes.router)`.

- [ ] **Step 6: Add the `voice_provider_mock` fixture if it doesn't exist**

Check `api/tests/conftest.py` first — if no such fixture exists for `VoiceProvider` (distinct from `livekit_mock`, which mocks LiveKit, not the carrier-numbers `VoiceProvider`), add:

```python
@pytest.fixture()
def voice_provider_mock() -> AsyncMock:
    from hailhq.core.providers.voice import ProviderNumber, VoiceProvider

    mock = AsyncMock(spec=VoiceProvider)
    mock.acquire_number.return_value = ProviderNumber(
        provider_resource_id="PN_test_acquired", e164="+14155550001",
        country_code="US", capabilities=["voice", "sms"], number_type="local",
    )
    return mock
```

and wire it into the `client` fixture's `dependency_overrides` for `get_voice_provider` (import from `hailhq.api.routes.numbers`), same pattern as `get_sms_provider`.

- [ ] **Step 7: Run tests to verify they pass**

Run: `cd api && uv run pytest tests/test_numbers_api.py -v`
Expected: 4 passed

- [ ] **Step 8: Run full API regression suite**

Run: `cd api && uv run pytest -v`
Expected: all passed

- [ ] **Step 9: Commit**

```bash
git add core/hailhq/core/models.py core/hailhq/core/schemas.py api/migrations/versions/0028_phone_number_messaging_service.py api/hailhq/api/routes/numbers.py api/hailhq/api/main.py api/tests/test_numbers_api.py api/tests/conftest.py
git commit -m "feat(api): add generic POST/GET /numbers dedicated-number provisioning"
```

---

### Task 3: `POST /numbers/{id}/enable-sms`

**Files:**

- Modify: `api/hailhq/api/routes/numbers.py` (or `sms.py` — see note below)
- Test: `api/tests/test_numbers_api.py` (append)

**Interfaces:**

- Consumes: `ensure_messaging_service`/`attach_number` (Task 1).
- Produces: `POST /numbers/{id}/enable-sms`.

Note: this route lives in `numbers.py` (not `sms.py`) despite being SMS-specific in effect, since it operates on a `PhoneNumber` resource by id and the plan's file-structure section already placed it there — adjust if a fresh read of both files at implementation time suggests otherwise, but keep it in one file, not split.

- [ ] **Step 1: Write the failing test**

```python
# api/tests/test_numbers_api.py — append
async def test_enable_sms_rejects_number_without_sms_capability(
    client, async_session, org_and_key
) -> None:
    from hailhq.core.models import PhoneNumber

    org_id, _, plaintext = org_and_key
    pn = PhoneNumber(
        organization_id=org_id, e164="+14155552222", country_code="US", number_type="local",
        provider_resource_id="PN_voice_only", provisioning_state="active",
        capabilities=["voice"],  # no sms
    )
    async_session.add(pn)
    await async_session.commit()

    resp = await client.post(
        f"/numbers/{pn.id}/enable-sms", headers={"Authorization": f"Bearer {plaintext}"}
    )
    assert resp.status_code == 422
    assert "does not support sms" in resp.json()["detail"].lower()


async def test_enable_sms_creates_messaging_service_and_attaches(
    client, async_session, org_and_key, sms_mock
) -> None:
    from hailhq.core.models import PhoneNumber

    org_id, _, plaintext = org_and_key
    pn = PhoneNumber(
        organization_id=org_id, e164="+14155553333", country_code="US", number_type="local",
        provider_resource_id="PN_sms_ok", provisioning_state="active",
        capabilities=["voice", "sms"],
    )
    async_session.add(pn)
    await async_session.commit()

    sms_mock.ensure_messaging_service.return_value = "MG_new_service"

    resp = await client.post(
        f"/numbers/{pn.id}/enable-sms", headers={"Authorization": f"Bearer {plaintext}"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["messaging_service_sid"] == "MG_new_service"
    sms_mock.attach_number.assert_awaited_once_with(
        messaging_service_sid="MG_new_service", provider_resource_id="PN_sms_ok"
    )
```

(`sms_mock` here is the same fixture from Phase 1's `conftest.py`, extended to be an `AsyncMock(spec=SmsProvider)` that now also has `ensure_messaging_service`/`attach_number` mockable — confirm the fixture's `spec=SmsProvider` still auto-covers the two new abstract methods from Task 1, which it should since `spec=` introspects the class at fixture-construction time.)

Add `messaging_service_sid: str | None` to `PhoneNumberResponse` (Task 2's schema) so the test above can assert on it.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd api && uv run pytest tests/test_numbers_api.py -v -k enable_sms`
Expected: FAIL — 404 on nonexistent route.

- [ ] **Step 3: Write the route**

Add to `numbers.py` (importing `SmsProvider`/`get_sms_provider` from `routes.sms`, and `PhoneNumberResponse` already imported):

```python
from hailhq.api.routes.sms import get_sms_provider
from hailhq.core.providers.sms import SmsProvider


@router.post("/{number_id}/enable-sms", response_model=PhoneNumberResponse)
async def enable_sms(
    number_id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    provider: Annotated[SmsProvider, Depends(get_sms_provider)],
) -> PhoneNumberResponse:
    stmt = select(PhoneNumber).where(
        PhoneNumber.id == number_id, PhoneNumber.organization_id == principal.organization_id
    )
    number = (await db.execute(stmt)).scalar_one_or_none()
    if number is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="number not found")

    if "sms" not in number.capabilities:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "this number does not support sms (fixed at purchase time by the "
                "carrier); acquire a new number with sms capability instead"
            ),
        )

    messaging_service_sid = await provider.ensure_messaging_service(
        organization_id=principal.organization_id, existing_sid=number.messaging_service_sid
    )
    await provider.attach_number(
        messaging_service_sid=messaging_service_sid,
        provider_resource_id=number.provider_resource_id,
    )

    number.messaging_service_sid = messaging_service_sid
    await db.commit()
    await db.refresh(number)
    return PhoneNumberResponse.model_validate(number)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd api && uv run pytest tests/test_numbers_api.py -v`
Expected: 6 passed

- [ ] **Step 5: Run full API regression suite**

Run: `cd api && uv run pytest -v`
Expected: all passed

- [ ] **Step 6: Commit**

```bash
git add api/hailhq/api/routes/numbers.py core/hailhq/core/schemas.py api/tests/test_numbers_api.py
git commit -m "feat(api): add POST /numbers/{id}/enable-sms"
```

---

### Task 4: `SmsSenderIdentity` + `GET/PATCH /sms/sender-id`

**Files:**

- Modify: `core/hailhq/core/models.py`
- Modify: `core/hailhq/core/schemas.py`
- Modify: `api/hailhq/api/routes/sms.py`
- Create: `api/migrations/versions/0029_sms_sender_identities.py`
- Test: `api/tests/test_sms_sender_id_api.py`

**Interfaces:**

- Produces: `SmsSenderIdentity` model; `SenderIdConfig`/`SenderIdResponse` schemas; `GET /sms/sender-id`, `PATCH /sms/sender-id`.

- [ ] **Step 1: Write the failing test**

```python
# api/tests/test_sms_sender_id_api.py
"""Tests for GET/PATCH /sms/sender-id."""

from __future__ import annotations


async def test_get_sender_id_defaults_to_hail(client, org_and_key) -> None:
    _, _, plaintext = org_and_key
    resp = await client.get("/sms/sender-id", headers={"Authorization": f"Bearer {plaintext}"})
    assert resp.status_code == 200
    assert resp.json()["custom_sender_id"] is None
    assert resp.json()["effective_default"] == "HAIL"


async def test_patch_sender_id_sets_custom_value(client, org_and_key) -> None:
    _, _, plaintext = org_and_key
    resp = await client.patch(
        "/sms/sender-id", json={"custom_sender_id": "ACME"}, headers={"Authorization": f"Bearer {plaintext}"}
    )
    assert resp.status_code == 200
    assert resp.json()["custom_sender_id"] == "ACME"

    resp = await client.get("/sms/sender-id", headers={"Authorization": f"Bearer {plaintext}"})
    assert resp.json()["custom_sender_id"] == "ACME"


async def test_patch_sender_id_rejects_too_long(client, org_and_key) -> None:
    _, _, plaintext = org_and_key
    resp = await client.patch(
        "/sms/sender-id",
        json={"custom_sender_id": "WAYTOOLONGID"},  # 12 chars, over the 11-char GSM limit
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 422


async def test_patch_sender_id_rejects_non_alphanumeric(client, org_and_key) -> None:
    _, _, plaintext = org_and_key
    resp = await client.patch(
        "/sms/sender-id", json={"custom_sender_id": "AC-ME!"}, headers={"Authorization": f"Bearer {plaintext}"}
    )
    assert resp.status_code == 422


async def test_patch_sender_id_clears_with_null(client, org_and_key) -> None:
    _, _, plaintext = org_and_key
    await client.patch(
        "/sms/sender-id", json={"custom_sender_id": "ACME"}, headers={"Authorization": f"Bearer {plaintext}"}
    )
    resp = await client.patch(
        "/sms/sender-id", json={"custom_sender_id": None}, headers={"Authorization": f"Bearer {plaintext}"}
    )
    assert resp.status_code == 200
    assert resp.json()["custom_sender_id"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd api && uv run pytest tests/test_sms_sender_id_api.py -v`
Expected: FAIL — 404 on nonexistent routes.

- [ ] **Step 3: Add the model**

```python
class SmsSenderIdentity(Base):
    """One row per org with a custom Sender ID set — absence of a row
    means the org uses the platform default ("HAIL"). Keyed by
    organization_id with no FK, matching OrgClosure's convention (hail's
    DB doesn't own the Organization table)."""

    __tablename__ = "sms_sender_identities"

    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    custom_sender_id: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=text("now()"), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(TS, server_default=text("now()"), nullable=False)
```

- [ ] **Step 4: Write the migration**

```python
# api/migrations/versions/0029_sms_sender_identities.py
"""sms_sender_identities table — one row per org with a custom Sender ID.

Revision ID: 0029
Revises: 0028
"""

from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0029"
down_revision: Union[str, None] = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sms_sender_identities",
        sa.Column("organization_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("custom_sender_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("sms_sender_identities")
```

- [ ] **Step 5: Add schemas**

```python
SENDER_ID_RE = re.compile(r"^[A-Za-z0-9]{2,11}$")


class SenderIdPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    custom_sender_id: str | None = None

    @field_validator("custom_sender_id")
    @classmethod
    def _validate_sender_id(cls, v: str | None) -> str | None:
        if v is not None and not SENDER_ID_RE.match(v):
            raise ValueError("must be 2-11 alphanumeric characters, no spaces or symbols")
        return v


class SenderIdResponse(BaseModel):
    custom_sender_id: str | None
    effective_default: str = "HAIL"
```

- [ ] **Step 6: Add the routes to `sms.py`**

```python
_PLATFORM_DEFAULT_SENDER_ID = "HAIL"


@router.get("/sender-id", response_model=SenderIdResponse)
async def get_sender_id(
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> SenderIdResponse:
    stmt = select(SmsSenderIdentity).where(SmsSenderIdentity.organization_id == principal.organization_id)
    row = (await db.execute(stmt)).scalar_one_or_none()
    return SenderIdResponse(
        custom_sender_id=row.custom_sender_id if row else None,
        effective_default=_PLATFORM_DEFAULT_SENDER_ID,
    )


@router.patch("/sender-id", response_model=SenderIdResponse)
async def patch_sender_id(
    body: SenderIdPatch,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> SenderIdResponse:
    stmt = select(SmsSenderIdentity).where(SmsSenderIdentity.organization_id == principal.organization_id)
    row = (await db.execute(stmt)).scalar_one_or_none()

    if body.custom_sender_id is None:
        if row is not None:
            await db.delete(row)
            await db.commit()
        return SenderIdResponse(custom_sender_id=None, effective_default=_PLATFORM_DEFAULT_SENDER_ID)

    if row is None:
        row = SmsSenderIdentity(
            organization_id=principal.organization_id, custom_sender_id=body.custom_sender_id
        )
        db.add(row)
    else:
        row.custom_sender_id = body.custom_sender_id
    await db.commit()
    return SenderIdResponse(
        custom_sender_id=body.custom_sender_id, effective_default=_PLATFORM_DEFAULT_SENDER_ID
    )
```

Add the necessary imports (`SmsSenderIdentity`, `SenderIdPatch`, `SenderIdResponse`) to `sms.py`'s existing import blocks.

- [ ] **Step 7: Run tests to verify they pass**

Run: `cd api && uv run pytest tests/test_sms_sender_id_api.py -v`
Expected: 5 passed

- [ ] **Step 8: Run full regression suites**

Run: `cd api && uv run pytest -v` and `cd core && uv run pytest -v`
Expected: all passed

- [ ] **Step 9: Commit**

```bash
git add core/hailhq/core/models.py core/hailhq/core/schemas.py api/hailhq/api/routes/sms.py api/migrations/versions/0029_sms_sender_identities.py api/tests/test_sms_sender_id_api.py
git commit -m "feat(api): add sms sender-id get/patch"
```

---

### Task 5: Sender ID resolution + conditional dedicated-number requirement in `POST /sms`

**Files:**

- Create: `core/hailhq/core/sender_id.py`
- Modify: `api/hailhq/api/routes/sms.py`
- Test: `core/tests/test_sender_id.py`, `api/tests/test_sms_api.py` (append)

**Interfaces:**

- Produces: `resolve_sender(to_e164: str, custom_sender_id: str | None) -> SenderResolution` (a dataclass with `kind: Literal["dedicated_number_required", "alphanumeric"]` and, for the alphanumeric case, `sender_id: str`).

- [ ] **Step 1: Write the failing test**

```python
# core/tests/test_sender_id.py
"""Tests for Sender ID corridor classification and resolution."""

from __future__ import annotations

from hailhq.core.sender_id import resolve_sender


def test_us_always_requires_dedicated_number() -> None:
    result = resolve_sender("+14155551234", custom_sender_id="ACME")
    assert result.kind == "dedicated_number_required"


def test_canada_always_requires_dedicated_number() -> None:
    # Canadian NANP number (+1 area code 416 = Toronto)
    result = resolve_sender("+14165551234", custom_sender_id="ACME")
    assert result.kind == "dedicated_number_required"


def test_germany_uses_custom_sender_id_when_set() -> None:
    result = resolve_sender("+491701234567", custom_sender_id="ACME")
    assert result.kind == "alphanumeric"
    assert result.sender_id == "ACME"


def test_germany_falls_back_to_platform_default_when_unset() -> None:
    result = resolve_sender("+491701234567", custom_sender_id=None)
    assert result.kind == "alphanumeric"
    assert result.sender_id == "HAIL"


def test_uk_uses_custom_sender_id() -> None:
    result = resolve_sender("+447911123456", custom_sender_id="ACME")
    assert result.kind == "alphanumeric"
    assert result.sender_id == "ACME"


def test_australia_ignores_custom_id_uses_platform_default() -> None:
    # Registration-required corridor: custom per-org IDs not supported yet.
    result = resolve_sender("+61412345678", custom_sender_id="ACME")
    assert result.kind == "alphanumeric"
    assert result.sender_id == "HAIL"


def test_india_excluded_requires_dedicated_number() -> None:
    result = resolve_sender("+919876543210", custom_sender_id="ACME")
    assert result.kind == "dedicated_number_required"


def test_unclassified_country_conservatively_requires_dedicated_number() -> None:
    # +33 France is not in the researched corridor list — safe fallback.
    result = resolve_sender("+33612345678", custom_sender_id="ACME")
    assert result.kind == "dedicated_number_required"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd core && uv run pytest tests/test_sender_id.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write the implementation**

```python
# core/hailhq/core/sender_id.py
"""Sender ID corridor classification and resolution for outbound SMS.

Only the corridors researched for the SMS design spec are classified —
US, Canada, UK, Germany, Australia, India. Any other destination
conservatively requires the org's dedicated number rather than guessing
at unresearched local Sender ID rules. Extend _CORRIDORS as more
countries get researched.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

__all__ = ["SenderResolution", "resolve_sender"]

PLATFORM_DEFAULT_SENDER_ID = "HAIL"

# Canadian area codes overlapping NANP's +1 — Canada shares the US country
# code, distinguished only by area code. Non-exhaustive but covers the
# major Canadian NPAs; anything +1 not in this set is treated as US (both
# outcomes are identical here — ALWAYS_NUMBER either way — so precision
# beyond "is this +1" doesn't currently change behavior).
_CANADIAN_AREA_CODES = frozenset(
    {"204", "226", "236", "249", "250", "289", "306", "343", "365", "387", "403", "416",
     "418", "431", "437", "438", "450", "506", "514", "519", "548", "579", "581", "587",
     "604", "613", "639", "647", "672", "705", "709", "778", "780", "782", "807", "819",
     "825", "867", "873", "902", "905"}
)

CorridorOutcome = Literal["always_number", "custom_ok", "platform_default_only", "excluded"]

# Keyed by E.164 country-calling-code prefix (longest match wins — checked
# in order below since +1 alone is ambiguous between US and Canada).
_PREFIX_OUTCOMES: dict[str, CorridorOutcome] = {
    "49": "custom_ok",  # Germany — no pre-registration required
    "44": "custom_ok",  # UK — no pre-registration for non-"protected" names (out of scope to detect here)
    "61": "platform_default_only",  # Australia — ACMA register requires pre-registration
    "91": "excluded",  # India — Twilio silently overwrites alphanumeric with a random short code
}


@dataclass
class SenderResolution:
    kind: Literal["dedicated_number_required", "alphanumeric"]
    sender_id: str | None = None


def _classify(to_e164: str) -> CorridorOutcome:
    digits = to_e164.lstrip("+")
    if digits.startswith("1"):
        area_code = digits[1:4]
        return "always_number"  # both US and Canada resolve the same way here
    for prefix, outcome in _PREFIX_OUTCOMES.items():
        if digits.startswith(prefix):
            return outcome
    return "always_number"  # unresearched corridor — conservative fallback


def resolve_sender(to_e164: str, custom_sender_id: str | None) -> SenderResolution:
    outcome = _classify(to_e164)

    if outcome == "always_number" or outcome == "excluded":
        return SenderResolution(kind="dedicated_number_required")

    if outcome == "platform_default_only":
        return SenderResolution(kind="alphanumeric", sender_id=PLATFORM_DEFAULT_SENDER_ID)

    # custom_ok
    return SenderResolution(
        kind="alphanumeric", sender_id=custom_sender_id or PLATFORM_DEFAULT_SENDER_ID
    )
```

Note: `_CANADIAN_AREA_CODES` is defined but unused in `_classify` above since both US and Canada currently resolve identically (`always_number`) — kept as a documented, ready-to-use lookup for when Canada's own pricing/behavior genuinely diverges from US (e.g. if a future phase needs to distinguish them for reasons beyond Sender ID). If a linter flags the unused variable, either wire it in for clarity (`if digits.startswith("1"): area_code = digits[1:4]; return "always_number"` — already effectively doing this) or remove it and note the simplification in the commit message; don't leave an actually-dead import/variable.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd core && uv run pytest tests/test_sender_id.py -v`
Expected: 8 passed

- [ ] **Step 5: Wire resolution into `POST /sms`'s from-resolution logic**

In `api/hailhq/api/routes/sms.py`'s `create_sms`, the existing logic unconditionally requires a dedicated `PhoneNumber`. Change it to consult `resolve_sender` first.

**⚠️ Stale-code heads-up (verify against the current file before writing):** the illustrative block below was written against an older inline-`select(PhoneNumber)` version of `create_sms`. On `main` today the from-resolution is ONE helper call, not inline selects:

```python
from hailhq.api.numbers import resolve_org_number  # already imported
...
    from_number = await resolve_org_number(
        db, principal.organization_id, body.from_, capability="sms"
    )
    if from_number is None:
        ... raise await cache_failure(idem, unprocessable(msg, loc=["body", "from"]))
```

So when you adapt the block below: (1) keep reusing `resolve_org_number(..., capability="sms")` for the `dedicated_number_required` branch instead of hand-rolling `select(PhoneNumber)`; (2) raise via the existing `unprocessable(...)` helper wrapped in `await cache_failure(idem, ...)` (NOT a bare `HTTPException`) so idempotency-key replay and the 422 `loc` stay correct; (3) the consent gate, `check_sms_allowed`, `require_funds`, and the idempotency replay all run BEFORE this block and must stay untouched. Treat the code below as intent, not literal copy-paste:

```python
from hailhq.core.sender_id import resolve_sender

# ... inside create_sms, replace the existing from-number resolution block with:

    sender_id_row_stmt = select(SmsSenderIdentity).where(
        SmsSenderIdentity.organization_id == principal.organization_id
    )
    sender_id_row = (await db.execute(sender_id_row_stmt)).scalar_one_or_none()
    resolution = resolve_sender(
        body.to, custom_sender_id=sender_id_row.custom_sender_id if sender_id_row else None
    )

    from_number: PhoneNumber | None = None
    alphanumeric_sender: str | None = None

    if resolution.kind == "alphanumeric":
        alphanumeric_sender = resolution.sender_id
        if body.from_ is not None:
            # An explicit --from is still honored even in alphanumeric-eligible
            # corridors, matching the existing named-number lookup behavior.
            stmt = select(PhoneNumber).where(
                PhoneNumber.organization_id == principal.organization_id,
                PhoneNumber.e164 == body.from_,
            )
            from_number = (await db.execute(stmt)).scalar_one_or_none()
            if from_number is None:
                raise HTTPException(
                    status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"phone number {body.from_} is not registered to this organization",
                )
            alphanumeric_sender = None  # explicit number overrides Sender ID
    else:
        # dedicated_number_required — the existing Phase 1 logic, unchanged.
        if body.from_ is not None:
            stmt = select(PhoneNumber).where(
                PhoneNumber.organization_id == principal.organization_id,
                PhoneNumber.e164 == body.from_,
            )
            from_number = (await db.execute(stmt)).scalar_one_or_none()
            if from_number is None:
                raise HTTPException(
                    status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"phone number {body.from_} is not registered to this organization",
                )
        else:
            stmt = (
                select(PhoneNumber)
                .where(
                    PhoneNumber.organization_id == principal.organization_id,
                    PhoneNumber.provisioning_state == "active",
                )
                .order_by(PhoneNumber.created_at.asc())
                .limit(1)
            )
            from_number = (await db.execute(stmt)).scalar_one_or_none()
            if from_number is None:
                raise HTTPException(
                    status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=(
                        "no dedicated phone number on this organization; SMS to this "
                        "destination requires a dedicated number, not the shared voice pool"
                    ),
                )
```

Then update the `Sms(...)` row construction and the `provider.send_sms(...)` call: when `alphanumeric_sender is not None`, `from_e164=alphanumeric_sender` (or a new `Sms.from_sender_id` column if the plan wants to distinguish a literal E.164 from an alphanumeric string in storage — simplest for this phase: store the alphanumeric string directly in the existing `from_e164` text column, which is untyped `Text` and accepts any string; a future phase can add a dedicated column if the distinction needs to be queryable). `from_number_id` becomes nullable in this path — check whether the `Sms` model's `from_number_id` column (currently `nullable=False` per Phase 1) needs a migration to allow `NULL` for alphanumeric-sender sends with no owning number. Write that migration (`0030_sms_from_number_id_nullable.py`, `down_revision="0029"`) as part of this task if so. (For reference, `Sms.from_number_id` is confirmed `nullable=False` on `main` — `core/hailhq/core/models.py`, the `class Sms` block — so this migration is genuinely needed for the alphanumeric-sender path.)

- [ ] **Step 6: Add regression tests**

```python
# api/tests/test_sms_api.py — append
async def test_create_sms_to_germany_uses_platform_default_without_dedicated_number(
    client, org_and_key, sms_mock
) -> None:
    """No dedicated number needed at all for a no-registration corridor."""
    _, _, plaintext = org_and_key
    resp = await client.post(
        "/sms",
        json={"to": "+491701234567", "body": "hallo", "recipient_consent": True},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["from_e164"] == "HAIL"


async def test_create_sms_to_india_still_requires_dedicated_number(client, org_and_key) -> None:
    _, _, plaintext = org_and_key
    resp = await client.post(
        "/sms",
        json={"to": "+919876543210", "body": "hi", "recipient_consent": True},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 422
    assert "dedicated" in resp.json()["detail"]
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `cd api && uv run pytest tests/test_sms_api.py -v`
Expected: all passed (Phase 1's tests + 2 new)

- [ ] **Step 8: Run full regression suites**

Run: `cd core && uv run pytest -v` and `cd api && uv run pytest -v`
Expected: all passed

- [ ] **Step 9: Commit**

```bash
git add core/hailhq/core/sender_id.py core/tests/test_sender_id.py api/hailhq/api/routes/sms.py api/tests/test_sms_api.py api/migrations/versions/
git commit -m "feat(core,api): add sender ID resolution, make dedicated-number requirement conditional"
```

---

### Task 6: CLI/SDK for numbers + sender-id

**Files:**

- Create: `cli/internal/cmd/number.go`
- Modify: `cli/internal/cmd/sms.go`
- Modify: `sdk/hail/models.py`, `sdk/hail/client.py`

Follow the exact same structure as Phase 1's Task 7 (CLI) and Task 8 (SDK): regenerate the OpenAPI spec + Go client first, verify real generated operationIds/types before writing consuming code, mirror `email_domain.go`'s multi-subcommand tree for `hail numbers` (acquire/list/get/enable-sms), add a `hail sms sender-id get/set` subcommand pair, add `Client.numbers` (`acquire`/`list`/`get`/`enable_sms`) and `Client.sms.sender_id` (`get`/`set`) to the SDK. Write tests using the same real-HTTP-server (Go) / `respx` (Python) conventions already established, not isolated mocks.

- [ ] **Step 1-N**: mirror Phase 1's Task 7/8 structure exactly (regenerate → verify real names → implement → test → self-review → do NOT commit the "Commit" step differently from every other task in this plan).

- [ ] **Final step: Commit**

```bash
git add cli/internal/cmd/number.go cli/internal/cmd/number_test.go cli/internal/cmd/sms.go cli/internal/cmd/sms_test.go cli/internal/cmd/root.go cli/internal/client/client.gen.go sdk/hail/models.py sdk/hail/client.py sdk/tests/test_sms.py sdk/tests/test_numbers.py openapi/openapi.yaml
git commit -m "feat(cli,sdk): add hail numbers command and sender-id support"
```

---

## Self-Review Notes

- **Spec coverage**: covers the "Number provisioning (generic, cross-channel) & Sender ID" section of the design spec, with one corrected assumption (capabilities aren't independently togglable — documented explicitly in Global Constraints) and one genuine extension beyond the spec's literal text (conditional dedicated-number requirement based on destination, which the spec implied via "Sender ID is outbound-only" but Phase 1 didn't implement).
- **Placeholder scan**: the Task 1 test has one deliberately-flagged verification step (the exact Messaging Service mock URL) rather than a guessed value presented as fact — this is intentional caution, not an unfinished placeholder, per the same lesson learned in Phase 1 about not trusting unverified SDK-generated names.
- **Type consistency**: `SenderResolution.kind` (`"dedicated_number_required" | "alphanumeric"`) is used consistently between `sender_id.py` and the route's branching logic.

## Remaining Phases (not this plan)

1. **Console UI** (`hail-website`) — `/console/sms`, Sender ID / Numbers / Suppression settings panels, monthly-fee billing.
2. **Docs & release** — mostly independent of this plan's specifics.
