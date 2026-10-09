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

import json
import uuid
from typing import Any

import httpx
from hailhq.core.carrier_offer import NumberQuotesResponse
from hailhq.core.config import settings
from hailhq.core.providers.verification import Requirements
from hailhq.core.schemas import (
    AgentCreate,
    AgentListResponse,
    AgentResponse,
    AgentUpdate,
    CallCreate,
    CallListResponse,
    CallResponse,
    ContactCreate,
    ContactEntry,
    ContactListResponse,
    EmailCreate,
    EmailDomainListResponse,
    EmailEventListResponse,
    EmailListResponse,
    EmailResponse,
    EmailStatsResponse,
    EventStreamResponse,
    NumberAcquireRequest,
    NumberQuoteRequest,
    PhoneNumberListResponse,
    PhoneNumberResponse,
    PhoneNumberRoutingUpdate,
    SmsCreate,
    SmsListResponse,
    SmsResponse,
    VerificationResponse,
    WhoamiResponse,
)
from typing_extensions import Self


class HailAPIError(Exception):
    """Non-2xx response from the Hail API.

    ``status`` is the HTTP status code; ``detail`` is the parsed ``detail``
    field from the JSON body when present, otherwise the raw response text.
    ``retry_after`` is the raw ``Retry-After`` header (delta-seconds) when the
    response carried one — set on 429s so the tool layer can tell the agent
    how long to wait; the agent cannot read response headers itself.
    The MCP tool layer converts this to an agent-facing error dict.
    """

    def __init__(
        self,
        status: int,
        detail: str,
        retry_after: str | None = None,
        problems: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(f"hail api error {status}: {detail}")
        self.status = status
        self.detail = detail
        self.retry_after = retry_after
        # Carrier problems from a 422 on POST /verifications, reduced to
        # ``loc`` and ``msg`` only. See ``HailClient.submit_verification``.
        self.problems = problems


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

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> None:
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
        language: str | None = None,
        ai_disclosure: bool = True,
        metadata: dict[str, Any] | None = None,
        tools: list[str] | None = None,
        idempotency_key: str | None = None,
        consent_source: str | None = None,
        consent_obtained_at: str | None = None,
        message_type: str = "informational",
        agent_id: str | None = None,
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
        if language is not None:
            fields["voice_config"] = {"language": language}
        if not ai_disclosure:
            fields["ai_disclosure"] = False
        if metadata is not None:
            fields["metadata"] = metadata
        if tools is not None:
            fields["tools"] = tools
        if consent_source is not None:
            fields["consent_source"] = consent_source
        if consent_obtained_at is not None:
            fields["consent_obtained_at"] = consent_obtained_at
        if agent_id is not None:
            fields["agent_id"] = agent_id

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
    # /agents and PATCH /numbers/{id}
    # ------------------------------------------------------------------ #

    async def list_agents(self) -> dict[str, Any]:
        resp = await self._client.get("/agents")
        return AgentListResponse.model_validate(_decode(resp)).model_dump(mode="json")

    async def create_agent(self, **fields: Any) -> dict[str, Any]:
        """POST /agents — body validated by :class:`AgentCreate` first."""
        body = AgentCreate.model_validate(fields).model_dump(
            mode="json", exclude_unset=True
        )
        resp = await self._client.post("/agents", json=body)
        return AgentResponse.model_validate(_decode(resp)).model_dump(mode="json")

    async def route_number(
        self,
        number_id: str,
        *,
        voice_agent_id: str | None = None,
        sms_agent_id: str | None = None,
        clear_voice: bool = False,
        clear_sms: bool = False,
    ) -> dict[str, Any]:
        """PATCH /numbers/{id} — which agent answers. ``clear_*`` sends null."""
        body: dict[str, Any] = {}
        if voice_agent_id is not None or clear_voice:
            body["voice_agent_id"] = voice_agent_id
        if sms_agent_id is not None or clear_sms:
            body["sms_agent_id"] = sms_agent_id
        PhoneNumberRoutingUpdate.model_validate(body)
        resp = await self._client.patch(f"/numbers/{number_id}", json=body)
        return PhoneNumberResponse.model_validate(_decode(resp)).model_dump(mode="json")

    async def get_agent(self, agent_id: str) -> dict[str, Any]:
        resp = await self._client.get(f"/agents/{agent_id}")
        return AgentResponse.model_validate(_decode(resp)).model_dump(mode="json")

    async def update_agent(self, agent_id: str, **fields: Any) -> dict[str, Any]:
        """PATCH /agents/{id} — body validated by :class:`AgentUpdate` first.

        Only the keys in ``fields`` are sent. A key whose value is ``None``
        goes out as an explicit JSON null (the API's clear convention), so
        the caller decides which Nones to pass.
        """
        body = AgentUpdate.model_validate(fields).model_dump(
            mode="json", exclude_unset=True
        )
        resp = await self._client.patch(f"/agents/{agent_id}", json=body)
        return AgentResponse.model_validate(_decode(resp)).model_dump(mode="json")

    async def delete_agent(self, agent_id: str) -> None:
        """DELETE /agents/{id} — 204; the API clears routing on its numbers."""
        _decode_empty(await self._client.delete(f"/agents/{agent_id}"))

    # ------------------------------------------------------------------ #
    # /numbers
    # ------------------------------------------------------------------ #

    async def quote_numbers(self, **fields: Any) -> dict[str, Any]:
        """POST /numbers/quotes — body validated by :class:`NumberQuoteRequest`."""
        body = NumberQuoteRequest.model_validate(fields).model_dump(
            mode="json", exclude_none=True
        )
        resp = await self._client.post("/numbers/quotes", json=body)
        return NumberQuotesResponse.model_validate(_decode(resp)).model_dump(
            mode="json"
        )

    async def acquire_number(
        self, *, idempotency_key: str | None = None, **fields: Any
    ) -> dict[str, Any]:
        """POST /numbers — buy a quoted number.

        Body validated by :class:`NumberAcquireRequest` before any HTTP.
        Spends money: the API refuses it (409) when ``expected_total_cents``
        differs from the quoted total.
        """
        body = NumberAcquireRequest.model_validate(fields).model_dump(
            mode="json", exclude_none=True
        )
        headers = {"Idempotency-Key": idempotency_key or str(uuid.uuid4())}
        resp = await self._client.post("/numbers", json=body, headers=headers)
        return PhoneNumberResponse.model_validate(_decode(resp)).model_dump(mode="json")

    async def list_numbers(
        self, *, limit: int = 50, cursor: str | None = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        resp = await self._client.get("/numbers", params=params)
        return PhoneNumberListResponse.model_validate(_decode(resp)).model_dump(
            mode="json"
        )

    async def get_number(self, number_id: str) -> dict[str, Any]:
        resp = await self._client.get(f"/numbers/{number_id}")
        return PhoneNumberResponse.model_validate(_decode(resp)).model_dump(mode="json")

    async def delete_number(self, number_id: str) -> None:
        """DELETE /numbers/{id} — 204; releases the number at the carrier."""
        _decode_empty(await self._client.delete(f"/numbers/{number_id}"))

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
        from_name: str | None = None,
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
        if from_name is not None:
            fields["from_name"] = from_name
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
    # /verifications
    # ------------------------------------------------------------------ #

    async def get_verification_requirements(
        self,
        *,
        country_code: str,
        number_type: str,
        subject_type: str = "person",
        provider: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "country_code": country_code,
            "number_type": number_type,
            "subject_type": subject_type,
        }
        if provider is not None:
            params["provider"] = provider
        resp = await self._client.get("/verifications/requirements", params=params)
        return Requirements.model_validate(_decode(resp)).model_dump(mode="json")

    async def list_verifications(self) -> dict[str, Any]:
        resp = await self._client.get("/verifications")
        items = [VerificationResponse.model_validate(r) for r in _decode(resp)]
        return {"items": [i.model_dump(mode="json") for i in items]}

    async def get_verification(self, verification_id: str) -> dict[str, Any]:
        resp = await self._client.get(f"/verifications/{verification_id}")
        return VerificationResponse.model_validate(_decode(resp)).model_dump(
            mode="json"
        )

    async def cancel_verification(self, verification_id: str) -> dict[str, Any]:
        resp = await self._client.delete(f"/verifications/{verification_id}")
        return VerificationResponse.model_validate(_decode(resp)).model_dump(
            mode="json"
        )

    async def submit_verification(
        self,
        *,
        country_code: str,
        number_type: str,
        fields: dict[str, str],
        documents: dict[str, Any],
        files: list[tuple[str, str, bytes]],
        subject_type: str = "person",
        address: dict[str, str] | None = None,
        provider: str | None = None,
    ) -> dict[str, Any]:
        """POST /verifications — multipart/form-data, personal data in transit.

        ``files`` is ``(slot, content_type, bytes)``; each goes out as the
        part ``file.<slot>`` with the fixed filename ``upload``. No
        ``Idempotency-Key``: the route has none (a repeat returns 409).

        A non-2xx response never exposes its raw body. A 422 keeps only
        ``loc`` and ``msg`` of each problem (the carrier's text says what to
        fix); the API's ``input`` and ``ctx`` keys are dropped. Any other
        status keeps ``detail`` only for the API's own short messages, which
        the tool layer decides to show.
        """
        data: dict[str, str] = {
            "country_code": country_code,
            "number_type": number_type,
            "subject_type": subject_type,
            "fields": json.dumps(fields),
            "documents": json.dumps(documents),
        }
        if provider is not None:
            data["provider"] = provider
        if address is not None:
            data["address"] = json.dumps(address)
        parts = [
            (f"file.{slot}", ("upload", content, content_type))
            for slot, content_type, content in files
        ]
        resp = await self._client.post(
            "/verifications", data=data, files=parts, timeout=120.0
        )
        if resp.status_code == 422:
            raise HailAPIError(
                status=422,
                detail="verification rejected",
                problems=_problems(resp),
            )
        if resp.status_code in (409, 503):
            raise HailAPIError(status=resp.status_code, detail=_error_detail(resp))
        if not 200 <= resp.status_code < 300:
            # Unknown failure: keep the status, drop the body.
            raise HailAPIError(
                status=resp.status_code,
                detail="request failed",
                retry_after=resp.headers.get("retry-after"),
            )
        return VerificationResponse.model_validate(resp.json()).model_dump(mode="json")

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

    # ------------------------------------------------------------------ #
    # GET /email-domains
    # ------------------------------------------------------------------ #

    async def list_email_domains(
        self, *, cursor: str | None = None, limit: int | None = None
    ) -> dict[str, Any]:
        """GET /email-domains — the identities this org can send from."""
        params: dict[str, Any] = {}
        if cursor is not None:
            params["cursor"] = cursor
        if limit is not None:
            params["limit"] = limit
        resp = await self._client.get("/email-domains", params=params)
        return EmailDomainListResponse.model_validate(_decode(resp)).model_dump(
            mode="json"
        )

    # ------------------------------------------------------------------ #
    # GET /whoami
    # ------------------------------------------------------------------ #

    async def whoami(self) -> dict[str, Any]:
        """GET /whoami — the caller's identity behind this bearer token."""
        resp = await self._client.get("/whoami")
        return WhoamiResponse.model_validate(_decode(resp)).model_dump(mode="json")


def _decode(resp: httpx.Response) -> Any:
    """Return the JSON body on 2xx, raise :class:`HailAPIError` otherwise."""
    if 200 <= resp.status_code < 300:
        return resp.json()
    raise HailAPIError(
        status=resp.status_code,
        detail=_error_detail(resp),
        retry_after=resp.headers.get("retry-after"),
    )


def _decode_empty(resp: httpx.Response) -> None:
    """Return on 2xx (a 204 has no body), raise :class:`HailAPIError` otherwise."""
    if 200 <= resp.status_code < 300:
        return
    raise HailAPIError(
        status=resp.status_code,
        detail=_error_detail(resp),
        retry_after=resp.headers.get("retry-after"),
    )


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


def _problems(resp: httpx.Response) -> list[dict[str, Any]]:
    """Reduce a 422 body to ``[{"loc": [...], "msg": "..."}]``.

    Nothing else from the body survives (FastAPI adds ``input`` and ``ctx``,
    which can hold submitted values). A body that is not a list of problems
    gives an empty list.
    """
    try:
        detail = resp.json().get("detail")
    except (ValueError, AttributeError):
        return []
    if not isinstance(detail, list):
        return []
    out: list[dict[str, Any]] = []
    for item in detail:
        if not isinstance(item, dict) or not isinstance(item.get("msg"), str):
            continue
        loc = item.get("loc")
        loc = (
            [p if isinstance(p, int) else str(p) for p in loc]
            if isinstance(loc, list)
            else []
        )
        out.append({"loc": loc, "msg": item["msg"]})
    return out


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
