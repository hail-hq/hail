"""Header-rewrite forwarding builder + loop guards.

Forwarded mail is **sent as Hail** with ``Reply-To:`` carrying the
original sender. This keeps SPF and DKIM aligned (the envelope sender
is Hail's domain, so Hail's DKIM signature stands and SPF passes)
without the SRS-rewrite complexity. The original ``Message-ID`` is
preserved in ``References:`` to keep threading intact for the forward
target's reply.

Loop guards are deliberately conservative:
- Hop counter via ``X-Hail-Forward-Hops`` header; refuses at the cap.
- Never forward to an address on the hail-mail base domain — that
  would loop back into ingest immediately.

See docs/superpowers/specs/2026-06-06-inbound-email-design.md §6.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from uuid import UUID

from hailhq.core.email_footer import append_forwarded_footer
from hailhq.core.email_mime import ParsedMime

__all__ = ["Forwarded", "LoopDetected", "build_forwarded", "detect_loop"]

_CRLF_RE = re.compile(r"[\r\n]+")


def _header_safe(value: str) -> str:
    """Collapse CR/LF — RFC2047-decoded inbound headers can smuggle them, and
    both stdlib EmailMessage and SES reject such values, killing the forward."""
    return _CRLF_RE.sub(" ", value)


class LoopDetected(RuntimeError):
    """The forward would loop or exceed the hop cap.

    Attributes:
        cause: ``"hop_cap"`` when the hop counter reached the maximum (global
            limit — abort all remaining targets); ``"base_domain"`` when the
            specific target's domain matches the hail-mail base domain (per-
            target — skip only this target, continue with siblings).
    """

    def __init__(self, cause: str) -> None:
        super().__init__(cause)
        self.cause = cause


@dataclass
class Forwarded:
    from_address: str
    to_addresses: list[str]
    reply_to: str
    subject: str
    body_text: str | None
    body_html: str | None
    headers: dict[str, str] = field(default_factory=dict)


_FWD_PREFIXES = ("fwd:", "fw:")


def _subject_with_prefix(subject: str) -> str:
    s = (subject or "").strip()
    if any(s.lower().startswith(p) for p in _FWD_PREFIXES):
        return s
    return f"Fwd: {s}".strip()


def _preamble(parsed: ParsedMime) -> str:
    lines = ["---------- Forwarded message ----------"]
    if parsed.from_address:
        lines.append(f"From: {parsed.from_address}")
    if parsed.subject:
        lines.append(f"Subject: {parsed.subject}")
    if parsed.to_addresses:
        lines.append(f"To: {', '.join(parsed.to_addresses)}")
    lines.append("")
    return "\n".join(lines) + "\n"


def build_forwarded(
    *,
    parsed: ParsedMime,
    target: str,
    forwarder_address: str,
    inbound_id: UUID,
    hops: int,
) -> Forwarded:
    new_subject = _subject_with_prefix(_header_safe(parsed.subject or ""))
    preamble = _preamble(parsed)
    body_text = None
    if parsed.body_text is not None:
        body_text = preamble + parsed.body_text
    body_html = None
    if parsed.body_html is not None:
        body_html = (
            "<div>" + preamble.replace("\n", "<br>") + "</div>" + parsed.body_html
        )
    if body_text is None and body_html is None:
        # Attachment-only original — the preamble alone satisfies the
        # emails_body_required CHECK on the queued outbound row.
        body_text = preamble
    body_text, body_html = append_forwarded_footer(body_text, body_html)
    headers = {
        "X-Hail-Forwarded-From": _header_safe(parsed.from_address or ""),
        "X-Hail-Original-Message-Id": _header_safe(parsed.message_id or ""),
        "X-Hail-Inbound-Id": str(inbound_id),
        "X-Hail-Forward-Hops": str(hops + 1),
        "Auto-Submitted": "auto-forwarded",
    }
    if parsed.message_id:
        headers["References"] = _header_safe(parsed.message_id)
    return Forwarded(
        from_address=forwarder_address,
        to_addresses=[target],
        reply_to=parsed.from_address or "",
        subject=new_subject,
        body_text=body_text,
        body_html=body_html,
        headers=headers,
    )


def detect_loop(*, target: str, hops: int, base_domain: str, max_hops: int) -> None:
    if hops >= max_hops:
        raise LoopDetected("hop_cap")
    _, _, dom = target.rpartition("@")
    if dom.lower() == base_domain.lower():
        raise LoopDetected("base_domain")
