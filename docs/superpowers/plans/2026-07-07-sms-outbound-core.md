# SMS Outbound Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a working, testable outbound SMS channel end-to-end (provider adapter, data model, compliance gate, API route, CLI, SDK, MCP tool) that an org with a dedicated phone number can use to send a text message today.

**Architecture:** Mirrors the existing `Call` channel byte-for-byte where the shapes match: a thin `SmsProvider` ABC wrapping Twilio's sync SDK in `asyncio.to_thread` (like `VoiceProvider`/`TwilioVoiceProvider`), a single `Sms` table with a plain-text `status` column (no ENUM, unlike `Call`'s `CallEndReason` — SMS has no equivalent multi-value end-reason), and a `POST/GET /sms` route that reuses the existing consent gate (`enforce_consent`), a new `check_sms_allowed` compliance-gate function (mirroring `check_call_allowed`), and the existing generic `Suppression` table (widened to accept `channel='sms'`) instead of a new SMS-specific suppression table.

**Tech Stack:** FastAPI + SQLAlchemy 2 (async) + Alembic (`hail/api`, `hail/core`), Twilio Python SDK (sync, wrapped), Go + Cobra + oapi-codegen (`hail/cli`), Python SDK hand-mirroring `core/hailhq/core/schemas.py` (`hail/sdk`), FastMCP (`hail/mcp`).

## Global Constraints

- **Provider is Twilio only.** No AWS SNS anywhere (SNS cannot receive inbound SMS — irrelevant to this phase but the ADR is locked platform-wide).
- **No pool fallback for SMS.** Two-way/US SMS requires a dedicated `PhoneNumber` row on the org (`organization_id` set, not `is_pool`). If none exists, `POST /sms` returns 422 — it must NOT silently fall back to `claim_pool_number` the way `POST /calls` does.
- **Reuse `enforce_consent()` unchanged.** `SmsCreate` carries the same `recipient_consent` / `consent_source` / `consent_obtained_at` / `message_type` fields as `CallCreate`/`EmailCreate`, via a new shared `ConsentAttestationMixin` (this phase also refactors `CallCreate`/`EmailCreate` onto the mixin — three near-identical copies crossed the "two concrete uses" bar).
- **No new suppression table.** The existing generic `Suppression` model/table is widened (`channel` CHECK constraint gains `'sms'`) rather than adding a parallel `SmsSuppression` table.
- **Text only.** `body: str`, no attachments/media fields. MMS is explicitly out of scope.
- **No Twilio Messaging Service yet.** This phase sends directly `from_=<dedicated e164>`. Messaging Service creation, Sender ID resolution, and self-serve number acquisition are a later phase (Numbers & Sender ID) — out of scope here. A dedicated number is assumed to already exist on the org (seeded the same manual way dedicated voice numbers are seeded today, per `docs/operations.md`).
- **No delivery-status webhook.** This phase records only the synchronous response from Twilio's `Messages.create` call (`status="queued"` or an immediate failure). Later `status` transitions (`delivered`/`undelivered` via Twilio's status callback) are inbound/webhook-phase work.
- **Migrations:** next two revision numbers are `0023` (new) and `0024` (new) — `0022_org_closures.py` is the current head.

---

## File Structure

```
core/hailhq/core/providers/sms/
  __init__.py       # re-exports (mirrors providers/voice/__init__.py)
  base.py           # SmsProvider ABC, ProviderSmsResult
  twilio.py         # TwilioSmsProvider

core/hailhq/core/models.py          # + Sms class; Suppression CHECK widened
core/hailhq/core/schemas.py         # + ConsentAttestationMixin, SmsCreate, SmsResponse,
                                     #   SmsListResponse, SmsStatus; CallCreate/EmailCreate
                                     #   refactored onto the mixin
core/hailhq/core/compliance_gate.py # + check_sms_allowed
core/hailhq/core/config.py          # + hail_velocity_sms_per_hour/_per_day

api/migrations/versions/0023_sms.py                       # new
api/migrations/versions/0024_suppressions_sms_channel.py  # new
api/hailhq/api/routes/sms.py                              # new
api/hailhq/api/main.py                                    # + router registration

core/tests/providers/test_twilio_sms.py   # new
api/tests/test_sms_api.py                 # new
core/tests/test_schemas.py                # + mixin/refactor regression assertions
api/tests/conftest.py                     # + sms_mock fixture, get_sms_provider override

cli/internal/cmd/sms.go             # new
sdk/hail/models.py                  # + SmsCreate, SmsResponse, SmsListResponse, SmsStatus
sdk/hail/client.py                  # + _SmsResource, Client.sms
mcp/hailhq/mcp/hail_client.py       # + send_sms/get_sms/list_sms
mcp/hailhq/mcp/tools.py             # + send_sms/get_sms/list_sms tools + registration

openapi/openapi.yaml                # regenerated (not hand-edited)
cli/internal/client/client.gen.go   # regenerated (not hand-edited)
.env.example                        # unchanged (no new env vars this phase)
```

---

### Task 1: SMS provider layer (`SmsProvider` / `TwilioSmsProvider`)

**Files:**

- Create: `core/hailhq/core/providers/sms/__init__.py`
- Create: `core/hailhq/core/providers/sms/base.py`
- Create: `core/hailhq/core/providers/sms/twilio.py`
- Test: `core/tests/providers/test_twilio_sms.py`

**Interfaces:**

- Produces: `ProviderSmsResult(provider_message_sid: str, status: str, segment_count: int, error_code: str | None)`, `SmsProvider.send_sms(self, from_e164: str, to_e164: str, body: str) -> ProviderSmsResult` (abstract), `TwilioSmsProvider(account_sid: str | None = None, auth_token: str | None = None, client: TwilioClient | None = None)`.

- [ ] **Step 1: Write the failing test**

```python
# core/tests/providers/test_twilio_sms.py
"""Unit tests for ``TwilioSmsProvider``.

Same approach as ``test_twilio_voice.py``: mock at the HTTP boundary via
``responses`` rather than monkeypatching Twilio SDK objects, so SDK drift
surfaces as a test failure the same way real usage would.
"""

from __future__ import annotations

from urllib.parse import parse_qs

import pytest
import responses

from hailhq.core.providers.sms import ProviderSmsResult, TwilioSmsProvider

ACCOUNT_SID = "ACtest1234567890abcdef1234567890ab"
AUTH_TOKEN = "test-auth-token"
API_BASE = f"https://api.twilio.com/2010-04-01/Accounts/{ACCOUNT_SID}"


@pytest.fixture()
def provider() -> TwilioSmsProvider:
    return TwilioSmsProvider(account_sid=ACCOUNT_SID, auth_token=AUTH_TOKEN)


@responses.activate
async def test_send_sms_success(provider: TwilioSmsProvider) -> None:
    responses.add(
        responses.POST,
        f"{API_BASE}/Messages.json",
        json={
            "sid": "SM1234567890abcdef1234567890abcd",
            "account_sid": ACCOUNT_SID,
            "to": "+14155551234",
            "from": "+14155559999",
            "body": "Hello from Hail",
            "status": "queued",
            "num_segments": "1",
            "error_code": None,
            "date_created": "Wed, 22 Apr 2026 12:00:00 +0000",
            "uri": f"/2010-04-01/Accounts/{ACCOUNT_SID}/Messages/SM1234567890abcdef1234567890abcd.json",
        },
        status=201,
    )

    result = await provider.send_sms(
        from_e164="+14155559999", to_e164="+14155551234", body="Hello from Hail"
    )

    assert isinstance(result, ProviderSmsResult)
    assert result.provider_message_sid == "SM1234567890abcdef1234567890abcd"
    assert result.status == "queued"
    assert result.segment_count == 1
    assert result.error_code is None

    sent_body = parse_qs(responses.calls[0].request.body)
    assert sent_body == {
        "To": ["+14155551234"],
        "From": ["+14155559999"],
        "Body": ["Hello from Hail"],
    }


@responses.activate
async def test_send_sms_multi_segment(provider: TwilioSmsProvider) -> None:
    long_body = "x" * 200  # over the 160-char single-segment threshold
    responses.add(
        responses.POST,
        f"{API_BASE}/Messages.json",
        json={
            "sid": "SM_multiseg",
            "account_sid": ACCOUNT_SID,
            "to": "+14155551234",
            "from": "+14155559999",
            "body": long_body,
            "status": "queued",
            "num_segments": "2",
            "error_code": None,
        },
        status=201,
    )

    result = await provider.send_sms(
        from_e164="+14155559999", to_e164="+14155551234", body=long_body
    )

    assert result.segment_count == 2


def test_constructor_raises_without_creds() -> None:
    with pytest.raises(ValueError, match="requires twilio_account_sid"):
        TwilioSmsProvider(account_sid="", auth_token="")


def test_constructor_falls_back_to_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    from hailhq.core.config import settings

    monkeypatch.setattr(settings, "twilio_account_sid", ACCOUNT_SID)
    monkeypatch.setattr(settings, "twilio_auth_token", AUTH_TOKEN)

    provider = TwilioSmsProvider()
    assert provider.account_sid == ACCOUNT_SID
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd core && uv run pytest tests/providers/test_twilio_sms.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'hailhq.core.providers.sms'`

- [ ] **Step 3: Write the base interface**

```python
# core/hailhq/core/providers/sms/base.py
"""Carrier-side SMS provider interface.

Unlike ``VoiceProvider`` (see ``providers/voice/base.py``), SMS has no
in-flight state to poll: a send either succeeds (accepted/queued at the
carrier) or raises immediately (transport/auth failure or a carrier-level
rejection Twilio surfaces synchronously). Later status transitions
(delivered/undelivered) arrive via a webhook, not polling — that's a
later phase's concern, not this interface's.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from pydantic import BaseModel

__all__ = ["ProviderSmsResult", "SmsProvider"]


class ProviderSmsResult(BaseModel):
    """The carrier's immediate response to a send request."""

    provider_message_sid: str
    status: str
    segment_count: int
    error_code: str | None = None


class SmsProvider(ABC):
    """Abstract carrier-side SMS provider."""

    @abstractmethod
    async def send_sms(self, from_e164: str, to_e164: str, body: str) -> ProviderSmsResult:
        """Send a single SMS. Raises on transport/auth/carrier-level failure."""
```

- [ ] **Step 4: Write the Twilio implementation**

```python
# core/hailhq/core/providers/sms/twilio.py
"""Twilio implementation of the carrier-side ``SmsProvider`` interface.

Same sync-SDK-wrapped-in-``asyncio.to_thread`` approach as
``providers/voice/twilio.py``; tests mock at the ``requests`` boundary via
``responses`` for the same reason (SDK drift shows up as a test failure).
"""

from __future__ import annotations

import asyncio

from twilio.rest import Client as TwilioClient

from hailhq.core.config import settings
from hailhq.core.providers.sms.base import ProviderSmsResult, SmsProvider


class TwilioSmsProvider(SmsProvider):
    """Carrier adapter for Twilio's Messages API."""

    def __init__(
        self,
        account_sid: str | None = None,
        auth_token: str | None = None,
        client: TwilioClient | None = None,
    ) -> None:
        self.account_sid = account_sid or settings.twilio_account_sid
        token = auth_token or settings.twilio_auth_token

        if client is None:
            if not self.account_sid or not token:
                raise ValueError(
                    "TwilioSmsProvider requires twilio_account_sid + "
                    "twilio_auth_token (set them in settings or pass them "
                    "explicitly)."
                )
            client = TwilioClient(self.account_sid, token)
        self._client = client

    async def send_sms(self, from_e164: str, to_e164: str, body: str) -> ProviderSmsResult:
        message = await asyncio.to_thread(
            self._client.messages.create,
            to=to_e164,
            from_=from_e164,
            body=body,
        )
        raw_segments = getattr(message, "num_segments", None)
        segment_count = int(raw_segments) if raw_segments is not None else 1
        return ProviderSmsResult(
            provider_message_sid=message.sid,
            status=message.status,
            segment_count=segment_count,
            error_code=getattr(message, "error_code", None),
        )
```

```python
# core/hailhq/core/providers/sms/__init__.py
from hailhq.core.providers.sms.base import ProviderSmsResult, SmsProvider
from hailhq.core.providers.sms.twilio import TwilioSmsProvider

__all__ = ["ProviderSmsResult", "SmsProvider", "TwilioSmsProvider"]
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd core && uv run pytest tests/providers/test_twilio_sms.py -v`
Expected: 4 passed

- [ ] **Step 6: Commit**

```bash
git add core/hailhq/core/providers/sms/ core/tests/providers/test_twilio_sms.py
git commit -m "feat(core): add SmsProvider/TwilioSmsProvider carrier adapter"
```

---

### Task 2: `Sms` model + migration

**Files:**

- Modify: `core/hailhq/core/models.py` (insert after the `CallEvent` class, before `class Email`)
- Create: `api/migrations/versions/0023_sms.py`
- Test: `core/tests/test_models.py` (append; create the file if it doesn't already exist — check first with `ls core/tests/test_models.py`)

**Interfaces:**

- Consumes: nothing new (uses existing `Base`, `PhoneNumber`).
- Produces: `Sms` SQLAlchemy model with columns `id, organization_id, from_number_id, from_e164, to_e164, direction, status, body, provider, provider_message_sid, segment_count, error_code, requested_at, sent_at, metadata_, created_at`.

- [ ] **Step 1: Write the failing test**

```python
# core/tests/test_models.py — append (create file if absent, with this import block at top)
from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from hailhq.core.models import PhoneNumber, Sms


async def _make_phone_number(session, organization_id: uuid.UUID) -> PhoneNumber:
    pn = PhoneNumber(
        organization_id=organization_id,
        e164="+14155551234",
        country_code="US",
        number_type="local",
        provider_resource_id="PN_test",
        provisioning_state="active",
    )
    session.add(pn)
    await session.flush()
    return pn


async def test_sms_insert_defaults(async_session) -> None:
    org_id = uuid.uuid4()
    pn = await _make_phone_number(async_session, org_id)

    sms = Sms(
        organization_id=org_id,
        from_number_id=pn.id,
        from_e164=pn.e164,
        to_e164="+14155559999",
        body="hi",
    )
    async_session.add(sms)
    await async_session.commit()
    await async_session.refresh(sms)

    assert sms.direction == "outbound"
    assert sms.status == "queued"
    assert sms.segment_count == 1
    assert sms.provider == "twilio"
    assert sms.metadata_ == {}


async def test_sms_status_check_constraint(async_session) -> None:
    org_id = uuid.uuid4()
    pn = await _make_phone_number(async_session, org_id)

    sms = Sms(
        organization_id=org_id,
        from_number_id=pn.id,
        from_e164=pn.e164,
        to_e164="+14155559999",
        body="hi",
        status="not_a_real_status",
    )
    async_session.add(sms)
    with pytest.raises(IntegrityError):
        await async_session.commit()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd core && uv run pytest tests/test_models.py -v -k sms`
Expected: FAIL with `ImportError: cannot import name 'Sms' from 'hailhq.core.models'`

- [ ] **Step 3: Add the `Sms` model**

Insert into `core/hailhq/core/models.py` immediately after the `CallEvent` class (before `class Email(Base):`):

```python
class Sms(Base):
    __tablename__ = "sms"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    from_number_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("phone_numbers.id"), nullable=False
    )
    from_e164: Mapped[str] = mapped_column(Text, nullable=False)
    to_e164: Mapped[str] = mapped_column(Text, nullable=False)
    direction: Mapped[str] = mapped_column(
        Text, server_default="outbound", nullable=False
    )
    status: Mapped[str] = mapped_column(Text, server_default="queued", nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(Text, server_default="twilio", nullable=False)
    provider_message_sid: Mapped[str | None] = mapped_column(
        Text, unique=True, nullable=True
    )
    segment_count: Mapped[int] = mapped_column(Integer, server_default="1", nullable=False)
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_at: Mapped[datetime] = mapped_column(
        TS, server_default=text("now()"), nullable=False
    )
    sent_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    metadata_: Mapped[dict] = mapped_column(
        "metadata", JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        TS, server_default=text("now()"), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "direction IN ('outbound','inbound')",
            name="sms_direction_check",
        ),
        CheckConstraint(
            "status IN ('queued','sent','delivered','failed','undelivered','received')",
            name="sms_status_check",
        ),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd core && uv run pytest tests/test_models.py -v -k sms`
Expected: 2 passed

- [ ] **Step 5: Write the Alembic migration**

```python
# api/migrations/versions/0023_sms.py
"""sms table — outbound (and later inbound) text messages.

One row per message, mirroring ``calls``' shape (single plain-text
``status`` column, not an ENUM — SMS has no multi-valued end-reason
analog). ``provider_message_sid`` is unique but nullable until the
provider call returns.

Revision ID: 0023
Revises: 0022
"""

from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0023"
down_revision: Union[str, None] = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sms",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("organization_id", UUID(as_uuid=True), nullable=False),
        sa.Column(
            "from_number_id",
            UUID(as_uuid=True),
            sa.ForeignKey("phone_numbers.id"),
            nullable=False,
        ),
        sa.Column("from_e164", sa.Text(), nullable=False),
        sa.Column("to_e164", sa.Text(), nullable=False),
        sa.Column("direction", sa.Text(), server_default="outbound", nullable=False),
        sa.Column("status", sa.Text(), server_default="queued", nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), server_default="twilio", nullable=False),
        sa.Column("provider_message_sid", sa.Text(), nullable=True, unique=True),
        sa.Column("segment_count", sa.Integer(), server_default="1", nullable=False),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "metadata", JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "direction IN ('outbound','inbound')",
            name="sms_direction_check",
        ),
        sa.CheckConstraint(
            "status IN ('queued','sent','delivered','failed','undelivered','received')",
            name="sms_status_check",
        ),
    )
    op.create_index("sms_organization_id_idx", "sms", ["organization_id"])


def downgrade() -> None:
    op.drop_index("sms_organization_id_idx", table_name="sms")
    op.drop_table("sms")
```

- [ ] **Step 6: Apply the migration against the local dev database**

Run: `cd api && uv run alembic upgrade head`
Expected: `Running upgrade 0022 -> 0023, sms table`, no errors

- [ ] **Step 7: Commit**

```bash
git add core/hailhq/core/models.py core/tests/test_models.py api/migrations/versions/0023_sms.py
git commit -m "feat(core): add Sms model and migration"
```

---

### Task 3: Widen `Suppression.channel` + `check_sms_allowed` compliance gate

**Files:**

- Modify: `core/hailhq/core/models.py` (`Suppression.__table_args__`)
- Modify: `core/hailhq/core/compliance_gate.py` (+ `check_sms_allowed`, `__all__`)
- Modify: `core/hailhq/core/config.py` (+ velocity settings)
- Create: `api/migrations/versions/0024_suppressions_sms_channel.py`
- Test: `core/tests/test_compliance_gate.py` (append)

**Interfaces:**

- Consumes: `Suppression`, `_suppression_hit`, `_check_velocity`, `_parse_blocked_prefixes`, `GateResult` — all already defined in `compliance_gate.py` (Task confirms they're reused unchanged).
- Produces: `check_sms_allowed(db: AsyncSession, organization_id: UUID, to_e164: str) -> GateResult`.

- [ ] **Step 1: Write the failing test**

```python
# core/tests/test_compliance_gate.py — append
async def test_check_sms_allowed_blocks_suppressed_recipient(async_session) -> None:
    import uuid

    from hailhq.core.compliance_gate import add_suppression, check_sms_allowed

    org_id = uuid.uuid4()
    await add_suppression(
        async_session,
        organization_id=org_id,
        recipient="+14155551234",
        channel="sms",
        reason="user opted out",
        source="manual",
    )
    await async_session.commit()

    result = await check_sms_allowed(async_session, org_id, "+14155551234")

    assert result.allowed is False
    assert "suppression" in result.reason.lower()


async def test_check_sms_allowed_permits_clean_recipient(async_session) -> None:
    import uuid

    from hailhq.core.compliance_gate import check_sms_allowed

    result = await check_sms_allowed(async_session, uuid.uuid4(), "+14155559999")

    assert result.allowed is True
    assert result.checks["suppression_hit"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd core && uv run pytest tests/test_compliance_gate.py -v -k sms`
Expected: FAIL with `ImportError: cannot import name 'check_sms_allowed'`

- [ ] **Step 3: Widen the `Suppression` CHECK constraint in the model**

In `core/hailhq/core/models.py`, change `Suppression.__table_args__`:

```python
    __table_args__ = (
        CheckConstraint(
            "channel IN ('voice','email','sms','all')",
            name="suppressions_channel_check",
        ),
        Index("suppressions_recipient_channel_idx", "recipient", "channel"),
    )
```

- [ ] **Step 4: Write the migration**

```python
# api/migrations/versions/0024_suppressions_sms_channel.py
"""Widen suppressions.channel CHECK to include 'sms'.

Revision ID: 0024
Revises: 0023
"""

from __future__ import annotations

from typing import Union

from alembic import op

revision: str = "0024"
down_revision: Union[str, None] = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("suppressions_channel_check", "suppressions", type_="check")
    op.create_check_constraint(
        "suppressions_channel_check",
        "suppressions",
        "channel IN ('voice','email','sms','all')",
    )


def downgrade() -> None:
    op.drop_constraint("suppressions_channel_check", "suppressions", type_="check")
    op.create_check_constraint(
        "suppressions_channel_check",
        "suppressions",
        "channel IN ('voice','email','all')",
    )
```

Run: `cd api && uv run alembic upgrade head`
Expected: `Running upgrade 0023 -> 0024, ...`, no errors

- [ ] **Step 5: Add velocity settings**

In `core/hailhq/core/config.py`, immediately after the existing `hail_velocity_email_per_hour`/`_per_day` lines:

```python
    hail_velocity_sms_per_hour: int = 100
    hail_velocity_sms_per_day: int = 1000
```

- [ ] **Step 6: Add `check_sms_allowed`**

In `core/hailhq/core/compliance_gate.py`, add `"check_sms_allowed"` to the `__all__` list, and append this function after `check_call_allowed` (before `check_email_allowed`):

```python
async def check_sms_allowed(
    db: AsyncSession, organization_id: UUID, to_e164: str
) -> GateResult:
    """Pre-send checks for an outbound SMS: suppression, premium-rate prefix
    block, then velocity cap. Mirrors ``check_call_allowed``'s single-E.164
    shape (not ``check_email_allowed``'s list shape) — Twilio's Messages API
    is single-recipient per call.
    """
    checks: dict[str, Any] = {}

    hit = await _suppression_hit(db, organization_id, [to_e164], "sms")
    checks["suppression_checked"] = True
    checks["suppression_hit"] = hit is not None
    if hit is not None:
        return GateResult(
            allowed=False,
            reason=f"recipient is on the suppression list ({hit.reason})",
            checks=checks,
        )

    blocked_prefixes = _parse_blocked_prefixes()
    for prefix in blocked_prefixes:
        if to_e164.startswith(prefix):
            checks["premium_rate_blocked"] = True
            return GateResult(
                allowed=False,
                reason=f"destination prefix {prefix!r} is blocked (premium-rate/high-risk)",
                checks=checks,
            )
    checks["premium_rate_blocked"] = False

    velocity_checks, reason = await _check_velocity(
        db,
        organization_id,
        "sms",
        per_hour=settings.hail_velocity_sms_per_hour,
        per_day=settings.hail_velocity_sms_per_day,
        unit="texts",
    )
    checks["velocity"] = velocity_checks
    if reason is not None:
        return GateResult(allowed=False, reason=reason, checks=checks)

    return GateResult(allowed=True, checks=checks)
```

Update the module docstring's opening line from "Exactly two call sites" to "Three call sites" (`calls.py`, `emails.py`, `sms.py`) — the abstraction is being extended, not introduced, so this doesn't reopen the "no abstraction without two concrete uses" question.

- [ ] **Step 7: Run test to verify it passes**

Run: `cd core && uv run pytest tests/test_compliance_gate.py -v -k sms`
Expected: 2 passed

- [ ] **Step 8: Run the full compliance-gate and model suites to check for regressions**

Run: `cd core && uv run pytest tests/test_compliance_gate.py tests/test_models.py -v`
Expected: all passed (existing voice/email suppression tests unaffected by the widened CHECK)

- [ ] **Step 9: Commit**

```bash
git add core/hailhq/core/models.py core/hailhq/core/compliance_gate.py core/hailhq/core/config.py core/tests/test_compliance_gate.py api/migrations/versions/0024_suppressions_sms_channel.py
git commit -m "feat(core): widen suppression channel check, add check_sms_allowed"
```

---

### Task 4: Schemas — `ConsentAttestationMixin`, `SmsCreate`, `SmsResponse`, `SmsListResponse`

**Files:**

- Modify: `core/hailhq/core/schemas.py`
- Test: `core/tests/test_schemas.py` (append)

**Interfaces:**

- Produces: `ConsentAttestationMixin` (pydantic `BaseModel` with the 4 consent fields), `SmsCreate(ConsentAttestationMixin)` with `to`, `from_`, `body`, `metadata`, `SmsStatus` (`Literal`), `SmsResponse`, `SmsListResponse`.
- Consumes: `E164` regex, already defined at the top of `schemas.py`.

- [ ] **Step 1: Write the failing test**

```python
# core/tests/test_schemas.py — append
def test_sms_create_requires_consent() -> None:
    import pytest
    from pydantic import ValidationError

    from hailhq.core.schemas import SmsCreate

    with pytest.raises(ValidationError):
        SmsCreate(to="+14155551234", body="hi")  # missing recipient_consent


def test_sms_create_validates_e164() -> None:
    import pytest
    from pydantic import ValidationError

    from hailhq.core.schemas import SmsCreate

    with pytest.raises(ValidationError):
        SmsCreate(to="not-a-number", body="hi", recipient_consent=True)


def test_sms_create_happy_path() -> None:
    from hailhq.core.schemas import SmsCreate

    sms = SmsCreate(to="+14155551234", body="hi", recipient_consent=True)
    assert sms.to == "+14155551234"
    assert sms.message_type == "informational"


def test_call_create_still_requires_consent_after_mixin_refactor() -> None:
    """Regression: CallCreate moving onto ConsentAttestationMixin must not
    change its externally-visible required-field behavior."""
    import pytest
    from pydantic import ValidationError

    from hailhq.core.schemas import CallCreate

    with pytest.raises(ValidationError):
        CallCreate(to="+14155551234", system_prompt="hi")  # missing recipient_consent


def test_email_create_still_requires_consent_after_mixin_refactor() -> None:
    import pytest
    from pydantic import ValidationError

    from hailhq.core.schemas import EmailCreate

    with pytest.raises(ValidationError):
        EmailCreate(
            to=["a@example.com"], subject="hi", body_text="hi"
        )  # missing recipient_consent
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd core && uv run pytest tests/test_schemas.py -v -k sms`
Expected: FAIL with `ImportError: cannot import name 'SmsCreate'`

- [ ] **Step 3: Extract `ConsentAttestationMixin` and refactor `CallCreate`/`EmailCreate`**

In `core/hailhq/core/schemas.py`, add this class immediately before `class CallCreate(BaseModel):`:

```python
class ConsentAttestationMixin(BaseModel):
    """Shared consent-attestation fields for every outbound-send schema
    (Call, Email, Sms). Extracted here once a third channel needed the
    identical block — the repo's "no abstraction without two concrete
    uses" tenet: two existing copies (Call, Email) plus this one crossed
    that bar.
    """

    recipient_consent: bool = Field(
        description=(
            "Attestation that you have obtained the lawful consent required "
            "to contact this recipient. Hail does not verify consent itself "
            "— you are responsible for a lawful basis under TCPA/ePrivacy/"
            "PECR/CAN-SPAM/GDPR as applicable. Rejected (422) if not true."
        )
    )
    consent_source: str | None = Field(
        default=None,
        description=(
            "Where/how consent was obtained (e.g. 'signup form', "
            "'prior customer relationship'). Required (non-empty) when "
            "message_type is 'marketing'."
        ),
    )
    consent_obtained_at: datetime | None = Field(
        default=None, description="When consent was obtained, if known."
    )
    message_type: Literal["marketing", "informational"] = Field(
        default="informational",
        description=(
            "'marketing' additionally requires a non-empty consent_source. "
            "Use 'informational' for transactional/service communications."
        ),
    )
```

Then change `class CallCreate(BaseModel):` to `class CallCreate(ConsentAttestationMixin):` and delete the now-duplicated `# --- Consent attestation ---` block (the 4 fields: `recipient_consent`, `consent_source`, `consent_obtained_at`, `message_type`) from inside `CallCreate` — everything else in `CallCreate` (`model_config`, `to`, `from_`, `system_prompt`, `llm`, `first_message`, `voice_config`, `conversation_id`, `metadata`, the two validators) stays exactly as-is.

Do the identical thing to `EmailCreate`: change `class EmailCreate(BaseModel):` to `class EmailCreate(ConsentAttestationMixin):` and delete its duplicated consent block, keeping everything else unchanged.

- [ ] **Step 4: Add the SMS schemas**

Immediately after `class CallListResponse(BaseModel):` (before the `EventResponse` class), add:

```python
class SmsCreate(ConsentAttestationMixin):
    model_config = ConfigDict(extra="forbid")

    to: str
    from_: str | None = Field(default=None, alias="from")
    body: str = Field(min_length=1, max_length=1600)
    metadata: dict = Field(default_factory=dict)

    @field_validator("to", "from_")
    @classmethod
    def _validate_e164(cls, v: str | None) -> str | None:
        if v is not None and not E164.match(v):
            raise ValueError("must be E.164 (e.g. +14155551234)")
        return v


SmsStatus = Literal["queued", "sent", "delivered", "failed", "undelivered", "received"]


class SmsResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    organization_id: UUID
    from_e164: str
    to_e164: str
    direction: Literal["outbound", "inbound"]
    status: SmsStatus
    body: str
    provider_message_sid: str | None
    segment_count: int
    error_code: str | None
    requested_at: datetime
    sent_at: datetime | None


class SmsListResponse(BaseModel):
    items: list[SmsResponse]
    next_cursor: str | None = None
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd core && uv run pytest tests/test_schemas.py -v -k "sms or consent_after_mixin"`
Expected: 5 passed

- [ ] **Step 6: Run the full schemas test suite to catch mixin-refactor regressions**

Run: `cd core && uv run pytest tests/test_schemas.py -v`
Expected: all passed — this specifically confirms `CallCreate`/`EmailCreate`'s field ordering/validation/serialization behavior is unchanged after moving onto the mixin

- [ ] **Step 7: Commit**

```bash
git add core/hailhq/core/schemas.py core/tests/test_schemas.py
git commit -m "refactor(core): extract ConsentAttestationMixin, add Sms schemas"
```

---

### Task 5: `POST/GET /sms` API route

**Files:**

- Create: `api/hailhq/api/routes/sms.py`
- Modify: `api/hailhq/api/main.py` (import + `include_router`)
- Modify: `api/tests/conftest.py` (+ `sms_mock` fixture, `get_sms_provider` override in `client`)
- Test: `api/tests/test_sms_api.py`

**Interfaces:**

- Consumes: `SmsProvider`, `TwilioSmsProvider` (Task 1); `Sms` model (Task 2); `check_sms_allowed` (Task 3); `SmsCreate`/`SmsResponse`/`SmsListResponse`/`SmsStatus` (Task 4); `enforce_consent`, `isoformat_or_none` (`api/hailhq/api/consent.py`, unchanged); `has_funds` (`core/hailhq/core/billing.py`, unchanged); `write_usage_event` (`api/hailhq/api/usage.py`, unchanged); `write_audit_log` (`api/hailhq/api/audit.py`, unchanged); `fetch_cursor_page` (`api/hailhq/api/pagination.py`, unchanged); `IdempotencyContext`/`idempotency_dep` (`api/hailhq/api/idempotency.py`, unchanged); `Principal`/`get_current_principal` (`api/hailhq/api/deps.py`, unchanged).
- Produces: `router` (FastAPI `APIRouter`), `get_sms_provider()` (overridable dependency, mirrors `get_livekit`).

- [ ] **Step 1: Write the failing test**

```python
# api/tests/test_sms_api.py
"""Tests for POST/GET /sms."""

from __future__ import annotations

import uuid

import pytest

from api.tests.conftest import insert_org_and_key


async def _seed_dedicated_number(async_session, organization_id) -> None:
    from hailhq.core.models import PhoneNumber

    pn = PhoneNumber(
        organization_id=organization_id,
        e164="+14155559999",
        country_code="US",
        number_type="local",
        provider_resource_id="PN_test",
        provisioning_state="active",
    )
    async_session.add(pn)
    await async_session.commit()


async def test_create_sms_requires_auth(client) -> None:
    resp = await client.post(
        "/sms",
        json={"to": "+14155551234", "body": "hi", "recipient_consent": True},
    )
    assert resp.status_code == 401


async def test_create_sms_requires_consent(client, async_session, org_and_key) -> None:
    org_id, _, plaintext = org_and_key
    await _seed_dedicated_number(async_session, org_id)

    resp = await client.post(
        "/sms",
        json={"to": "+14155551234", "body": "hi", "recipient_consent": False},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 422


async def test_create_sms_without_dedicated_number_422s(client, org_and_key) -> None:
    org_id, _, plaintext = org_and_key

    resp = await client.post(
        "/sms",
        json={"to": "+14155551234", "body": "hi", "recipient_consent": True},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 422
    assert "dedicated" in resp.json()["detail"]


async def test_create_sms_happy_path(client, async_session, org_and_key, sms_mock) -> None:
    org_id, _, plaintext = org_and_key
    await _seed_dedicated_number(async_session, org_id)

    resp = await client.post(
        "/sms",
        json={"to": "+14155551234", "body": "hello", "recipient_consent": True},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "sent"
    assert body["from_e164"] == "+14155559999"
    assert body["to_e164"] == "+14155551234"
    assert body["segment_count"] == 1
    sms_mock.send_sms.assert_awaited_once()


async def test_create_sms_blocked_by_suppression(
    client, async_session, org_and_key
) -> None:
    from hailhq.core.compliance_gate import add_suppression

    org_id, _, plaintext = org_and_key
    await _seed_dedicated_number(async_session, org_id)
    await add_suppression(
        async_session,
        organization_id=org_id,
        recipient="+14155551234",
        channel="sms",
        reason="opted out",
        source="manual",
    )
    await async_session.commit()

    resp = await client.post(
        "/sms",
        json={"to": "+14155551234", "body": "hi", "recipient_consent": True},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 403


async def test_get_sms_not_found(client, org_and_key) -> None:
    _, _, plaintext = org_and_key
    resp = await client.get(
        f"/sms/{uuid.uuid4()}", headers={"Authorization": f"Bearer {plaintext}"}
    )
    assert resp.status_code == 404


async def test_list_sms_empty(client, org_and_key) -> None:
    _, _, plaintext = org_and_key
    resp = await client.get("/sms", headers={"Authorization": f"Bearer {plaintext}"})
    assert resp.status_code == 200
    assert resp.json() == {"items": [], "next_cursor": None}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd api && uv run pytest tests/test_sms_api.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'hailhq.api.routes.sms'` (and `fixture 'sms_mock' not found`)

- [ ] **Step 3: Add the `sms_mock` fixture and `get_sms_provider` override**

In `api/tests/conftest.py`, add near the existing `email_mock` fixture:

```python
from hailhq.core.providers.sms import ProviderSmsResult, SmsProvider


@pytest.fixture()
def sms_mock() -> AsyncMock:
    """Default mock SMS provider — happy-path send for every call."""
    mock = AsyncMock(spec=SmsProvider)

    counter = {"n": 0}

    async def _send(**kwargs):
        counter["n"] += 1
        return ProviderSmsResult(
            provider_message_sid=f"SM_test_{counter['n']}",
            status="queued",
            segment_count=1,
            error_code=None,
        )

    mock.send_sms.side_effect = _send
    return mock
```

Then update the `client` fixture to depend on and override the new provider — add `sms_mock: AsyncMock` to its parameter list and, inside the function body, add the import and two more override lines:

```python
from hailhq.api.routes.sms import get_sms_provider
```

```python
@pytest.fixture()
async def client(
    async_session: AsyncSession,  # noqa: F811
    livekit_mock: AsyncMock,
    email_mock: AsyncMock,
    sms_mock: AsyncMock,
) -> AsyncIterator[httpx.AsyncClient]:
    async def override_get_session() -> AsyncIterator[AsyncSession]:
        yield async_session

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_livekit] = lambda: livekit_mock
    app.dependency_overrides[get_email_provider] = lambda: email_mock
    app.dependency_overrides[get_sms_provider] = lambda: sms_mock

    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(get_livekit, None)
        app.dependency_overrides.pop(get_email_provider, None)
        app.dependency_overrides.pop(get_sms_provider, None)
```

- [ ] **Step 4: Write the route**

```python
# api/hailhq/api/routes/sms.py
"""Routes for the outbound SMS channel.

POST /sms - send an outbound SMS from the org's dedicated number.
GET /sms/{id} - read a single message (org-scoped).
GET /sms - cursor-paginated list (org-scoped, optional status / to filters).

No pool fallback: SMS requires a dedicated PhoneNumber on the org (see
Decision 6 of the SMS design spec) — inbound replies need unambiguous
number-to-org routing, which a shared pool number can't provide.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi import status as http_status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.api.audit import write_audit_log
from hailhq.api.consent import enforce_consent, isoformat_or_none
from hailhq.api.deps import Principal, get_current_principal
from hailhq.api.idempotency import IdempotencyContext, idempotency_dep
from hailhq.api.pagination import fetch_cursor_page
from hailhq.api.usage import write_usage_event
from hailhq.core.billing import has_funds
from hailhq.core.compliance_gate import check_sms_allowed
from hailhq.core.db import get_session
from hailhq.core.models import PhoneNumber, Sms
from hailhq.core.providers.sms import SmsProvider, TwilioSmsProvider
from hailhq.core.schemas import SmsCreate, SmsListResponse, SmsResponse, SmsStatus

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sms", tags=["sms"])

_DEFAULT_LIST_LIMIT = 50
_MAX_LIST_LIMIT = 200
_SMS_SEND_FAILED_DETAIL = "sms send failed"


_sms_provider_singleton: SmsProvider | None = None


def get_sms_provider() -> SmsProvider:
    """Return a process-wide ``SmsProvider``. Tests override via
    ``app.dependency_overrides``."""
    global _sms_provider_singleton
    if _sms_provider_singleton is None:
        _sms_provider_singleton = TwilioSmsProvider()
    return _sms_provider_singleton


@router.post("", response_model=SmsResponse, status_code=http_status.HTTP_201_CREATED)
async def create_sms(
    body: SmsCreate,
    response: Response,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    provider: Annotated[SmsProvider, Depends(get_sms_provider)],
    idem: Annotated[IdempotencyContext | None, Depends(idempotency_dep)] = None,
) -> SmsResponse:
    if idem is not None and idem.is_replay:
        cached = idem.cached_response or {}
        if idem.cached_status and idem.cached_status >= 400:
            raise HTTPException(
                status_code=idem.cached_status,
                detail=cached.get("detail", "cached failure"),
                headers={"Idempotency-Replay": "true"},
            )
        cached_id = UUID(cached["id"])
        await write_audit_log(
            organization_id=principal.organization_id,
            api_key_id=principal.api_key_id,
            action="sms.create.replayed",
            resource_type="sms",
            resource_id=cached_id,
            payload={"to": cached.get("to_e164"), "from": cached.get("from_e164")},
        )
        response.headers["Idempotency-Replay"] = "true"
        response.headers["Location"] = f"/sms/{cached_id}"
        return SmsResponse.model_validate(cached)

    enforce_consent(
        recipient_consent=body.recipient_consent,
        consent_source=body.consent_source,
        message_type=body.message_type,
    )

    gate = await check_sms_allowed(db, principal.organization_id, body.to)
    if not gate.allowed:
        await write_audit_log(
            organization_id=principal.organization_id,
            api_key_id=principal.api_key_id,
            action="sms.blocked",
            resource_type="sms",
            resource_id=None,
            payload={"to": body.to, "reason": gate.reason, "checks": gate.checks},
        )
        raise HTTPException(status_code=http_status.HTTP_403_FORBIDDEN, detail=gate.reason)

    if principal.api_key_id is not None:
        if not await has_funds(db, principal.organization_id):
            raise HTTPException(
                status_code=http_status.HTTP_402_PAYMENT_REQUIRED,
                detail="insufficient credits; top up at https://hail.so/console/billing",
            )

    # No pool fallback for SMS — a dedicated number is required (Decision 6).
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
                    "no dedicated phone number on this organization; SMS "
                    "requires a dedicated number, not the shared voice pool"
                ),
            )

    sms = Sms(
        organization_id=principal.organization_id,
        from_number_id=from_number.id,
        from_e164=from_number.e164,
        to_e164=body.to,
        direction="outbound",
        status="queued",
        body=body.body,
        metadata_=dict(body.metadata),
    )
    db.add(sms)
    await db.commit()
    await db.refresh(sms)

    await write_audit_log(
        organization_id=principal.organization_id,
        api_key_id=principal.api_key_id,
        action="sms.create",
        resource_type="sms",
        resource_id=sms.id,
        payload={
            "to": sms.to_e164,
            "from": sms.from_e164,
            "recipient_consent": body.recipient_consent,
            "consent_source": body.consent_source,
            "consent_obtained_at": isoformat_or_none(body.consent_obtained_at),
            "message_type": body.message_type,
            "compliance": gate.checks,
        },
    )

    try:
        result = await provider.send_sms(
            from_e164=sms.from_e164, to_e164=sms.to_e164, body=sms.body
        )
    except Exception as exc:
        logger.warning("sms send failed for sms_id=%s", sms.id, exc_info=True)
        sms.status = "failed"
        await db.commit()
        if idem is not None:
            await idem.store(
                status_code=http_status.HTTP_502_BAD_GATEWAY,
                body={"detail": _SMS_SEND_FAILED_DETAIL},
            )
        raise HTTPException(
            status_code=http_status.HTTP_502_BAD_GATEWAY,
            detail=_SMS_SEND_FAILED_DETAIL,
        ) from exc

    sms.status = "sent"
    sms.provider_message_sid = result.provider_message_sid
    sms.segment_count = result.segment_count
    sms.error_code = result.error_code
    sms.sent_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(sms)

    await write_usage_event(
        organization_id=principal.organization_id,
        channel="sms",
        units=sms.segment_count,
        ref=f"sms:{sms.id}",
    )

    response.headers["Location"] = f"/sms/{sms.id}"
    sms_response = SmsResponse.model_validate(sms)

    if idem is not None:
        await idem.store(
            status_code=http_status.HTTP_201_CREATED,
            body=sms_response.model_dump(mode="json"),
        )

    return sms_response


@router.get("/{sms_id}", response_model=SmsResponse)
async def get_sms(
    sms_id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> SmsResponse:
    stmt = select(Sms).where(
        Sms.id == sms_id,
        Sms.organization_id == principal.organization_id,
    )
    sms = (await db.execute(stmt)).scalar_one_or_none()
    if sms is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="sms not found")
    return SmsResponse.model_validate(sms)


@router.get("", response_model=SmsListResponse)
async def list_sms(
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    cursor: str | None = Query(default=None),
    limit: int = Query(default=_DEFAULT_LIST_LIMIT, ge=1, le=_MAX_LIST_LIMIT),
    status: SmsStatus | None = Query(default=None),
    to: str | None = Query(default=None),
) -> SmsListResponse:
    stmt = select(Sms).where(Sms.organization_id == principal.organization_id)
    if status is not None:
        stmt = stmt.where(Sms.status == status)
    if to is not None:
        stmt = stmt.where(Sms.to_e164 == to)
    rows, next_cursor = await fetch_cursor_page(
        db, stmt, Sms.created_at, Sms.id, cursor=cursor, limit=limit, newest_first=True
    )
    return SmsListResponse(
        items=[SmsResponse.model_validate(s) for s in rows],
        next_cursor=next_cursor,
    )


__all__ = ["router", "get_sms_provider"]
```

- [ ] **Step 5: Register the router**

In `api/hailhq/api/main.py`, add the import alongside the existing route imports:

```python
from hailhq.api.routes import sms as sms_routes
```

and register it alongside the existing routers:

```python
app.include_router(sms_routes.router)
```

- [ ] **Step 6: Run test to verify it passes**

Run: `cd api && uv run pytest tests/test_sms_api.py -v`
Expected: 8 passed

- [ ] **Step 7: Run the full API test suite to catch regressions**

Run: `cd api && uv run pytest -v`
Expected: all passed (in particular `test_calls_api.py` and `test_emails_api.py`, since `conftest.py`'s `client` fixture signature changed)

- [ ] **Step 8: Commit**

```bash
git add api/hailhq/api/routes/sms.py api/hailhq/api/main.py api/tests/conftest.py api/tests/test_sms_api.py
git commit -m "feat(api): add POST/GET /sms route"
```

---

### Task 6: OpenAPI regeneration

**Files:**

- Modify: `openapi/openapi.yaml` (regenerated, not hand-edited)

**Interfaces:**

- Consumes: the FastAPI app object (`hailhq.api.main.app`), now carrying the `sms` router from Task 5.
- Produces: `SmsCreate`, `SmsResponse`, `SmsListResponse` schemas and `/sms`, `/sms/{sms_id}` paths in the committed spec.

- [ ] **Step 1: Find and run the exact regeneration command**

Check how the spec is generated today — search for the generation script:

Run: `grep -rn "openapi.yaml\|openapi.json\|get_openapi" api/hailhq/api/*.py api/pyproject.toml api/Makefile 2>/dev/null`

This surfaces the exact invocation (likely a small script that imports `app.openapi()` and dumps it as YAML, or a `fastapi` CLI command). Run whatever that command is, redirecting output to `openapi/openapi.yaml`.

- [ ] **Step 2: Diff the regenerated spec**

Run: `git diff openapi/openapi.yaml`
Expected: new `SmsCreate`/`SmsResponse`/`SmsListResponse` schema blocks and new `/sms`, `/sms/{sms_id}` path blocks, shaped like the existing `CallCreate`/`CallResponse`/`CallListResponse` blocks and `/calls`/`/calls/{call_id}` paths. No unrelated diffs — if other paths/schemas changed unexpectedly, investigate before committing (likely means the generation command or FastAPI/pydantic version drifted).

- [ ] **Step 3: Run the openapi-check CI gate locally if possible**

Run: `cat .github/workflows/openapi-check.yml` to see the exact check CI runs, and replicate it locally to confirm this change passes before pushing.

- [ ] **Step 4: Commit**

```bash
git add openapi/openapi.yaml
git commit -m "chore(openapi): regenerate spec for POST/GET /sms"
```

---

### Task 7: CLI — `hail sms send|status|list`

**Files:**

- Create: `cli/internal/cmd/sms.go`
- Modify: `cli/internal/client/client.gen.go` (regenerated, not hand-edited)
- Test: `cli/internal/cmd/sms_test.go`

**Interfaces:**

- Consumes: `client.SmsCreate`, `client.SmsResponse`, `client.CreateSmsSmsPostWithResponse`, `client.GetSmsSmsSmsIdGetWithResponse`, `client.ListSmsSmsGetWithResponse` (regenerated in Step 1 below — names predicted from the established `Create<Op><Path>`/`Get<Op><Path>`/`List<Op><Path>` pattern already used by `calls`/`emails`; **verify the actual generated names with the grep in Step 2 and adjust if they differ** — the shape of the surrounding Go code is identical either way).
- Produces: `newSmsCmd(opts *Options) *cobra.Command`, wired into the root command alongside `newCallCmd`.

- [ ] **Step 1: Regenerate the Go client from the updated OpenAPI spec**

Run: `cd cli && make generate` (per `cli/Makefile`'s documented target — read the Makefile first if the exact target name differs)

- [ ] **Step 2: Confirm the generated function/type names**

Run: `grep -n "Sms" cli/internal/client/client.gen.go | grep -i "^func\|type.*Sms"`
Expected output includes something matching `CreateSmsSmsPostWithResponse`, `GetSmsSmsSmsIdGetWithResponse`, `ListSmsSmsGetWithResponse`, `SmsCreate`, `SmsResponse` struct types (mirroring `CallCreate`/`CallResponse`/`CreateCallCallsPostWithResponse` exactly). If the actual names differ, use the real names in every step below instead.

- [ ] **Step 2: Write the failing test**

```go
// cli/internal/cmd/sms_test.go
package cmd

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/hail-hq/hail/cli/internal/client"
)

func TestRunSmsHappyPath(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/sms" || r.Method != http.MethodPost {
			t.Fatalf("unexpected request: %s %s", r.Method, r.URL.Path)
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		_ = json.NewEncoder(w).Encode(client.SmsResponse{
			Id:       mustParseUUID(t, "00000000-0000-0000-0000-000000000001"),
			FromE164: "+14155559999",
			ToE164:   "+14155551234",
			Status:   "sent",
		})
	}))
	defer server.Close()

	var stdout bytes.Buffer
	opts := &Options{
		Stdout:   &stdout,
		BaseURL:  server.URL,
		newClient: func(editors ...client.RequestEditorFn) (*client.ClientWithResponses, error) {
			return client.NewClientWithResponses(server.URL, client.WithRequestEditorFn(editors[0]))
		},
	}

	f := &smsFlags{body: "hello", recipientConsent: true}
	err := runSms(nil, opts, f, "+14155551234")
	if err != nil {
		t.Fatalf("runSms returned error: %v", err)
	}
	if !bytes.Contains(stdout.Bytes(), []byte("SMS sent")) {
		t.Fatalf("expected success output, got: %s", stdout.String())
	}
}
```

> Note: `mustParseUUID` and the exact `Options`/`newClient` field shapes should match whatever test helpers `cli/internal/cmd/call_test.go` already defines — read that file first and reuse its helpers rather than redefining them.

- [ ] **Step 3: Run test to verify it fails**

Run: `cd cli && go test ./internal/cmd/... -run TestRunSmsHappyPath -v`
Expected: FAIL — `undefined: smsFlags`, `undefined: runSms`

- [ ] **Step 4: Write `sms.go`**

```go
// cli/internal/cmd/sms.go
package cmd

import (
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/spf13/cobra"

	"github.com/hail-hq/hail/cli/internal/client"
)

// smsFlags are the values bound by `hail sms`.
type smsFlags struct {
	body              string
	from              string
	idempotencyKey    string
	recipientConsent  bool
	consentSource     string
	consentObtainedAt string
	messageType       string
}

func newSmsCmd(opts *Options) *cobra.Command {
	f := &smsFlags{}

	cmd := &cobra.Command{
		Use:   "sms <to-number>",
		Short: "Send an outbound SMS (or use a subcommand)",
		Long: `hail sms — send an outbound text message

Requires a dedicated phone number on your organization — SMS does not
use the shared voice pool.`,
		Args: argsOrHelp(1, "<to-number>"),
		RunE: func(cmd *cobra.Command, args []string) error {
			return runSms(cmd, opts, f, args[0])
		},
	}

	cmd.Flags().StringVar(&f.body, "body", "", "Message text (required)")
	cmd.Flags().StringVar(&f.from, "from", "", "Override the from-number (default: the org's dedicated number)")
	cmd.Flags().StringVar(&f.idempotencyKey, "idempotency-key", "", "Defaults to a fresh UUID")
	cmd.Flags().BoolVar(&f.recipientConsent, "recipient-consent", false, "Confirm the recipient has consented to receive this text (required by the API)")
	cmd.Flags().StringVar(&f.consentSource, "consent-source", "", "Where/how consent was obtained (required if --message-type=marketing)")
	cmd.Flags().StringVar(&f.consentObtainedAt, "consent-obtained-at", "", "RFC 3339 timestamp consent was obtained at (optional)")
	cmd.Flags().StringVar(&f.messageType, "message-type", "", "\"marketing\" or \"informational\" (default: informational)")

	cmd.AddCommand(newSmsStatusCmd(opts))
	cmd.AddCommand(newSmsListCmd(opts))

	return cmd
}

func runSms(cmd *cobra.Command, opts *Options, f *smsFlags, toNumber string) error {
	if f.body == "" {
		return helpAndFail(cmd, "--body is required")
	}
	ctx := cmd.Context()

	body := client.SmsCreate{
		To:   toNumber,
		Body: f.body,
		From: strPtr(f.from),
	}
	body.RecipientConsent = f.recipientConsent
	if f.consentSource != "" {
		body.ConsentSource = strPtr(f.consentSource)
	}
	if f.consentObtainedAt != "" {
		t, err := time.Parse(time.RFC3339, f.consentObtainedAt)
		if err != nil {
			return fmt.Errorf("--consent-obtained-at: invalid RFC 3339 timestamp: %w", err)
		}
		body.ConsentObtainedAt = &t
	}
	if f.messageType != "" {
		mt := client.SmsCreateMessageType(f.messageType)
		body.MessageType = &mt
	}

	idem := f.idempotencyKey
	if idem == "" {
		idem = uuid.NewString()
	}

	apiClient, err := opts.newClient(idempotencyEditor(idem))
	if err != nil {
		return err
	}

	resp, err := apiClient.CreateSmsSmsPostWithResponse(ctx, body)
	if err != nil {
		return fmt.Errorf("sms API: %w", err)
	}
	if resp.HTTPResponse.StatusCode != http.StatusCreated || resp.JSON201 == nil {
		return apiError(resp.HTTPResponse.StatusCode, resp.Body)
	}

	return printSms(opts, resp.JSON201)
}

// printSms renders the success response in either JSON or human form.
func printSms(opts *Options, sms *client.SmsResponse) error {
	if opts.JSON {
		return printJSON(opts.Stdout, sms)
	}

	fmt.Fprintf(opts.Stdout, "SMS sent: %s\n", sms.Id.String())
	fmt.Fprintf(opts.Stdout, "  From:    %s\n", sms.FromE164)
	fmt.Fprintf(opts.Stdout, "  To:      %s\n", sms.ToE164)
	fmt.Fprintf(opts.Stdout, "  Status:  %s\n", string(sms.Status))
	fmt.Fprintf(opts.Stdout, "  Track:   hail sms status %s\n", sms.Id.String())
	return nil
}

func newSmsStatusCmd(opts *Options) *cobra.Command {
	return &cobra.Command{
		Use:   "status <id>",
		Short: "Fetch the current state of one SMS",
		Args:  argsOrHelp(1, "<id>"),
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			apiClient, err := opts.newClient()
			if err != nil {
				return err
			}
			resp, err := apiClient.GetSmsSmsSmsIdGetWithResponse(ctx, args[0])
			if err != nil {
				return fmt.Errorf("sms API: %w", err)
			}
			if resp.HTTPResponse.StatusCode != http.StatusOK || resp.JSON200 == nil {
				return apiError(resp.HTTPResponse.StatusCode, resp.Body)
			}
			return printSms(opts, resp.JSON200)
		},
	}
}

func newSmsListCmd(opts *Options) *cobra.Command {
	return &cobra.Command{
		Use:   "list",
		Short: "List recent SMS messages",
		Args:  argsOrHelp(0, ""),
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			apiClient, err := opts.newClient()
			if err != nil {
				return err
			}
			resp, err := apiClient.ListSmsSmsGetWithResponse(ctx, &client.ListSmsSmsGetParams{})
			if err != nil {
				return fmt.Errorf("sms API: %w", err)
			}
			if resp.HTTPResponse.StatusCode != http.StatusOK || resp.JSON200 == nil {
				return apiError(resp.HTTPResponse.StatusCode, resp.Body)
			}
			if opts.JSON {
				return printJSON(opts.Stdout, resp.JSON200)
			}
			for _, s := range resp.JSON200.Items {
				fmt.Fprintf(opts.Stdout, "%s  %-12s  %s -> %s\n", s.Id.String(), string(s.Status), s.FromE164, s.ToE164)
			}
			return nil
		},
	}
}
```

Then register `newSmsCmd` in the root command (find where `newCallCmd(opts)` is added — likely `cli/internal/cmd/root.go` — and add `rootCmd.AddCommand(newSmsCmd(opts))` alongside it).

- [ ] **Step 5: Run test to verify it passes**

Run: `cd cli && go test ./internal/cmd/... -run TestRunSmsHappyPath -v`
Expected: PASS

- [ ] **Step 6: Run the full CLI test suite**

Run: `cd cli && go test ./... -v`
Expected: all passed

- [ ] **Step 7: Commit**

```bash
git add cli/internal/cmd/sms.go cli/internal/cmd/sms_test.go cli/internal/cmd/root.go cli/internal/client/client.gen.go
git commit -m "feat(cli): add hail sms send/status/list"
```

---

### Task 8: SDK — `Client.sms`

**Files:**

- Modify: `sdk/hail/models.py` (+ `SmsCreate`, `SmsResponse`, `SmsListResponse`, `SmsStatus`)
- Modify: `sdk/hail/client.py` (+ `_SmsResource`, `Client.sms`)
- Test: `sdk/tests/test_client_sms.py` (check for the existing calls-resource test file's exact name/pattern first, e.g. `sdk/tests/test_client.py` or `test_calls_resource.py`, and mirror its fixture/mocking approach)

**Interfaces:**

- Consumes: nothing new — this is a hand-maintained mirror of `core/hailhq/core/schemas.py`'s `SmsCreate`/`SmsResponse`/`SmsListResponse` (the SDK ships standalone, no import from `hailhq.*`).
- Produces: `Client.sms.create(...)`, `.get(...)`, `.list(...)`.

- [ ] **Step 1: Find the existing calls-resource SDK test file and its pattern**

Run: `ls sdk/tests/ | grep -i call`
Read whichever file that finds in full before writing the SMS test, to match its exact HTTP-mocking approach (likely `httpx.MockTransport` or `respx`).

- [ ] **Step 2: Write the failing test**

```python
# sdk/tests/test_client_sms.py
"""Tests for Client.sms.*, mirroring the calls-resource test file's
mocking pattern (see that file for the exact transport-mocking helper)."""

from __future__ import annotations

import httpx
import pytest

from hail import Client


@pytest.fixture()
def transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sms" and request.method == "POST":
            return httpx.Response(
                201,
                json={
                    "id": "00000000-0000-0000-0000-000000000001",
                    "organization_id": "00000000-0000-0000-0000-000000000002",
                    "from_e164": "+14155559999",
                    "to_e164": "+14155551234",
                    "direction": "outbound",
                    "status": "sent",
                    "body": "hi",
                    "provider_message_sid": "SM_test",
                    "segment_count": 1,
                    "error_code": None,
                    "requested_at": "2026-07-07T00:00:00Z",
                    "sent_at": "2026-07-07T00:00:01Z",
                },
            )
        if request.url.path.startswith("/sms/") and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "id": "00000000-0000-0000-0000-000000000001",
                    "organization_id": "00000000-0000-0000-0000-000000000002",
                    "from_e164": "+14155559999",
                    "to_e164": "+14155551234",
                    "direction": "outbound",
                    "status": "sent",
                    "body": "hi",
                    "provider_message_sid": "SM_test",
                    "segment_count": 1,
                    "error_code": None,
                    "requested_at": "2026-07-07T00:00:00Z",
                    "sent_at": "2026-07-07T00:00:01Z",
                },
            )
        return httpx.Response(404)

    return httpx.MockTransport(handler)


async def test_sms_create(transport: httpx.MockTransport) -> None:
    async with Client(api_key="test", transport=transport) as client:
        sms = await client.sms.create(to="+14155551234", body="hi", recipient_consent=True)
    assert sms.status == "sent"
    assert sms.from_e164 == "+14155559999"


async def test_sms_get(transport: httpx.MockTransport) -> None:
    async with Client(api_key="test", transport=transport) as client:
        sms = await client.sms.get("00000000-0000-0000-0000-000000000001")
    assert sms.to_e164 == "+14155551234"
```

> Note: if `Client.__init__` doesn't currently accept a `transport` kwarg for test injection, check how the existing calls-resource test file injects a mock transport (likely via `_HailHTTP` construction) and match that instead.

- [ ] **Step 3: Run test to verify it fails**

Run: `cd sdk && uv run pytest tests/test_client_sms.py -v`
Expected: FAIL with `AttributeError: 'Client' object has no attribute 'sms'`

- [ ] **Step 4: Add the SMS models**

In `sdk/hail/models.py`, immediately after `class CallListResponse(BaseModel):`, add:

```python
class SmsCreate(BaseModel):
    """Body shape for ``POST /sms``."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    to: str
    from_: str | None = Field(default=None, alias="from")
    body: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    recipient_consent: bool
    consent_source: str | None = None
    consent_obtained_at: datetime | None = None
    message_type: Literal["marketing", "informational"] = "informational"

    @field_validator("to", "from_")
    @classmethod
    def _validate_e164(cls, v: str | None) -> str | None:
        if v is not None and not E164.match(v):
            raise ValueError("must be E.164 (e.g. +14155551234)")
        return v


SmsStatus = Literal["queued", "sent", "delivered", "failed", "undelivered", "received"]


class SmsResponse(BaseModel):
    """Shape returned by ``POST /sms`` and ``GET /sms/{id}``."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    organization_id: UUID
    from_e164: str
    to_e164: str
    direction: Literal["outbound", "inbound"]
    status: SmsStatus
    body: str
    provider_message_sid: str | None = None
    segment_count: int
    error_code: str | None = None
    requested_at: datetime
    sent_at: datetime | None = None


class SmsListResponse(BaseModel):
    items: list[SmsResponse]
    next_cursor: str | None = None
```

- [ ] **Step 5: Add `_SmsResource` and wire it into `Client`**

In `sdk/hail/client.py`, add the import (extend the existing `from hail.models import (...)` block) with `SmsCreate, SmsListResponse, SmsResponse, SmsStatus`, then add this class immediately after `_CallsResource`:

```python
class _SmsResource:
    """``client.sms.*`` — POST/GET/LIST against ``/sms``."""

    def __init__(self, http: _HailHTTP) -> None:
        self._http = http

    async def create(
        self,
        *,
        to: str,
        body: str,
        recipient_consent: bool,
        from_: str | None = None,
        metadata: dict[str, Any] | None = None,
        consent_source: str | None = None,
        consent_obtained_at: datetime | None = None,
        message_type: Literal["marketing", "informational"] | None = None,
        idempotency_key: str | None = None,
    ) -> SmsResponse:
        """Send an outbound SMS from the org's dedicated number.

        ``recipient_consent`` is required — the server 422s without it.
        ``idempotency_key`` defaults to a fresh UUIDv4.
        """
        fields: dict[str, Any] = {
            "to": to,
            "body": body,
            "recipient_consent": recipient_consent,
        }
        if from_ is not None:
            fields["from"] = from_
        if metadata is not None:
            fields["metadata"] = metadata
        if consent_source is not None:
            fields["consent_source"] = consent_source
        if consent_obtained_at is not None:
            fields["consent_obtained_at"] = consent_obtained_at.isoformat()
        if message_type is not None:
            fields["message_type"] = message_type

        key = idempotency_key or generate_idempotency_key()
        data = await self._http.request(
            "POST", "/sms", json=fields, headers={"Idempotency-Key": key}
        )
        return SmsResponse.model_validate(data)

    async def get(self, sms_id: str | UUID) -> SmsResponse:
        """Fetch a single SMS by id."""
        sid = str(sms_id)
        data = await self._http.request("GET", f"/sms/{sid}")
        return SmsResponse.model_validate(data)

    async def list(
        self,
        *,
        cursor: str | None = None,
        limit: int = 50,
        status: SmsStatus | None = None,
        to: str | None = None,
    ) -> SmsListResponse:
        """Cursor-paginated list, scoped to the caller's organization."""
        params = {"limit": limit, "cursor": cursor, "status": status, "to": to}
        data = await self._http.request("GET", "/sms", params=params)
        return SmsListResponse.model_validate(data)
```

Then, in `Client.__init__`, immediately after `self.calls = _CallsResource(self._http)`, add:

```python
        self.sms = _SmsResource(self._http)
```

- [ ] **Step 6: Run test to verify it passes**

Run: `cd sdk && uv run pytest tests/test_client_sms.py -v`
Expected: 2 passed

- [ ] **Step 7: Run the full SDK test suite**

Run: `cd sdk && uv run pytest -v`
Expected: all passed

- [ ] **Step 8: Commit**

```bash
git add sdk/hail/models.py sdk/hail/client.py sdk/tests/test_client_sms.py
git commit -m "feat(sdk): add Client.sms resource"
```

---

### Task 9: MCP — `send_sms`, `get_sms`, `list_sms` tools

**Files:**

- Modify: `mcp/hailhq/mcp/hail_client.py` (+ `send_sms`, `get_sms`, `list_sms` on `HailClient`)
- Modify: `mcp/hailhq/mcp/tools.py` (+ tool functions, + registration in `register_tools`)
- Test: `mcp/tests/test_tools.py` (append — check the file's existing `place_call`/`send_email` test pattern first and mirror it)

**Interfaces:**

- Consumes: `SmsCreate`, `SmsListResponse`, `SmsResponse` (`hailhq.core.schemas`, Task 4); `HailAPIError` (`mcp/hailhq/mcp/hail_client.py`, unchanged); `_client_for`, `_format_api_error`, `_validation_error_message` (`mcp/hailhq/mcp/tools.py`, unchanged).
- Produces: `HailClient.send_sms/get_sms/list_sms`; module-level `send_sms`/`get_sms`/`list_sms` tool functions; `send_sms`/`get_sms`/`list_sms` entries registered in `register_tools`.

- [ ] **Step 1: Write the failing test**

```python
# mcp/tests/test_tools.py — append (mirror whatever mocking pattern the
# existing test_place_call/test_send_email functions in this file use for
# HailClient — likely an AsyncMock(spec=HailClient))
from unittest.mock import AsyncMock

import pytest

from hailhq.mcp.hail_client import HailAPIError, HailClient
from hailhq.mcp.tools import get_sms, list_sms, send_sms


async def test_send_sms_success() -> None:
    client = AsyncMock(spec=HailClient)
    client.send_sms.return_value = {
        "id": "sms-1",
        "status": "sent",
        "to_e164": "+14155551234",
    }

    result = await send_sms(
        client=client,
        to="+14155551234",
        body="hi",
        recipient_consent=True,
    )

    assert result["status"] == "sent"
    assert "idempotency_key" in result
    client.send_sms.assert_awaited_once()


async def test_send_sms_api_error() -> None:
    client = AsyncMock(spec=HailClient)
    client.send_sms.side_effect = HailAPIError(status=403, detail="blocked")

    result = await send_sms(
        client=client, to="+14155551234", body="hi", recipient_consent=True
    )

    assert result == {"error": "blocked"}


async def test_get_sms() -> None:
    client = AsyncMock(spec=HailClient)
    client.get_sms.return_value = {"id": "sms-1", "status": "sent"}

    result = await get_sms(client=client, sms_id="sms-1")

    assert result["status"] == "sent"


async def test_list_sms() -> None:
    client = AsyncMock(spec=HailClient)
    client.list_sms.return_value = {"items": [], "next_cursor": None}

    result = await list_sms(client=client)

    assert result["items"] == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd mcp && uv run pytest tests/test_tools.py -v -k sms`
Expected: FAIL with `ImportError: cannot import name 'send_sms' from 'hailhq.mcp.tools'`

- [ ] **Step 3: Add `HailClient.send_sms/get_sms/list_sms`**

In `mcp/hailhq/mcp/hail_client.py`, add the import `SmsCreate, SmsListResponse, SmsResponse` to the existing `from hailhq.core.schemas import (...)` block, then add this section after the `GET /calls` methods (or anywhere alongside the other resource methods — matches the file's existing "one comment-delimited section per resource" layout):

```python
    # ------------------------------------------------------------------ #
    # POST /sms
    # ------------------------------------------------------------------ #

    async def send_sms(
        self,
        *,
        to: str,
        body: str,
        recipient_consent: bool,
        from_: str | None = None,
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        consent_source: str | None = None,
        consent_obtained_at: str | None = None,
        message_type: str = "informational",
    ) -> dict[str, Any]:
        """POST /sms — send an outbound SMS.

        Builds the body from :class:`SmsCreate` (E.164 + consent
        attestation). Construction raises ``pydantic.ValidationError``
        before any HTTP on bad input.
        """
        fields: dict[str, Any] = {
            "to": to,
            "body": body,
            "recipient_consent": recipient_consent,
            "message_type": message_type,
        }
        if from_ is not None:
            fields["from"] = from_
        if metadata is not None:
            fields["metadata"] = metadata
        if consent_source is not None:
            fields["consent_source"] = consent_source
        if consent_obtained_at is not None:
            fields["consent_obtained_at"] = consent_obtained_at

        body_dict = SmsCreate.model_validate(fields).model_dump(
            mode="json", by_alias=True, exclude_unset=True
        )
        headers = {"Idempotency-Key": idempotency_key or str(uuid.uuid4())}
        resp = await self._client.post("/sms", json=body_dict, headers=headers)
        return SmsResponse.model_validate(_decode(resp)).model_dump(mode="json")

    # ------------------------------------------------------------------ #
    # GET /sms/{id}
    # ------------------------------------------------------------------ #

    async def get_sms(self, sms_id: str) -> dict[str, Any]:
        resp = await self._client.get(f"/sms/{sms_id}")
        return SmsResponse.model_validate(_decode(resp)).model_dump(mode="json")

    # ------------------------------------------------------------------ #
    # GET /sms
    # ------------------------------------------------------------------ #

    async def list_sms(
        self,
        *,
        cursor: str | None = None,
        limit: int | None = None,
        status: str | None = None,
        to: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if cursor is not None:
            params["cursor"] = cursor
        if limit is not None:
            params["limit"] = limit
        if status is not None:
            params["status"] = status
        if to is not None:
            params["to"] = to
        resp = await self._client.get("/sms", params=params)
        return SmsListResponse.model_validate(_decode(resp)).model_dump(mode="json")
```

- [ ] **Step 4: Add the module-level tool functions**

In `mcp/hailhq/mcp/tools.py`, add after the `list_calls` function:

```python
async def send_sms(
    *,
    client: HailClient,
    to: str,
    body: str,
    recipient_consent: bool,
    from_: str | None = None,
    metadata: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
    consent_source: str | None = None,
    consent_obtained_at: str | None = None,
    message_type: str = "informational",
) -> dict[str, Any]:
    if idempotency_key is None:
        idempotency_key = str(uuid.uuid4())
    try:
        result = await client.send_sms(
            to=to,
            body=body,
            recipient_consent=recipient_consent,
            from_=from_,
            metadata=metadata,
            idempotency_key=idempotency_key,
            consent_source=consent_source,
            consent_obtained_at=consent_obtained_at,
            message_type=message_type,
        )
    except ValidationError as exc:
        return {"error": _validation_error_message(exc)}
    except HailAPIError as exc:
        return _format_api_error(exc)
    if isinstance(result, dict):
        result.setdefault("idempotency_key", idempotency_key)
    return result


async def get_sms(*, client: HailClient, sms_id: str) -> dict[str, Any]:
    try:
        return await client.get_sms(sms_id)
    except ValidationError as exc:
        return {"error": _validation_error_message(exc)}
    except HailAPIError as exc:
        return _format_api_error(exc)


async def list_sms(
    *,
    client: HailClient,
    cursor: str | None = None,
    limit: int = 50,
    status: str | None = None,
    to: str | None = None,
) -> dict[str, Any]:
    try:
        return await client.list_sms(cursor=cursor, limit=limit, status=status, to=to)
    except ValidationError as exc:
        return {"error": _validation_error_message(exc)}
    except HailAPIError as exc:
        return _format_api_error(exc)
```

- [ ] **Step 5: Register the tools**

In `mcp/hailhq/mcp/tools.py`'s `register_tools`, add after the `list_calls` tool registration (`list_calls_tool`):

```python
    @mcp_app.tool(name="send_sms")
    async def send_sms_tool(
        ctx: Context,
        to: str,
        body: str,
        recipient_consent: bool,
        from_: str | None = None,
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        consent_source: str | None = None,
        consent_obtained_at: str | None = None,
        message_type: str = "informational",
    ) -> dict[str, Any]:
        """Send an outbound SMS from your organization's dedicated number.

        ``to`` must be E.164 (e.g. ``+14155551234``). ``body`` is the
        message text. SMS requires a dedicated phone number on your
        organization — it does not use the shared voice-call pool.

        ``recipient_consent`` is required: attest that you (the caller
        triggering this request) have obtained the lawful consent needed
        to text this recipient. The API rejects the request (422) if
        this is not ``true`` — Hail does not verify consent for you. Set
        ``message_type="marketing"`` for promotional texts (this
        additionally requires a non-empty ``consent_source``) — leave as
        the default ``"informational"`` for transactional/service texts.

        ``idempotency_key`` defaults to a fresh UUID and is returned in
        the response under ``idempotency_key`` — pass the same value on
        a retry to replay rather than re-send.

        Example:
            send_sms(to="+14155551234", body="Your order shipped!",
                     recipient_consent=True)

        Returns the ``SmsResponse`` dict (id, status, from_e164,
        to_e164, segment_count, ...). On failure returns
        ``{"error": "<message>"}`` instead.
        """
        try:
            async with _client_for(ctx, mode=mode, singleton=singleton) as client:
                return await send_sms(
                    client=client,
                    to=to,
                    body=body,
                    recipient_consent=recipient_consent,
                    from_=from_,
                    metadata=metadata,
                    idempotency_key=idempotency_key,
                    consent_source=consent_source,
                    consent_obtained_at=consent_obtained_at,
                    message_type=message_type,
                )
        except RuntimeError as exc:
            return {"error": str(exc)}

    @mcp_app.tool(name="get_sms")
    async def get_sms_tool(ctx: Context, sms_id: str) -> dict[str, Any]:
        """Fetch the current state of one SMS by id.

        Use this after ``send_sms`` to check delivery status.

        Example:
            get_sms(sms_id="...")
        """
        try:
            async with _client_for(ctx, mode=mode, singleton=singleton) as client:
                return await get_sms(client=client, sms_id=sms_id)
        except RuntimeError as exc:
            return {"error": str(exc)}

    @mcp_app.tool(name="list_sms")
    async def list_sms_tool(
        ctx: Context,
        cursor: str | None = None,
        limit: int = 50,
        status: str | None = None,
        to: str | None = None,
    ) -> dict[str, Any]:
        """Page through recent SMS messages for your organization.

        ``status`` filters to one of: queued, sent, delivered, failed,
        undelivered, received. ``to`` filters to messages sent to a
        specific E.164 number. Paginate with the returned
        ``next_cursor``.
        """
        try:
            async with _client_for(ctx, mode=mode, singleton=singleton) as client:
                return await list_sms(
                    client=client, cursor=cursor, limit=limit, status=status, to=to
                )
        except RuntimeError as exc:
            return {"error": str(exc)}
```

Also update the module docstring's tool count/list at the top of the file (currently "eleven tools" listing `place_call` through `get_events`) to add the three new entries and bump the count to fourteen.

- [ ] **Step 6: Run test to verify it passes**

Run: `cd mcp && uv run pytest tests/test_tools.py -v -k sms`
Expected: 4 passed

- [ ] **Step 7: Run the full MCP test suite**

Run: `cd mcp && uv run pytest -v`
Expected: all passed

- [ ] **Step 8: Commit**

```bash
git add mcp/hailhq/mcp/hail_client.py mcp/hailhq/mcp/tools.py mcp/tests/test_tools.py
git commit -m "feat(mcp): add send_sms/get_sms/list_sms tools"
```

---

## Self-Review Notes (completed during writing, not a separate pass)

- **Spec coverage**: this phase covers Decisions 1 (provider), 6 (dedicated number, no pool fallback), 8 partial (consent reuse — full Suppression/compliance-gate reuse), 11 (MMS deferred by omission), and the "Architecture & data model", part of "Numbers & Sender ID" (dedicated-number check only, not the self-serve acquisition flow), and "API/CLI/SDK/MCP/OpenAPI surface" sections of the spec. NOT covered here (explicitly deferred to later phases per Global Constraints): inbound webhook, opt-out/STOP handling, abuse monitoring, Sender ID resolution, Twilio Messaging Service creation, self-serve number acquisition, billing-rate changes in `hail-website`, console UI, docs/changelog.
- **Placeholder scan**: no TBD/TODO in any step; the one place genuine uncertainty exists (exact oapi-codegen-generated Go symbol names in Task 7) is handled with an explicit verification step (grep) rather than a placeholder, with the predicted names following the codebase's own established, mechanical naming convention.
- **Type consistency**: `SmsResponse`/`SmsCreate`/`SmsStatus` field names and types are identical across `core/hailhq/core/schemas.py` (Task 4), the API route (Task 5), the SDK (Task 8), and the MCP layer (Task 9) — all trace back to the same field list (`id, organization_id, from_e164, to_e164, direction, status, body, provider_message_sid, segment_count, error_code, requested_at, sent_at`).

## Remaining Phases (not this plan — write separately when this one lands)

1. **Inbound & compliance** — Twilio inbound webhook, signature verification, opt-out (STOP/HELP) via the generic `Suppression` table, `ChannelSuspension`/abuse monitoring.
2. **Numbers & Sender ID** — generic cross-channel `/numbers` API, Twilio Messaging Service creation, Sender ID resolution and per-org customization.
3. **Console UI** (`hail-website`) — `/console/sms` activity log, Sender ID / Numbers / Suppression settings panels, pricing page update, the new recurring-fee billing mechanism.
4. **Docs & release** — `docs/setup/sms.md`, `CHANGELOG.md`, `README.md` milestones, legal-doc language flips.
