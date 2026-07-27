"""POST /internal/ses-events with the delivery_event envelope."""

from __future__ import annotations

import json

import pytest
from hailhq.core.config import settings

from .conftest import insert_org_and_key
from .test_internal_ses_events import HMAC_SECRET as SECRET
from .test_internal_ses_events import _signed as _sign_raw


def _signed(body: dict) -> tuple[bytes, dict[str, str]]:
    raw = json.dumps(body, separators=(",", ":")).encode()
    return raw, {"Content-Type": "application/json", **_sign_raw(raw)}


def _delivery_envelope(pmid: str) -> dict:
    return {
        "type": "delivery_event",
        "event": {
            "eventType": "Delivery",
            "mail": {"messageId": pmid, "timestamp": "2026-07-01T12:00:00.000Z"},
            "delivery": {
                "timestamp": "2026-07-01T12:00:03.000Z",
                "recipients": ["bob@example.com"],
                "smtpResponse": "250 OK",
            },
        },
    }


@pytest.fixture(autouse=True)
def _hmac_secret(monkeypatch):
    monkeypatch.setattr(settings, "hail_inbound_hmac_secret", SECRET)
    # Delivery events must work with inbound DISABLED.
    monkeypatch.setattr(settings, "hail_inbound_enabled", False)
    # Auto-mint a hail-mail sender for POST /emails (no explicit `from`).
    monkeypatch.setattr(settings, "hail_mail_base_domain", "mail.hail.so")
    monkeypatch.setattr(settings, "hail_mail_default_user_prefix", "admin")


async def test_delivery_event_applies_with_inbound_disabled(client, async_session):
    # Arrange: a sent outbound email (POST /emails via stubbed provider).
    _, _, plain = await insert_org_and_key(async_session)
    resp = await client.post(
        "/emails",
        json={
            "to": ["bob@example.com"],
            "subject": "hi",
            "body_text": "hello",
            "recipient_consent": True,
        },
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 201
    pmid = resp.json()["provider_message_id"]

    raw, headers = _signed(_delivery_envelope(pmid))
    r = await client.post("/internal/ses-events", content=raw, headers=headers)
    assert r.status_code == 200
    assert r.json()["status"] == "applied"

    detail = await client.get(
        f"/emails/{resp.json()['id']}",
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert detail.json()["status"] == "delivered"


async def test_duplicate_delivery_event_reports_duplicate(client, async_session):
    _, _, plain = await insert_org_and_key(async_session)
    resp = await client.post(
        "/emails",
        json={
            "to": ["bob@example.com"],
            "subject": "hi",
            "body_text": "hello",
            "recipient_consent": True,
        },
        headers={"Authorization": f"Bearer {plain}"},
    )
    pmid = resp.json()["provider_message_id"]
    raw, headers = _signed(_delivery_envelope(pmid))
    await client.post("/internal/ses-events", content=raw, headers=headers)
    r2 = await client.post("/internal/ses-events", content=raw, headers=headers)
    assert r2.json()["status"] == "duplicate"


async def test_unmatched_pmid_returns_200_unmatched(client):
    raw, headers = _signed(_delivery_envelope("pmid-not-ours"))
    r = await client.post("/internal/ses-events", content=raw, headers=headers)
    assert r.status_code == 200
    assert r.json()["status"] == "unmatched"


async def test_untracked_event_type_ignored(client):
    env = _delivery_envelope("x")
    env["event"]["eventType"] = "Send"
    raw, headers = _signed(env)
    r = await client.post("/internal/ses-events", content=raw, headers=headers)
    assert r.json()["status"] == "ignored"


async def test_bad_signature_401(client):
    raw, headers = _signed(_delivery_envelope("x"))
    headers["X-Hail-Signature"] = "sha256=deadbeef"
    r = await client.post("/internal/ses-events", content=raw, headers=headers)
    assert r.status_code == 401


async def test_legacy_envelope_inbound_disabled_503_wins_over_bad_signature(client):
    # inbound disabled (autouse fixture), legacy-shaped body (no "type"),
    # BAD signature — 503 must win over 401, matching pre-restructure order.
    raw, headers = _signed({"message_id": "m1", "recipients": ["bob@example.com"]})
    headers["X-Hail-Signature"] = "sha256=deadbeef"
    r = await client.post("/internal/ses-events", content=raw, headers=headers)
    assert r.status_code == 503


async def test_legacy_envelope_inbound_disabled_503_with_valid_signature(client):
    # Same, but with a VALID signature — still 503, since inbound is
    # disabled and this isn't a delivery_event envelope.
    raw, headers = _signed({"message_id": "m1", "recipients": ["bob@example.com"]})
    r = await client.post("/internal/ses-events", content=raw, headers=headers)
    assert r.status_code == 503


async def test_delivery_envelope_missing_event_422(client):
    raw, headers = _signed({"type": "delivery_event"})
    r = await client.post("/internal/ses-events", content=raw, headers=headers)
    assert r.status_code == 422


async def test_delivery_envelope_non_dict_event_422(client):
    raw, headers = _signed({"type": "delivery_event", "event": "garbage"})
    r = await client.post("/internal/ses-events", content=raw, headers=headers)
    assert r.status_code == 422


async def test_delivery_envelope_malformed_inner_event_422(client):
    # Tracked eventType but no "mail" section — parser raises KeyError.
    raw, headers = _signed(
        {"type": "delivery_event", "event": {"eventType": "Delivery"}}
    )
    r = await client.post("/internal/ses-events", content=raw, headers=headers)
    assert r.status_code == 422
