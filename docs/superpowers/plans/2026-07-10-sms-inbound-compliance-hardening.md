# SMS Inbound Compliance Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the four compliance gaps found in review — split inbound/outbound number FKs, add HELP handling with opt-in auto-replies, document the SMS webhook + Twilio opt-out setup, and draft consumer SMS legal disclosures.

**Architecture:** Four independently-shippable workstreams. Workstream 1 (schema) and 3 (docs) land first; Workstream 2 (replies) depends on both the number columns and a new config flag; Workstream 4 is a separate `hail-website` PR. All reply behavior is opt-in via a flag defaulting off, because Twilio auto-replies to STOP/HELP/START by default and Hail replies on top would double-text.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy 2 async, Pydantic v2, Alembic, pytest-asyncio, testcontainers Postgres.

## Global Constraints

- Design spec: `docs/superpowers/specs/2026-07-10-sms-inbound-compliance-hardening-design.md`.
- Test schema is built from the ORM models via `Base.metadata.create_all` (`core/conftest.py:21`) — **model changes drive tests; migrations are prod-only and must mirror the model exactly.**
- Current migration head is `0028`; the new migration is `0029` (`down_revision = "0028"`).
  - **RENUMBERED post-authoring (parallel-branch 0028 collision):** a concurrent branch took `0028` (org_provider_config). Their `0028` wins, so on this branch `0028_channel_suspensions` → `0029_channel_suspensions` and this task's `sms_to_number_id` → `0030_sms_to_number_id` (`revision "0030"`, `down_revision "0029"`). The `0029`/`0028` names below reflect the original authoring; the on-disk files are `0029`/`0030`. Migration tests fail standalone until this branch rebases onto the main that carries their `0028`.
- Compliance replies default **OFF**: `HAIL_SMS_COMPLIANCE_REPLIES_ENABLED` / `settings.hail_sms_compliance_replies_enabled: bool = False`.
- Suppression record (STOP/START) is written **unconditionally**, independent of the reply flag.
- Compliance replies **bypass** `check_sms_allowed`, write **no** `UsageEvent`, and require **no** funds check.
- `core/` must not import provider SDKs directly or depend on `api/`; the `SmsProvider` is injected into `ingest_inbound_sms` from the route.
- New env var → update `.env.example` in the same commit (repo invariant).
- Lint/format: `ruff` + `black` (via `uv run --with black black`), `gofmt` for Go. Run before every commit.
- Commit messages: Conventional Commits, imperative mood, **no** `Co-Authored-By` trailer.
- Do not run git writes automatically unless the user has said subagents may commit; otherwise surface the commit command for the user to run.

---

### Task 1: Split inbound/outbound number FKs (`to_number_id`)

**Files:**

- Modify: `core/hailhq/core/models.py` (class `Sms`, `from_number_id` ~line 465)
- Create: `api/migrations/versions/0029_sms_to_number_id.py`
- Modify: `core/hailhq/core/sms_ingest.py` (the `Sms(...)` construction, ~lines 100-119)
- Test: `core/tests/test_sms_ingest.py`, `core/tests/test_models.py`

**Interfaces:**

- Produces: `Sms.to_number_id: Mapped[uuid.UUID | None]`; `Sms.from_number_id` becomes nullable. Inbound rows: `from_number_id IS NULL`, `to_number_id = <org receiving number id>`. Outbound rows unchanged: `from_number_id = <sender id>`, `to_number_id IS NULL`.

- [ ] **Step 1: Write the failing test (inbound sets to_number_id, clears from_number_id)**

Add to `core/tests/test_sms_ingest.py`:

```python
async def test_ingest_sets_to_number_id_and_null_from_number_id(async_session) -> None:
    org_id = uuid.uuid4()
    number = await _seed_number(async_session, org_id)
    await async_session.commit()

    result = await ingest_inbound_sms(
        async_session,
        from_e164="+14155551234",
        to_e164="+14155559999",
        body="hi",
        provider_message_sid="SM_numfk",
        opt_out_type=None,
    )
    await async_session.commit()

    sms = (
        await async_session.execute(select(Sms).where(Sms.id == result.sms_id))
    ).scalar_one()
    assert sms.to_number_id == number.id
    assert sms.from_number_id is None
```

- [ ] **Step 2: Run it, verify it fails**

Run: `cd core && uv run pytest tests/test_sms_ingest.py::test_ingest_sets_to_number_id_and_null_from_number_id -q`
Expected: FAIL — `AttributeError: to_number_id` (column does not exist yet).

- [ ] **Step 3: Add the columns to the model**

In `core/hailhq/core/models.py`, class `Sms`, replace the `from_number_id` mapped_column and add `to_number_id` immediately after it:

```python
    from_number_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("phone_numbers.id"), nullable=True
    )
    to_number_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("phone_numbers.id"), nullable=True
    )
```

- [ ] **Step 4: Set the columns in inbound ingest**

In `core/hailhq/core/sms_ingest.py`, replace the number-id comment block and the `Sms(...)` construction so inbound stores the receiving number in `to_number_id` and leaves `from_number_id` NULL:

```python
    # Inbound: the external sender has no PhoneNumber row, so from_number_id is
    # NULL; the org's receiving number (Twilio `To`) is recorded in to_number_id.
    # Outbound sends do the mirror (from_number_id set, to_number_id NULL).
    sms = Sms(
        organization_id=organization_id,
        from_number_id=None,
        to_number_id=number.id,
        from_e164=from_e164,
        to_e164=to_e164,
        direction="inbound",
        status="received",
        body=body,
        provider_message_sid=sid,
    )
```

- [ ] **Step 5: Write the migration**

Create `api/migrations/versions/0029_sms_to_number_id.py`:

```python
"""sms.to_number_id + nullable from_number_id.

Inbound rows have no sending PhoneNumber (the sender is external), so
from_number_id becomes nullable and the org's receiving number is recorded
in the new to_number_id FK. Outbound rows are unchanged (from_number_id set,
to_number_id NULL). No backfill: existing outbound rows keep from_number_id
and leave to_number_id NULL.

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
    op.add_column(
        "sms",
        sa.Column(
            "to_number_id",
            UUID(as_uuid=True),
            sa.ForeignKey("phone_numbers.id"),
            nullable=True,
        ),
    )
    op.alter_column("sms", "from_number_id", existing_type=UUID(as_uuid=True), nullable=True)


def downgrade() -> None:
    # Restoring NOT NULL is safe only while no inbound (NULL from_number_id) rows exist.
    op.alter_column("sms", "from_number_id", existing_type=UUID(as_uuid=True), nullable=False)
    op.drop_column("sms", "to_number_id")
```

- [ ] **Step 6: Run the new test + full ingest/models suites**

Run: `cd core && uv run pytest tests/test_sms_ingest.py tests/test_models.py -q`
Expected: PASS (all, including the new test).

- [ ] **Step 7: Verify the migration applies + reverses on real Postgres**

Run: `docker compose -f docker-compose.yml -f docker-compose.local.yml up -d postgres && cd api && uv run alembic upgrade head && uv run alembic downgrade -1 && uv run alembic upgrade head`
Expected: no errors; `0029` is head.

- [ ] **Step 8: Lint + format**

Run: `cd core && uv run ruff check hailhq/core/models.py hailhq/core/sms_ingest.py && uv run --with black black core/hailhq/core/models.py core/hailhq/core/sms_ingest.py ../api/migrations/versions/0029_sms_to_number_id.py`

- [ ] **Step 9: Commit**

```bash
git add core/hailhq/core/models.py core/hailhq/core/sms_ingest.py api/migrations/versions/0029_sms_to_number_id.py core/tests/test_sms_ingest.py
git commit -m "feat(sms): add to_number_id FK and make from_number_id nullable for inbound"
```

---

### Task 2: Config — compliance-reply flag + templates

**Files:**

- Modify: `core/hailhq/core/config.py` (after the abuse settings, ~line 220)
- Modify: `.env.example` (after the abuse block, ~line 190)
- Test: `core/tests/test_config.py` (create if absent, else append)

**Interfaces:**

- Produces: `settings.hail_sms_compliance_replies_enabled: bool` (default `False`); `settings.hail_sms_stop_reply`, `settings.hail_sms_help_reply`, `settings.hail_sms_start_reply` (str defaults). Consumed by Task 3.

- [ ] **Step 1: Write the failing test**

Create/append `core/tests/test_config.py`:

```python
def test_compliance_reply_defaults() -> None:
    from hailhq.core.config import settings

    assert settings.hail_sms_compliance_replies_enabled is False
    assert "STOP" in settings.hail_sms_stop_reply
    assert "hi@hail.so" in settings.hail_sms_help_reply
    assert settings.hail_sms_start_reply
```

- [ ] **Step 2: Run it, verify it fails**

Run: `cd core && uv run pytest tests/test_config.py::test_compliance_reply_defaults -q`
Expected: FAIL — `AttributeError`.

- [ ] **Step 3: Add the settings**

In `core/hailhq/core/config.py`, immediately after `hail_abuse_monitor_poll_seconds`:

```python
    # SMS compliance auto-replies (HELP/STOP/START). OFF by default: Twilio's
    # own opt-out handling already auto-replies to these keywords, so enabling
    # Hail replies on top would double-text. Enable only when Twilio's default
    # filtering is disabled (account-wide Support ticket) or on a non-Twilio
    # provider. The suppression record is written regardless of this flag.
    hail_sms_compliance_replies_enabled: bool = False
    hail_sms_stop_reply: str = (
        "You are unsubscribed from Hail messages and will receive no more. "
        "Reply START to resubscribe. Help: hi@hail.so"
    )
    hail_sms_help_reply: str = (
        "Hail: for help contact hi@hail.so. Msg&data rates may apply. "
        "Reply STOP to unsubscribe."
    )
    hail_sms_start_reply: str = (
        "You are resubscribed to Hail messages. Reply STOP to unsubscribe, "
        "HELP for help."
    )
```

- [ ] **Step 4: Add to `.env.example`**

After the `HAIL_SMS_ABUSE_MAX_OPT_OUT_RATE=0.05` line:

```bash
# SMS compliance auto-replies (HELP/STOP/START). OFF by default — Twilio
# already auto-replies to these keywords, so turning this on without first
# disabling Twilio's own opt-out handling double-texts recipients. The STOP
# suppression record is written regardless.
HAIL_SMS_COMPLIANCE_REPLIES_ENABLED=false
```

- [ ] **Step 5: Run test + lint/format**

Run: `cd core && uv run pytest tests/test_config.py -q && uv run ruff check hailhq/core/config.py && uv run --with black black hailhq/core/config.py`
Expected: PASS, clean.

- [ ] **Step 6: Commit**

```bash
git add core/hailhq/core/config.py .env.example core/tests/test_config.py
git commit -m "feat(sms): add opt-in compliance-reply flag and templates"
```

---

### Task 3: HELP detection + compliance-reply dispatch

**Files:**

- Modify: `core/hailhq/core/sms_ingest.py` (keyword sets ~line 41, `_opt_out_action` ~line 47, `ingest_inbound_sms` signature + body)
- Modify: `api/hailhq/api/routes/sms.py` (`receive_inbound_sms`, ~line 249)
- Test: `core/tests/test_sms_ingest.py`, `api/tests/test_sms_inbound_api.py`

**Interfaces:**

- Consumes: `settings.hail_sms_*` (Task 2); `Sms.to_number_id`/nullable `from_number_id` (Task 1); `SmsProvider.send_sms(from_e164, to_e164, body) -> ProviderSmsResult`.
- Produces: `ingest_inbound_sms(..., provider: SmsProvider | None = None)`; `_opt_out_action` returns `"STOP" | "START" | "HELP" | None`.

- [ ] **Step 1: Write the failing tests (core)**

Add to `core/tests/test_sms_ingest.py`:

```python
async def test_help_keyword_sends_reply_when_enabled(async_session, monkeypatch) -> None:
    from unittest.mock import AsyncMock

    from hailhq.core import config
    from hailhq.core.providers.sms import ProviderSmsResult

    monkeypatch.setattr(config.settings, "hail_sms_compliance_replies_enabled", True)
    provider = AsyncMock()
    provider.send_sms.return_value = ProviderSmsResult(
        provider_message_sid="SM_reply", status="queued", segment_count=1
    )

    org_id = uuid.uuid4()
    await _seed_number(async_session, org_id)
    await async_session.commit()

    await ingest_inbound_sms(
        async_session,
        from_e164="+14155551234",
        to_e164="+14155559999",
        body="HELP",
        provider_message_sid="SM_help",
        opt_out_type=None,
        provider=provider,
    )
    await async_session.commit()

    provider.send_sms.assert_awaited_once()
    kwargs = provider.send_sms.await_args.kwargs
    assert kwargs["from_e164"] == "+14155559999"  # org receiving number
    assert kwargs["to_e164"] == "+14155551234"    # external sender


async def test_help_keyword_no_reply_when_disabled(async_session, monkeypatch) -> None:
    from unittest.mock import AsyncMock

    from hailhq.core import config

    monkeypatch.setattr(config.settings, "hail_sms_compliance_replies_enabled", False)
    provider = AsyncMock()

    org_id = uuid.uuid4()
    await _seed_number(async_session, org_id)
    await async_session.commit()

    await ingest_inbound_sms(
        async_session,
        from_e164="+14155551234",
        to_e164="+14155559999",
        body="HELP",
        provider_message_sid="SM_help2",
        opt_out_type=None,
        provider=provider,
    )
    provider.send_sms.assert_not_awaited()


async def test_stop_writes_suppression_regardless_of_reply_flag(async_session, monkeypatch) -> None:
    from hailhq.core import config
    from hailhq.core.models import Suppression

    monkeypatch.setattr(config.settings, "hail_sms_compliance_replies_enabled", False)

    org_id = uuid.uuid4()
    await _seed_number(async_session, org_id)
    await async_session.commit()

    await ingest_inbound_sms(
        async_session,
        from_e164="+14155551234",
        to_e164="+14155559999",
        body="STOP",
        provider_message_sid="SM_stop_flagoff",
        opt_out_type="STOP",
        provider=None,
    )
    await async_session.commit()

    hit = (
        await async_session.execute(
            select(Suppression).where(Suppression.recipient == "+14155551234")
        )
    ).scalar_one_or_none()
    assert hit is not None
```

- [ ] **Step 2: Run them, verify they fail**

Run: `cd core && uv run pytest tests/test_sms_ingest.py -q -k "help_keyword or regardless"`
Expected: FAIL — `ingest_inbound_sms() got an unexpected keyword argument 'provider'`.

- [ ] **Step 3: Add HELP keywords + action branch**

In `core/hailhq/core/sms_ingest.py`, add after `_START_KEYWORDS`:

```python
_HELP_KEYWORDS = frozenset({"HELP", "INFO"})
```

In `_opt_out_action`, add a HELP branch before the final `return None`:

```python
    if opt_out_type == "HELP" or keyword in _HELP_KEYWORDS:
        return "HELP"
    return None
```

- [ ] **Step 4: Add the reply helper + provider param + dispatch**

In `core/hailhq/core/sms_ingest.py`, add imports at top:

```python
from hailhq.core.config import settings
from hailhq.core.providers.sms import ProviderSmsResult, SmsProvider
```

Add the helper (module level):

```python
async def _send_compliance_reply(
    db: AsyncSession,
    provider: SmsProvider,
    *,
    org_number,  # PhoneNumber
    sender_e164: str,
    body: str,
) -> None:
    """Send a single carrier-mandated compliance reply and persist it as an
    outbound Sms row for audit. Bypasses check_sms_allowed / usage / funds.
    Failures are logged and swallowed so the webhook still returns 200."""
    try:
        result: ProviderSmsResult = await provider.send_sms(
            from_e164=org_number.e164, to_e164=sender_e164, body=body
        )
    except Exception:
        logger.exception("compliance reply send failed to %s", sender_e164)
        return
    db.add(
        Sms(
            organization_id=org_number.organization_id,
            from_number_id=org_number.id,
            to_number_id=None,
            from_e164=org_number.e164,
            to_e164=sender_e164,
            direction="outbound",
            status=result.status,
            body=body,
            provider=org_number.provider,
            provider_message_sid=result.provider_message_sid,
            segment_count=result.segment_count,
            error_code=result.error_code,
        )
    )
    await db.flush()
```

Change the `ingest_inbound_sms` signature to add `provider: SmsProvider | None = None` (last keyword param). Replace the STOP/START `action` block with STOP/START/HELP handling + gated reply dispatch:

```python
    action = _opt_out_action(body, opt_out_type)
    reply_body: str | None = None
    if action == "STOP":
        await add_suppression(
            db,
            organization_id=organization_id,
            recipient=from_e164,
            channel="sms",
            reason="recipient replied STOP",
            source="stop_keyword",
        )
        reply_body = settings.hail_sms_stop_reply
    elif action == "START":
        await remove_suppression(
            db, organization_id=organization_id, recipient=from_e164, channel="sms"
        )
        reply_body = settings.hail_sms_start_reply
    elif action == "HELP":
        reply_body = settings.hail_sms_help_reply

    if (
        reply_body is not None
        and provider is not None
        and settings.hail_sms_compliance_replies_enabled
    ):
        await _send_compliance_reply(
            db, provider, org_number=number, sender_e164=from_e164, body=reply_body
        )
```

(Keep the existing `SmsEvent` write and `fanout_sms_event` call after this block.)

- [ ] **Step 5: Inject the provider in the route**

In `api/hailhq/api/routes/sms.py`, `receive_inbound_sms`: add the provider dependency and pass it to ingest:

```python
async def receive_inbound_sms(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
    provider: Annotated[SmsProvider, Depends(get_sms_provider)],
) -> Response:
    ...
    await ingest_inbound_sms(
        db,
        from_e164=params.get("From", ""),
        to_e164=params.get("To", ""),
        body=params.get("Body", ""),
        provider_message_sid=params.get("MessageSid") or None,
        opt_out_type=params.get("OptOutType"),
        provider=provider,
    )
```

- [ ] **Step 6: Add an API-level test (reply flows end to end through the route)**

Add to `api/tests/test_sms_inbound_api.py`:

```python
async def test_inbound_help_sends_reply_when_enabled(
    client, async_session, monkeypatch, sms_mock
) -> None:
    from hailhq.core.config import settings

    monkeypatch.setattr(settings, "twilio_auth_token", AUTH_TOKEN)
    monkeypatch.setattr(settings, "hail_api_url", "http://t")
    monkeypatch.setattr(settings, "hail_sms_compliance_replies_enabled", True)
    await _seed_number(async_session, uuid.uuid4())

    params = {"From": "+14155551234", "To": "+14155559999", "Body": "HELP", "MessageSid": "SM_help_api"}
    form, headers = _signed_form(params)
    resp = await client.post("/sms/inbound", data=form, headers=headers)
    assert resp.status_code == 200
    sms_mock.send_sms.assert_awaited()
```

- [ ] **Step 7: Run the tests**

Run: `cd core && uv run pytest tests/test_sms_ingest.py -q && cd ../api && uv run pytest tests/test_sms_inbound_api.py -q`
Expected: PASS.

- [ ] **Step 8: Lint + format**

Run: `cd core && uv run ruff check hailhq/core/sms_ingest.py ../api/hailhq/api/routes/sms.py && uv run --with black black hailhq/core/sms_ingest.py ../api/hailhq/api/routes/sms.py`

- [ ] **Step 9: Commit**

```bash
git add core/hailhq/core/sms_ingest.py api/hailhq/api/routes/sms.py core/tests/test_sms_ingest.py api/tests/test_sms_inbound_api.py
git commit -m "feat(sms): handle HELP and send opt-in STOP/START/HELP compliance replies"
```

---

### Task 4: Docs — sms.received webhook + Twilio opt-out setup

**Files:**

- Modify: `docs/setup/webhooks.md` (Event types + Payload sections)
- Modify: `docs/setup/twilio.md` (new inbound-SMS section)

**Interfaces:** none (docs only).

- [ ] **Step 1: Add `sms.received` to the webhooks event catalog**

In `docs/setup/webhooks.md`, under `## Event types`, after the `email.*` bullets:

```markdown
- **`sms.received`** — an inbound SMS arrived and was accepted. Delivered through
  the same signed, retried webhook worker as the email events (`X-Hail-Signature`,
  `X-Hail-Event`, `X-Hail-Delivery`); the `X-Hail-Email-Domain` header is omitted.
```

Under `## Payload`, after the email inbound example, add:

````markdown
**Inbound example** (`sms.received`):

```json
{
  "id": "7a1b…",
  "type": "sms.received",
  "api_version": "2026-06-06",
  "created_at": "2026-07-10T12:00:00+00:00",
  "organization_id": "org-uuid",
  "data": {
    "id": "sms-uuid",
    "from": "+14155551234",
    "to": "+14155559999",
    "body": "hello back"
  }
}
```
````

- [ ] **Step 2: Document the inbound-SMS + opt-out setup in `twilio.md`**

Append to `docs/setup/twilio.md`:

```markdown
## 4. Inbound SMS & opt-out

Point the number's **A Message Comes In** webhook at
`https://<your-api-host>/sms/inbound` (HTTP POST). Hail verifies Twilio's
`X-Twilio-Signature` against `HAIL_API_URL`, so that value must match the public
URL Twilio posts to.

**Recognized keywords** (matched on the message body, case-insensitive):

- Opt out (STOP): `STOP`, `STOPALL`, `UNSUBSCRIBE`, `CANCEL`, `END`, `QUIT`
- Opt in (START): `START`, `YES`, `UNSTOP`
- Help: `HELP`, `INFO`

Hail records opt-outs in its own suppression list (checked before every send)
regardless of Twilio configuration.

**Opt-out replies:** By default **Twilio** auto-replies to STOP/HELP/START and
carrier-blocks opted-out numbers. Leave `HAIL_SMS_COMPLIANCE_REPLIES_ENABLED=false`
in that setup. To have **Hail** own the replies (e.g. a non-Twilio provider, or a
custom keyword experience), first disable Twilio's default opt-out handling — this
is **account-wide and requires a Twilio Support request; there is no API for it** —
then set `HAIL_SMS_COMPLIANCE_REPLIES_ENABLED=true`.
```

- [ ] **Step 3: Prettier-format the docs**

Run: `pnpm exec prettier --write docs/setup/webhooks.md docs/setup/twilio.md` (skip if prettier is unavailable in the environment; the edits already match house style).

- [ ] **Step 4: Commit**

```bash
git add docs/setup/webhooks.md docs/setup/twilio.md
git commit -m "docs(sms): document sms.received webhook and Twilio opt-out setup"
```

---

### Task 5: Legal `sms.md` disclosures (hail-website repo)

**Files:**

- Create: `~/hail-website/content/legal/sms.md` (separate repo — its own branch/PR)

**Interfaces:** none.

> This task ships in the `hail-website` repo, not `hail`. Do not stage it in the `hail` commit. Draft copy is **placeholder pending legal sign-off** — do not treat as final legal text.

- [ ] **Step 1: Draft the page**

Create `~/hail-website/content/legal/sms.md`:

```markdown
---
title: SMS Terms & Opt-Out
description: How Hail SMS messaging, opt-out, and help keywords work.
---

<!-- DRAFT — requires legal sign-off before publishing. -->

# SMS messaging terms

Hail sends and receives SMS on behalf of the products that integrate it. By
providing your number to one of those products you consent to receive recurring
automated text messages related to the service you interact with. **Message
frequency varies. Message and data rates may apply.**

## Opting out

Reply **STOP** (or `STOPALL`, `UNSUBSCRIBE`, `CANCEL`, `END`, `QUIT`) to any
message to opt out. You will be added to a platform-wide do-not-contact list and
will receive no further messages except a single confirmation. Reply **START**
(or `YES`, `UNSTOP`) to resubscribe.

## Getting help

Reply **HELP** (or `INFO`) for assistance, or contact
[hi@hail.so](mailto:hi@hail.so).

## Your data

Opt-out state is stored on Hail's platform and honored across every product built
on Hail. See our [Privacy Policy](/legal/privacy) and
[Acceptable Use Policy](/legal/aup) for how messaging data is handled.
```

- [ ] **Step 2: Link from the AUP opt-out section**

In `~/hail-website/content/legal/aup.md` §5 (opt-out), add a reference:

```markdown
See our [SMS Terms & Opt-Out](/legal/sms) for the SMS-specific keywords and disclosures.
```

- [ ] **Step 3: Commit (in hail-website)**

```bash
cd ~/hail-website
git add content/legal/sms.md content/legal/aup.md
git commit -m "docs(legal): draft SMS terms and opt-out disclosures"
```

Then flag for human/legal review before merging.

---

## Self-Review

- **Spec coverage:** WS1 → Task 1; WS2 → Tasks 2-3 (flag + templates + HELP + replies, default off, provider-injected, persisted outbound rows, no usage event); WS3 → Task 4; WS4 → Task 5. Out-of-scope items (delivery-status callbacks, un-suspend tooling) correctly absent.
- **Placeholder scan:** all steps carry real code/commands; the only intentional "DRAFT" marker is the legal copy, which the spec requires to be review-flagged.
- **Type consistency:** `ingest_inbound_sms(..., provider: SmsProvider | None = None)` used identically in Task 3 core + route; `_opt_out_action` returns `STOP|START|HELP|None` consistently; `to_number_id` naming matches across model, migration, ingest, and tests; reply helper uses `ProviderSmsResult` fields (`status`, `provider_message_sid`, `segment_count`, `error_code`) exactly as defined in `providers/sms/base.py`.
