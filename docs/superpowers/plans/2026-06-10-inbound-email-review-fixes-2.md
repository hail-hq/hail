# Inbound Email — Review Fixes Round 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Git policy for this repo:** Claude and subagents NEVER run git write commands (no `add`/`commit`/`stash`). "Commit checkpoint" steps mean: report the suggested commit message to the user; the user commits manually. Verification commands (pytest, go test, etc.) ARE run by the executor.

**Goal:** Fix all 5 critical, 7 important, and ~15 minor findings from the 2026-06-10 code review of the uncommitted inbound-email work.

**Architecture:** The ingest pipeline gains created-row gating (no duplicate side effects on SES redelivery), body coalescing, and a global hail-mail uniqueness index. Forwarding becomes functional end-to-end via a new `OutboundForwardWorker` (mirrors `WebhookWorker`) plus `headers`/`attachments` support on `EmailProvider.send_email` (SESv2 Raw content). The API degrades gracefully when `HAIL_WEBHOOK_SECRET_KEY` is unset. Suppression events go on the wire per spec §6.2/§7.

**Tech stack:** FastAPI + SQLAlchemy async + Alembic (api/, core/), boto3 SESv2 via `asyncio.to_thread`, Go/Cobra CLI, Terraform.

**Review-finding → task map:**

| Finding                                                       | Task  |
| ------------------------------------------------------------- | ----- |
| C1 forwarding never sends (+ headers/attachments unreachable) | 5, 6  |
| C2 redelivery duplicates side effects                         | 1     |
| C3 body-less mail silently destroyed                          | 2     |
| C4 cross-tenant hail-mail interception                        | 3     |
| C5 API won't boot without HAIL_WEBHOOK_SECRET_KEY             | 4     |
| I1 suppressed/bounced events never fire (+ reason rename)     | 8     |
| I2 org cap doesn't gate forwarding                            | 7     |
| I3 IPv6 SSRF bypass (+ blocking DNS)                          | 9     |
| I4 mixed-case local parts dropped                             | 10    |
| I5 dead 409 branch in PATCH                                   | 11    |
| I6 SDK EmailDomainPatch stale                                 | 12    |
| I7 missing redeliver / CLI webhooks tests                     | 13    |
| Minors (each named in its task)                               | 14–22 |

---

### Task 1: Gate forwarding + fan-out on row creation (C2)

SES delivery is at-least-once. `_persist_one` must tell the caller whether the row was created or already existed; side effects fire only on creation.

**Files:**

- Modify: `core/hailhq/core/email_ingest.py`
- Test: `core/tests/test_email_ingest.py`

- [ ] **Step 1: Write the failing test** — append to `core/tests/test_email_ingest.py` (reuse the module's `_make_inbound_domain` helper and `FIX` path; mirror the existing `test_ingest_persists_inbound_row_with_attachment` setup):

```python
@pytest.mark.asyncio
async def test_replay_does_not_refire_forwards_or_fanout(async_session):
    org_id = uuid.uuid4()
    domain = _make_inbound_domain(org_id)
    domain.inbound_enabled = True
    domain.forward_to = ["ops@example.com"]
    async_session.add(domain)
    await async_session.commit()

    msg = InboundMessage(
        provider_message_id="replay-1",
        envelope_from="alice@example.com",
        envelope_recipients=[domain.domain],
        raw_s3_bucket="b",
        raw_s3_key="raw/replay-1",
        spam_verdict="PASS",
        virus_verdict="PASS",
        spf_verdict="PASS",
        dkim_verdict="PASS",
        dmarc_verdict="PASS",
        received_at=datetime(2026, 6, 6, tzinfo=timezone.utc),
    )
    s3 = AsyncMock()
    s3.fetch_raw.return_value = (FIX / "simple.eml").read_bytes()
    forward_enqueue = AsyncMock()
    fanout = AsyncMock(return_value=1)

    for _ in range(2):  # initial delivery + SES redelivery
        await ingest_inbound(
            async_session,
            message=msg,
            s3=s3,
            hail_mail_base_domain="mail.hail.so",
            forward_enqueue=forward_enqueue,
            fanout=fanout,
            org_rate_per_hour=10_000,
        )

    assert forward_enqueue.await_count == 1
    assert fanout.await_count == 1
```

- [ ] **Step 2: Run it** — `cd core && uv run pytest tests/test_email_ingest.py::test_replay_does_not_refire_forwards_or_fanout -v` → expect FAIL (counts are 2).

- [ ] **Step 3: Implement.** In `core/hailhq/core/email_ingest.py`:

Change `_persist_one`'s signature/return (currently returns `UUID | None`) to return `tuple[UUID | None, bool]` — `(email_id, created)`:

```python
async def _persist_one(
    db: AsyncSession,
    *,
    parsed: ParsedMime,
    message: InboundMessage,
    domain: EmailDomain,
    suppress: str | None,
    s3: S3InboundClient,
) -> tuple[UUID | None, bool]:
```

Return sites:

- early short-circuit (`existing_id is not None`): `return existing_id, False`
- IntegrityError dedupe path: `return existing_id, False`
- success tail: `return email.id, True`

In `ingest_inbound`, change the call site:

```python
        email_id, created = await _persist_one(
            db,
            parsed=parsed,
            message=message,
            domain=domain,
            suppress=suppress,
            s3=s3,
        )
        if email_id is None:
            continue
        result.email_ids.append(email_id)
```

and add `created` to both side-effect guards:

```python
        if (
            created
            and suppress is None
            and forward_enqueue is not None
            and domain.inbound_enabled
        ):
```

```python
        if created and suppress is None and fanout is not None and not over_cap:
```

- [ ] **Step 4: Run** — same test → PASS. Then full ingest suites: `cd core && uv run pytest tests/test_email_ingest.py -v` and `cd api && uv run pytest tests/test_internal_ses_events.py tests/test_internal_ses_events_multi_org.py -v`. Fix any test that imported `_persist_one` directly (unpack the new tuple).

- [ ] **Step 5: Commit checkpoint (user).** `fix(core): gate inbound forward/fan-out on row creation so SES redelivery is side-effect idempotent`

---

### Task 2: Stop destroying body-less inbound mail; narrow the IntegrityError catch (C3)

**Files:**

- Modify: `core/hailhq/core/email_ingest.py`
- Test: `core/tests/test_email_ingest.py`, new fixture `core/tests/fixtures/inbound/attachment_only.eml`

- [ ] **Step 1: Create the fixture** `core/tests/fixtures/inbound/attachment_only.eml` (attachment-only, no text/html parts):

```
From: alice@example.com
To: alice+acme@mail.hail.so
Subject: invoice attached
Message-ID: <attonly@example.com>
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="b1"

--b1
Content-Type: application/pdf
Content-Disposition: attachment; filename="invoice.pdf"
Content-Transfer-Encoding: base64

JVBERi0xLjQK
--b1--
```

- [ ] **Step 2: Write the failing tests** — append to `core/tests/test_email_ingest.py`:

```python
@pytest.mark.asyncio
async def test_attachment_only_mail_is_persisted_not_destroyed(async_session):
    org_id = uuid.uuid4()
    domain = _make_inbound_domain(org_id)
    async_session.add(domain)
    await async_session.commit()

    msg = InboundMessage(
        provider_message_id="attonly-1",
        envelope_from="alice@example.com",
        envelope_recipients=[domain.domain],
        raw_s3_bucket="b",
        raw_s3_key="raw/attonly-1",
        spam_verdict="PASS",
        virus_verdict="PASS",
        spf_verdict="PASS",
        dkim_verdict="PASS",
        dmarc_verdict="PASS",
        received_at=datetime(2026, 6, 6, tzinfo=timezone.utc),
    )
    s3 = AsyncMock()
    s3.fetch_raw.return_value = (FIX / "attachment_only.eml").read_bytes()

    result = await ingest_inbound(
        async_session,
        message=msg,
        s3=s3,
        hail_mail_base_domain="mail.hail.so",
        org_rate_per_hour=10_000,
    )
    assert len(result.email_ids) == 1
    email = (
        await async_session.execute(
            select(Email).where(Email.id == result.email_ids[0])
        )
    ).scalar_one()
    assert email.body_text == ""  # coalesced, not lost
    assert email.body_html is None


@pytest.mark.asyncio
async def test_unrelated_integrity_error_is_not_swallowed(async_session):
    """A non-dedupe constraint violation must propagate, not 200-skip."""
    from sqlalchemy.exc import IntegrityError as SAIntegrityError

    org_id = uuid.uuid4()
    domain = _make_inbound_domain(org_id)
    async_session.add(domain)
    await async_session.commit()

    msg = InboundMessage(
        provider_message_id="fk-violation-1",
        envelope_from="alice@example.com",
        envelope_recipients=[domain.domain],
        raw_s3_bucket="b",
        raw_s3_key="raw/fk-violation-1",
        spam_verdict="PASS",
        virus_verdict="PASS",
        spf_verdict="PASS",
        dkim_verdict="PASS",
        dmarc_verdict="PASS",
        received_at=datetime(2026, 6, 6, tzinfo=timezone.utc),
    )
    s3 = AsyncMock()
    s3.fetch_raw.return_value = (FIX / "simple.eml").read_bytes()

    # Delete the domain row after lookup would have happened: simulate by
    # pointing the FK at a random UUID via a monkeypatched domain object.
    domain.id = uuid.uuid4()  # detached id → FK violation on flush

    with pytest.raises(SAIntegrityError):
        await ingest_inbound(
            async_session,
            message=msg,
            s3=s3,
            hail_mail_base_domain="mail.hail.so",
            org_rate_per_hour=10_000,
        )
```

(If the FK-violation setup proves flaky under the session's identity map, replace it with a direct `_persist_one` call passing a `domain` object whose `id`/`organization_id` don't exist — the assertion that matters is `pytest.raises(IntegrityError)` instead of a silent skip.)

- [ ] **Step 3: Run** — both tests FAIL (first: empty result; second: no raise).

- [ ] **Step 4: Implement** in `_persist_one`:

Body coalescing — replace the two body kwargs in the `Email(...)` constructor:

```python
    # Real mail can be body-less (attachment-only, calendar-only). The
    # emails_body_required CHECK predates inbound; coalesce to "" rather
    # than dropping the message.
    body_text = parsed.body_text
    if body_text is None and parsed.body_html is None:
        body_text = ""
```

and use `body_text=body_text` in the constructor (keep `body_html=parsed.body_html`).

Narrow the except (only the dedupe index is a benign race; everything else must propagate):

```python
    except IntegrityError as exc:
        await nested.rollback()
        if "emails_inbound_message_id_uq" not in str(exc.orig):
            raise
        # A concurrent delivery won the race on emails_inbound_message_id_uq.
        existing_id = await _existing_inbound_id(
            db, domain.organization_id, parsed.message_id
        )
        return existing_id, False
```

(Note: Task 14 widens this check to also accept the new provider-message-id dedupe index.)

- [ ] **Step 5: Run** — both tests PASS; full `core/tests/test_email_ingest.py` and `api/tests/test_internal_ses_events*.py` green.

- [ ] **Step 6: Commit checkpoint (user).** `fix(core): persist body-less inbound mail; only swallow the dedupe IntegrityError`

---

### Task 3: Global hail-mail address uniqueness (C4)

Org B must not be able to register org A's exact `user+org@mail.hail.so` address. `domain` holds the full address for `kind='hail_mail'` rows, so a partial unique index on `domain WHERE kind='hail_mail'` closes the hole.

**Files:**

- Create: `api/migrations/versions/0010_hail_mail_global_unique.py`
- Modify: `core/hailhq/core/models.py` (EmailDomain `__table_args__`), `api/hailhq/api/routes/email_domains.py` (409 messages)
- Test: `api/tests/test_email_domains_api.py`, `api/tests/test_migrations.py`

- [ ] **Step 1: Write the failing API test** — append to `api/tests/test_email_domains_api.py` (mirror that module's existing two-org setup helpers; it already creates orgs/keys for cross-org isolation tests):

```python
@pytest.mark.asyncio
async def test_hail_mail_address_is_globally_unique(
    client, async_session, monkeypatch
):
    """Two different orgs may NOT register the same hail-mail address."""
    monkeypatch.setattr(settings, "hail_mail_base_domain", "mail.hail.so")
    org_a_headers = await _auth_headers_for_new_org(client, async_session)
    org_b_headers = await _auth_headers_for_new_org(client, async_session)

    body = {
        "kind": "hail_mail",
        "local_prefix_user": "alice",
        "local_prefix_org": "acme",
    }
    r1 = await client.post("/email-domains", json=body, headers=org_a_headers)
    assert r1.status_code == 201
    r2 = await client.post("/email-domains", json=body, headers=org_b_headers)
    assert r2.status_code == 409
```

Use the file's existing fixture/helper for minting an org + API key (read the top of `test_email_domains_api.py` and reuse it; if it's a fixture like `org_and_key`, parametrize two instances the way `test_internal_ses_events_multi_org.py` does). The assertion pair 201/409 across **different** orgs is the point.

- [ ] **Step 2: Run it** — FAIL (`r2.status_code == 201` today).

- [ ] **Step 3: Add the model index** — in `core/hailhq/core/models.py`, append to `EmailDomain.__table_args__` (after the `UniqueConstraint`):

```python
        # Hail-mail addresses route inbound mail by (user, org) prefix with no
        # org scoping at lookup time — the full address must be globally
        # unique or org B could register org A's address and intercept mail.
        Index(
            "email_domains_hail_mail_domain_uq",
            "domain",
            unique=True,
            postgresql_where=text("kind = 'hail_mail'"),
        ),
```

- [ ] **Step 4: Write migration** `api/migrations/versions/0010_hail_mail_global_unique.py` (mirror the header/style of `0009_provider_message_id_partial_unique.py` for `revision`/`down_revision` chaining — `down_revision = "0009"` or whatever identifier 0009 uses):

```python
"""Global uniqueness for hail-mail addresses.

Inbound routing looks up email_domains by (local_prefix_user,
local_prefix_org) with NO org scoping — without this index two orgs can
hold the same hail-mail address and one intercepts the other's mail.

Upgrade fails if duplicate hail_mail `domain` values already exist;
0006-0010 ship unreleased together, so no production data predates this.
"""

from alembic import op
import sqlalchemy as sa

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "email_domains_hail_mail_domain_uq",
        "email_domains",
        ["domain"],
        unique=True,
        postgresql_where=sa.text("kind = 'hail_mail'"),
    )


def downgrade() -> None:
    op.drop_index("email_domains_hail_mail_domain_uq", table_name="email_domains")
```

Add a schema assertion to `api/tests/test_migrations.py` mirroring the existing 0009 index assertion (same inspection pattern: upgrade head → assert index exists with `unique=True`).

- [ ] **Step 5: Fix the 409 messages** in `api/hailhq/api/routes/email_domains.py` — the conflict can now be cross-org, so drop the "for this organization" claim:

Create path (`create_email_domain`, hail_mail branch, ~line 233):

```python
            raise HTTPException(
                status_code=http_status.HTTP_409_CONFLICT,
                detail=f"hail-mail address {address!r} is already registered",
            ) from exc
```

PATCH conflict path (~line 469, the prefix-edit 409 — Task 11 fixes the dead guard around it):

```python
                detail=(
                    f"hail-mail address {updates.get('domain')!r} is already registered"
                ),
```

- [ ] **Step 6: Run** — Step 1 test PASSES; `cd api && uv run pytest tests/test_email_domains_api.py tests/test_migrations.py -v` green; `cd api && uv run alembic upgrade head` succeeds against the dev DB.

- [ ] **Step 7: Commit checkpoint (user).** `fix(api): enforce global uniqueness of hail-mail addresses (cross-tenant interception)`

---

### Task 4: Boot without HAIL_WEBHOOK_SECRET_KEY; 503 on webhook routes (C5)

**Files:**

- Modify: `api/hailhq/api/main.py`
- Test: `api/tests/test_webhooks_api.py` (or new `api/tests/test_lifespan.py`)

- [ ] **Step 1: Write the failing tests** — new file `api/tests/test_lifespan.py`:

```python
"""Boot resilience: a deployment without HAIL_WEBHOOK_SECRET_KEY must start."""

from __future__ import annotations

import pytest

from hailhq.api.main import app, lifespan
from hailhq.core.config import settings


@pytest.mark.asyncio
async def test_lifespan_starts_without_webhook_secret_key(monkeypatch):
    monkeypatch.setattr(settings, "hail_webhook_secret_key", "")
    async with lifespan(app):
        pass  # boot + teardown without raising


@pytest.mark.asyncio
async def test_webhook_routes_503_without_secret_key(client, monkeypatch):
    monkeypatch.setattr(settings, "hail_webhook_secret_key", "")
    resp = await client.post(
        "/webhooks",
        json={"target_url": "https://example.com/hook", "event_types": ["email.received"]},
    )
    assert resp.status_code == 503
```

(The `client` fixture in `api/tests/conftest.py` is authenticated; reuse it as the other webhooks tests do.)

- [ ] **Step 2: Run** — first test FAILS (`SecretKeyMissing` raised in lifespan), second FAILS (500).

- [ ] **Step 3: Implement** in `api/hailhq/api/main.py`:

Add imports:

```python
from fastapi import Request
from fastapi.responses import JSONResponse
from hailhq.core.secret_cipher import SecretCipher, SecretKeyMissing
```

Replace the unconditional cipher/worker block in `lifespan`:

```python
    webhook_worker: WebhookWorker | None = None
    webhook_task: asyncio.Task | None = None
    try:
        cipher = SecretCipher(settings.hail_webhook_secret_key)
    except SecretKeyMissing:
        logger.warning(
            "HAIL_WEBHOOK_SECRET_KEY is unset or invalid; webhook delivery "
            "worker disabled and webhook routes will return 503. Generate a "
            "key with: python -c \"from hailhq.core.secret_cipher import "
            "generate_key; print(generate_key())\""
        )
    else:
        webhook_worker = WebhookWorker(
            session_factory=session_scope,
            http_post=partial(
                httpx_post,
                allow_private_networks=settings.hail_webhook_allow_private_networks,
            ),
            decrypt=cipher.decrypt,
        )
        webhook_task = asyncio.create_task(
            webhook_worker.run_forever(), name="webhook-worker"
        )
```

and guard the teardown:

```python
        if webhook_worker is not None and webhook_task is not None:
            await webhook_worker.stop()
            try:
                await asyncio.wait_for(webhook_task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                webhook_task.cancel()
```

Add an exception handler after `app = FastAPI(...)` so route-level `SecretCipher(...)` constructions surface as 503 (routes in `webhooks.py` and `email_domains.py` construct it lazily):

```python
@app.exception_handler(SecretKeyMissing)
async def _secret_key_missing(_request: Request, exc: SecretKeyMissing) -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": str(exc)})
```

- [ ] **Step 4: Run** — both tests PASS; full `api/tests/test_webhooks_api.py` still green (it monkeypatches a real key).

- [ ] **Step 5: Commit checkpoint (user).** `fix(api): boot without HAIL_WEBHOOK_SECRET_KEY; webhook routes 503 instead of crash/500`

---

### Task 5: `headers` + `attachments` on EmailProvider.send_email (C1 part 1)

SESv2 Simple content supports a `Headers` list; attachments require Raw MIME. Also fix `build_forwarded` gaps surfaced by the review: missing `References` header, body-less forwards violating `emails_body_required`, HTML preamble.

**Files:**

- Modify: `core/hailhq/core/providers/email/base.py`, `core/hailhq/core/providers/email/ses.py`, `core/hailhq/core/email_forwarding.py`
- Test: `core/tests/providers/test_ses_email.py` (or wherever the existing Stubber-based SES tests live — `ls core/tests/providers/` first and append to the existing module), `core/tests/test_email_forwarding.py`

- [ ] **Step 1: Write failing provider tests** — append to the existing SES provider test module (mirror its Stubber setup):

```python
@pytest.mark.asyncio
async def test_send_email_with_headers_uses_simple_headers():
    client = botocore.session.get_session().create_client(
        "sesv2", region_name="us-east-1",
        aws_access_key_id="x", aws_secret_access_key="y",
    )
    stubber = Stubber(client)
    stubber.add_response(
        "send_email",
        {"MessageId": "m-1"},
        {
            "FromEmailAddress": "forwarder+acme@mail.hail.so",
            "Destination": {"ToAddresses": ["ops@example.com"]},
            "Content": {
                "Simple": {
                    "Subject": {"Data": "Fwd: hi", "Charset": "UTF-8"},
                    "Body": {"Text": {"Data": "x", "Charset": "UTF-8"}},
                    "Headers": [
                        {"Name": "X-Hail-Forward-Hops", "Value": "1"},
                        {"Name": "Auto-Submitted", "Value": "auto-forwarded"},
                    ],
                }
            },
        },
    )
    provider = SesEmailProvider(client=client)
    with stubber:
        result = await provider.send_email(
            from_address="forwarder+acme@mail.hail.so",
            to_addresses=["ops@example.com"],
            subject="Fwd: hi",
            body_text="x",
            body_html=None,
            headers={
                "X-Hail-Forward-Hops": "1",
                "Auto-Submitted": "auto-forwarded",
            },
        )
    assert result.provider_message_id == "m-1"


@pytest.mark.asyncio
async def test_send_email_with_attachments_uses_raw_mime():
    client = botocore.session.get_session().create_client(
        "sesv2", region_name="us-east-1",
        aws_access_key_id="x", aws_secret_access_key="y",
    )
    stubber = Stubber(client)
    stubber.add_response("send_email", {"MessageId": "m-2"}, None)  # skip param match
    provider = SesEmailProvider(client=client)
    with stubber:
        result = await provider.send_email(
            from_address="forwarder+acme@mail.hail.so",
            to_addresses=["ops@example.com"],
            subject="Fwd: invoice",
            body_text="see attached",
            body_html=None,
            headers={"X-Hail-Forward-Hops": "1"},
            attachments=[
                ProviderAttachment(
                    filename="invoice.pdf",
                    content_type="application/pdf",
                    payload=b"%PDF-1.4",
                )
            ],
        )
    assert result.provider_message_id == "m-2"
```

For the Raw test, if `None` expected-params isn't supported by the Stubber version, capture kwargs with a fake client instead (a small class recording `send_email(**kwargs)` and returning `{"MessageId": "m-2"}`), then assert `"Raw" in kwargs["Content"]` and that `b"invoice.pdf" in kwargs["Content"]["Raw"]["Data"]` and `b"X-Hail-Forward-Hops"` appears.

- [ ] **Step 2: Run** — FAIL (`send_email() got an unexpected keyword argument 'headers'`).

- [ ] **Step 3: Implement base** — in `core/hailhq/core/providers/email/base.py`, add the model and extend the signature:

```python
class ProviderAttachment(BaseModel):
    """One file to attach on send. Payload is raw bytes (already fetched)."""

    filename: str
    content_type: str
    payload: bytes
```

Add `"ProviderAttachment"` to `__all__`. Extend the abstract `send_email` signature (both in the ABC and its docstring):

```python
    async def send_email(
        self,
        *,
        from_address: str,
        to_addresses: list[str],
        subject: str,
        body_text: str | None,
        body_html: str | None,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        reply_to: str | None = None,
        headers: dict[str, str] | None = None,
        attachments: list[ProviderAttachment] | None = None,
    ) -> ProviderSendResult:
        """Send one message. Implementations must raise on provider error.

        ``headers`` are extra top-level headers (loop-prevention,
        Auto-Submitted, References). ``attachments`` forces the raw-MIME
        path on providers whose simple-content API can't carry files.
        """
```

- [ ] **Step 4: Implement SES** — in `core/hailhq/core/providers/email/ses.py`:

Add imports at top:

```python
from email.message import EmailMessage

from hailhq.core.providers.email.base import (
    DkimRecord,
    EmailProvider,
    IdentityVerificationStatus,
    ProviderAttachment,
    ProviderIdentity,
    ProviderSendResult,
)
```

Add a module-level helper:

```python
def _build_raw_mime(
    *,
    from_address: str,
    to_addresses: list[str],
    subject: str,
    body_text: str | None,
    body_html: str | None,
    cc: list[str] | None,
    reply_to: str | None,
    headers: dict[str, str],
    attachments: list[ProviderAttachment],
) -> bytes:
    msg = EmailMessage()
    msg["From"] = from_address
    msg["To"] = ", ".join(to_addresses)
    if cc:
        msg["Cc"] = ", ".join(cc)
    if reply_to:
        msg["Reply-To"] = reply_to
    msg["Subject"] = subject
    for name, value in headers.items():
        if value:
            msg[name] = value
    if body_text is not None:
        msg.set_content(body_text)
        if body_html is not None:
            msg.add_alternative(body_html, subtype="html")
    elif body_html is not None:
        msg.set_content(body_html, subtype="html")
    for att in attachments:
        maintype, _, subtype = att.content_type.partition("/")
        msg.add_attachment(
            att.payload,
            maintype=maintype or "application",
            subtype=subtype or "octet-stream",
            filename=att.filename,
        )
    return msg.as_bytes()
```

Extend `SesEmailProvider.send_email` — new params `headers: dict[str, str] | None = None, attachments: list[ProviderAttachment] | None = None`; after the existing `destination`/`body`/`message` construction, branch before building `kwargs`:

```python
        if attachments:
            raw = _build_raw_mime(
                from_address=from_address,
                to_addresses=to_addresses,
                subject=subject,
                body_text=body_text,
                body_html=body_html,
                cc=cc,
                reply_to=reply_to,
                headers={k: v for k, v in (headers or {}).items() if v},
                attachments=attachments,
            )
            kwargs = {
                "FromEmailAddress": from_address,
                "Destination": destination,
                "Content": {"Raw": {"Data": raw}},
            }
            response = await asyncio.to_thread(self._client.send_email, **kwargs)
            return ProviderSendResult(provider_message_id=response["MessageId"])

        if headers:
            message["Headers"] = [
                {"Name": k, "Value": v} for k, v in headers.items() if v
            ]
```

(The existing Simple-content tail stays as-is.)

- [ ] **Step 5: Fix `build_forwarded`** in `core/hailhq/core/email_forwarding.py` — add `References` (spec §6.1 table), HTML preamble, and a body-less floor:

Replace the body/headers construction in `build_forwarded`:

```python
    new_subject = _subject_with_prefix(parsed.subject)
    preamble = _preamble(parsed)
    body_text = None
    if parsed.body_text is not None:
        body_text = preamble + parsed.body_text
    body_html = None
    if parsed.body_html is not None:
        body_html = (
            "<div>" + preamble.replace("\n", "<br>") + "</div>" + parsed.body_html
        )
    if body_text is None and body_html is None:
        # Attachment-only original — the preamble alone satisfies the
        # emails_body_required CHECK on the queued outbound row.
        body_text = preamble
    headers = {
        "X-Hail-Forwarded-From": parsed.from_address or "",
        "X-Hail-Original-Message-Id": parsed.message_id or "",
        "X-Hail-Inbound-Id": str(inbound_id),
        "X-Hail-Forward-Hops": str(hops + 1),
        "Auto-Submitted": "auto-forwarded",
    }
    if parsed.message_id:
        headers["References"] = parsed.message_id
```

Add tests to `core/tests/test_email_forwarding.py`:

```python
def test_build_forwarded_sets_references_and_html_preamble():
    parsed = ParsedMime(
        from_address="alice@example.com",
        to_addresses=["alice+acme@mail.hail.so"],
        cc_addresses=[],
        subject="hi",
        message_id="<orig@example.com>",
        in_reply_to=None,
        references_ids=None,
        body_text=None,
        body_html="<p>hello</p>",
    )
    fwd = build_forwarded(
        parsed=parsed,
        target="ops@example.com",
        forwarder_address="forwarder+acme@mail.hail.so",
        inbound_id=uuid.uuid4(),
        hops=0,
    )
    assert fwd.headers["References"] == "<orig@example.com>"
    assert "Forwarded message" in fwd.body_html


def test_build_forwarded_bodyless_gets_preamble_text():
    parsed = ParsedMime(
        from_address="alice@example.com",
        to_addresses=["alice+acme@mail.hail.so"],
        cc_addresses=[],
        subject="invoice",
        message_id=None,
        in_reply_to=None,
        references_ids=None,
        body_text=None,
        body_html=None,
    )
    fwd = build_forwarded(
        parsed=parsed,
        target="ops@example.com",
        forwarder_address="forwarder+acme@mail.hail.so",
        inbound_id=uuid.uuid4(),
        hops=0,
    )
    assert fwd.body_text is not None and "Forwarded message" in fwd.body_text
```

- [ ] **Step 6: Run** — `cd core && uv run pytest tests/test_email_forwarding.py tests/providers -v` → PASS. Run the full core suite to catch other `send_email` call-site assumptions.

- [ ] **Step 7: Commit checkpoint (user).** `feat(core): headers + attachments on EmailProvider.send_email (SESv2 Raw path); References + HTML preamble on forwards`

---

### Task 6: OutboundForwardWorker — actually send queued forwards (C1 part 2)

**Files:**

- Create: `core/hailhq/core/outbound_worker.py`
- Modify: `api/hailhq/api/main.py`, `api/hailhq/api/outbound_queue.py` (docstring)
- Test: `core/tests/test_outbound_worker.py`

- [ ] **Step 1: Write the failing tests** — new `core/tests/test_outbound_worker.py`:

```python
"""OutboundForwardWorker: drains status='queued' forward rows via the provider."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from hailhq.core.models import Email, EmailAttachment, EmailDomain
from hailhq.core.outbound_worker import OutboundForwardWorker
from hailhq.core.providers.email.base import ProviderSendResult


def _domain(org_id):
    return EmailDomain(
        organization_id=org_id,
        kind="hail_mail",
        domain="alice+acme@mail.hail.so",
        local_prefix_user="alice",
        local_prefix_org="acme",
        verification_status="verified",
        provider="ses",
        verified_at=datetime.now(timezone.utc),
    )


def _queued_forward(org_id, domain_id, inbound_id, headers=None):
    return Email(
        organization_id=org_id,
        email_domain_id=domain_id,
        direction="outbound",
        from_address="forwarder+acme@mail.hail.so",
        to_addresses=["ops@example.com"],
        reply_to="alice@example.com",
        subject="Fwd: hi",
        body_text="forwarded",
        status="queued",
        provider="ses",
        metadata_={
            "forwarded_from": str(inbound_id),
            "forward_headers": headers or {"X-Hail-Forward-Hops": "1"},
        },
    )


def _worker(async_session, provider, s3=None):
    @asynccontextmanager
    async def session_factory():
        yield async_session

    return OutboundForwardWorker(
        session_factory=session_factory,
        provider_factory=lambda: provider,
        s3_factory=lambda: s3 or AsyncMock(),
    )


@pytest.mark.asyncio
async def test_tick_sends_queued_forward_and_marks_sent(async_session):
    org_id = uuid.uuid4()
    dom = _domain(org_id)
    async_session.add(dom)
    await async_session.flush()
    inbound_id = uuid.uuid4()
    row = _queued_forward(org_id, dom.id, inbound_id)
    async_session.add(row)
    await async_session.commit()

    provider = AsyncMock()
    provider.send_email.return_value = ProviderSendResult(provider_message_id="ses-1")

    processed = await _worker(async_session, provider).tick()
    assert processed == 1

    refreshed = (
        await async_session.execute(select(Email).where(Email.id == row.id))
    ).scalar_one()
    assert refreshed.status == "sent"
    assert refreshed.provider_message_id == "ses-1"
    kwargs = provider.send_email.await_args.kwargs
    assert kwargs["headers"]["X-Hail-Forward-Hops"] == "1"


@pytest.mark.asyncio
async def test_tick_reattaches_inbound_attachments(async_session):
    org_id = uuid.uuid4()
    dom = _domain(org_id)
    async_session.add(dom)
    await async_session.flush()

    inbound = Email(
        organization_id=org_id,
        email_domain_id=dom.id,
        direction="inbound",
        from_address="alice@example.com",
        to_addresses=[dom.domain],
        subject="hi",
        body_text="x",
        status="received",
        provider="ses",
    )
    async_session.add(inbound)
    await async_session.flush()
    att = EmailAttachment(
        email_id=inbound.id,
        filename="invoice.pdf",
        content_type="application/pdf",
        size_bytes=8,
        s3_key=f"attachments/{inbound.id}/x",
    )
    async_session.add(att)
    row = _queued_forward(org_id, dom.id, inbound.id)
    async_session.add(row)
    await async_session.commit()

    provider = AsyncMock()
    provider.send_email.return_value = ProviderSendResult(provider_message_id="ses-2")
    s3 = AsyncMock()
    s3.fetch_raw.return_value = b"%PDF-1.4"

    await _worker(async_session, provider, s3).tick()

    kwargs = provider.send_email.await_args.kwargs
    assert kwargs["attachments"][0].filename == "invoice.pdf"
    assert kwargs["attachments"][0].payload == b"%PDF-1.4"


@pytest.mark.asyncio
async def test_provider_failure_marks_failed_with_reason(async_session):
    org_id = uuid.uuid4()
    dom = _domain(org_id)
    async_session.add(dom)
    await async_session.flush()
    row = _queued_forward(org_id, dom.id, uuid.uuid4())
    async_session.add(row)
    await async_session.commit()

    provider = AsyncMock()
    provider.send_email.side_effect = RuntimeError("ses down")

    await _worker(async_session, provider).tick()

    refreshed = (
        await async_session.execute(select(Email).where(Email.id == row.id))
    ).scalar_one()
    assert refreshed.status == "failed"
    assert refreshed.end_reason == "RuntimeError"


@pytest.mark.asyncio
async def test_tick_ignores_direct_post_emails_queued_rows(async_session):
    """POST /emails rows (no metadata.forwarded_from) are sent inline by the
    route — the worker must never race it."""
    org_id = uuid.uuid4()
    dom = _domain(org_id)
    async_session.add(dom)
    await async_session.flush()
    direct = Email(
        organization_id=org_id,
        email_domain_id=dom.id,
        direction="outbound",
        from_address=dom.domain,
        to_addresses=["bob@example.com"],
        subject="direct",
        body_text="x",
        status="queued",
        provider="ses",
    )
    async_session.add(direct)
    await async_session.commit()

    provider = AsyncMock()
    processed = await _worker(async_session, provider).tick()
    assert processed == 0
    provider.send_email.assert_not_awaited()
```

- [ ] **Step 2: Run** — FAIL (`No module named hailhq.core.outbound_worker`).

- [ ] **Step 3: Implement** `core/hailhq/core/outbound_worker.py`:

```python
"""Background sender for ingest-queued forward emails.

``enqueue_outbound_forward`` (api/) writes Email rows with
``status='queued'`` and ``metadata.forwarded_from`` set. This worker is
the only consumer: it claims those rows with ``FOR UPDATE SKIP LOCKED``,
re-attaches the inbound row's attachments from S3, sends via the
EmailProvider (headers ride the SESv2 Raw/Headers path), and marks each
row ``sent`` or ``failed``.

Scope guard: rows WITHOUT ``metadata.forwarded_from`` are direct
``POST /emails`` rows, sent synchronously inline by the route between
its own commit and status update — the filter below must never claim
them or mail double-sends.

Single attempt per row (no retry ladder): a forward is best-effort relay;
the inbound row and raw MIME survive in S3 for manual replay.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.core.models import Email, EmailAttachment
from hailhq.core.providers.email.base import EmailProvider, ProviderAttachment
from hailhq.core.s3_inbound import S3InboundClient

logger = logging.getLogger(__name__)

POLL_BATCH = 20

SessionFactory = Callable[[], "asynccontextmanager[AsyncSession]"]


class OutboundForwardWorker:
    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        provider_factory: Callable[[], EmailProvider],
        s3_factory: Callable[[], S3InboundClient],
        poll_interval: float = 1.0,
    ) -> None:
        self._session_factory = session_factory
        self._provider_factory = provider_factory
        self._s3_factory = s3_factory
        self._provider: EmailProvider | None = None
        self._s3: S3InboundClient | None = None
        self._poll_interval = poll_interval
        self._stop = asyncio.Event()

    def _get_provider(self) -> EmailProvider:
        if self._provider is None:
            self._provider = self._provider_factory()
        return self._provider

    def _get_s3(self) -> S3InboundClient:
        if self._s3 is None:
            self._s3 = self._s3_factory()
        return self._s3

    async def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                processed = await self.tick()
            except Exception:  # pragma: no cover — defensive; logged + retried
                logger.exception("outbound forward worker tick failed")
                processed = 0
            if not processed:
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=self._poll_interval
                    )
                except asyncio.TimeoutError:
                    pass

    async def stop(self) -> None:
        self._stop.set()

    async def tick(self) -> int:
        """Claim one batch and send each row. Returns the number claimed.

        Rows stay locked for the duration of the sends (one transaction
        per tick) — simplest correct claim without a 'sending' status.
        Fine at v1 forward volumes; revisit if a tick ever spans minutes.
        """
        async with self._session_factory() as session:
            stmt = (
                select(Email)
                .where(Email.status == "queued")
                .where(Email.direction == "outbound")
                .where(Email.metadata_["forwarded_from"].astext.isnot(None))
                .order_by(Email.created_at.asc())
                .limit(POLL_BATCH)
                .with_for_update(skip_locked=True)
            )
            rows = list((await session.execute(stmt)).scalars().all())
            for row in rows:
                await self._send_one(session, row)
            await session.commit()
        return len(rows)

    async def _send_one(self, session: AsyncSession, row: Email) -> None:
        meta: dict[str, Any] = row.metadata_ or {}
        headers: dict[str, str] = meta.get("forward_headers") or {}
        now = datetime.now(timezone.utc)
        try:
            attachments = await self._load_attachments(session, meta)
            result = await self._get_provider().send_email(
                from_address=row.from_address,
                to_addresses=row.to_addresses,
                subject=row.subject,
                body_text=row.body_text,
                body_html=row.body_html,
                cc=row.cc_addresses,
                bcc=row.bcc_addresses,
                reply_to=row.reply_to,
                headers=headers,
                attachments=attachments or None,
            )
        except Exception as exc:
            logger.warning(
                "forward send failed for email_id=%s", row.id, exc_info=True
            )
            row.status = "failed"
            row.end_reason = type(exc).__name__
            row.failed_at = now
            return
        row.status = "sent"
        row.provider_message_id = result.provider_message_id
        row.sent_at = now

    async def _load_attachments(
        self, session: AsyncSession, meta: dict[str, Any]
    ) -> list[ProviderAttachment]:
        inbound_id = meta.get("forwarded_from")
        if not inbound_id:
            return []
        atts = (
            (
                await session.execute(
                    select(EmailAttachment).where(
                        EmailAttachment.email_id == UUID(inbound_id)
                    )
                )
            )
            .scalars()
            .all()
        )
        out: list[ProviderAttachment] = []
        for att in atts:
            payload = await self._get_s3().fetch_raw(att.s3_key)
            out.append(
                ProviderAttachment(
                    filename=att.filename,
                    content_type=att.content_type,
                    payload=payload,
                )
            )
        return out


__all__ = ["OutboundForwardWorker"]
```

- [ ] **Step 4: Wire into lifespan** — `api/hailhq/api/main.py`, add imports:

```python
from hailhq.core.outbound_worker import OutboundForwardWorker
from hailhq.core.providers.email.ses import SesEmailProvider
from hailhq.core.s3_inbound import S3InboundClient
```

In `lifespan`, after the webhook-worker block:

```python
    forward_worker: OutboundForwardWorker | None = None
    forward_task: asyncio.Task | None = None
    if settings.hail_inbound_enabled and settings.hail_inbound_bucket:
        forward_worker = OutboundForwardWorker(
            session_factory=session_scope,
            provider_factory=SesEmailProvider,
            s3_factory=lambda: S3InboundClient(bucket=settings.hail_inbound_bucket),
        )
        forward_task = asyncio.create_task(
            forward_worker.run_forever(), name="outbound-forward-worker"
        )
```

Teardown (next to the webhook teardown):

```python
        if forward_worker is not None and forward_task is not None:
            await forward_worker.stop()
            try:
                await asyncio.wait_for(forward_task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                forward_task.cancel()
```

- [ ] **Step 5: Fix the stale docstring** in `api/hailhq/api/outbound_queue.py` — replace "the outbound send loop (existing for direct POST /emails) handles the SES call" with "the `OutboundForwardWorker` (started in main.py's lifespan when inbound is enabled) claims and sends it".

- [ ] **Step 6: Run** — `cd core && uv run pytest tests/test_outbound_worker.py -v` → PASS; `cd api && uv run pytest -x -q` green (lifespan test from Task 4 still passes — worker is gated off by default settings).

- [ ] **Step 7: Commit checkpoint (user).** `feat(core,api): OutboundForwardWorker drains queued forwards — forwarding now sends end-to-end`

---

### Task 7: Org inbound cap also gates forwarding (I2)

**Files:**

- Modify: `core/hailhq/core/email_ingest.py`
- Test: `core/tests/test_email_ingest.py`

- [ ] **Step 1: Failing test:**

```python
@pytest.mark.asyncio
async def test_org_cap_suppresses_forwarding_too(async_session):
    org_id = uuid.uuid4()
    domain = _make_inbound_domain(org_id)
    domain.inbound_enabled = True
    domain.forward_to = ["ops@example.com"]
    async_session.add(domain)
    await async_session.commit()

    s3 = AsyncMock()
    s3.fetch_raw.return_value = (FIX / "simple.eml").read_bytes()
    forward_enqueue = AsyncMock()

    result = await ingest_inbound(
        async_session,
        message=InboundMessage(
            provider_message_id="cap-1",
            envelope_from="alice@example.com",
            envelope_recipients=[domain.domain],
            raw_s3_bucket="b",
            raw_s3_key="raw/cap-1",
            spam_verdict="PASS",
            virus_verdict="PASS",
            spf_verdict="PASS",
            dkim_verdict="PASS",
            dmarc_verdict="PASS",
            received_at=datetime(2026, 6, 6, tzinfo=timezone.utc),
        ),
        s3=s3,
        hail_mail_base_domain="mail.hail.so",
        forward_enqueue=forward_enqueue,
        org_rate_per_hour=0,  # cap of 0 → always over
    )
    assert len(result.email_ids) == 1  # persisted
    forward_enqueue.assert_not_awaited()  # but not forwarded
```

- [ ] **Step 2: Run** — FAIL (forward fired).

- [ ] **Step 3: Implement** — in `ingest_inbound`, add `not over_cap` to the forward guard (combined with Task 1's `created`):

```python
        if (
            created
            and suppress is None
            and not over_cap
            and forward_enqueue is not None
            and domain.inbound_enabled
        ):
```

- [ ] **Step 4: Run** → PASS; whole ingest suite green.
- [ ] **Step 5: Commit checkpoint (user).** `fix(core): org inbound cap suppresses forwarding, not just webhooks (spec §7 fan-out)`

---

### Task 8: Emit `email.received.suppressed` on the wire; rename `org_rate_limit` → `inbound_rate_limit` (I1)

Spec §6.2/§7 require a suppressed event with `reason ∈ {forward_loop, forward_rate_limit, inbound_rate_limit}`. (`email.bounced`/`email.complained` remain in the enum: SES bounce/complaint ingestion is the next milestone's wiring — note this in the schemas docstring rather than churning the enum.)

**Files:**

- Modify: `core/hailhq/core/email_ingest.py`, `core/hailhq/core/schemas.py` (enum comment)
- Test: `core/tests/test_email_ingest.py`; grep-update any test asserting `"org_rate_limit"`

- [ ] **Step 1: Failing test:**

```python
@pytest.mark.asyncio
async def test_suppressed_event_emitted_on_rate_limit(async_session):
    org_id = uuid.uuid4()
    domain = _make_inbound_domain(org_id)
    async_session.add(domain)
    await async_session.commit()

    s3 = AsyncMock()
    s3.fetch_raw.return_value = (FIX / "simple.eml").read_bytes()
    fanout = AsyncMock(return_value=1)

    result = await ingest_inbound(
        async_session,
        message=InboundMessage(
            provider_message_id="sup-1",
            envelope_from="alice@example.com",
            envelope_recipients=[domain.domain],
            raw_s3_bucket="b",
            raw_s3_key="raw/sup-1",
            spam_verdict="PASS",
            virus_verdict="PASS",
            spf_verdict="PASS",
            dkim_verdict="PASS",
            dmarc_verdict="PASS",
            received_at=datetime(2026, 6, 6, tzinfo=timezone.utc),
        ),
        s3=s3,
        hail_mail_base_domain="mail.hail.so",
        fanout=fanout,
        org_rate_per_hour=0,
    )
    assert result.suppressed_reasons == ["inbound_rate_limit"]
    fanout.assert_awaited_once()
    kwargs = fanout.await_args.kwargs
    assert kwargs["event_type"] == "email.received.suppressed"
    assert kwargs["data"]["reason"] == "inbound_rate_limit"


@pytest.mark.asyncio
async def test_suppressed_event_emitted_on_forward_loop(async_session):
    org_id = uuid.uuid4()
    domain = _make_inbound_domain(org_id)
    domain.inbound_enabled = True
    domain.forward_to = ["ops@mail.hail.so"]  # base-domain target → loop
    async_session.add(domain)
    await async_session.commit()

    s3 = AsyncMock()
    s3.fetch_raw.return_value = (FIX / "simple.eml").read_bytes()
    fanout = AsyncMock(return_value=1)
    forward_enqueue = AsyncMock()

    result = await ingest_inbound(
        async_session,
        message=InboundMessage(
            provider_message_id="sup-2",
            envelope_from="alice@example.com",
            envelope_recipients=[domain.domain],
            raw_s3_bucket="b",
            raw_s3_key="raw/sup-2",
            spam_verdict="PASS",
            virus_verdict="PASS",
            spf_verdict="PASS",
            dkim_verdict="PASS",
            dmarc_verdict="PASS",
            received_at=datetime(2026, 6, 6, tzinfo=timezone.utc),
        ),
        s3=s3,
        hail_mail_base_domain="mail.hail.so",
        forward_enqueue=forward_enqueue,
        fanout=fanout,
        org_rate_per_hour=10_000,
    )
    assert "forward_loop" in result.suppressed_reasons
    event_types = [c.kwargs["event_type"] for c in fanout.await_args_list]
    assert "email.received" in event_types
    assert "email.received.suppressed" in event_types
    sup = next(
        c.kwargs
        for c in fanout.await_args_list
        if c.kwargs["event_type"] == "email.received.suppressed"
    )
    assert sup["data"]["reason"] == "forward_loop"
```

- [ ] **Step 2: Run** — FAIL.

- [ ] **Step 3: Implement** in `core/hailhq/core/email_ingest.py`:

(a) Refactor `_enqueue_forwards` to **return** this row's suppression reasons instead of mutating `result`. Change signature — drop the `result: "IngestResult"` parameter, return `list[str]`:

```python
) -> list[str]:
    targets = list(domain.forward_to or [])
    if not targets:
        return []

    reasons: list[str] = []
    limiter = ForwardLimiter(default_per_hour=forward_default_per_hour)
    if not await limiter.can_forward(
        db,
        organization_id=domain.organization_id,
        email_domain_id=domain.id,
        override=domain.forward_rate_per_hour,
    ):
        return ["forward_rate_limit"]
```

In the loop, replace the `result.suppressed_reasons` mutation:

```python
        except LoopDetected as exc:
            if "forward_loop" not in reasons:
                reasons.append("forward_loop")
            if exc.cause == "hop_cap":
                return reasons
            continue
```

and `return reasons` at the end.

(b) In `ingest_inbound`, restructure the per-recipient tail (this incorporates Tasks 1 and 7's guards):

```python
        row_reasons: list[str] = []
        if over_cap:
            row_reasons.append("inbound_rate_limit")

        if (
            created
            and suppress is None
            and not over_cap
            and forward_enqueue is not None
            and domain.inbound_enabled
        ):
            row_reasons += await _enqueue_forwards(
                db,
                domain=domain,
                parsed=parsed,
                inbound_id=email_id,
                hops=inbound_hops,
                hail_mail_base_domain=hail_mail_base_domain,
                forward_max_hops=forward_max_hops,
                forward_default_per_hour=forward_default_per_hour,
                forward_enqueue=forward_enqueue,
            )

        for reason in row_reasons:
            if reason not in result.suppressed_reasons:
                result.suppressed_reasons.append(reason)

        if created and suppress is None and fanout is not None:
            if not over_cap:
                data = build_event_data(... unchanged ...)
                await fanout(
                    db,
                    organization_id=domain.organization_id,
                    email_domain_id=domain.id,
                    event_type="email.received",
                    event_id=email_id,
                    data=data,
                )
            for reason in row_reasons:
                await fanout(
                    db,
                    organization_id=domain.organization_id,
                    email_domain_id=domain.id,
                    event_type="email.received.suppressed",
                    event_id=email_id,
                    data={
                        "id": str(email_id),
                        "direction": "inbound",
                        "from_address": parsed.from_address,
                        "to_addresses": parsed.to_addresses
                        or list(message.envelope_recipients),
                        "subject": parsed.subject or "",
                        "message_id": parsed.message_id,
                        "reason": reason,
                    },
                )
```

(Keep the existing `build_event_data(...)` call body exactly as it is today — only its position moves under `if not over_cap:`.)

(c) Delete the old `over_cap`-appends-`"org_rate_limit"` block (lines 332-333). Grep `org_rate_limit` across `core/ api/` and update any test assertions to `inbound_rate_limit`.

(d) In `core/hailhq/core/schemas.py`, above `WebhookEventType`, add:

```python
# email.bounced / email.complained are subscribable now but only emitted once
# SES bounce/complaint ingestion lands (next milestone); documented in
# docs/setup/aws-ses.md. email.received.suppressed fires with
# data.reason ∈ {forward_loop, forward_rate_limit, inbound_rate_limit}.
```

- [ ] **Step 4: Run** — new tests PASS; `uv run pytest core/tests api/tests -q` green.
- [ ] **Step 5: Commit checkpoint (user).** `feat(core): emit email.received.suppressed events; rename org_rate_limit reason to inbound_rate_limit (spec §6.2/§7)`

---

### Task 9: IPv6-aware SSRF guard + non-blocking DNS (I3 + minor)

**Files:**

- Modify: `core/hailhq/core/http_post.py`
- Test: `core/tests/test_http_post.py`

- [ ] **Step 1: Failing tests** — append to `core/tests/test_http_post.py`:

```python
def test_ipv6_loopback_literal_is_private():
    assert is_private_url("https://[::1]/hook")


def test_ipv6_only_hostname_resolving_private_is_private(monkeypatch):
    def fake_getaddrinfo(host, port, **kw):
        # AAAA-only host resolving to IPv6 loopback
        return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 0, 0, 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    assert is_private_url("https://internal.example.com/hook")


def test_mixed_records_any_private_is_private(monkeypatch):
    def fake_getaddrinfo(host, port, **kw):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    assert is_private_url("https://rebind.example.com/hook")
```

- [ ] **Step 2: Run** — second and third FAIL (gethostbyname is IPv4-only / single-record).

- [ ] **Step 3: Implement** — replace `_is_private_host` in `core/hailhq/core/http_post.py`:

```python
def _ip_is_private(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved


def _resolve_all(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return []
    out: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for _family, _type, _proto, _canon, sockaddr in infos:
        try:
            out.append(ipaddress.ip_address(sockaddr[0]))
        except ValueError:
            continue
    return out


def _is_private_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return _ip_is_private(ipaddress.ip_address(host))
    except ValueError:
        pass
    # Check EVERY resolved address (A + AAAA) — an AAAA-only host pointing
    # at ::1/ULA space must not slip through an IPv4-only lookup.
    ips = _resolve_all(host)
    return any(_ip_is_private(ip) for ip in ips)
```

And make the delivery-time check non-blocking — in `httpx_post`, add `import asyncio` at top and change the guard:

```python
    if not allow_private_networks and await asyncio.to_thread(is_private_url, url):
        raise PrivateNetworkBlockedError(url)
```

(`validate_webhook_target` stays sync — it runs once per write-time validation; the worker hot path is `httpx_post`.)

- [ ] **Step 4: Run** — `cd core && uv run pytest tests/test_http_post.py -v` → PASS (existing tests too; `"::1"` literal now parses via `ip_address` so the old set-membership shortcut can go).
- [ ] **Step 5: Commit checkpoint (user).** `fix(core): SSRF guard resolves A+AAAA and checks every address; DNS off the event loop`

---

### Task 10: Lowercase local parts in routing (I4)

**Files:**

- Modify: `core/hailhq/core/email_routing.py`
- Test: `core/tests/test_email_routing.py`

- [ ] **Step 1: Failing test:**

```python
def test_mixed_case_local_part_routes():
    r = classify_hail_mail_recipient("Alice+Acme@MAIL.HAIL.SO", "mail.hail.so")
    assert r is not None
    assert r.user_prefix == "alice"
    assert r.org_prefix == "acme"
```

- [ ] **Step 2: Run** — FAIL (returns None).

- [ ] **Step 3: Implement** — in `classify_hail_mail_recipient`, after `local, _, domain = address.partition("@")`:

```python
    # Hail mints lowercase prefixes; senders type whatever they like.
    # Local-part case-insensitivity is safe here because these are our
    # own addresses, not arbitrary third-party mailboxes.
    local = local.lower()
```

- [ ] **Step 4: Run** → PASS; routing suite green.
- [ ] **Step 5: Commit checkpoint (user).** `fix(core): route mixed-case hail-mail recipients (lowercase local part)`

---

### Task 11: Fix dead 409 branch in PATCH /email-domains (I5)

**Files:**

- Modify: `api/hailhq/api/routes/email_domains.py:468`
- Test: `api/tests/test_email_domains_api.py`

- [ ] **Step 1: Failing test** (same-org duplicate via PATCH must 409, not 422):

```python
@pytest.mark.asyncio
async def test_patch_duplicate_prefix_returns_409(client, monkeypatch):
    monkeypatch.setattr(settings, "hail_mail_base_domain", "mail.hail.so")
    r1 = await client.post(
        "/email-domains",
        json={"kind": "hail_mail", "local_prefix_user": "a", "local_prefix_org": "acme"},
    )
    r2 = await client.post(
        "/email-domains",
        json={"kind": "hail_mail", "local_prefix_user": "b", "local_prefix_org": "acme"},
    )
    assert r1.status_code == 201 and r2.status_code == 201

    resp = await client.patch(
        f"/email-domains/{r2.json()['id']}",
        json={"local_prefix_user": "a"},  # collides with r1's address
    )
    assert resp.status_code == 409
```

- [ ] **Step 2: Run** — FAIL (422 today: `"local_prefix" in updates` never matches because the keys are `local_prefix_user`/`local_prefix_org`).

- [ ] **Step 3: Implement** — line 468:

```python
        if "local_prefix_user" in updates:
```

- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit checkpoint (user).** `fix(api): duplicate hail-mail prefix PATCH returns 409 (dead exact-key guard)`

---

### Task 12: SDK EmailDomainPatch — add the inbound fields (I6)

**Files:**

- Modify: `sdk/hail/models.py:399-431`
- Test: `sdk/tests/test_email_domains.py`

- [ ] **Step 1: Failing test:**

```python
def test_email_domain_patch_accepts_inbound_fields():
    p = EmailDomainPatch(
        inbound_enabled=True,
        forward_to=["ops@example.com"],
        webhook_url="https://example.com/hook",
        forward_rate_per_hour=100,
    )
    assert p.inbound_enabled is True


def test_email_domain_patch_still_requires_at_least_one_field():
    with pytest.raises(ValidationError):
        EmailDomainPatch()
```

- [ ] **Step 2: Run** — FAIL (`extra="forbid"` rejects the fields).

- [ ] **Step 3: Implement** — in `sdk/hail/models.py`, replace the `EmailDomainPatch` docstring and add fields (keep `extra="forbid"` and the prefix validator):

```python
class EmailDomainPatch(BaseModel):
    """Body for ``PATCH /email-domains/{id}``.

    Two clusters of mutable fields, mirroring the server contract:

    * Hail-mail addressing — ``local_prefix_user`` / ``local_prefix_org``
      (``kind='hail_mail'`` rows only; the server 422s otherwise).
    * Inbound action — ``inbound_enabled``, ``forward_to``,
      ``webhook_url`` (empty string clears it; the server returns the new
      plaintext secret once when set), ``forward_rate_per_hour``.
    """

    model_config = ConfigDict(extra="forbid")

    local_prefix_user: str | None = None
    local_prefix_org: str | None = None
    inbound_enabled: bool | None = None
    forward_to: list[str] | None = None
    webhook_url: str | None = None
    forward_rate_per_hour: int | None = None
```

and replace the `_at_least_one_field` validator:

```python
    @model_validator(mode="after")
    def _at_least_one_field(self):
        if not self.model_fields_set:
            raise ValueError("at least one field must be set")
        return self
```

- [ ] **Step 4: Run** — `cd sdk && uv run pytest tests/test_email_domains.py -v` → PASS (full sdk suite green).
- [ ] **Step 5: Commit checkpoint (user).** `fix(sdk): EmailDomainPatch carries the inbound fields the API accepts`

---

### Task 13: Missing tests — redeliver round-trip, CLI webhooks behavior (I7)

**Files:**

- Test: `api/tests/test_webhooks_api.py`, `core/tests/test_webhook_worker.py`, `cli/internal/cmd/webhooks_test.go`
- Modify: `cli/internal/cmd/webhooks.go` (empty-events guard)

- [ ] **Step 1: API redeliver test** — append to `api/tests/test_webhooks_api.py` (reuse that module's existing subscription-creation helper/fixtures and its secret-key monkeypatch):

```python
@pytest.mark.asyncio
async def test_redeliver_resets_dead_row_to_pending(client, async_session, webhook_key):
    sub = await _create_subscription(client)  # module's existing helper
    delivery = WebhookDelivery(
        subscription_id=UUID(sub["id"]),
        email_domain_id=None,
        event_type="email.received",
        event_id=uuid.uuid4(),
        payload={"organization_id": sub["organization_id"], "data": {"id": "x"}},
        attempt=7,
        status="dead",
    )
    async_session.add(delivery)
    await async_session.commit()

    resp = await client.post(
        f"/webhooks/{sub['id']}/deliveries/{delivery.id}/redeliver"
    )
    assert resp.status_code == 200

    await async_session.refresh(delivery)
    assert delivery.status == "pending"
    assert delivery.attempt == 0
```

(Adapt helper/fixture names to what the module actually defines — read its top 60 lines first. The behavioral assertions are the contract.)

- [ ] **Step 2: Worker re-delivers a redelivered row** — append to `core/tests/test_webhook_worker.py`, mirroring its existing fixture pattern (fake `http_post` returning 200): insert a `pending, attempt=0` row that was previously dead, run `await worker.tick()`, assert status `succeeded`.

- [ ] **Step 3: CLI guard + tests.** In `cli/internal/cmd/webhooks.go`, `runWebhooksCreate`, replace the events loop:

```go
	events := strings.Split(f.events, ",")
	body := client.WebhookSubscriptionCreate{
		TargetUrl: f.url,
	}
	for _, e := range events {
		e = strings.TrimSpace(e)
		if e == "" {
			continue
		}
		body.EventTypes = append(
			body.EventTypes, client.WebhookSubscriptionCreateEventTypes(e),
		)
	}
	if len(body.EventTypes) == 0 {
		return fmt.Errorf("--events must name at least one event type")
	}
```

Append to `cli/internal/cmd/webhooks_test.go` (mirror the fake-server harness used in `email_domain_test.go`):

```go
func TestWebhooksCreate_SendsParsedEvents(t *testing.T) {
	var got map[string]any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || r.URL.Path != "/webhooks" {
			t.Fatalf("unexpected %s %s", r.Method, r.URL.Path)
		}
		_ = json.NewDecoder(r.Body).Decode(&got)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		_, _ = w.Write([]byte(`{"id":"3a4ce607-7b26-4807-8cd1-7c66f4c1bd9d",
			"organization_id":"3a4ce607-7b26-4807-8cd1-7c66f4c1bd9e",
			"target_url":"https://example.com/hook","event_types":["email.received"],
			"status":"active","consecutive_failures":0,
			"created_at":"2026-06-10T00:00:00Z","updated_at":"2026-06-10T00:00:00Z",
			"secret":"whs_x"}`))
	}))
	defer srv.Close()

	out := runCLI(t, srv.URL,
		"webhooks", "create",
		"--url", "https://example.com/hook",
		"--events", "email.received, email.bounced")
	_ = out
	evs, _ := got["event_types"].([]any)
	if len(evs) != 2 || evs[0] != "email.received" || evs[1] != "email.bounced" {
		t.Fatalf("event_types = %v", got["event_types"])
	}
}

func TestWebhooksCreate_RejectsEmptyEvents(t *testing.T) {
	err := runCLIExpectError(t, "http://unused.invalid",
		"webhooks", "create", "--url", "https://example.com/hook", "--events", " , ")
	if err == nil || !strings.Contains(err.Error(), "at least one event type") {
		t.Fatalf("expected empty-events error, got %v", err)
	}
}

func TestWebhooksRedeliver_HitsCorrectPath(t *testing.T) {
	subID := "3a4ce607-7b26-4807-8cd1-7c66f4c1bd9d"
	delID := "3a4ce607-7b26-4807-8cd1-7c66f4c1bd9f"
	var gotPath string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.URL.Path
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"id":"` + delID + `","subscription_id":"` + subID + `",
			"email_domain_id":null,"event_type":"email.received",
			"event_id":"3a4ce607-7b26-4807-8cd1-7c66f4c1bd90","attempt":0,
			"status":"pending","created_at":"2026-06-10T00:00:00Z",
			"next_attempt_at":"2026-06-10T00:00:00Z"}`))
	}))
	defer srv.Close()

	runCLI(t, srv.URL, "webhooks", "redeliver", subID, delID)
	want := "/webhooks/" + subID + "/deliveries/" + delID + "/redeliver"
	if gotPath != want {
		t.Fatalf("path = %q, want %q", gotPath, want)
	}
}
```

`runCLI`/`runCLIExpectError` stand for the harness functions `email_domain_test.go` actually uses — read that file and reuse its exact pattern (Options with fake client base URL, captured stdout, etc.). Adjust the canned JSON bodies to whatever the generated response types require to deserialize.

- [ ] **Step 4: Run** — `cd api && uv run pytest tests/test_webhooks_api.py -v`; `cd core && uv run pytest tests/test_webhook_worker.py -v`; `cd cli && go test ./internal/cmd/ -run 'Webhooks' -v` → all PASS.
- [ ] **Step 5: Commit checkpoint (user).** `test(api,core,cli): redeliver round-trip + behavioral webhooks CLI coverage; reject empty --events`

---

### Task 14: Ingest polish — `envelope_from` fallback, dedupe for ID-less mail (minors)

**Files:**

- Modify: `core/hailhq/core/email_ingest.py`, `core/hailhq/core/models.py`, `api/migrations/versions/0010_hail_mail_global_unique.py` (extend)
- Test: `core/tests/test_email_ingest.py`

- [ ] **Step 1: Failing tests:**

```python
@pytest.mark.asyncio
async def test_missing_from_header_falls_back_to_envelope_from(async_session):
    # fixture without a From: header
    raw = b"To: alice+acme@mail.hail.so\r\nSubject: no from\r\nMessage-ID: <nf@x>\r\n\r\nhi"
    org_id = uuid.uuid4()
    domain = _make_inbound_domain(org_id)
    async_session.add(domain)
    await async_session.commit()
    s3 = AsyncMock()
    s3.fetch_raw.return_value = raw
    result = await ingest_inbound(
        async_session,
        message=InboundMessage(
            provider_message_id="nf-1",
            envelope_from="bounce@example.com",
            envelope_recipients=[domain.domain],
            raw_s3_bucket="b",
            raw_s3_key="raw/nf-1",
            spam_verdict="PASS", virus_verdict="PASS", spf_verdict="PASS",
            dkim_verdict="PASS", dmarc_verdict="PASS",
            received_at=datetime(2026, 6, 6, tzinfo=timezone.utc),
        ),
        s3=s3,
        hail_mail_base_domain="mail.hail.so",
        org_rate_per_hour=10_000,
    )
    email = (
        await async_session.execute(select(Email).where(Email.id == result.email_ids[0]))
    ).scalar_one()
    assert email.from_address == "bounce@example.com"


@pytest.mark.asyncio
async def test_mail_without_message_id_is_still_dedupe_safe(async_session):
    raw = b"From: a@x\r\nTo: alice+acme@mail.hail.so\r\nSubject: no mid\r\n\r\nhi"
    org_id = uuid.uuid4()
    domain = _make_inbound_domain(org_id)
    async_session.add(domain)
    await async_session.commit()
    s3 = AsyncMock()
    s3.fetch_raw.return_value = raw
    msg = InboundMessage(
        provider_message_id="ses-receipt-1",
        envelope_from="a@x",
        envelope_recipients=[domain.domain],
        raw_s3_bucket="b",
        raw_s3_key="raw/ses-receipt-1",
        spam_verdict="PASS", virus_verdict="PASS", spf_verdict="PASS",
        dkim_verdict="PASS", dmarc_verdict="PASS",
        received_at=datetime(2026, 6, 6, tzinfo=timezone.utc),
    )
    for _ in range(2):  # redelivery of the same SES receipt
        await ingest_inbound(
            async_session, message=msg, s3=s3,
            hail_mail_base_domain="mail.hail.so", org_rate_per_hour=10_000,
        )
    count = (
        await async_session.execute(
            select(func.count()).select_from(Email).where(
                Email.organization_id == org_id, Email.direction == "inbound"
            )
        )
    ).scalar_one()
    assert count == 1
```

- [ ] **Step 2: Run** — FAIL (empty `from_address`; 2 rows).

- [ ] **Step 3: Implement.**

(a) In `_persist_one`, the `Email(...)` constructor: `from_address=parsed.from_address or message.envelope_from,`.

(b) Inbound dedupe for ID-less mail keys on the SES receipt id, which is what actually repeats on redelivery. Extend `_existing_inbound_id` → `_existing_inbound`:

```python
async def _existing_inbound_id(
    db: AsyncSession,
    organization_id: UUID,
    message_id: str | None,
    provider_message_id: str | None,
) -> UUID | None:
    if message_id is not None:
        stmt = select(Email.id).where(
            Email.organization_id == organization_id,
            Email.message_id == message_id,
            Email.direction == "inbound",
        )
        found = (await db.execute(stmt)).scalar_one_or_none()
        if found is not None:
            return found
    if provider_message_id is not None:
        stmt = select(Email.id).where(
            Email.organization_id == organization_id,
            Email.provider_message_id == provider_message_id,
            Email.direction == "inbound",
        )
        return (await db.execute(stmt)).scalar_one_or_none()
    return None
```

Update both call sites in `_persist_one` to pass `message.provider_message_id`.

(c) Add the backing partial unique index. `core/hailhq/core/models.py`, `Email.__table_args__`:

```python
        Index(
            "emails_inbound_provider_message_id_uq",
            "organization_id",
            "provider_message_id",
            unique=True,
            postgresql_where=text(
                "direction = 'inbound' AND provider_message_id IS NOT NULL"
            ),
        ),
```

Extend migration `0010` upgrade (and mirror in downgrade):

```python
    op.create_index(
        "emails_inbound_provider_message_id_uq",
        "emails",
        ["organization_id", "provider_message_id"],
        unique=True,
        postgresql_where=sa.text(
            "direction = 'inbound' AND provider_message_id IS NOT NULL"
        ),
    )
```

(d) Widen Task 2's narrowed except to accept both dedupe constraints:

```python
        if (
            "emails_inbound_message_id_uq" not in str(exc.orig)
            and "emails_inbound_provider_message_id_uq" not in str(exc.orig)
        ):
            raise
```

- [ ] **Step 4: Run** — new tests PASS; ingest + multi-org suites green (`test_internal_ses_events_multi_org.py` exercises same-provider-id-across-orgs — the new index is org-scoped so it must stay green).
- [ ] **Step 5: Commit checkpoint (user).** `fix(core): envelope_from fallback + provider-receipt dedupe for mail without Message-ID`

---

### Task 15: MIME — keep ALL inline text parts (minor)

**Files:**

- Modify: `core/hailhq/core/email_mime.py`
- Test: `core/tests/test_email_mime.py`

- [ ] **Step 1: Failing test:**

```python
def test_multiple_inline_text_parts_are_concatenated():
    raw = (
        b"From: a@x\r\nTo: b@y\r\nSubject: s\r\nMIME-Version: 1.0\r\n"
        b'Content-Type: multipart/mixed; boundary="b1"\r\n\r\n'
        b"--b1\r\nContent-Type: text/plain\r\n\r\npart one\r\n"
        b"--b1\r\nContent-Type: application/pdf\r\n"
        b'Content-Disposition: attachment; filename="f.pdf"\r\n\r\nx\r\n'
        b"--b1\r\nContent-Type: text/plain\r\n\r\npart two\r\n"
        b"--b1--\r\n"
    )
    parsed = parse_mime(raw)
    assert "part one" in parsed.body_text
    assert "part two" in parsed.body_text
```

- [ ] **Step 2: Run** — FAIL ("part two" dropped).

- [ ] **Step 3: Implement** — in `_collect`, drop the first-only guards:

```python
    if ctype == "text/plain":
        text.append(_safe_text(part))
    elif ctype == "text/html":
        html.append(_safe_text(part))
```

and join in `_walk_bodies`:

```python
    return (
        "\n".join(text) if text else None,
        "\n".join(html) if html else None,
        atts,
    )
```

Caveat to watch: `multipart/alternative` (plain + html of the SAME content) must not duplicate — alternative parts land in _different_ buckets (text vs html), so joining within a bucket stays correct. Run the full mime suite to confirm existing alternative fixtures still pass.

- [ ] **Step 4: Run** → PASS; `tests/test_email_mime.py` green.
- [ ] **Step 5: Commit checkpoint (user).** `fix(core): preserve all inline text/html parts when parsing inbound MIME`

---

### Task 16: Ingest endpoint hardening + docstring corrections (minors)

**Files:**

- Modify: `core/hailhq/core/providers/email/inbound/ses.py`, `api/hailhq/api/routes/internal/ses_events.py`, `core/hailhq/core/models.py` (docstring), `core/hailhq/core/schemas.py` (docstring), `core/hailhq/core/email_ingest.py` (comments)
- Test: `api/tests/test_internal_ses_events.py`, `core/tests/providers/` (inbound ses tests)

- [ ] **Step 1: Failing tests:**

Core (non-ASCII signature must be a clean False, not TypeError):

```python
@pytest.mark.asyncio
async def test_non_ascii_signature_is_rejected_not_500():
    provider = SesInboundProvider(hmac_secret="s")
    ok = await provider.verify_notification(
        {"X-Hail-Signature": "sha256=héllo"}, b"{}"
    )
    assert ok is False
```

API (validly-signed garbage body → 400, not 500; unset HMAC secret with inbound enabled → 503):

```python
@pytest.mark.asyncio
async def test_malformed_signed_body_returns_400(
    client, inbound_enabled, override_internal_deps
):
    body = json.dumps({"not": "a ses envelope"}).encode()
    resp = await client.post(
        "/internal/ses-events", content=body, headers=_signed(body)
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_enabled_but_missing_hmac_secret_returns_503(client, monkeypatch):
    monkeypatch.setattr(settings, "hail_inbound_enabled", True)
    monkeypatch.setattr(settings, "hail_inbound_hmac_secret", "")
    resp = await client.post("/internal/ses-events", content=b"{}")
    assert resp.status_code == 503
```

- [ ] **Step 2: Run** — FAIL (TypeError→500, KeyError→500, ValueError→500).

- [ ] **Step 3: Implement.**

(a) `core/hailhq/core/providers/email/inbound/ses.py:36-38` — compare bytes:

```python
        provided = header.split("=", 1)[1]
        expected = hmac.new(self._secret, body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(provided.encode(), expected.encode())
```

(b) `api/hailhq/api/routes/internal/ses_events.py`:

```python
def get_inbound_provider() -> SesInboundProvider:
    try:
        return SesInboundProvider(hmac_secret=settings.hail_inbound_hmac_secret)
    except ValueError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="inbound enabled but HAIL_INBOUND_HMAC_SECRET is unset",
        ) from exc
```

and wrap parsing in the handler:

```python
    try:
        message = await provider.parse_notification(body)
    except Exception as exc:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail="malformed SES notification payload",
        ) from exc
```

(c) Docstring/comment sweep (no behavior):

- `ses_events.py:10-11`: replace "Fan-out … lands in subsequent phases." with "then enqueues forwarding sends and webhook fan-out for the persisted rows."
- `core/hailhq/core/models.py` `WebhookSubscription` docstring (~line 632): "bcrypt-hashed" → "Fernet-encrypted at rest (see `hailhq.core.secret_cipher`)". Also fix `Email`'s class docstring first line: "A single outbound email message." → "A single email message, outbound or inbound."
- `core/hailhq/core/schemas.py` `EmailDomainPatch` docstring: replace "in practice only `custom` rows can receive inbound mail (SES rules route on the bare domain)" with "this milestone routes inbound only to `hail_mail` rows; custom-domain inbound (MX delegation) is the next milestone".
- `core/hailhq/core/email_ingest.py` `_enqueue_forwards`: add above the limiter call:

```python
    # Soft cap: checked once per message, then we enqueue len(targets) sends —
    # can overshoot by N-1 on a multi-target domain. Acceptable for a soft cap.
```

and above `_incoming_forward_hops` usage in `ingest_inbound`:

```python
    # Hop counting trusts the X-Hail-Forward-Hops header; an external sender
    # can spoof it to suppress a tenant's forwarding (inherent to header-based
    # loop prevention — same trade-off as classic Received: counting).
```

- [ ] **Step 4: Run** — all three tests PASS; suites green.
- [ ] **Step 5: Commit checkpoint (user).** `fix(api,core): 400 on malformed SES payloads, 503 on missing HMAC secret, bytes-safe signature compare; docstring corrections`

---

### Task 17: Mirror migration-only indexes in ORM metadata (minor)

**Files:**

- Modify: `core/hailhq/core/models.py` (`Email.__table_args__`)
- Test: `api/tests/test_migrations.py` (existing parity coverage)

- [ ] **Step 1: Implement** — add to `Email.__table_args__` (migration 0007 created these; `create_all`-based test schemas drift without them):

```python
        Index("emails_org_direction_created_idx",
              "organization_id", "direction", text("created_at DESC")),
        Index("emails_message_id_idx", "message_id"),
```

- [ ] **Step 2: Run** — `cd api && uv run pytest tests/test_migrations.py -q && cd ../core && uv run pytest tests/test_models.py -q` → PASS.
- [ ] **Step 3: Commit checkpoint (user).** `fix(core): mirror emails indexes from migration 0007 in ORM metadata`

---

### Task 18: Audit logs on webhooks CRUD (minor)

**Files:**

- Modify: `api/hailhq/api/routes/webhooks.py`
- Test: `api/tests/test_webhooks_api.py`

- [ ] **Step 1: Failing test** (mirror how `test_email_domains_api.py` asserts audit rows — query `AuditLog` by `action`):

```python
@pytest.mark.asyncio
async def test_create_subscription_writes_audit_log(client, async_session, webhook_key):
    sub = await _create_subscription(client)
    row = (
        await async_session.execute(
            select(AuditLog).where(
                AuditLog.action == "webhook.create",
                AuditLog.resource_id == UUID(sub["id"]),
            )
        )
    ).scalar_one_or_none()
    assert row is not None
```

- [ ] **Step 2: Run** — FAIL.

- [ ] **Step 3: Implement** — in `api/hailhq/api/routes/webhooks.py`, import `write_audit_log` from `hailhq.api.audit` and add after each successful mutation (each handler already has `principal`):

```python
    await write_audit_log(
        organization_id=principal.organization_id,
        api_key_id=principal.api_key_id,
        action="webhook.create",          # .patch / .delete / .rotate_secret / .redeliver
        resource_type="webhook_subscription",
        resource_id=sub.id,               # delivery routes: resource_id=sub_id, payload={"delivery_id": str(delivery_id)}
        payload={"target_url": sub.target_url},
    )
```

Apply to: `create_subscription` (`webhook.create`), `patch_subscription` (`webhook.patch`), `delete_subscription` (`webhook.delete`), `rotate_secret` (`webhook.rotate_secret`), `redeliver` (`webhook.redeliver`). Keep payloads small — never the secret.

- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit checkpoint (user).** `fix(api): audit-log webhooks CRUD mutations`

---

### Task 19: API typing polish — typed rotate response, dup import, forward_to validation (minors)

**Files:**

- Modify: `core/hailhq/core/schemas.py`, `api/hailhq/api/routes/email_domains.py`, `api/hailhq/api/routes/emails.py`
- Test: `api/tests/test_email_domains_inbound_patch.py`

- [ ] **Step 1: Failing test** (forward_to entries validated at write time):

```python
@pytest.mark.asyncio
async def test_patch_rejects_non_email_forward_targets(client, hail_mail_domain):
    resp = await client.patch(
        f"/email-domains/{hail_mail_domain['id']}",
        json={"inbound_enabled": True, "forward_to": ["not-an-address"]},
    )
    assert resp.status_code == 422
```

(Reuse the module's existing domain-creation fixture/helper.)

- [ ] **Step 2: Implement.**

(a) `core/hailhq/core/schemas.py` — add next to the webhook schemas:

```python
class WebhookSecretResponse(BaseModel):
    """Plaintext webhook secret, returned exactly once (create/rotate)."""

    webhook_secret: str
```

and add a validator on `EmailDomainPatch.forward_to`:

```python
    @field_validator("forward_to")
    @classmethod
    def _forward_to_look_like_addresses(
        cls, v: list[str] | None
    ) -> list[str] | None:
        if v is None:
            return v
        for addr in v:
            local, sep, domain = addr.rpartition("@")
            if not sep or not local or "." not in domain:
                raise ValueError(f"forward_to entry {addr!r} is not an email address")
        return v
```

(b) `api/hailhq/api/routes/email_domains.py` — `rotate_webhook_secret`: import `WebhookSecretResponse`, change decorator to `response_model=WebhookSecretResponse`, return `WebhookSecretResponse(webhook_secret=secret)`, and update the function's return annotation.

(c) `api/hailhq/api/routes/emails.py:22,25` — merge the duplicate `from typing import` lines into one (`from typing import Annotated, Literal`).

- [ ] **Step 3: Run** — `cd api && uv run pytest tests/test_email_domains_inbound_patch.py tests/test_email_domains_api.py -v` → PASS.
- [ ] **Step 4: Commit checkpoint (user).** `fix(api): typed rotate-webhook-secret response; validate forward_to addresses; dedupe typing imports`

---

### Task 20: Regenerate OpenAPI + CLI client (invariant)

Tasks 19 changed response models → the spec must be regenerated in the same PR (CLAUDE.md invariant).

**Files:**

- Modify: `openapi/openapi.yaml`, `cli/internal/client/client.gen.go`

- [ ] **Step 1: Regenerate spec** — start the API (`cd api && uv run uvicorn hailhq.api.main:app --port 8080 &` with dev env), then per docs/contributing.md:

```bash
curl -s http://localhost:8080/openapi.json \
  | python -c "import json, sys, yaml; yaml.safe_dump(json.load(sys.stdin), sys.stdout, sort_keys=False)" \
  > openapi/openapi.yaml
```

Stop the server afterwards. If booting the server is awkward in the execution environment, generate in-process instead:

```bash
cd api && uv run python -c "
import json, yaml
from hailhq.api.main import app
print(yaml.safe_dump(json.loads(json.dumps(app.openapi())), sort_keys=False))
" > ../openapi/openapi.yaml
```

(Match whichever method produced the current committed file — diff should show ONLY the `WebhookSecretResponse` schema + rotate-webhook-secret response change.)

- [ ] **Step 2: Regenerate CLI client** — `cd cli && make codegen && go build ./... && go test ./...` → all green. Fix any compile fallout in `email_domain.go` from the now-typed rotate response (the generated method gains a `JSON200` struct with `WebhookSecret`).

- [ ] **Step 3: Commit checkpoint (user).** `chore(openapi,cli): regenerate spec + client for typed rotate-webhook-secret response`

---

### Task 21: CLI polish — `--json` consistency, dead import crutch, hermetic call test (minors)

**Files:**

- Modify: `cli/internal/cmd/webhooks.go`, `cli/internal/cmd/email_domain.go`, `cli/internal/cmd/call_test.go`
- Test: `cli/internal/cmd/webhooks_test.go`

- [ ] **Step 1: `email_domain.go`** — delete the `var _ = strings.TrimSpace` crutch (~line 363) and remove the `strings` import if now unused. `gofmt -w` the file.

- [ ] **Step 2: `webhooks.go` table output.** Mirror `email_domain.go`'s human-table default + `--json` escape hatch. `list`: render a table (`ID`, `URL`, `EVENTS`, `STATUS`, `FAILURES`) via the same tabwriter pattern `email_domain.go` uses; emit raw JSON when `opts.JSON` is set. `deliveries`: table (`ID`, `EVENT`, `STATUS`, `ATTEMPT`, `NEXT_ATTEMPT`). `create`/`redeliver`: keep JSON output (they return one object including a once-only secret), but route through the existing `printJSON` helper consistently. Read `email_domain.go`'s list command for the exact tabwriter idiom and copy it.

- [ ] **Step 3: Add a table test** asserting `webhooks list` renders the table headers without `--json` and raw JSON with it (same fake-server harness as Task 13).

- [ ] **Step 4: Hermetic fix for the pre-existing flake** — in `cli/internal/cmd/call_test.go` `TestCallSubcommand_MissingAPIKey` (~line 381), add as the first line:

```go
	t.Setenv("HOME", t.TempDir()) // ignore ~/.hail/credentials.json on dev machines
```

- [ ] **Step 5: Run** — `cd cli && gofmt -l . && go vet ./... && go test ./...` → clean, all green (including on a machine with `~/.hail/credentials.json`).
- [ ] **Step 6: Commit checkpoint (user).** `fix(cli): webhooks table output honors --json; drop dead import; hermetic call test`

---

### Task 22: Infra polish — Lambda DLQ, modern S3 policy condition, URL-join comment (minors)

**Files:**

- Modify: `infra/terraform/lambda_ingest.tf`, `infra/terraform/s3_inbound.tf`, `infra/terraform/outputs.tf`, `infra/ses-ingest-lambda/handler.py`, `docs/setup/aws-ses.md`

- [ ] **Step 1: DLQ.** In `lambda_ingest.tf` (adapt resource names/prefix to the file's existing conventions — read it first):

```hcl
# Failed async invokes (Hail API unreachable past Lambda's built-in retries)
# land here instead of vanishing. Raw MIME is already safe in S3; this queue
# preserves the SES notification for replay.
resource "aws_sqs_queue" "ses_ingest_dlq" {
  name                      = "hail-ses-ingest-dlq"
  message_retention_seconds = 1209600 # 14 days
}
```

Add to the lambda function resource:

```hcl
  dead_letter_config {
    target_arn = aws_sqs_queue.ses_ingest_dlq.arn
  }
```

Extend the Lambda role's policy with:

```hcl
    {
      Effect   = "Allow"
      Action   = ["sqs:SendMessage"]
      Resource = aws_sqs_queue.ses_ingest_dlq.arn
    }
```

Add an output in `outputs.tf`:

```hcl
output "ingest_dlq_url" {
  description = "SQS DLQ holding SES notifications the Lambda failed to deliver to the Hail API"
  value       = aws_sqs_queue.ses_ingest_dlq.url
}
```

- [ ] **Step 2: S3 policy condition.** In `s3_inbound.tf`, replace the legacy `aws:Referer = <account_id>` condition on the SES write statement with:

```hcl
        Condition = {
          StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id }
        }
```

(Add the `data "aws_caller_identity" "current" {}` block if `main.tf` doesn't already declare it — check first.)

- [ ] **Step 3: handler.py comment** — above the `HAIL_API_URL.rstrip("/")` line (~56):

```python
    # Ad-hoc join is a deliberate exception to the hailhq.core.urls invariant:
    # the Lambda is stdlib-only (no hailhq import), the URL never crosses into
    # a comparison, and the tfvars/docs both pin "no trailing slash".
```

- [ ] **Step 4: Docs** — in `docs/setup/aws-ses.md`'s inbound section, add one operator line: failed Lambda→API deliveries (API down beyond the 2 async retries) land in the `hail-ses-ingest-dlq` SQS queue; replay by re-driving the message at `POST /internal/ses-events` (raw MIME is still in S3).

- [ ] **Step 5: Validate** — `cd infra/terraform && terraform fmt -check && terraform validate` (run `terraform init -backend=false` first if needed) → clean.
- [ ] **Step 6: Commit checkpoint (user).** `feat(infra): SQS DLQ on ses-ingest lambda; modernize S3 policy to aws:SourceAccount`

---

### Task 23: Full verification sweep + commit hygiene

- [ ] **Step 1: Run everything:**

```bash
cd api && uv run pytest -q                       # API suite
cd ../core && uv run pytest -q                   # core suite
cd ../sdk && uv run pytest -q                    # SDK suite
cd ../cli && gofmt -l . && go vet ./... && go test ./...
cd .. && uv run ruff check api core sdk && uv run black --check api core sdk
uv run mypy api core 2>/dev/null || true         # match whatever CI runs
```

Expected: all green, gofmt/ruff/black clean.

- [ ] **Step 2: OpenAPI drift check** — regenerate per Task 20 Step 1 into a temp file and `diff` against `openapi/openapi.yaml`. Expected: zero drift.

- [ ] **Step 3: Migration round-trip** — `cd api && uv run alembic downgrade 0009 && uv run alembic upgrade head` against the dev DB. Expected: clean both ways.

- [ ] **Step 4: Report to the user:**
  - Suggested final verification summary (suite counts).
  - 🟡 Reminder: `docs/operations/litellm-upstream.md` and `docs/operations/refresh-costs.md` are unrelated to inbound email and should go in a **separate commit** (the round-1 fix plan already called this out).
  - Suggested commit sequence (one per task checkpoint above) — the user commits manually.

---

## Self-review notes

- **Spec coverage:** every review finding maps to a task (table at top). `email.bounced`/`email.complained` emission is intentionally deferred to the SES-events milestone with a docstring note (Task 8) rather than enum churn — flag this to the user as the one consciously-not-implemented item.
- **Type consistency:** `_persist_one → tuple[UUID | None, bool]` (Tasks 1/2/14 all edit it — they are ordered and cumulative); `_enqueue_forwards → list[str]` (Task 8 builds on Task 7's guard); `ProviderAttachment` defined in Task 5, consumed in Task 6; `WebhookSecretResponse` defined in Task 19, consumed in Tasks 19/20.
- **Migration:** single new file `0010` touched by Tasks 3 and 14 (both indexes in one revision); verify `down_revision` matches 0009's actual revision id string before writing.
