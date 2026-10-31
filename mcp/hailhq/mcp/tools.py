"""MCP tool surface for the Hail API.

Exposes the following tools to the calling agent:

* ``place_call`` — originate an outbound phone call
* ``get_call`` — fetch the current state of one call
* ``list_calls`` — page through recent calls
* ``get_events`` — page through the event stream (call-narrow or org-wide)
* ``send_email`` — send an outbound email
* ``upload_email_attachment`` — upload a small file (base64) to attach to an outbound email
* ``create_file_upload`` — a one-time link to PUT a file with curl, for an
  email attachment or a verification document
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
* ``submit_verification`` — send the user's details and documents to the
  carrier (irreversible once submitted; needs ``attest_authorized=True``)
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
is the FastMCP wiring step. Each registered tool closure accepts a
FastMCP ``Context`` (auto-injected on dispatch) and uses the
``_client_for`` async context manager to obtain a per-call ``HailClient``
(oauth-rs mode, built from the inbound bearer) or the shared singleton
(static-key mode).
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import json
import re
import time
import uuid
from collections.abc import AsyncIterator
from typing import Annotated, Any

import httpx
from hailhq.core.file_uploads import (
    PURPOSES as _UPLOAD_PURPOSES,
)
from hailhq.core.file_uploads import (
    VERIFICATION_FILE_TYPES,
    VERIFICATION_MAX_FILE_BYTES,
    matches_declared_type,
    valid_content_type,
)
from hailhq.core.internal_webhook import fetch_organization_name
from hailhq.core.schemas import parse_resource_id
from hailhq.core.telemetry import telemetry_enabled
from hailhq.core.telemetry_identity import bind_root, identity_scope, tag_context
from hailhq.mcp.auth import AuthMode
from hailhq.mcp.hail_client import (
    HailAPIError,
    HailClient,
    InvalidPathIdError,
    PreviewShapeError,
)
from pydantic import StrictBool, ValidationError, WithJsonSchema

from mcp.server.fastmcp import Context, FastMCP
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


_VERIFICATION_PURPOSES = ("number", "sms")
_PURPOSE_ERROR = "purpose must be 'number' or 'sms'"
_SUBJECT_TYPES = ("person", "business")
_SUBJECT_TYPE_ERROR = (
    "subject_type must be 'person' or 'business': ask the user whether this "
    "is for them or for their company"
)
_QUOTE_REQUIRED = (
    "quote_id is required for purpose='number': pass the quote_id of the "
    "offer from quote_numbers"
)


# Typed in the JSON schema only. Validation stays ``Any`` so FastMCP never
# echoes a submitted value; ``_verification_shape_error`` checks the shape.
_JsonObject = Annotated[Any, WithJsonSchema({"type": "object"})]
_JsonObjectList = Annotated[
    Any, WithJsonSchema({"type": "array", "items": {"type": "object"}})
]
_JsonObjectOrNone = Annotated[
    Any, WithJsonSchema({"anyOf": [{"type": "object"}, {"type": "null"}]})
]


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
    live here, not in the tool signature: a typed parameter makes FastMCP
    echo the offending value in its validation error."""
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


async def create_file_upload(
    *, client: HailClient, purpose: str, filename: str, content_type: str
) -> dict[str, Any]:
    if purpose not in _UPLOAD_PURPOSES:
        return {
            "error": "purpose must be 'email_attachment' or 'verification_document'"
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
    # Both go into a shell line in single quotes: refuse anything that could
    # end the quote (the API never sends one).
    if not _SHELL_SAFE_URL.fullmatch(link["upload_url"]) or not valid_content_type(
        link["content_type"]
    ):
        return {"error": "the API returned an upload link this tool cannot use"}
    return {
        **link,
        "curl": (
            "curl -sS -X PUT --data-binary @<path to the file> "
            f"-H 'Content-Type: {link['content_type']}' '{link['upload_url']}'"
        ),
        "next": (
            "Run the curl line from a shell, with the file's path after the @. "
            "The response has size_bytes and sha256: compare them with the "
            "local file (for example `wc -c` and `shasum -a 256`). The link "
            "works once, until expires_at."
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
_CLEARABLE_AGENT_FIELDS = (
    "recording_enabled",
    "first_message",
    "ai_disclosure_line",
    "tools",
    "max_duration_seconds",
    "handover_max_duration_seconds",
)


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
    """Extract the Bearer token from FastMCP's request context.

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


class TaggedFastMCP(FastMCP):
    """FastMCP that tags each tool call's trace with the tool name.

    The tool call runs in the session's task, in its own trace, under the
    "MCP server handle request: tools/call" span, so that span is the root.
    """

    async def call_tool(self, name, arguments):
        bind_root()
        tag_context(channel="mcp", tool=name)
        return await super().call_tool(name, arguments)


_ACTOR_TTL_SECONDS = 300
_ACTOR_CACHE_MAX = 1024
_actor_cache: dict[str, tuple[float, dict[str, Any]]] = {}


@contextlib.asynccontextmanager
async def _actor_for(client: HailClient, cache_key: str | None = None):
    identity: dict[str, Any] = {}
    if telemetry_enabled():
        # Key by token hash so the raw bearer is never held as a dict key.
        key = hashlib.sha256(cache_key.encode()).hexdigest() if cache_key else None
        hit = _actor_cache.get(key) if key else None
        if hit and hit[0] > time.monotonic():
            identity = dict(hit[1])
        else:
            try:
                # Resolve the actor only through the API that verifies the bearer.
                actor = await client.whoami()
                identity = {
                    "organization_id": actor.get("organization_id"),
                    "user_id": actor.get("user_id"),
                    "user_email": actor.get("email"),
                    "auth_kind": actor.get("auth_kind"),
                    "actor_kind": actor.get("auth_kind"),
                }
                org_id = actor.get("organization_id")
                if org_id:
                    identity["organization_name"] = await fetch_organization_name(
                        str(org_id)
                    )
                if key:
                    if len(_actor_cache) >= _ACTOR_CACHE_MAX:
                        _actor_cache.clear()
                    _actor_cache[key] = (
                        time.monotonic() + _ACTOR_TTL_SECONDS,
                        dict(identity),
                    )
            except Exception:
                # Metadata enrichment never decides whether the tool is authorized.
                pass
    with identity_scope(identity):
        yield


# --------------------------------------------------------------------------- #
# FastMCP registration.
#
# We register thin wrappers that delegate to the module-level domain
# functions — FastMCP derives the JSON schema from the *registered*
# function's signature, and we want the agent-facing signature to omit
# the ``client`` argument (it's resolved per-call by ``_client_for``).
# Each closure takes a ``ctx: Context`` first param (FastMCP auto-injects
# it) and wraps the delegation in ``async with _client_for(ctx, ...)``.
# A ``RuntimeError`` from ``_client_for`` (missing/malformed bearer in
# oauth-rs mode) is caught and surfaced as a structured tool error.
# --------------------------------------------------------------------------- #


def register_tools(
    mcp_app: FastMCP,
    *,
    mode: AuthMode,
    singleton: HailClient | None,
) -> None:
    """Register the Hail tools on a FastMCP app.

    Tools accept a FastMCP ``Context`` parameter (auto-injected). The
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
        to: str,
        recipient_consent: StrictBool,
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
        """Start an outbound AI phone call.

        At least one of ``system_prompt``, ``llm`` or ``agent_id`` is required.
        An ``agent_id`` selects saved settings; explicit arguments override them.
        ``llm`` contains ``base_url``, ``api_key`` and ``model`` for a compatible endpoint.
        A supplied prompt becomes part of the call agent's instructions.
        ``to`` is an E.164 phone number. ``from_`` is optional and defaults to the first active number.
        ``first_message`` is spoken on pickup. Without it, the agent reacts to the answer or silence.
        ``language`` is a supported lowercase ISO 639-1 code; English is the default.
        An unsupported code returns 422.
        ``ai_disclosure`` is true by default and speaks an AI notice at the start of the call.
        ``ai_disclosure=false`` skips it. US artificial-voice calls (47 CFR 64.1200(b)(1)) require it.
        Hail does not verify the lawful basis.
        ``recording_enabled`` overrides the agent or workspace recording setting for this call.
        ``tools`` limits the call agent's capabilities; an empty list disables them.

        ``recipient_consent=true`` attests to the recipient's lawful consent.
        Other values are rejected. Hail does not verify consent.
        ``message_type="marketing"`` also requires ``consent_source``.
        ``metadata`` is stored on the call record.
        A returned ``idempotency_key`` identifies the original request for a retry.
        A different key represents a new call. A 4xx response returns no key.

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
        to: list[str],
        subject: str,
        recipient_consent: StrictBool,
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
        """Send an email from a verified workspace sender.

        ``to`` is a non-empty list of email addresses.
        At least one of ``body_text`` or ``body_html`` is required.
        Open and click tracking requires an HTML body.
        Plain-text emails still have sending, delivery and bounce events.
        ``cc``, ``bcc`` and ``reply_to`` are optional. Hail supplies no default ``reply_to``.

        Without ``from_``, one verified custom domain takes priority over the Hail mail address.
        Several verified custom domains require an explicit ``from_`` address.
        When configured, a workspace without a sender receives a Hail mail address automatically.
        An explicit sender must match a verified workspace identity.
        ``from_name`` is an optional display name; control characters are rejected.
        ``attachment_ids`` contains IDs from uploaded email attachments.
        ``metadata`` is stored on the email record.

        ``recipient_consent=true`` attests to lawful consent. Other values are rejected.
        Hail does not verify consent.
        ``message_type="marketing"`` also requires ``consent_source``.
        A returned ``idempotency_key`` identifies the original request for a retry.
        A different key represents a new email.

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
        content_base64: str,
        filename: str,
        content_type: str,
    ) -> dict[str, Any]:
        """Upload a small attachment for outbound email, as base64 text.

        Meant for small files (under about 50 KB): every base64 character goes
        through the tool call itself. Larger files go through ``create_file_upload``
        with ``purpose="email_attachment"``: its ``curl`` line sends the bytes, and its
        ``id`` works in ``send_email``'s ``attachment_ids``.
        ``content_base64`` contains the file bytes encoded as base64.
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
        purpose: str,
        filename: str,
        content_type: str,
    ) -> dict[str, Any]:
        """Create a one-time link that uploads one file from a shell with curl.

        The bytes go from the local disk to Hail directly, not through the tool call,
        so any file that is not tiny goes this way.
        ``purpose`` is ``email_attachment`` (25 MB at most; the ``id`` works in
        ``send_email``'s ``attachment_ids``) or ``verification_document`` (JPG, PNG
        or PDF, 10 MB at most; ``{"slot": ..., "upload_id": id}`` works in the
        ``files`` of ``check_verification`` and ``submit_verification`` for 1 hour
        after the upload).
        ``content_type`` is the file's MIME type, like ``application/pdf``.
        Returns ``id``, ``upload_url``, ``expires_at`` (15 minutes), ``max_bytes``
        and a ready ``curl`` line with a placeholder for the file's path after ``@``.
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
    async def get_call_tool(ctx: Context, call_id: str) -> dict[str, Any]:
        """Read a call's current status and timing by ID.

        ``call_id`` identifies an existing workspace call.
        Returns the call record, or an error when the call is unavailable."""
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
        cursor: str | None = None,
        limit: int = 50,
        status: str | None = None,
        to: str | None = None,
    ) -> dict[str, Any]:
        """List recent workspace calls, newest first.

        ``cursor`` accepts the previous page's ``next_cursor``.
        Optional filters are ``status`` and the E.164 destination ``to``.
        Statuses include queued, dialing, ringing, in_progress, completed, failed,
        busy, no_answer and canceled.
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
        to: str,
        body: str,
        recipient_consent: StrictBool,
        from_: str | None = None,
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        consent_source: str | None = None,
        consent_obtained_at: str | None = None,
        message_type: str = "informational",
    ) -> dict[str, Any]:
        """Send an outbound text message.

        ``to`` is an E.164 phone number and ``body`` is the message text.
        Without ``from_``, supported destinations use the workspace's alphanumeric sender ID.
        Other destinations use an eligible workspace SMS-capable number.
        A missing required number returns 422. SMS does not use the shared voice-call pool.
        US long-code texting to US recipients requires ready A2P 10DLC registration and an attached number.

        ``recipient_consent=true`` attests to lawful consent. Other values are rejected.
        Hail does not verify consent.
        ``message_type="marketing"`` also requires ``consent_source``.
        A returned ``idempotency_key`` identifies the original request for a retry.
        A different key represents a new message.

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
    async def get_sms_tool(ctx: Context, sms_id: str) -> dict[str, Any]:
        """Read a text message's current state by ID.

        ``sms_id`` identifies an existing workspace message.
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
        cursor: str | None = None,
        limit: int = 50,
        status: str | None = None,
        to: str | None = None,
    ) -> dict[str, Any]:
        """List recent workspace text messages.

        ``status`` filters by queued, sent, delivered, failed, undelivered or received.
        ``to`` filters by E.164 destination.
        ``cursor`` accepts the previous page's ``next_cursor``.
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
    async def get_email_tool(ctx: Context, email_id: str) -> dict[str, Any]:
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
        cursor: str | None = None,
        limit: int = 50,
        status: str | None = None,
        direction: str | None = None,
    ) -> dict[str, Any]:
        """List workspace email summaries, newest first.

        ``direction`` is ``inbound`` or ``outbound``.
        ``status`` is queued, sent, failed, bounced, complained or received.
        ``cursor`` accepts the previous page's ``next_cursor``.
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
    async def get_email_raw_tool(ctx: Context, email_id: str) -> dict[str, Any]:
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
        ctx: Context, email_id: str, attachment_id: str
    ) -> dict[str, Any]:
        """Return a temporary download URL for an inbound email attachment.

        ``email_id`` identifies the email; ``attachment_id`` identifies one of its attachments.
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
        email_id: str,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Read one email's delivery and engagement timeline.

        Events include sent, delivered, delivery_delayed, bounced, complained,
        rejected, opened and clicked, with ``payload`` and ``occurred_at``.
        Open and click events require an HTML email body.
        ``cursor`` accepts the previous page's ``next_cursor``.
        ``limit`` is 1 to 1000; the default is 100.
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
        from_: str | None = None,
        to: str | None = None,
        bucket: str = "day",
    ) -> dict[str, Any]:
        """Read workspace email delivery counts, rates and time series.

        ``from_`` and ``to`` are ISO 8601 timestamps, defaulting to the last seven days.
        ``bucket`` is ``day`` or ``hour``. Hourly ranges are limited to eight days.
        All ranges are limited to 92 days.
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
        id: str | None = None,
        kind: str | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Read a page of workspace events or events for one resource.

        ``id`` optionally selects ``call:<uuid>``, ``email:<uuid>`` or ``sms:<uuid>``.
        ``kind`` filters event types such as state_change, agent_turn, user_turn, tool_call or error.
        ``cursor`` accepts the previous page's ``next_cursor``.
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
        name: str,
        system_prompt: str,
        first_message: str | None = None,
        ai_disclosure: bool = True,
        recording_enabled: bool | None = None,
        ai_disclosure_line: str | None = None,
        language: str | None = None,
        voice_id: str | None = None,
        tools: list[str] | None = None,
        max_duration_seconds: int | None = None,
        handover_max_duration_seconds: int | None = None,
        voice_enabled: bool = True,
        sms_enabled: bool = True,
        email_enabled: bool = True,
        handover_contacts: list[dict] | None = None,
    ) -> dict[str, Any]:
        """Save an agent. Then call ``route_number`` so a number answers
        with it, ``route_email_domain`` so an email address does, or pass
        its id as ``agent_id`` to ``place_call``.

        ``system_prompt`` defines the saved agent's behavior.
        ``first_message`` is an optional greeting after the AI disclosure.
        Without a greeting, an inbound agent waits for the caller.
        ``ai_disclosure_line`` overrides the workspace line; ``{org}`` expands to the organization name.
        ``language`` is a lowercase ISO 639-1 code.
        ``max_duration_seconds`` is 60 to 3600.
        ``handover_max_duration_seconds`` is 60 to 3600, defaulting to 1800.
        ``handover_contacts`` contains up to ten ``{contact_id, note}`` entries.
        Contact IDs are UUIDs or ``member:<user_id>``. Each contact needs a phone number.
        Each note is 1 to 200 characters. The phone number is hidden from the call agent.
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
        number_id: str,
        voice_agent_id: str | None = None,
        sms_agent_id: str | None = None,
        clear_voice: bool = False,
        clear_sms: bool = False,
    ) -> dict[str, Any]:
        """Set the saved agents that answer a number's calls and texts.

        ``number_id`` identifies an existing workspace number.
        ``voice_agent_id`` selects the agent for incoming calls.
        ``sms_agent_id`` selects the agent for incoming texts.
        ``clear_voice=true`` or ``clear_sms=true`` removes the corresponding agent.
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
        domain_id: str,
        email_agent_id: str | None = None,
        email_agent_scope: str | None = None,
        clear_agent: bool = False,
    ) -> dict[str, Any]:
        """Choose which agent answers mail to an address, and who it answers.

        ``email_agent_id`` makes the agent reply to inbound mail;
        ``clear_agent=True`` stops that. ``email_agent_scope`` is
        ``"members"`` (the default: verified members of the workspace whose
        mail passes DMARC) or ``"anyone"`` (every sender that passes the spam
        checks). With ``"anyone"``, a stranger's email can make the agent use
        its tools, so ask your user before you set it. A field left out keeps
        its value. ``domain_id`` comes from ``list_email_domains``.

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
        country_code: str,
        capabilities: list[str] | None = None,
        number_type: str | None = None,
        area_code: str | None = None,
        city: str | None = None,
        region: str | None = None,
    ) -> dict[str, Any]:
        """Compare available phone numbers and their prices without buying or charging.

        ``country_code`` is an ISO alpha-2 code, such as ``US``.
        ``capabilities`` selects voice, SMS or both. ``number_type`` is optional:
        local, mobile, national or toll_free.
        ``area_code`` is an area code or number prefix in national format.
        ``city`` is optional. ``region`` distinguishes US states or Canadian provinces.
        Offers include ``locality`` and ``region`` because city names can repeat.

        Returns ``offers``, ``recommended_quote_id`` and ``expires_at``.
        Each offer includes ``e164``, ``number_type``, ``capabilities``, ``quote_id``,
        ``monthly_cents``, ``setup_cents``, ``readiness``, ``requirements``,
        ``subject_types`` and ``verifications``. Prices are USD cents.
        Quotes expire after ten minutes. Expired quotes cannot authorize a purchase.
        ``recommended_quote_id`` identifies a ready offer or is null when none is ready.

        ``subject_types`` identifies eligible personal or business registrations.
        ``verifications`` contains accepted registrations, with approved entries first.
        Entries include ``id``, ``state``, ``subject_type`` and ``subject_name``.
        A purchase with several approved registrations requires an explicit ``verification_id``.
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
        quote_id: str,
        country_code: str,
        number_type: str,
        confirm_total_cents: int,
        idempotency_key: str | None = None,
        subject_type: str | None = None,
        verification_id: str | None = None,
    ) -> dict[str, Any]:
        """Buy a quoted phone number with explicit price confirmation.

        This charges the setup fee and first month immediately, then renews monthly.
        The purchase requires the user's authorization for that number and cost.
        ``quote_id``, ``country_code`` and ``number_type`` identify a current offer.
        ``confirm_total_cents`` must equal the offer's setup fee plus monthly fee.
        Expired or changed offers return 409 without authorizing a different price.
        Each quote can buy at most one number.

        A retry with the same quote and ``idempotency_key`` refers to the original purchase.
        After a timeout, a new quote starts a different purchase and can buy a second number.
        A returned retry key is available after a timeout or server error.
        A 4xx failure returns no retry key; that failed request cannot be reused.
        ``verification_id`` selects an approved registration accepted by the offer.
        It is required when several approved registrations are eligible.
        ``subject_type`` optionally limits eligible registrations to ``person`` or ``business``.
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
        title="List numbers",
        annotations=ToolAnnotations(
            title="List numbers",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def list_numbers_tool(
        ctx: Context,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """List dedicated workspace phone numbers, newest first.

        ``limit`` is 1 to 200, defaulting to 50.
        ``cursor`` accepts the previous page's ``next_cursor``.
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
        number_id: str,
    ) -> dict[str, Any]:
        """Read a workspace number's state, capabilities and assigned agents.

        ``number_id`` identifies the number.
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
        number_id: str,
        confirm_e164: str,
    ) -> dict[str, Any]:
        """Permanently release a phone number after explicit user authorization.

        ``confirm_e164`` must match the number's E.164 value.
        A mismatch returns an error without releasing anything.
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
        country_code: str,
        number_type: str,
        subject_type: str,
        quote_id: str | None = None,
        number_id: str | None = None,
        purpose: str = "number",
    ) -> dict[str, Any]:
        """Read a carrier's current registration fields, documents, address rules and fees.

        ``purpose`` is ``number`` or ``sms``. ``number`` is the default.
        ``country_code`` is an ISO alpha-2 code.
        ``number_type`` is local, mobile, national or toll_free.
        ``subject_type`` identifies the actual applicant: ``person`` or ``business``.
        The response's ``subject_types`` identifies the types the carrier accepts.
        For number registration, ``quote_id`` is required and selects the exact offer's form.

        US SMS registration uses ``purpose="sms"``, country ``US`` and type ``local``.
        ``number_id`` identifies an existing workspace US number when required by the carrier.
        Applicant country and business tax status determine the registration route.
        The personal route is available to US or Canadian applicants without a business tax number.
        SMS fees apply at submission, carrier review and monthly after approval.

        Returns ``required``, ``fields``, ``documents`` and ``address_required``.
        ``required=false`` means a registration submission is unnecessary and would fail.
        Fields and files require accurate information supplied by the authorized applicant.
        Choice fields accept comma-separated option keys within their ``min_choices`` and ``max_choices``.
        Field ``max_length`` uses UTF-16 code units; an emoji counts as two.
        Selected consent methods can require evidence even when shared fields are optional.
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
        country_code: str,
        number_type: str,
        fields: _JsonObject,
        documents: _JsonObject,
        files: _JsonObjectList,
        # Required, but checked inside check_verification: a missing
        # required argument makes FastMCP echo every argument, personal data
        # included, in its validation error.
        subject_type: str | None = None,
        address: _JsonObjectOrNone = None,
        quote_id: str | None = None,
        number_id: str | None = None,
        purpose: str = "number",
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
        # Untyped on purpose, same as submit_verification: FastMCP's schema
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
        country_code: str,
        number_type: str,
        fields: _JsonObject,
        documents: _JsonObject,
        files: _JsonObjectList,
        attest_authorized: StrictBool,
        # Required, but checked inside submit_verification: a missing
        # required argument makes FastMCP echo every argument, personal data
        # included, in its validation error.
        subject_type: str | None = None,
        address: _JsonObjectOrNone = None,
        quote_id: str | None = None,
        number_id: str | None = None,
        purpose: str = "number",
    ) -> dict[str, Any]:
        """Submit authorized applicant details and documents for carrier registration.

        Submission is irreversible once sent for review. Carriers retain submitted documents.
        ``attest_authorized=true`` attests that the named person or business authorized this submission
        and supplied the actual details and files for it. False sends nothing.
        ``subject_type`` identifies that person or business and must match the requirements.
        ``fields``, ``address``, ``documents`` and ``files`` follow the carrier's current requirements.

        ``purpose="number"`` requires the same ``quote_id`` as the requirements response.
        The balance must cover the offer's setup fee plus first month; otherwise nothing is sent.
        That amount is reserved during review and pays for a later purchase using this registration.
        It is returned after rejection, cancellation or 30 days without purchase following approval.
        ``purpose="sms"`` registers a US number for outbound US texting and charges carrier filing fees.
        The same ``number_id`` identifies the sender when it was required for the form.
        SMS campaign text requires user review and approval before submission.

        ``address`` contains customer_name, street, city, region, postal_code and country_code when required.
        ``documents`` maps each needed slot to its selected ``option`` and ``fields``.
        ``files`` has one entry per required file: ``{slot, upload_id}`` with the ``id``
        from ``create_file_upload`` (purpose ``verification_document``, uploaded within
        the last hour), or ``{slot, content_type, content_base64}`` for a very small file.
        With base64, every byte of the file is written into the tool call.
        Slot names match letters, digits, hyphens and underscores, up to 64 characters.
        Accepted files are JPEG, PNG or PDF: at most 10 MiB each and 20 files.
        Base64 files total at most 29 MiB, checked here; the API checks the 30 MB
        total of all files, uploads included.
        At most 100 fields are accepted. Base64 adds about one third to each file's size in the request.
        ``check_verification`` takes the same arguments and files nothing.

        Returns ``id``, ``state`` and ``subject_name``. Carrier review takes business days.
        A 409 identifies an existing registration; a timeout can leave a submission in progress.
        Carrier field errors include ``problems`` and leave no saved submission."""
        # ``fields``, ``documents``, ``files`` and ``address`` are untyped on
        # purpose: FastMCP's schema validation echoes a rejected value
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
        ctx: Context, purpose: str | None = None
    ) -> dict[str, Any]:
        """List workspace carrier registrations, newest first.

        ``purpose`` optionally selects ``number`` or ``sms`` registrations.
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
        ctx: Context, verification_id: str
    ) -> dict[str, Any]:
        """Read one carrier registration's current state and reported next step.

        ``verification_id`` identifies an existing registration.
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
        ctx: Context, verification_id: str
    ) -> dict[str, Any]:
        """Read saved answers for an existing workspace SMS registration.

        Returns private applicant fields, address and available sender information.
        ``verification_id`` identifies the original application, including one needing corrections.
        An older application without saved answers returns 404.
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
        subject_type: str,
        fields: _JsonObject,
        address: _JsonObjectOrNone = None,
        number_id: str | None = None,
    ) -> dict[str, Any]:
        """Preview the campaign text for a US SMS registration without filing or charging.

        ``subject_type``, ``fields``, ``address`` and ``number_id`` describe the proposed application.
        The response contains the description, consent flow and samples for user review before submission.
        Answers require actual applicant information and consent evidence.
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
        verification_id: str,
        fields: _JsonObject,
        attest_authorized: StrictBool,
        address: _JsonObjectOrNone = None,
    ) -> dict[str, Any]:
        """Correct an existing SMS registration at the needs_information step.

        ``verification_id`` identifies the same paid application and identity.
        ``fields`` contains complete corrected answers that satisfy the current requirements.
        The address must be accurate; saved details can be reused when still correct.
        Identity changes are rejected. This operation does not create a replacement brand.
        ``attest_authorized=true`` attests that the applicant reviewed and authorized the corrections.
        Actual consent evidence is required.
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
        ctx: Context, verification_id: str
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
        ctx: Context, verification_id: str, code: str
    ) -> dict[str, Any]:
        """Confirm a registration using the code the carrier sent to the applicant.

        ``verification_id`` identifies a registration with ``confirmation="code"``.
        ``code`` is the applicant-supplied code, containing 4 to 10 characters.
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
        agent_id: str,
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
        agent_id: str,
        name: str | None = None,
        system_prompt: str | None = None,
        first_message: str | None = None,
        ai_disclosure: bool | None = None,
        recording_enabled: bool | None = None,
        ai_disclosure_line: str | None = None,
        language: str | None = None,
        voice_id: str | None = None,
        tools: list[str] | None = None,
        max_duration_seconds: int | None = None,
        handover_max_duration_seconds: int | None = None,
        voice_enabled: bool | None = None,
        sms_enabled: bool | None = None,
        email_enabled: bool | None = None,
        status: str | None = None,
        handover_contacts: list[dict] | None = None,
        clear_fields: list[str] | None = None,
    ) -> dict[str, Any]:
        """Update selected fields on a saved workspace agent.

        Omitted fields retain their values. Repeating the same update gives the same result.
        ``status`` is ``live`` or ``paused``; a paused agent does not answer.
        ``language`` is a lowercase ISO 639-1 code.
        Changing only language or voice preserves the other value.
        Call and handover duration limits are 60 to 3600 seconds.
        ``handover_contacts`` replaces the entire list with up to ten entries; an empty list removes them.
        Each entry contains ``contact_id`` and a note of 1 to 200 characters.
        ``clear_fields`` explicitly resets first_message, ai_disclosure_line, tools,
        max_duration_seconds or handover_max_duration_seconds.
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
        agent_id: str,
        confirm_name: str,
    ) -> dict[str, Any]:
        """Permanently delete a saved agent after explicit user authorization.

        ``confirm_name`` must match the agent's saved name.
        A mismatch returns an error without deleting anything.
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
        q: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """List workspace members and manually saved contacts.

        ``q`` filters by name, email or phone. ``limit`` defaults to 100, with a maximum of 500.
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
    async def lookup_contact_tool(ctx: Context, query: str) -> dict[str, Any]:
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
        cursor: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """List the workspace's sending identities and default sender address.

        Returns ``items``, ``next_cursor`` and ``default_from``.
        Only identities with ``verification_status="verified"`` can send.
        For ``kind="custom"``, ``domain`` is a domain supporting any local part.
        For ``kind="hail_mail"``, it is already a complete email address.
        One verified custom domain takes priority over the Hail mail address.
        With several verified custom domains, ``default_from`` is null and email sending requires an explicit sender.
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
        name: str,
        phone_e164: str | None = None,
        email: str | None = None,
    ) -> dict[str, Any]:
        """Save a workspace contact with a name and a phone number or email address.

        At least one of ``phone_e164`` or ``email`` is required.
        Phone numbers use E.164 format. A duplicate phone or email returns 409.
        Returns the new contact record or an error."""
        try:
            async with _client_for(ctx, mode=mode, singleton=singleton) as client:
                return await create_contact(
                    client=client, name=name, phone_e164=phone_e164, email=email
                )
        except RuntimeError as exc:
            return {"error": str(exc)}
