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
from typing import Any

import boto3
from botocore.exceptions import ClientError

from hailhq.core.config import settings
from hailhq.core.providers.email.base import (
    DkimRecord,
    EmailProvider,
    IdentityVerificationStatus,
    ProviderIdentity,
    ProviderSendResult,
)


def _build_default_client() -> Any:
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
    )


def _status_from_ses(raw: str | None) -> IdentityVerificationStatus:
    """Map SES ``VerificationStatus`` onto Hail's tri-state.

    SES values: SUCCESS, PENDING, FAILED, TEMPORARY_FAILURE, NOT_STARTED.
    Everything that isn't an outright FAILED is treated as still pending
    so a transient TEMPORARY_FAILURE doesn't permanently quarantine a
    domain — the operator can re-poll via POST /sender-domains/{id}/verify.
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


class SesEmailProvider(EmailProvider):
    """SESv2-backed adapter."""

    def __init__(self, *, client: Any | None = None) -> None:
        self._client = client if client is not None else _build_default_client()

    async def send_email(
        self,
        *,
        from_address: str,
        to_addresses: list[str],
        subject: str,
        body_text: str | None,
        body_html: str | None,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        reply_to: str | None = None,
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

        message = {
            "Subject": {"Data": subject, "Charset": "UTF-8"},
            "Body": body,
        }

        kwargs: dict[str, Any] = {
            "FromEmailAddress": from_address,
            "Destination": destination,
            "Content": {"Simple": message},
        }
        if reply_to:
            kwargs["ReplyToAddresses"] = [reply_to]

        response = await asyncio.to_thread(self._client.send_email, **kwargs)
        return ProviderSendResult(provider_message_id=response["MessageId"])

    async def create_identity(self, domain: str) -> ProviderIdentity:
        response = await asyncio.to_thread(
            self._client.create_email_identity,
            EmailIdentity=domain,
        )
        tokens: list[str] = (response.get("DkimAttributes") or {}).get("Tokens") or []
        # CreateEmailIdentity returns DKIM tokens immediately but never a
        # SUCCESS status — the domain is always pending until the operator
        # publishes the CNAMEs and SES re-checks. Surface that explicitly.
        return ProviderIdentity(
            domain=domain,
            verification_status="pending",
            dkim_records=_dkim_records_for(domain, tokens),
            mail_from_domain=None,
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

        mail_from = (response.get("MailFromAttributes") or {}).get("MailFromDomain")

        return ProviderIdentity(
            domain=domain,
            verification_status=status,
            dkim_records=_dkim_records_for(domain, tokens),
            mail_from_domain=mail_from,
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
