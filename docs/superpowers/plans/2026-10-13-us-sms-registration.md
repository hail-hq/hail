# US SMS Registration (A2P 10DLC) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A customer registers their own A2P 10DLC brand and campaign (business or person) with Twilio or Telnyx from the console, API, or MCP; Hail charges the carrier fee from prepaid credit, attaches US numbers on approval, and blocks unregistered US-to-US texts with a 422.

**Architecture:** Reuse the carrier verification framework. `carrier_verifications` gains a `purpose` column (`number` | `sms`). Two new plug-ins (Twilio 10DLC, Telnyx 10DLC) implement the existing `VerificationProvider` contract plus `attach_number`. The existing routes, background pass, console wizard, and MCP tools carry `purpose`. The SMS route gates US-to-US sends from long codes on an approved `sms` row.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy async, Alembic, Pydantic v2, pytest (`uv run pytest`), Twilio Python SDK (`twilio`), httpx (Telnyx), Next.js + vitest (hail-website), Go CLI generated from OpenAPI.

**Spec:** `docs/superpowers/specs/2026-10-13-us-sms-registration-design.md`

## Global Constraints

- Customer-facing text never names a carrier. Error details, docstrings (they become OpenAPI descriptions), and console copy say "the carrier".
- Hail stores state and opaque carrier IDs only. No names, EINs, addresses, phone numbers, or sample messages in the database or logs.
- `api/` must not import carrier SDKs; everything goes through `core/hailhq/core/providers/`.
- Every new env var goes into `.env.example` in the same commit, and each worktree `.env` mirrors `.env.example` line for line.
- After any API route change, regenerate `openapi/openapi.yaml` and the Go client in the same PR.
- Conventional Commits. No `Co-Authored-By` line. Never push to `main`.
- Branch: `feat/us-sms-registration` in a worktree of each repo (hail, and hail-website for Task 11). Run every command from the worktree.
- Tests: `cd api && uv run pytest tests/<file> -q`, `cd core && uv run pytest tests/<file> -q`. Lint: `uv run ruff check . && uv run black --check .` at the repo root.
- Copy follows ASD-STE100: short sentences, active voice, no em dash.

## Review Focus

1. A person submits a Hail number as their mobile. The carrier refuses the OTP. Expected: 422 at submit naming `mobile_phone_number`: "Use your own mobile number, not a Hail number." Test in Task 4.
2. The org has credit for the brand fee but the carrier fails the brand. Expected: row `rejected`, fee kept (carrier keeps it), the reason shown. Test in Task 2.
3. A US toll-free from-number texts a US number with no `sms` row. Expected: not blocked (toll-free is out of scope). Test in Task 6.
4. The org is approved on Twilio and buys a Telnyx US number. Expected: 422 on a US send from the Telnyx number (registration is per carrier). Test in Task 6.
5. The carrier approves the campaign while `attach_number` fails for one number. Expected: row stays `submitted`, next pass retries attach; no duplicate campaign. Test in Task 7.

---

### Task 1: `purpose` column, model, response schema

**Files:**

- Create: `api/migrations/versions/0057_verification_purpose.py`
- Modify: `core/hailhq/core/models.py:1692-1745` (`CarrierVerification`)
- Modify: `core/hailhq/core/schemas.py:2538-2566` (`VerificationResponse`)
- Test: `api/tests/test_verifications_api.py`

**Interfaces:**

- Produces: `CarrierVerification.purpose: str` (`'number'` | `'sms'`), `VerificationResponse.purpose: str`.

- [ ] **Step 1: Write the failing test**

Append to `api/tests/test_verifications_api.py`:

```python
async def test_response_carries_purpose_number_by_default(
    client, org_and_key, carrier
) -> None:
    _, _, key = org_and_key
    resp = await _create(client, key)
    assert resp.status_code == 201, resp.text
    assert resp.json()["purpose"] == "number"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd api && uv run pytest tests/test_verifications_api.py::test_response_carries_purpose_number_by_default -q`
Expected: FAIL with `KeyError: 'purpose'`.

- [ ] **Step 3: Write the migration**

`api/migrations/versions/0057_verification_purpose.py`:

```python
"""Add ``purpose`` to carrier_verifications: 'number' (regulatory bundle
for buying a number) or 'sms' (US A2P 10DLC brand + campaign). The live
uniqueness index includes it so an org can hold one of each per carrier
and country.

Revision ID: 0057
Revises: 0056
"""

from __future__ import annotations

from alembic import op

revision: str = "0057"
down_revision: str | None = "0056"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE carrier_verifications ADD COLUMN purpose TEXT NOT NULL "
        "DEFAULT 'number'"
    )
    op.execute(
        "ALTER TABLE carrier_verifications ADD CONSTRAINT "
        "carrier_verifications_purpose_check CHECK (purpose IN ('number','sms'))"
    )
    op.execute("DROP INDEX IF EXISTS carrier_verifications_live_uniq")
    op.execute(
        "CREATE UNIQUE INDEX carrier_verifications_live_uniq ON "
        "carrier_verifications (organization_id, provider, country_code, "
        "number_type, purpose) WHERE state NOT IN ('cancelled','rejected')"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS carrier_verifications_live_uniq")
    op.execute(
        "CREATE UNIQUE INDEX carrier_verifications_live_uniq ON "
        "carrier_verifications (organization_id, provider, country_code, "
        "number_type) WHERE state NOT IN ('cancelled','rejected')"
    )
    op.execute(
        "ALTER TABLE carrier_verifications DROP CONSTRAINT "
        "carrier_verifications_purpose_check"
    )
    op.execute("ALTER TABLE carrier_verifications DROP COLUMN purpose")
```

- [ ] **Step 4: Update the model**

In `core/hailhq/core/models.py`, class `CarrierVerification`, after `subject_type`:

```python
    purpose: Mapped[str] = mapped_column(
        Text, server_default="number", nullable=False
    )
```

In `__table_args__`, add a check and extend the index:

```python
        CheckConstraint(
            "purpose IN ('number','sms')",
            name="carrier_verifications_purpose_check",
        ),
```

and change the `Index("carrier_verifications_live_uniq", ...)` columns to
`"organization_id", "provider", "country_code", "number_type", "purpose"`.

- [ ] **Step 5: Update the response schema**

In `core/hailhq/core/schemas.py`, class `VerificationResponse`, after `subject_type`:

```python
    purpose: str = Field(
        description=(
            "'number': permission to buy a number in this country. "
            "'sms': registration to text US numbers from a US number."
        )
    )
```

- [ ] **Step 6: Run the test and the whole verification suite**

Run: `cd api && uv run alembic upgrade head && uv run pytest tests/test_verifications_api.py -q`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add api/migrations/versions/0057_verification_purpose.py core/hailhq/core/models.py core/hailhq/core/schemas.py api/tests/test_verifications_api.py
git commit -m "feat(verifications): purpose column (number | sms)"
```

---

### Task 2: Routes carry `purpose`; registry keyed by purpose; fee debit at creation

**Files:**

- Modify: `core/hailhq/core/providers/verification/__init__.py`
- Modify: `core/hailhq/core/providers/verification/base.py` (add `SmsRegistrationProvider`)
- Create: `core/hailhq/core/sms_registration_fees.py`
- Create: `costs/sms-registration.json`
- Modify: `api/hailhq/api/routes/verifications.py`
- Test: `api/tests/test_verifications_api.py`, `core/tests/test_sms_registration_fees.py`

**Interfaces:**

- Consumes: `CarrierVerification.purpose` (Task 1).
- Produces:
  - `register_verification_provider(name, factory, purpose="number")`, `get_verification_provider(name, purpose="number")`, `default_verification_provider_name(purpose="number")`.
  - `Registry = Callable[[str, str], VerificationProvider | None]` (name, purpose).
  - `class SmsRegistrationProvider(VerificationProvider)` with `async def attach_number(self, refs: dict, *, provider_resource_id: str, e164: str) -> None` and `create_draft(..., messaging_service_sid: str | None = None)`.
  - `sms_registration_fees.filing_fee_cents(provider: str, subject_type: str) -> int` and `monthly_fee_cents(provider, subject_type) -> int`.
  - `POST /verifications` part `purpose` (default `number`); `GET /verifications/requirements?purpose=`; `GET /verifications?purpose=` filter.

- [ ] **Step 1: Write the failing fee tests**

`core/tests/test_sms_registration_fees.py`:

```python
from hailhq.core import sms_registration_fees as fees


def test_twilio_business_filing_fee_is_brand_plus_campaign_vetting() -> None:
    # 46.00 brand (with vetting) + 15.00 campaign vetting
    assert fees.filing_fee_cents("twilio", "business") == 6100


def test_twilio_person_filing_fee() -> None:
    # 4.50 brand + 15.00 campaign vetting
    assert fees.filing_fee_cents("twilio", "person") == 1950


def test_monthly_fee() -> None:
    assert fees.monthly_fee_cents("twilio", "business") == 1000
    assert fees.monthly_fee_cents("twilio", "person") == 200


def test_unknown_provider_raises() -> None:
    import pytest

    with pytest.raises(KeyError):
        fees.filing_fee_cents("didww", "person")
```

- [ ] **Step 2: Run to verify failure**

Run: `cd core && uv run pytest tests/test_sms_registration_fees.py -q`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Create the fee file and module**

`costs/sms-registration.json`:

```json
{
  "version": 1,
  "license": "CC-BY-4.0",
  "note": "Carrier fees for US A2P 10DLC registration, USD, passed through at cost. Refresh with the weekly cost job.",
  "providers": [
    {
      "provider": "twilio",
      "last_verified": "2026-10-13",
      "source_url": "https://support.twilio.com/hc/en-us/articles/1260803965530",
      "person": {
        "brand_usd": "4.50",
        "campaign_vetting_usd": "15.00",
        "campaign_monthly_usd": "2.00"
      },
      "business": {
        "brand_usd": "46.00",
        "campaign_vetting_usd": "15.00",
        "campaign_monthly_usd": "10.00"
      }
    },
    {
      "provider": "telnyx",
      "last_verified": "2026-10-13",
      "source_url": "https://telnyx.com/pricing/messaging",
      "person": {
        "brand_usd": "4.50",
        "campaign_vetting_usd": "15.00",
        "campaign_monthly_usd": "2.00"
      },
      "business": {
        "brand_usd": "46.00",
        "campaign_vetting_usd": "15.00",
        "campaign_monthly_usd": "10.00"
      }
    }
  ]
}
```

Before committing, open https://telnyx.com/pricing/messaging and replace the Telnyx numbers with the fees on that page (TCR fees are the same at every carrier; Telnyx adds no markup as of 2026-10). If the page shows different values, use the page.

`core/hailhq/core/sms_registration_fees.py`:

```python
"""Carrier fees for US A2P 10DLC registration, read from
``costs/sms-registration.json`` and passed through to the customer at cost."""

from __future__ import annotations

import json
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from hailhq.core.telephony_catalog import catalog_dir

_FILE = "sms-registration.json"


@lru_cache(maxsize=1)
def _load() -> dict[str, dict]:
    path = Path(catalog_dir()) / _FILE
    data = json.loads(path.read_text())
    return {row["provider"]: row for row in data["providers"]}


def _cents(value: str) -> int:
    return int((Decimal(value) * 100).to_integral_value())


def filing_fee_cents(provider: str, subject_type: str) -> int:
    """One-time fee charged when the registration is filed: brand plus
    campaign vetting. Raises KeyError for a carrier with no SMS registration."""
    row = _load()[provider][subject_type]
    return _cents(row["brand_usd"]) + _cents(row["campaign_vetting_usd"])


def monthly_fee_cents(provider: str, subject_type: str) -> int:
    row = _load()[provider][subject_type]
    return _cents(row["campaign_monthly_usd"])
```

Check `core/hailhq/core/telephony_catalog.py` for the name of the function that returns the catalog directory (it reads `HAIL_TELEPHONY_CATALOG_DIR`). If it is not `catalog_dir`, use the real name. The test `pinned_catalog` fixture in `api/tests/test_verifications_api.py` writes a temp dir; add `sms-registration.json` there in Step 6.

- [ ] **Step 4: Run fee tests**

Run: `cd core && uv run pytest tests/test_sms_registration_fees.py -q`
Expected: PASS.

- [ ] **Step 5: Extend the registry and the base contract**

In `core/hailhq/core/providers/verification/base.py`, after `VerificationProvider`:

```python
class SmsRegistrationProvider(VerificationProvider):
    """A carrier's US A2P 10DLC registration, driven through the same
    lifecycle as number verification. ``purpose`` is 'sms'.

    ``create_draft`` takes one extra keyword: the organization's messaging
    service (Twilio) or messaging profile (Telnyx) id, when one exists.
    ``status`` creates the campaign once the brand is approved and reports
    'approved' only when the campaign is approved. ``attach_number`` puts
    one of the organization's US long codes on the approved campaign."""

    purpose = "sms"

    @abstractmethod
    async def attach_number(
        self, refs: dict, *, provider_resource_id: str, e164: str
    ) -> None:
        """Attach a number to the approved campaign. Idempotent."""
```

Add `"SmsRegistrationProvider"` to `__all__` in `base.py` and `__init__.py`.

In `core/hailhq/core/providers/verification/__init__.py`, key the registry by `(name, purpose)`:

```python
_FACTORIES: dict[tuple[str, str], Callable[[], VerificationProvider]] = {}
_INSTANCES: dict[tuple[str, str], VerificationProvider] = {}


def register_verification_provider(
    name: str, factory: Callable[[], VerificationProvider], purpose: str = "number"
) -> None:
    _FACTORIES[(name, purpose)] = factory
    _INSTANCES.pop((name, purpose), None)


def get_verification_provider(
    name: str, purpose: str = "number"
) -> VerificationProvider | None:
    key = (name, purpose)
    instance = _INSTANCES.get(key)
    if instance is not None:
        return instance
    factory = _FACTORIES.get(key)
    if factory is None:
        return None
    try:
        instance = factory()
    except ValueError:
        return None
    _INSTANCES[key] = instance
    return instance


def default_verification_provider_name(purpose: str = "number") -> str | None:
    return next(
        (n for (n, p) in _FACTORIES if p == purpose and get_verification_provider(n, p)),
        None,
    )
```

Keep `_register_builtin()` as is (purpose defaults to `number`). Tasks 4 and 5 add the `sms` registrations.

Run: `grep -rn "get_verification_provider\|default_verification_provider_name" --include=*.py api core mcp | grep -v tests` and update every caller to pass the purpose where it is known (number purchase paths pass nothing: default `number`).

- [ ] **Step 6: Write the failing route tests**

Append to `api/tests/test_verifications_api.py`. First extend the fakes and fixtures:

```python
from hailhq.core.providers.verification import SmsRegistrationProvider
from hailhq.core.models import AccountCredit


class FakeSmsCarrier(SmsRegistrationProvider):
    name = "fake"

    def __init__(self) -> None:
        self.remote_status = ProviderStatus(state="pending")
        self.drafts: list[dict] = []
        self.submitted: list[dict] = []
        self.discarded: list[dict] = []
        self.attached: list[tuple[dict, str, str]] = []
        self.attach_error: Exception | None = None
        self.problems: list[Problem] = []

    async def requirements(self, country_code, number_type, subject_type):
        return Requirements(
            provider=self.name,
            country_code=country_code,
            number_type=number_type,
            subject_type=subject_type,
            required=True,
            fields=(
                FieldSpec(name="legal_name", label="Legal name"),
                FieldSpec(name="campaign_description", label="What you send"),
            ),
            address_required=True,
            subject_types=("person", "business"),
        )

    async def create_draft(self, *, messaging_service_sid=None, **kwargs):
        self.drafts.append({**kwargs, "messaging_service_sid": messaging_service_sid})
        if self.problems:
            return DraftResult(refs={}, problems=self.problems)
        return DraftResult(refs={"brand_sid": f"BN{len(self.drafts)}"})

    async def check(self, refs):
        return []

    async def submit(self, refs):
        self.submitted.append(refs)

    async def status(self, refs):
        return self.remote_status

    async def purchase_handle(self, refs):
        return {}

    async def discard(self, refs):
        self.discarded.append(refs)

    async def attach_number(self, refs, *, provider_resource_id, e164):
        if self.attach_error:
            raise self.attach_error
        self.attached.append((refs, provider_resource_id, e164))


@pytest.fixture()
def sms_carrier():
    fake = FakeSmsCarrier()
    number_fake = FakeCarrier()

    def registry(name, purpose="number"):
        if name != "fake":
            return None
        return fake if purpose == "sms" else number_fake

    app.dependency_overrides[get_verification_registry] = lambda: registry
    verifications_routes._last_polled.clear()
    app.dependency_overrides[get_default_provider_name] = lambda: "fake"
    yield fake
    app.dependency_overrides.pop(get_verification_registry, None)
    app.dependency_overrides.pop(get_default_provider_name, None)
```

Update the existing `carrier` fixture's registry lambda to accept `(name, purpose="number")`.

In `pinned_catalog`, also write:

```python
    (tmp_path / "sms-registration.json").write_text(json.dumps({
        "version": 1, "license": "CC-BY-4.0", "providers": [{
            "provider": "fake", "last_verified": "2026-10-13", "source_url": "",
            "person": {"brand_usd": "4.50", "campaign_vetting_usd": "15.00", "campaign_monthly_usd": "2.00"},
            "business": {"brand_usd": "46.00", "campaign_vetting_usd": "15.00", "campaign_monthly_usd": "10.00"},
        }]}))
```

and call `sms_registration_fees._load.cache_clear()` before and after (import the module).

Then the tests:

```python
def _sms_submission(**overrides) -> dict:
    data = {
        "provider": "fake",
        "purpose": "sms",
        "country_code": "US",
        "number_type": "local",
        "subject_type": "business",
        "fields": json.dumps({"legal_name": "Acme", "campaign_description": "Order updates"}),
        "address": json.dumps({
            "customer_name": "Acme", "street": "1 Main St", "city": "Austin",
            "region": "TX", "postal_code": "78701", "country_code": "US",
        }),
    }
    data.update(overrides)
    return data


async def _fund(async_session, org_id, cents: int) -> None:
    async_session.add(AccountCredit(
        organization_id=org_id, kind="credit", channel="credit",
        amount_cents=cents, qty=1, ref=f"test-topup-{cents}", source="test",
    ))
    await async_session.commit()


async def _balance(async_session, org_id) -> int:
    from sqlalchemy import func
    total = (await async_session.execute(
        select(func.coalesce(func.sum(AccountCredit.amount_cents), 0))
        .where(AccountCredit.organization_id == org_id)
    )).scalar_one()
    return int(total)


async def test_sms_registration_requires_purpose_sms_provider(
    client, org_and_key, carrier
) -> None:
    # Only the number plug-in is registered: purpose=sms is 404.
    _, _, key = org_and_key
    resp = await client.post("/verifications", data=_sms_submission(), headers=_auth(key))
    assert resp.status_code == 404, resp.text


async def test_sms_registration_debits_filing_fee(
    client, org_and_key, sms_carrier, async_session
) -> None:
    org_id, _, key = org_and_key
    await _fund(async_session, org_id, 10_000)
    resp = await client.post("/verifications", data=_sms_submission(), headers=_auth(key))
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["purpose"] == "sms"
    assert body["state"] == "submitted"
    assert await _balance(async_session, org_id) == 10_000 - 6100
    debit = (await async_session.execute(
        select(AccountCredit).where(AccountCredit.ref == f"sms_registration:{body['id']}")
    )).scalar_one()
    assert debit.channel == "sms" and int(debit.amount_cents) == -6100


async def test_sms_registration_402_when_balance_too_low(
    client, org_and_key, sms_carrier, async_session
) -> None:
    org_id, _, key = org_and_key
    await _fund(async_session, org_id, 5_000)
    resp = await client.post("/verifications", data=_sms_submission(), headers=_auth(key))
    assert resp.status_code == 402, resp.text
    assert sms_carrier.drafts == []
    assert await _balance(async_session, org_id) == 5_000


async def test_sms_registration_rejected_brand_keeps_fee(
    client, org_and_key, sms_carrier, async_session
) -> None:
    org_id, _, key = org_and_key
    await _fund(async_session, org_id, 10_000)
    vid = (await client.post("/verifications", data=_sms_submission(), headers=_auth(key))).json()["id"]
    sms_carrier.remote_status = ProviderStatus(state="rejected", reason="EIN does not match")
    verifications_routes._last_polled.clear()
    resp = await client.get(f"/verifications/{vid}", headers=_auth(key))
    assert resp.json()["state"] == "rejected"
    assert resp.json()["rejection_reason"] == "EIN does not match"
    assert await _balance(async_session, org_id) == 10_000 - 6100


async def test_sms_and_number_rows_coexist(
    client, org_and_key, sms_carrier, async_session
) -> None:
    org_id, _, key = org_and_key
    await _fund(async_session, org_id, 10_000)
    assert (await client.post("/verifications", data=_sms_submission(), headers=_auth(key))).status_code == 201
    assert (await client.post("/verifications", data=_sms_submission(), headers=_auth(key))).status_code == 409
    resp = await client.get("/verifications", params={"purpose": "sms"}, headers=_auth(key))
    assert [r["purpose"] for r in resp.json()] == ["sms"]


async def test_sms_requirements_endpoint(client, org_and_key, sms_carrier) -> None:
    _, _, key = org_and_key
    resp = await client.get(
        "/verifications/requirements",
        params={"country_code": "US", "number_type": "local", "purpose": "sms",
                "subject_type": "business", "provider": "fake"},
        headers=_auth(key),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["address_required"] is True
```

- [ ] **Step 7: Run to verify failure**

Run: `cd api && uv run pytest tests/test_verifications_api.py -q -k "sms"`
Expected: FAIL (404 or KeyError on `purpose`).

- [ ] **Step 8: Implement the route changes**

In `api/hailhq/api/routes/verifications.py`:

1. `Registry = Callable[[str, str], VerificationProvider | None]`. `get_verification_registry` returns `get_verification_provider` (it now takes `(name, purpose)`). `get_default_provider_name` stays for `number`; add:

```python
def get_default_sms_provider_name() -> str | None:
    return default_verification_provider_name("sms")
```

2. `_provider_or_404(registry, name, purpose)` passes purpose through. Every `registry(row.provider)` call becomes `registry(row.provider, row.purpose)`. In `_fetch_status`, `_retry_submit`, `_reject_row`, `cancel_verification`, `admin_approve`, `approved_purchase_handle` (purpose `"number"`).

3. `PURPOSES = {"number", "sms"}`. Add `purpose` to `_Submission` (default `"number"`), parse it with `_text_part(form, "purpose", "number")`, and reject other values with `unprocessable("unknown purpose", loc=["body","purpose"])`. For `purpose == "sms"`: `country_code` must be `US` and `number_type` must be `local`, else 422 `loc=["body","country_code"]` "SMS registration is for US local numbers".

4. `get_requirements` takes `purpose: str = "number"` and uses `get_default_sms_provider_name` when `purpose == "sms"` and no provider is given. Inject both defaults as dependencies.

5. `create_verification`, `existing` query adds `CarrierVerification.purpose == sub.purpose`. The row gets `purpose=sub.purpose`.

6. Fee: before `provider.create_draft`, when `sub.purpose == "sms"`:

```python
    fee_cents = 0
    if sub.purpose == "sms":
        try:
            fee_cents = sms_registration_fees.filing_fee_cents(provider_name, sub.subject_type)
        except KeyError:
            raise HTTPException(status_code=404, detail="no SMS registration for this carrier")
        if principal.auth_kind != "shared":
            balance = await get_balance_cents(db, principal.organization_id)
            if balance < fee_cents:
                raise HTTPException(
                    status_code=http_status.HTTP_402_PAYMENT_REQUIRED,
                    detail=f"filing costs ${fee_cents/100:.2f}; add credit first",
                )
```

Import `get_balance_cents` from `hailhq.core.billing` and `sms_registration_fees` from `hailhq.core`.

7. Pass the messaging service id when `purpose == "sms"`:

```python
    draft_kwargs = {}
    if sub.purpose == "sms":
        draft_kwargs["messaging_service_sid"] = (
            await db.execute(
                select(PhoneNumber.messaging_service_sid).where(
                    PhoneNumber.organization_id == principal.organization_id,
                    PhoneNumber.provider == provider_name,
                    PhoneNumber.messaging_service_sid.is_not(None),
                ).limit(1)
            )
        ).scalar_one_or_none()
    draft = await provider.create_draft(..., **draft_kwargs)
```

8. After `db.add(row)` and before `db.commit()`, when `fee_cents`:

```python
    if fee_cents:
        db.add(AccountCredit(
            organization_id=principal.organization_id,
            kind="debit", channel="sms", amount_cents=-fee_cents, qty=1,
            ref=f"sms_registration:{row.id}", source="sms_registration_fee",
        ))
```

`row.id` is a server default; call `await db.flush()` after `db.add(row)` to get it. The audit payload for `verification.submit` gains `"fee_cents": fee_cents` when purpose is `sms` (pass it through `_submit_row` as a new optional `extra: dict | None` merged into the payload).

9. `list_verifications` takes `purpose: str | None = None` and filters when given.

- [ ] **Step 8b: Fee read endpoint for the console**

Test, appended to `api/tests/test_verifications_api.py`:

```python
async def test_sms_fees_endpoint(client, org_and_key, sms_carrier) -> None:
    _, _, key = org_and_key
    resp = await client.get("/verifications/sms-fees", headers=_auth(key))
    assert resp.status_code == 200, resp.text
    assert resp.json()["fake"] == {
        "person": {"filing_cents": 1950, "monthly_cents": 200},
        "business": {"filing_cents": 6100, "monthly_cents": 1000},
    }
```

Route, in `verifications.py` before the `/{verification_id}` routes (so the path is not read as an id):

```python
class SmsFee(BaseModel):
    filing_cents: int = Field(description="Charged once when the registration is filed.")
    monthly_cents: int = Field(description="Charged each month while approved.")


@router.get(
    "/sms-fees",
    response_model=dict[str, dict[str, SmsFee]],
    operation_id="get_sms_registration_fees",
)
async def get_sms_fees(
    principal: Annotated[Principal, Depends(get_current_principal)],
) -> dict[str, dict[str, SmsFee]]:
    """Carrier fees for US SMS registration, per carrier and subject type,
    in cents. Passed through at cost."""
    out: dict[str, dict[str, SmsFee]] = {}
    for provider in sms_registration_fees.providers():
        out[provider] = {
            subject: SmsFee(
                filing_cents=sms_registration_fees.filing_fee_cents(provider, subject),
                monthly_cents=sms_registration_fees.monthly_fee_cents(provider, subject),
            )
            for subject in ("person", "business")
        }
    return out
```

Add to `sms_registration_fees.py`:

```python
def providers() -> list[str]:
    return list(_load())
```

- [ ] **Step 9: Run the full verification suite**

Run: `cd api && uv run pytest tests/test_verifications_api.py tests/test_numbers_api.py -q`
Expected: PASS. If `test_numbers_api.py` does not exist, run `uv run pytest tests -q -k "number or verif"`.

- [ ] **Step 10: Lint and commit**

```bash
uv run ruff check . && uv run black --check . || uv run black .
git add core/hailhq/core/providers/verification/__init__.py core/hailhq/core/providers/verification/base.py core/hailhq/core/sms_registration_fees.py costs/sms-registration.json core/tests/test_sms_registration_fees.py api/hailhq/api/routes/verifications.py api/tests/test_verifications_api.py
git commit -m "feat(verifications): purpose=sms rows, registry by purpose, filing fee debit"
```

---

### Task 3: Shared 10DLC form definition and campaign text builder

**Files:**

- Create: `core/hailhq/core/providers/verification/sms_form.py`
- Test: `core/tests/test_sms_form.py`

**Interfaces:**

- Produces:
  - `sms_requirements(provider: str, subject_type: SubjectType) -> Requirements`
  - `campaign_text(fields: dict[str, str], subject_type: SubjectType) -> CampaignText` where `CampaignText` has `description: str`, `message_flow: str`, `samples: list[str]`, `has_links: bool`, `use_case: str` (`"MIXED"` or `"SOLE_PROPRIETOR"`).
  - Field names (both carriers use them): business: `legal_name`, `business_type` (options `PRIVATE_PROFIT`, `PUBLIC_PROFIT`, `NON_PROFIT`, `GOVERNMENT`), `ein`, `industry` (options from `_VERTICALS`), `website`, `rep_name`, `rep_email`, `rep_phone`, `rep_title`; person: `first_name`, `last_name`, `email`, `mobile_phone_number`; both: `campaign_description`, `sample_1`, `sample_2`, `opt_in_description`, `has_links` (options `YES`/`NO`).

- [ ] **Step 1: Write the failing tests**

`core/tests/test_sms_form.py`:

```python
import pytest
from hailhq.core.providers.verification.sms_form import campaign_text, sms_requirements


def test_business_form_lists_company_and_campaign_fields() -> None:
    req = sms_requirements("twilio", "business")
    names = [f.name for f in req.fields]
    assert names[:4] == ["legal_name", "business_type", "ein", "industry"]
    assert "campaign_description" in names and "sample_2" in names
    assert req.address_required is True
    assert req.documents == ()
    assert req.subject_types == ("person", "business")


def test_person_form_asks_for_mobile_not_ein() -> None:
    req = sms_requirements("telnyx", "person")
    names = [f.name for f in req.fields]
    assert "mobile_phone_number" in names and "ein" not in names
    mobile = next(f for f in req.fields if f.name == "mobile_phone_number")
    assert "not a Hail number" in mobile.help


def test_campaign_text_pads_short_values_to_carrier_minimums() -> None:
    text = campaign_text(
        {"campaign_description": "Order updates", "sample_1": "Hi", "sample_2": "Bye",
         "opt_in_description": "Web form", "has_links": "NO"},
        "business",
    )
    assert len(text.description) >= 40
    assert len(text.message_flow) >= 40
    assert all(len(s) >= 20 for s in text.samples)
    assert text.use_case == "MIXED" and text.has_links is False


def test_person_use_case() -> None:
    assert campaign_text({"campaign_description": "x", "sample_1": "x", "sample_2": "x",
                          "opt_in_description": "x", "has_links": "YES"}, "person").use_case == "SOLE_PROPRIETOR"


@pytest.mark.parametrize("field", ["sample_1", "campaign_description"])
def test_missing_required_field_raises(field) -> None:
    data = {"campaign_description": "x", "sample_1": "x", "sample_2": "x",
            "opt_in_description": "x", "has_links": "NO"}
    data.pop(field)
    with pytest.raises(KeyError):
        campaign_text(data, "business")
```

- [ ] **Step 2: Run to verify failure**

Run: `cd core && uv run pytest tests/test_sms_form.py -q`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

`core/hailhq/core/providers/verification/sms_form.py`:

```python
"""The form and fixed campaign text for US A2P 10DLC registration. Both
carrier plug-ins use it, so the customer sees one form whatever the carrier.
Carrier words stay out of labels and help."""

from __future__ import annotations

from dataclasses import dataclass

from hailhq.core.providers.verification.base import (
    FieldOption,
    FieldSpec,
    Requirements,
    SubjectType,
)

_BUSINESS_TYPES = (
    FieldOption(key="PRIVATE_PROFIT", label="Private company"),
    FieldOption(key="PUBLIC_PROFIT", label="Publicly traded company"),
    FieldOption(key="NON_PROFIT", label="Non-profit"),
    FieldOption(key="GOVERNMENT", label="Government"),
)
_VERTICALS = (
    ("PROFESSIONAL", "Professional services"),
    ("REAL_ESTATE", "Real estate"),
    ("HEALTHCARE", "Healthcare"),
    ("HUMAN_RESOURCES", "Human resources"),
    ("ENERGY", "Energy"),
    ("ENTERTAINMENT", "Entertainment"),
    ("RETAIL", "Retail"),
    ("TRANSPORTATION", "Transportation"),
    ("AGRICULTURE", "Agriculture"),
    ("INSURANCE", "Insurance"),
    ("POSTAL", "Postal"),
    ("EDUCATION", "Education"),
    ("HOSPITALITY", "Hospitality"),
    ("FINANCIAL", "Financial"),
    ("POLITICAL", "Political"),
    ("GAMBLING", "Gambling"),
    ("LEGAL", "Legal"),
    ("CONSTRUCTION", "Construction"),
    ("NGO", "Non-governmental organization"),
    ("MANUFACTURING", "Manufacturing"),
    ("GOVERNMENT", "Government"),
    ("TECHNOLOGY", "Technology"),
    ("COMMUNICATION", "Communication"),
)
_YES_NO = (FieldOption(key="YES", label="Yes"), FieldOption(key="NO", label="No"))

_BUSINESS_FIELDS = (
    FieldSpec(name="legal_name", label="Legal company name"),
    FieldSpec(name="business_type", label="Company type", options=_BUSINESS_TYPES),
    FieldSpec(name="ein", label="US employer ID (EIN)", pattern=r"^\d{2}-?\d{7}$",
              help="Nine digits, as on your IRS letter."),
    FieldSpec(name="industry", label="Industry",
              options=tuple(FieldOption(key=k, label=v) for k, v in _VERTICALS)),
    FieldSpec(name="website", label="Website", kind="url",
              help="The carrier checks that the site names your company."),
    FieldSpec(name="rep_name", label="Contact person name"),
    FieldSpec(name="rep_email", label="Contact person email", kind="email"),
    FieldSpec(name="rep_phone", label="Contact person phone", kind="phone"),
    FieldSpec(name="rep_title", label="Contact person job title"),
)
_PERSON_FIELDS = (
    FieldSpec(name="first_name", label="First name"),
    FieldSpec(name="last_name", label="Last name"),
    FieldSpec(name="email", label="Email", kind="email"),
    FieldSpec(
        name="mobile_phone_number",
        label="Your mobile number",
        kind="phone",
        help=(
            "The carrier texts this number. Reply YES to confirm. Use your own "
            "mobile, not a Hail number. A mobile can confirm three registrations "
            "in its lifetime."
        ),
    ),
)
_CAMPAIGN_FIELDS = (
    FieldSpec(name="campaign_description", label="What you send",
              help="One or two sentences. Example: appointment reminders and replies to customer questions."),
    FieldSpec(name="sample_1", label="Sample message 1"),
    FieldSpec(name="sample_2", label="Sample message 2"),
    FieldSpec(name="opt_in_description", label="How people agree to receive texts",
              help="Example: they enter their number on our website and tick a box."),
    FieldSpec(name="has_links", label="Do your texts contain links?", options=_YES_NO),
)


def sms_requirements(provider: str, subject_type: SubjectType) -> Requirements:
    subject = _PERSON_FIELDS if subject_type == "person" else _BUSINESS_FIELDS
    return Requirements(
        provider=provider,
        country_code="US",
        number_type="local",
        subject_type=subject_type,
        required=True,
        fields=subject + _CAMPAIGN_FIELDS,
        documents=(),
        address_required=True,
        subject_types=("person", "business"),
    )


@dataclass(frozen=True)
class CampaignText:
    use_case: str
    description: str
    message_flow: str
    samples: list[str]
    has_links: bool


_OPT_OUT = " Reply STOP to opt out, HELP for help."


def _at_least(text: str, n: int, pad: str) -> str:
    text = text.strip()
    return text if len(text) >= n else (text + " " + pad).strip()


def campaign_text(fields: dict[str, str], subject_type: SubjectType) -> CampaignText:
    """Carrier minimums: description 40+, message flow 40+, samples 20+."""
    description = _at_least(
        fields["campaign_description"], 40,
        "Messages are sent only to people who agreed to receive them.",
    )
    flow = _at_least(
        "Recipients opt in as follows: " + fields["opt_in_description"], 40,
        "They can reply STOP at any time to opt out.",
    )
    samples = [_at_least(fields["sample_1"], 20, _OPT_OUT), _at_least(fields["sample_2"], 20, _OPT_OUT)]
    return CampaignText(
        use_case="SOLE_PROPRIETOR" if subject_type == "person" else "MIXED",
        description=description,
        message_flow=flow,
        samples=samples,
        has_links=fields["has_links"].upper() == "YES",
    )
```

- [ ] **Step 4: Run tests**

Run: `cd core && uv run pytest tests/test_sms_form.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add core/hailhq/core/providers/verification/sms_form.py core/tests/test_sms_form.py
git commit -m "feat(verifications): shared 10DLC form and campaign text"
```

---

### Task 4: Twilio 10DLC plug-in

**Files:**

- Create: `core/hailhq/core/providers/verification/twilio_sms.py`
- Modify: `core/hailhq/core/providers/verification/__init__.py` (`_register_builtin`)
- Test: `core/tests/test_twilio_sms_registration.py`

**Interfaces:**

- Consumes: `SmsRegistrationProvider` (Task 2), `sms_requirements`, `campaign_text` (Task 3).
- Produces: `TwilioSmsRegistrationProvider(account_sid=None, auth_token=None, client=None)` registered as `("twilio", "sms")`. `refs` keys: `profile_sid`, `trust_sid`, `brand_sid`, `messaging_service_sid`, `campaign_sid`, `subject_type`, `campaign` (the fixed campaign text, no personal data), `otp_sent_at`.

Twilio facts used here (from the ISV guides, 2026-10):

- Secondary customer profile policy SID (business): `RNdfbf3fae0e1107f8aded0e7cead80bf5`. Starter profile policy SID (person): `RN806dd6cd175f314e1f96a9727ee271f4`.
- A2P trust product policy SID (business): `RNb0d4771c2c98518d916a3d4cd70a8f8b`. Sole proprietor trust product policy SID (person): `RN670d5d2e282a6130ae063b234b6019c8`.
- End user types: business: `customer_profile_business_information`, `authorized_representative_1`; a2p: `us_a2p_messaging_profile_information`; person: `sole_proprietor_information` (`brand_name`, `mobile_phone_number`, `vertical`).
- Brand: `client.messaging.v1.a2p.brand_registrations.create(customer_profile_bundle_sid=..., a2p_profile_bundle_sid=..., brand_type="SOLE_PROPRIETOR" for person)`. Brand `status` in `PENDING | APPROVED | FAILED | SUSPENDED`; `identity_status` in `UNVERIFIED | VERIFIED`. Person OTP: `brand_registrations(sid).brand_registration_otps.create()` (REST `POST /v1/a2p/BrandRegistrations/{sid}/SmsOtp`).
- Campaign: `client.messaging.v1.services(MG).us_app_to_person.create(brand_registration_sid=..., description=..., message_flow=..., message_samples=[...], us_app_to_person_usecase=..., has_embedded_links=..., has_embedded_phone=False)`. `campaign_status` in `PENDING | IN_PROGRESS | VERIFIED | FAILED`; `errors` carries the reason.
- Messaging service: one per org, created by `hailhq.core.providers.sms.twilio` `ensure_messaging_service`. Numbers in the service are covered by its campaign (`attach_number` is `ensure` of the number in the service, which `sms_setup.ensure_sms` already does; the plug-in re-checks membership and adds the number when missing). A person's service may hold one number.

Before writing code, open `cli/../core/hailhq/core/providers/verification/twilio.py` lines 273-410 and copy its pattern for `asyncio.to_thread` around SDK calls and for mapping `TwilioRestException` to `VerificationProviderError`.

- [ ] **Step 1: Write the failing tests**

`core/tests/test_twilio_sms_registration.py` uses a hand-rolled fake Twilio client that records calls and returns `SimpleNamespace` objects. Write:

```python
from types import SimpleNamespace

import pytest
from hailhq.core.providers.verification.base import Address
from hailhq.core.providers.verification.twilio_sms import TwilioSmsRegistrationProvider


class Recorder:
    """A fake twilio.rest.Client: every attribute path records calls and
    returns canned objects from ``responses`` keyed by the last attribute."""

    def __init__(self, responses: dict):
        self.calls: list[tuple[str, dict]] = []
        self.responses = responses
        self._path: list[str] = []

    def __getattr__(self, name):
        self._path.append(name)
        return self

    def __call__(self, *args, **kwargs):
        if args and not kwargs:  # resource(sid) style
            self._path.append(f"({args[0]})")
            return self
        name = ".".join(self._path)
        self._path = []
        self.calls.append((name, kwargs))
        key = name.rsplit(".", 1)[-1]
        resp = self.responses.get(name) or self.responses.get(key)
        return resp() if callable(resp) else resp


def _ok_responses():
    return {
        "customer_profiles.create": SimpleNamespace(sid="BU1"),
        "end_users.create": SimpleNamespace(sid="IT1"),
        "addresses.create": SimpleNamespace(sid="AD1"),
        "supporting_documents.create": SimpleNamespace(sid="RD1"),
        "customer_profiles_entity_assignments.create": SimpleNamespace(sid="BV1"),
        "customer_profiles_evaluations.create": SimpleNamespace(status="compliant", results=[]),
        "customer_profiles.update": SimpleNamespace(sid="BU1", status="pending-review"),
        "trust_products.create": SimpleNamespace(sid="BU2"),
        "trust_products_entity_assignments.create": SimpleNamespace(sid="BV2"),
        "trust_products_evaluations.create": SimpleNamespace(status="compliant", results=[]),
        "trust_products.update": SimpleNamespace(sid="BU2", status="pending-review"),
        "brand_registrations.create": SimpleNamespace(sid="BN1", status="PENDING", identity_status="UNVERIFIED"),
        "services.create": SimpleNamespace(sid="MG1"),
    }


FIELDS = {
    "legal_name": "Acme Inc", "business_type": "PRIVATE_PROFIT", "ein": "12-3456789",
    "industry": "RETAIL", "website": "https://acme.example", "rep_name": "Ada Lovelace",
    "rep_email": "ada@acme.example", "rep_phone": "+15125550100", "rep_title": "CEO",
    "campaign_description": "Order updates and delivery notices for customers who ordered on our site.",
    "sample_1": "Acme: your order 123 shipped. Reply STOP to opt out.",
    "sample_2": "Acme: your order 123 was delivered. Reply STOP to opt out.",
    "opt_in_description": "Customers tick a box at checkout on acme.example.",
    "has_links": "NO",
}
ADDRESS = Address(customer_name="Acme Inc", street="1 Main St", city="Austin",
                  region="TX", postal_code="78701", country_code="US")


def _provider(responses=None):
    return TwilioSmsRegistrationProvider(account_sid="AC1", auth_token="t", client=Recorder(responses or _ok_responses()))


async def test_business_draft_creates_profile_trust_and_brand() -> None:
    p = _provider()
    req = await p.requirements("US", "local", "business")
    draft = await p.create_draft(
        organization_id="org1", contact_email="ops@hail.so", requirements=req,
        fields=FIELDS, address=ADDRESS, documents={}, messaging_service_sid="MG9",
    )
    assert draft.problems == []
    assert draft.refs["profile_sid"] == "BU1"
    assert draft.refs["trust_sid"] == "BU2"
    assert draft.refs["brand_sid"] == "BN1"
    assert draft.refs["messaging_service_sid"] == "MG9"
    assert draft.refs["subject_type"] == "business"
    names = [n for n, _ in p._client.calls]
    assert "trusthub.v1.customer_profiles.create" in names
    brand = next(kw for n, kw in p._client.calls if n.endswith("brand_registrations.create"))
    assert brand["customer_profile_bundle_sid"] == "BU1"
    assert brand["a2p_profile_bundle_sid"] == "BU2"
    assert "brand_type" not in brand
    # No personal data in refs
    assert "Acme" not in repr(draft.refs) and "12-3456789" not in repr(draft.refs)


async def test_person_draft_uses_sole_proprietor_and_sends_otp() -> None:
    p = _provider()
    fields = {**FIELDS, "first_name": "Ada", "last_name": "Lovelace",
              "email": "ada@example.com", "mobile_phone_number": "+15125550199"}
    req = await p.requirements("US", "local", "person")
    draft = await p.create_draft(
        organization_id="org1", contact_email="ops@hail.so", requirements=req,
        fields=fields, address=ADDRESS, documents={}, messaging_service_sid=None,
    )
    brand = next(kw for n, kw in p._client.calls if n.endswith("brand_registrations.create"))
    assert brand["brand_type"] == "SOLE_PROPRIETOR"
    assert any(n.endswith("brand_registration_otps.create") for n, _ in p._client.calls)
    assert draft.refs["messaging_service_sid"] == "MG1"  # created when none exists


async def test_person_draft_refuses_hail_number_as_mobile(monkeypatch) -> None:
    p = _provider()
    monkeypatch.setattr(p, "_is_hail_number", lambda e164: e164 == "+15125550199")
    fields = {**FIELDS, "first_name": "Ada", "last_name": "Lovelace",
              "email": "ada@example.com", "mobile_phone_number": "+15125550199"}
    req = await p.requirements("US", "local", "person")
    draft = await p.create_draft(
        organization_id="org1", contact_email="ops@hail.so", requirements=req,
        fields=fields, address=ADDRESS, documents={}, messaging_service_sid=None,
    )
    assert [pr.field for pr in draft.problems] == ["mobile_phone_number"]
    assert p._client.calls == []


async def test_status_pending_while_brand_pending() -> None:
    r = _ok_responses()
    r["brand_registrations.fetch"] = SimpleNamespace(status="PENDING", identity_status="UNVERIFIED", brand_feedback=None)
    p = _provider(r)
    st = await p.status({"brand_sid": "BN1", "messaging_service_sid": "MG1", "subject_type": "business",
                         "campaign": {"use_case": "MIXED", "description": "d" * 40, "message_flow": "f" * 40,
                                      "samples": ["s" * 20, "t" * 20], "has_links": False}})
    assert st.state == "pending"


async def test_status_creates_campaign_once_brand_approved() -> None:
    r = _ok_responses()
    r["brand_registrations.fetch"] = SimpleNamespace(status="APPROVED", identity_status="VERIFIED", brand_feedback=None)
    r["us_app_to_person.create"] = SimpleNamespace(sid="QE1", campaign_status="PENDING", errors=[])
    p = _provider(r)
    refs = {"brand_sid": "BN1", "messaging_service_sid": "MG1", "subject_type": "business",
            "campaign": {"use_case": "MIXED", "description": "d" * 40, "message_flow": "f" * 40,
                         "samples": ["s" * 20, "t" * 20], "has_links": False}}
    st = await p.status(refs)
    assert st.state == "pending"
    assert refs["campaign_sid"] == "QE1"
    create = next(kw for n, kw in p._client.calls if n.endswith("us_app_to_person.create"))
    assert create["us_app_to_person_usecase"] == "MIXED"
    assert create["brand_registration_sid"] == "BN1"


async def test_status_person_waits_for_otp_before_campaign() -> None:
    r = _ok_responses()
    r["brand_registrations.fetch"] = SimpleNamespace(status="APPROVED", identity_status="UNVERIFIED", brand_feedback=None)
    p = _provider(r)
    refs = {"brand_sid": "BN1", "messaging_service_sid": "MG1", "subject_type": "person",
            "campaign": {"use_case": "SOLE_PROPRIETOR", "description": "d" * 40, "message_flow": "f" * 40,
                         "samples": ["s" * 20, "t" * 20], "has_links": False}}
    st = await p.status(refs)
    assert st.state == "pending" and "campaign_sid" not in refs


async def test_status_approved_when_campaign_verified() -> None:
    r = _ok_responses()
    r["brand_registrations.fetch"] = SimpleNamespace(status="APPROVED", identity_status="VERIFIED", brand_feedback=None)
    r["us_app_to_person.fetch"] = SimpleNamespace(sid="QE1", campaign_status="VERIFIED", errors=[])
    p = _provider(r)
    st = await p.status({"brand_sid": "BN1", "messaging_service_sid": "MG1", "campaign_sid": "QE1",
                         "subject_type": "business", "campaign": {}})
    assert st.state == "approved"


async def test_status_rejected_carries_carrier_reason() -> None:
    r = _ok_responses()
    r["brand_registrations.fetch"] = SimpleNamespace(status="APPROVED", identity_status="VERIFIED", brand_feedback=None)
    r["us_app_to_person.fetch"] = SimpleNamespace(
        sid="QE1", campaign_status="FAILED",
        errors=[{"description": "Website does not contain sufficient business information."}],
    )
    p = _provider(r)
    st = await p.status({"brand_sid": "BN1", "messaging_service_sid": "MG1", "campaign_sid": "QE1",
                         "subject_type": "business", "campaign": {}})
    assert st.state == "rejected"
    assert "sufficient business information" in st.reason


async def test_attach_number_adds_missing_number_to_service() -> None:
    r = _ok_responses()
    r["phone_numbers.list"] = []
    r["phone_numbers.create"] = SimpleNamespace(sid="PN1")
    p = _provider(r)
    await p.attach_number({"messaging_service_sid": "MG1", "subject_type": "business"},
                          provider_resource_id="PN1", e164="+15125550100")
    assert any(n.endswith("phone_numbers.create") for n, _ in p._client.calls)


async def test_discard_deletes_in_reverse_order_and_ignores_missing() -> None:
    from twilio.base.exceptions import TwilioRestException

    r = _ok_responses()
    r["us_app_to_person.delete"] = lambda: (_ for _ in ()).throw(TwilioRestException(404, "u", msg="gone"))
    r["brand_registrations.delete"] = True
    r["trust_products.delete"] = True
    r["customer_profiles.delete"] = True
    p = _provider(r)
    await p.discard({"profile_sid": "BU1", "trust_sid": "BU2", "brand_sid": "BN1",
                     "messaging_service_sid": "MG1", "campaign_sid": "QE1"})
    names = [n.rsplit(".", 1)[-1] for n, _ in p._client.calls]
    assert names == ["delete"] * 4
```

- [ ] **Step 2: Run to verify failure**

Run: `cd core && uv run pytest tests/test_twilio_sms_registration.py -q`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement the plug-in**

`core/hailhq/core/providers/verification/twilio_sms.py`:

```python
"""Twilio plug-in for US A2P 10DLC registration under Hail's account (ISV
model): a customer profile, an A2P trust product, a brand, then a campaign
on the organization's messaging service. This module is the only place
that knows those words."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone

from hailhq.core.config import settings
from hailhq.core.providers.verification.base import (
    Address,
    DocumentInput,
    DraftResult,
    Problem,
    ProviderStatus,
    Requirements,
    SmsRegistrationProvider,
    SubjectType,
    VerificationProviderError,
)
from hailhq.core.providers.verification.sms_form import campaign_text, sms_requirements
from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client as TwilioClient

logger = logging.getLogger(__name__)

_SECONDARY_PROFILE_POLICY = "RNdfbf3fae0e1107f8aded0e7cead80bf5"
_STARTER_PROFILE_POLICY = "RN806dd6cd175f314e1f96a9727ee271f4"
_A2P_TRUST_POLICY = "RNb0d4771c2c98518d916a3d4cd70a8f8b"
_SOLE_PROP_TRUST_POLICY = "RN670d5d2e282a6130ae063b234b6019c8"
_EIN = re.compile(r"^\d{2}-?\d{7}$")


class TwilioSmsRegistrationProvider(SmsRegistrationProvider):
    name = "twilio"

    def __init__(self, account_sid=None, auth_token=None, client=None) -> None:
        self.account_sid = account_sid or settings.twilio_account_sid
        token = auth_token or settings.twilio_auth_token
        if client is None:
            if not self.account_sid or not token:
                raise ValueError("TwilioSmsRegistrationProvider needs Twilio credentials")
            client = TwilioClient(self.account_sid, token)
        self._client = client
        self._primary_profile_sid = settings.twilio_primary_profile_sid

    # -- requirements ------------------------------------------------------

    async def requirements(self, country_code, number_type, subject_type) -> Requirements:
        return sms_requirements(self.name, subject_type)

    # -- draft ---------------------------------------------------------------

    def _is_hail_number(self, e164: str) -> bool:
        """True when the mobile the person gave is one of Hail's own numbers."""
        try:
            return bool(self._client.incoming_phone_numbers.list(phone_number=e164, limit=1))
        except TwilioRestException:
            return False

    def _validate(self, fields, address, subject_type) -> list[Problem]:
        problems: list[Problem] = []
        required = [f.name for f in sms_requirements(self.name, subject_type).fields if f.required]
        for name in required:
            if not (fields.get(name) or "").strip():
                problems.append(Problem(field=name, message="This is required."))
        if subject_type == "business" and fields.get("ein") and not _EIN.match(fields["ein"]):
            problems.append(Problem(field="ein", message="Nine digits, like 12-3456789."))
        if subject_type == "person" and fields.get("mobile_phone_number"):
            if self._is_hail_number(fields["mobile_phone_number"]):
                problems.append(Problem(field="mobile_phone_number",
                                        message="Use your own mobile number, not a Hail number."))
        if address is None:
            problems.append(Problem(field="address", message="An address is required."))
        return problems

    async def create_draft(self, *, organization_id, contact_email, requirements, fields,
                           address, documents, messaging_service_sid=None) -> DraftResult:
        subject_type = requirements.subject_type
        problems = self._validate(fields, address, subject_type)
        if problems:
            return DraftResult(refs={}, problems=problems)
        text = campaign_text(fields, subject_type)
        refs: dict = {"subject_type": subject_type, "campaign": text.__dict__}
        try:
            await asyncio.to_thread(self._build, refs, organization_id, contact_email,
                                    fields, address, subject_type, messaging_service_sid)
        except TwilioRestException as exc:
            await self.discard(refs)
            if 400 <= (exc.status or 0) < 500 and exc.msg:
                return DraftResult(refs={}, problems=[Problem(field="", message=str(exc.msg))])
            raise VerificationProviderError("Twilio request failed") from exc
        return DraftResult(refs=refs)

    def _build(self, refs, organization_id, contact_email, fields, address,
               subject_type, messaging_service_sid) -> None:
        th = self._client.trusthub.v1
        person = subject_type == "person"
        friendly = f"hail-{organization_id}"
        profile = th.customer_profiles.create(
            friendly_name=friendly, email=contact_email,
            policy_sid=_STARTER_PROFILE_POLICY if person else _SECONDARY_PROFILE_POLICY,
        )
        refs["profile_sid"] = profile.sid
        if not person:
            biz = th.end_users.create(
                type="customer_profile_business_information", friendly_name=friendly,
                attributes={
                    "business_name": fields["legal_name"],
                    "business_type": _business_type(fields["business_type"]),
                    "business_registration_identifier": "EIN",
                    "business_registration_number": fields["ein"].replace("-", ""),
                    "business_industry": fields["industry"],
                    "business_identity": "direct_customer",
                    "business_regions_of_operation": "USA_AND_CANADA",
                    "website_url": fields["website"],
                },
            )
            th.customer_profiles(profile.sid).customer_profiles_entity_assignments.create(object_sid=biz.sid)
            rep = th.end_users.create(
                type="authorized_representative_1", friendly_name=friendly,
                attributes={
                    "first_name": fields["rep_name"].split(" ")[0],
                    "last_name": " ".join(fields["rep_name"].split(" ")[1:]) or fields["rep_name"],
                    "email": fields["rep_email"], "phone_number": fields["rep_phone"],
                    "business_title": fields["rep_title"], "job_position": "Other",
                },
            )
            th.customer_profiles(profile.sid).customer_profiles_entity_assignments.create(object_sid=rep.sid)
            addr = self._client.addresses.create(
                customer_name=address.customer_name, street=address.street, city=address.city,
                region=address.region, postal_code=address.postal_code, iso_country=address.country_code,
            )
            doc = th.supporting_documents.create(
                friendly_name=friendly, type="customer_profile_address",
                attributes={"address_sids": addr.sid},
            )
            th.customer_profiles(profile.sid).customer_profiles_entity_assignments.create(object_sid=doc.sid)
            if self._primary_profile_sid:
                th.customer_profiles(profile.sid).customer_profiles_entity_assignments.create(
                    object_sid=self._primary_profile_sid)
            ev = th.customer_profiles(profile.sid).customer_profiles_evaluations.create(
                policy_sid=_SECONDARY_PROFILE_POLICY)
            if ev.status != "compliant":
                raise TwilioRestException(400, "", msg=_evaluation_message(ev))
            th.customer_profiles(profile.sid).update(status="pending-review")

        trust = th.trust_products.create(
            friendly_name=friendly, email=contact_email,
            policy_sid=_SOLE_PROP_TRUST_POLICY if person else _A2P_TRUST_POLICY,
        )
        refs["trust_sid"] = trust.sid
        if person:
            eu = th.end_users.create(
                type="sole_proprietor_information", friendly_name=friendly,
                attributes={
                    "brand_name": f"{fields['first_name']} {fields['last_name']}",
                    "mobile_phone_number": fields["mobile_phone_number"],
                    "vertical": "COMMUNICATION",
                },
            )
        else:
            eu = th.end_users.create(
                type="us_a2p_messaging_profile_information", friendly_name=friendly,
                attributes={"company_type": _company_type(fields["business_type"])},
            )
        th.trust_products(trust.sid).trust_products_entity_assignments.create(object_sid=eu.sid)
        th.trust_products(trust.sid).trust_products_entity_assignments.create(object_sid=profile.sid)
        ev = th.trust_products(trust.sid).trust_products_evaluations.create(
            policy_sid=_SOLE_PROP_TRUST_POLICY if person else _A2P_TRUST_POLICY)
        if ev.status != "compliant":
            raise TwilioRestException(400, "", msg=_evaluation_message(ev))
        th.trust_products(trust.sid).update(status="pending-review")

        brand_kwargs = {"customer_profile_bundle_sid": profile.sid, "a2p_profile_bundle_sid": trust.sid}
        if person:
            brand_kwargs["brand_type"] = "SOLE_PROPRIETOR"
        brand = self._client.messaging.v1.a2p.brand_registrations.create(**brand_kwargs)
        refs["brand_sid"] = brand.sid
        if person:
            self._client.messaging.v1.a2p.brand_registrations(brand.sid).brand_registration_otps.create()
            refs["otp_sent_at"] = datetime.now(timezone.utc).isoformat()

        if messaging_service_sid is None:
            svc = self._client.messaging.v1.services.create(friendly_name=f"hail-org-{organization_id}")
            messaging_service_sid = svc.sid
        refs["messaging_service_sid"] = messaging_service_sid

    # -- lifecycle -----------------------------------------------------------

    async def check(self, refs) -> list[Problem]:
        return []

    async def submit(self, refs) -> None:
        # Vetting starts when the brand is created; nothing more to send.
        return None

    async def status(self, refs) -> ProviderStatus:
        try:
            return await asyncio.to_thread(self._status, refs)
        except TwilioRestException as exc:
            raise VerificationProviderError("Twilio request failed") from exc

    def _status(self, refs) -> ProviderStatus:
        brand = self._client.messaging.v1.a2p.brand_registrations(refs["brand_sid"]).fetch()
        if brand.status in ("FAILED", "SUSPENDED"):
            return ProviderStatus(state="rejected", reason=_brand_reason(brand))
        if brand.status != "APPROVED":
            return ProviderStatus(state="pending")
        if refs.get("subject_type") == "person" and brand.identity_status != "VERIFIED":
            return ProviderStatus(state="pending")
        svc = self._client.messaging.v1.services(refs["messaging_service_sid"])
        if "campaign_sid" not in refs:
            text = refs["campaign"]
            campaign = svc.us_app_to_person.create(
                brand_registration_sid=refs["brand_sid"],
                description=text["description"],
                message_flow=text["message_flow"],
                message_samples=text["samples"],
                us_app_to_person_usecase=text["use_case"],
                has_embedded_links=text["has_links"],
                has_embedded_phone=False,
            )
            refs["campaign_sid"] = campaign.sid
            return ProviderStatus(state="pending")
        campaign = svc.us_app_to_person(refs["campaign_sid"]).fetch()
        if campaign.campaign_status == "VERIFIED":
            return ProviderStatus(state="approved")
        if campaign.campaign_status == "FAILED":
            errors = campaign.errors or []
            reason = "; ".join(e.get("description", "") for e in errors if isinstance(e, dict)) or "the carrier rejected the campaign"
            return ProviderStatus(state="rejected", reason=reason)
        return ProviderStatus(state="pending")

    async def purchase_handle(self, refs) -> dict:
        return {}

    async def attach_number(self, refs, *, provider_resource_id, e164) -> None:
        svc = self._client.messaging.v1.services(refs["messaging_service_sid"])
        def _attach():
            present = svc.phone_numbers.list(limit=50)
            if any(getattr(p, "sid", None) == provider_resource_id for p in present):
                return
            if refs.get("subject_type") == "person" and present:
                return  # a person's campaign holds one number
            svc.phone_numbers.create(phone_number_sid=provider_resource_id)
        try:
            await asyncio.to_thread(_attach)
        except TwilioRestException as exc:
            if exc.code == 21710:  # already in this service
                return
            raise VerificationProviderError("Twilio request failed") from exc

    async def discard(self, refs) -> None:
        steps = []
        if refs.get("campaign_sid") and refs.get("messaging_service_sid"):
            steps.append(self._client.messaging.v1.services(refs["messaging_service_sid"])
                         .us_app_to_person(refs["campaign_sid"]).delete)
        if refs.get("brand_sid"):
            steps.append(self._client.messaging.v1.a2p.brand_registrations(refs["brand_sid"]).delete)
        if refs.get("trust_sid"):
            steps.append(self._client.trusthub.v1.trust_products(refs["trust_sid"]).delete)
        if refs.get("profile_sid"):
            steps.append(self._client.trusthub.v1.customer_profiles(refs["profile_sid"]).delete)
        for step in steps:
            try:
                await asyncio.to_thread(step)
            except TwilioRestException as exc:
                if exc.status != 404:
                    logger.warning("10DLC discard step failed: %s", exc.msg)
            except Exception:
                logger.warning("10DLC discard step failed", exc_info=True)


def _business_type(key: str) -> str:
    return {"PRIVATE_PROFIT": "Corporation", "PUBLIC_PROFIT": "Corporation",
            "NON_PROFIT": "Non-profit Corporation", "GOVERNMENT": "Government"}[key]


def _company_type(key: str) -> str:
    return {"PRIVATE_PROFIT": "private", "PUBLIC_PROFIT": "public",
            "NON_PROFIT": "non-profit", "GOVERNMENT": "government"}[key]


def _evaluation_message(ev) -> str:
    for result in getattr(ev, "results", None) or []:
        for field in result.get("fields", []) if isinstance(result, dict) else []:
            if not field.get("passed", True):
                return field.get("failure_reason") or "the carrier refused a detail"
    return "the carrier refused a detail"


def _brand_reason(brand) -> str:
    fb = getattr(brand, "brand_feedback", None) or []
    return "; ".join(str(f) for f in fb) or "the carrier refused the brand"
```

The `Recorder` fake in the tests returns itself for attribute chains; `svc.us_app_to_person(refs["campaign_sid"])` style calls (a resource with a sid) work because `__call__` with one positional arg records the sid and returns the recorder. If a test fails only because of the fake's path handling, fix the fake, not the plug-in.

Add to `core/hailhq/core/config.py` after `twilio_sip_trunk_sid`:

```python
    # Hail's approved primary customer profile (Trust Hub, business identity
    # "ISV Reseller or Partner"). Customers' 10DLC profiles are assigned to it.
    twilio_primary_profile_sid: str = ""
```

Add to `.env.example` after `TWILIO_SIP_TRUNK_SID=`:

```
# Trust Hub primary customer profile SID (BU...), identity "ISV Reseller or
# Partner". Needed for customers' US SMS registration (A2P 10DLC).
TWILIO_PRIMARY_PROFILE_SID=
```

Then add the same key and comment to the worktree `.env` (the real `BU...` value) and run the masked diff from Global Constraints; it must print nothing.

Register in `__init__.py` `_register_builtin`:

```python
    from hailhq.core.providers.verification.twilio_sms import TwilioSmsRegistrationProvider

    register_verification_provider("twilio", TwilioSmsRegistrationProvider, purpose="sms")
```

- [ ] **Step 4: Run tests**

Run: `cd core && uv run pytest tests/test_twilio_sms_registration.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add core/hailhq/core/providers/verification/twilio_sms.py core/hailhq/core/providers/verification/__init__.py core/hailhq/core/config.py .env.example core/tests/test_twilio_sms_registration.py
git commit -m "feat(verifications): Twilio 10DLC registration plug-in"
```

---

### Task 5: Telnyx 10DLC plug-in

**Files:**

- Create: `core/hailhq/core/providers/verification/telnyx_sms.py`
- Modify: `core/hailhq/core/providers/verification/__init__.py`
- Test: `core/tests/test_telnyx_sms_registration.py`

**Interfaces:**

- Consumes: `SmsRegistrationProvider`, `sms_requirements`, `campaign_text`, `TelnyxClient.request(method, path, params=None, json=None)` from `core/hailhq/core/providers/telnyx.py`.
- Produces: `TelnyxSmsRegistrationProvider(api_key=None, client=None)` registered as `("telnyx", "sms")`. `refs` keys: `brand_id`, `campaign_id`, `messaging_profile_id`, `subject_type`, `campaign`.

Telnyx facts (developers.telnyx.com/docs/messaging/10dlc, 2026-10):

- `POST /10dlc/brand` body: `entityType` (`PRIVATE_PROFIT`, `PUBLIC_PROFIT`, `NON_PROFIT`, `GOVERNMENT`, `SOLE_PROPRIETOR`), `displayName`, `companyName`, `ein`, `phone`, `street`, `city`, `state`, `postalCode`, `country`, `email`, `website`, `vertical`, `mobilePhone` (sole proprietor only). Response `brandId`, `identityStatus` (`SELF_DECLARED`, `UNVERIFIED`, `VERIFIED`, `VETTED_VERIFIED`), `status`.
- `POST /10dlc/brand/{brandId}/externalVetting` body `{"evpId": "AEGIS", "vettingClass": "STANDARD"}`; business only.
- `GET /10dlc/brand/{brandId}` returns `identityStatus`, `brandRelationship`, `failureReasons`.
- `POST /10dlc/campaignBuilder` body: `brandId`, `usecase`, `description`, `sample1`, `sample2`, `messageFlow`, `embeddedLink`, `embeddedPhone`, `numberPool`, `ageGated`, `directLending`, `subscriberOptin`, `subscriberOptout`, `subscriberHelp`, `optoutKeywords`, `helpKeywords`, `helpMessage`, `optinMessage`. Response `campaignId`.
- `GET /10dlc/campaign/{campaignId}` returns `campaignStatus` and `failureReasons`; `GET /10dlc/campaign/{campaignId}/mnoMetadata` reports carrier status. Treat `TCR_ACCEPTED`/`MNO_ACCEPTED` as approved, `TCR_FAILED`/`MNO_REJECTED` as rejected, else pending.
- `POST /10dlc/phoneNumberCampaign` body `{"phoneNumber": "+1...", "campaignId": "..."}`; `DELETE /10dlc/phoneNumberCampaign/{phoneNumber}` to unlink.
- Sole proprietor OTP: TCR texts the `mobilePhone`; the person replies YES; `identityStatus` becomes `VERIFIED`.

Before coding, open https://developers.telnyx.com/docs/messaging/10dlc and the sole proprietor subpage. If a field name above differs from the page, use the page.

- [ ] **Step 1: Write the failing tests**

`core/tests/test_telnyx_sms_registration.py` with an httpx `MockTransport`, following `core/tests/test_telnyx_verification.py` (open it and copy its transport helper). Tests, same shape as Task 4:

```python
import json

import httpx
import pytest
from hailhq.core.providers.verification.base import Address
from hailhq.core.providers.verification.telnyx_sms import TelnyxSmsRegistrationProvider

FIELDS = { ...same as Task 4 FIELDS... }
ADDRESS = Address(customer_name="Acme Inc", street="1 Main St", city="Austin",
                  region="TX", postal_code="78701", country_code="US")


def _provider(handler):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.telnyx.com/v2")
    return TelnyxSmsRegistrationProvider(api_key="k", client=client)


async def test_business_draft_creates_brand_and_requests_vetting() -> None:
    calls = []

    def handler(req: httpx.Request):
        calls.append((req.method, req.url.path, json.loads(req.content or b"{}")))
        if req.url.path.endswith("/10dlc/brand"):
            return httpx.Response(200, json={"brandId": "B1", "identityStatus": "SELF_DECLARED"})
        if req.url.path.endswith("/externalVetting"):
            return httpx.Response(200, json={"evpId": "AEGIS"})
        return httpx.Response(404)

    p = _provider(handler)
    req = await p.requirements("US", "local", "business")
    draft = await p.create_draft(organization_id="org1", contact_email="ops@hail.so", requirements=req,
                                 fields=FIELDS, address=ADDRESS, documents={}, messaging_service_sid="MP1")
    assert draft.problems == []
    assert draft.refs["brand_id"] == "B1" and draft.refs["messaging_profile_id"] == "MP1"
    body = calls[0][2]
    assert body["entityType"] == "PRIVATE_PROFIT" and body["ein"] == "123456789"
    assert body["country"] == "US"
    assert calls[1][1].endswith("/10dlc/brand/B1/externalVetting")


async def test_person_draft_sets_sole_proprietor_and_mobile() -> None:
    calls = []

    def handler(req):
        calls.append(json.loads(req.content or b"{}"))
        return httpx.Response(200, json={"brandId": "B1"})

    p = _provider(handler)
    fields = {**FIELDS, "first_name": "Ada", "last_name": "Lovelace", "email": "ada@example.com",
              "mobile_phone_number": "+15125550199"}
    req = await p.requirements("US", "local", "person")
    await p.create_draft(organization_id="org1", contact_email="ops@hail.so", requirements=req,
                         fields=fields, address=ADDRESS, documents={}, messaging_service_sid="MP1")
    assert calls[0]["entityType"] == "SOLE_PROPRIETOR"
    assert calls[0]["mobilePhone"] == "+15125550199"
    assert len(calls) == 1  # no vetting for a person


async def test_status_creates_campaign_once_identity_verified() -> None:
    posted = []

    def handler(req):
        if req.url.path.endswith("/10dlc/brand/B1"):
            return httpx.Response(200, json={"identityStatus": "VETTED_VERIFIED", "failureReasons": None})
        if req.url.path.endswith("/10dlc/campaignBuilder"):
            posted.append(json.loads(req.content))
            return httpx.Response(200, json={"campaignId": "C1"})
        return httpx.Response(404)

    p = _provider(handler)
    refs = {"brand_id": "B1", "messaging_profile_id": "MP1", "subject_type": "business",
            "campaign": {"use_case": "MIXED", "description": "d" * 40, "message_flow": "f" * 40,
                         "samples": ["s" * 20, "t" * 20], "has_links": False}}
    st = await p.status(refs)
    assert st.state == "pending" and refs["campaign_id"] == "C1"
    assert posted[0]["usecase"] == "MIXED" and posted[0]["brandId"] == "B1"
    assert posted[0]["optoutKeywords"] == "STOP,STOPALL,UNSUBSCRIBE,CANCEL,END,QUIT"


async def test_status_approved_and_rejected_from_campaign() -> None:
    state = {"campaignStatus": "TCR_ACCEPTED", "failureReasons": None}

    def handler(req):
        if req.url.path.endswith("/10dlc/brand/B1"):
            return httpx.Response(200, json={"identityStatus": "VERIFIED"})
        if req.url.path.endswith("/10dlc/campaign/C1"):
            return httpx.Response(200, json=state)
        return httpx.Response(404)

    p = _provider(handler)
    refs = {"brand_id": "B1", "messaging_profile_id": "MP1", "campaign_id": "C1", "subject_type": "person", "campaign": {}}
    assert (await p.status(refs)).state == "approved"
    state.update({"campaignStatus": "TCR_FAILED", "failureReasons": "Sample messages missing opt-out language"})
    st = await p.status(refs)
    assert st.state == "rejected" and "opt-out" in st.reason


async def test_attach_and_discard() -> None:
    calls = []

    def handler(req):
        calls.append((req.method, req.url.path))
        return httpx.Response(200, json={})

    p = _provider(handler)
    await p.attach_number({"campaign_id": "C1"}, provider_resource_id="PN1", e164="+15125550100")
    assert calls[-1] == ("POST", "/v2/10dlc/phoneNumberCampaign")
    await p.discard({"brand_id": "B1", "campaign_id": "C1"})
    assert ("DELETE", "/v2/10dlc/campaign/C1") in calls and ("DELETE", "/v2/10dlc/brand/B1") in calls
```

Adjust the asserted paths if `TelnyxClient.request` builds them without the `/v2` prefix; check `core/hailhq/core/providers/telnyx.py` first.

- [ ] **Step 2: Run to verify failure**

Run: `cd core && uv run pytest tests/test_telnyx_sms_registration.py -q`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

`core/hailhq/core/providers/verification/telnyx_sms.py`:

```python
"""Telnyx plug-in for US A2P 10DLC registration: a brand, optional vetting,
then a campaign; numbers are linked to the campaign one by one."""

from __future__ import annotations

import logging
import re

import httpx
from hailhq.core.providers.telnyx import TelnyxClient
from hailhq.core.providers.verification.base import (
    DraftResult,
    Problem,
    ProviderStatus,
    Requirements,
    SmsRegistrationProvider,
    VerificationProviderError,
)
from hailhq.core.providers.verification.sms_form import campaign_text, sms_requirements

logger = logging.getLogger(__name__)

_EIN = re.compile(r"^\d{2}-?\d{7}$")
_OPT_OUT = "STOP,STOPALL,UNSUBSCRIBE,CANCEL,END,QUIT"
_HELP = "HELP,INFO"
_APPROVED = {"TCR_ACCEPTED", "MNO_ACCEPTED", "ACCEPTED", "ACTIVE"}
_REJECTED = {"TCR_FAILED", "MNO_REJECTED", "REJECTED", "FAILED"}


class TelnyxSmsRegistrationProvider(SmsRegistrationProvider):
    name = "telnyx"

    def __init__(self, api_key=None, client=None) -> None:
        self._api = TelnyxClient(api_key, client)

    async def requirements(self, country_code, number_type, subject_type) -> Requirements:
        return sms_requirements(self.name, subject_type)

    def _validate(self, fields, address, subject_type) -> list[Problem]:
        problems = []
        for spec in sms_requirements(self.name, subject_type).fields:
            if spec.required and not (fields.get(spec.name) or "").strip():
                problems.append(Problem(field=spec.name, message="This is required."))
        if subject_type == "business" and fields.get("ein") and not _EIN.match(fields["ein"]):
            problems.append(Problem(field="ein", message="Nine digits, like 12-3456789."))
        if address is None:
            problems.append(Problem(field="address", message="An address is required."))
        return problems

    async def create_draft(self, *, organization_id, contact_email, requirements, fields,
                           address, documents, messaging_service_sid=None) -> DraftResult:
        subject_type = requirements.subject_type
        problems = self._validate(fields, address, subject_type)
        if problems:
            return DraftResult(refs={}, problems=problems)
        person = subject_type == "person"
        text = campaign_text(fields, subject_type)
        display = (f"{fields['first_name']} {fields['last_name']}" if person else fields["legal_name"])
        body = {
            "entityType": "SOLE_PROPRIETOR" if person else fields["business_type"],
            "displayName": display,
            "companyName": display,
            "email": fields["email"] if person else fields["rep_email"],
            "phone": fields["mobile_phone_number"] if person else fields["rep_phone"],
            "street": address.street, "city": address.city, "state": address.region,
            "postalCode": address.postal_code, "country": address.country_code,
            "vertical": "COMMUNICATION" if person else fields["industry"],
            "isReseller": False,
        }
        if person:
            body["mobilePhone"] = fields["mobile_phone_number"]
        else:
            body["ein"] = fields["ein"].replace("-", "")
            body["website"] = fields["website"]
        refs: dict = {"subject_type": subject_type, "campaign": text.__dict__,
                      "messaging_profile_id": messaging_service_sid}
        try:
            brand = await self._api.request("POST", "/10dlc/brand", json=body)
            refs["brand_id"] = brand["brandId"]
            if not person:
                await self._api.request("POST", f"/10dlc/brand/{brand['brandId']}/externalVetting",
                                        json={"evpId": "AEGIS", "vettingClass": "STANDARD"})
        except httpx.HTTPStatusError as exc:
            await self.discard(refs)
            if 400 <= exc.response.status_code < 500:
                return DraftResult(refs={}, problems=[Problem(field="", message=_detail(exc.response))])
            raise VerificationProviderError("Telnyx request failed") from exc
        except httpx.HTTPError as exc:
            await self.discard(refs)
            raise VerificationProviderError("Telnyx request failed") from exc
        return DraftResult(refs=refs)

    async def check(self, refs) -> list[Problem]:
        return []

    async def submit(self, refs) -> None:
        return None

    async def status(self, refs) -> ProviderStatus:
        try:
            brand = await self._api.request("GET", f"/10dlc/brand/{refs['brand_id']}")
            identity = brand.get("identityStatus")
            if brand.get("failureReasons"):
                return ProviderStatus(state="rejected", reason=str(brand["failureReasons"]))
            if identity not in ("VERIFIED", "VETTED_VERIFIED"):
                return ProviderStatus(state="pending")
            if "campaign_id" not in refs:
                text = refs["campaign"]
                campaign = await self._api.request("POST", "/10dlc/campaignBuilder", json={
                    "brandId": refs["brand_id"], "usecase": text["use_case"],
                    "description": text["description"], "messageFlow": text["message_flow"],
                    "sample1": text["samples"][0], "sample2": text["samples"][1],
                    "embeddedLink": text["has_links"], "embeddedPhone": False,
                    "numberPool": False, "ageGated": False, "directLending": False,
                    "subscriberOptin": True, "subscriberOptout": True, "subscriberHelp": True,
                    "optoutKeywords": _OPT_OUT, "helpKeywords": _HELP,
                    "helpMessage": "Reply STOP to opt out. For help contact hi@hail.so.",
                    "optinMessage": "You are opted in. Reply STOP to opt out, HELP for help.",
                })
                refs["campaign_id"] = campaign["campaignId"]
                return ProviderStatus(state="pending")
            campaign = await self._api.request("GET", f"/10dlc/campaign/{refs['campaign_id']}")
            status = (campaign.get("campaignStatus") or "").upper()
            if status in _APPROVED:
                return ProviderStatus(state="approved")
            if status in _REJECTED:
                return ProviderStatus(state="rejected",
                                      reason=str(campaign.get("failureReasons") or "the carrier rejected the campaign"))
            return ProviderStatus(state="pending")
        except httpx.HTTPError as exc:
            raise VerificationProviderError("Telnyx request failed") from exc

    async def purchase_handle(self, refs) -> dict:
        return {}

    async def attach_number(self, refs, *, provider_resource_id, e164) -> None:
        try:
            await self._api.request("POST", "/10dlc/phoneNumberCampaign",
                                    json={"phoneNumber": e164, "campaignId": refs["campaign_id"]})
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 409:
                return
            raise VerificationProviderError("Telnyx request failed") from exc
        except httpx.HTTPError as exc:
            raise VerificationProviderError("Telnyx request failed") from exc

    async def discard(self, refs) -> None:
        for method, path in (
            ("DELETE", f"/10dlc/campaign/{refs.get('campaign_id')}") if refs.get("campaign_id") else (None, None),
            ("DELETE", f"/10dlc/brand/{refs.get('brand_id')}") if refs.get("brand_id") else (None, None),
        ):
            if method is None:
                continue
            try:
                await self._api.request(method, path)
            except httpx.HTTPError:
                logger.warning("10DLC discard step failed: %s", path)


def _detail(response: httpx.Response) -> str:
    try:
        errors = response.json().get("errors") or []
        return "; ".join(e.get("detail") or e.get("title") or "" for e in errors) or "the carrier refused a detail"
    except ValueError:
        return "the carrier refused a detail"
```

Check how `TelnyxClient.request` raises on 4xx (it may raise `httpx.HTTPStatusError` via `raise_for_status()` or return the error body). Match whichever it does; the existing `telnyx.py` verification plug-in shows the pattern.

Register in `_register_builtin` after the Telnyx number plug-in:

```python
    from hailhq.core.providers.verification.telnyx_sms import TelnyxSmsRegistrationProvider

    register_verification_provider("telnyx", TelnyxSmsRegistrationProvider, purpose="sms")
```

- [ ] **Step 4: Run tests**

Run: `cd core && uv run pytest tests/test_telnyx_sms_registration.py tests/test_telnyx_verification.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add core/hailhq/core/providers/verification/telnyx_sms.py core/hailhq/core/providers/verification/__init__.py core/tests/test_telnyx_sms_registration.py
git commit -m "feat(verifications): Telnyx 10DLC registration plug-in"
```

---

### Task 6: Send gate on US-to-US long-code texts

**Files:**

- Create: `core/hailhq/core/sms_registration_gate.py`
- Modify: `api/hailhq/api/routes/sms.py:301-306` (after `from_number` resolves)
- Modify: `core/hailhq/core/config.py`, `.env.example`, worktree `.env`
- Test: `api/tests/test_sms_api.py` (find the file that tests `POST /sms`; `grep -ln "no dedicated SMS-capable" api/tests/*.py`)

**Interfaces:**

- Produces: `async def us_registration_block(db, organization_id, from_number: PhoneNumber, to_e164: str) -> str | None` returning the 422 message, or `None` when the send may proceed.
- The gate is always on; there is no setting.

- [ ] **Step 1: Write the failing tests**

Append to the SMS route test file. Use its existing helpers to create an org, a key, and an active SMS-capable number (`grep -n "def _number\|async def _active_number\|insert_number" api/tests/test_sms_api.py api/tests/conftest.py`). Then:

```python
from hailhq.core.models import CarrierVerification


async def _approve_sms(async_session, org_id, provider="twilio") -> None:
    async_session.add(CarrierVerification(
        organization_id=org_id, provider=provider, country_code="US", number_type="local",
        subject_type="business", purpose="sms", state="approved",
        provider_refs={"campaign_sid": "QE1"}, requirements_version="x",
    ))
    await async_session.commit()


async def test_us_to_us_from_unregistered_long_code_is_422(client, org_and_key, async_session, sms_number) -> None:
    # sms_number: an active twilio local +1 number on the org (adapt to the file's fixture)
    _, _, key = org_and_key
    resp = await client.post("/sms", json={"to": "+14155550123", "body": "hi", "recipient_consent": True},
                             headers=_auth(key))
    assert resp.status_code == 422, resp.text
    assert "SMS registration" in resp.text
    assert resp.json()["detail"][0]["loc"] == ["body", "from"]


async def test_us_to_us_passes_when_registered(client, org_and_key, async_session, sms_number, fake_sms_provider) -> None:
    org_id, _, key = org_and_key
    await _approve_sms(async_session, org_id)
    resp = await client.post("/sms", json={"to": "+14155550123", "body": "hi", "recipient_consent": True},
                             headers=_auth(key))
    assert resp.status_code == 201, resp.text


async def test_registration_is_per_carrier(client, org_and_key, async_session, telnyx_sms_number) -> None:
    org_id, _, key = org_and_key
    await _approve_sms(async_session, org_id, provider="twilio")
    resp = await client.post("/sms", json={"to": "+14155550123", "body": "hi", "recipient_consent": True,
                                           "from": telnyx_sms_number.e164}, headers=_auth(key))
    assert resp.status_code == 422


async def test_toll_free_and_non_us_are_not_gated(client, org_and_key, async_session, toll_free_number, fake_sms_provider) -> None:
    _, _, key = org_and_key
    resp = await client.post("/sms", json={"to": "+14155550123", "body": "hi", "recipient_consent": True,
                                           "from": toll_free_number.e164}, headers=_auth(key))
    assert resp.status_code == 201, resp.text
    resp = await client.post("/sms", json={"to": "+447700900123", "body": "hi", "recipient_consent": True},
                             headers=_auth(key))
    assert resp.status_code == 201, resp.text


```

Build the `sms_number`, `telnyx_sms_number`, and `toll_free_number` fixtures from the file's existing number helper with `country_code="US"`, `number_type="local"` or `"toll_free"`, `provider="twilio"` or `"telnyx"`, `capabilities=["sms","voice"]`, `provisioning_state="active"`. The `fake_sms_provider` fixture is whatever the file already uses to stub the carrier send.

- [ ] **Step 2: Run to verify failure**

Run: `cd api && uv run pytest tests/test_sms_api.py -q -k "registered or gated or long_code or per_carrier"`
Expected: the 422 tests FAIL (sends succeed today).

- [ ] **Step 3: Implement**

`core/hailhq/core/sms_registration_gate.py`:

```python
"""Whether a text may leave a US long code for a US number: only when the
organization holds an approved US SMS registration with that carrier."""

from __future__ import annotations

from uuid import UUID

from hailhq.core.config import settings
from hailhq.core.models import CarrierVerification, PhoneNumber
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

BLOCK_MESSAGE = (
    "US texting from this number needs SMS registration. Open Numbers in the "
    "console and choose Register for US texting."
)


def _is_us(e164: str) -> bool:
    return e164.startswith("+1")


async def us_registration_block(
    db: AsyncSession, organization_id: UUID, from_number: PhoneNumber, to_e164: str
) -> str | None:
    if not (_is_us(to_e164) and _is_us(from_number.e164)):
        return None
    if from_number.number_type == "toll_free":
        return None
    approved = (
        await db.execute(
            select(CarrierVerification.id).where(
                CarrierVerification.organization_id == organization_id,
                CarrierVerification.provider == from_number.provider,
                CarrierVerification.purpose == "sms",
                CarrierVerification.state == "approved",
            ).limit(1)
        )
    ).scalar_one_or_none()
    return None if approved else BLOCK_MESSAGE
```

In `api/hailhq/api/routes/sms.py`, right after `from_e164 = from_number.e164` (inside the `if from_e164 is None:` block, after the `if from_number is None:` check):

```python
        block = await us_registration_block(
            db, principal.organization_id, from_number, body.to
        )
        if block:
            raise await cache_failure(idem, unprocessable(block, loc=["body", "from"]))
```

Import `from hailhq.core.sms_registration_gate import us_registration_block`.

The gate is always on: no setting, no env var.

- [ ] **Step 4: Run the SMS suite**

Run: `cd api && uv run pytest tests/test_sms_api.py -q`
Expected: PASS. Existing tests that send US-to-US from a fixture number without a registration now need `_approve_sms`; fix them by adding the registration row in their fixture, not by weakening the gate.

- [ ] **Step 5: Commit**

```bash
git add core/hailhq/core/sms_registration_gate.py api/hailhq/api/routes/sms.py api/tests/test_sms_api.py
git commit -m "feat(sms): block US-to-US texts from unregistered long codes"
```

---

### Task 7: Attach numbers on approval and on activation

**Files:**

- Create: `api/hailhq/api/sms_registration.py`
- Modify: `api/hailhq/api/routes/verifications.py` (`_apply_status` callers: `_refresh`, `list_verifications`)
- Modify: `core/hailhq/core/sms_setup.py` (`ensure_sms`)
- Modify: `api/hailhq/api/number_orders.py` (release path for Telnyx unlink; find `release` in `api/hailhq/api/routes/numbers.py`)
- Test: `api/tests/test_verifications_api.py`

**Interfaces:**

- Produces: `async def attach_org_numbers(db, row: CarrierVerification, provider: SmsRegistrationProvider) -> bool` (True when every active US long code of the org on that carrier is attached; False when one failed). `async def attach_if_registered(db, number: PhoneNumber, registry) -> None`.

- [ ] **Step 1: Write the failing tests**

Append to `api/tests/test_verifications_api.py`:

```python
from hailhq.core.models import PhoneNumber


async def _us_number(async_session, org_id, e164="+15125550100", provider="fake", resource="PN1") -> PhoneNumber:
    n = PhoneNumber(organization_id=org_id, e164=e164, country_code="US", number_type="local",
                    capabilities=["voice", "sms"], provider=provider, provider_resource_id=resource,
                    provisioning_state="active", messaging_service_sid="MG1")
    async_session.add(n)
    await async_session.commit()
    return n


async def test_approval_attaches_every_us_long_code(client, org_and_key, sms_carrier, async_session) -> None:
    org_id, _, key = org_and_key
    await _fund(async_session, org_id, 10_000)
    await _us_number(async_session, org_id, "+15125550100", resource="PN1")
    await _us_number(async_session, org_id, "+15125550101", resource="PN2")
    vid = (await client.post("/verifications", data=_sms_submission(), headers=_auth(key))).json()["id"]
    sms_carrier.remote_status = ProviderStatus(state="approved")
    verifications_routes._last_polled.clear()
    resp = await client.get(f"/verifications/{vid}", headers=_auth(key))
    assert resp.json()["state"] == "approved"
    assert sorted(r for _, r, _ in sms_carrier.attached) == ["PN1", "PN2"]


async def test_attach_failure_keeps_row_submitted_and_retries(client, org_and_key, sms_carrier, async_session) -> None:
    org_id, _, key = org_and_key
    await _fund(async_session, org_id, 10_000)
    await _us_number(async_session, org_id)
    vid = (await client.post("/verifications", data=_sms_submission(), headers=_auth(key))).json()["id"]
    sms_carrier.remote_status = ProviderStatus(state="approved")
    sms_carrier.attach_error = RuntimeError("carrier down")
    verifications_routes._last_polled.clear()
    resp = await client.get(f"/verifications/{vid}", headers=_auth(key))
    assert resp.json()["state"] == "submitted"
    sms_carrier.attach_error = None
    verifications_routes._last_polled.clear()
    resp = await client.get(f"/verifications/{vid}", headers=_auth(key))
    assert resp.json()["state"] == "approved"
    assert len(sms_carrier.attached) == 1


async def test_number_activated_later_is_attached(client, org_and_key, sms_carrier, async_session) -> None:
    from hailhq.api import sms_registration

    org_id, _, key = org_and_key
    await _fund(async_session, org_id, 10_000)
    vid = (await client.post("/verifications", data=_sms_submission(), headers=_auth(key))).json()["id"]
    sms_carrier.remote_status = ProviderStatus(state="approved")
    verifications_routes._last_polled.clear()
    await client.get(f"/verifications/{vid}", headers=_auth(key))
    number = await _us_number(async_session, org_id, "+15125550177", resource="PN7")
    await sms_registration.attach_if_registered(
        async_session, number, lambda n, p="number": sms_carrier if p == "sms" else None
    )
    assert sms_carrier.attached[-1][1] == "PN7"
```

- [ ] **Step 2: Run to verify failure**

Run: `cd api && uv run pytest tests/test_verifications_api.py -q -k attach`
Expected: FAIL (`attached` empty, or import error).

- [ ] **Step 3: Implement**

`api/hailhq/api/sms_registration.py`:

```python
"""Put an organization's US long codes on its approved SMS registration."""

from __future__ import annotations

import logging

from hailhq.core.models import CarrierVerification, PhoneNumber
from hailhq.core.providers.verification.base import SmsRegistrationProvider
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


def _us_long_codes(organization_id, provider: str):
    return select(PhoneNumber).where(
        PhoneNumber.organization_id == organization_id,
        PhoneNumber.provider == provider,
        PhoneNumber.provisioning_state == "active",
        PhoneNumber.country_code == "US",
        PhoneNumber.number_type == "local",
        PhoneNumber.capabilities.contains(["sms"]),
    ).order_by(PhoneNumber.acquired_at)


async def attach_org_numbers(
    db: AsyncSession, row: CarrierVerification, provider: SmsRegistrationProvider
) -> bool:
    """Attach every active US long code of the organization on this carrier.
    Stops at the first failure and returns False; the next pass tries again.
    A person's registration takes one number."""
    numbers = (await db.execute(_us_long_codes(row.organization_id, row.provider))).scalars().all()
    if row.subject_type == "person":
        numbers = numbers[:1]
    for number in numbers:
        if number.provider_resource_id is None:
            continue
        try:
            await provider.attach_number(
                row.provider_refs, provider_resource_id=number.provider_resource_id, e164=number.e164
            )
        except Exception:
            logger.warning("SMS registration attach failed for %s", number.e164, exc_info=True)
            return False
    return True


async def approved_registration(
    db: AsyncSession, organization_id, provider: str
) -> CarrierVerification | None:
    return (
        await db.execute(
            select(CarrierVerification).where(
                CarrierVerification.organization_id == organization_id,
                CarrierVerification.provider == provider,
                CarrierVerification.purpose == "sms",
                CarrierVerification.state == "approved",
            )
        )
    ).scalar_one_or_none()


async def attach_if_registered(db: AsyncSession, number: PhoneNumber, registry) -> None:
    """A newly active US long code joins the org's approved registration, when
    there is one. Failures are logged; the number still works for voice."""
    if number.country_code != "US" or number.number_type != "local":
        return
    row = await approved_registration(db, number.organization_id, number.provider)
    if row is None:
        return
    provider = registry(number.provider, "sms")
    if provider is None or number.provider_resource_id is None:
        return
    if row.subject_type == "person":
        return  # a person's registration holds the first number only
    try:
        await provider.attach_number(
            row.provider_refs, provider_resource_id=number.provider_resource_id, e164=number.e164
        )
    except Exception:
        logger.warning("SMS registration attach failed for %s", number.e164, exc_info=True)
```

In `api/hailhq/api/routes/verifications.py`, change `_apply_status` so that an `approved` status on a `purpose == "sms"` row does not flip the row by itself. Add a helper and call it from `_refresh` and `list_verifications`:

```python
async def _settle_sms_approval(
    db: AsyncSession, row: CarrierVerification, status: ProviderStatus | None, registry: Registry
) -> ProviderStatus | None:
    """For an SMS registration the carrier approved, attach the org's numbers
    first. Until that succeeds the row stays 'submitted' (status 'pending')."""
    if status is None or status.state != "approved" or row.purpose != "sms":
        return status
    provider = registry(row.provider, "sms")
    if not isinstance(provider, SmsRegistrationProvider):
        return status
    if await attach_org_numbers(db, row, provider):
        return status
    return ProviderStatus(state="pending")
```

In `_refresh`: `status = await _settle_sms_approval(db, row, await _fetch_status(row, registry), registry)` then `_apply_status(row, status)`. In `list_verifications`: after `statuses = await asyncio.gather(...)`, map each through `_settle_sms_approval` sequentially (the session is used in order). Also persist `provider_refs` changes made inside `status()` (the plug-in writes `campaign_sid` into the dict): after `_fetch_status`, add `flag_modified(row, "provider_refs")` from `sqlalchemy.orm.attributes` whenever the dict changed, and commit even when the state did not change. Simplest: in `_refresh` and `list_verifications`, compute `before = dict(row.provider_refs)` before the fetch and, when `row.provider_refs != before`, call `flag_modified(row, "provider_refs")` and set `changed = True`.

The `verification.approve`/`submit` audit on recovery stays. Add an audit row `verification.sms_attached` with `{"numbers": <count>}` when `attach_org_numbers` returns True inside `_settle_sms_approval`.

In `core/hailhq/core/sms_setup.py`, `ensure_sms` is in `core` and has no registry; leave it. In `api/hailhq/api/number_orders.py` right after `await sms_setup.ensure_sms(db, number)` (both call sites, lines 152 and 851) and in `api/hailhq/api/routes/numbers.py` after lines 434 and 610, add:

```python
            await sms_registration.attach_if_registered(db, number, get_verification_provider)
```

with `from hailhq.api import sms_registration` and `from hailhq.core.providers.verification import get_verification_provider`.

Telnyx release: in the number release path (`grep -n "release(" api/hailhq/api/routes/numbers.py api/hailhq/api/number_orders.py`), before the carrier release call for a Telnyx US local number, call the Telnyx SMS plug-in's unlink. Add to `TelnyxSmsRegistrationProvider`:

```python
    async def detach_number(self, e164: str) -> None:
        try:
            await self._api.request("DELETE", f"/10dlc/phoneNumberCampaign/{e164}")
        except httpx.HTTPError:
            logger.warning("10DLC unlink failed for %s", e164)
```

and call it from the release path when `number.provider == "telnyx" and number.country_code == "US"` through `get_verification_provider("telnyx", "sms")` (skip when None). Twilio needs nothing on release.

- [ ] **Step 4: Run tests**

Run: `cd api && uv run pytest tests/test_verifications_api.py tests/test_sms_api.py -q && uv run pytest tests -q -k number`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add api/hailhq/api/sms_registration.py api/hailhq/api/routes/verifications.py api/hailhq/api/number_orders.py api/hailhq/api/routes/numbers.py core/hailhq/core/providers/verification/telnyx_sms.py api/tests/test_verifications_api.py
git commit -m "feat(verifications): attach US numbers to the approved SMS registration"
```

---

### Task 8: OpenAPI, CLI, SDK, docs, changelog, abuse monitor note

**Files:**

- Modify: `openapi/openapi.yaml` (regenerated), `cli/internal/client/client.gen.go` (regenerated), `cli/internal/cmd/number.go`, `sdk/hail/client.py`
- Modify: `core/hailhq/core/abuse_monitor.py:1-9`
- Modify: `docs/operations/carrier-verification.md`, `docs/public/self-host/twilio.md:54-58`, `docs/public/self-host/telnyx.md`, `CHANGELOG.md`
- Test: `cli/internal/cmd/number_test.go`

- [ ] **Step 1: Regenerate OpenAPI and the Go client**

Read `docs/public/self-host/operations.md` section "Development → Regenerating OpenAPI" and run the commands it gives (export the spec from the FastAPI app, then `cd cli && make generate` or the target named there). Confirm `openapi/openapi.yaml` now shows `purpose` on `VerificationResponse`, on `create_verification`'s multipart schema, and on the two query parameters.

- [ ] **Step 2: CLI flag**

Find where the CLI submits a verification (`grep -rn "CreateVerification\|verifications" cli/internal/cmd/*.go`). If there is a `hail number verify` (or similar) command, add `--purpose` (default `number`, allowed `number|sms`) and `--for-sms` as an alias that sets `purpose=sms`, `country US`, `type local`. If there is none (today only an error message mentions verification at `number.go:211`), add a minimal `hail number register-sms` command that posts `purpose=sms` with `--subject person|business`, `--field key=value` (repeatable), and `--address key=value` (repeatable), and prints the state. Write a table test in `number_test.go` that asserts the multipart body contains `purpose=sms` and `country_code=US`.

- [ ] **Step 3: SDK**

In `sdk/hail/client.py`, the verifications methods (`grep -n "verifications" sdk/hail/client.py`) gain `purpose: str = "number"` and pass it through. If the SDK has no verification methods, skip and note it in the commit body.

- [ ] **Step 4: Docs and notes**

- `core/hailhq/core/abuse_monitor.py` docstring lines 3-9: replace with "Each organization registers its own US A2P 10DLC brand and campaign (see docs/superpowers/specs/2026-10-13-us-sms-registration-design.md). One org's abusive traffic still risks Hail's standing with the carrier, so this module computes each org's rolling opt-out rate ..." (keep the rest).
- `docs/operations/carrier-verification.md`: add a section "US SMS registration (`purpose=sms`)" with: the state meanings table from the spec, the fee debit (`account_credits.ref = sms_registration:<id>`), the attach step, `TWILIO_PRIMARY_PROFILE_SID`, and one runnable curl:

```bash
curl -s -X POST "$HAIL_API_URL/verifications" -H "Authorization: Bearer $HAIL_API_KEY" \
  -F purpose=sms -F country_code=US -F number_type=local -F subject_type=business -F provider=twilio \
  -F 'fields={"legal_name":"Acme Inc","business_type":"PRIVATE_PROFIT","ein":"12-3456789","industry":"RETAIL","website":"https://acme.example","rep_name":"Ada Lovelace","rep_email":"ada@acme.example","rep_phone":"+15125550100","rep_title":"CEO","campaign_description":"Order updates for customers who bought on our site.","sample_1":"Acme: your order 123 shipped. Reply STOP to opt out.","sample_2":"Acme: your order 123 was delivered. Reply STOP to opt out.","opt_in_description":"Customers tick a box at checkout.","has_links":"NO"}' \
  -F 'address={"customer_name":"Acme Inc","street":"1 Main St","city":"Austin","region":"TX","postal_code":"78701","country_code":"US"}'
```

- `docs/public/self-host/twilio.md:54-58`: replace the paragraph with: "A2P 10DLC (United States). Each organization registers its own brand and campaign from the console (Numbers → Register for US texting) or `POST /verifications` with `purpose=sms`. Set `TWILIO_PRIMARY_PROFILE_SID` to your approved Trust Hub primary profile (business identity ISV Reseller or Partner). Until an organization is approved, texts from its US long codes to US numbers return 422." Same paragraph in `telnyx.md` without the profile line.
- `CHANGELOG.md` Unreleased: "US SMS registration: organizations register their own A2P 10DLC brand and campaign (business or person) with Twilio or Telnyx via console, API (`purpose=sms` on `/verifications`), CLI and MCP. The carrier fee is debited from prepaid credit at filing. Texts from a US long code to a US number need an approved registration (422 otherwise). New env: `TWILIO_PRIMARY_PROFILE_SID`."

- [ ] **Step 5: Run everything**

Run: `uv run ruff check . && uv run black --check . && cd api && uv run pytest -q && cd ../core && uv run pytest -q && cd ../cli && go test ./...`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add openapi/openapi.yaml cli sdk core/hailhq/core/abuse_monitor.py docs/operations/carrier-verification.md docs/public/self-host/twilio.md docs/public/self-host/telnyx.md CHANGELOG.md
git commit -m "feat(sms): OpenAPI, CLI, SDK and docs for US SMS registration"
```

---

### Task 9: MCP tools carry `purpose`

**Files:**

- Modify: `mcp/hailhq/mcp/tools.py`, `mcp/hailhq/mcp/hail_client.py`, `docs/public/mcp.md`
- Test: `mcp/tests/test_tools.py` (or the file that tests the verification tools)

Precondition: hail-hq/hail#172 (verification MCP tools) is merged into `main` and this branch is rebased on it (`git fetch origin && git rebase origin/main`). If #172 is still open when you reach this task, stop, report it, and skip this task; the plan's other tasks do not depend on it.

- [ ] **Step 1: Write the failing test**

In the MCP tools test file, next to the existing `submit_verification` test:

```python
async def test_submit_verification_passes_purpose(mcp_client, recorded_api):
    await mcp_client.call_tool("submit_verification", {
        "country_code": "US", "number_type": "local", "subject_type": "business",
        "purpose": "sms", "fields": {"legal_name": "Acme"}, "attestation": True,
    })
    sent = recorded_api.last_multipart()
    assert sent["purpose"] == "sms"
```

Adapt `mcp_client` and `recorded_api` to the fixtures #172 introduced.

- [ ] **Step 2: Run to verify failure**

Run: `cd mcp && uv run pytest tests -q -k purpose`
Expected: FAIL (unknown argument `purpose`).

- [ ] **Step 3: Implement**

Add `purpose: str = "number"` to `get_verification_requirements`, `submit_verification`, and `list_verifications` in `tools.py` and `hail_client.py`, forwarded as the query parameter or multipart part. Tool docstring line: "`purpose='sms'` registers the organization to text US numbers from a US number (country_code US, number_type local). The carrier fee is debited from credit at filing." Update the example block in `docs/public/mcp.md` with one `purpose: "sms"` call.

- [ ] **Step 4: Run and commit**

Run: `cd mcp && uv run pytest -q`
Expected: PASS.

```bash
git add mcp docs/public/mcp.md
git commit -m "feat(mcp): purpose on verification tools for US SMS registration"
```

---

### Task 10: Push the hail branch and mark the PR ready

- [ ] **Step 1: Push and check CI**

```bash
git push -u origin feat/us-sms-registration
gh pr ready 182
gh pr checks 182 --watch
```

Expected: all checks green. Fix failures on the branch; do not merge.

---

### Task 11: Console (hail-website): wizard, Numbers card, monthly fee

**Files (repo `hail-website`, in a worktree, branch `feat/us-sms-registration`):**

- Modify: `lib/verification.ts` (`VerificationRow.purpose`)
- Modify: `app/console/numbers/verification-actions.ts` (`purpose` through both actions)
- Modify: `app/console/numbers/VerificationWizard.tsx` (`purpose` prop, fee line, person copy)
- Create: `app/console/numbers/UsTextingCard.tsx`
- Modify: `app/console/numbers/page.tsx`, `app/console/numbers/VerificationsPanel.tsx`
- Modify: `lib/monthly-fee-rater.ts`, `lib/private-rates.ts` (or wherever registration fees live after reading it)
- Test: `app/console/numbers/__tests__/UsTextingCard.test.tsx`, `lib/__tests__/monthly-fee-rater.test.ts`

Set up: `cd <hail-website root> && git fetch origin && git worktree add -b feat/us-sms-registration <hail-website worktree> origin/master && cd <hail-website worktree> && cp <hail-website root>/.env .env && pnpm install --frozen-lockfile`.

- [ ] **Step 1: Types and actions**

`lib/verification.ts`: add `purpose: "number" | "sms"` to `VerificationRow`. `verification-actions.ts`: `getVerificationRequirementsAction(countryCode, numberType, subjectType, provider, purpose: "number" | "sms" = "number")` appends `purpose` to the query string; `submitVerificationAction` passes the form through unchanged (the wizard appends the `purpose` part).

- [ ] **Step 2: Write the failing card test**

`app/console/numbers/__tests__/UsTextingCard.test.tsx`:

```tsx
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { UsTextingCard } from "../UsTextingCard";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn() }) }));

const base = {
  id: "v1",
  provider: "twilio",
  country_code: "US",
  number_type: "local",
  subject_type: "business",
  purpose: "sms" as const,
  rejection_reason: null,
  created_at: "2026-10-13T00:00:00Z",
  updated_at: "2026-10-13T00:00:00Z",
  submitted_at: "2026-10-13T00:00:00Z",
  approved_at: null,
};

describe("UsTextingCard", () => {
  it("offers registration when the org has a US long code and no row", () => {
    render(
      <UsTextingCard
        carriers={["twilio"]}
        rows={[]}
        fees={{ twilio: { business: 6100, person: 1950 } }}
        canManage
      />,
    );
    expect(
      screen.getByRole("button", { name: "Register for US texting" }),
    ).toBeInTheDocument();
  });
  it("shows In review with the filing date", () => {
    render(
      <UsTextingCard
        carriers={["twilio"]}
        rows={[{ ...base, state: "submitted" }]}
        fees={{}}
        canManage
      />,
    );
    expect(screen.getByText(/In review/)).toBeInTheDocument();
    expect(screen.getByText(/1 to 7 business days/)).toBeInTheDocument();
  });
  it("shows the carrier reason and Try again when rejected", () => {
    render(
      <UsTextingCard
        carriers={["twilio"]}
        rows={[
          {
            ...base,
            state: "rejected",
            rejection_reason: "EIN does not match",
          },
        ]}
        fees={{}}
        canManage
      />,
    );
    expect(screen.getByText("EIN does not match")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Try again" }),
    ).toBeInTheDocument();
  });
  it("renders nothing without US long codes", () => {
    const { container } = render(
      <UsTextingCard carriers={[]} rows={[]} fees={{}} canManage />,
    );
    expect(container).toBeEmptyDOMElement();
  });
});
```

Run: `npx vitest run app/console/numbers/__tests__/UsTextingCard.test.tsx`
Expected: FAIL (module not found).

- [ ] **Step 3: Build the card and wire the page**

`app/console/numbers/UsTextingCard.tsx` ("use client"): props `carriers: string[]` (carriers of the org's active US local SMS numbers), `rows: VerificationRow[]` (purpose `sms`), `fees: Record<string, { person: number; business: number }>` (cents), `canManage: boolean`. One block per carrier: state label (Not registered / In review / Approved / Not approved), filing date via `formatRelative`, reason text, and a button that opens `VerificationWizard` with `countryCode="US" numberType="local" provider={carrier} purpose="sms"`. Copy, ASD-STE100:

- Heading: "US texting"
- Not registered: "To text US numbers from your US number, register with the carrier. It takes 1 to 7 business days."
- Fee line (from `fees`): `Filing costs $61.00 now and $10.00 per month after approval. Paid from your credit.` for business; person values from `fees[carrier].person` and `$2.00`.
- In review: "In review. Filed {date}. 1 to 7 business days."
- Person in review: add "Check your phone. Reply YES to the text from the carrier."
- Approved: "Approved {date}. Your US numbers can text US numbers."
- Rejected: reason, then button "Try again" (calls `cancelVerificationAction(id)` then opens the wizard).

`page.tsx`: compute `carriers` from the numbers list (`country_code === "US" && number_type === "local" && capabilities.includes("sms") && provisioning_state === "active"`), split `verifications` by `purpose`, pass `sms` rows to the card and `number` rows to `VerificationsPanel`. Fees: `page.tsx` calls `GET /verifications/sms-fees` (Task 2, Step 8b) through `callHailApiAsOrg` and maps it to `fees[carrier] = { person: filing_cents, business: filing_cents }` plus `monthly[carrier]`. The card hides the fee line when `fees[carrier]` is missing (API down).

`VerificationWizard.tsx`: new prop `purpose?: "number" | "sms"` (default `number`), passed to `getVerificationRequirementsAction` and appended to the `FormData` as `purpose`. When `purpose === "sms"`: the intro text reads "Register to text US numbers. The carrier reviews your details." and the person toggle label reads "I text as myself (no company)". Fee line rendered above the submit button when the parent passes `feeCents` (new optional prop). The mobile field help already comes from the API.

`VerificationsPanel.tsx`: rows with `purpose === "sms"` are not shown there (the card owns them).

- [ ] **Step 4: Monthly fee**

`lib/monthly-fee-rater.ts`: extend `monthlyFeeIdempotencyKey`'s `feeKind` union with `"sms_registration"`. Add a second pass after numbers: select `carrier_verifications` rows with `purpose='sms' AND state='approved'` (the website DB has the hail tables through the same Postgres; confirm by `grep -n "phone_numbers" lib/monthly-fee-rater.ts`), bill each month from `approved_at` through now at `monthly_fee_cents(provider, subject_type)` read from `costs/sms-registration.json` through `lib/telephony-costs.ts` (add a `fetchSmsRegistrationFees()` next to `fetchCarrierCatalogs`), `channel 'sms'`, `ref = monthly_fee:${orgId}:sms_registration:${verificationId}:${month}`, `source 'sms_registration_monthly'`. Test in `lib/__tests__/monthly-fee-rater.test.ts`: one approved row, two months, two debits, re-run inserts none.

- [ ] **Step 5: Verify, build, screenshot**

```bash
npx vitest run app/console/numbers lib/__tests__/monthly-fee-rater.test.ts
pnpm run build
```

Then render the Numbers page component with fixture data per AGENTS.md "Console pages need a session" (vite bundle in a scratch dir) and read a screenshot of the card in the four states.

- [ ] **Step 6: Commit, push, PR**

```bash
git add lib/verification.ts app/console/numbers lib/monthly-fee-rater.ts lib/telephony-costs.ts lib/__tests__/monthly-fee-rater.test.ts
git commit -m "feat(console): US texting registration card and monthly campaign fee"
git push -u origin feat/us-sms-registration
gh pr create --title "feat(console): US SMS registration (A2P 10DLC)" --body "Companion to https://github.com/hail-hq/hail/pull/182. Numbers page gets a US texting card; the verification wizard files purpose=sms; the monthly rater bills the campaign fee."
```

Report both PR links. Do not merge.
