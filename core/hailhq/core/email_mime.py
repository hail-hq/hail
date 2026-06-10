"""Stdlib-`email` MIME parser tailored to inbound ingestion.

The parser keeps the API tight: a single ``parse_mime`` entry point
returns a ``ParsedMime`` dataclass with the fields the inbound pipeline
needs (envelope-like header derivatives, body parts, attachments as raw
bytes). Storing attachments to S3 is the caller's job.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from email import message_from_bytes, policy
from email.message import Message
from email.utils import getaddresses

__all__ = ["ParsedMime", "ParsedAttachment", "parse_mime"]


@dataclass
class ParsedAttachment:
    filename: str
    content_type: str
    payload: bytes
    content_id: str | None = None


@dataclass
class ParsedMime:
    from_address: str
    to_addresses: list[str]
    cc_addresses: list[str]
    subject: str
    message_id: str | None
    in_reply_to: str | None
    references_ids: list[str] | None
    body_text: str | None
    body_html: str | None
    attachments: list[ParsedAttachment] = field(default_factory=list)


def _addresses(msg: Message, header: str) -> list[str]:
    raw = msg.get_all(header) or []
    return [addr for _name, addr in getaddresses(raw) if addr]


def _references(msg: Message) -> list[str] | None:
    raw = msg.get("References")
    if not raw:
        return None
    parts = [p.strip() for p in raw.split() if p.strip()]
    return parts or None


def _safe_text(part: Message) -> str:
    """Decode a text part, tolerating bogus/unknown charsets."""
    try:
        return part.get_content()
    except (LookupError, UnicodeDecodeError, ValueError):
        raw = part.get_payload(decode=True) or b""
        charset = part.get_content_charset() or "utf-8"
        try:
            return raw.decode(charset, errors="replace")
        except LookupError:
            return raw.decode("utf-8", errors="replace")


def _collect(
    part: Message,
    text: list[str],
    html: list[str],
    atts: list[ParsedAttachment],
) -> None:
    ctype = part.get_content_type()
    if ctype == "message/rfc822":
        # Leaf: capture the embedded message as an attachment, do NOT
        # descend (walk() would otherwise attribute its body to the parent).
        # get_payload(decode=True) always returns None for message/rfc822
        # parts (they carry a Message object, not encoded bytes), so
        # serialize the embedded message directly.
        inner = part.get_payload()
        raw = inner[0].as_bytes() if isinstance(inner, list) and inner else b""
        atts.append(
            ParsedAttachment(
                filename=part.get_filename() or "message.eml",
                content_type="message/rfc822",
                payload=raw,
                content_id=(part.get("Content-ID") or "").strip("<>") or None,
            )
        )
        return
    if part.is_multipart():
        for child in part.get_payload():
            _collect(child, text, html, atts)
        return
    disp = (part.get_content_disposition() or "").lower()
    filename = part.get_filename()
    if disp == "attachment" or filename:
        atts.append(
            ParsedAttachment(
                filename=filename or "attachment",
                content_type=ctype,
                payload=part.get_payload(decode=True) or b"",
                content_id=(part.get("Content-ID") or "").strip("<>") or None,
            )
        )
        return
    if ctype == "text/plain":
        text.append(_safe_text(part))
    elif ctype == "text/html":
        html.append(_safe_text(part))


def _walk_bodies(
    msg: Message,
) -> tuple[str | None, str | None, list[ParsedAttachment]]:
    text: list[str] = []
    html: list[str] = []
    atts: list[ParsedAttachment] = []
    _collect(msg, text, html, atts)
    return (
        "\n".join(text) if text else None,
        "\n".join(html) if html else None,
        atts,
    )


def parse_mime(raw: bytes) -> ParsedMime:
    msg = message_from_bytes(raw, policy=policy.default)
    text, html, atts = _walk_bodies(msg)
    from_list = _addresses(msg, "From")
    return ParsedMime(
        from_address=from_list[0] if from_list else "",
        to_addresses=_addresses(msg, "To"),
        cc_addresses=_addresses(msg, "Cc"),
        subject=msg.get("Subject", ""),
        message_id=(msg.get("Message-ID") or msg.get("Message-Id")),
        in_reply_to=msg.get("In-Reply-To"),
        references_ids=_references(msg),
        body_text=text,
        body_html=html,
        attachments=atts,
    )
