"""The operator backfill fills ``subject_name`` from each carrier's record
and never logs a name. Carriers are faked."""

from __future__ import annotations

import uuid

from hailhq.core.models import CarrierVerification
from hailhq.core.verification_names import backfill_subject_names
from sqlalchemy import select


class _Plugin:
    def __init__(self, names: dict[str, str | None], fail: set[str] = frozenset()):
        self.names, self.fail, self.asked = names, fail, []

    async def fetch_subject_name(self, refs, subject_type):
        self.asked.append((refs["key"], subject_type))
        if refs["key"] in self.fail:
            raise RuntimeError("carrier said: Zelda Quixote is unknown")
        return self.names.get(refs["key"])


def _row(org, key, provider="twilio", name=None, **over) -> CarrierVerification:
    return CarrierVerification(
        organization_id=org,
        provider=provider,
        country_code=over.pop("country_code", "GB"),
        number_type="mobile",
        subject_type=over.pop("subject_type", "person"),
        purpose=over.pop("purpose", "number"),
        subject_name=name,
        state=over.pop("state", "approved"),
        provider_refs={"key": key},
        requirements_version="1",
    )


async def test_backfill_names_rows_from_their_carrier(async_session, caplog) -> None:
    org = uuid.uuid4()
    rows = {
        # Nameless live rows of one kind are one per key (a unique index),
        # so each gets its own country.
        "named": _row(org, "named", country_code="FR"),
        "sms": _row(org, "sms", purpose="sms", subject_type="business"),
        "cancelled": _row(org, "cancelled", state="cancelled"),
        "none": _row(org, "none", country_code="DE"),
        "boom": _row(org, "boom", country_code="ES"),
        "nocarrier": _row(org, "nocarrier", provider="gone"),
        "kept": _row(org, "kept", name="Already Named"),
        # A live row of the same kind whose name another live row holds.
        "dupe": _row(org, "dupe"),
        "holder": _row(org, "holder", name="ada lovelace"),
    }
    async_session.add_all(rows.values())
    await async_session.commit()
    number = _Plugin(
        {
            "named": "Zelda Quixote",
            "cancelled": "Grace Hopper",
            "boom": "x",
            "dupe": "Ada Lovelace",
        },
        fail={"boom"},
    )
    sms = _Plugin({"sms": "Acme Inc"})

    def registry(name, purpose):
        if name == "gone":
            return None
        return sms if purpose == "sms" else number

    with caplog.at_level("DEBUG"):
        summary = await backfill_subject_names(async_session, registry)

    assert (
        summary.named,
        summary.no_name,
        summary.no_plugin,
        summary.failed,
        summary.duplicate,
    ) == (3, 1, 1, 1, 1)
    assert ("sms", "business") in sms.asked
    assert all(key != "kept" and key != "holder" for key, _ in number.asked)
    got = {
        r.provider_refs["key"]: r.subject_name
        for r in (
            await async_session.execute(
                select(CarrierVerification).execution_options(populate_existing=True)
            )
        ).scalars()
    }
    assert got == {
        "named": "Zelda Quixote",
        "sms": "Acme Inc",
        "cancelled": "Grace Hopper",
        "none": None,
        "boom": None,
        "nocarrier": None,
        "kept": "Already Named",
        "dupe": None,
        "holder": "ada lovelace",
    }
    logged = " ".join(r.getMessage() for r in caplog.records).lower()
    for name in ("zelda", "quixote", "grace", "acme", "lovelace"):
        assert name not in logged
