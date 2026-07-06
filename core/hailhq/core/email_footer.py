"""Branding footer appended to every outbound message.

Applied at the send boundary for direct sends (the stored Email row keeps
the tenant-authored body; the wire message carries the footer) and at
build time for forwards (the queued row already holds the final body).
"""

from __future__ import annotations

__all__ = [
    "FOOTER_SENT",
    "FOOTER_FORWARDED",
    "AI_DISCLOSURE_LINE",
    "append_footer",
    "append_disclosure",
]

_LINK = "https://hail.so"

FOOTER_SENT = "Sent by Hail.so"
FOOTER_FORWARDED = "Forwarded by Hail.so"

# Fixed AI-disclosure line appended at the wire-send boundary, same as the
# branding footer — it always rides the wire message and is never part of
# the tenant-authored stored body.
AI_DISCLOSURE_LINE = (
    "This message was sent using an AI-assisted communication platform "
    "on behalf of the sender."
)


def _text_footer(label: str) -> str:
    return f"\n\n--\n{label} ({_LINK})"


def _html_footer(label: str) -> str:
    return (
        '<p style="margin-top:16px;font-size:12px;color:#8a8a8a;">'
        f'--<br>{label} (<a href="{_LINK}">hail.so</a>)</p>'
    )


def append_footer(
    body_text: str | None, body_html: str | None, *, label: str
) -> tuple[str | None, str | None]:
    """Append the branding footer to whichever body parts exist."""
    if body_text is not None:
        body_text = body_text + _text_footer(label)
    if body_html is not None:
        body_html = body_html + _html_footer(label)
    return body_text, body_html


def append_disclosure(
    body_text: str | None, body_html: str | None
) -> tuple[str | None, str | None]:
    """Append the fixed AI-disclosure line to whichever body parts exist.

    Same pattern as :func:`append_footer` — applied at the send boundary so
    the disclosure always rides the wire message, never the stored row.
    """
    if body_text is not None:
        body_text = body_text + f"\n\n{AI_DISCLOSURE_LINE}"
    if body_html is not None:
        body_html = (
            body_html
            + f'<p style="margin-top:8px;font-size:12px;color:#8a8a8a;">{AI_DISCLOSURE_LINE}</p>'
        )
    return body_text, body_html
