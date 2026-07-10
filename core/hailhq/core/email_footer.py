"""Branding footer appended to every outbound message.

Applied at the send boundary for direct sends (the stored Email row keeps
the tenant-authored body; the wire message carries the footer) and at
build time for forwards (the queued row already holds the final body).

Direct sends get one blended line that is both branding and AI
disclosure ("Sent via Hail.so, an AI communication platform.") — kept as
a single sentence deliberately so it reads as attribution, not a legal
disclaimer, while still disclosing AI involvement. Forwards keep the
plain "Forwarded by Hail.so" label: forwarded mail is a relayed human
message, not AI-generated content, so no AI disclosure applies.
"""

from __future__ import annotations

__all__ = [
    "FOOTER_FORWARDED",
    "SENT_FOOTER_TEXT",
    "append_footer",
    "append_sent_footer",
]

_LINK = "https://hail.so"

FOOTER_FORWARDED = "Forwarded by Hail.so"

# The single blended branding + AI-disclosure line for direct sends.
# Text form carries the literal URL; the HTML form hyperlinks "Hail.so".
SENT_FOOTER_TEXT = f"Sent via Hail.so ({_LINK}), an AI communication platform."
_SENT_FOOTER_HTML = (
    '<p style="margin-top:16px;font-size:12px;color:#8a8a8a;">'
    f'--<br>Sent via <a href="{_LINK}">Hail.so</a>, '
    "an AI communication platform.</p>"
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
    """Append the labeled branding footer (forwards) to whichever parts exist."""
    if body_text is not None:
        body_text = body_text + _text_footer(label)
    if body_html is not None:
        body_html = body_html + _html_footer(label)
    return body_text, body_html


def append_sent_footer(
    body_text: str | None, body_html: str | None
) -> tuple[str | None, str | None]:
    """Append the blended branding + AI-disclosure line for direct sends.

    Applied at the send boundary so the line always rides the wire
    message, never the stored row.
    """
    if body_text is not None:
        body_text = body_text + f"\n\n--\n{SENT_FOOTER_TEXT}"
    if body_html is not None:
        body_html = body_html + _SENT_FOOTER_HTML
    return body_text, body_html
