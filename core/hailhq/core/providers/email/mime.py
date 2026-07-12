"""Shared raw-MIME builder for provider adapters."""

from __future__ import annotations

from email.message import EmailMessage

from hailhq.core.providers.email.base import ProviderAttachment

__all__ = ["build_raw_mime"]


def build_raw_mime(
    *,
    from_address: str,
    to_addresses: list[str],
    subject: str,
    body_text: str | None,
    body_html: str | None,
    cc: list[str] | None,
    bcc: list[str] | None = None,
    reply_to: str | None,
    headers: dict[str, str],
    attachments: list[ProviderAttachment],
) -> bytes:
    msg = EmailMessage()
    msg["From"] = from_address
    msg["To"] = ", ".join(to_addresses)
    if cc:
        msg["Cc"] = ", ".join(cc)
    # Gmail derives recipients from the MIME headers, so Bcc must be present
    # (Gmail strips it before delivery). SES call sites pass no bcc — SES
    # carries Bcc in the API Destination instead.
    if bcc:
        msg["Bcc"] = ", ".join(bcc)
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
