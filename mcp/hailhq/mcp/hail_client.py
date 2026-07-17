"""Thin async httpx wrapper around the Hail API.

The MCP service talks to the same public ``POST /calls`` / ``POST /emails``
/ ``GET /calls`` / ``GET /events`` surface external clients use. Request
bodies are built from the *shared* ``hailhq.core.schemas`` models the API
itself uses, and 2xx responses are parsed through the matching response
model — so the wire contract (field names, aliases, validation) lives in
exactly one place and cannot drift from the API.

* ``Authorization: Bearer <hail_api_key>`` is auto-injected on every request.
* ``Idempotency-Key`` is auto-injected on ``place_call`` / ``send_email``
  (a fresh UUID per invocation unless the caller passed one).
* Non-2xx responses map to :class:`HailAPIError`; the tool layer turns that
  into a structured ``{"error": ...}`` payload. A request model that fails
  validation raises ``pydantic.ValidationError`` *before* any HTTP call —
  the tool layer maps that too.

Configuration reads from :data:`hailhq.core.config.settings`; constructor
kwargs override for tests.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx

from hailhq.core.config import settings
from hailhq.core.schemas import (
    CallCreate,
    CallListResponse,
    CallResponse,
    ContactCreate,
    ContactEntry,
    ContactListResponse,
    EmailCreate,
    EmailEventListResponse,
    EmailListResponse,
    EmailResponse,
    EmailStatsResponse,
    EventStreamResponse,
    SmsCreate,
    SmsListResponse,
    SmsResponse,
)


class HailAPIError(Exception):
    """Non-2xx response from the Hail API.

    ``status`` is the HTTP status code; ``detail`` is the parsed ``detail``
    field from the JSON body when present, otherwise the raw response text.
    The MCP tool layer converts this to an agent-facing error dict.
    """

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"hail api error {status}: {detail}")
        self.status = status
        self.detail = detail


class HailClient:
    """Async httpx client for the Hail API."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = (base_url or settings.hail_api_url).rstrip("/")
        self._api_key = api_key if api_key is not None else settings.hail_api_key
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout,
            transport=transport,
            headers={"Authorization": f"Bearer {self._api_key}"},
        )

    async def __aenter__(self) -> "HailClient":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ #
    # POST /calls
    # ------------------------------------------------------------------ #

    async def place_call(
        self,
        *,
        to: str,
        recipient_consent: bool,
        system_prompt: str | None = None,
        llm: dict[str, Any] | None = None,
        from_: str | None = None,
        first_message: str | None = None,
        metadata: dict[str, Any] | None = None,
        tools: list[str] | None = None,
        idempotency_key: str | None = None,
        consent_source: str | None = None,
        consent_obtained_at: str | None = None,
        message_type: str = "informational",
    ) -> dict[str, Any]:
        """POST /calls — originate an outbound call.

        Builds the body from :class:`CallCreate` (which enforces E.164,
        system_prompt-XOR-llm, ``LLMConfig`` completeness, and consent
        attestation). Construction raises ``pydantic.ValidationError``
        before any HTTP on bad input.
        """
        fields: dict[str, Any] = {
            "to": to,
            "recipient_consent": recipient_consent,
            "message_type": message_type,
        }
        if from_ is not None:
            fields["from"] = from_  # alias key — CallCreate has no populate_by_name
        if system_prompt is not None:
            fields["system_prompt"] = system_prompt
        if llm is not None:
            fields["llm"] = llm
        if first_message is not None:
            fields["first_message"] = first_message
        if metadata is not None:
            fields["metadata"] = metadata
        if tools is not None:
            fields["tools"] = tools
        if consent_source is not None:
            fields["consent_source"] = consent_source
        if consent_obtained_at is not None:
            fields["consent_obtained_at"] = consent_obtained_at

        body = CallCreate.model_validate(fields).model_dump(
            mode="json", by_alias=True, exclude_unset=True
        )
        headers = {"Idempotency-Key": idempotency_key or str(uuid.uuid4())}
        resp = await self._client.post("/calls", json=body, headers=headers)
        return CallResponse.model_validate(_decode(resp)).model_dump(mode="json")

    # ------------------------------------------------------------------ #
    # GET /calls/{id}
    # ------------------------------------------------------------------ #

    async def get_call(self, call_id: str) -> dict[str, Any]:
        resp = await self._client.get(f"/calls/{call_id}")
        return CallResponse.model_validate(_decode(resp)).model_dump(mode="json")

    # ------------------------------------------------------------------ #
    # GET /calls
    # ------------------------------------------------------------------ #

    async def list_calls(
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
        resp = await self._client.get("/calls", params=params)
        return CallListResponse.model_validate(_decode(resp)).model_dump(mode="json")

    # ------------------------------------------------------------------ #
    # GET /contacts
    # ------------------------------------------------------------------ #

    async def list_contacts(
        self,
        *,
        q: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if q is not None:
            params["q"] = q
        if limit is not None:
            params["limit"] = limit
        resp = await self._client.get("/contacts", params=params)
        return ContactListResponse.model_validate(_decode(resp)).model_dump(mode="json")

    # ------------------------------------------------------------------ #
    # POST /contacts
    # ------------------------------------------------------------------ #

    async def create_contact(
        self,
        *,
        name: str,
        phone_e164: str | None = None,
        email: str | None = None,
    ) -> dict[str, Any]:
        """POST /contacts — save a manual contact.

        Builds the body from :class:`ContactCreate` (which enforces a
        non-empty name and phone_e164-or-email). Construction raises
        ``pydantic.ValidationError`` before any HTTP on bad input.
        """
        fields: dict[str, Any] = {"name": name}
        if phone_e164 is not None:
            fields["phone_e164"] = phone_e164
        if email is not None:
            fields["email"] = email
        body = ContactCreate.model_validate(fields).model_dump(
            mode="json", exclude_unset=True
        )
        resp = await self._client.post("/contacts", json=body)
        return ContactEntry.model_validate(_decode(resp)).model_dump(mode="json")

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

    # ------------------------------------------------------------------ #
    # POST /emails
    # ------------------------------------------------------------------ #

    async def send_email(
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
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        consent_source: str | None = None,
        consent_obtained_at: str | None = None,
        message_type: str = "informational",
        attachment_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """POST /emails — send an outbound message.

        Builds the body from :class:`EmailCreate` (which enforces ≥1
        recipient, a non-empty subject, body-required, email formats, and
        consent attestation).
        """
        fields: dict[str, Any] = {
            "to": list(to),
            "subject": subject,
            "recipient_consent": recipient_consent,
            "message_type": message_type,
        }
        if from_ is not None:
            fields["from"] = from_
        if body_text is not None:
            fields["body_text"] = body_text
        if body_html is not None:
            fields["body_html"] = body_html
        if cc:
            fields["cc"] = list(cc)
        if bcc:
            fields["bcc"] = list(bcc)
        if reply_to is not None:
            fields["reply_to"] = reply_to
        if metadata is not None:
            fields["metadata"] = metadata
        if consent_source is not None:
            fields["consent_source"] = consent_source
        if consent_obtained_at is not None:
            fields["consent_obtained_at"] = consent_obtained_at
        if attachment_ids:
            fields["attachment_ids"] = list(attachment_ids)

        body = EmailCreate.model_validate(fields).model_dump(
            mode="json", by_alias=True, exclude_unset=True
        )
        headers = {"Idempotency-Key": idempotency_key or str(uuid.uuid4())}
        resp = await self._client.post("/emails", json=body, headers=headers)
        return EmailResponse.model_validate(_decode(resp)).model_dump(mode="json")

    # ------------------------------------------------------------------ #
    # POST /email-attachments
    # ------------------------------------------------------------------ #

    async def upload_email_attachment(
        self, *, filename: str, content: bytes, content_type: str
    ) -> dict[str, Any]:
        """POST /email-attachments — upload a file for outbound attachment.

        Returns ``{"id": ..., "filename": ..., "content_type": ...,
        "size_bytes": ...}``; the ``id`` is reusable via
        ``send_email(attachment_ids=[...])``.
        """
        resp = await self._client.post(
            "/email-attachments",
            files={"file": (filename, content, content_type)},
        )
        return _decode(resp)

    # ------------------------------------------------------------------ #
    # GET /emails/{id}
    # ------------------------------------------------------------------ #

    async def get_email(self, email_id: str) -> dict[str, Any]:
        resp = await self._client.get(f"/emails/{email_id}")
        return EmailResponse.model_validate(_decode(resp)).model_dump(mode="json")

    # ------------------------------------------------------------------ #
    # GET /emails
    # ------------------------------------------------------------------ #

    async def list_emails(
        self,
        *,
        cursor: str | None = None,
        limit: int | None = None,
        status: str | None = None,
        direction: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if cursor is not None:
            params["cursor"] = cursor
        if limit is not None:
            params["limit"] = limit
        if status is not None:
            params["status"] = status
        if direction is not None:
            params["direction"] = direction
        resp = await self._client.get("/emails", params=params)
        return EmailListResponse.model_validate(_decode(resp)).model_dump(mode="json")

    # ------------------------------------------------------------------ #
    # GET /emails/{id}/raw — 302 → presigned S3 URL
    # ------------------------------------------------------------------ #

    async def get_email_raw(self, email_id: str) -> dict[str, Any]:
        resp = await self._client.get(f"/emails/{email_id}/raw", follow_redirects=False)
        return {"url": _location(resp)}

    # ------------------------------------------------------------------ #
    # GET /emails/{id}/attachments/{aid} — 302 → presigned S3 URL
    # ------------------------------------------------------------------ #

    async def get_email_attachment(
        self, email_id: str, attachment_id: str
    ) -> dict[str, Any]:
        resp = await self._client.get(
            f"/emails/{email_id}/attachments/{attachment_id}",
            follow_redirects=False,
        )
        return {"url": _location(resp)}

    # ------------------------------------------------------------------ #
    # GET /events
    # ------------------------------------------------------------------ #

    async def get_events(
        self,
        *,
        id: str | None = None,
        kind: str | None = None,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if id is not None:
            params["id"] = id
        if kind is not None:
            params["kind"] = kind
        if cursor is not None:
            params["cursor"] = cursor
        if limit is not None:
            params["limit"] = limit
        resp = await self._client.get("/events", params=params)
        return EventStreamResponse.model_validate(_decode(resp)).model_dump(mode="json")

    # ------------------------------------------------------------------ #
    # GET /emails/{id}/events
    # ------------------------------------------------------------------ #

    async def get_email_events(
        self,
        email_id: str,
        *,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if cursor is not None:
            params["cursor"] = cursor
        if limit is not None:
            params["limit"] = limit
        resp = await self._client.get(f"/emails/{email_id}/events", params=params)
        return EmailEventListResponse.model_validate(_decode(resp)).model_dump(
            mode="json"
        )

    # ------------------------------------------------------------------ #
    # GET /emails/stats
    # ------------------------------------------------------------------ #

    async def get_email_stats(
        self,
        *,
        from_: str | None = None,
        to: str | None = None,
        bucket: str = "day",
    ) -> dict[str, Any]:
        """GET /emails/stats — account-level deliverability aggregates."""
        params: dict[str, str] = {"bucket": bucket}
        if from_:
            params["from"] = from_
        if to:
            params["to"] = to
        resp = await self._client.get("/emails/stats", params=params)
        # by_alias keeps the wire-shaped ``from``/``to`` keys on the way out.
        return EmailStatsResponse.model_validate(_decode(resp)).model_dump(
            mode="json", by_alias=True
        )


def _decode(resp: httpx.Response) -> Any:
    """Return the JSON body on 2xx, raise :class:`HailAPIError` otherwise."""
    if 200 <= resp.status_code < 300:
        return resp.json()
    raise HailAPIError(status=resp.status_code, detail=_error_detail(resp))


def _location(resp: httpx.Response) -> str:
    """Return the ``Location`` header of a 3xx, raise on anything else.

    The /raw and /attachments endpoints 302-redirect to a short-lived
    presigned S3 URL. We capture that URL rather than follow it — the
    bytes are large/binary and belong in the agent's fetch, not the
    JSON tool response. A non-3xx (e.g. 404 outbound) maps through the
    same ``HailAPIError`` path as every other tool.
    """
    if 300 <= resp.status_code < 400:
        loc = resp.headers.get("location")
        if loc:
            return loc
        raise HailAPIError(status=resp.status_code, detail="redirect without Location")
    raise HailAPIError(status=resp.status_code, detail=_error_detail(resp))


def _error_detail(resp: httpx.Response) -> str:
    """Extract a ``detail`` string from a non-success response body."""
    try:
        payload = resp.json()
    except ValueError:
        return resp.text or resp.reason_phrase
    if isinstance(payload, dict) and "detail" in payload:
        d = payload["detail"]
        return d if isinstance(d, str) else str(d)
    return str(payload)


__all__ = ["HailAPIError", "HailClient"]
