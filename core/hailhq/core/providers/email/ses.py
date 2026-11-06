"""AWS SES (SESv2) implementation of the ``EmailProvider`` interface.

boto3 is sync-only; each SDK call is dropped into ``asyncio.to_thread`` so
this adapter exposes ``async def`` methods to FastAPI handlers without
blocking the event loop. Tests mock at the botocore boundary via
``botocore.stub.Stubber`` so SDK API drift surfaces as test failures.

The SESv2 surface is much cleaner than v1 for identity management:
``CreateEmailIdentity`` returns DKIM tokens in a single call, and
``GetEmailIdentity`` exposes per-identity verification status.
"""

from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor


import random
from email.headerregistry import Address
from email.message import EmailMessage
from typing import Any
from uuid import UUID

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from hailhq.core.config import settings
from hailhq.core.providers.email.base import (
    DkimRecord,
    EmailProvider,
    IdentityVerificationStatus,
    ProviderAttachment,
    ProviderIdentity,
    ProviderSendResult,
)

# Every SES call shares these timeouts. botocore's default of 5 attempts lets one
# send run for minutes. An agent's reply is resent once its queued row is older
# than _QUEUED_GRACE in routes/internal/agent.py, so a send must end first. The
# read timeout stays at 60 seconds for large attachment sends.
SES_CONNECT_TIMEOUT_SECONDS = 10
SES_READ_TIMEOUT_SECONDS = 60
SES_MAX_ATTEMPTS = 3
# Failures that prove SES did not accept the message, so a resend cannot deliver
# it twice. After anything else, such as a read timeout or a closed connection,
# SES may already have sent it.
SES_NOT_ACCEPTED_FAILURES = frozenset(
    {
        "ConnectTimeoutError",
        "EndpointConnectionError",
        "LimitExceededException",
        "TooManyRequestsException",
    }
)
# All attempts timing out, plus the two waits between them (at most 1 and 2 seconds).
SES_SEND_BUDGET_SECONDS = (
    SES_MAX_ATTEMPTS * (SES_CONNECT_TIMEOUT_SECONDS + SES_READ_TIMEOUT_SECONDS) + 3
)


def _build_default_client(*, max_attempts: int = SES_MAX_ATTEMPTS) -> Any:
    """Lazy-construct a SESv2 boto3 client from ``settings``.

    boto3 itself falls back to the AWS credential chain (env vars,
    ``~/.aws/credentials``, IAM role) when explicit keys are empty, so
    operators running on EC2/ECS get IAM-role creds automatically.
    """
    return boto3.client(
        "sesv2",
        region_name=settings.aws_region or None,
        aws_access_key_id=settings.aws_access_key_id or None,
        aws_secret_access_key=settings.aws_secret_access_key or None,
        config=Config(
            connect_timeout=SES_CONNECT_TIMEOUT_SECONDS,
            read_timeout=SES_READ_TIMEOUT_SECONDS,
            retries={"total_max_attempts": max_attempts, "mode": "standard"},
        ),
    )


def _default_aws_options() -> dict[str, str | None]:
    return {
        "region_name": settings.aws_region or None,
        "aws_access_key_id": settings.aws_access_key_id or None,
        "aws_secret_access_key": settings.aws_secret_access_key or None,
    }


# Credential providers can run a credential_process or wait on role endpoints.
# One dedicated worker and one shared future bound both running work and queue
# size, even after callers time out. Never use the application's executor here.
_credential_executor = ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="hail-aws-readiness"
)
_credential_lock = threading.Lock()
_credential_future: Future[bool] | None = None
_credential_expires_at = 0.0
_CREDENTIAL_WAIT_SECONDS = 2.0
_CREDENTIAL_SUCCESS_TTL = 30.0
_CREDENTIAL_FAILURE_TTL = 2.0


def _resolve_credentials(options: dict[str, str | None]) -> bool:
    try:
        session = boto3.Session(**options)
        if not session.region_name:
            return False
        credentials = session.get_credentials()
        if credentials is None:
            return False
        frozen = credentials.get_frozen_credentials()
        return bool(frozen.access_key and frozen.secret_key)
    except Exception:
        return False


def _credentials_completed(future: Future[bool]) -> None:
    global _credential_expires_at
    ttl = _CREDENTIAL_SUCCESS_TTL if future.result() else _CREDENTIAL_FAILURE_TTL
    with _credential_lock:
        if _credential_future is future:
            _credential_expires_at = time.monotonic() + ttl


async def credentials_ready() -> bool:
    """Share one bounded credential lookup without calling SES.

    A slow SDK call occupies only the dedicated worker. Timeout/cancellation
    never cancels its future or queues another lookup. Completed outcomes are
    briefly cached; failures can retry after two seconds.
    """
    global _credential_future, _credential_expires_at
    created = False
    with _credential_lock:
        if _credential_future is None or (
            _credential_future.done() and time.monotonic() >= _credential_expires_at
        ):
            _credential_future = _credential_executor.submit(
                _resolve_credentials, _default_aws_options()
            )
            _credential_expires_at = float("inf")
            created = True
        future = _credential_future
    # add_done_callback can run immediately: do this outside the lock.
    if created:
        future.add_done_callback(_credentials_completed)
    # Poll this single shared SDK future rather than attaching a new asyncio
    # wrapper per request. A hung provider therefore retains no timed-out
    # callers or callbacks. Each caller owns at most one short-lived timer.
    deadline = time.monotonic() + _CREDENTIAL_WAIT_SECONDS
    while not future.done():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(0.05, remaining))
    return future.result()


def _status_from_ses(raw: str | None) -> IdentityVerificationStatus:
    """Map SES ``VerificationStatus`` onto Hail's tri-state.

    SES values: SUCCESS, PENDING, FAILED, TEMPORARY_FAILURE, NOT_STARTED.
    Everything that isn't an outright FAILED is treated as still pending
    so a transient TEMPORARY_FAILURE doesn't permanently quarantine a
    domain — the operator can re-poll via POST /email-domains/{id}/verify.
    """
    if raw == "SUCCESS":
        return "verified"
    if raw == "FAILED":
        return "failed"
    return "pending"


def _dkim_records_for(domain: str, tokens: list[str]) -> list[DkimRecord]:
    """Render the three SES DKIM tokens as the CNAMEs to publish.

    Per SES docs the operator publishes
    ``<token>._domainkey.<domain> CNAME <token>.dkim.amazonses.com``.
    """
    return [
        DkimRecord(
            name=f"{token}._domainkey.{domain}",
            value=f"{token}.dkim.amazonses.com",
        )
        for token in tokens
    ]


def _mail_from_records(domain: str) -> list[DkimRecord]:
    """The two records for a custom MAIL FROM on ``send.<domain>``.

    The MX points at the region's SES feedback endpoint; the TXT is the SPF
    record authorising SES. Region-specific — unlike the DKIM CNAMEs.
    """
    mail_from = f"send.{domain}"
    region = settings.aws_region or "us-east-1"
    return [
        DkimRecord(
            name=mail_from,
            value=f"feedback-smtp.{region}.amazonses.com",
            type="MX",
            priority=10,
        ),
        DkimRecord(
            name=mail_from,
            value="v=spf1 include:amazonses.com ~all",
            type="TXT",
        ),
    ]


def _friendly_from(from_name: str | None, from_address: str) -> str:
    """Render ``"Name <addr>"`` as a single header-safe line.

    ``Address`` handles display-name quoting; folding the header through
    the default policy RFC-2047-encodes non-ASCII names AND splits them
    into <=75-char encoded words (RFC 2047 §2 — ``formataddr`` would emit
    one arbitrarily long word). Unfolding keeps it a single line for the
    SESv2 ``FromEmailAddress`` parameter. The raw-MIME path gets the same
    rendering by assigning ``Address`` directly (string round-trips are
    lossy for adjacent encoded words, so it can't share this output).
    """
    if not from_name:
        return from_address
    msg = EmailMessage()
    msg["From"] = Address(display_name=from_name, addr_spec=from_address)
    folded = msg["From"].fold(policy=msg.policy)  # "From: ...\n ..." folded
    return "".join(folded.splitlines()).removeprefix("From: ").strip()


def _build_raw_mime(
    *,
    from_address: str,
    from_name: str | None,
    to_addresses: list[str],
    subject: str,
    body_text: str | None,
    body_html: str | None,
    cc: list[str] | None,
    reply_to: str | None,
    headers: dict[str, str],
    attachments: list[ProviderAttachment],
) -> bytes:
    """Render a multipart MIME message for SES ``Content.Raw``.

    SESv2 Simple content can't carry attachments, so any send with files
    goes through this path instead.
    """
    msg = EmailMessage()
    # Same rendering as _friendly_from (Address + policy folding); assigned
    # as an Address object because re-parsing an encoded string is lossy.
    msg["From"] = (
        Address(display_name=from_name, addr_spec=from_address)
        if from_name
        else from_address
    )
    msg["To"] = ", ".join(to_addresses)
    if cc:
        msg["Cc"] = ", ".join(cc)
    if reply_to:
        msg["Reply-To"] = reply_to
    msg["Subject"] = subject
    for name, value in headers.items():
        if value:
            msg[name] = value
    if body_text is not None:
        msg.set_content(body_text)
        if body_html is not None:
            msg.add_alternative(body_html, subtype="html")
    elif body_html is not None:
        msg.set_content(body_html, subtype="html")
    for att in attachments:
        maintype, _, subtype = att.content_type.partition("/")
        msg.add_attachment(
            att.payload,
            maintype=maintype or "application",
            subtype=subtype or "octet-stream",
            filename=att.filename,
        )
    return msg.as_bytes()


def _feedback_id_tags(organization_id: UUID) -> list[dict[str, str]]:
    """The SES message tag that names the sending org in ``Feedback-ID``.

    SES puts the ``ses:feedback-id-a`` value in the first part of the
    ``Feedback-ID`` header it adds, which Gmail Postmaster Tools groups
    spam rate by. Tag values allow letters, digits, ``_`` and ``-``: a
    UUID's string form fits.
    """
    return [{"Name": "ses:feedback-id-a", "Value": str(organization_id)}]


class SesEmailProvider(EmailProvider):
    """SESv2-backed adapter."""

    def __init__(self, *, client: Any | None = None) -> None:
        self._client = client if client is not None else _build_default_client()
        # botocore would repeat a read timeout or a closed connection, and SES may
        # already have sent that message. _send repeats only what SES did not take.
        self._send_client = (
            client if client is not None else _build_default_client(max_attempts=1)
        )

    async def _send(self, *, at_most_once: bool, **kwargs: Any) -> dict[str, Any]:
        if not at_most_once:
            return await asyncio.to_thread(self._client.send_email, **kwargs)
        for attempt in range(SES_MAX_ATTEMPTS - 1):
            try:
                return await asyncio.to_thread(self._send_client.send_email, **kwargs)
            except (BotoCoreError, ClientError) as exc:
                if type(exc).__name__ not in SES_NOT_ACCEPTED_FAILURES:
                    raise
            await asyncio.sleep(random.random() * 2**attempt)
        return await asyncio.to_thread(self._send_client.send_email, **kwargs)

    async def send_email(
        self,
        *,
        from_address: str,
        from_name: str | None = None,
        to_addresses: list[str],
        subject: str,
        body_text: str | None,
        body_html: str | None,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        reply_to: str | None = None,
        headers: dict[str, str] | None = None,
        attachments: list[ProviderAttachment] | None = None,
        organization_id: UUID,
        at_most_once: bool = False,
    ) -> ProviderSendResult:
        if not to_addresses:
            raise ValueError("send_email requires at least one recipient")
        if body_text is None and body_html is None:
            raise ValueError("send_email requires body_text or body_html")

        destination: dict[str, list[str]] = {"ToAddresses": list(to_addresses)}
        if cc:
            destination["CcAddresses"] = list(cc)
        if bcc:
            destination["BccAddresses"] = list(bcc)

        body: dict[str, Any] = {}
        if body_text is not None:
            body["Text"] = {"Data": body_text, "Charset": "UTF-8"}
        if body_html is not None:
            body["Html"] = {"Data": body_html, "Charset": "UTF-8"}

        message: dict[str, Any] = {
            "Subject": {"Data": subject, "Charset": "UTF-8"},
            "Body": body,
        }

        # Set the same way on the raw and the simple path.
        shared: dict[str, Any] = {
            "Destination": destination,
            "EmailTags": _feedback_id_tags(organization_id),
        }
        if settings.hail_ses_configuration_set:
            shared["ConfigurationSetName"] = settings.hail_ses_configuration_set

        if attachments:
            # Serialization base64-encodes every attachment (pure-Python
            # CPU work, up to tens of MB) — keep it off the event loop.
            raw = await asyncio.to_thread(
                _build_raw_mime,
                from_address=from_address,
                from_name=from_name,
                to_addresses=to_addresses,
                subject=subject,
                body_text=body_text,
                body_html=body_html,
                cc=cc,
                reply_to=reply_to,
                headers={k: v for k, v in (headers or {}).items() if v},
                attachments=attachments,
            )
            response = await self._send(
                at_most_once=at_most_once,
                **shared,
                FromEmailAddress=from_address,
                Content={"Raw": {"Data": raw}},
            )
            return ProviderSendResult(provider_message_id=response["MessageId"])

        if headers:
            message["Headers"] = [
                {"Name": k, "Value": v} for k, v in headers.items() if v
            ]

        kwargs: dict[str, Any] = {
            **shared,
            # SESv2 accepts a friendly-from here ("Name <addr>");
            # _friendly_from quotes specials and RFC-2047-encodes non-ASCII.
            "FromEmailAddress": _friendly_from(from_name, from_address),
            "Content": {"Simple": message},
        }
        if reply_to:
            kwargs["ReplyToAddresses"] = [reply_to]

        response = await self._send(at_most_once=at_most_once, **kwargs)
        return ProviderSendResult(provider_message_id=response["MessageId"])

    async def create_identity(self, domain: str) -> ProviderIdentity:
        response = await asyncio.to_thread(
            self._client.create_email_identity,
            EmailIdentity=domain,
        )
        tokens: list[str] = (response.get("DkimAttributes") or {}).get("Tokens") or []

        mail_from = f"send.{domain}"
        # Configure a custom MAIL FROM so the Return-Path aligns to the
        # customer's domain (no "via amazonses.com"). USE_DEFAULT_VALUE keeps
        # sending working while the MX/SPF DNS is still propagating.
        await asyncio.to_thread(
            self._client.put_email_identity_mail_from_attributes,
            EmailIdentity=domain,
            MailFromDomain=mail_from,
            BehaviorOnMxFailure="USE_DEFAULT_VALUE",
        )

        records = _dkim_records_for(domain, tokens) + _mail_from_records(domain)
        return ProviderIdentity(
            domain=domain,
            verification_status="pending",
            dkim_records=records,
            mail_from_domain=mail_from,
            mail_from_status="pending",
            provider_resource_id=domain,
        )

    async def get_identity(self, domain: str) -> ProviderIdentity:
        try:
            response = await asyncio.to_thread(
                self._client.get_email_identity,
                EmailIdentity=domain,
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code == "NotFoundException":
                raise LookupError(f"SES identity {domain!r} not found") from exc
            raise

        dkim = response.get("DkimAttributes") or {}
        tokens: list[str] = dkim.get("Tokens") or []
        status = _status_from_ses(
            dkim.get("Status") or response.get("VerificationStatus")
        )

        mail_from_attrs = response.get("MailFromAttributes") or {}
        mail_from = mail_from_attrs.get("MailFromDomain")
        mail_from_status = (
            _status_from_ses(mail_from_attrs.get("MailFromDomainStatus"))
            if mail_from
            else None
        )

        records = _dkim_records_for(domain, tokens)
        if mail_from:
            records = records + _mail_from_records(domain)

        return ProviderIdentity(
            domain=domain,
            verification_status=status,
            dkim_records=records,
            mail_from_domain=mail_from,
            mail_from_status=mail_from_status,
            provider_resource_id=domain,
        )

    async def delete_identity(self, domain: str) -> None:
        try:
            await asyncio.to_thread(
                self._client.delete_email_identity,
                EmailIdentity=domain,
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            # Already gone — keep the API delete handler idempotent.
            if code == "NotFoundException":
                return
            raise
