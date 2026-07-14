# SMS Inbound & Compliance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Receive inbound SMS via a Twilio webhook, route it to the right org, fan it out to the org's own webhook subscribers, honor STOP/HELP/START opt-out signals against the shared `Suppression` table, expose a suppression-list API, and add a minimal abuse-monitoring guardrail against the shared-campaign risk the design spec accepts.

**Architecture:** Mirrors the existing inbound-email pipeline (`core/hailhq/core/email_ingest.py` → `webhook_fanout.py` → `webhook_worker.py`) structurally, but with Twilio's own signature scheme (not the repo's shared HMAC helper) verifying the inbound webhook, and a new `sms_ingest.py` module doing org resolution + idempotent insert + opt-out detection + fan-out in one place, called from a thin route.

**Tech Stack:** FastAPI + SQLAlchemy 2 (async) + Alembic (`hail/api`, `hail/core`), `twilio.request_validator.RequestValidator` for inbound signature verification, the existing `WebhookSubscription`/`WebhookDelivery`/`webhook_worker.py` delivery infra (unchanged).

## Global Constraints

- **This phase assumes SMS Outbound Core (the prior plan) is already merged** — `Sms` model, `check_sms_allowed`, `POST/GET /sms` route, `ConsentAttestationMixin` all exist.
- **Twilio's signature scheme is NOT the repo's `hmac_signing.py` HMAC-SHA256 scheme.** Twilio signs with HMAC-SHA1 over the full request URL + sorted form-param concatenation, base64-encoded, in an `X-Twilio-Signature` header, verified via `twilio.request_validator.RequestValidator(auth_token).validate(url, params, signature)`. Write a dedicated verification helper — do not try to reuse `hailhq.core.hmac_signing`.
- **No new suppression table.** Opt-out (STOP/HELP/START) writes/removes rows in the existing generic `Suppression` table with `channel='sms'`, reusing `add_suppression()` from Phase 1's `compliance_gate.py`. This phase adds the missing `remove_suppression()` (confirmed not to exist anywhere in the codebase).
- **Org resolution for inbound is by `To` number only** (no `organization_id` filter, since org isn't known yet): `select(PhoneNumber).where(PhoneNumber.e164 == to_e164, PhoneNumber.is_pool.is_(False))`. A pool number or unknown number gets a 200-but-ignored response (Twilio expects 200 regardless, to avoid retries; log and drop).
- **Fan-out reuses `webhook_fanout.py`'s existing delivery infra** (`WebhookSubscription`, `WebhookDelivery`, `webhook_worker.py`) — these are already event-type-agnostic. This phase adds a thin `fanout_sms_event()` wrapper (parallel to `fanout_email_event()`) since the existing one is named/typed for email (`email_domain_id` param, email-shaped `build_event_data`).
- **`ChannelSuspension` is a new table**, distinct from `OrgClosure` (whole-account closure — confirmed unrelated) and distinct from `Suppression` (per-recipient, not per-org-per-channel). Shape: `organization_id, channel, reason, suspended_at`, unique per `(organization_id, channel)` while active.
- **Migrations continue the chain**: this phase's single migration is `0028_channel_suspensions` (`revision="0028"`, `down_revision="0027"`). The current head on `main` is `0027` — `ls api/migrations/versions/ | sort | tail -3` shows `0025_sms.py`, `0026_suppressions_sms_channel.py`, `0027_sms_events.py`, all landed by the SMS-outbound + unified-events work. Re-confirm the head at implementation time before writing the file.
- **No MCP tool for suppressions** — per the design spec, suppression/number management is account-config, not a conversational-agent action. Only API/CLI/SDK get this surface.

---

## File Structure

```
core/hailhq/core/twilio_signature.py          # new — Twilio inbound signature verification
core/hailhq/core/sms_ingest.py                # new — org resolution, idempotent insert, opt-out, fan-out
core/hailhq/core/webhook_fanout.py            # + fanout_sms_event
core/hailhq/core/compliance_gate.py           # + remove_suppression, check_channel_suspended
core/hailhq/core/config.py                    # + hail_sms_opt_out_rate_window_hours, _min_sends, _max_rate
core/hailhq/core/models.py                    # + ChannelSuspension

api/migrations/versions/0028_channel_suspensions.py   # new table
api/hailhq/api/routes/sms.py                          # + inbound webhook route, suppressions GET/DELETE
api/hailhq/api/main.py                                # (no change — sms router already registered)

core/tests/test_twilio_signature.py           # new
core/tests/test_sms_ingest.py                 # new
core/tests/test_compliance_gate.py            # + remove_suppression, check_channel_suspended tests
api/tests/test_sms_inbound_api.py             # new
api/tests/test_sms_suppressions_api.py        # new

cli/internal/cmd/sms.go                       # + suppressions list/delete subcommands
sdk/hail/client.py                             # + _SmsResource.suppressions
sdk/hail/models.py                             # + SuppressionResponse/ListResponse
```

---

### Task 1: Twilio inbound signature verification

**Files:**

- Create: `core/hailhq/core/twilio_signature.py`
- Test: `core/tests/test_twilio_signature.py`

**Interfaces:**

- Produces: `verify_twilio_signature(url: str, params: dict[str, str], signature: str | None, auth_token: str) -> bool`.

- [ ] **Step 1: Write the failing test**

```python
# core/tests/test_twilio_signature.py
"""Tests for Twilio inbound webhook signature verification.

Twilio signs with HMAC-SHA1 over the full URL + sorted form-param
concatenation, base64-encoded — a different scheme from the repo's own
hailhq.core.hmac_signing (HMAC-SHA256 over a raw body), so this has its
own module rather than extending that one.
"""

from __future__ import annotations

from twilio.request_validator import RequestValidator

from hailhq.core.twilio_signature import verify_twilio_signature

AUTH_TOKEN = "test-auth-token"
URL = "https://api.hail.so/sms/inbound"


def _real_signature(params: dict[str, str]) -> str:
    return RequestValidator(AUTH_TOKEN).compute_signature(URL, params)


def test_verify_accepts_genuine_signature() -> None:
    params = {"From": "+14155551234", "To": "+14155559999", "Body": "hi"}
    sig = _real_signature(params)
    assert verify_twilio_signature(URL, params, sig, AUTH_TOKEN) is True


def test_verify_rejects_tampered_params() -> None:
    params = {"From": "+14155551234", "To": "+14155559999", "Body": "hi"}
    sig = _real_signature(params)
    tampered = {**params, "Body": "tampered"}
    assert verify_twilio_signature(URL, tampered, sig, AUTH_TOKEN) is False


def test_verify_rejects_missing_signature() -> None:
    params = {"From": "+14155551234", "To": "+14155559999", "Body": "hi"}
    assert verify_twilio_signature(URL, params, None, AUTH_TOKEN) is False


def test_verify_rejects_wrong_url() -> None:
    params = {"From": "+14155551234", "To": "+14155559999", "Body": "hi"}
    sig = _real_signature(params)
    assert (
        verify_twilio_signature("https://api.hail.so/sms/wrong-path", params, sig, AUTH_TOKEN)
        is False
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd core && uv run pytest tests/test_twilio_signature.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'hailhq.core.twilio_signature'`

- [ ] **Step 3: Write the implementation**

```python
# core/hailhq/core/twilio_signature.py
"""Verification for Twilio's inbound webhook signature scheme.

Twilio signs `X-Twilio-Signature` as base64(HMAC-SHA1(auth_token, url +
sorted-concatenated-form-params)) — this is Twilio's own scheme, distinct
from the repo's `hailhq.core.hmac_signing` (HMAC-SHA256 over a raw JSON
body), which is used for Hail-to-Hail internal signing (Lambda -> API,
website -> API). Do not conflate the two.

The `url` passed to `RequestValidator.validate` must be the exact public
URL Twilio POSTed to, including scheme and host — get this from
`hailhq.core.urls.canonical_url` composed with the request path, not
`request.url` directly, since a reverse proxy can rewrite scheme/host in
ways that break the signature check (reconstruct the public URL with the
`hailhq.core.urls` helpers — `canonical_url` exists and is used by
`hailhq.core.url_guard`; there is no `ses_events.py` in this repo).
"""

from __future__ import annotations

from twilio.request_validator import RequestValidator

__all__ = ["verify_twilio_signature"]


def verify_twilio_signature(
    url: str, params: dict[str, str], signature: str | None, auth_token: str
) -> bool:
    if not signature:
        return False
    validator = RequestValidator(auth_token)
    return validator.validate(url, params, signature)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd core && uv run pytest tests/test_twilio_signature.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add core/hailhq/core/twilio_signature.py core/tests/test_twilio_signature.py
git commit -m "feat(core): add Twilio inbound webhook signature verification"
```

---

### Task 2: `remove_suppression` + `ChannelSuspension` model/migration/gate check

**Files:**

- Modify: `core/hailhq/core/compliance_gate.py`
- Modify: `core/hailhq/core/models.py`
- Modify: `core/hailhq/core/config.py`
- Create: `api/migrations/versions/0028_channel_suspensions.py`
- Test: `core/tests/test_compliance_gate.py` (append), `core/tests/test_models.py` (append)

**Interfaces:**

- Produces: `remove_suppression(db, *, organization_id, recipient, channel) -> bool` (True if a row was deleted); `ChannelSuspension` model; `check_channel_suspended(db, organization_id, channel) -> bool`.

- [ ] **Step 1: Write the failing tests**

```python
# core/tests/test_compliance_gate.py — append
async def test_remove_suppression_deletes_matching_row(async_session) -> None:
    import uuid
    from hailhq.core.compliance_gate import add_suppression, remove_suppression

    org_id = uuid.uuid4()
    await add_suppression(
        async_session,
        organization_id=org_id,
        recipient="+14155551234",
        channel="sms",
        reason="user opted out",
        source="stop_keyword",
    )
    await async_session.commit()

    removed = await remove_suppression(
        async_session, organization_id=org_id, recipient="+14155551234", channel="sms"
    )
    await async_session.commit()

    assert removed is True

    from hailhq.core.compliance_gate import check_sms_allowed

    result = await check_sms_allowed(async_session, org_id, "+14155551234")
    assert result.allowed is True


async def test_remove_suppression_no_match_returns_false(async_session) -> None:
    import uuid
    from hailhq.core.compliance_gate import remove_suppression

    removed = await remove_suppression(
        async_session, organization_id=uuid.uuid4(), recipient="+14155559999", channel="sms"
    )
    assert removed is False


async def test_check_channel_suspended_blocks_when_suspended(async_session) -> None:
    import uuid
    from hailhq.core.compliance_gate import check_channel_suspended
    from hailhq.core.models import ChannelSuspension

    org_id = uuid.uuid4()
    async_session.add(ChannelSuspension(organization_id=org_id, channel="sms", reason="high opt-out rate"))
    await async_session.commit()

    assert await check_channel_suspended(async_session, org_id, "sms") is True
    assert await check_channel_suspended(async_session, org_id, "voice") is False


async def test_check_sms_allowed_blocks_when_channel_suspended(async_session) -> None:
    import uuid
    from hailhq.core.compliance_gate import check_sms_allowed
    from hailhq.core.models import ChannelSuspension

    org_id = uuid.uuid4()
    async_session.add(ChannelSuspension(organization_id=org_id, channel="sms", reason="abuse"))
    await async_session.commit()

    result = await check_sms_allowed(async_session, org_id, "+14155551234")
    assert result.allowed is False
    assert "suspend" in result.reason.lower()
```

```python
# core/tests/test_models.py — append
async def test_channel_suspension_unique_per_org_and_channel(async_session) -> None:
    import uuid
    from sqlalchemy.exc import IntegrityError
    from hailhq.core.models import ChannelSuspension

    org_id = uuid.uuid4()
    async_session.add(ChannelSuspension(organization_id=org_id, channel="sms", reason="a"))
    await async_session.commit()

    async_session.add(ChannelSuspension(organization_id=org_id, channel="sms", reason="b"))
    with pytest.raises(IntegrityError):
        await async_session.commit()
```

(Add `import pytest` at the top of `test_models.py` if not already present.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd core && uv run pytest tests/test_compliance_gate.py tests/test_models.py -v -k "suppression or suspen"`
Expected: FAIL — `ImportError: cannot import name 'remove_suppression'` / `ChannelSuspension`

- [ ] **Step 3: Add the `ChannelSuspension` model**

In `core/hailhq/core/models.py`, add immediately after the `Suppression` class:

```python
class ChannelSuspension(Base):
    """A targeted per-org, per-channel sending pause — distinct from
    ``OrgClosure`` (whole-account closure) and from ``Suppression``
    (per-recipient opt-out). Backs the abuse-monitoring guardrail: when an
    org's opt-out rate on a channel crosses a threshold, a row here blocks
    further sends on that channel until an operator lifts it (or, later,
    an automated cooldown expires).
    """

    __tablename__ = "channel_suspensions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    channel: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    suspended_at: Mapped[datetime] = mapped_column(
        TS, server_default=text("now()"), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "channel IN ('sms','voice','email')",
            name="channel_suspensions_channel_check",
        ),
        UniqueConstraint(
            "organization_id", "channel", name="channel_suspensions_org_channel_uniq"
        ),
    )
```

Add `UniqueConstraint` to the existing `from sqlalchemy import (...)` import block at the top of `models.py` if it isn't already imported (check first — `CheckConstraint`/`Index` are already there per Phase 1).

- [ ] **Step 4: Write the migration**

```python
# api/migrations/versions/0028_channel_suspensions.py
"""channel_suspensions table — per-org, per-channel sending pause.

Backs the abuse-monitoring guardrail described in the SMS design spec:
one platform-level 10DLC campaign means one org's abusive traffic can get
everyone throttled, so a targeted pause on just that org+channel is the
mitigation. Distinct from org_closures (whole-account) and suppressions
(per-recipient).

Revision ID: 0028
Revises: 0027
"""

from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0028"
down_revision: Union[str, None] = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "channel_suspensions",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("organization_id", UUID(as_uuid=True), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "suspended_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("channel IN ('sms','voice','email')", name="channel_suspensions_channel_check"),
        sa.UniqueConstraint("organization_id", "channel", name="channel_suspensions_org_channel_uniq"),
    )


def downgrade() -> None:
    op.drop_table("channel_suspensions")
```

Run: `cd api && uv run alembic upgrade head` — expect `Running upgrade 0027 -> 0028, channel_suspensions table`.

- [ ] **Step 5: Add `remove_suppression` and `check_channel_suspended` to `compliance_gate.py`**

Add both names to `__all__`. Add `ChannelSuspension` to the existing `from hailhq.core.models import Sms, Suppression, UsageEvent` line (making it `Sms, Suppression, UsageEvent, ChannelSuspension`). Append after `add_suppression`:

```python
async def remove_suppression(
    db: AsyncSession, *, organization_id: UUID | None, recipient: str, channel: str
) -> bool:
    """Delete a suppression row matching (recipient, channel), scoped to
    this org OR a platform-wide (NULL org) row — the mirror of
    ``add_suppression`` for the STOP->START re-subscribe flow. Flushes but
    does not commit, matching this module's session convention. Returns
    True iff a row was actually deleted."""
    normalized = normalize_recipient(recipient)
    stmt = select(Suppression).where(
        Suppression.recipient == normalized,
        Suppression.channel == channel,
        or_(
            Suppression.organization_id == organization_id,
            Suppression.organization_id.is_(None),
        ),
    )
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        return False
    await db.delete(row)
    await db.flush()
    return True


async def check_channel_suspended(db: AsyncSession, organization_id: UUID, channel: str) -> bool:
    """True iff this org has an active ChannelSuspension for this channel."""
    stmt = select(ChannelSuspension).where(
        ChannelSuspension.organization_id == organization_id,
        ChannelSuspension.channel == channel,
    )
    return (await db.execute(stmt)).scalar_one_or_none() is not None
```

- [ ] **Step 6: Wire the suspension check into `check_sms_allowed`**

In `check_sms_allowed` (already exists from Phase 1), add a suspension check as the FIRST check, before the suppression-hit check:

```python
    if await check_channel_suspended(db, organization_id, "sms"):
        checks["channel_suspended"] = True
        return GateResult(
            allowed=False,
            reason="SMS sending is suspended for this organization (contact support)",
            checks=checks,
        )
    checks["channel_suspended"] = False
```

(Insert this immediately after `checks: dict[str, Any] = {}` and before the existing `reason = await _check_phone_destination(db, organization_id, to_e164, "sms", checks)` line — `check_sms_allowed` on `main` does NOT call `_suppression_hit` directly; the suppression scrub happens inside `_check_phone_destination`.)

- [ ] **Step 7: Add abuse-monitoring config settings**

In `core/hailhq/core/config.py`, immediately after the sms velocity settings from Phase 1:

```python
    # Abuse-monitoring guardrail (SMS opt-out rate -> ChannelSuspension).
    # Conservative starting thresholds per the design spec's own caution
    # that these are unvalidated pending real traffic data — tune post-launch.
    hail_sms_abuse_window_hours: int = 24
    hail_sms_abuse_min_sends: int = 20  # floor: don't flag low-volume orgs
    hail_sms_abuse_max_opt_out_rate: float = 0.05  # 5% opt-out rate trips it
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `cd core && uv run pytest tests/test_compliance_gate.py tests/test_models.py -v -k "suppression or suspen"`
Expected: 6 passed (2 new remove_suppression + 2 new suspension + the 2 you added to test_models.py, adjust count to what's actually new)

- [ ] **Step 9: Run full core regression suite**

Run: `cd core && uv run pytest -v`
Expected: all passed (existing voice/email suppression tests unaffected — `remove_suppression`/`ChannelSuspension` are additive)

- [ ] **Step 10: Commit**

```bash
git add core/hailhq/core/compliance_gate.py core/hailhq/core/models.py core/hailhq/core/config.py core/tests/test_compliance_gate.py core/tests/test_models.py api/migrations/versions/0028_channel_suspensions.py
git commit -m "feat(core): add ChannelSuspension, remove_suppression, wire into check_sms_allowed"
```

---

### Task 3: `fanout_sms_event` + `sms_ingest.py`

**Files:**

- Modify: `core/hailhq/core/webhook_fanout.py`
- Create: `core/hailhq/core/sms_ingest.py`
- Test: `core/tests/test_sms_ingest.py`

**Interfaces:**

- Consumes: `WebhookSubscription`, `WebhookDelivery` models (unchanged); `Sms` model (Phase 1); `add_suppression`/`remove_suppression` (Task 2); `PhoneNumber` model.
- Produces: `fanout_sms_event(db, *, organization_id, event_type, event_id, data) -> int`; `ingest_inbound_sms(db, *, from_e164, to_e164, body, provider_message_sid, opt_out_type) -> IngestResult`.

- [ ] **Step 1: Write the failing test**

```python
# core/tests/test_sms_ingest.py
"""Tests for inbound SMS ingest: org resolution, idempotent insert,
opt-out handling, and webhook fan-out."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from hailhq.core.models import PhoneNumber, Sms, Suppression, WebhookDelivery, WebhookSubscription
from hailhq.core.sms_ingest import ingest_inbound_sms


async def _seed_number(session, organization_id) -> PhoneNumber:
    pn = PhoneNumber(
        organization_id=organization_id,
        e164="+14155559999",
        country_code="US",
        number_type="local",
        provider_resource_id="PN_test",
        provisioning_state="active",
    )
    session.add(pn)
    await session.flush()
    return pn


async def test_ingest_unknown_number_is_dropped_not_error(async_session) -> None:
    result = await ingest_inbound_sms(
        async_session,
        from_e164="+14155551234",
        to_e164="+19999999999",  # not registered to anyone
        body="hi",
        provider_message_sid="SM_test_1",
        opt_out_type=None,
    )
    assert result.dropped_reason == "unknown_number"
    assert result.sms_id is None


async def test_ingest_creates_inbound_row_and_fans_out(async_session) -> None:
    org_id = uuid.uuid4()
    await _seed_number(async_session, org_id)
    async_session.add(
        WebhookSubscription(
            organization_id=org_id,
            target_url="https://example.com/hook",
            secret_encrypted=b"fake",
            status="active",
            event_types=["sms.received"],
        )
    )
    await async_session.commit()

    result = await ingest_inbound_sms(
        async_session,
        from_e164="+14155551234",
        to_e164="+14155559999",
        body="hello back",
        provider_message_sid="SM_test_2",
        opt_out_type=None,
    )
    await async_session.commit()

    assert result.sms_id is not None
    sms = (await async_session.execute(select(Sms).where(Sms.id == result.sms_id))).scalar_one()
    assert sms.direction == "inbound"
    assert sms.status == "received"
    assert sms.organization_id == org_id

    deliveries = (
        await async_session.execute(select(WebhookDelivery).where(WebhookDelivery.event_type == "sms.received"))
    ).scalars().all()
    assert len(deliveries) == 1


async def test_ingest_duplicate_message_sid_is_idempotent(async_session) -> None:
    org_id = uuid.uuid4()
    await _seed_number(async_session, org_id)
    await async_session.commit()

    first = await ingest_inbound_sms(
        async_session, from_e164="+14155551234", to_e164="+14155559999",
        body="hi", provider_message_sid="SM_dupe", opt_out_type=None,
    )
    await async_session.commit()

    second = await ingest_inbound_sms(
        async_session, from_e164="+14155551234", to_e164="+14155559999",
        body="hi", provider_message_sid="SM_dupe", opt_out_type=None,
    )
    await async_session.commit()

    assert second.sms_id == first.sms_id
    count = (
        await async_session.execute(select(Sms).where(Sms.provider_message_sid == "SM_dupe"))
    ).scalars().all()
    assert len(count) == 1


async def test_ingest_stop_adds_suppression() -> None:
    pass  # see full test below, uses async_session — kept separate for clarity


async def test_ingest_stop_keyword_adds_suppression(async_session) -> None:
    org_id = uuid.uuid4()
    await _seed_number(async_session, org_id)
    await async_session.commit()

    await ingest_inbound_sms(
        async_session, from_e164="+14155551234", to_e164="+14155559999",
        body="STOP", provider_message_sid="SM_stop", opt_out_type="STOP",
    )
    await async_session.commit()

    hit = (
        await async_session.execute(
            select(Suppression).where(Suppression.recipient == "+14155551234", Suppression.channel == "sms")
        )
    ).scalar_one_or_none()
    assert hit is not None
    assert hit.source == "stop_keyword"


async def test_ingest_start_removes_suppression(async_session) -> None:
    from hailhq.core.compliance_gate import add_suppression

    org_id = uuid.uuid4()
    await _seed_number(async_session, org_id)
    await add_suppression(
        async_session, organization_id=org_id, recipient="+14155551234",
        channel="sms", reason="prior stop", source="stop_keyword",
    )
    await async_session.commit()

    await ingest_inbound_sms(
        async_session, from_e164="+14155551234", to_e164="+14155559999",
        body="START", provider_message_sid="SM_start", opt_out_type="START",
    )
    await async_session.commit()

    hit = (
        await async_session.execute(
            select(Suppression).where(Suppression.recipient == "+14155551234", Suppression.channel == "sms")
        )
    ).scalar_one_or_none()
    assert hit is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd core && uv run pytest tests/test_sms_ingest.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'hailhq.core.sms_ingest'`

- [ ] **Step 3: Add `fanout_sms_event` to `webhook_fanout.py`**

Add immediately after `fanout_email_event`:

```python
async def fanout_sms_event(
    db: AsyncSession,
    *,
    organization_id: UUID,
    event_type: str,
    event_id: UUID,
    data: dict[str, Any],
) -> int:
    """Insert one WebhookDelivery row per active subscription whose
    event_types includes event_type. Thin SMS-shaped wrapper around the
    same delivery mechanism fanout_email_event uses — email_domain_id is
    always None here (SMS has no domain concept)."""
    subs = (
        await db.execute(
            select(WebhookSubscription).where(
                WebhookSubscription.organization_id == organization_id,
                WebhookSubscription.status == "active",
            )
        )
    ).scalars().all()
    inserted = 0
    for sub in subs:
        if event_type not in (sub.event_types or []):
            continue
        db.add(
            WebhookDelivery(
                subscription_id=sub.id,
                email_domain_id=None,
                event_type=event_type,
                event_id=event_id,
                payload=_payload(organization_id, data),
            )
        )
        inserted += 1
    return inserted
```

(This reuses the module's existing private `_payload` helper — check its exact name/signature in the file first and match it; if it's inlined rather than a shared helper in `fanout_email_event`, inline the same shape here instead of assuming a `_payload` function exists.)

- [ ] **Step 4: Write `sms_ingest.py`**

```python
# core/hailhq/core/sms_ingest.py
"""Inbound SMS ingest: resolve the owning org by the Twilio `To` number,
idempotently persist the message, detect and apply STOP/START opt-out
signals, and fan out to the org's webhook subscribers.

Mirrors core/hailhq/core/email_ingest.py's shape (verify -> resolve org ->
persist -> fan out) but simpler: there's no forwarding, no attachment
parsing, and org resolution is a single PhoneNumber lookup rather than a
domain-suffix match.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.core.compliance_gate import add_suppression, remove_suppression
from hailhq.core.models import PhoneNumber, Sms
from hailhq.core.webhook_fanout import fanout_sms_event

logger = logging.getLogger(__name__)

__all__ = ["IngestResult", "ingest_inbound_sms"]


@dataclass
class IngestResult:
    sms_id: UUID | None
    dropped_reason: str | None = None


async def _resolve_org_for_number(db: AsyncSession, to_e164: str) -> PhoneNumber | None:
    stmt = select(PhoneNumber).where(
        PhoneNumber.e164 == to_e164,
        PhoneNumber.is_pool.is_(False),
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def ingest_inbound_sms(
    db: AsyncSession,
    *,
    from_e164: str,
    to_e164: str,
    body: str,
    provider_message_sid: str,
    opt_out_type: str | None,
) -> IngestResult:
    number = await _resolve_org_for_number(db, to_e164)
    if number is None or number.organization_id is None:
        logger.info("inbound sms to unrecognized/pool number=%s dropped", to_e164)
        return IngestResult(sms_id=None, dropped_reason="unknown_number")

    organization_id = number.organization_id

    # Idempotent insert: a duplicate webhook delivery (Twilio retry) must
    # not create a second row. provider_message_sid is unique in the Sms
    # table (the Sms table + its unique constraint were created in
    # 0025_sms.py), so a duplicate insert raises
    # IntegrityError — catch it and return the existing row's id, mirroring
    # email_ingest.py's SAVEPOINT dedup pattern.
    async with db.begin_nested():
        sms = Sms(
            organization_id=organization_id,
            from_number_id=number.id,
            from_e164=from_e164,
            to_e164=to_e164,
            direction="inbound",
            status="received",
            body=body,
            provider_message_sid=provider_message_sid,
        )
        db.add(sms)
        try:
            await db.flush()
        except IntegrityError:
            await db.rollback()
            existing = (
                await db.execute(select(Sms).where(Sms.provider_message_sid == provider_message_sid))
            ).scalar_one_or_none()
            return IngestResult(sms_id=existing.id if existing else None)

    if opt_out_type == "STOP":
        await add_suppression(
            db,
            organization_id=organization_id,
            recipient=from_e164,
            channel="sms",
            reason="recipient replied STOP",
            source="stop_keyword",
        )
    elif opt_out_type == "START":
        await remove_suppression(
            db, organization_id=organization_id, recipient=from_e164, channel="sms"
        )

    await fanout_sms_event(
        db,
        organization_id=organization_id,
        event_type="sms.received",
        event_id=sms.id,
        data={
            "id": str(sms.id),
            "from": from_e164,
            "to": to_e164,
            "body": body,
        },
    )

    return IngestResult(sms_id=sms.id)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd core && uv run pytest tests/test_sms_ingest.py -v`
Expected: 6 passed

- [ ] **Step 6: Run full core regression suite**

Run: `cd core && uv run pytest -v`
Expected: all passed

- [ ] **Step 7: Commit**

```bash
git add core/hailhq/core/webhook_fanout.py core/hailhq/core/sms_ingest.py core/tests/test_sms_ingest.py
git commit -m "feat(core): add inbound sms ingest with opt-out handling and webhook fanout"
```

---

### Task 4: Inbound webhook route + suppression list/delete routes

**Files:**

- Modify: `api/hailhq/api/routes/sms.py`
- Modify: `core/hailhq/core/config.py` (if a dedicated inbound webhook auth setting is needed — see Step 3)
- Test: `api/tests/test_sms_inbound_api.py` (new), `api/tests/test_sms_suppressions_api.py` (new)

**Interfaces:**

- Consumes: `verify_twilio_signature` (Task 1), `ingest_inbound_sms` (Task 3), `remove_suppression` (Task 2), `Suppression` model, `fetch_cursor_page`.
- Produces: `POST /sms/inbound` (public, Twilio-signature-verified, not org-authenticated — org is resolved from the payload); `GET /sms/suppressions`, `DELETE /sms/suppressions/{number}` (org-authenticated, same as the rest of `sms.py`).

- [ ] **Step 1: Write the failing tests**

```python
# api/tests/test_sms_inbound_api.py
"""Tests for POST /sms/inbound — the Twilio inbound webhook."""

from __future__ import annotations

import uuid

from twilio.request_validator import RequestValidator

AUTH_TOKEN = "test-twilio-auth-token"
INBOUND_URL = "http://t/sms/inbound"


def _signed_form(params: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    sig = RequestValidator(AUTH_TOKEN).compute_signature(INBOUND_URL, params)
    return params, {"X-Twilio-Signature": sig}


async def _seed_number(async_session, organization_id) -> None:
    from hailhq.core.models import PhoneNumber

    pn = PhoneNumber(
        organization_id=organization_id, e164="+14155559999", country_code="US",
        number_type="local", provider_resource_id="PN_test", provisioning_state="active",
    )
    async_session.add(pn)
    await async_session.commit()


async def test_inbound_rejects_bad_signature(client, monkeypatch) -> None:
    from hailhq.core.config import settings

    monkeypatch.setattr(settings, "twilio_auth_token", AUTH_TOKEN)
    params = {"From": "+14155551234", "To": "+14155559999", "Body": "hi", "MessageSid": "SM1"}
    resp = await client.post(
        "/sms/inbound", data=params, headers={"X-Twilio-Signature": "sha1=bogus"}
    )
    assert resp.status_code == 403


async def test_inbound_accepts_valid_signature_and_creates_row(
    client, async_session, monkeypatch
) -> None:
    from hailhq.core.config import settings

    monkeypatch.setattr(settings, "twilio_auth_token", AUTH_TOKEN)
    org_id = uuid.uuid4()
    await _seed_number(async_session, org_id)

    params = {"From": "+14155551234", "To": "+14155559999", "Body": "hi", "MessageSid": "SM_ok"}
    form, headers = _signed_form(params)
    resp = await client.post("/sms/inbound", data=form, headers=headers)
    assert resp.status_code == 200


async def test_inbound_unknown_number_still_returns_200(client, monkeypatch) -> None:
    from hailhq.core.config import settings

    monkeypatch.setattr(settings, "twilio_auth_token", AUTH_TOKEN)
    params = {"From": "+14155551234", "To": "+19999999999", "Body": "hi", "MessageSid": "SM_unknown"}
    form, headers = _signed_form(params)
    resp = await client.post("/sms/inbound", data=form, headers=headers)
    # Twilio expects 200 regardless, to avoid retry storms on numbers we don't own.
    assert resp.status_code == 200
```

```python
# api/tests/test_sms_suppressions_api.py
"""Tests for GET /sms/suppressions and DELETE /sms/suppressions/{number}."""

from __future__ import annotations


async def test_list_suppressions_empty(client, org_and_key) -> None:
    _, _, plaintext = org_and_key
    resp = await client.get("/sms/suppressions", headers={"Authorization": f"Bearer {plaintext}"})
    assert resp.status_code == 200
    assert resp.json() == {"items": [], "next_cursor": None}


async def test_list_suppressions_returns_org_rows(client, async_session, org_and_key) -> None:
    from hailhq.core.compliance_gate import add_suppression

    org_id, _, plaintext = org_and_key
    await add_suppression(
        async_session, organization_id=org_id, recipient="+14155551234",
        channel="sms", reason="opted out", source="stop_keyword",
    )
    await async_session.commit()

    resp = await client.get("/sms/suppressions", headers={"Authorization": f"Bearer {plaintext}"})
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["recipient"] == "+14155551234"


async def test_delete_suppression_removes_row(client, async_session, org_and_key) -> None:
    from hailhq.core.compliance_gate import add_suppression

    org_id, _, plaintext = org_and_key
    await add_suppression(
        async_session, organization_id=org_id, recipient="+14155551234",
        channel="sms", reason="opted out", source="stop_keyword",
    )
    await async_session.commit()

    resp = await client.delete(
        "/sms/suppressions/+14155551234", headers={"Authorization": f"Bearer {plaintext}"}
    )
    assert resp.status_code == 204

    resp = await client.get("/sms/suppressions", headers={"Authorization": f"Bearer {plaintext}"})
    assert resp.json()["items"] == []


async def test_delete_suppression_not_found(client, org_and_key) -> None:
    _, _, plaintext = org_and_key
    resp = await client.delete(
        "/sms/suppressions/+14155559999", headers={"Authorization": f"Bearer {plaintext}"}
    )
    assert resp.status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd api && uv run pytest tests/test_sms_inbound_api.py tests/test_sms_suppressions_api.py -v`
Expected: FAIL — 404s on nonexistent routes.

- [ ] **Step 3: Add `SuppressionResponse`/`SuppressionListResponse` schemas**

In `core/hailhq/core/schemas.py`, after the `SmsListResponse` class:

```python
class SuppressionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    recipient: str
    channel: str
    reason: str
    source: str
    created_at: datetime


class SuppressionListResponse(BaseModel):
    items: list[SuppressionResponse]
    next_cursor: str | None = None
```

- [ ] **Step 4: Add the routes to `api/hailhq/api/routes/sms.py`**

Add these imports at the top: `from fastapi import Request` (for reading the raw form body/URL), `from hailhq.core.twilio_signature import verify_twilio_signature`, `from hailhq.core.sms_ingest import ingest_inbound_sms`, `from hailhq.core.compliance_gate import remove_suppression`, `from hailhq.core.models import Suppression`, `from hailhq.core.schemas import SuppressionListResponse, SuppressionResponse`, `from hailhq.core.urls import canonical_url` (confirmed present on `main`: `canonical_url(url: str) -> str` in `core/hailhq/core/urls.py`, also used by `hailhq.core.url_guard`).

Append these three routes at the end of the file, before `__all__`:

```python
@router.post("/inbound", include_in_schema=False)
async def receive_inbound_sms(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    form = await request.form()
    params = {k: str(v) for k, v in form.items()}
    signature = request.headers.get("X-Twilio-Signature")
    url = canonical_url(str(request.url))

    if not verify_twilio_signature(url, params, signature, settings.twilio_auth_token):
        raise HTTPException(status_code=http_status.HTTP_403_FORBIDDEN, detail="invalid signature")

    opt_out_type = params.get("OptOutType")
    await ingest_inbound_sms(
        db,
        from_e164=params.get("From", ""),
        to_e164=params.get("To", ""),
        body=params.get("Body", ""),
        provider_message_sid=params.get("MessageSid", ""),
        opt_out_type=opt_out_type,
    )
    await db.commit()
    return Response(status_code=http_status.HTTP_200_OK)


@router.get("/suppressions", response_model=SuppressionListResponse)
async def list_sms_suppressions(
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    cursor: str | None = Query(default=None),
    limit: int = Query(default=_DEFAULT_LIST_LIMIT, ge=1, le=_MAX_LIST_LIMIT),
) -> SuppressionListResponse:
    stmt = select(Suppression).where(
        Suppression.organization_id == principal.organization_id,
        Suppression.channel == "sms",
    )
    rows, next_cursor = await fetch_cursor_page(
        db, stmt, Suppression.created_at, Suppression.id, cursor=cursor, limit=limit, newest_first=True
    )
    return SuppressionListResponse(
        items=[SuppressionResponse.model_validate(r) for r in rows],
        next_cursor=next_cursor,
    )


@router.delete("/suppressions/{number}", status_code=http_status.HTTP_204_NO_CONTENT)
async def delete_sms_suppression(
    number: str,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    removed = await remove_suppression(
        db, organization_id=principal.organization_id, recipient=number, channel="sms"
    )
    if not removed:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="suppression not found")
    await db.commit()
```

Note: `Response` needs importing from `fastapi` alongside the existing `Response` import (already imported per Phase 1's route for the `Location` header — reuse it).

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd api && uv run pytest tests/test_sms_inbound_api.py tests/test_sms_suppressions_api.py -v`
Expected: 7 passed

- [ ] **Step 6: Run full API regression suite**

Run: `cd api && uv run pytest -v`
Expected: all passed

- [ ] **Step 7: Commit**

```bash
git add api/hailhq/api/routes/sms.py core/hailhq/core/schemas.py api/tests/test_sms_inbound_api.py api/tests/test_sms_suppressions_api.py
git commit -m "feat(api): add inbound sms webhook and suppression list/delete routes"
```

---

### Task 5: Abuse-monitoring scheduled check

**Files:**

- Create: `core/hailhq/core/abuse_monitor.py`
- Modify: `api/hailhq/api/main.py` (wire into lifespan, mirroring the existing worker-startup pattern)
- Test: `core/tests/test_abuse_monitor.py`

**Interfaces:**

- Produces: `async def check_and_suspend_abusive_orgs(db_session_factory) -> int` (returns count of orgs newly suspended); a `run_forever()` loop wired into `main.py`'s lifespan, matching `OutboundForwardWorker`/`DomainVerificationWorker`'s existing pattern.

- [ ] **Step 1: Write the failing test**

```python
# core/tests/test_abuse_monitor.py
"""Tests for the SMS abuse-monitoring guardrail: rolling opt-out rate ->
ChannelSuspension."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from hailhq.core.abuse_monitor import check_and_suspend_abusive_orgs
from hailhq.core.models import ChannelSuspension, Suppression, UsageEvent


async def _seed_sends(session, org_id, count: int) -> None:
    now = datetime.now(timezone.utc)
    for i in range(count):
        session.add(
            UsageEvent(organization_id=org_id, channel="sms", units=1, ref=f"sms:{i}", occurred_at=now)
        )
    await session.flush()


async def _seed_opt_outs(session, org_id, count: int) -> None:
    for i in range(count):
        session.add(
            Suppression(
                organization_id=org_id, recipient=f"+1415555{1000+i}", channel="sms",
                reason="stop", source="stop_keyword",
            )
        )
    await session.flush()


async def test_high_opt_out_rate_triggers_suspension(async_session, monkeypatch) -> None:
    from hailhq.core import config

    monkeypatch.setattr(config.settings, "hail_sms_abuse_min_sends", 10)
    monkeypatch.setattr(config.settings, "hail_sms_abuse_max_opt_out_rate", 0.05)

    org_id = uuid.uuid4()
    await _seed_sends(async_session, org_id, 20)
    await _seed_opt_outs(async_session, org_id, 5)  # 25% opt-out rate, well over 5%
    await async_session.commit()

    suspended_count = await check_and_suspend_abusive_orgs(async_session)
    await async_session.commit()

    assert suspended_count == 1
    from sqlalchemy import select

    row = (
        await async_session.execute(
            select(ChannelSuspension).where(ChannelSuspension.organization_id == org_id)
        )
    ).scalar_one_or_none()
    assert row is not None
    assert row.channel == "sms"


async def test_low_volume_org_is_not_flagged_even_with_high_rate(async_session, monkeypatch) -> None:
    from hailhq.core import config

    monkeypatch.setattr(config.settings, "hail_sms_abuse_min_sends", 20)
    monkeypatch.setattr(config.settings, "hail_sms_abuse_max_opt_out_rate", 0.05)

    org_id = uuid.uuid4()
    await _seed_sends(async_session, org_id, 5)  # below the min_sends floor
    await _seed_opt_outs(async_session, org_id, 3)  # 60% rate, but volume too low
    await async_session.commit()

    suspended_count = await check_and_suspend_abusive_orgs(async_session)

    assert suspended_count == 0


async def test_healthy_org_is_not_flagged(async_session, monkeypatch) -> None:
    from hailhq.core import config

    monkeypatch.setattr(config.settings, "hail_sms_abuse_min_sends", 10)
    monkeypatch.setattr(config.settings, "hail_sms_abuse_max_opt_out_rate", 0.05)

    org_id = uuid.uuid4()
    await _seed_sends(async_session, org_id, 100)
    await _seed_opt_outs(async_session, org_id, 1)  # 1% rate
    await async_session.commit()

    suspended_count = await check_and_suspend_abusive_orgs(async_session)

    assert suspended_count == 0


async def test_already_suspended_org_is_not_double_suspended(async_session, monkeypatch) -> None:
    from hailhq.core import config

    monkeypatch.setattr(config.settings, "hail_sms_abuse_min_sends", 10)
    monkeypatch.setattr(config.settings, "hail_sms_abuse_max_opt_out_rate", 0.05)

    org_id = uuid.uuid4()
    await _seed_sends(async_session, org_id, 20)
    await _seed_opt_outs(async_session, org_id, 5)
    async_session.add(ChannelSuspension(organization_id=org_id, channel="sms", reason="already flagged"))
    await async_session.commit()

    suspended_count = await check_and_suspend_abusive_orgs(async_session)

    assert suspended_count == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd core && uv run pytest tests/test_abuse_monitor.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'hailhq.core.abuse_monitor'`

- [ ] **Step 3: Write the implementation**

```python
# core/hailhq/core/abuse_monitor.py
"""SMS abuse-monitoring guardrail.

Because all orgs share one Hail-owned A2P 10DLC Brand/Campaign (see the
SMS design spec's Decision 2 and its accepted-risk note), one org's
abusive traffic risks getting the whole platform's SMS sending throttled
by carriers. This module computes each org's rolling opt-out rate over a
configurable window and inserts a ChannelSuspension row when it crosses a
threshold — the actual mitigation for that accepted risk, not optional
polish.

Thresholds (core/hailhq/core/config.py's hail_sms_abuse_* settings) are
explicitly a starting guess pending real traffic data, per the design
spec's own caution — expect to tune post-launch.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.core.config import settings
from hailhq.core.models import ChannelSuspension, Suppression, UsageEvent

logger = logging.getLogger(__name__)

__all__ = ["check_and_suspend_abusive_orgs"]


async def check_and_suspend_abusive_orgs(db: AsyncSession) -> int:
    """Scan for orgs whose SMS opt-out rate over the configured window
    exceeds the threshold, and suspend any not already suspended. Returns
    the count of orgs newly suspended this run."""
    window_start = datetime.now(timezone.utc) - timedelta(
        hours=settings.hail_sms_abuse_window_hours
    )

    send_counts = dict(
        (
            await db.execute(
                select(UsageEvent.organization_id, func.count())
                .where(UsageEvent.channel == "sms", UsageEvent.occurred_at >= window_start)
                .group_by(UsageEvent.organization_id)
            )
        ).all()
    )

    opt_out_counts = dict(
        (
            await db.execute(
                select(Suppression.organization_id, func.count())
                .where(
                    Suppression.channel == "sms",
                    Suppression.source == "stop_keyword",
                    Suppression.created_at >= window_start,
                    Suppression.organization_id.is_not(None),
                )
                .group_by(Suppression.organization_id)
            )
        ).all()
    )

    already_suspended = set(
        (
            await db.execute(
                select(ChannelSuspension.organization_id).where(ChannelSuspension.channel == "sms")
            )
        )
        .scalars()
        .all()
    )

    suspended = 0
    for org_id, sends in send_counts.items():
        if org_id in already_suspended:
            continue
        if sends < settings.hail_sms_abuse_min_sends:
            continue
        opt_outs = opt_out_counts.get(org_id, 0)
        rate = opt_outs / sends
        if rate > settings.hail_sms_abuse_max_opt_out_rate:
            db.add(
                ChannelSuspension(
                    organization_id=org_id,
                    channel="sms",
                    reason=(
                        f"opt-out rate {rate:.1%} over {settings.hail_sms_abuse_window_hours}h "
                        f"window ({opt_outs}/{sends}) exceeds "
                        f"{settings.hail_sms_abuse_max_opt_out_rate:.1%} threshold"
                    ),
                )
            )
            logger.warning("suspending org=%s sms channel for abuse: rate=%.1f%%", org_id, rate * 100)
            suspended += 1
    await db.flush()
    return suspended
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd core && uv run pytest tests/test_abuse_monitor.py -v`
Expected: 4 passed

- [ ] **Step 5: Wire a periodic worker into `main.py`'s lifespan**

Read `api/hailhq/api/main.py`'s `lifespan()` function first to see the exact existing pattern for a periodic worker (e.g. how `DomainVerificationWorker`/`OutboundForwardWorker` are started/stopped, and the polling-interval settings convention like `HAIL_DOMAIN_VERIFY_POLL_SECONDS`). Add a new setting `hail_abuse_monitor_poll_seconds: int = 3600` to `config.py` (hourly by default — this is a coarse-grained batch check, not a per-send check), and a small wrapper loop in `abuse_monitor.py`:

```python
async def run_forever(session_factory, *, poll_seconds: int) -> None:
    """Periodic loop wrapper, matching the existing worker convention in
    this codebase (see OutboundForwardWorker/DomainVerificationWorker)."""
    import asyncio

    while True:
        try:
            async with session_factory() as db:
                count = await check_and_suspend_abusive_orgs(db)
                await db.commit()
                if count:
                    logger.info("abuse monitor suspended %d org(s) this run", count)
        except Exception:  # pragma: no cover - logged, loop continues
            logger.warning("abuse monitor tick failed", exc_info=True)
        await asyncio.sleep(poll_seconds)
```

Add `"run_forever"` to `__all__`. Then in `main.py`'s `lifespan()`, follow the exact existing pattern for starting/canceling a background task for the other periodic workers (create an `asyncio.create_task(abuse_monitor.run_forever(session_factory, poll_seconds=settings.hail_abuse_monitor_poll_seconds))` alongside the others, cancel it on shutdown the same way).

- [ ] **Step 6: Run full core regression suite**

Run: `cd core && uv run pytest -v`
Expected: all passed

- [ ] **Step 7: Commit**

```bash
git add core/hailhq/core/abuse_monitor.py core/hailhq/core/config.py core/tests/test_abuse_monitor.py api/hailhq/api/main.py
git commit -m "feat(core): add SMS abuse-monitoring guardrail and periodic worker"
```

---

### Task 6: CLI/SDK for suppressions

**Files:**

- Modify: `cli/internal/cmd/sms.go`
- Modify: `sdk/hail/models.py`, `sdk/hail/client.py`
- Test: `cli/internal/cmd/sms_test.go` (append), `sdk/tests/test_sms.py` (append)

**Interfaces:**

- Produces: `hail sms suppressions list|delete <number>`; `Client.sms.suppressions.list()`/`.delete(number)`.

- [ ] **Step 1: Regenerate the OpenAPI spec and Go client**

This depends on Task 4's new routes existing. Run: `curl -s http://localhost:8080/openapi.json | python -c "import json, sys, yaml; yaml.safe_dump(json.load(sys.stdin), sys.stdout, sort_keys=False)" > openapi/openapi.yaml`, then `npx --yes prettier --write openapi/openapi.yaml` (matching Phase 1's Task 6 finding that raw PyYAML output needs prettier-formatting to produce a clean diff outside the commit-time hook), then `cd cli && make generate` (check `cli/Makefile` for the exact target name first) to regenerate `client.gen.go`. Grep the result for the new operationIds (`ListSmsSuppressionsSmsSuppressionsGetWithResponse`, `DeleteSmsSuppressionSmsSuppressionsNumberDeleteWithResponse` or whatever the actual FastAPI-derived names are — verify, don't assume, per the lesson from Phase 1's CLI task).

- [ ] **Step 2: Add CLI subcommands**

Add a `suppressions` subcommand tree to `newSmsCmd` in `cli/internal/cmd/sms.go`, following the exact same `newSmsStatusCmd`/`newSmsListCmd` pattern already in that file:

```go
func newSmsSuppressionsCmd(opts *Options) *cobra.Command {
	cmd := &cobra.Command{
		Use:   "suppressions",
		Short: "Manage the SMS opt-out (suppression) list",
	}
	cmd.AddCommand(newSmsSuppressionsListCmd(opts))
	cmd.AddCommand(newSmsSuppressionsDeleteCmd(opts))
	return cmd
}

func newSmsSuppressionsListCmd(opts *Options) *cobra.Command {
	return &cobra.Command{
		Use:   "list",
		Short: "List opted-out numbers",
		Args:  argsOrHelp(0, ""),
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			apiClient, err := opts.newClient()
			if err != nil {
				return err
			}
			resp, err := apiClient.ListSmsSuppressionsSmsSuppressionsGetWithResponse(ctx, &client.ListSmsSuppressionsSmsSuppressionsGetParams{})
			if err != nil {
				return fmt.Errorf("sms suppressions API: %w", err)
			}
			if resp.HTTPResponse.StatusCode != http.StatusOK || resp.JSON200 == nil {
				return apiError(resp.HTTPResponse.StatusCode, resp.Body)
			}
			if opts.JSON {
				return printJSON(opts.Stdout, resp.JSON200)
			}
			for _, s := range resp.JSON200.Items {
				fmt.Fprintf(opts.Stdout, "%s  %s  %s\n", s.Recipient, s.Reason, s.Source)
			}
			return nil
		},
	}
}

func newSmsSuppressionsDeleteCmd(opts *Options) *cobra.Command {
	return &cobra.Command{
		Use:   "delete <number>",
		Short: "Remove a number from the opt-out list (manual correction only)",
		Args:  argsOrHelp(1, "<number>"),
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			apiClient, err := opts.newClient()
			if err != nil {
				return err
			}
			resp, err := apiClient.DeleteSmsSuppressionSmsSuppressionsNumberDeleteWithResponse(ctx, args[0])
			if err != nil {
				return fmt.Errorf("sms suppressions API: %w", err)
			}
			if resp.HTTPResponse.StatusCode != http.StatusNoContent {
				return apiError(resp.HTTPResponse.StatusCode, resp.Body)
			}
			fmt.Fprintf(opts.Stdout, "Removed %s from the opt-out list.\n", args[0])
			return nil
		},
	}
}
```

Register it: `cmd.AddCommand(newSmsSuppressionsCmd(opts))` alongside `newSmsStatusCmd`/`newSmsListCmd` in `newSmsCmd`. Verify the exact generated function/type names first (per Step 1) and use the real ones if they differ from the prediction above.

- [ ] **Step 3: Add SDK support**

In `sdk/hail/models.py`, add `SuppressionResponse`/`SuppressionListResponse` (mirroring the core schema field-for-field, no `hailhq.*` import). In `sdk/hail/client.py`'s `_SmsResource`, add:

```python
    async def list_suppressions(
        self, *, cursor: str | None = None, limit: int = 50
    ) -> SuppressionListResponse:
        params = {"limit": limit, "cursor": cursor}
        data = await self._http.request("GET", "/sms/suppressions", params=params)
        return SuppressionListResponse.model_validate(data)

    async def delete_suppression(self, number: str) -> None:
        await self._http.request("DELETE", f"/sms/suppressions/{number}")
```

- [ ] **Step 4: Write tests, run full suites**

Follow the exact test-writing conventions already established in Phase 1's Task 7 (Go, hitting a fake HTTP server) and Task 8 (Python, `respx`-mocked). Run `cd cli && go test ./... -v` and `cd sdk && uv run pytest -v`; expect all passing.

- [ ] **Step 5: Commit**

```bash
git add cli/internal/cmd/sms.go cli/internal/cmd/sms_test.go cli/internal/client/client.gen.go sdk/hail/models.py sdk/hail/client.py sdk/tests/test_sms.py openapi/openapi.yaml
git commit -m "feat(cli,sdk): add sms suppressions list/delete"
```

---

## Self-Review Notes

- **Spec coverage**: covers the "Inbound, webhooks & compliance" and "Abuse monitoring" sections of the design spec in full — signature verification, org resolution, opt-out (STOP/START), webhook fan-out, suppression API, and the abuse-monitoring guardrail with `ChannelSuspension`. Does NOT cover Numbers & Sender ID or Console UI (separate plans).
- **Placeholder scan**: abuse-monitoring thresholds are concrete numbers (not TBD), explicitly flagged in the spec and this plan's comments as a starting guess to tune post-launch — this is a documented design decision, not an unfinished placeholder.
- **Type consistency**: `remove_suppression`'s signature (`db, *, organization_id, recipient, channel`) matches `add_suppression`'s keyword shape exactly. `ingest_inbound_sms`'s `opt_out_type` parameter is a plain `str | None`, matching what Twilio's `OptOutType` form field actually is (not an enum) — consistent through `sms_ingest.py` and the route.

## Remaining Phases (not this plan)

1. **Numbers & Sender ID** — generic `/numbers` API, Sender ID resolution, Twilio Messaging Service.
2. **Console UI** (`hail-website`) — `/console/sms`, settings panels, monthly-fee billing.
3. **Docs & release** — `docs/setup/sms.md`, CHANGELOG, README, legal docs (partially already done by a parallel workstream — verify current state before duplicating).
