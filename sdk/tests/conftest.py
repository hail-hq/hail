"""Shared fixtures for the SDK test suite."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wipe Hail env vars from every test so client-construction is deterministic.

    Tests that want a key inject it explicitly. ``HAIL_API_URL`` defaults to
    the production base in production code; we point at a benign localhost so
    a respx-bypass doesn't accidentally hit the real API.
    """
    monkeypatch.delenv("HAIL_API_KEY", raising=False)
    monkeypatch.delenv("HAIL_API_URL", raising=False)


@pytest.fixture
def base_url() -> str:
    return "https://api.test"


@pytest.fixture
def api_key() -> str:
    return "sk-test"


def make_call_response(
    *,
    call_id: UUID | None = None,
    status: str = "queued",
) -> dict:
    """Server-shaped JSON for a CallResponse."""
    cid = call_id or uuid4()
    return {
        "id": str(cid),
        "organization_id": str(uuid4()),
        "conversation_id": None,
        "from_e164": "+15550001111",
        "to_e164": "+15555550123",
        "direction": "outbound",
        "status": status,
        "end_reason": None,
        "provider_call_sid": None,
        "livekit_room": None,
        "initial_prompt": "test prompt",
        "recording_s3_key": None,
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "started_at": None,
        "answered_at": None,
        "ended_at": None,
    }


def make_sms_response(
    *,
    sms_id: UUID | None = None,
    status: str = "sent",
) -> dict:
    """Server-shaped JSON for an SmsResponse."""
    sid = sms_id or uuid4()
    return {
        "id": str(sid),
        "organization_id": str(uuid4()),
        "from_e164": "+15550001111",
        "to_e164": "+15555550123",
        "direction": "outbound",
        "status": status,
        "body": "test body",
        "provider_message_sid": "SM_test",
        "segment_count": 1,
        "error_code": None,
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "sent_at": datetime.now(timezone.utc).isoformat() if status == "sent" else None,
    }


def make_suppression_response(
    *,
    suppression_id: UUID | None = None,
    recipient: str = "+15555550123",
    reason: str = "stop_keyword",
    source: str = "inbound_sms",
) -> dict:
    """Server-shaped JSON for a SuppressionResponse."""
    return {
        "id": str(suppression_id or uuid4()),
        "recipient": recipient,
        "channel": "sms",
        "reason": reason,
        "source": source,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def make_email_domain_response(
    *,
    domain_id: UUID | None = None,
    kind: str = "hail_mail",
    domain: str = "alice+acme@mail.hail.so",
    verification_status: str = "verified",
    local_prefix_user: str | None = "alice",
    local_prefix_org: str | None = "acme",
    dns_records: list[dict] | None = None,
    dkim_records: list[dict] | None = None,
) -> dict:
    """Server-shaped JSON for an EmailDomainResponse."""
    did = domain_id or uuid4()
    now = datetime.now(timezone.utc)
    return {
        "id": str(did),
        "organization_id": str(uuid4()),
        "kind": kind,
        "domain": domain,
        "local_prefix_user": local_prefix_user if kind == "hail_mail" else None,
        "local_prefix_org": local_prefix_org if kind == "hail_mail" else None,
        "verification_status": verification_status,
        "dns_records": dns_records or dkim_records or [],
        "mail_from_domain": None,
        "mail_from_status": None,
        "provider": "ses",
        "verified_at": now.isoformat() if verification_status == "verified" else None,
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
    }


def make_email_response(
    *,
    email_id: UUID | None = None,
    status: str = "sent",
    from_address: str = "alice+acme@mail.hail.so",
) -> dict:
    """Server-shaped JSON for an EmailResponse."""
    eid = email_id or uuid4()
    now = datetime.now(timezone.utc)
    return {
        "id": str(eid),
        "organization_id": str(uuid4()),
        "conversation_id": None,
        "email_domain_id": str(uuid4()),
        "from_address": from_address,
        "to_addresses": ["recipient@example.com"],
        "cc_addresses": None,
        "bcc_addresses": None,
        "reply_to": None,
        "subject": "test subject",
        "body_text": "test body",
        "body_html": None,
        "status": status,
        "end_reason": None,
        "provider_message_id": "ses-msg-1",
        "requested_at": now.isoformat(),
        "sent_at": now.isoformat() if status == "sent" else None,
        "failed_at": None,
        "metadata": {},
    }


def make_event(
    *,
    call_id: UUID,
    kind: str = "agent_turn",
    payload: dict | None = None,
    occurred_at: datetime | None = None,
    event_id: UUID | None = None,
) -> dict:
    return {
        "id": str(event_id or uuid4()),
        "source": "call",
        "call_id": str(call_id),
        "kind": kind,
        "payload": payload if payload is not None else {"text": "hello"},
        "occurred_at": (occurred_at or datetime.now(timezone.utc)).isoformat(),
    }
