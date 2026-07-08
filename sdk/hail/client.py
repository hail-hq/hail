"""Hail SDK public client.

Usage::

    from hail import Client

    async with Client(api_key="sk-...") as client:
        call = await client.calls.create(
            to="+15551234567",
            system_prompt="You are calling to confirm a reschedule.",
            recipient_consent=True,
        )
        async for event in client.events.tail(id=f"call:{call.id}"):
            print(event)

The client is async-only. There is no sync facade in v1; build one on top
with ``asyncio.run`` if you need it.
"""

from __future__ import annotations

import asyncio
import base64
import os
from datetime import datetime
from typing import Any, AsyncIterator, Literal
from uuid import UUID

import httpx

from hail._errors import HailConfigError
from hail._http import _HailHTTP, generate_idempotency_key
from hail._resource_id import parse_resource_id
from hail.models import (
    CallEventResponse,
    CallListResponse,
    CallResponse,
    CallStatus,
    EmailListResponse,
    EmailResponse,
    EmailStatus,
    EventStreamResponse,
    LLMConfig,
    EmailDomainListResponse,
    EmailDomainResponse,
    SmsListResponse,
    SmsResponse,
    SmsStatus,
    TERMINAL_CALL_STATUSES,
)

_DEFAULT_BASE_URL = "https://api.hail.so"
_TAIL_PAGE_SIZE = 1000


def _encode_event_cursor(occurred_at: datetime, event_id: UUID) -> str:
    """Base64-urlsafe (no padding) of ``"<isoformat>|<uuid>"``.

    Mirrors ``hailhq.core.schemas.encode_cursor`` byte-for-byte; the SDK
    can't import core, so the logic is duplicated. Used to synthesize the
    next polling cursor when the server didn't hand one back.
    """
    raw = f"{occurred_at.isoformat()}|{event_id}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


class _CallsResource:
    """``client.calls.*`` — POST/GET/LIST against ``/calls``."""

    def __init__(self, http: _HailHTTP) -> None:
        self._http = http

    async def create(
        self,
        *,
        to: str,
        recipient_consent: bool,
        system_prompt: str | None = None,
        llm: LLMConfig | dict[str, Any] | None = None,
        from_: str | None = None,
        first_message: str | None = None,
        metadata: dict[str, Any] | None = None,
        consent_source: str | None = None,
        consent_obtained_at: datetime | None = None,
        message_type: Literal["marketing", "informational"] | None = None,
        idempotency_key: str | None = None,
    ) -> CallResponse:
        """Originate an outbound call.

        Exactly one of ``system_prompt`` (mode A) or a fully-populated
        ``llm`` block (mode B) must be provided — server enforces this with
        a 422; we don't pre-validate so SDK and API stay in lockstep on the
        rule. ``recipient_consent`` is required — the server 422s without
        it. ``idempotency_key`` defaults to a fresh UUIDv4.
        """
        body: dict[str, Any] = {"to": to, "recipient_consent": recipient_consent}
        if from_ is not None:
            body["from"] = from_
        if system_prompt is not None:
            body["system_prompt"] = system_prompt
        if first_message is not None:
            body["first_message"] = first_message
        if metadata is not None:
            body["metadata"] = metadata
        if llm is not None:
            body["llm"] = llm.model_dump() if isinstance(llm, LLMConfig) else llm
        if consent_source is not None:
            body["consent_source"] = consent_source
        if consent_obtained_at is not None:
            body["consent_obtained_at"] = consent_obtained_at.isoformat()
        if message_type is not None:
            body["message_type"] = message_type

        key = idempotency_key or generate_idempotency_key()
        data = await self._http.request(
            "POST",
            "/calls",
            json=body,
            headers={"Idempotency-Key": key},
        )
        return CallResponse.model_validate(data)

    async def get(self, call_id: str | UUID) -> CallResponse:
        """Fetch a single call by id."""
        cid = str(call_id)
        data = await self._http.request("GET", f"/calls/{cid}")
        return CallResponse.model_validate(data)

    async def list(
        self,
        *,
        cursor: str | None = None,
        limit: int = 50,
        status: CallStatus | None = None,
        to: str | None = None,
    ) -> CallListResponse:
        """Cursor-paginated list, scoped to the caller's organization."""
        params = {"limit": limit, "cursor": cursor, "status": status, "to": to}
        data = await self._http.request("GET", "/calls", params=params)
        return CallListResponse.model_validate(data)


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


class _EmailsResource:
    """``client.emails.*`` — POST/GET/LIST against ``/emails``."""

    def __init__(self, http: _HailHTTP) -> None:
        self._http = http

    async def create(
        self,
        *,
        to: list[str],
        subject: str,
        recipient_consent: bool,
        body_text: str | None = None,
        body_html: str | None = None,
        from_: str | None = None,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        reply_to: str | None = None,
        conversation_id: UUID | str | None = None,
        metadata: dict[str, Any] | None = None,
        consent_source: str | None = None,
        consent_obtained_at: datetime | None = None,
        message_type: Literal["marketing", "informational"] | None = None,
        idempotency_key: str | None = None,
    ) -> EmailResponse:
        """Send an outbound email.

        At least one of ``body_text`` / ``body_html`` is required; the
        server returns 422 if neither is supplied. ``recipient_consent``
        is required — the server 422s without it. ``from_`` is optional:
        when omitted the server picks the first verified sender on the
        org or auto-mints a hail-mail address (operator-configured).
        ``idempotency_key`` defaults to a fresh UUIDv4.
        """
        # Build the body with the wire-side ``"from"`` alias. We don't
        # construct EmailCreate then dump it — caller might pass values
        # the server-side validator accepts but the SDK one rejects (e.g.
        # a recipient with an unusual TLD); keeping pre-validation off
        # mirrors how _CallsResource defers to the server.
        body: dict[str, Any] = {
            "to": list(to),
            "subject": subject,
            "recipient_consent": recipient_consent,
        }
        if from_ is not None:
            body["from"] = from_
        if body_text is not None:
            body["body_text"] = body_text
        if body_html is not None:
            body["body_html"] = body_html
        if cc:
            body["cc"] = list(cc)
        if bcc:
            body["bcc"] = list(bcc)
        if reply_to is not None:
            body["reply_to"] = reply_to
        if conversation_id is not None:
            body["conversation_id"] = str(conversation_id)
        if metadata is not None:
            body["metadata"] = metadata
        if consent_source is not None:
            body["consent_source"] = consent_source
        if consent_obtained_at is not None:
            body["consent_obtained_at"] = consent_obtained_at.isoformat()
        if message_type is not None:
            body["message_type"] = message_type

        key = idempotency_key or generate_idempotency_key()
        data = await self._http.request(
            "POST",
            "/emails",
            json=body,
            headers={"Idempotency-Key": key},
        )
        return EmailResponse.model_validate(data)

    async def get(self, email_id: str | UUID) -> EmailResponse:
        """Fetch a single email by id."""
        eid = str(email_id)
        data = await self._http.request("GET", f"/emails/{eid}")
        return EmailResponse.model_validate(data)

    async def list(
        self,
        *,
        cursor: str | None = None,
        limit: int = 50,
        status: EmailStatus | None = None,
        direction: Literal["outbound", "inbound"] | None = None,
    ) -> EmailListResponse:
        """Cursor-paginated list, scoped to the caller's organization."""
        params = {
            "limit": limit,
            "cursor": cursor,
            "status": status,
            "direction": direction,
        }
        data = await self._http.request("GET", "/emails", params=params)
        return EmailListResponse.model_validate(data)

    async def events(
        self,
        email_id: str | UUID,
        *,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Delivery/engagement timeline for one email.

        Cursor-paginated: pass ``cursor`` from a previous response's
        ``next_cursor`` to fetch the next page.
        """
        eid = str(email_id)
        params: dict[str, Any] = {}
        if cursor is not None:
            params["cursor"] = cursor
        if limit is not None:
            params["limit"] = limit
        return await self._http.request(
            "GET", f"/emails/{eid}/events", params=params or None
        )

    async def stats(
        self,
        *,
        from_: str | datetime | None = None,
        to: str | datetime | None = None,
        bucket: str = "day",
    ) -> dict[str, Any]:
        """Account-level deliverability stats for a time window."""
        params: dict[str, Any] = {"bucket": bucket}
        if from_ is not None:
            params["from"] = from_.isoformat() if isinstance(from_, datetime) else from_
        if to is not None:
            params["to"] = to.isoformat() if isinstance(to, datetime) else to
        return await self._http.request("GET", "/emails/stats", params=params)


class _EmailDomainsResource:
    """``client.email_domains.*`` — manage SES identities."""

    def __init__(self, http: _HailHTTP) -> None:
        self._http = http

    async def create(
        self,
        *,
        kind: str,
        domain: str | None = None,
        local_prefix_user: str | None = None,
        local_prefix_org: str | None = None,
        idempotency_key: str | None = None,
    ) -> EmailDomainResponse:
        """Register an email domain.

        ``kind`` is ``'hail_mail'`` (server composes the address from
        the prefixes) or ``'custom'`` (tenant DNS — server returns DKIM
        CNAMEs to publish). Validation is deferred to the server so
        SDK and API stay in lockstep on the rule set.
        """
        body: dict[str, Any] = {"kind": kind}
        if domain is not None:
            body["domain"] = domain
        if local_prefix_user is not None:
            body["local_prefix_user"] = local_prefix_user
        if local_prefix_org is not None:
            body["local_prefix_org"] = local_prefix_org

        key = idempotency_key or generate_idempotency_key()
        data = await self._http.request(
            "POST",
            "/email-domains",
            json=body,
            headers={"Idempotency-Key": key},
        )
        return EmailDomainResponse.model_validate(data)

    async def get(self, domain_id: str | UUID) -> EmailDomainResponse:
        """Fetch a single email domain by id."""
        did = str(domain_id)
        data = await self._http.request("GET", f"/email-domains/{did}")
        return EmailDomainResponse.model_validate(data)

    async def list(
        self,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> EmailDomainListResponse:
        """Cursor-paginated list, scoped to the caller's organization."""
        params = {"limit": limit, "cursor": cursor}
        data = await self._http.request("GET", "/email-domains", params=params)
        return EmailDomainListResponse.model_validate(data)

    async def verify(self, domain_id: str | UUID) -> EmailDomainResponse:
        """Re-poll the email provider for a custom row's DKIM status.

        No-op on hail-mail rows (they're verified by construction);
        returns the row's current shape so callers can treat both kinds
        uniformly.
        """
        did = str(domain_id)
        data = await self._http.request("POST", f"/email-domains/{did}/verify")
        return EmailDomainResponse.model_validate(data)

    async def patch(
        self,
        domain_id: str | UUID,
        *,
        local_prefix_user: str | None = None,
        local_prefix_org: str | None = None,
        inbound_enabled: bool | None = None,
        forward_to: list[str] | None = None,
        webhook_url: str | None = None,
        forward_rate_per_hour: int | None = None,
    ) -> EmailDomainResponse:
        """Edit hail-mail prefixes and/or inbound action settings.

        Prefix edits (``local_prefix_user``/``local_prefix_org``) only
        apply to hail_mail rows. Inbound action fields (``inbound_enabled``,
        ``forward_to``, ``webhook_url``, ``forward_rate_per_hour``) apply
        to any kind. Setting ``webhook_url`` mints a fresh secret and
        returns it once in the response's ``webhook_secret``.
        """
        body: dict[str, Any] = {}
        if local_prefix_user is not None:
            body["local_prefix_user"] = local_prefix_user
        if local_prefix_org is not None:
            body["local_prefix_org"] = local_prefix_org
        if inbound_enabled is not None:
            body["inbound_enabled"] = inbound_enabled
        if forward_to is not None:
            body["forward_to"] = list(forward_to)
        if webhook_url is not None:
            body["webhook_url"] = webhook_url
        if forward_rate_per_hour is not None:
            body["forward_rate_per_hour"] = forward_rate_per_hour
        did = str(domain_id)
        data = await self._http.request("PATCH", f"/email-domains/{did}", json=body)
        return EmailDomainResponse.model_validate(data)

    async def rotate_webhook_secret(self, domain_id: str | UUID) -> str:
        """Rotate the per-domain webhook secret; returns the new plaintext."""
        did = str(domain_id)
        data = await self._http.request(
            "POST", f"/email-domains/{did}/rotate-webhook-secret"
        )
        return data["webhook_secret"]

    async def delete(self, domain_id: str | UUID) -> None:
        """Remove an email domain. SES identity is deleted for custom rows."""
        did = str(domain_id)
        await self._http.request("DELETE", f"/email-domains/{did}")


class _WebhooksResource:
    """``client.webhooks.*`` — manage outbound webhook subscriptions."""

    def __init__(self, http: _HailHTTP) -> None:
        self._http = http

    async def create(
        self, *, target_url: str, event_types: list[str]
    ) -> dict[str, Any]:
        """Create a subscription; returns the response including the plaintext secret once."""
        body = {"target_url": target_url, "event_types": list(event_types)}
        return await self._http.request("POST", "/webhooks", json=body)

    async def list(self) -> dict[str, Any]:
        return await self._http.request("GET", "/webhooks")

    async def get(self, sub_id: str | UUID) -> dict[str, Any]:
        return await self._http.request("GET", f"/webhooks/{sub_id}")

    async def patch(
        self,
        sub_id: str | UUID,
        *,
        target_url: str | None = None,
        event_types: list[str] | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if target_url is not None:
            body["target_url"] = target_url
        if event_types is not None:
            body["event_types"] = list(event_types)
        if status is not None:
            body["status"] = status
        return await self._http.request("PATCH", f"/webhooks/{sub_id}", json=body)

    async def delete(self, sub_id: str | UUID) -> None:
        await self._http.request("DELETE", f"/webhooks/{sub_id}")

    async def rotate_secret(self, sub_id: str | UUID) -> str:
        data = await self._http.request("POST", f"/webhooks/{sub_id}/rotate-secret")
        return data["secret"]

    async def deliveries(self, sub_id: str | UUID) -> dict[str, Any]:
        return await self._http.request("GET", f"/webhooks/{sub_id}/deliveries")

    async def redeliver(
        self, sub_id: str | UUID, delivery_id: str | UUID
    ) -> dict[str, Any]:
        return await self._http.request(
            "POST", f"/webhooks/{sub_id}/deliveries/{delivery_id}/redeliver"
        )


class _EventsResource:
    """``client.events.*`` — list and tail against ``/events``."""

    def __init__(self, http: _HailHTTP) -> None:
        self._http = http

    async def list(
        self,
        *,
        id: str | None = None,
        kind: str | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> EventStreamResponse:
        """One-shot list (matches ``GET /events`` exactly)."""
        if id is not None:
            # Validate locally first so a typo fails before any HTTP. The wire
            # form is the original ``<type>:<uuid>`` string — we don't rebuild
            # it from the parsed pieces.
            parse_resource_id(id)
        return await self._fetch_page(id=id, kind=kind, cursor=cursor, limit=limit)

    async def tail(
        self,
        *,
        id: str | None = None,
        kind: str | None = None,
        interval_seconds: float = 0.5,
        follow: bool = True,
    ) -> AsyncIterator[CallEventResponse]:
        """Yield events as they arrive.

        Mirrors the CLI's tail loop (``cli/internal/cmd/tail.go``):
          * Drains all inner pages while the server reports ``next_cursor``.
          * After draining, synthesizes the next polling cursor from the last
            seen event's ``(occurred_at, id)`` — needed because the API only
            sets ``next_cursor`` when ``len(rows) > limit``.
          * When ``id`` resolves to a call (``call:<uuid>``), exits cleanly
            once ``call_status`` reaches a terminal value.
          * ``follow=False`` makes it stop after the first page (CLI's
            ``--no-follow``).
        """
        single_call = False
        if id is not None:
            type_str, _ = parse_resource_id(id)
            single_call = type_str == "call"

        cursor: str | None = None
        while True:
            page_resp = await self._fetch_page(
                id=id, kind=kind, cursor=cursor, limit=_TAIL_PAGE_SIZE
            )
            last_event: CallEventResponse | None = None
            page = page_resp
            while True:
                for ev in page.items:
                    last_event = ev
                    yield ev
                if page.next_cursor:
                    cursor = page.next_cursor
                    page = await self._fetch_page(
                        id=id, kind=kind, cursor=cursor, limit=_TAIL_PAGE_SIZE
                    )
                else:
                    break
            # Synthesize forward cursor for the next outer poll.
            if last_event is not None:
                cursor = _encode_event_cursor(last_event.occurred_at, last_event.id)

            if not follow:
                return
            if (
                single_call
                and page_resp.call_status is not None
                and page_resp.call_status in TERMINAL_CALL_STATUSES
            ):
                return

            await asyncio.sleep(interval_seconds)

    async def _fetch_page(
        self,
        *,
        id: str | None,
        kind: str | None,
        cursor: str | None,
        limit: int,
    ) -> EventStreamResponse:
        params = {"limit": limit, "id": id, "kind": kind, "cursor": cursor}
        data = await self._http.request("GET", "/events", params=params)
        return EventStreamResponse.model_validate(data)


class Client:
    """Hail API client.

    ``api_key`` defaults to ``$HAIL_API_KEY``; ``base_url`` defaults to
    ``$HAIL_API_URL`` and then to ``https://api.hail.so``. Construction
    raises :class:`HailConfigError` if no API key is discoverable.

    The underlying ``httpx.AsyncClient`` is built lazily on first request
    and torn down by :meth:`aclose` (or ``__aexit__``).
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: httpx.Timeout | float | None = None,
        _transport_client: httpx.AsyncClient | None = None,
    ) -> None:
        resolved_key = (
            api_key if api_key is not None else os.environ.get("HAIL_API_KEY")
        )
        if not resolved_key:
            raise HailConfigError(
                "no api_key provided; pass api_key= or set HAIL_API_KEY"
            )
        resolved_base = (
            base_url
            if base_url is not None
            else os.environ.get("HAIL_API_URL", _DEFAULT_BASE_URL)
        )

        self._http = _HailHTTP(
            base_url=resolved_base,
            api_key=resolved_key,
            timeout=timeout,
            transport_client=_transport_client,
        )
        self.calls = _CallsResource(self._http)
        self.sms = _SmsResource(self._http)
        self.emails = _EmailsResource(self._http)
        self.email_domains = _EmailDomainsResource(self._http)
        self.webhooks = _WebhooksResource(self._http)
        self.events = _EventsResource(self._http)
        self.base_url = resolved_base.rstrip("/")

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "Client":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.aclose()

    def __repr__(self) -> str:
        return f"<hail.Client base_url={self.base_url!r}>"


__all__ = ["Client"]
