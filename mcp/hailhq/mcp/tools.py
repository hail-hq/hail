"""MCP tool surface for Hail's outbound-call API.

Exposes eighteen tools to the calling agent:

* ``place_call`` — originate an outbound phone call
* ``get_call`` — fetch the current state of one call
* ``list_calls`` — page through recent calls
* ``get_events`` — page through the event stream (call-narrow or org-wide)
* ``send_email`` — send an outbound email
* ``upload_email_attachment`` — upload a file to attach to an outbound email
* ``get_email`` — fetch the full record of one email
* ``list_emails`` — page through emails (``direction="inbound"`` for replies)
* ``get_email_raw`` — presigned URL for an inbound email's raw MIME
* ``get_email_attachment`` — presigned URL for one inbound attachment
* ``get_email_events`` — delivery/engagement timeline for one email
* ``get_email_stats`` — account-level deliverability stats (counts, rates, series)
* ``send_sms`` — send an outbound SMS
* ``get_sms`` — fetch the current state of one SMS
* ``list_sms`` — page through recent SMS messages
* ``list_contacts`` — page through the workspace's contacts (members + manual)
* ``lookup_contact`` — find a contact by name/email/phone fragment
* ``create_contact`` — save a manual contact (phone and/or email)

The tool docstrings are the agent's only documentation, so each one
spells out the contract (required vs optional fields, mutually exclusive
modes, example invocation, terminal-status loop hint).

Errors are returned as ``{"error": "<message>"}`` dicts rather than
raised — agents read tool responses, not exception traces. Field and
shape validation comes from the shared ``hailhq.core.schemas`` request
models (``CallCreate``, ``EmailCreate``, ``SmsCreate``) constructed
inside ``hail_client``; a ``pydantic.ValidationError`` is caught and
mapped to an ``{"error": ...}`` dict. Only the ``<type>:<uuid>``
resource-id shape for ``get_events`` is still checked locally via
``parse_resource_id``.

The fifteen tool functions are kept module-importable so unit tests can
call them directly with a constructed ``HailClient``; ``register_tools``
is the FastMCP wiring step. Each registered tool closure accepts a
FastMCP ``Context`` (auto-injected on dispatch) and uses the
``_client_for`` async context manager to obtain a per-call ``HailClient``
(oauth-rs mode, built from the inbound bearer) or the shared singleton
(static-key mode).
"""

from __future__ import annotations

import base64
import contextlib
import uuid
from collections.abc import AsyncIterator
from typing import Any

from hailhq.core.schemas import parse_resource_id
from hailhq.mcp.auth import AuthMode
from hailhq.mcp.hail_client import HailAPIError, HailClient
from pydantic import ValidationError

from mcp.server.fastmcp import Context, FastMCP

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
    if status in (409, 422, 503):
        return {"error": exc.detail}
    if 500 <= status < 600:
        return {"error": f"hail upstream error: {status}"}
    return {"error": f"hail api error {status}: {exc.detail}"}


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
    metadata: dict[str, Any] | None = None,
    tools: list[str] | None = None,
    idempotency_key: str | None = None,
    consent_source: str | None = None,
    consent_obtained_at: str | None = None,
    message_type: str = "informational",
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
            metadata=metadata,
            tools=tools,
            idempotency_key=idempotency_key,
            consent_source=consent_source,
            consent_obtained_at=consent_obtained_at,
            message_type=message_type,
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
    except HailAPIError as exc:
        return _format_api_error(exc)


async def get_email_attachment(
    *, client: HailClient, email_id: str, attachment_id: str
) -> dict[str, Any]:
    try:
        return await client.get_email_attachment(email_id, attachment_id)
    except ValidationError as exc:
        return {"error": _validation_error_message(exc)}
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
            yield client
        finally:
            await client.aclose()
        return

    # static-key
    if singleton is None:  # defensive — server.py wires this
        raise RuntimeError("static-key mode requires a singleton HailClient")
    yield singleton


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
    """Register the eighteen Hail tools on a FastMCP app.

    Tools accept a FastMCP ``Context`` parameter (auto-injected). The
    ``_client_for`` helper picks the right HailClient for the active mode
    — per-tool-call in oauth-rs, shared singleton in static-key.
    """

    @mcp_app.tool(name="place_call")
    async def place_call_tool(
        ctx: Context,
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
    ) -> dict[str, Any]:
        """Originate an outbound phone call.

        Provide either ``system_prompt`` (mode A — Hail's bundled
        fallback LLM uses this prompt) or ``llm`` (mode B — bring your
        own OpenAI-compatible endpoint as
        ``{"base_url": ..., "api_key": ..., "model": ...}``).
        Mode A and mode B are mutually exclusive; supply exactly one.

        ``to`` must be E.164 (e.g. ``+14155551234``). ``from_`` is
        optional and defaults to the first active number on your org.
        ``first_message`` is spoken verbatim on pickup; omit it to let
        the agent open the conversation itself — it reacts to how the
        call was answered, or introduces itself after silence.
        ``language`` sets the call's spoken language for both
        speech-to-text and text-to-speech, as a lowercase ISO 639-1 code
        (e.g. ``"fr"``); omit for English.
        ``ai_disclosure=False`` skips the spoken "this is an AI
        assistant" line at the start of the call. Leave enabled unless
        the user has verified it is not required for this call — US
        artificial-voice calls (47 CFR 64.1200(b)(1)) and several AI
        bot-disclosure laws require it, and Hail does not verify this.
        The agent still identifies itself as an AI if asked.
        ``metadata`` is free-form JSON attached to the call record.
        ``tools`` are the agent tools to allow on this call. Omit for all
        available; pass ``[]`` to disable.

        ``recipient_consent`` is required: attest that you (the caller
        triggering this request) have obtained the lawful consent needed
        to contact this recipient. The API rejects the request (422) if
        this is not ``true`` — Hail does not verify consent for you, you
        are responsible for having a lawful basis (TCPA / ePrivacy / GDPR
        as applicable). Set ``message_type="marketing"`` for promotional
        calls (this additionally requires a non-empty ``consent_source``
        describing how/where consent was obtained) — leave as the default
        ``"informational"`` for transactional/service calls.

        ``idempotency_key`` defaults to a fresh UUID per invocation
        and is returned in the response under ``idempotency_key`` — to
        retry *this* exact request (rather than dispatch a second call),
        pass the value back on the retry. A new key is a new call.

        Example:
            place_call(to="+14155551234",
                       recipient_consent=True,
                       system_prompt="You are scheduling a haircut.",
                       first_message="Hi, I'm calling on behalf of Alex.")

        Returns the API's ``CallResponse`` as a dict (id, status,
        from_e164, to_e164, ...). On failure returns
        ``{"error": "<message>"}`` instead.
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
                    metadata=metadata,
                    tools=tools,
                    idempotency_key=idempotency_key,
                    consent_source=consent_source,
                    consent_obtained_at=consent_obtained_at,
                    message_type=message_type,
                )
        except RuntimeError as exc:
            return {"error": str(exc)}

    @mcp_app.tool(name="send_email")
    async def send_email_tool(
        ctx: Context,
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
        """Send an outbound email through your configured SES sender.

        ``to`` is a non-empty list of RFC-style email addresses. At
        least one of ``body_text`` / ``body_html`` is required (both
        is fine — multipart-alternative). ``cc``, ``bcc``, and
        ``reply_to`` are optional and follow the usual mail
        conventions.

        ``recipient_consent`` is required: attest that you (the caller
        triggering this request) have obtained the lawful consent needed
        to email this recipient. The API rejects the request (422) if
        this is not ``true`` — Hail does not verify consent for you. Set
        ``message_type="marketing"`` for promotional email (this
        additionally requires a non-empty ``consent_source`` describing
        how/where consent was obtained) — leave as the default
        ``"informational"`` for transactional/service email.

        ``from_`` is optional. When omitted, Hail picks the first
        verified sender domain on your organization, or — if the
        operator configured ``HAIL_MAIL_BASE_DOMAIN`` — auto-mints a
        per-org hail-mail address of the form ``<user>+<org>@<base>``
        (the ``<org>`` part is derived from your organization id; the
        ``<user>`` part comes from ``HAIL_MAIL_FROM`` /
        ``HAIL_MAIL_DEFAULT_USER_PREFIX``, or an explicit row created
        via ``POST /email-domains``). When supplied, it must
        match a verified row already in ``email_domains`` (register
        one with the website console or ``POST /email-domains``).

        ``metadata`` is free-form JSON attached to the email record.

        ``idempotency_key`` defaults to a fresh UUID and is returned
        in the response under ``idempotency_key`` — pass the same
        value on a retry to replay rather than re-send. A new key
        is a new message.

        ``attachment_ids`` are ids returned by ``upload_email_attachment``
        — upload a file first, then pass its id(s) here to attach it.

        Example:
            send_email(to=["alice@example.com"],
                       subject="Welcome",
                       recipient_consent=True,
                       body_text="Thanks for signing up.")

        Returns the ``EmailResponse`` dict (id, status, from_address,
        to_addresses, sent_at, provider_message_id, ...). On failure
        returns ``{"error": "<message>"}`` instead.
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

    @mcp_app.tool(name="upload_email_attachment")
    async def upload_email_attachment_tool(
        ctx: Context,
        content_base64: str,
        filename: str,
        content_type: str,
    ) -> dict[str, Any]:
        """Upload a file to attach to a future outbound email.

        ``content_base64`` is the file's raw bytes, base64-encoded.
        Returns ``{"id": ..., "filename": ..., "content_type": ...,
        "size_bytes": ...}`` — pass ``id`` in ``send_email``'s
        ``attachment_ids`` list. The id is reusable across many sends
        and expires in 24h if never used. Files over 10MB (combined
        with the message body and any other attachments, per send) are
        rejected — host large files externally and link to them in the
        body instead.
        """
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

    @mcp_app.tool(name="get_call")
    async def get_call_tool(ctx: Context, call_id: str) -> dict[str, Any]:
        """Fetch the current state of one call by id.

        Use this after ``place_call`` (or to check on any prior call)
        to read the call's latest ``status`` and timing fields.

        Returns the API's ``CallResponse`` as a dict, or
        ``{"error": "call not found"}`` for an unknown id.
        """
        try:
            async with _client_for(ctx, mode=mode, singleton=singleton) as client:
                return await get_call(client=client, call_id=call_id)
        except RuntimeError as exc:
            return {"error": str(exc)}

    @mcp_app.tool(name="list_calls")
    async def list_calls_tool(
        ctx: Context,
        cursor: str | None = None,
        limit: int = 50,
        status: str | None = None,
        to: str | None = None,
    ) -> dict[str, Any]:
        """List recent calls in your organization, newest first.

        Cursor-paginated: pass the previous response's ``next_cursor``
        to fetch the next page. ``status`` (one of queued, dialing,
        ringing, in_progress, completed, failed, busy, no_answer,
        canceled) and ``to`` (E.164) are optional server-side filters.

        Returns a dict ``{"items": [...], "next_cursor": <str|None>}``.
        """
        try:
            async with _client_for(ctx, mode=mode, singleton=singleton) as client:
                return await list_calls(
                    client=client, cursor=cursor, limit=limit, status=status, to=to
                )
        except RuntimeError as exc:
            return {"error": str(exc)}

    @mcp_app.tool(name="send_sms")
    async def send_sms_tool(
        ctx: Context,
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
        """Send an outbound SMS from your organization's dedicated number.

        ``to`` must be E.164 (e.g. ``+14155551234``). ``body`` is the
        message text. SMS requires a dedicated phone number on your
        organization — it does not use the shared voice-call pool.

        ``recipient_consent`` is required: attest that you (the caller
        triggering this request) have obtained the lawful consent needed
        to text this recipient. The API rejects the request (422) if
        this is not ``true`` — Hail does not verify consent for you. Set
        ``message_type="marketing"`` for promotional texts (this
        additionally requires a non-empty ``consent_source``) — leave as
        the default ``"informational"`` for transactional/service texts.

        ``idempotency_key`` defaults to a fresh UUID and is returned in
        the response under ``idempotency_key`` — pass the same value on
        a retry to replay rather than re-send.

        Example:
            send_sms(to="+14155551234", body="Your order shipped!",
                     recipient_consent=True)

        Returns the ``SmsResponse`` dict (id, status, from_e164,
        to_e164, segment_count, ...). On failure returns
        ``{"error": "<message>"}`` instead.
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

    @mcp_app.tool(name="get_sms")
    async def get_sms_tool(ctx: Context, sms_id: str) -> dict[str, Any]:
        """Fetch the current state of one SMS by id.

        Use this after ``send_sms`` to check delivery status.

        Example:
            get_sms(sms_id="...")
        """
        try:
            async with _client_for(ctx, mode=mode, singleton=singleton) as client:
                return await get_sms(client=client, sms_id=sms_id)
        except RuntimeError as exc:
            return {"error": str(exc)}

    @mcp_app.tool(name="list_sms")
    async def list_sms_tool(
        ctx: Context,
        cursor: str | None = None,
        limit: int = 50,
        status: str | None = None,
        to: str | None = None,
    ) -> dict[str, Any]:
        """Page through recent SMS messages for your organization.

        ``status`` filters to one of: queued, sent, delivered, failed,
        undelivered, received. ``to`` filters to messages sent to a
        specific E.164 number. Paginate with the returned
        ``next_cursor``.
        """
        try:
            async with _client_for(ctx, mode=mode, singleton=singleton) as client:
                return await list_sms(
                    client=client, cursor=cursor, limit=limit, status=status, to=to
                )
        except RuntimeError as exc:
            return {"error": str(exc)}

    @mcp_app.tool(name="get_email")
    async def get_email_tool(ctx: Context, email_id: str) -> dict[str, Any]:
        """Fetch the full record of one email by id.

        Returns the complete row — including ``body_text`` / ``body_html``
        and (for inbound mail) the ``in_reply_to`` / ``message_id`` headers
        and ``spam``/``virus``/``spf``/``dkim``/``dmarc`` verdicts. Use this
        after ``list_emails`` to read a received reply's body.

        Returns the API's ``EmailResponse`` as a dict, or
        ``{"error": "resource not found"}`` for an unknown id.
        """
        try:
            async with _client_for(ctx, mode=mode, singleton=singleton) as client:
                return await get_email(client=client, email_id=email_id)
        except RuntimeError as exc:
            return {"error": str(exc)}

    @mcp_app.tool(name="list_emails")
    async def list_emails_tool(
        ctx: Context,
        cursor: str | None = None,
        limit: int = 50,
        status: str | None = None,
        direction: str | None = None,
    ) -> dict[str, Any]:
        """List emails in your organization, newest first.

        Cursor-paginated: pass the previous response's ``next_cursor`` to
        fetch the next page. Two optional server-side filters:

        * ``direction`` — ``outbound`` or ``inbound``. Pass
          ``direction="inbound"`` to read replies and other received mail.
        * ``status`` — one of ``queued``, ``sent``, ``failed``,
          ``bounced``, ``complained``, ``received``.

        Items are trimmed summaries (no message body). Call ``get_email``
        with an item's ``id`` to read the full body.

        Example:
            list_emails(direction="inbound")

        Returns ``{"items": [...], "next_cursor": <str|None>}`` on success,
        or ``{"error": "<message>"}`` instead.
        """
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

    @mcp_app.tool(name="get_email_raw")
    async def get_email_raw_tool(ctx: Context, email_id: str) -> dict[str, Any]:
        """Get a fetchable URL for an email's original MIME source.

        Returns ``{"url": "<presigned-s3-url>"}`` — a short-lived
        (~5 minute) link to the full raw RFC822 message. Fetch the URL
        directly to read the complete original; it needs no auth header.
        Raw source exists for **inbound** mail only — outbound ids return
        ``{"error": "resource not found"}``.
        """
        try:
            async with _client_for(ctx, mode=mode, singleton=singleton) as client:
                return await get_email_raw(client=client, email_id=email_id)
        except RuntimeError as exc:
            return {"error": str(exc)}

    @mcp_app.tool(name="get_email_attachment")
    async def get_email_attachment_tool(
        ctx: Context, email_id: str, attachment_id: str
    ) -> dict[str, Any]:
        """Get a fetchable URL for one inbound email attachment.

        ``attachment_id`` comes from an item in ``get_email``'s
        ``attachments`` list. Returns ``{"url": "<presigned-s3-url>"}`` —
        a short-lived (~5 minute) link to the attachment bytes, fetchable
        directly with no auth header. Unknown ids return
        ``{"error": "resource not found"}``.
        """
        try:
            async with _client_for(ctx, mode=mode, singleton=singleton) as client:
                return await get_email_attachment(
                    client=client, email_id=email_id, attachment_id=attachment_id
                )
        except RuntimeError as exc:
            return {"error": str(exc)}

    @mcp_app.tool(name="get_email_events")
    async def get_email_events_tool(
        ctx: Context,
        email_id: str,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Delivery/engagement timeline (sent→delivered→opened…) for one email.

        Chronological lifecycle events for a single email — use this to see
        exactly what happened to one message (bounced? opened? clicked?)
        rather than the account-wide aggregates ``get_email_stats`` returns.

        Returns ``{"items": [...], "next_cursor": ...}`` where each item has
        ``kind`` (one of sent, delivered, delivery_delayed, bounced,
        complained, rejected, opened, clicked), ``payload``, and
        ``occurred_at``. Pass ``cursor`` from a previous ``next_cursor`` to
        page (``limit`` 1..1000, default 100). On failure returns
        ``{"error": "resource not found"}`` for an unknown id.
        """
        try:
            async with _client_for(ctx, mode=mode, singleton=singleton) as client:
                return await get_email_events(
                    client=client, email_id=email_id, cursor=cursor, limit=limit
                )
        except RuntimeError as exc:
            return {"error": str(exc)}

    @mcp_app.tool(name="get_email_stats")
    async def get_email_stats_tool(
        ctx: Context,
        from_: str | None = None,
        to: str | None = None,
        bucket: str = "day",
    ) -> dict[str, Any]:
        """Account-level email deliverability stats (counts, rates, time series).

        Aggregates across your whole organization's outbound mail over a
        window — use this for "how's deliverability doing" rather than
        one message's history (``get_email_events`` covers that).

        ``from_`` / ``to`` are ISO 8601 timestamps (defaults: last 7 days
        ending now). ``bucket`` is ``"day"`` (default) or ``"hour"`` —
        ``"hour"`` is limited to an 8-day span; any range is capped at 92
        days.

        Returns ``{"from": ..., "to": ..., "bucket": ..., "totals": {...},
        "rates": {...}, "series": [...]}`` — ``totals``/each ``series``
        bucket carry counts (sent, delivered, bounced, opened, ...) and
        ``rates`` carries derived ratios (delivery, bounce, open, click),
        each ``None`` when ``totals.sent`` is 0. On failure returns
        ``{"error": "<message>"}`` instead.
        """
        try:
            async with _client_for(ctx, mode=mode, singleton=singleton) as client:
                return await get_email_stats(
                    client=client, from_=from_, to=to, bucket=bucket
                )
        except RuntimeError as exc:
            return {"error": str(exc)}

    @mcp_app.tool(name="get_events")
    async def get_events_tool(
        ctx: Context,
        id: str | None = None,
        kind: str | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Page through events from across the org or one resource.

        Pass ``id="<type>:<uuid>"`` to narrow to a single resource —
        supported types are ``call``, ``email``, and ``sms`` (e.g.
        ``id="sms:<uuid>"`` after ``send_sms``). When narrowed to a
        call, the response includes a ``call_status`` field reflecting
        the call's current state. Without ``id``, returns events from
        across the whole org. ``kind`` filters server-side by event kind
        (``state_change``, ``agent_turn``, ``user_turn``, ``tool_call``,
        ``error``, ...).

        This is **not** a streaming subscription — the call returns
        whatever events exist now plus a ``next_cursor`` if more pages
        remain. To follow a call to completion, loop: pass the previous
        response's ``next_cursor`` until ``next_cursor`` is null and,
        when narrowed to a call, ``call_status`` is one of
        ``completed``, ``failed``, ``busy``, ``no_answer``, ``canceled``
        (the terminal set).

        Example:
            get_events(id="call:0c2f...-...", limit=200)

        Returns ``{"items": [...], "next_cursor": <str|None>,
        "call_status": <str|None>}`` on success, or
        ``{"error": ...}`` on a malformed ``id`` or upstream failure.
        """
        try:
            async with _client_for(ctx, mode=mode, singleton=singleton) as client:
                return await get_events(
                    client=client, id=id, kind=kind, cursor=cursor, limit=limit
                )
        except RuntimeError as exc:
            return {"error": str(exc)}

    @mcp_app.tool(name="list_contacts")
    async def list_contacts_tool(
        ctx: Context,
        q: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """List the workspace's contacts: org members (with their phone/email)
        plus manually saved contacts. Use ``lookup_contact`` for name searches.

        ``q`` optionally filters server-side (matches name/email/phone).
        ``limit`` caps the page (server default 100, max 500).

        Returns ``{"items": [{"id", "kind", "name", "phone_e164", "email",
        "role"}, ...]}`` — ``kind`` is ``"member"`` (id ``member:<user_id>``,
        ``role`` set) or ``"manual"`` (id is the contact's UUID, ``role``
        null).
        """
        try:
            async with _client_for(ctx, mode=mode, singleton=singleton) as client:
                return await list_contacts(client=client, q=q, limit=limit)
        except RuntimeError as exc:
            return {"error": str(exc)}

    @mcp_app.tool(name="lookup_contact")
    async def lookup_contact_tool(ctx: Context, query: str) -> dict[str, Any]:
        """Find a contact by name, email, or phone fragment. Resolve a person
        to their ``phone_e164``/``email`` BEFORE calling ``place_call``,
        ``send_sms``, or ``send_email`` — do not guess a contact's number.

        Returns up to 10 matches, same item shape as ``list_contacts``.

        Example:
            lookup_contact(query="maya")
        """
        try:
            async with _client_for(ctx, mode=mode, singleton=singleton) as client:
                return await lookup_contact(client=client, query=query)
        except RuntimeError as exc:
            return {"error": str(exc)}

    @mcp_app.tool(name="create_contact")
    async def create_contact_tool(
        ctx: Context,
        name: str,
        phone_e164: str | None = None,
        email: str | None = None,
    ) -> dict[str, Any]:
        """Save a new contact for the workspace. Provide at least one of
        ``phone_e164`` (E.164, e.g. ``+14155551234``) or ``email`` — the API
        rejects (422) a contact with neither. A duplicate phone or email on
        an existing contact returns 409.

        Example:
            create_contact(name="Maya Chen", phone_e164="+14155551234")

        Returns the created contact entry as a dict. On failure returns
        ``{"error": "<message>"}`` instead.
        """
        try:
            async with _client_for(ctx, mode=mode, singleton=singleton) as client:
                return await create_contact(
                    client=client, name=name, phone_e164=phone_e164, email=email
                )
        except RuntimeError as exc:
            return {"error": str(exc)}


__all__ = [
    "create_contact",
    "get_call",
    "get_email",
    "get_email_attachment",
    "get_email_events",
    "get_email_raw",
    "get_email_stats",
    "get_events",
    "get_sms",
    "list_calls",
    "list_contacts",
    "list_emails",
    "list_sms",
    "lookup_contact",
    "place_call",
    "register_tools",
    "send_email",
    "send_sms",
]
