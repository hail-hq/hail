"""MCP tool surface for the Hail API.

Exposes the following tools to the calling agent:

* ``place_call`` — originate an outbound phone call
* ``get_call`` — fetch the current state and transcript of one call
* ``list_calls`` — page through recent calls
* ``get_events`` — page through the event stream (call-narrow or org-wide)
* ``send_email`` — send an outbound email
* ``upload_email_attachment`` — upload a small file (base64) to attach to an outbound email
* ``create_file_upload`` — a one-time link to PUT a file with curl, for an
  email attachment, a verification document or US texting consent evidence
* ``get_email`` — fetch the full record of one email
* ``list_emails`` — page through emails (``direction="inbound"`` for replies)
* ``get_email_raw`` — presigned URL for an inbound email's raw MIME
* ``get_email_attachment`` — presigned URL for one inbound attachment
* ``get_email_events`` — delivery/engagement timeline for one email
* ``get_email_stats`` — account-level deliverability stats (counts, rates, series)
* ``list_email_domains`` — the identities this org can send from, plus the
  address a ``from_``-less send goes out as
* ``whoami`` — the org/user behind the session (its ``email`` is what an
  agent passes as ``reply_to``)
* ``send_sms`` — send an outbound SMS
* ``get_sms`` — fetch the current state of one SMS
* ``list_sms`` — page through recent SMS messages
* ``list_contacts`` — page through the workspace's contacts (members + manual)
* ``lookup_contact`` — find a contact by name/email/phone fragment
* ``create_contact`` — save a manual contact (phone and/or email)
* ``list_agents`` — the workspace's saved agents
* ``get_agent`` — fetch one saved agent
* ``create_agent`` — save an agent (the brain behind calls and texts)
* ``update_agent`` — change an agent; ``clear_fields`` sends explicit nulls
* ``delete_agent`` — delete an agent (destructive; needs ``confirm_name``)
* ``route_number`` — choose which agent answers a number
* ``route_email_domain`` — choose which agent answers an email address
* ``quote_numbers`` — compare live carrier offers for a new number
* ``acquire_number`` — buy a quoted number (spends money; needs ``confirm_total_cents``)
* ``list_numbers`` — page through the workspace's numbers
* ``get_number`` — fetch one number
* ``release_number`` — release a number (destructive; needs ``confirm_e164``)
* ``get_verification_requirements`` — what a carrier needs to verify the org
  for a number type (fields, documents, address)
* ``check_verification`` — run every check Hail can run on a verification
  form without filing anything
* ``check_verification_documents`` — read the files of a number
  verification form: values to fill it, and warnings (expired, upside down,
  wrong document type, a name that differs from the printed one)
* ``submit_verification`` — send the user's details and documents to the
  carrier (irreversible once submitted; needs ``attest_authorized=True``)
* ``resubmit_verification`` — file a rejected number registration again from
  the answers and files Hail kept, with fixes (needs ``attest_authorized=True``)
* ``list_verifications`` — the org's verifications and their states
* ``get_verification`` — one verification; poll or repair its reported step
* ``get_sms_application`` — read the saved application or recover the original sender
* ``preview_sms_application`` — the campaign text a carrier reviews, before submitting
* ``amend_sms_application`` — correct evidence on the same paid sender and row
  (needs ``attest_authorized=True``)
* ``cancel_verification`` — cancel or dismiss (``awaiting_review``/``rejected``, or an
  approved US SMS registration)
* ``confirm_verification`` — enter the code the carrier texted a person
  (when a registration shows ``confirmation: "code"``)

Every tool sets explicit ``ToolAnnotations`` hints (read-only, destructive,
idempotent, open-world) and a human title, set both on the tool and as
``annotations.title`` (directory listings read the latter);
``tests/test_tool_annotations.py`` fails when a new tool is left
unclassified.

The tool docstrings are the agent's only documentation, so each one
spells out the contract (required vs optional fields, how modes combine,
example invocation, terminal-status loop hint).

Errors are returned as ``{"error": "<message>"}`` dicts rather than
raised — agents read tool responses, not exception traces. Field and
shape validation comes from the shared ``hailhq.core.schemas`` request
models (``CallCreate``, ``EmailCreate``, ``SmsCreate``) constructed
inside ``hail_client``; a ``pydantic.ValidationError`` is caught and
mapped to an ``{"error": ...}`` dict. Only the ``<type>:<uuid>``
resource-id shape for ``get_events`` is still checked locally via
``parse_resource_id``.

The tool functions are kept module-importable so unit tests can
call them directly with a constructed ``HailClient``; ``register_tools``
is the MCPServer wiring step. Each registered tool closure accepts an
MCPServer ``Context`` (auto-injected on dispatch) and uses the
``_client_for`` async context manager to obtain a per-call ``HailClient``
(oauth-rs mode, built from the inbound bearer) or the shared singleton
(static-key mode).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import hashlib
import json
import re
import time
import uuid
from collections.abc import AsyncIterator
from typing import Annotated, Any, Literal, get_args

import httpx
from hailhq.core.carrier_offer import SubjectType
from hailhq.core.file_uploads import (
    EVIDENCE_PURPOSE,
    VERIFICATION_FILE_TYPES,
    VERIFICATION_MAX_FILE_BYTES,
    matches_declared_type,
)
from hailhq.core.file_uploads import (
    PURPOSES as _UPLOAD_PURPOSES,
)
from hailhq.core.file_uploads import Purpose as UploadPurpose
from hailhq.core.internal_webhook import fetch_organization_name
from hailhq.core.languages import Language
from hailhq.core.schemas import (
    E164,
    Address,
    AgentCreate,
    AgentStatus,
    AgentUpdate,
    CallStatus,
    Capability,
    EmailAgentScope,
    EmailCreate,
    EmailStatus,
    NumberQuoteRequest,
    NumberType,
    SmsStatus,
    parse_resource_id,
)
from hailhq.core.telemetry import telemetry_enabled
from hailhq.core.telemetry_identity import identity_scope, tag_context
from hailhq.mcp.auth import AuthMode
from hailhq.mcp.hail_client import (
    HailAPIError,
    HailClient,
    InvalidPathIdError,
    PreviewShapeError,
)
from pydantic import BaseModel, Field, StrictBool, ValidationError, WithJsonSchema

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from mcp.types import ToolAnnotations

# --------------------------------------------------------------------------- #
# Error mapping — turns HailAPIError into a stable agent-facing message.
# --------------------------------------------------------------------------- #


def _format_api_error(exc: HailAPIError) -> dict[str, Any]:
    status = exc.status
    if status == 401:
        # Generic — the message surfaces to LLM agents in both oauth-rs
        # (token bad/expired) and static-key (HAIL_API_KEY wrong) modes.
        # Naming a specific env var would mislead the cloud user.
        return {"error": "auth failed: token rejected by Hail API"}
    if status == 404:
        return {"error": "resource not found"}
    if status == 429 and exc.retry_after and exc.retry_after.strip().isdigit():
        # The API's 429 detail points at the Retry-After header, which the
        # agent cannot see, so put the wait in the message itself. Only the
        # delta-seconds form is echoed: a proxy in front of the API may send
        # the HTTP-date form, which would read "retry after <date> seconds".
        return {
            "error": f"hail api error 429: {exc.detail} "
            f"(retry after {exc.retry_after.strip()} seconds)"
        }
    if status in (409, 422, 503):
        return {"error": exc.detail}
    if 500 <= status < 600:
        return {"error": f"hail upstream error: {status}"}
    return {"error": f"hail api error {status}: {exc.detail}"}


def _as_uuid(value: Any) -> str | None:
    """Canonical text of a UUID, or None. Checked before any HTTP call."""
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError):
        return None


def _not_uuid(name: str) -> dict[str, Any]:
    # Never echo the value: it may be a hostile or personal string.
    return {"error": f"{name} must be a UUID"}


def _validation_error_message(exc: ValidationError) -> str:
    """First Pydantic error as a compact agent-facing string.

    Field errors read as ``loc: msg`` (e.g. ``llm.model: Field required``);
    model-level errors (mode A/B, body-required) have an empty loc, so just
    the message. The text comes from ``hailhq.core.schemas`` — the single
    source of the contract — so this layer never restates a rule.
    """
    err = exc.errors()[0]
    loc = ".".join(str(p) for p in err["loc"])
    msg = str(err["msg"])
    return f"{loc}: {msg}" if loc else msg


# --------------------------------------------------------------------------- #
# Tool functions.
#
# Each returns a dict — either the raw API response on success, or
# ``{"error": ...}`` on a known failure mode. Type hints are deliberately
# concrete (no Pydantic models) because the MCP framework derives the
# tool's JSON schema from these annotations + the docstring.
# --------------------------------------------------------------------------- #


async def place_call(
    *,
    client: HailClient,
    to: str,
    recipient_consent: bool,
    system_prompt: str | None = None,
    llm: dict[str, Any] | None = None,
    from_: str | None = None,
    first_message: str | None = None,
    language: str | None = None,
    ai_disclosure: bool = True,
    recording_enabled: bool | None = None,
    metadata: dict[str, Any] | None = None,
    tools: list[str] | None = None,
    idempotency_key: str | None = None,
    consent_source: str | None = None,
    consent_obtained_at: str | None = None,
    message_type: str = "informational",
    agent_id: str | None = None,
) -> dict[str, Any]:
    if idempotency_key is None:
        idempotency_key = str(uuid.uuid4())
    try:
        result = await client.place_call(
            to=to,
            recipient_consent=recipient_consent,
            system_prompt=system_prompt,
            llm=llm,
            from_=from_,
            first_message=first_message,
            language=language,
            ai_disclosure=ai_disclosure,
            recording_enabled=recording_enabled,
            metadata=metadata,
            tools=tools,
            idempotency_key=idempotency_key,
            consent_source=consent_source,
            consent_obtained_at=consent_obtained_at,
            message_type=message_type,
            agent_id=agent_id,
        )
    except ValidationError as exc:
        return {"error": _validation_error_message(exc)}
    except HailAPIError as exc:
        return _format_api_error(exc)
    # Surface the key in the response so the agent can replay this exact
    # request deterministically on a retry. ``setdefault`` so a future
    # server-side echo isn't clobbered.
    if isinstance(result, dict):
        result.setdefault("idempotency_key", idempotency_key)
    return result


async def send_email(
    *,
    client: HailClient,
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
    if idempotency_key is None:
        idempotency_key = str(uuid.uuid4())
    try:
        result = await client.send_email(
            to=to,
            subject=subject,
            recipient_consent=recipient_consent,
            body_text=body_text,
            body_html=body_html,
            from_=from_,
            from_name=from_name,
            cc=cc,
            bcc=bcc,
            reply_to=reply_to,
            metadata=metadata,
            idempotency_key=idempotency_key,
            consent_source=consent_source,
            consent_obtained_at=consent_obtained_at,
            message_type=message_type,
            attachment_ids=attachment_ids,
        )
    except ValidationError as exc:
        return {"error": _validation_error_message(exc)}
    except HailAPIError as exc:
        return _format_api_error(exc)
    if isinstance(result, dict):
        result.setdefault("idempotency_key", idempotency_key)
    return result


async def upload_email_attachment(
    *, client: HailClient, content_base64: str, filename: str, content_type: str
) -> dict[str, Any]:
    try:
        content = base64.b64decode(content_base64)
    except Exception:
        return {"error": "content_base64: invalid base64 encoding"}
    try:
        return await client.upload_email_attachment(
            filename=filename, content=content, content_type=content_type
        )
    except HailAPIError as exc:
        return _format_api_error(exc)


async def get_call(*, client: HailClient, call_id: str) -> dict[str, Any]:
    try:
        return await client.get_call(call_id)
    except ValidationError as exc:
        return {"error": _validation_error_message(exc)}
    except InvalidPathIdError as exc:
        return {"error": str(exc)}
    except HailAPIError as exc:
        return _format_api_error(exc)


async def list_calls(
    *,
    client: HailClient,
    cursor: str | None = None,
    limit: int = 50,
    status: str | None = None,
    to: str | None = None,
) -> dict[str, Any]:
    try:
        return await client.list_calls(cursor=cursor, limit=limit, status=status, to=to)
    except ValidationError as exc:
        return {"error": _validation_error_message(exc)}
    except HailAPIError as exc:
        return _format_api_error(exc)


async def send_sms(
    *,
    client: HailClient,
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
    if idempotency_key is None:
        idempotency_key = str(uuid.uuid4())
    try:
        result = await client.send_sms(
            to=to,
            body=body,
            recipient_consent=recipient_consent,
            from_=from_,
            metadata=metadata,
            idempotency_key=idempotency_key,
            consent_source=consent_source,
            consent_obtained_at=consent_obtained_at,
            message_type=message_type,
        )
    except ValidationError as exc:
        return {"error": _validation_error_message(exc)}
    except HailAPIError as exc:
        return _format_api_error(exc)
    if isinstance(result, dict):
        result.setdefault("idempotency_key", idempotency_key)
    return result


async def get_sms(*, client: HailClient, sms_id: str) -> dict[str, Any]:
    try:
        return await client.get_sms(sms_id)
    except ValidationError as exc:
        return {"error": _validation_error_message(exc)}
    except InvalidPathIdError as exc:
        return {"error": str(exc)}
    except HailAPIError as exc:
        return _format_api_error(exc)


async def list_sms(
    *,
    client: HailClient,
    cursor: str | None = None,
    limit: int = 50,
    status: str | None = None,
    to: str | None = None,
) -> dict[str, Any]:
    try:
        return await client.list_sms(cursor=cursor, limit=limit, status=status, to=to)
    except ValidationError as exc:
        return {"error": _validation_error_message(exc)}
    except HailAPIError as exc:
        return _format_api_error(exc)


async def get_email(*, client: HailClient, email_id: str) -> dict[str, Any]:
    try:
        return await client.get_email(email_id)
    except ValidationError as exc:
        return {"error": _validation_error_message(exc)}
    except InvalidPathIdError as exc:
        return {"error": str(exc)}
    except HailAPIError as exc:
        return _format_api_error(exc)


async def list_emails(
    *,
    client: HailClient,
    cursor: str | None = None,
    limit: int = 50,
    status: str | None = None,
    direction: str | None = None,
) -> dict[str, Any]:
    try:
        return await client.list_emails(
            cursor=cursor, limit=limit, status=status, direction=direction
        )
    except ValidationError as exc:
        return {"error": _validation_error_message(exc)}
    except HailAPIError as exc:
        return _format_api_error(exc)


async def get_email_raw(*, client: HailClient, email_id: str) -> dict[str, Any]:
    try:
        return await client.get_email_raw(email_id)
    except ValidationError as exc:
        return {"error": _validation_error_message(exc)}
    except InvalidPathIdError as exc:
        return {"error": str(exc)}
    except HailAPIError as exc:
        return _format_api_error(exc)


async def get_email_attachment(
    *, client: HailClient, email_id: str, attachment_id: str
) -> dict[str, Any]:
    try:
        return await client.get_email_attachment(email_id, attachment_id)
    except ValidationError as exc:
        return {"error": _validation_error_message(exc)}
    except InvalidPathIdError as exc:
        return {"error": str(exc)}
    except HailAPIError as exc:
        return _format_api_error(exc)


async def get_email_events(
    *,
    client: HailClient,
    email_id: str,
    cursor: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    try:
        return await client.get_email_events(email_id, cursor=cursor, limit=limit)
    except ValidationError as exc:
        return {"error": _validation_error_message(exc)}
    except InvalidPathIdError as exc:
        return {"error": str(exc)}
    except HailAPIError as exc:
        return _format_api_error(exc)


async def get_email_stats(
    *,
    client: HailClient,
    from_: str | None = None,
    to: str | None = None,
    bucket: str = "day",
) -> dict[str, Any]:
    try:
        return await client.get_email_stats(from_=from_, to=to, bucket=bucket)
    except ValidationError as exc:
        return {"error": _validation_error_message(exc)}
    except HailAPIError as exc:
        return _format_api_error(exc)


async def list_email_domains(
    *,
    client: HailClient,
    cursor: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    try:
        return await client.list_email_domains(cursor=cursor, limit=limit)
    except ValidationError as exc:
        return {"error": _validation_error_message(exc)}
    except HailAPIError as exc:
        return _format_api_error(exc)


async def whoami(*, client: HailClient) -> dict[str, Any]:
    try:
        return await client.whoami()
    except ValidationError as exc:
        return {"error": _validation_error_message(exc)}
    except HailAPIError as exc:
        return _format_api_error(exc)


async def get_events(
    *,
    client: HailClient,
    id: str | None = None,
    kind: str | None = None,
    cursor: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    if id is not None:
        try:
            parse_resource_id(id)
        except ValueError as exc:
            return {"error": str(exc)}
    try:
        return await client.get_events(id=id, kind=kind, cursor=cursor, limit=limit)
    except ValidationError as exc:
        return {"error": _validation_error_message(exc)}
    except HailAPIError as exc:
        return _format_api_error(exc)


async def list_contacts(
    *,
    client: HailClient,
    q: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    try:
        return await client.list_contacts(q=q, limit=limit)
    except ValidationError as exc:
        return {"error": _validation_error_message(exc)}
    except HailAPIError as exc:
        return _format_api_error(exc)


async def lookup_contact(*, client: HailClient, query: str) -> dict[str, Any]:
    if not query or not query.strip():
        return {"error": "query must be a non-empty name, email, or phone fragment"}
    try:
        return await client.list_contacts(q=query, limit=10)
    except ValidationError as exc:
        return {"error": _validation_error_message(exc)}
    except HailAPIError as exc:
        return _format_api_error(exc)


async def create_contact(
    *,
    client: HailClient,
    name: str,
    phone_e164: str | None = None,
    email: str | None = None,
) -> dict[str, Any]:
    try:
        return await client.create_contact(
            name=name, phone_e164=phone_e164, email=email
        )
    except ValidationError as exc:
        return {"error": _validation_error_message(exc)}
    except HailAPIError as exc:
        return _format_api_error(exc)


async def list_agents(*, client: HailClient) -> dict[str, Any]:
    try:
        return await client.list_agents()
    except HailAPIError as exc:
        return _format_api_error(exc)


async def create_agent(*, client: HailClient, **fields: Any) -> dict[str, Any]:
    try:
        return await client.create_agent(**fields)
    except ValidationError as exc:
        return {"error": _validation_error_message(exc)}
    except HailAPIError as exc:
        return _format_api_error(exc)


# Hail does not tell agents which carrier backs a number, an offer or a
# verification. The public API no longer returns these keys; this stays as a
# guard against an older API that still does.
_CARRIER_KEYS = frozenset(
    {
        "provider",
        "unavailable_providers",
        "messaging_service_sid",
        # Not ``verification_id``: on a number it is Hail's own verification
        # id. Responses are parsed with the public models, which carry no
        # carrier id under that name.
        "address_id",
    }
)


def _drop_carrier(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    return {k: v for k, v in value.items() if k not in _CARRIER_KEYS}


def _without_carrier(result: Any) -> Any:
    result = _drop_carrier(result)
    if isinstance(result, dict):
        for key in ("offers", "items"):
            if isinstance(result.get(key), list):
                result[key] = [_drop_carrier(item) for item in result[key]]
    return result


async def route_number(
    *,
    client: HailClient,
    number_id: str,
    voice_agent_id: str | None = None,
    sms_agent_id: str | None = None,
    clear_voice: bool = False,
    clear_sms: bool = False,
) -> dict[str, Any]:
    number_id_ = _as_uuid(number_id)
    if number_id_ is None:
        return _not_uuid("number_id")
    try:
        return _without_carrier(
            await client.route_number(
                number_id_,
                voice_agent_id=voice_agent_id,
                sms_agent_id=sms_agent_id,
                clear_voice=clear_voice,
                clear_sms=clear_sms,
            )
        )
    except ValidationError as exc:
        return {"error": _validation_error_message(exc)}
    except HailAPIError as exc:
        return _format_api_error(exc)


async def route_email_domain(
    *,
    client: HailClient,
    domain_id: str,
    email_agent_id: str | None = None,
    email_agent_scope: str | None = None,
    clear_agent: bool = False,
) -> dict[str, Any]:
    domain_id_ = _as_uuid(domain_id)
    if domain_id_ is None:
        return _not_uuid("domain_id")
    if clear_agent and email_agent_id is not None:
        return {"error": "use email_agent_id or clear_agent, not both"}
    try:
        return await client.route_email_domain(
            domain_id_,
            email_agent_id=email_agent_id,
            email_agent_scope=email_agent_scope,
            clear_agent=clear_agent,
        )
    except ValidationError as exc:
        return {"error": _validation_error_message(exc)}
    except HailAPIError as exc:
        return _format_api_error(exc)


async def quote_numbers(
    *,
    client: HailClient,
    country_code: str,
    capabilities: list[str] | None = None,
    number_type: str | None = None,
    area_code: str | None = None,
    city: str | None = None,
    region: str | None = None,
) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "country_code": country_code,
        "capabilities": ["voice", "sms"] if capabilities is None else capabilities,
    }
    if number_type is not None:
        fields["number_type"] = number_type
    if area_code is not None:
        fields["area_code"] = area_code
    if city is not None:
        fields["city"] = city
    if region is not None:
        fields["region"] = region
    try:
        return _without_carrier(await client.quote_numbers(**fields))
    except ValidationError as exc:
        return {"error": _validation_error_message(exc)}
    except HailAPIError as exc:
        return _format_api_error(exc)


async def acquire_number(
    *,
    client: HailClient,
    quote_id: str,
    country_code: str,
    number_type: str,
    confirm_total_cents: int,
    idempotency_key: str | None = None,
    subject_type: str | None = None,
    verification_id: str | None = None,
) -> dict[str, Any]:
    quote_id_ = _as_uuid(quote_id)
    if quote_id_ is None:
        return _not_uuid("quote_id")
    verification_id_ = None
    if verification_id is not None:
        verification_id_ = _as_uuid(verification_id)
        if verification_id_ is None:
            return _not_uuid("verification_id")
    if subject_type is not None and subject_type not in _SUBJECT_TYPES:
        return {"error": f"{_SUBJECT_TYPE_ERROR}."}
    if idempotency_key is None:
        idempotency_key = str(uuid.uuid4())
    try:
        result = await client.acquire_number(
            quote_id=quote_id_,
            country_code=country_code,
            number_type=number_type,
            expected_total_cents=confirm_total_cents,
            idempotency_key=idempotency_key,
            **({"subject_type": subject_type} if subject_type else {}),
            **({"verification_id": verification_id_} if verification_id_ else {}),
        )
    except ValidationError as exc:
        return {"error": _validation_error_message(exc)}
    except httpx.TransportError:
        return {
            "error": "request timed out; the purchase may have completed. "
            "Call list_numbers to check, or retry with the same quote_id and "
            "this idempotency_key. Do not request a new quote.",
            "idempotency_key": idempotency_key,
        }
    except HailAPIError as exc:
        error = _format_api_error(exc)
        if exc.status == 429 or exc.status >= 500:
            # Nothing was cached under the key: a retry with it can succeed.
            return {**error, "idempotency_key": idempotency_key}
        # The API caches a 4xx under the key. A retry with it replays the
        # same failure (or 409s on a changed body), so do not hand it back.
        return error
    # Echo the key so the agent can retry this exact purchase safely.
    result.setdefault("idempotency_key", idempotency_key)
    return _without_carrier(result)


async def list_numbers(
    *, client: HailClient, limit: int = 50, cursor: str | None = None
) -> dict[str, Any]:
    try:
        return _without_carrier(await client.list_numbers(limit=limit, cursor=cursor))
    except HailAPIError as exc:
        return _format_api_error(exc)


async def list_number_regions(*, client: HailClient) -> dict[str, Any]:
    try:
        return await client.list_number_regions()
    except HailAPIError as exc:
        return _format_api_error(exc)


async def get_number(*, client: HailClient, number_id: str) -> dict[str, Any]:
    number_id_ = _as_uuid(number_id)
    if number_id_ is None:
        return _not_uuid("number_id")
    try:
        return _without_carrier(await client.get_number(number_id_))
    except HailAPIError as exc:
        return _format_api_error(exc)


def _squash_whitespace(value: str) -> str:
    return "".join(value.split())


async def release_number(
    *, client: HailClient, number_id: str, confirm_e164: str
) -> dict[str, Any]:
    # Destructive: read the number first and release only when the caller
    # typed back its exact E.164. A mismatch makes no DELETE call.
    number_id_ = _as_uuid(number_id)
    if number_id_ is None:
        return _not_uuid("number_id")
    try:
        number = await client.get_number(number_id_)
        e164 = str(number["e164"])
        if _squash_whitespace(confirm_e164) != _squash_whitespace(e164):
            return {
                "error": "confirm_e164 does not match this number. "
                f"Pass {e164} to release it. Nothing was released."
            }
        await client.delete_number(number_id_)
    except HailAPIError as exc:
        return _format_api_error(exc)
    if number.get("provisioning_state") == "failed":
        # DELETE on a failed order only hides the row; nothing was released.
        return {"dismissed": True, "number_id": number_id, "e164": e164}
    return {"released": True, "number_id": number_id, "e164": e164}


# --------------------------------------------------------------------------- #
# Carrier verification.
#
# ``submit_verification`` carries personal data and identity documents. Rules
# for this code: never log an argument, never put a submitted value into a
# return value or an error string (errors name slots, counts and sizes only),
# and never return exception text.
# --------------------------------------------------------------------------- #

_TRANSPORT_ERROR = "request timed out or failed; try again"
_UNEXPECTED_ERROR = "unexpected error; try again"
_VERIFICATION_FILE_TYPES = VERIFICATION_FILE_TYPES
_VERIFICATION_MAX_FILE_BYTES = VERIFICATION_MAX_FILE_BYTES
# The API caps the whole 30 MiB multipart body, so the files get 29 MiB and
# the rest (details, boundaries) fits in the last MiB.
_VERIFICATION_MAX_TOTAL_BYTES = 29 * 1024 * 1024
_VERIFICATION_MAX_DETAILS_BYTES = 64 * 1024
_SLOT_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
_SLOT_RULE = "use 1 to 64 letters, digits, - or _"
_VERIFICATION_MAX_FILES = 20
_VERIFICATION_MAX_FIELDS = 100
# Largest base64 text a legal file can have; checked before decoding.
_VERIFICATION_MAX_B64_CHARS = (_VERIFICATION_MAX_FILE_BYTES + 2) // 3 * 4


def _is_str_map(value: Any) -> bool:
    return isinstance(value, dict) and all(
        isinstance(k, str) and isinstance(v, str) for k, v in value.items()
    )


# The API's verification purposes (``PURPOSES`` in api/routes/verifications.py;
# tests/test_schema_matches_api.py keeps them equal).
VerificationPurpose = Literal["number", "sms"]
_VERIFICATION_PURPOSES = get_args(VerificationPurpose)
_PURPOSE_ERROR = "purpose must be 'number' or 'sms'"
_SUBJECT_TYPES = get_args(SubjectType)
_SUBJECT_TYPE_ERROR = (
    "subject_type must be 'person' or 'business': ask the user whether this "
    "is for them or for their company"
)
_QUOTE_REQUIRED = (
    "quote_id is required for purpose='number': pass the quote_id of the "
    "offer from quote_numbers"
)


# Typed in the JSON schema only. Validation stays ``Any`` so the shape errors
# come from ``_verification_shape_error``, which never echoes a value.
_JsonObject = Annotated[Any, WithJsonSchema({"type": "object"})]


def _verification_shape_error(
    fields: Any, documents: Any, files: Any, address: Any
) -> str | None:
    """Name the argument with the wrong shape. Never echoes a value."""
    if not _is_str_map(fields):
        return "fields must be an object of string values"
    if address is not None and not _is_str_map(address):
        return "address must be an object of string values"
    if not isinstance(documents, dict) or not all(
        isinstance(k, str) and isinstance(v, dict) for k, v in documents.items()
    ):
        return "documents must be an object of {slot: {option, fields}} objects"
    if not isinstance(files, list):
        return (
            "files must be a list of {slot, upload_id} or "
            "{slot, content_type, content_base64}"
        )
    return None


def _check_verification_files(
    files: list[dict[str, str]], documents: dict[str, Any]
) -> tuple[str | None, list[tuple[str, str, bytes]], list[tuple[str, str]]]:
    """Validate and decode the files. Returns ``(error, decoded, uploads)``:
    ``decoded`` holds ``(slot, content_type, bytes)`` for base64 entries,
    ``uploads`` holds ``(slot, upload_id)`` for entries that name an upload.

    Every message names an index, a slot, a count or a size. None of them
    contains a submitted value or any part of the base64 text.
    """
    decoded: list[tuple[str, str, bytes]] = []
    uploads: list[tuple[str, str]] = []
    seen: set[str] = set()
    total = 0
    if any(
        not isinstance(key, str) or not _SLOT_NAME.fullmatch(key) for key in documents
    ):
        return (
            f"documents has a key that is not a valid slot name ({_SLOT_RULE})",
            [],
            [],
        )
    if len(files) > _VERIFICATION_MAX_FILES:
        return (
            f"too many files: {len(files)} (at most {_VERIFICATION_MAX_FILES})",
            [],
            [],
        )
    for i, entry in enumerate(files):
        if isinstance(entry, dict) and "upload_id" in entry:
            if set(entry) != {"slot", "upload_id"} or not isinstance(
                entry.get("slot"), str
            ):
                return (
                    (
                        f"files[{i}] with upload_id must have exactly the "
                        "string keys slot and upload_id"
                    ),
                    [],
                    [],
                )
            slot = entry["slot"]
            # Validated before it appears in any message below.
            if not _SLOT_NAME.fullmatch(slot):
                return (
                    f"files[{i}]: slot is not a valid slot name ({_SLOT_RULE})",
                    [],
                    [],
                )
            upload_id = _as_uuid(entry["upload_id"])
            if upload_id is None:
                return (
                    (
                        f"files[{i}] (slot {slot!r}): upload_id must be the id "
                        "create_file_upload returned"
                    ),
                    [],
                    [],
                )
            if slot in seen:
                return f"files[{i}]: more than one file for slot {slot!r}", [], []
            seen.add(slot)
            if slot not in documents:
                return f"files[{i}]: slot {slot!r} is not a key in documents", [], []
            uploads.append((slot, upload_id))
            continue
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("slot"), str)
            or not isinstance(entry.get("content_type"), str)
            or not isinstance(entry.get("content_base64"), str)
        ):
            return (
                (
                    f"files[{i}] must be an object with string keys slot, "
                    "content_type and content_base64, or slot and upload_id"
                ),
                [],
                [],
            )
        slot = entry["slot"]
        # Validated before it appears in any message below.
        if not _SLOT_NAME.fullmatch(slot):
            return f"files[{i}]: slot is not a valid slot name ({_SLOT_RULE})", [], []
        if entry["content_type"] not in _VERIFICATION_FILE_TYPES:
            return (
                f"files[{i}] (slot {slot!r}): content_type must be one of "
                + ", ".join(_VERIFICATION_FILE_TYPES),
                [],
                [],
            )
        if slot in seen:
            return f"files[{i}]: more than one file for slot {slot!r}", [], []
        seen.add(slot)
        if slot not in documents:
            return f"files[{i}]: slot {slot!r} is not a key in documents", [], []
        b64 = entry["content_base64"]
        if len(b64) > _VERIFICATION_MAX_B64_CHARS:
            return (
                (
                    f"files[{i}] (slot {slot!r}): file is larger than "
                    f"{_VERIFICATION_MAX_FILE_BYTES // (1024 * 1024)} MiB"
                ),
                [],
                [],
            )
        try:
            content = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError):
            return (
                f"files[{i}] (slot {slot!r}): content_base64 is not valid base64",
                [],
                [],
            )
        if not content:
            return f"files[{i}] (slot {slot!r}): file is empty", [], []
        if len(content) > _VERIFICATION_MAX_FILE_BYTES:
            return (
                (
                    f"files[{i}] (slot {slot!r}): file is {len(content)} bytes; "
                    f"the limit is {_VERIFICATION_MAX_FILE_BYTES} bytes"
                ),
                [],
                [],
            )
        if not matches_declared_type(content, entry["content_type"]):
            declared = entry["content_type"]
            return f"files[{i}]: the content does not look like {declared}", [], []
        total += len(content)
        if total > _VERIFICATION_MAX_TOTAL_BYTES:
            limit_mib = _VERIFICATION_MAX_TOTAL_BYTES // (1024 * 1024)
            return f"files total more than {limit_mib} MiB", [], []
        decoded.append((slot, entry["content_type"], content))
    return None, decoded, uploads


async def get_verification_requirements(
    *,
    client: HailClient,
    country_code: str,
    number_type: str,
    subject_type: str,
    quote_id: str | None = None,
    number_id: str | None = None,
    purpose: str = "number",
) -> dict[str, Any]:
    if subject_type not in _SUBJECT_TYPES:
        return {"error": f"{_SUBJECT_TYPE_ERROR}."}
    if purpose not in _VERIFICATION_PURPOSES:
        return {"error": f"{_PURPOSE_ERROR}."}
    if purpose == "number" and quote_id is None:
        return {"error": f"{_QUOTE_REQUIRED}."}
    if quote_id is not None and _as_uuid(quote_id) is None:
        return _not_uuid("quote_id")
    if number_id is not None and _as_uuid(number_id) is None:
        return _not_uuid("number_id")
    try:
        return _without_carrier(
            await client.get_verification_requirements(
                country_code=country_code,
                number_type=number_type,
                subject_type=subject_type,
                quote_id=quote_id,
                number_id=number_id,
                purpose=purpose,
            )
        )
    except HailAPIError as exc:
        return _format_api_error(exc)
    except httpx.TransportError:
        return {"error": _TRANSPORT_ERROR}
    except Exception:
        return {"error": _UNEXPECTED_ERROR}


async def list_verifications(
    *, client: HailClient, purpose: str | None = None
) -> dict[str, Any]:
    if purpose is not None and purpose not in _VERIFICATION_PURPOSES:
        return {"error": f"{_PURPOSE_ERROR}."}
    try:
        return _without_carrier(await client.list_verifications(purpose=purpose))
    except HailAPIError as exc:
        return _format_api_error(exc)
    except httpx.TransportError:
        return {"error": _TRANSPORT_ERROR}
    except Exception:
        return {"error": _UNEXPECTED_ERROR}


async def get_verification(
    *, client: HailClient, verification_id: str
) -> dict[str, Any]:
    verification_id_ = _as_uuid(verification_id)
    if verification_id_ is None:
        return _not_uuid("verification_id")
    try:
        return _without_carrier(await client.get_verification(verification_id_))
    except HailAPIError as exc:
        return _format_api_error(exc)
    except httpx.TransportError:
        return {"error": _TRANSPORT_ERROR}
    except Exception:
        # No exception text: a decode or validation error can carry a body.
        return {"error": _UNEXPECTED_ERROR}


async def get_sms_application(
    *, client: HailClient, verification_id: str
) -> dict[str, Any]:
    verification_id_ = _as_uuid(verification_id)
    if verification_id_ is None:
        return _not_uuid("verification_id")
    try:
        return _without_carrier(await client.get_sms_application(verification_id_))
    except HailAPIError as exc:
        return _format_api_error(exc)
    except httpx.TransportError:
        return {"error": _TRANSPORT_ERROR}
    except Exception:
        return {"error": _UNEXPECTED_ERROR}


async def preview_sms_application(
    *,
    client: HailClient,
    subject_type: Any,
    fields: Any,
    address: Any = None,
    number_id: Any = None,
) -> dict[str, Any]:
    if subject_type not in _SUBJECT_TYPES:
        return {"error": f"{_SUBJECT_TYPE_ERROR}. Nothing was sent."}
    number_id_ = None
    if number_id is not None:
        number_id_ = _as_uuid(number_id)
        if number_id_ is None:
            return _not_uuid("number_id")
    input_error = _application_input_error(fields, address)
    if input_error:
        return {"error": f"{input_error}. Nothing was sent."}
    try:
        return await client.preview_sms_application(
            subject_type=subject_type,
            fields=fields,
            address=address,
            number_id=number_id_,
        )
    except HailAPIError as exc:
        if exc.status in (404, 422):
            return _answer_problems(
                exc,
                "Correct the listed fields with the user, then preview again before submitting.",
                "Nothing was saved. Check the answers against get_verification_requirements with the user.",
            )
        return _format_api_error(exc)
    except PreviewShapeError:
        return {"error": _VERSION_MISMATCH}
    except httpx.TransportError:
        return {"error": _TRANSPORT_ERROR}
    except Exception:
        return {"error": _UNEXPECTED_ERROR}


def _details_bytes(*parts: Any) -> int | None:
    """The UTF-8 size of the parts as JSON, so non-ASCII text is not
    over-counted; None when a part is not valid JSON text."""
    try:
        return sum(len(json.dumps(p, ensure_ascii=False).encode()) for p in parts)
    except (TypeError, ValueError):
        return None


def _application_input_error(fields: Any, address: Any) -> str | None:
    """The shape or size problem with application answers, or None."""
    shape_error = _verification_shape_error(fields, {}, [], address)
    if shape_error:
        return shape_error
    if len(fields) > _VERIFICATION_MAX_FIELDS:
        return f"too many fields (at most {_VERIFICATION_MAX_FIELDS})"
    size = _details_bytes(fields, address or {})
    if size is None:
        return "fields and address must be valid text"
    if size > _VERIFICATION_MAX_DETAILS_BYTES:
        return f"fields and address exceed {_VERIFICATION_MAX_DETAILS_BYTES} bytes"
    return None


# Request parts a 422 can name that are not the user's answers.
_NOT_ANSWERS = {"number_id", "subject_type"}


def _answer_problems(exc: HailAPIError, fix: str, fallback: str) -> dict[str, Any]:
    """The reply to a 404 or 422 about an application. Problems on the
    user's answers get `fix`. Other problems (pick a number, contact support)
    add the API's own messages, read from the stripped problems or the API's
    plain sentence only, so no submitted value or proxy page is echoed."""
    problems = exc.problems or []
    # A user's answer is a body part that is not a request setting.
    others = [
        p["msg"]
        for p in problems
        if not (
            len(p.get("loc", [])) >= 2
            and p["loc"][0] == "body"
            and str(p["loc"][1]) not in _NOT_ANSWERS
        )
    ]
    if problems:
        parts = [*others, fix] if len(others) < len(problems) else others
        return {"error": " ".join(_sentence(m) for m in parts), "problems": problems}
    message = (exc.message or "").strip()
    # FastAPI's own "Not Found" means the route is missing: an older API.
    if exc.status == 404 and message == "Not Found":
        return {"error": _VERSION_MISMATCH}
    # A plain API sentence, not an HTML page.
    if message and len(message) <= 500 and not message.startswith("<"):
        return {"error": _sentence(message)}
    if exc.status == 404:
        return _format_api_error(exc)
    return {"error": f"the API refused the request ({exc.status}). {fallback}"}


def _sentence(text: str) -> str:
    """The text with an end mark, unless it already has one (also before a
    closing quote or bracket)."""
    text = text.strip()
    return text if text.rstrip("\"')]").endswith((".", "!", "?")) else f"{text}."


_VERSION_MISMATCH = (
    "the API and this MCP version differ; retrying will not help. "
    "Ask the user to review the application in the Hail console, or contact support."
)


async def amend_sms_application(
    *,
    client: HailClient,
    verification_id: str,
    fields: Any,
    attest_authorized: bool,
    address: Any = None,
) -> dict[str, Any]:
    verification_id_ = _as_uuid(verification_id)
    if verification_id_ is None:
        return _not_uuid("verification_id")
    if attest_authorized is not True:
        return {
            "error": "attest_authorized must be true only after the user reviews and authorizes these corrected answers. Nothing was sent."
        }
    if address is None:
        return {
            "error": "address is required; use the saved address only if still correct. Nothing was sent."
        }
    input_error = _application_input_error(fields, address)
    if input_error:
        return {"error": f"{input_error}. Nothing was sent."}
    try:
        return _without_carrier(
            await client.amend_sms_application(
                verification_id_, fields=fields, address=address
            )
        )
    except HailAPIError as exc:
        if exc.status == 422:
            return _answer_problems(
                exc,
                "Correct the listed application fields with the user, then amend the same verification_id.",
                "Nothing was changed. Keep the same verification_id: check the answers with the user and amend again. Do not cancel or submit a new one.",
            )
        return _format_api_error(exc)
    except httpx.TransportError:
        return {
            "error": "request timed out; the amendment may have been saved. Call get_verification with the same verification_id before retrying. Do not create or cancel a registration."
        }
    except Exception:
        return {
            "error": "unexpected error; call get_verification with the same verification_id to check the amendment before retrying."
        }


async def cancel_verification(
    *, client: HailClient, verification_id: str
) -> dict[str, Any]:
    verification_id_ = _as_uuid(verification_id)
    if verification_id_ is None:
        return _not_uuid("verification_id")
    try:
        return _without_carrier(await client.cancel_verification(verification_id_))
    except HailAPIError as exc:
        return _format_api_error(exc)
    except httpx.TransportError:
        return {"error": _TRANSPORT_ERROR}
    except Exception:
        return {"error": _UNEXPECTED_ERROR}


async def confirm_verification(
    *, client: HailClient, verification_id: str, code: Any
) -> dict[str, Any]:
    verification_id_ = _as_uuid(verification_id)
    if verification_id_ is None:
        return _not_uuid("verification_id")
    # Checked here so a bad code is never echoed back in an error.
    if not isinstance(code, str) or not 4 <= len(code.strip()) <= 10:
        return {"error": "code must be the 4 to 10 characters the carrier texted"}
    try:
        return _without_carrier(
            await client.confirm_verification(verification_id_, code.strip())
        )
    except HailAPIError as exc:
        return _format_api_error(exc)
    except httpx.TransportError:
        return {"error": _TRANSPORT_ERROR}
    except Exception:
        return {"error": _UNEXPECTED_ERROR}


def _verification_request(
    *,
    country_code: str,
    number_type: str,
    fields: Any,
    documents: Any,
    files: Any,
    subject_type: str | None,
    address: Any,
    quote_id: str | None,
    number_id: str | None,
    purpose: str,
) -> tuple[str | None, dict[str, Any]]:
    """Every local check of a verification form. Returns ``(error, form)``:
    an error ending in "Nothing was sent.", or the keyword arguments for
    ``HailClient.submit_verification`` / ``check_verification``. Shape checks
    live here, not in the tool signature, so each error names the argument
    and the rule in its own words and never echoes a value."""
    if subject_type not in _SUBJECT_TYPES:
        return f"{_SUBJECT_TYPE_ERROR}. Nothing was sent.", {}
    if purpose not in _VERIFICATION_PURPOSES:
        return f"{_PURPOSE_ERROR}. Nothing was sent.", {}
    if purpose == "number" and quote_id is None:
        return f"{_QUOTE_REQUIRED}. Nothing was sent.", {}
    if quote_id is not None and _as_uuid(quote_id) is None:
        return "quote_id must be a UUID. Nothing was sent.", {}
    if number_id is not None and _as_uuid(number_id) is None:
        return "number_id must be a UUID. Nothing was sent.", {}
    shape_error = _verification_shape_error(fields, documents, files, address)
    if shape_error:
        return f"{shape_error}. Nothing was sent.", {}
    if len(fields) > _VERIFICATION_MAX_FIELDS:
        return (
            f"too many fields: {len(fields)} "
            f"(at most {_VERIFICATION_MAX_FIELDS}). Nothing was sent."
        ), {}
    details_bytes = _details_bytes(fields, address or {}, documents)
    if details_bytes is None:
        return (
            "fields, address and documents must be valid JSON text. Nothing was sent."
        ), {}
    if details_bytes > _VERIFICATION_MAX_DETAILS_BYTES:
        return (
            f"fields, address and documents total {details_bytes} "
            f"bytes; the limit is {_VERIFICATION_MAX_DETAILS_BYTES} bytes. "
            "Nothing was sent."
        ), {}
    error, decoded, uploads = _check_verification_files(files, documents)
    if error:
        return f"{error}. Nothing was sent.", {}
    return None, {
        "country_code": country_code,
        "number_type": number_type,
        "subject_type": subject_type,
        "fields": fields,
        "documents": documents,
        "files": decoded,
        "uploads": uploads,
        "address": address,
        "quote_id": quote_id,
        "number_id": number_id,
        "purpose": purpose,
    }


async def submit_verification(
    *,
    client: HailClient,
    country_code: str,
    number_type: str,
    fields: Any,
    documents: Any,
    files: Any,
    attest_authorized: bool,
    subject_type: str | None,
    address: Any = None,
    quote_id: str | None = None,
    number_id: str | None = None,
    purpose: str = "number",
) -> dict[str, Any]:
    if attest_authorized is not True:
        return {
            "error": "attest_authorized must be true. Pass true only if the "
            "person or company in these details asked for this submission "
            "and gave you the details and files in this conversation. "
            "Nothing was sent."
        }
    error, form = _verification_request(
        country_code=country_code,
        number_type=number_type,
        fields=fields,
        documents=documents,
        files=files,
        subject_type=subject_type,
        address=address,
        quote_id=quote_id,
        number_id=number_id,
        purpose=purpose,
    )
    if error:
        return {"error": error}
    try:
        return _without_carrier(await client.submit_verification(**form))
    except httpx.TransportError:
        return {
            "error": "request timed out; the submission may have gone through. "
            "Call list_verifications to check before submitting again."
        }
    except HailAPIError as exc:
        return _submit_error(exc)
    except Exception:
        # No exception text: it could carry a request or response body.
        return {
            "error": "unexpected error; the submission may have gone through. "
            "Call list_verifications to check before submitting again."
        }


async def resubmit_verification(
    *,
    client: HailClient,
    verification_id: str,
    attest_authorized: bool,
    fields: Any = None,
    documents: Any = None,
    files: Any = None,
    address: Any = None,
    quote_id: str | None = None,
) -> dict[str, Any]:
    verification_id_ = _as_uuid(verification_id)
    if verification_id_ is None:
        return _not_uuid("verification_id")
    if attest_authorized is not True:
        return {
            "error": "attest_authorized must be true. Pass true only if the "
            "person or company in these details asked to send them again and "
            "agreed to the changes in this conversation. Nothing was sent."
        }
    if quote_id is not None and _as_uuid(quote_id) is None:
        return {"error": "quote_id must be a UUID. Nothing was sent."}
    shape_error = _verification_shape_error(
        {} if fields is None else fields,
        {} if documents is None else documents,
        [] if files is None else files,
        address,
    )
    if shape_error:
        return {"error": f"{shape_error}. Nothing was sent."}
    details_bytes = _details_bytes(fields or {}, address or {}, documents or {})
    if details_bytes is None or details_bytes > _VERIFICATION_MAX_DETAILS_BYTES:
        return {
            "error": "fields, address and documents must be valid JSON text of "
            f"at most {_VERIFICATION_MAX_DETAILS_BYTES} bytes. Nothing was sent."
        }
    # Without ``documents`` the kept ones name the slots, so a replaced
    # file's slot is checked by the API. With them, each file's slot must be
    # one of them, as in ``submit_verification``.
    slots = (
        {
            entry["slot"]: {}
            for entry in files or []
            if isinstance(entry, dict) and isinstance(entry.get("slot"), str)
        }
        if documents is None
        else {}
    )
    error, decoded, uploads = _check_verification_files(
        files or [], {**slots, **(documents or {})}
    )
    if error:
        return {"error": f"{error}. Nothing was sent."}
    try:
        return _without_carrier(
            await client.resubmit_verification(
                verification_id_,
                fields=fields,
                documents=documents,
                files=decoded,
                address=address,
                quote_id=quote_id,
                uploads=uploads,
            )
        )
    except httpx.TransportError:
        return {
            "error": "request timed out; the resubmit may have gone through. "
            "Call list_verifications to check before sending it again."
        }
    except HailAPIError as exc:
        return _submit_error(exc)
    except Exception:
        # No exception text: it could carry a request or response body.
        return {
            "error": "unexpected error; the resubmit may have gone through. "
            "Call list_verifications to check before sending it again."
        }


async def check_verification(
    *,
    client: HailClient,
    country_code: str,
    number_type: str,
    fields: Any,
    documents: Any,
    files: Any,
    subject_type: str | None,
    address: Any = None,
    quote_id: str | None = None,
    number_id: str | None = None,
    purpose: str = "number",
) -> dict[str, Any]:
    error, form = _verification_request(
        country_code=country_code,
        number_type=number_type,
        fields=fields,
        documents=documents,
        files=files,
        subject_type=subject_type,
        address=address,
        quote_id=quote_id,
        number_id=number_id,
        purpose=purpose,
    )
    if error:
        return {"error": error}
    try:
        return await client.check_verification(**form)
    except httpx.TransportError:
        return {"error": _TRANSPORT_ERROR}
    except HailAPIError as exc:
        if exc.status == 422:
            return {
                "error": "the check found problems; nothing was filed. Fix the "
                "listed problems with the user and check again.",
                "problems": exc.problems or [],
            }
        return _submit_error(exc)
    except Exception:
        # No exception text: it could carry a request or response body.
        return {"error": _UNEXPECTED_ERROR}


async def check_verification_documents(
    *,
    client: HailClient,
    country_code: str,
    number_type: str,
    fields: Any,
    documents: Any,
    files: Any,
    subject_type: str | None,
    address: Any = None,
    quote_id: str | None = None,
) -> dict[str, Any]:
    error, form = _verification_request(
        country_code=country_code,
        number_type=number_type,
        fields=fields,
        documents=documents,
        files=files,
        subject_type=subject_type,
        address=address,
        quote_id=quote_id,
        number_id=None,
        purpose="number",
    )
    if error:
        return {"error": error}
    form.pop("purpose")
    form.pop("number_id")
    try:
        return await client.check_verification_documents(**form)
    except httpx.TransportError:
        return {"error": _TRANSPORT_ERROR}
    except HailAPIError as exc:
        if exc.status == 422:
            return {
                "error": "the form has problems; nothing was read. Fix the "
                "listed problems and try again.",
                "problems": exc.problems or [],
            }
        return _submit_error(exc)
    except Exception:
        # No exception text: it could carry a request or response body.
        return {"error": _UNEXPECTED_ERROR}


async def create_file_upload(
    *, client: HailClient, purpose: str, filename: str, content_type: str
) -> dict[str, Any]:
    if purpose not in _UPLOAD_PURPOSES:
        return {
            "error": "purpose must be "
            + ", ".join(f"'{p}'" for p in _UPLOAD_PURPOSES[:-1])
            + f" or '{_UPLOAD_PURPOSES[-1]}'"
        }
    try:
        link = await client.create_file_upload(
            purpose=purpose, filename=filename, content_type=content_type
        )
    except ValidationError as exc:
        return {"error": _validation_error_message(exc)}
    except HailAPIError as exc:
        return _format_api_error(exc)
    except httpx.TransportError:
        return {"error": _TRANSPORT_ERROR}
    # The link goes into a shell line in single quotes: refuse anything that
    # could end the quote (the API never sends one).
    if not _SHELL_SAFE_URL.fullmatch(link["upload_url"]):
        return {"error": "the API returned an upload link this tool cannot use"}
    return {
        **link,
        "curl": (
            "curl --silent --show-error --globoff --upload-file 'FILE_PATH' "
            f"'{link['upload_url']}'"
        ),
        "next": (
            "Replace FILE_PATH with the file's absolute path (no ~ or $HOME), "
            "keep the single quotes, write any ' in the path as '\\'', and run "
            "the curl line from a shell. "
            "The response has size_bytes and sha256: compare them with the "
            "local file (for example `wc -c` and `shasum -a 256`). The link "
            "works once, until expires_at."
            + (
                " The response's url is the public evidence link: put it in the "
                "*_evidence_url field of the texting registration."
                if purpose == EVIDENCE_PURPOSE
                else ""
            )
        ),
    }


_SHELL_SAFE_URL = re.compile(r"https?://[A-Za-z0-9.:/?=&_%~+-]+")


def _submit_error(exc: HailAPIError) -> dict[str, Any]:
    if exc.status == 422:
        return {
            "error": "the carrier or the API rejected the submission; nothing "
            "was stored. Fix the listed problems with the user and submit again.",
            "problems": exc.problems or [],
        }
    if exc.status == 409 and str(exc.detail).startswith(
        "You already have a verification"
    ):
        # This 409 names the person or company; never echo it.
        return {
            "error": "a live verification with the same subject type and the "
            "same name already exists; nothing new was stored. Call "
            "list_verifications to see it."
        }
    if exc.status in (401, 402, 404, 409, 429, 503):
        # 402 says how much credit is needed; it never holds submitted data.
        return _format_api_error(exc)
    return {"error": f"hail api error {exc.status}"}


async def get_agent(*, client: HailClient, agent_id: str) -> dict[str, Any]:
    agent_id_ = _as_uuid(agent_id)
    if agent_id_ is None:
        return _not_uuid("agent_id")
    try:
        return await client.get_agent(agent_id_)
    except HailAPIError as exc:
        return _format_api_error(exc)


# Fields an explicit JSON null clears. Null on any other AgentUpdate field is
# ignored by the API, so ``update_agent`` refuses them instead of lying.
# Settings update_agent can reset (sent to the API as null).
ClearableAgentField = Literal[
    "recording_enabled",
    "first_message",
    "ai_disclosure_line",
    "tools",
    "max_duration_seconds",
    "handover_max_duration_seconds",
]
_CLEARABLE_AGENT_FIELDS = get_args(ClearableAgentField)


async def update_agent(
    *,
    client: HailClient,
    agent_id: str,
    language: str | None = None,
    voice_id: str | None = None,
    clear_fields: list[str] | None = None,
    **fields: Any,
) -> dict[str, Any]:
    """``fields`` holds only the values to set; ``clear_fields`` the nulls."""
    agent_id_ = _as_uuid(agent_id)
    if agent_id_ is None:
        return _not_uuid("agent_id")
    # Check the arguments before any HTTP call.
    clear = list(clear_fields or [])
    bad = [f for f in clear if f not in _CLEARABLE_AGENT_FIELDS]
    if bad:
        return {
            "error": f"cannot clear {', '.join(bad)}. "
            f"clear_fields accepts only: {', '.join(_CLEARABLE_AGENT_FIELDS)}"
        }
    both = [f for f in clear if f in fields]
    if both:
        return {"error": f"cannot both set and clear: {', '.join(both)}"}
    if (language is None) != (voice_id is None):
        # The API replaces voice_config whole on PATCH: start from the
        # agent's current one so the value not given is kept.
        try:
            current = await client.get_agent(agent_id_)
        except HailAPIError as exc:
            return _format_api_error(exc)
        fields["voice_config"] = {**(current.get("voice_config") or {})}
    elif language is not None:
        fields["voice_config"] = {}
    if "voice_config" in fields:
        for key, value in (("language", language), ("voice_id", voice_id)):
            if value is not None:
                fields["voice_config"][key] = value
    body = dict(fields)
    for name in clear:
        body[name] = None
    try:
        return await client.update_agent(agent_id_, **body)
    except ValidationError as exc:
        return {"error": _validation_error_message(exc)}
    except HailAPIError as exc:
        return _format_api_error(exc)


async def delete_agent(
    *, client: HailClient, agent_id: str, confirm_name: str
) -> dict[str, Any]:
    # Destructive: delete only when the caller typed back the agent's name.
    agent_id_ = _as_uuid(agent_id)
    if agent_id_ is None:
        return _not_uuid("agent_id")
    try:
        agent = await client.get_agent(agent_id_)
        name = str(agent["name"])
        if confirm_name.strip() != name:
            return {
                "error": "confirm_name does not match this agent. "
                f'Pass "{name}" to delete it. Nothing was deleted.'
            }
        await client.delete_agent(agent_id_)
    except HailAPIError as exc:
        return _format_api_error(exc)
    return {"deleted": True, "agent_id": agent_id, "name": name}


# --------------------------------------------------------------------------- #
# Per-tool-call client helper.
#
# Tools are oblivious to the active auth mode: ``_client_for`` either
# builds a fresh ``HailClient`` from the inbound JWT (oauth-rs) or yields
# the shared singleton (static-key). The helper lives here, next to the
# tool wiring, so the registration loop can call it uniformly.
# --------------------------------------------------------------------------- #


def _bearer_from_ctx(ctx: Context) -> str:
    """Extract the Bearer token from the MCPServer request context.

    The lookup is case-insensitive (Starlette's ``Headers`` already lowercases
    on read, but a plain ``dict`` may not). A missing or malformed header
    raises ``RuntimeError``; the tool wrapper catches that and returns a
    structured ``{"error": ...}`` to the agent rather than 500-ing.
    """
    headers = ctx.request_context.request.headers
    raw = headers.get("authorization") or headers.get("Authorization") or ""
    if not raw:
        raise RuntimeError("missing Authorization header on MCP request")
    parts = raw.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise RuntimeError("missing Authorization Bearer token on MCP request")
    return parts[1].strip()


@contextlib.asynccontextmanager
async def _client_for(
    ctx: Context,
    *,
    mode: AuthMode,
    singleton: HailClient | None,
) -> AsyncIterator[HailClient]:
    """Yield a HailClient appropriate to the active auth mode.

    oauth-rs: build a per-call client from the inbound Authorization
    bearer (a JWT minted by hail-website's Better Auth oauth-provider).
    The client closes its httpx pool on context exit.

    static-key: yield the shared singleton without closing it on exit
    (its httpx pool lives for the lifetime of the process).
    """
    if mode is AuthMode.OAUTH_RS:
        bearer = _bearer_from_ctx(ctx)
        client = HailClient(api_key=bearer)
        try:
            async with _actor_for(client, cache_key=bearer):
                yield client
        finally:
            await client.aclose()
        return

    # static-key
    if singleton is None:  # defensive — server.py wires this
        raise RuntimeError("static-key mode requires a singleton HailClient")
    async with _actor_for(singleton):
        yield singleton


class TaggedMCPServer(MCPServer):
    """MCPServer that tags each tool call's trace with the tool name.

    mcp 2 runs the tool call in the POST request's context, under the SDK's
    "tools/call <tool>" span. IdentityMiddleware already bound that POST span
    as the trace root, so both spans get the tags (stdio has no root: only
    the current span is tagged).

    ``subscriptions/listen`` stays off: the tool list never changes while the
    server runs, so 2026-07-28 clients see ``listChanged: false``, the same
    as older clients do.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("subscriptions", False)
        super().__init__(*args, **kwargs)

    async def call_tool(self, name, arguments, context=None):
        tag_context(channel="mcp", tool=name)
        try:
            return await super().call_tool(name, arguments, context)
        except ToolError as exc:
            cause = exc.__cause__
            if isinstance(exc, UnexpectedToolError) or not isinstance(
                cause, ValidationError
            ):
                raise
            # The SDK's text is pydantic's full error, which repeats the
            # rejected values (personal data included) and links to its docs.
            # Name each field and the rule instead; the cause stays attached,
            # so the SDK still logs the field names.
            raise ToolError(
                f"Invalid arguments for {name}: {_argument_errors(cause)}"
            ) from cause


def _argument_errors(exc: ValidationError) -> str:
    """Every argument error as ``field: rule``. Never includes a value."""
    parts = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"])
        parts.append(f"{loc}: {err['msg']}" if loc else str(err["msg"]))
    return "; ".join(parts)


_ACTOR_TTL_SECONDS = 300
# A failed lookup is retried after this, not on every tool call.
_ACTOR_FAILURE_TTL_SECONDS = 30
# Bound optional telemetry lookups, including when the identity API is down.
_ACTOR_LOOKUP_SECONDS = 2
# Set by analytics.instrument_analytics once PostHog instrumentation succeeded.
_analytics_actor = False


def resolve_actor_for_analytics(enabled: bool) -> None:
    """Fill the actor cache for analytics even when Logfire is off."""
    global _analytics_actor
    _analytics_actor = enabled


_ACTOR_CACHE_MAX = 1024
_actor_cache: dict[str, tuple[float, dict[str, Any]]] = {}
# In-flight lookups by cache key, so parallel calls share one whoami.
_actor_lookups: dict[str, asyncio.Future[dict[str, Any]]] = {}


def _actor_cache_key(bearer: str) -> str:
    # Key by token hash so the raw bearer is never held as a dict key.
    return hashlib.sha256(bearer.encode()).hexdigest()


def cached_actor(bearer: str) -> dict[str, Any] | None:
    """The actor ``resolve_actor`` cached for ``bearer``. Never calls the API."""
    hit = _actor_cache.get(_actor_cache_key(bearer))
    return dict(hit[1]) if hit and hit[0] > time.monotonic() else None


async def _lookup_actor(
    client: HailClient, identity: dict[str, Any], tracing: bool
) -> None:
    # Fills ``identity`` in place, so a timeout keeps the ids already read.
    # Resolve the actor only through the API that verifies the bearer.
    actor = await client.whoami()
    identity.update(
        organization_id=actor.get("organization_id"),
        user_id=actor.get("user_id"),
        user_email=actor.get("email"),
        user_name=actor.get("name"),
    )
    org_id = actor.get("organization_id")
    if tracing:
        identity["auth_kind"] = actor.get("auth_kind")
        identity["actor_kind"] = actor.get("auth_kind")
        if org_id:
            identity["organization_name"] = await fetch_organization_name(str(org_id))


async def _resolve_and_cache(
    client: HailClient, cache_key: str | None, tracing: bool
) -> dict[str, Any]:
    identity: dict[str, Any] = {}
    try:
        # One bound for whoami and the org-name lookup together.
        await asyncio.wait_for(
            _lookup_actor(client, identity, tracing), _ACTOR_LOOKUP_SECONDS
        )
        ttl = _ACTOR_TTL_SECONDS
    except Exception:
        # Metadata enrichment never decides whether the tool is authorized.
        # Keep the ids whoami already returned. Retry sooner only when whoami
        # itself failed; a slow org-name lookup alone keeps the full TTL.
        ttl = _ACTOR_TTL_SECONDS if identity else _ACTOR_FAILURE_TTL_SECONDS
    if cache_key:
        if len(_actor_cache) >= _ACTOR_CACHE_MAX:
            _actor_cache.clear()
        _actor_cache[_actor_cache_key(cache_key)] = (
            time.monotonic() + ttl,
            dict(identity),
        )
    return identity


def _forget_lookup(key: str, lookup: asyncio.Future[dict[str, Any]]) -> None:
    if _actor_lookups.get(key) is lookup:
        del _actor_lookups[key]


async def resolve_actor(
    client: HailClient, cache_key: str | None = None
) -> dict[str, Any]:
    """Verified actor for tool traces and analytics. Concurrent lookups for
    one bearer share a single ``whoami``."""
    tracing = telemetry_enabled()
    if not (tracing or (cache_key and _analytics_actor)):
        return {}
    if not cache_key:
        return await _resolve_and_cache(client, None, tracing)
    cached = cached_actor(cache_key)
    if cached is not None:
        return cached
    key = _actor_cache_key(cache_key)
    lookup = _actor_lookups.get(key)
    if lookup is None:
        lookup = asyncio.ensure_future(_resolve_and_cache(client, cache_key, tracing))
        _actor_lookups[key] = lookup
        lookup.add_done_callback(lambda done: _forget_lookup(key, done))
        # The lookup runs on this caller's client, which closes when this
        # caller ends. If this caller is cancelled, the lookup is cancelled
        # too (nothing is cached) and the other callers look up again.
        return dict(await lookup)
    try:
        return dict(await asyncio.shield(lookup))
    except asyncio.CancelledError:
        current = asyncio.current_task()
        if not lookup.cancelled() or (current and current.cancelling()):
            raise
    # The owner was cancelled. Forget its lookup before retrying, so the
    # retry starts a new one instead of awaiting the cancelled one again.
    _forget_lookup(key, lookup)
    return await resolve_actor(client, cache_key)


@contextlib.asynccontextmanager
async def _actor_for(client: HailClient, cache_key: str | None = None):
    with identity_scope(await resolve_actor(client, cache_key)):
        yield


# --------------------------------------------------------------------------- #
# Input descriptions. Each lands on its parameter's JSON schema, where clients
# and directories show it. Shared text lives here; one-off text sits inline.
# --------------------------------------------------------------------------- #


def _d(text: str) -> Any:
    return Field(description=text)


def _rules(model: type[BaseModel], field: str) -> tuple[Any, ...]:
    """The API request model's own constraints on one field (ranges, lengths,
    patterns), so the tool schema states the limits the API applies."""
    return tuple(model.model_fields[field].metadata)


# Page sizes the API accepts: the ``limit`` Query bounds of each list route.
# tests/test_schema_matches_api.py compares them with openapi/openapi.yaml.
_PAGE_SIZE = (
    "How many items one page holds. ``next_cursor`` in the response points to "
    "the next page."
)
_LIMIT_200 = Field(ge=1, le=200, description=_PAGE_SIZE)
_LIMIT_1000 = Field(ge=1, le=1000, description=_PAGE_SIZE)

# Value sets the API states inline, not as a shared type. The same test
# compares each with openapi/openapi.yaml.
EmailDirection = Literal["outbound", "inbound"]
StatsBucket = Literal["hour", "day"]

# A timestamp the API parses as ISO 8601; passed through as text.
_DateTime = Annotated[str, WithJsonSchema({"type": "string", "format": "date-time"})]

# Schema only, for the verification tools: their checks run inside the tool,
# with messages that never echo a value (see ``_verification_request``).
_UUID_OR_NULL = WithJsonSchema(
    {"anyOf": [{"type": "string", "format": "uuid"}, {"type": "null"}]}
)
# Schema-only enum: the tool's own check answers a bad value with its
# "ask the user" message instead of a generic validation error.
_SUBJECT_TYPE_ENUM = {"type": "string", "enum": list(_SUBJECT_TYPES)}
_SUBJECT_TYPE_SCHEMA = WithJsonSchema(_SUBJECT_TYPE_ENUM)
_SUBJECT_TYPE_OR_NULL = WithJsonSchema(
    {"anyOf": [_SUBJECT_TYPE_ENUM, {"type": "null"}]}
)
_NUMBER_TYPE_SCHEMA = WithJsonSchema(
    {"type": "string", "enum": list(get_args(NumberType))}
)
_PURPOSE_SCHEMA = WithJsonSchema(
    {"type": "string", "enum": list(_VERIFICATION_PURPOSES)}
)
# Schema only: the API strips ``from_name`` before its length check, so a
# validating max_length here would reject a padded name the API accepts.
_FROM_NAME_SCHEMA = WithJsonSchema(
    {"anyOf": EmailCreate.model_json_schema()["properties"]["from_name"]["anyOf"]}
)
_ADDRESS_SCHEMA = {k: v for k, v in Address.model_json_schema().items() if k != "title"}
_ADDRESS_OR_NULL = Annotated[
    Any, WithJsonSchema({"anyOf": [_ADDRESS_SCHEMA, {"type": "null"}]})
]
# The same parts, each optional: a document check takes a partial address.
_PARTIAL_ADDRESS_OR_NULL = Annotated[
    Any,
    WithJsonSchema(
        {
            "anyOf": [
                {k: v for k, v in _ADDRESS_SCHEMA.items() if k != "required"},
                {"type": "null"},
            ]
        }
    ),
]
_SLOT_FIELD = {
    "type": "string",
    "pattern": f"^{_SLOT_NAME.pattern}$",
    "description": "The document slot this file fills: a key in ``documents``.",
}
_VERIFICATION_FILES_LIST = Annotated[
    Any,
    WithJsonSchema(
        {
            "type": "array",
            "maxItems": _VERIFICATION_MAX_FILES,
            "items": {
                "anyOf": [
                    {
                        "type": "object",
                        "properties": {
                            "slot": _SLOT_FIELD,
                            "upload_id": {
                                "type": "string",
                                "format": "uuid",
                                "description": (
                                    "The ``id`` from ``create_file_upload`` with "
                                    "purpose ``verification_document``."
                                ),
                            },
                        },
                        "required": ["slot", "upload_id"],
                        "additionalProperties": False,
                    },
                    {
                        "type": "object",
                        "properties": {
                            "slot": _SLOT_FIELD,
                            "content_type": {
                                "type": "string",
                                "enum": list(VERIFICATION_FILE_TYPES),
                                "description": "The file's format.",
                            },
                            "content_base64": {
                                "type": "string",
                                "contentEncoding": "base64",
                                "description": "The whole file, base64-encoded.",
                            },
                        },
                        "required": ["slot", "content_type", "content_base64"],
                    },
                ]
            },
        }
    ),
]


_CURSOR = _d(
    "Pagination cursor: the ``next_cursor`` from the previous page. "
    "Omitted: the first page."
)
_IDEMPOTENCY_KEY = _d(
    "Retry key. The ``idempotency_key`` from an earlier response refers to "
    "that original request instead of starting a new one. Omitted: a new key "
    "is generated and returned."
)
_RECIPIENT_CONSENT = _d(
    "Attestation that the recipient lawfully agreed to be contacted, as the "
    "user confirmed it. Hail does not check it; only ``true`` is accepted."
)
_CONSENT_SOURCE = _d(
    "Where or how consent was obtained, such as ``signup form``. "
    "Required when ``message_type`` is ``marketing``."
)
_CONSENT_OBTAINED_AT = _d(
    "When consent was obtained, as an ISO 8601 timestamp, if known."
)
_MESSAGE_TYPE = _d(
    "``informational`` for transactional or service messages, or ``marketing``."
)
_PHONE_TO = _d("Recipient phone number in E.164 format, such as ``+14155551234``.")
_FILTER_TO = _d("Only items sent to this E.164 phone number.")
_CALL_ID = _d("Call ID, from ``place_call`` or ``list_calls``.")
_SMS_ID = _d("Text message ID, from ``send_sms`` or ``list_sms``.")
_EMAIL_ID = _d("Email ID, from ``send_email`` or ``list_emails``.")
_AGENT_ID = _d("Saved agent ID, from ``list_agents`` or ``create_agent``.")
_NUMBER_ID = _d(
    "Workspace phone number ID, from ``list_numbers`` or ``acquire_number``."
)
_VERIFICATION_ID = _d(
    "Registration ID, from ``list_verifications`` or ``submit_verification``."
)
_COUNTRY_CODE = _d("ISO 3166-1 alpha-2 country code, such as ``US``.")
_NUMBER_TYPE = _d(
    "The kind of number this registration covers. Copy it from the offer in "
    "``quote_numbers`` or the number in ``list_numbers``."
)
_QUOTE_ID = _d("``quote_id`` of one offer from ``quote_numbers``.")
_SUBJECT_TYPE = _d(
    "Who the registration is for: the user themselves (``person``) or their "
    "company (``business``)."
)
_VERIFICATION_PURPOSE = _d(
    "What the registration is for: ``number`` registers the applicant so a "
    "number can be bought; ``sms`` registers US texting (local 10DLC or "
    "toll-free)."
)
_VERIFICATION_NUMBER_ID = _d(
    "The owned number this registration is for: its ID from ``list_numbers``, "
    "the same one used for the requirements. Give it for toll-free texting "
    "and when the form asks for the sending number; otherwise leave it out."
)
_VERIFICATION_QUOTE_ID = _d(
    "The number offer this registration lets the user buy: the offer's "
    "``quote_id`` from ``quote_numbers``, the same one used for the "
    "requirements. Give it when ``purpose`` is ``number``; leave it out for "
    "``sms``."
)
_SUBJECT_TYPE_REQUIRED = _d(
    "Who the registration is for, matching the requirements: the user "
    "(``person``) or their company (``business``). It has no default; "
    "without it, nothing is sent."
)
_VERIFICATION_FIELDS = _d(
    "Applicant answers keyed by the field names in "
    "``get_verification_requirements``. Choice fields take comma-separated option keys."
)
_VERIFICATION_DOCUMENTS = _d(
    "Map from each needed document slot to ``{option, fields}``, with the "
    "option chosen from that slot in the requirements."
)
_DOCUMENT_CHECK_DOCUMENTS = _d(
    "Map from each document slot to ``{option, fields, covers}``. ``option`` "
    "may be empty while the type is not chosen. ``covers`` lists the other "
    "slots the same file stands for: the file is sent once, for this slot, "
    "and one read fills all of them."
)
_VERIFICATION_FILES = _d(
    "The document files, one per slot named in ``documents``. Use an upload: "
    "the ``id`` from ``create_file_upload`` with purpose "
    "``verification_document``, within an hour of uploading. A very small "
    "file can go inline as base64 instead. Each file is at most 10 MiB."
)
_VERIFICATION_ADDRESS = _d(
    "The applicant's address. Give it when the requirements include one; "
    "otherwise leave it out."
)
_ATTEST_AUTHORIZED = _d(
    "Attestation that the named person or business authorized this filing and "
    "supplied its details and files. Only ``true`` sends anything."
)
_AGENT_NAME = _d("Agent display name.")
_AGENT_PROMPT = _d(
    "Instructions that define the agent's behavior on calls, texts and email replies."
)
_AGENT_FIRST_MESSAGE = _d(
    "Greeting spoken after the AI disclosure. Without one, an inbound agent "
    "waits for the caller."
)
_AGENT_AI_DISCLOSURE = _d("Speak the AI disclosure line first on every call.")
_AGENT_RECORDING = _d(
    "Recording for this agent's calls. ``true`` or ``false`` overrides the "
    "workspace setting."
)
_AGENT_DISCLOSURE_LINE = _d(
    "Custom AI disclosure line replacing the workspace line. ``{org}`` expands "
    "to the organization name."
)
_AGENT_LANGUAGE = _d(
    "Language the agent speaks, as an ISO 639-1 code such as ``es``. "
    "Omitted: English."
)
_AGENT_VOICE_ID = _d(
    "Text-to-speech voice ID from the voice provider that serves the call, "
    "such as an ElevenLabs voice ID. Omitted: the workspace's default voice."
)
_AGENT_TOOLS = _d(
    "Names of the tools the agent may use during a call or reply. "
    "Omitted: every tool the workspace's channels support. An empty list: none."
)
_AGENT_MAX_DURATION = _d(
    "Longest a call with this agent may last, in seconds, counted from "
    "pickup. At the limit the agent says goodbye and hangs up. "
    "Omitted: the workspace's call limit."
)
_AGENT_HANDOVER_MAX = _d(
    "Longest a call may last after a handover contact answers, in seconds, "
    "counted from that answer. Omitted: 1800 (30 minutes)."
)
_AGENT_VOICE_ENABLED = _d(
    "Whether this agent answers calls on numbers whose calls route to it."
)
_AGENT_SMS_ENABLED = _d(
    "Whether this agent answers texts on numbers whose texts route to it."
)
_AGENT_EMAIL_ENABLED = _d(
    "Whether this agent replies to mail on addresses routed to it."
)
_AGENT_DURATION_RULES = _rules(AgentCreate, "max_duration_seconds")
_AGENT_HANDOVER_RULES = _rules(AgentCreate, "handover_max_duration_seconds")
_AGENT_UPDATE_DURATION_RULES = _rules(AgentUpdate, "max_duration_seconds")
_AGENT_UPDATE_HANDOVER_RULES = _rules(AgentUpdate, "handover_max_duration_seconds")
_AGENT_HANDOVER_CONTACTS = _d(
    "Up to ten ``{contact_id, note}`` entries for people a call can be handed "
    "over to. ``contact_id`` is an ID from ``list_contacts``, ``lookup_contact`` "
    "or ``create_contact`` (a UUID or ``member:<user_id>``); the contact needs a "
    "phone number. ``note`` (1 to 200 characters) says when the agent hands over "
    "to that person, such as ``billing questions``."
)

_AGENT_HTTP_TOOLS = _d(
    "Up to ten customer APIs the agent may call during calls, texts and email. "
    "Each entry: ``name`` (letters, digits, _), ``description`` (read by the "
    "model), ``url`` (public https), optional ``parameters`` (JSON Schema, "
    "type object), ``headers`` (static, stored encrypted, returned as names "
    "only), ``timeout_seconds`` (1 to 10, default 5) and ``wait_message`` "
    "(said on a call while waiting). The list replaces the saved one; an "
    "empty list removes it. On update, an entry without ``headers`` keeps "
    "the saved headers of the same name while the url keeps its host; a url "
    "on a new host needs ``headers`` again. The first save returns "
    "``http_tool_secret`` once; requests carry ``X-Hail-Signature`` signed "
    "with it."
)

# --------------------------------------------------------------------------- #
# MCPServer registration.
#
# We register thin wrappers that delegate to the module-level domain
# functions — MCPServer derives the JSON schema from the *registered*
# function's signature, and we want the agent-facing signature to omit
# the ``client`` argument (it's resolved per-call by ``_client_for``).
# Each closure takes a ``ctx: Context`` first param (MCPServer auto-injects
# it) and wraps the delegation in ``async with _client_for(ctx, ...)``.
# A ``RuntimeError`` from ``_client_for`` (missing/malformed bearer in
# oauth-rs mode) is caught and surfaced as a structured tool error.
# --------------------------------------------------------------------------- #


def register_tools(
    mcp_app: MCPServer,
    *,
    mode: AuthMode,
    singleton: HailClient | None,
) -> None:
    """Register the Hail tools on an MCPServer app.

    Tools accept an MCPServer ``Context`` parameter (auto-injected). The
    ``_client_for`` helper picks the right HailClient for the active mode
    — per-tool-call in oauth-rs, shared singleton in static-key.
    """

    @mcp_app.tool(
        name="place_call",
        title="Place a phone call",
        annotations=ToolAnnotations(
            title="Place a phone call",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        ),
    )
    async def place_call_tool(
        ctx: Context,
        to: Annotated[str, _PHONE_TO],
        recipient_consent: Annotated[StrictBool, _RECIPIENT_CONSENT],
        system_prompt: Annotated[
            str | None,
            _d("Task instructions, added to the call agent's instructions."),
        ] = None,
        llm: Annotated[
            dict[str, Any] | None,
            _d(
                "Own model endpoint compatible with OpenAI's chat completions API: "
                "``{base_url, api_key, model}``. Omitted: Hail's bundled model."
            ),
        ] = None,
        from_: Annotated[
            str | None,
            _d(
                "Caller ID: an E.164 workspace number with voice, from ``list_numbers``. "
                "Omitted: an active workspace number, else one from the shared pool."
            ),
        ] = None,
        first_message: Annotated[
            str | None,
            _d(
                "Line spoken on pickup. Without it, the agent reacts to the "
                "answer or silence."
            ),
        ] = None,
        language: Annotated[
            str | None,
            _d(
                "Spoken language as a supported lowercase ISO 639-1 code, such as "
                "``es``. Omitted: English. An unsupported code returns 422."
            ),
        ] = None,
        ai_disclosure: Annotated[
            bool, _d("Speak an AI notice at the start of the call. ``false`` skips it.")
        ] = True,
        recording_enabled: Annotated[
            bool | None,
            _d("Recording for this call, overriding the agent and workspace settings."),
        ] = None,
        metadata: Annotated[
            dict[str, Any] | None,
            _d(
                "Free-form JSON object stored on the call record. Keys starting "
                "with ``recording_`` are reserved."
            ),
        ] = None,
        tools: Annotated[
            list[str] | None,
            _d(
                "Names of the agent tools allowed on this call. An empty list "
                "disables them. Omitted: every tool the workspace's channels support."
            ),
        ] = None,
        idempotency_key: Annotated[str | None, _IDEMPOTENCY_KEY] = None,
        consent_source: Annotated[str | None, _CONSENT_SOURCE] = None,
        consent_obtained_at: Annotated[str | None, _CONSENT_OBTAINED_AT] = None,
        message_type: Annotated[str, _MESSAGE_TYPE] = "informational",
        agent_id: Annotated[
            str | None,
            _d(
                "Saved agent whose settings the call uses, from ``list_agents`` or "
                "``create_agent``. Explicit arguments override its settings."
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Start an outbound AI phone call.

        At least one of ``system_prompt``, ``llm`` or ``agent_id`` is required.
        US artificial-voice calls (47 CFR 64.1200(b)(1)) require the AI disclosure.
        Hail does not verify the lawful basis or the recipient's consent.
        A 4xx response returns no ``idempotency_key``.

        Returns the call ID, status, phone numbers and ``idempotency_key``, or an error.
        """
        try:
            async with _client_for(ctx, mode=mode, singleton=singleton) as client:
                return await place_call(
                    client=client,
                    to=to,
                    recipient_consent=recipient_consent,
                    system_prompt=system_prompt,
                    llm=llm,
                    from_=from_,
                    first_message=first_message,
                    language=language,
                    ai_disclosure=ai_disclosure,
                    recording_enabled=recording_enabled,
                    metadata=metadata,
                    tools=tools,
                    idempotency_key=idempotency_key,
                    consent_source=consent_source,
                    consent_obtained_at=consent_obtained_at,
                    message_type=message_type,
                    agent_id=agent_id,
                )
        except RuntimeError as exc:
            return {"error": str(exc)}

    @mcp_app.tool(
        name="send_email",
        title="Send an email",
        annotations=ToolAnnotations(
            title="Send an email",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        ),
    )
    async def send_email_tool(
        ctx: Context,
        to: Annotated[
            list[str],
            *_rules(EmailCreate, "to"),
            _d("Email addresses of the people the message is for."),
        ],
        subject: Annotated[
            str,
            *_rules(EmailCreate, "subject"),
            _d("Subject line the recipients see in their inbox."),
        ],
        recipient_consent: Annotated[StrictBool, _RECIPIENT_CONSENT],
        body_text: Annotated[
            str | None,
            _d(
                "The message as plain text, shown by mail apps that cannot "
                "display HTML. Give this, ``body_html``, or both."
            ),
        ] = None,
        body_html: Annotated[
            str | None,
            _d(
                "The message as HTML. Open and click tracking works only with "
                "an HTML body. Give this, ``body_text``, or both."
            ),
        ] = None,
        from_: Annotated[
            str | None,
            _d(
                "Sender address. It must match a verified identity from "
                "``list_email_domains``; on a custom domain any local part works. "
                "Omitted: the workspace default sender."
            ),
        ] = None,
        from_name: Annotated[
            str | None,
            _FROM_NAME_SCHEMA,
            _d(
                "Display name for the From header, such as ``Acme Billing``. "
                "Control characters are rejected."
            ),
        ] = None,
        cc: Annotated[
            list[str] | None,
            _d("Email addresses to copy; every recipient sees them."),
        ] = None,
        bcc: Annotated[
            list[str] | None,
            _d(
                "Email addresses to copy in secret: they get the message, and "
                "no other recipient sees their addresses."
            ),
        ] = None,
        reply_to: Annotated[
            str | None,
            _d(
                "Address that receives replies instead of the sender. "
                "Omitted: replies go to the sender."
            ),
        ] = None,
        metadata: Annotated[
            dict[str, Any] | None,
            _d("Free-form JSON object stored on the email record."),
        ] = None,
        idempotency_key: Annotated[str | None, _IDEMPOTENCY_KEY] = None,
        consent_source: Annotated[str | None, _CONSENT_SOURCE] = None,
        consent_obtained_at: Annotated[str | None, _CONSENT_OBTAINED_AT] = None,
        message_type: Annotated[str, _MESSAGE_TYPE] = "informational",
        attachment_ids: Annotated[
            list[str] | None,
            _d(
                "Attachment IDs: the ``id`` from ``upload_email_attachment``, or from "
                "``create_file_upload`` with purpose ``email_attachment``."
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Send an email from a verified workspace sender.

        Plain-text emails still have sending, delivery and bounce events.

        Without ``from_``, one verified custom domain takes priority over the Hail mail address.
        Several verified custom domains require an explicit ``from_`` address.
        A custom domain blocked from sending (``sends_enabled`` false, PATCH ``/v1/email-domains/{id}``) is skipped for picks and refuses an explicit ``from_``.
        When configured, a workspace without a sender receives a Hail mail address automatically.

        Hail does not verify consent.

        Returns the email ID, status, sender, recipients and ``idempotency_key``, or an error.
        """
        try:
            async with _client_for(ctx, mode=mode, singleton=singleton) as client:
                return await send_email(
                    client=client,
                    to=to,
                    subject=subject,
                    recipient_consent=recipient_consent,
                    body_text=body_text,
                    body_html=body_html,
                    from_=from_,
                    from_name=from_name,
                    cc=cc,
                    bcc=bcc,
                    reply_to=reply_to,
                    metadata=metadata,
                    idempotency_key=idempotency_key,
                    consent_source=consent_source,
                    consent_obtained_at=consent_obtained_at,
                    message_type=message_type,
                    attachment_ids=attachment_ids,
                )
        except RuntimeError as exc:
            return {"error": str(exc)}

    @mcp_app.tool(
        name="upload_email_attachment",
        title="Upload an email attachment",
        annotations=ToolAnnotations(
            title="Upload an email attachment",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    async def upload_email_attachment_tool(
        ctx: Context,
        content_base64: Annotated[str, _d("The file's bytes, base64-encoded.")],
        filename: Annotated[
            str, _d("File name shown to recipients, such as ``invoice.pdf``.")
        ],
        content_type: Annotated[
            str, _d("The file's MIME type, such as ``application/pdf``.")
        ],
    ) -> dict[str, Any]:
        """Upload a small attachment for outbound email, as base64 text.

        Meant for small files (under about 50 KB): every base64 character goes
        through the tool call itself. Larger files go through ``create_file_upload``
        with ``purpose="email_attachment"``: its ``curl`` line sends the bytes, and its
        ``id`` works in ``send_email``'s ``attachment_ids``.
        Returns ``id``, ``filename``, ``content_type`` and ``size_bytes``.
        The ID is accepted in an email's ``attachment_ids`` and is reusable across sends.
        Unused uploads expire after 24 hours.
        The combined message and attachments must fit the 25 MB sending limit."""
        try:
            async with _client_for(ctx, mode=mode, singleton=singleton) as client:
                return await upload_email_attachment(
                    client=client,
                    content_base64=content_base64,
                    filename=filename,
                    content_type=content_type,
                )
        except RuntimeError as exc:
            return {"error": str(exc)}

    @mcp_app.tool(
        name="create_file_upload",
        title="Create a file upload link",
        annotations=ToolAnnotations(
            title="Create a file upload link",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    async def create_file_upload_tool(
        ctx: Context,
        purpose: Annotated[
            UploadPurpose,
            _d(
                "What the file is for. ``email_attachment``: its ``id`` goes in "
                "``send_email``'s ``attachment_ids``. ``verification_document``: "
                "its ``id`` goes in the ``files`` of ``check_verification`` and "
                "``submit_verification`` as ``{slot, upload_id}``. "
                "``sms_evidence``: a US texting consent screenshot; the upload "
                "response's ``url`` goes in an ``*_evidence_url`` field. Anyone "
                "with that link can open the file, and a workspace keeps at "
                "most 100. Deleting one needs the console or "
                "``DELETE /v1/verifications/sms/evidence/{organization_id}/{token}``."
            ),
        ],
        filename: Annotated[str, _d("File name, such as ``passport.pdf``.")],
        content_type: Annotated[
            str, _d("The file's MIME type, such as ``application/pdf``.")
        ],
    ) -> dict[str, Any]:
        """Create a one-time link that uploads one file from a shell with curl.

        The bytes go from the local disk to Hail directly, not through the tool call,
        so any file that is not tiny goes this way.
        An email attachment can be up to 25 MB. A verification document must be
        JPG, PNG or PDF, up to 10 MB, and is usable for 1 hour after the upload.
        SMS evidence must be a JPG, PNG or PDF of the given ``content_type``, up to 10 MB.
        Its upload response has a public ``url`` for an ``*_evidence_url`` field.
        Carrier reviewers open it without signing in, so remove personal information first.
        Returns ``id``, ``upload_url``, ``expires_at`` (15 minutes), ``max_bytes``
        and a ready ``curl`` line with a ``'FILE_PATH'`` placeholder for the
        file's absolute path.
        The link needs no key and works once. The upload response holds
        ``size_bytes`` and ``sha256`` to compare with the local file."""
        try:
            async with _client_for(ctx, mode=mode, singleton=singleton) as client:
                return await create_file_upload(
                    client=client,
                    purpose=purpose,
                    filename=filename,
                    content_type=content_type,
                )
        except RuntimeError as exc:
            return {"error": str(exc)}

    @mcp_app.tool(
        name="get_call",
        title="Get a call",
        annotations=ToolAnnotations(
            title="Get a call",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def get_call_tool(
        ctx: Context, call_id: Annotated[str, _CALL_ID]
    ) -> dict[str, Any]:
        """Read a call's current status, timing and transcript by ID.

        Returns the call record with its transcript (spoken turns, in the
        order they were said), or an error when the call is unavailable."""
        try:
            async with _client_for(ctx, mode=mode, singleton=singleton) as client:
                return await get_call(client=client, call_id=call_id)
        except RuntimeError as exc:
            return {"error": str(exc)}

    @mcp_app.tool(
        name="list_calls",
        title="List calls",
        annotations=ToolAnnotations(
            title="List calls",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def list_calls_tool(
        ctx: Context,
        cursor: Annotated[str | None, _CURSOR] = None,
        limit: Annotated[int, _LIMIT_200] = 50,
        status: Annotated[
            CallStatus | None,
            _d(
                "Show only calls in this state now, for example ``in_progress`` "
                "for live calls or ``no_answer`` for missed ones. Omitted: all calls."
            ),
        ] = None,
        to: Annotated[str | None, _FILTER_TO] = None,
    ) -> dict[str, Any]:
        """List recent workspace calls, newest first.

        Returns ``items`` and ``next_cursor``."""
        try:
            async with _client_for(ctx, mode=mode, singleton=singleton) as client:
                return await list_calls(
                    client=client, cursor=cursor, limit=limit, status=status, to=to
                )
        except RuntimeError as exc:
            return {"error": str(exc)}

    @mcp_app.tool(
        name="send_sms",
        title="Send a text message",
        annotations=ToolAnnotations(
            title="Send a text message",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        ),
    )
    async def send_sms_tool(
        ctx: Context,
        to: Annotated[str, _PHONE_TO],
        body: Annotated[
            str, _d("Message text. Long bodies are split into carrier segments.")
        ],
        recipient_consent: Annotated[StrictBool, _RECIPIENT_CONSENT],
        from_: Annotated[
            str | None,
            _d(
                "Sender: an active E.164 workspace number with SMS, from "
                "``list_numbers``. Omitted: picked by destination (see the tool description)."
            ),
        ] = None,
        metadata: Annotated[
            dict[str, Any] | None,
            _d("Free-form JSON object stored on the message record."),
        ] = None,
        idempotency_key: Annotated[str | None, _IDEMPOTENCY_KEY] = None,
        consent_source: Annotated[str | None, _CONSENT_SOURCE] = None,
        consent_obtained_at: Annotated[str | None, _CONSENT_OBTAINED_AT] = None,
        message_type: Annotated[str, _MESSAGE_TYPE] = "informational",
    ) -> dict[str, Any]:
        """Send an outbound text message.

        Without ``from_``, supported destinations use the workspace's alphanumeric sender ID.
        Other destinations use an eligible workspace SMS-capable number.
        A missing required number returns 422. SMS does not use the shared voice-call pool.
        US long-code texting to US recipients requires ready A2P 10DLC registration and an attached number.
        Trial texts (only to the workspace's own verified phone, set up in the console: Overview > Test a text): without from_, Hail uses one when the workspace's own number cannot text that US phone; set from_ to the trial number to use one directly. Each starts with "[Hail trial]".

        Hail does not verify consent.

        Returns the SMS ID, status, sender, destination, segment count and retry key, or an error.
        """
        try:
            async with _client_for(ctx, mode=mode, singleton=singleton) as client:
                return await send_sms(
                    client=client,
                    to=to,
                    body=body,
                    recipient_consent=recipient_consent,
                    from_=from_,
                    metadata=metadata,
                    idempotency_key=idempotency_key,
                    consent_source=consent_source,
                    consent_obtained_at=consent_obtained_at,
                    message_type=message_type,
                )
        except RuntimeError as exc:
            return {"error": str(exc)}

    @mcp_app.tool(
        name="get_sms",
        title="Get a text message",
        annotations=ToolAnnotations(
            title="Get a text message",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def get_sms_tool(
        ctx: Context, sms_id: Annotated[str, _SMS_ID]
    ) -> dict[str, Any]:
        """Read a text message's current state by ID.

        Returns the message record, including delivery status, or an error."""
        try:
            async with _client_for(ctx, mode=mode, singleton=singleton) as client:
                return await get_sms(client=client, sms_id=sms_id)
        except RuntimeError as exc:
            return {"error": str(exc)}

    @mcp_app.tool(
        name="list_sms",
        title="List text messages",
        annotations=ToolAnnotations(
            title="List text messages",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def list_sms_tool(
        ctx: Context,
        cursor: Annotated[str | None, _CURSOR] = None,
        limit: Annotated[int, _LIMIT_200] = 50,
        status: Annotated[
            SmsStatus | None,
            _d(
                "Only messages currently in this status; ``received`` means "
                "inbound. Omitted: every status."
            ),
        ] = None,
        to: Annotated[str | None, _FILTER_TO] = None,
    ) -> dict[str, Any]:
        """List recent workspace text messages.

        Returns ``items`` and ``next_cursor``."""
        try:
            async with _client_for(ctx, mode=mode, singleton=singleton) as client:
                return await list_sms(
                    client=client, cursor=cursor, limit=limit, status=status, to=to
                )
        except RuntimeError as exc:
            return {"error": str(exc)}

    @mcp_app.tool(
        name="get_email",
        title="Get an email",
        annotations=ToolAnnotations(
            title="Get an email",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def get_email_tool(
        ctx: Context, email_id: Annotated[str, _EMAIL_ID]
    ) -> dict[str, Any]:
        """Read a complete workspace email by ID.

        Returns ``body_text``, ``body_html`` and other email fields.
        Inbound records include reply headers and spam, virus, SPF, DKIM and DMARC verdicts.
        Attachments include IDs and file metadata.
        An unknown or inaccessible email returns an error."""
        try:
            async with _client_for(ctx, mode=mode, singleton=singleton) as client:
                return await get_email(client=client, email_id=email_id)
        except RuntimeError as exc:
            return {"error": str(exc)}

    @mcp_app.tool(
        name="list_emails",
        title="List emails",
        annotations=ToolAnnotations(
            title="List emails",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def list_emails_tool(
        ctx: Context,
        cursor: Annotated[str | None, _CURSOR] = None,
        limit: Annotated[int, _LIMIT_200] = 50,
        status: Annotated[
            EmailStatus | None,
            _d(
                "Only emails currently in this status; ``received`` means "
                "inbound. Omitted: every status."
            ),
        ] = None,
        direction: Annotated[
            EmailDirection | None,
            _d(
                "Only mail received by the workspace (``inbound``) or only mail "
                "it sent (``outbound``). Omitted: both."
            ),
        ] = None,
    ) -> dict[str, Any]:
        """List workspace email summaries, newest first.

        Summaries contain IDs and metadata, without message bodies.
        Returns ``items`` and ``next_cursor``, or an error."""
        try:
            async with _client_for(ctx, mode=mode, singleton=singleton) as client:
                return await list_emails(
                    client=client,
                    cursor=cursor,
                    limit=limit,
                    status=status,
                    direction=direction,
                )
        except RuntimeError as exc:
            return {"error": str(exc)}

    @mcp_app.tool(
        name="get_email_raw",
        title="Get raw email",
        annotations=ToolAnnotations(
            title="Get raw email",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def get_email_raw_tool(
        ctx: Context,
        email_id: Annotated[
            str,
            _d("Inbound email ID, from ``list_emails`` with direction ``inbound``."),
        ],
    ) -> dict[str, Any]:
        """Return a temporary download URL for an inbound email's original MIME source.

        The URL expires after approximately five minutes and requires no authorization header.
        Raw source is available only for inbound emails.
        Returns ``url``, or an error for an unavailable email."""
        try:
            async with _client_for(ctx, mode=mode, singleton=singleton) as client:
                return await get_email_raw(client=client, email_id=email_id)
        except RuntimeError as exc:
            return {"error": str(exc)}

    @mcp_app.tool(
        name="get_email_attachment",
        title="Get an email attachment",
        annotations=ToolAnnotations(
            title="Get an email attachment",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def get_email_attachment_tool(
        ctx: Context,
        email_id: Annotated[
            str,
            _d("Inbound email ID, from ``list_emails`` with direction ``inbound``."),
        ],
        attachment_id: Annotated[
            str, _d("Attachment ID, from the ``attachments`` of ``get_email``.")
        ],
    ) -> dict[str, Any]:
        """Return a temporary download URL for an inbound email attachment.

        The URL expires after approximately five minutes and requires no authorization header.
        Returns ``url``, or an error for an unavailable attachment."""
        try:
            async with _client_for(ctx, mode=mode, singleton=singleton) as client:
                return await get_email_attachment(
                    client=client, email_id=email_id, attachment_id=attachment_id
                )
        except RuntimeError as exc:
            return {"error": str(exc)}

    @mcp_app.tool(
        name="get_email_events",
        title="Get email events",
        annotations=ToolAnnotations(
            title="Get email events",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def get_email_events_tool(
        ctx: Context,
        email_id: Annotated[str, _EMAIL_ID],
        cursor: Annotated[str | None, _CURSOR] = None,
        limit: Annotated[
            int | None,
            Field(
                ge=1,
                le=1000,
                description=f"{_PAGE_SIZE} Omitted: the API returns 100.",
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Read one email's delivery and engagement timeline.

        Events include sent, delivered, delivery_delayed, bounced, complained,
        rejected, opened and clicked, with ``payload`` and ``occurred_at``.
        Open and click events require an HTML email body.
        Returns ``items`` and ``next_cursor``, or an error."""
        try:
            async with _client_for(ctx, mode=mode, singleton=singleton) as client:
                return await get_email_events(
                    client=client, email_id=email_id, cursor=cursor, limit=limit
                )
        except RuntimeError as exc:
            return {"error": str(exc)}

    @mcp_app.tool(
        name="get_email_stats",
        title="Get email stats",
        annotations=ToolAnnotations(
            title="Get email stats",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def get_email_stats_tool(
        ctx: Context,
        from_: Annotated[
            _DateTime | None,
            _d(
                "Start of the counting window, with a time zone. "
                "Omitted: seven days before ``to``."
            ),
        ] = None,
        to: Annotated[
            _DateTime | None,
            _d("End of the counting window, with a time zone. Omitted: now."),
        ] = None,
        bucket: Annotated[
            StatsBucket,
            _d(
                "Time span each point in ``series`` counts. Hourly points suit "
                "short ranges; daily points suit weeks."
            ),
        ] = "day",
    ) -> dict[str, Any]:
        """Read workspace email delivery counts, rates and time series.

        Without ``from_`` and ``to``, the range is the last seven days.
        Hourly ranges are limited to eight days; all ranges to 92 days.
        Returns ``from``, ``to``, ``bucket``, ``totals``, ``rates`` and ``series``.
        Rates are null when the sent count is zero.
        Open and click counts cover HTML emails only; plain-text emails lower these rates.
        An upstream failure returns an error."""
        try:
            async with _client_for(ctx, mode=mode, singleton=singleton) as client:
                return await get_email_stats(
                    client=client, from_=from_, to=to, bucket=bucket
                )
        except RuntimeError as exc:
            return {"error": str(exc)}

    @mcp_app.tool(
        name="get_events",
        title="Get events",
        annotations=ToolAnnotations(
            title="Get events",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def get_events_tool(
        ctx: Context,
        id: Annotated[
            str | None,
            _d(
                "One resource: ``call:<uuid>``, ``email:<uuid>`` or ``sms:<uuid>``, "
                "with the ID from ``place_call``, ``send_email``, ``send_sms`` or "
                "their list tools. Omitted: events for the whole workspace."
            ),
        ] = None,
        kind: Annotated[
            str | None,
            _d(
                "Only events of this type, such as state_change, agent_turn, "
                "user_turn, tool_call or error."
            ),
        ] = None,
        cursor: Annotated[str | None, _CURSOR] = None,
        limit: Annotated[int, _LIMIT_1000] = 100,
    ) -> dict[str, Any]:
        """Read a page of workspace events or events for one resource.

        A call-specific response includes the current ``call_status``.
        Terminal call states are completed, failed, busy, no_answer and canceled.
        The response is a snapshot, not a streaming subscription.
        Returns ``items``, ``next_cursor`` and optional ``call_status``, or an error."""
        try:
            async with _client_for(ctx, mode=mode, singleton=singleton) as client:
                return await get_events(
                    client=client, id=id, kind=kind, cursor=cursor, limit=limit
                )
        except RuntimeError as exc:
            return {"error": str(exc)}

    @mcp_app.tool(
        name="list_agents",
        title="List agents",
        annotations=ToolAnnotations(
            title="List agents",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def list_agents_tool(ctx: Context) -> dict[str, Any]:
        """List the workspace's saved agents and their settings.

        Agent records contain IDs, names, instructions, greetings, disclosure settings,
        voice settings, enabled channels, status, duration limits and handover contacts.
        Returns ``items`` or an error."""
        try:
            async with _client_for(ctx, mode=mode, singleton=singleton) as client:
                return await list_agents(client=client)
        except HailAPIError as exc:
            return _format_api_error(exc)

    @mcp_app.tool(
        name="create_agent",
        title="Create an agent",
        annotations=ToolAnnotations(
            title="Create an agent",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    async def create_agent_tool(
        ctx: Context,
        name: Annotated[str, _AGENT_NAME],
        system_prompt: Annotated[str, _AGENT_PROMPT],
        first_message: Annotated[str | None, _AGENT_FIRST_MESSAGE] = None,
        ai_disclosure: Annotated[bool, _AGENT_AI_DISCLOSURE] = True,
        recording_enabled: Annotated[bool | None, _AGENT_RECORDING] = None,
        ai_disclosure_line: Annotated[str | None, _AGENT_DISCLOSURE_LINE] = None,
        language: Annotated[Language | None, _AGENT_LANGUAGE] = None,
        voice_id: Annotated[str | None, _AGENT_VOICE_ID] = None,
        tools: Annotated[list[str] | None, _AGENT_TOOLS] = None,
        max_duration_seconds: Annotated[
            int | None, *_AGENT_DURATION_RULES, _AGENT_MAX_DURATION
        ] = None,
        handover_max_duration_seconds: Annotated[
            int | None, *_AGENT_HANDOVER_RULES, _AGENT_HANDOVER_MAX
        ] = None,
        voice_enabled: Annotated[bool, _AGENT_VOICE_ENABLED] = True,
        sms_enabled: Annotated[bool, _AGENT_SMS_ENABLED] = True,
        email_enabled: Annotated[bool, _AGENT_EMAIL_ENABLED] = True,
        handover_contacts: Annotated[
            list[dict] | None, _AGENT_HANDOVER_CONTACTS
        ] = None,
        http_tools: Annotated[list[dict] | None, _AGENT_HTTP_TOOLS] = None,
    ) -> dict[str, Any]:
        """Save an agent for calls, texts, and email replies.

        A handover contact's phone number is hidden from the call agent.
        Saving an agent does not change any phone number's routing.
        Returns the saved agent record or an error."""
        fields: dict[str, Any] = {
            "name": name,
            "system_prompt": system_prompt,
            "ai_disclosure": ai_disclosure,
            "voice_enabled": voice_enabled,
            "sms_enabled": sms_enabled,
            "email_enabled": email_enabled,
        }
        if recording_enabled is not None:
            fields["recording_enabled"] = recording_enabled
        if first_message is not None:
            fields["first_message"] = first_message
        if ai_disclosure_line is not None:
            fields["ai_disclosure_line"] = ai_disclosure_line
        if language is not None or voice_id is not None:
            fields["voice_config"] = {
                k: v
                for k, v in (("language", language), ("voice_id", voice_id))
                if v is not None
            }
        if tools is not None:
            fields["tools"] = tools
        if max_duration_seconds is not None:
            fields["max_duration_seconds"] = max_duration_seconds
        if handover_max_duration_seconds is not None:
            fields["handover_max_duration_seconds"] = handover_max_duration_seconds
        if handover_contacts is not None:
            fields["handover_contacts"] = handover_contacts
        if http_tools is not None:
            fields["http_tools"] = http_tools
        try:
            async with _client_for(ctx, mode=mode, singleton=singleton) as client:
                return await create_agent(client=client, **fields)
        except HailAPIError as exc:
            return _format_api_error(exc)

    @mcp_app.tool(
        name="route_number",
        title="Route a number to an agent",
        annotations=ToolAnnotations(
            title="Route a number to an agent",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def route_number_tool(
        ctx: Context,
        number_id: Annotated[str, _NUMBER_ID],
        voice_agent_id: Annotated[
            str | None,
            _d(
                "Agent that answers incoming calls, from ``list_agents`` or ``create_agent``."
            ),
        ] = None,
        sms_agent_id: Annotated[
            str | None,
            _d(
                "Agent that answers incoming texts, from ``list_agents`` or ``create_agent``."
            ),
        ] = None,
        clear_voice: Annotated[
            bool, _d("``true`` removes the agent that answers calls.")
        ] = False,
        clear_sms: Annotated[
            bool, _d("``true`` removes the agent that answers texts.")
        ] = False,
    ) -> dict[str, Any]:
        """Set the saved agents that answer a number's calls and texts.

        Omitted settings retain their current values.
        Returns the updated number record or an error."""
        try:
            async with _client_for(ctx, mode=mode, singleton=singleton) as client:
                return await route_number(
                    client=client,
                    number_id=number_id,
                    voice_agent_id=voice_agent_id,
                    sms_agent_id=sms_agent_id,
                    clear_voice=clear_voice,
                    clear_sms=clear_sms,
                )
        except HailAPIError as exc:
            return _format_api_error(exc)

    @mcp_app.tool(
        name="route_email_domain",
        title="Route an email address to an agent",
        annotations=ToolAnnotations(
            title="Route an email address to an agent",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def route_email_domain_tool(
        ctx: Context,
        domain_id: Annotated[
            str, _d("Email identity ID, the ``id`` from ``list_email_domains``.")
        ],
        email_agent_id: Annotated[
            str | None,
            _d(
                "Agent that replies to inbound mail, from ``list_agents`` or "
                "``create_agent``."
            ),
        ] = None,
        email_agent_scope: Annotated[
            EmailAgentScope | None,
            _d(
                "Who the agent answers: ``members`` (verified members of the "
                "workspace whose mail passes DMARC) or ``anyone`` (every sender "
                "whose mail passes DMARC and the spam checks). Omitted: the "
                "current scope stays; a newly set agent starts at ``members``."
            ),
        ] = None,
        clear_agent: Annotated[
            bool, _d("``true`` stops the agent replying to this address's mail.")
        ] = False,
    ) -> dict[str, Any]:
        """Choose which agent answers mail to an address, and who it answers.

        Setting ``email_agent_id`` together with ``clear_agent=True`` is an error.
        With ``email_agent_scope="anyone"``, a stranger can make the agent use its
        allowed tools. Mail without a passing DMARC result gets no reply.
        A custom domain must be verified with inbound and sending on.
        A field left out keeps its value, except that a new agent starts at ``"members"``.

        Returns the updated ``EmailDomainResponse`` as a dict, or
        ``{"error": "<message>"}``.
        """
        try:
            async with _client_for(ctx, mode=mode, singleton=singleton) as client:
                return await route_email_domain(
                    client=client,
                    domain_id=domain_id,
                    email_agent_id=email_agent_id,
                    email_agent_scope=email_agent_scope,
                    clear_agent=clear_agent,
                )
        except HailAPIError as exc:
            return _format_api_error(exc)

    @mcp_app.tool(
        name="quote_numbers",
        title="Quote phone numbers",
        annotations=ToolAnnotations(
            title="Quote phone numbers",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
    )
    async def quote_numbers_tool(
        ctx: Context,
        country_code: Annotated[str, _COUNTRY_CODE],
        capabilities: Annotated[
            list[Capability] | None,
            *_rules(NumberQuoteRequest, "capabilities"),
            _d(
                "What every offered number must do: calls (``voice``), texts "
                "(``sms``) or both. Omitted: both."
            ),
        ] = None,
        number_type: Annotated[
            NumberType | None,
            _d(
                "Offer only this kind of number, for example ``toll_free`` for a "
                "number callers reach for free. Omitted: every kind."
            ),
        ] = None,
        area_code: Annotated[
            str | None,
            *_rules(NumberQuoteRequest, "area_code"),
            _d(
                "First digits of the number as dialed inside the country, such "
                "as ``415``. Omitted: any digits."
            ),
        ] = None,
        city: Annotated[
            str | None,
            *_rules(NumberQuoteRequest, "city"),
            _d("City name as carriers list it, such as ``Austin``. Omitted: any city."),
        ] = None,
        region: Annotated[
            str | None,
            *_rules(NumberQuoteRequest, "region"),
            _d(
                "US state or Canadian province code from ``list_number_regions``, "
                "to tell apart cities with the same name. Omitted: any region."
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Compare available phone numbers and their prices without buying or charging.

        Returns ``offers``, ``recommended_quote_id`` and ``expires_at``.
        Each offer includes ``e164``, ``number_type``, ``capabilities``, ``quote_id``,
        ``monthly_cents``, ``setup_cents``, ``readiness``, ``requirements``,
        ``subject_types``, ``verifications``, ``locality`` and ``region``.
        Prices are USD cents. ``locality`` and ``region`` tell apart cities with the same name.
        Quotes expire after ten minutes. Expired quotes cannot authorize a purchase.
        ``recommended_quote_id`` identifies a ready offer or is null when none is ready.

        ``subject_types`` identifies eligible personal or business registrations.
        ``verifications`` contains accepted registrations, with approved entries first.
        Entries include ``id``, ``state``, ``subject_type`` and ``subject_name``.
        ``readiness="verification_required"`` means registration approval is still needed.
        Registration requirements are specific to the quote and applicant type.
        An offer with ``by_request=true`` has no assigned number and requires a manual support order.
        Such offers remain valid for registration but are unavailable for automatic purchase.
        An upstream failure returns an error."""
        try:
            async with _client_for(ctx, mode=mode, singleton=singleton) as client:
                return await quote_numbers(
                    client=client,
                    country_code=country_code,
                    capabilities=capabilities,
                    number_type=number_type,
                    area_code=area_code,
                    city=city,
                    region=region,
                )
        except RuntimeError as exc:
            return {"error": str(exc)}

    @mcp_app.tool(
        name="acquire_number",
        title="Buy a phone number",
        annotations=ToolAnnotations(
            title="Buy a phone number",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=True,
        ),
    )
    async def acquire_number_tool(
        ctx: Context,
        quote_id: Annotated[str, _QUOTE_ID],
        country_code: Annotated[
            str,
            _d(
                "Country of the number being bought, as an ISO alpha-2 code: the "
                "``country_code`` sent to ``quote_numbers`` for this offer."
            ),
        ],
        number_type: Annotated[
            NumberType,
            _d(
                "Kind of number being bought: copy the offer's ``number_type`` "
                "from ``quote_numbers``. Hail checks it against the offer."
            ),
        ],
        confirm_total_cents: Annotated[
            int,
            _d(
                "The offer's ``setup_cents`` plus ``monthly_cents``, in USD cents, "
                "as the user authorized it."
            ),
        ],
        idempotency_key: Annotated[str | None, _IDEMPOTENCY_KEY] = None,
        subject_type: Annotated[
            str | None,
            _SUBJECT_TYPE_OR_NULL,
            _d(
                "Whose approved registration may be used: the user's own "
                "(``person``) or their company's (``business``). Omitted: either."
            ),
        ] = None,
        verification_id: Annotated[
            str | None,
            _UUID_OR_NULL,
            _d(
                "Which approved registration covers this purchase: an ``id`` from "
                "the offer's ``verifications``. Give it when the offer lists more "
                "than one approved registration. Omitted: the only approved one, "
                "if any."
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Buy a quoted phone number with explicit price confirmation.

        This charges the setup fee and first month immediately, then renews monthly.
        The purchase requires the user's authorization for that number and cost.
        Expired or changed offers return 409 without authorizing a different price.
        Each quote can buy at most one number.

        A retry with the same quote and ``idempotency_key`` refers to the original purchase.
        After a timeout, a new quote starts a different purchase and can buy a second number.
        A returned retry key is available after a timeout or server error.
        A 4xx failure returns no retry key; that failed request cannot be reused.
        A rejected or ambiguous registration selection returns 422.
        A purchase does not assign an answering agent automatically.
        Returns the number record and ``idempotency_key``, or an error with any available retry key.
        """
        try:
            async with _client_for(ctx, mode=mode, singleton=singleton) as client:
                return await acquire_number(
                    client=client,
                    quote_id=quote_id,
                    country_code=country_code,
                    number_type=number_type,
                    confirm_total_cents=confirm_total_cents,
                    idempotency_key=idempotency_key,
                    subject_type=subject_type,
                    verification_id=verification_id,
                )
        except RuntimeError as exc:
            return {"error": str(exc)}

    @mcp_app.tool(
        name="list_numbers",
        title="List phone numbers",
        annotations=ToolAnnotations(
            title="List phone numbers",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def list_numbers_tool(
        ctx: Context,
        limit: Annotated[int, _LIMIT_200] = 50,
        cursor: Annotated[str | None, _CURSOR] = None,
    ) -> dict[str, Any]:
        """List dedicated workspace phone numbers, newest first.

        Items include ID, E.164 number, country, type, capabilities, provisioning state and assigned agents.
        Returns ``items`` and ``next_cursor``, or an error."""
        try:
            async with _client_for(ctx, mode=mode, singleton=singleton) as client:
                return await list_numbers(client=client, limit=limit, cursor=cursor)
        except RuntimeError as exc:
            return {"error": str(exc)}

    @mcp_app.tool(
        name="list_number_regions",
        title="List number search regions",
        annotations=ToolAnnotations(
            title="List number search regions",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def list_number_regions_tool(ctx: Context) -> dict[str, Any]:
        """List US state and Canadian province codes supported by number searches.

        Returns ``regions`` containing ``country_code``, ``code`` and ``name``.
        These codes identify the optional region filter for a city search.
        An upstream failure returns an error."""
        try:
            async with _client_for(ctx, mode=mode, singleton=singleton) as client:
                return await list_number_regions(client=client)
        except RuntimeError as exc:
            return {"error": str(exc)}

    @mcp_app.tool(
        name="get_number",
        title="Get a number",
        annotations=ToolAnnotations(
            title="Get a number",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def get_number_tool(
        ctx: Context,
        number_id: Annotated[str, _NUMBER_ID],
    ) -> dict[str, Any]:
        """Read a workspace number's state, capabilities and assigned agents.

        Returns the number record or an error."""
        try:
            async with _client_for(ctx, mode=mode, singleton=singleton) as client:
                return await get_number(client=client, number_id=number_id)
        except RuntimeError as exc:
            return {"error": str(exc)}

    @mcp_app.tool(
        name="release_number",
        title="Release a number",
        annotations=ToolAnnotations(
            title="Release a number",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=True,
        ),
    )
    async def release_number_tool(
        ctx: Context,
        number_id: Annotated[str, _NUMBER_ID],
        confirm_e164: Annotated[
            str,
            _d(
                "The number's ``e164`` value, such as ``+14155551234``, as a safety check."
            ),
        ],
    ) -> dict[str, Any]:
        """Permanently release a phone number after explicit user authorization.

        A mismatched ``confirm_e164`` returns an error without releasing anything.
        Calls and texts to a released number stop. Recovery is not guaranteed.
        Current and previous monthly fees remain due, without a refund.
        Pending orders cannot be released and return 409.
        A failed order was not purchased; this operation only dismisses it from the list.
        Returns ``released`` or ``dismissed``, plus ``number_id`` and ``e164``, or an error.
        """
        try:
            async with _client_for(ctx, mode=mode, singleton=singleton) as client:
                return await release_number(
                    client=client, number_id=number_id, confirm_e164=confirm_e164
                )
        except RuntimeError as exc:
            return {"error": str(exc)}

    @mcp_app.tool(
        name="get_verification_requirements",
        title="Get verification requirements",
        annotations=ToolAnnotations(
            title="Get verification requirements",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
    )
    async def get_verification_requirements_tool(
        ctx: Context,
        country_code: Annotated[str, _COUNTRY_CODE],
        number_type: Annotated[NumberType, _NUMBER_TYPE],
        subject_type: Annotated[str, _SUBJECT_TYPE_SCHEMA, _SUBJECT_TYPE],
        quote_id: Annotated[
            str | None,
            _d(
                "The offer to register for: its ``quote_id`` from "
                "``quote_numbers``, which selects that offer's form. Give it when "
                "``purpose`` is ``number``; leave it out for ``sms``."
            ),
        ] = None,
        number_id: Annotated[str | None, _VERIFICATION_NUMBER_ID] = None,
        purpose: Annotated[str, _PURPOSE_SCHEMA, _VERIFICATION_PURPOSE] = "number",
    ) -> dict[str, Any]:
        """Read a carrier's current registration fields, documents, address rules and fees.

        The response's ``subject_types`` lists the applicant types the carrier accepts.
        US local SMS registration uses ``purpose="sms"``, country ``US`` and type ``local``.
        Toll-free verification uses ``purpose="sms"``, the number's country ``US`` or ``CA``,
        type ``toll_free`` and its owned ``number_id``. Each number needs its own verification.
        Applicant country and business tax status determine the registration route.
        The personal route is available to US or Canadian applicants without a business tax number.
        Local 10DLC fees apply at submission, carrier review and monthly after approval.
        Toll-free verification is free and sends no identity confirmation code.

        Returns ``required``, ``fields``, ``documents`` and ``address_required``.
        ``required=false`` means a registration submission is unnecessary and would fail.
        Fields and files require accurate information supplied by the authorized applicant.
        Choice fields accept comma-separated option keys within their ``min_choices`` and ``max_choices``.
        Field ``max_length`` uses UTF-16 code units; an emoji counts as two.
        Selected consent methods can require evidence even when shared fields are optional.
        No public link for an ``*_evidence_url`` field? Upload the screenshot with
        ``create_file_upload`` (purpose ``sms_evidence``) and use the ``url`` it returns.
        Document slots with ``needs_input=false`` require no upload.
        An upstream failure returns an error."""
        try:
            async with _client_for(ctx, mode=mode, singleton=singleton) as client:
                return await get_verification_requirements(
                    client=client,
                    country_code=country_code,
                    number_type=number_type,
                    subject_type=subject_type,
                    quote_id=quote_id,
                    number_id=number_id,
                    purpose=purpose,
                )
        except RuntimeError as exc:
            return {"error": str(exc)}

    @mcp_app.tool(
        name="check_verification",
        title="Check a verification",
        annotations=ToolAnnotations(
            title="Check a verification",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
    )
    async def check_verification_tool(
        ctx: Context,
        country_code: Annotated[str, _COUNTRY_CODE],
        number_type: Annotated[str, _NUMBER_TYPE_SCHEMA, _NUMBER_TYPE],
        fields: Annotated[_JsonObject, _VERIFICATION_FIELDS],
        documents: Annotated[_JsonObject, _VERIFICATION_DOCUMENTS],
        files: Annotated[_VERIFICATION_FILES_LIST, _VERIFICATION_FILES],
        # Required, but checked inside check_verification, so a missing one
        # gets the message that tells the model to ask the user.
        subject_type: Annotated[
            str | None, _SUBJECT_TYPE_OR_NULL, _SUBJECT_TYPE_REQUIRED
        ] = None,
        address: Annotated[_ADDRESS_OR_NULL, _VERIFICATION_ADDRESS] = None,
        quote_id: Annotated[str | None, _UUID_OR_NULL, _VERIFICATION_QUOTE_ID] = None,
        number_id: Annotated[str | None, _UUID_OR_NULL, _VERIFICATION_NUMBER_ID] = None,
        purpose: Annotated[str, _PURPOSE_SCHEMA, _VERIFICATION_PURPOSE] = "number",
    ) -> dict[str, Any]:
        """Check a carrier registration form without filing anything.

        Takes the same arguments as ``submit_verification`` except ``attest_authorized``.
        Runs every check Hail can run before filing: purpose rules, credit (402),
        the carrier's current form, duplicates (409), field lengths, each file
        (type, size, and for ``upload_id`` that it is this workspace's verification
        document, uploaded within the last hour), and the form's required fields,
        documents and address (for ``purpose="sms"``, the US texting application).
        Nothing is filed, saved, held or charged, and uploads stay usable.
        Returns ``ok``, ``files`` (slot, upload_id, size_bytes, sha256 per file) and
        ``note``; problems come back as ``problems`` with the field named.
        The carrier runs its own checks at submission."""
        # Untyped on purpose, same as submit_verification: MCPServer's schema
        # validation would echo a rejected value into the error.
        try:
            async with _client_for(ctx, mode=mode, singleton=singleton) as client:
                return await check_verification(
                    client=client,
                    country_code=country_code,
                    number_type=number_type,
                    fields=fields,
                    documents=documents,
                    files=files,
                    subject_type=subject_type,
                    address=address,
                    quote_id=quote_id,
                    number_id=number_id,
                    purpose=purpose,
                )
        except RuntimeError:
            # Not the exception text: it must never carry submitted values.
            return {"error": "could not authenticate the request"}

    @mcp_app.tool(
        name="check_verification_documents",
        title="Read verification documents",
        annotations=ToolAnnotations(
            title="Read verification documents",
            # Not read-only: each call uses the workspace's daily document reads,
            # which cost money.
            readOnlyHint=False,
            destructiveHint=False,
            # Each call uses reads of the workspace's daily quota.
            idempotentHint=False,
            openWorldHint=True,
        ),
    )
    async def check_verification_documents_tool(
        ctx: Context,
        country_code: Annotated[str, _COUNTRY_CODE],
        number_type: Annotated[str, _NUMBER_TYPE_SCHEMA, _NUMBER_TYPE],
        fields: Annotated[_JsonObject, _VERIFICATION_FIELDS],
        documents: Annotated[_JsonObject, _DOCUMENT_CHECK_DOCUMENTS],
        files: Annotated[_VERIFICATION_FILES_LIST, _VERIFICATION_FILES],
        quote_id: Annotated[str | None, _UUID_OR_NULL, _VERIFICATION_QUOTE_ID] = None,
        subject_type: Annotated[
            str | None, _SUBJECT_TYPE_OR_NULL, _SUBJECT_TYPE_REQUIRED
        ] = None,
        address: Annotated[_PARTIAL_ADDRESS_OR_NULL, _VERIFICATION_ADDRESS] = None,
    ) -> dict[str, Any]:
        """Read the files of a number verification form, before the form is filled or submitted.

        Takes the arguments of ``check_verification``. ``fields`` and ``address``
        may be empty or partial. An AI model reads each file.
        Returns ``fields``, ``address`` and ``documents.<slot>.fields``: the values the
        files show, by the form's own keys, to fill the form after the user confirms
        them. ``documents.<slot>.option`` is the document type the file looks like.
        ``warnings`` (``field``, ``kind``, ``message``) lists what the carrier may
        refuse: an unreadable, upside-down or expired document, a file of another
        type than the chosen one, or a typed name that differs from the printed one.
        Each message names the fix. A submission with warnings is still accepted.
        A file nothing was read from has ``read`` false; an unclear file also has an
        ``unreadable`` warning, and a file past the daily reads has ``skipped``
        ``daily_limit``. Only the first 3 pages of a PDF are read; a PDF of more
        than 5 pages is not read.
        Each file read uses one of the workspace's daily document reads (30 per day);
        a file given for several slots uses one. Nothing is filed, saved, held or
        charged, and uploads stay usable."""
        try:
            async with _client_for(ctx, mode=mode, singleton=singleton) as client:
                return await check_verification_documents(
                    client=client,
                    country_code=country_code,
                    number_type=number_type,
                    fields=fields,
                    documents=documents,
                    files=files,
                    subject_type=subject_type,
                    address=address,
                    quote_id=quote_id,
                )
        except RuntimeError:
            # Not the exception text: it must never carry submitted values.
            return {"error": "could not authenticate the request"}

    @mcp_app.tool(
        name="submit_verification",
        title="Submit a verification",
        annotations=ToolAnnotations(
            title="Submit a verification",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=True,
        ),
    )
    async def submit_verification_tool(
        ctx: Context,
        country_code: Annotated[str, _COUNTRY_CODE],
        number_type: Annotated[str, _NUMBER_TYPE_SCHEMA, _NUMBER_TYPE],
        fields: Annotated[_JsonObject, _VERIFICATION_FIELDS],
        documents: Annotated[_JsonObject, _VERIFICATION_DOCUMENTS],
        files: Annotated[_VERIFICATION_FILES_LIST, _VERIFICATION_FILES],
        attest_authorized: Annotated[StrictBool, _ATTEST_AUTHORIZED],
        # Required, but checked inside submit_verification, so a missing one
        # gets the message that tells the model to ask the user.
        subject_type: Annotated[
            str | None, _SUBJECT_TYPE_OR_NULL, _SUBJECT_TYPE_REQUIRED
        ] = None,
        address: Annotated[_ADDRESS_OR_NULL, _VERIFICATION_ADDRESS] = None,
        quote_id: Annotated[str | None, _UUID_OR_NULL, _VERIFICATION_QUOTE_ID] = None,
        number_id: Annotated[str | None, _UUID_OR_NULL, _VERIFICATION_NUMBER_ID] = None,
        purpose: Annotated[str, _PURPOSE_SCHEMA, _VERIFICATION_PURPOSE] = "number",
    ) -> dict[str, Any]:
        """Submit authorized applicant details and documents for carrier registration.

        Submission is irreversible once sent for review. Carriers retain submitted documents.
        ``fields``, ``address``, ``documents`` and ``files`` follow the carrier's current requirements.
        SMS campaign text requires user review and approval before submission.

        ``purpose="number"``: the balance must cover the offer's setup fee plus first month;
        otherwise nothing is sent. That amount is reserved during review and pays for a
        later purchase using this registration. It is returned after rejection, cancellation
        or 30 days without purchase following approval.
        ``purpose="sms"`` registers US local numbers for US texting with carrier filing fees.
        Free toll-free verification uses type ``toll_free``, country ``US`` or ``CA``, the
        owned ``number_id`` and ``registration_kind=toll_free`` among the form fields.

        Base64 files are written into the tool call and add about a third to their size.
        Base64 files total at most 29 MiB, checked here; the API checks the 30 MB total of
        all files, uploads included. Slot names use letters, digits, hyphens and underscores,
        up to 64 characters. At most 100 fields are accepted.
        ``check_verification`` takes the same arguments and files nothing.

        Returns ``id``, ``state`` and ``subject_name``. Carrier review takes business days.
        A 409 identifies an existing registration; a timeout can leave a submission in progress.
        Carrier field errors include ``problems`` and leave no saved submission."""
        # ``fields``, ``documents``, ``files`` and ``address`` are untyped on
        # purpose: MCPServer's schema validation echoes a rejected value
        # (``input_value=...``) into the error the agent sees. The shape is
        # checked inside ``submit_verification`` with messages that name the
        # argument only.
        try:
            async with _client_for(ctx, mode=mode, singleton=singleton) as client:
                return await submit_verification(
                    client=client,
                    country_code=country_code,
                    number_type=number_type,
                    fields=fields,
                    documents=documents,
                    files=files,
                    attest_authorized=attest_authorized,
                    subject_type=subject_type,
                    address=address,
                    quote_id=quote_id,
                    number_id=number_id,
                    purpose=purpose,
                )
        except RuntimeError:
            # Not the exception text: it must never carry submitted values.
            return {"error": "could not authenticate the request"}

    @mcp_app.tool(
        name="resubmit_verification",
        title="Fix and resubmit a verification",
        annotations=ToolAnnotations(
            title="Fix and resubmit a verification",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=True,
        ),
    )
    async def resubmit_verification_tool(
        ctx: Context,
        verification_id: Annotated[
            str,
            _d(
                "ID of a rejected number registration with "
                "``resubmittable: true``, from ``list_verifications``."
            ),
        ],
        attest_authorized: Annotated[StrictBool, _ATTEST_AUTHORIZED],
        fields: Annotated[
            _JsonObject | None,
            _d("All form fields, replacing the kept ones. Null keeps them."),
        ] = None,
        documents: Annotated[
            _JsonObject | None,
            _d(
                "All document slots ``{slot: {option, fields}}``, replacing the "
                "kept ones. Null keeps them."
            ),
        ] = None,
        files: Annotated[
            _VERIFICATION_FILES_LIST | None,
            _d(
                "Only the files that replace kept ones, in the "
                "``submit_verification`` format. A slot not listed reuses its "
                "kept file."
            ),
        ] = None,
        address: Annotated[_ADDRESS_OR_NULL, _VERIFICATION_ADDRESS] = None,
        quote_id: Annotated[
            str | None,
            _UUID_OR_NULL,
            _d("A new offer, for when the kept quote is gone (404). Null reuses it."),
        ] = None,
    ) -> dict[str, Any]:
        """File a rejected number registration again, with fixes, from what Hail kept.

        Hail keeps a number registration's answers and files for 30 days after
        submit (``kept_until``); ``get_sms_application`` returns the kept answers
        and file names. Each argument given replaces its kept value; the rest
        are reused. Country, number type and subject type stay as filed.

        Irreversible once sent: it files a new registration (a new ``id``) with
        the same credit rules as ``submit_verification``, and dismisses the
        rejected one. Returns the new registration, or an error with
        ``problems`` when nothing was sent."""
        try:
            async with _client_for(ctx, mode=mode, singleton=singleton) as client:
                return await resubmit_verification(
                    client=client,
                    verification_id=verification_id,
                    attest_authorized=attest_authorized,
                    fields=fields,
                    documents=documents,
                    files=files,
                    address=address,
                    quote_id=quote_id,
                )
        except RuntimeError:
            # Not the exception text: it must never carry submitted values.
            return {"error": "could not authenticate the request"}

    @mcp_app.tool(
        name="list_verifications",
        title="List verifications",
        annotations=ToolAnnotations(
            title="List verifications",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
    )
    async def list_verifications_tool(
        ctx: Context,
        purpose: Annotated[
            VerificationPurpose | None,
            _d(
                "Show one kind of registration: ``number`` for registrations that "
                "let the user buy numbers, ``sms`` for US texting. Omitted: both."
            ),
        ] = None,
    ) -> dict[str, Any]:
        """List workspace carrier registrations, newest first.

        Items include IDs, applicant names and types, country, number type, purpose,
        state, rejection details, timestamps, confirmation method and SMS progress fields.
        States are awaiting_review, submitting, submitted, approved, rejected or cancelled.
        SMS steps include confirm_identity, filing, carrier_review, needs_information and runtime_setup.
        ``campaign_accepted`` alone does not authorize texting: runtime readiness and number attachment are also required.
        ``identity_confirmed`` can carry over to a later filing for the same person.
        ``waiting_for_credit`` means a carrier fee prevents the next step.
        ``credit_needed_cents`` is that registration's share of the additional credit needed,
        or null when it is not waiting. Shares across waiting registrations are additive.
        ``confirmation="code"`` identifies a carrier-texted confirmation code.
        Returns ``items`` or an error."""
        try:
            async with _client_for(ctx, mode=mode, singleton=singleton) as client:
                return await list_verifications(client=client, purpose=purpose)
        except RuntimeError as exc:
            return {"error": str(exc)}

    @mcp_app.tool(
        name="get_verification",
        title="Get a verification",
        annotations=ToolAnnotations(
            title="Get a verification",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
    )
    async def get_verification_tool(
        ctx: Context, verification_id: Annotated[str, _VERIFICATION_ID]
    ) -> dict[str, Any]:
        """Read one carrier registration's current state and reported next step.

        The carrier state refreshes at most once per minute. Review can take business days.
        ``step="needs_information"`` means the existing SMS application needs corrections;
        its saved applicant details and paid identity remain attached to the same registration.
        An approved registration can make matching number offers ready for purchase.
        Rejections include ``rejection_reason``.
        ``waiting_for_credit`` indicates a fee awaiting payment.
        ``credit_needed_cents`` is this registration's share of the additional credit needed.
        Returns the registration record or an error."""
        try:
            async with _client_for(ctx, mode=mode, singleton=singleton) as client:
                return await get_verification(
                    client=client, verification_id=verification_id
                )
        except RuntimeError as exc:
            return {"error": str(exc)}

    @mcp_app.tool(
        name="get_sms_application",
        title="Read saved SMS application",
        annotations=ToolAnnotations(
            title="Read saved SMS application",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
    )
    async def get_sms_application_tool(
        ctx: Context,
        verification_id: Annotated[
            str,
            _d(
                "ID of the original registration, from ``list_verifications``: "
                "an SMS one, including one needing corrections, or a rejected "
                "number one."
            ),
        ],
    ) -> dict[str, Any]:
        """Read saved answers for an existing workspace registration.

        Returns private applicant fields, address and available sender information.
        An older application without saved answers returns 404.
        A number registration (``purpose="number"``) returns its kept answers,
        ``documents`` and the kept ``files`` by name (without their bytes) until
        ``kept_until``; ``resubmit_verification`` files it again from them.
        This read can recover an existing sender's details but creates no brand and files no campaign.
        Corrections retain the existing registration and its paid identity."""
        try:
            async with _client_for(ctx, mode=mode, singleton=singleton) as client:
                return await get_sms_application(
                    client=client, verification_id=verification_id
                )
        except RuntimeError as exc:
            return {"error": str(exc)}

    @mcp_app.tool(
        name="preview_sms_application",
        title="Preview a US texting registration",
        annotations=ToolAnnotations(
            title="Preview a US texting registration",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
    )
    async def preview_sms_application_tool(
        ctx: Context,
        subject_type: Annotated[str, _SUBJECT_TYPE_SCHEMA, _SUBJECT_TYPE],
        fields: Annotated[
            _JsonObject,
            _d(
                "Proposed applicant answers keyed by the field names in "
                "``get_verification_requirements`` with purpose ``sms``."
            ),
        ],
        address: Annotated[_ADDRESS_OR_NULL, _VERIFICATION_ADDRESS] = None,
        number_id: Annotated[
            str | None,
            _d(
                "Owned toll-free number ID from ``list_numbers``, for a toll-free "
                "preview. Omitted: a US local preview."
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Preview local or toll-free SMS registration text without filing or charging.

        The response contains the description, consent flow and samples for user review before submission.
        Answers require actual applicant information and consent evidence.
        Evidence links can come from ``create_file_upload`` with purpose ``sms_evidence``.
        Field or length errors appear in ``problems``.
        Limits are 100 fields and 64 KiB of combined fields and address.
        A successful preview does not guarantee submission: duplicates, low credit or carrier checks can still prevent it.
        """
        try:
            async with _client_for(ctx, mode=mode, singleton=singleton) as client:
                return await preview_sms_application(
                    client=client,
                    subject_type=subject_type,
                    fields=fields,
                    address=address,
                    number_id=number_id,
                )
        except RuntimeError as exc:
            return {"error": str(exc)}

    @mcp_app.tool(
        name="amend_sms_application",
        title="Correct an existing SMS application",
        annotations=ToolAnnotations(
            title="Correct an existing SMS application",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=True,
        ),
    )
    async def amend_sms_application_tool(
        ctx: Context,
        verification_id: Annotated[
            str,
            _d(
                "ID of the SMS registration at step ``needs_information``, from "
                "``list_verifications``; the same paid application and identity."
            ),
        ],
        fields: Annotated[
            _JsonObject,
            _d(
                "Complete corrected answers that satisfy the current requirements; "
                "``get_sms_application`` returns the saved ones."
            ),
        ],
        attest_authorized: Annotated[
            StrictBool,
            _d(
                "``true`` records that the applicant reviewed and approved these "
                "corrections."
            ),
        ],
        address: Annotated[
            _ADDRESS_OR_NULL,
            _d(
                "The sender's original address, as saved; ``get_sms_application`` "
                "returns it. Omitted: the correction is refused, because Hail "
                "cannot confirm the sender is unchanged."
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Correct an existing SMS registration at the needs_information step.

        The address must be accurate; saved details can be reused when still correct.
        Identity changes are rejected. This operation does not create a replacement brand.
        Actual consent evidence is required; ``create_file_upload`` with purpose
        ``sms_evidence`` returns a public evidence link.
        Limits are 100 fields and 64 KiB of combined fields and address. No documents are uploaded.
        A later campaign filing can incur the normal review fee.
        After a timeout, the existing registration can already reflect the correction.
        Returns the updated registration or an error."""
        try:
            async with _client_for(ctx, mode=mode, singleton=singleton) as client:
                return await amend_sms_application(
                    client=client,
                    verification_id=verification_id,
                    fields=fields,
                    address=address,
                    attest_authorized=attest_authorized,
                )
        except RuntimeError as exc:
            return {"error": str(exc)}

    @mcp_app.tool(
        name="cancel_verification",
        title="Cancel a verification",
        annotations=ToolAnnotations(
            title="Cancel a verification",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=True,
        ),
    )
    async def cancel_verification_tool(
        ctx: Context, verification_id: Annotated[str, _VERIFICATION_ID]
    ) -> dict[str, Any]:
        """Cancel an eligible carrier registration after explicit user authorization.

        Awaiting-review drafts are discarded; rejected registrations are dismissed.
        A rejected registration must be dismissed before a new one is submitted
        for the same country and number type.
        An approved US SMS registration can be cancelled: attached numbers lose US texting eligibility
        and its monthly fee ends after the current month.
        Some registrations have a minimum of three monthly fees even after early cancellation.
        Submitting or submitted registrations normally return 409; an accepted SMS campaign
        at runtime_setup is an exception.
        An SMS application at needs_information supports correction on its existing paid identity.
        Returns the cancelled registration or an error."""
        try:
            async with _client_for(ctx, mode=mode, singleton=singleton) as client:
                return await cancel_verification(
                    client=client, verification_id=verification_id
                )
        except RuntimeError as exc:
            return {"error": str(exc)}

    @mcp_app.tool(
        name="confirm_verification",
        title="Confirm a verification with a code",
        annotations=ToolAnnotations(
            title="Confirm a verification with a code",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        ),
    )
    async def confirm_verification_tool(
        ctx: Context,
        verification_id: Annotated[
            str,
            _d(
                'ID of a registration with ``confirmation="code"``, from '
                "``list_verifications`` or ``get_verification``."
            ),
        ],
        code: Annotated[
            str,
            Field(min_length=4, max_length=10),
            _d(
                "The code the carrier texted to the applicant's mobile, exactly "
                "as the applicant read it out."
            ),
        ],
    ) -> dict[str, Any]:
        """Confirm a registration using the code the carrier sent to the applicant.

        Incorrect or expired codes return a field error; the carrier can send a replacement after a day.
        Success advances the registration but does not guarantee immediate approval.
        A ``confirmation="reply"`` registration requires a direct reply to the carrier's text instead.
        Returns the updated registration or an error."""
        try:
            async with _client_for(ctx, mode=mode, singleton=singleton) as client:
                return await confirm_verification(
                    client=client, verification_id=verification_id, code=code
                )
        except RuntimeError as exc:
            return {"error": str(exc)}

    @mcp_app.tool(
        name="get_agent",
        title="Get an agent",
        annotations=ToolAnnotations(
            title="Get an agent",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def get_agent_tool(
        ctx: Context,
        agent_id: Annotated[str, _AGENT_ID],
    ) -> dict[str, Any]:
        """Read one saved workspace agent by ID.

        Returns its name, instructions, greeting, disclosure, voice settings, enabled channels,
        status, limits and other saved settings, or an error."""
        try:
            async with _client_for(ctx, mode=mode, singleton=singleton) as client:
                return await get_agent(client=client, agent_id=agent_id)
        except RuntimeError as exc:
            return {"error": str(exc)}

    @mcp_app.tool(
        name="update_agent",
        title="Update an agent",
        annotations=ToolAnnotations(
            title="Update an agent",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def update_agent_tool(
        ctx: Context,
        agent_id: Annotated[str, _AGENT_ID],
        name: Annotated[str | None, _AGENT_NAME] = None,
        system_prompt: Annotated[str | None, _AGENT_PROMPT] = None,
        first_message: Annotated[str | None, _AGENT_FIRST_MESSAGE] = None,
        ai_disclosure: Annotated[bool | None, _AGENT_AI_DISCLOSURE] = None,
        recording_enabled: Annotated[bool | None, _AGENT_RECORDING] = None,
        ai_disclosure_line: Annotated[str | None, _AGENT_DISCLOSURE_LINE] = None,
        language: Annotated[Language | None, _AGENT_LANGUAGE] = None,
        voice_id: Annotated[str | None, _AGENT_VOICE_ID] = None,
        tools: Annotated[list[str] | None, _AGENT_TOOLS] = None,
        max_duration_seconds: Annotated[
            int | None, *_AGENT_UPDATE_DURATION_RULES, _AGENT_MAX_DURATION
        ] = None,
        handover_max_duration_seconds: Annotated[
            int | None, *_AGENT_UPDATE_HANDOVER_RULES, _AGENT_HANDOVER_MAX
        ] = None,
        voice_enabled: Annotated[bool | None, _AGENT_VOICE_ENABLED] = None,
        sms_enabled: Annotated[bool | None, _AGENT_SMS_ENABLED] = None,
        email_enabled: Annotated[bool | None, _AGENT_EMAIL_ENABLED] = None,
        status: Annotated[
            AgentStatus | None,
            _d("``paused`` stops the agent answering; ``live`` turns it back on."),
        ] = None,
        handover_contacts: Annotated[
            list[dict] | None,
            _d(
                "Replaces the whole handover list; an empty list removes it. "
                "Up to ten ``{contact_id, note}`` entries, with ``contact_id`` from "
                "``list_contacts``, ``lookup_contact`` or ``create_contact`` and a "
                "``note`` of 1 to 200 characters."
            ),
        ] = None,
        http_tools: Annotated[list[dict] | None, _AGENT_HTTP_TOOLS] = None,
        clear_fields: Annotated[
            list[ClearableAgentField] | None,
            _d(
                "Settings to reset to the workspace default. "
                "Omitted: nothing is reset."
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Update selected fields on a saved workspace agent.

        Omitted fields retain their values. Repeating the same update gives the same result.
        Changing only language or voice preserves the other value.
        A field cannot be both set and cleared in the same request.
        Removing a greeting makes an inbound agent wait for the caller.
        Other resets restore workspace defaults; the handover limit resets to 1800 seconds.
        Existing calls keep their starting settings; changes affect later calls and texts.
        Returns the updated agent or an error."""
        try:
            async with _client_for(ctx, mode=mode, singleton=singleton) as client:
                return await update_agent(
                    client=client,
                    agent_id=agent_id,
                    clear_fields=clear_fields,
                    **{
                        k: v
                        for k, v in {
                            "name": name,
                            "system_prompt": system_prompt,
                            "first_message": first_message,
                            "ai_disclosure": ai_disclosure,
                            "recording_enabled": recording_enabled,
                            "ai_disclosure_line": ai_disclosure_line,
                            "language": language,
                            "voice_id": voice_id,
                            "tools": tools,
                            "max_duration_seconds": max_duration_seconds,
                            "handover_max_duration_seconds": handover_max_duration_seconds,
                            "voice_enabled": voice_enabled,
                            "sms_enabled": sms_enabled,
                            "email_enabled": email_enabled,
                            "status": status,
                            "handover_contacts": handover_contacts,
                            "http_tools": http_tools,
                        }.items()
                        if v is not None
                    },
                )
        except RuntimeError as exc:
            return {"error": str(exc)}

    @mcp_app.tool(
        name="delete_agent",
        title="Delete an agent",
        annotations=ToolAnnotations(
            title="Delete an agent",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def delete_agent_tool(
        ctx: Context,
        agent_id: Annotated[str, _AGENT_ID],
        confirm_name: Annotated[
            str, _d("The agent's saved ``name``, as a safety check.")
        ],
    ) -> dict[str, Any]:
        """Permanently delete a saved agent after explicit user authorization.

        A mismatched ``confirm_name`` returns an error without deleting anything.
        Numbers assigned to the deleted agent stop answering calls with it;
        texts are delivered to webhooks only.
        Pausing an agent is a separate non-deleting status change.
        Returns ``deleted``, ``agent_id`` and ``name``, or an error."""
        try:
            async with _client_for(ctx, mode=mode, singleton=singleton) as client:
                return await delete_agent(
                    client=client, agent_id=agent_id, confirm_name=confirm_name
                )
        except RuntimeError as exc:
            return {"error": str(exc)}

    @mcp_app.tool(
        name="list_contacts",
        title="List contacts",
        annotations=ToolAnnotations(
            title="List contacts",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def list_contacts_tool(
        ctx: Context,
        q: Annotated[
            str | None,
            _d("Only contacts whose name, email or phone contains this text."),
        ] = None,
        limit: Annotated[
            int | None,
            Field(
                ge=1,
                le=500,
                description="How many contacts to return in one reply. Omitted: 100.",
            ),
        ] = None,
    ) -> dict[str, Any]:
        """List workspace members and manually saved contacts.

        Returns ``items`` with ID, kind, name, phone_e164, email and role.
        Members have ``kind="member"``, an ID of ``member:<user_id>`` and a role.
        Manual entries have ``kind="manual"``, a UUID and no role."""
        try:
            async with _client_for(ctx, mode=mode, singleton=singleton) as client:
                return await list_contacts(client=client, q=q, limit=limit)
        except RuntimeError as exc:
            return {"error": str(exc)}

    @mcp_app.tool(
        name="lookup_contact",
        title="Look up a contact",
        annotations=ToolAnnotations(
            title="Look up a contact",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def lookup_contact_tool(
        ctx: Context,
        query: Annotated[
            str, _d("Part of a name, email address or phone number, such as ``Dana``.")
        ],
    ) -> dict[str, Any]:
        """Find workspace contacts by name, email or phone fragment.

        Returns up to ten matches containing names, phone numbers, email addresses and IDs.
        Several matches can refer to different people."""
        try:
            async with _client_for(ctx, mode=mode, singleton=singleton) as client:
                return await lookup_contact(client=client, query=query)
        except RuntimeError as exc:
            return {"error": str(exc)}

    @mcp_app.tool(
        name="list_email_domains",
        title="List email domains",
        annotations=ToolAnnotations(
            title="List email domains",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def list_email_domains_tool(
        ctx: Context,
        cursor: Annotated[str | None, _CURSOR] = None,
        limit: Annotated[
            int | None,
            Field(
                ge=1,
                le=200,
                description=f"{_PAGE_SIZE} Omitted: the API returns 50.",
            ),
        ] = None,
    ) -> dict[str, Any]:
        """List the workspace's sending identities and default sender address.

        Returns ``items``, ``next_cursor`` and ``default_from``.
        Only identities with ``verification_status="verified"`` can send.
        For ``kind="custom"``, ``domain`` is a domain supporting any local part.
        For ``kind="hail_mail"``, it is already a complete email address.
        One verified custom domain takes priority over the Hail mail address.
        With several verified custom domains, ``default_from`` is null and email sending requires an explicit sender.
        A custom domain with ``sends_enabled`` false cannot send: it is skipped for picks and refuses an explicit ``from_``.
        Toggle it with PATCH ``/v1/email-domains/{id}`` (custom domains only).
        """
        try:
            async with _client_for(ctx, mode=mode, singleton=singleton) as client:
                return await list_email_domains(
                    client=client, cursor=cursor, limit=limit
                )
        except RuntimeError as exc:
            return {"error": str(exc)}

    @mcp_app.tool(
        name="whoami",
        title="Who am I",
        annotations=ToolAnnotations(
            title="Who am I",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def whoami_tool(ctx: Context) -> dict[str, Any]:
        """Identify the account and organization authorized by the current credentials.

        Returns ``auth_kind``, ``organization_id``, ``user_id``, ``email`` and ``name``.
        Shared operator credentials have ``auth_kind="shared"`` with null user details.
        The response identifies the current actor, not a message recipient."""
        try:
            async with _client_for(ctx, mode=mode, singleton=singleton) as client:
                return await whoami(client=client)
        except RuntimeError as exc:
            return {"error": str(exc)}

    @mcp_app.tool(
        name="create_contact",
        title="Create a contact",
        annotations=ToolAnnotations(
            title="Create a contact",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    async def create_contact_tool(
        ctx: Context,
        name: Annotated[str, _d("The contact's name.")],
        phone_e164: Annotated[
            str | None,
            Field(pattern=E164.pattern),
            _d(
                "The number to call or text this contact on, such as "
                "``+14155551234``. Give this, ``email``, or both."
            ),
        ] = None,
        email: Annotated[
            str | None,
            _d(
                "The address to email this contact at, such as "
                "``ana@example.com``. Give this, ``phone_e164``, or both."
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Save a workspace contact with a name and a phone number or email address.

        A duplicate phone or email returns 409.
        Returns the new contact record or an error."""
        try:
            async with _client_for(ctx, mode=mode, singleton=singleton) as client:
                return await create_contact(
                    client=client, name=name, phone_e164=phone_e164, email=email
                )
        except RuntimeError as exc:
            return {"error": str(exc)}
