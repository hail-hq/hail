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
    "SENT_FOOTER_TEXT",
    "append_forwarded_footer",
    "append_sent_footer",
]

_LINK = "https://hail.so"

# The single blended branding + AI-disclosure line for direct sends.
# Text form carries the literal URL; the HTML form hyperlinks "Hail.so".
SENT_FOOTER_TEXT = f"Sent via Hail.so ({_LINK}), an AI communication platform."

_FORWARDED_FOOTER_TEXT = f"Forwarded by Hail.so ({_LINK})"
_FORWARDED_FOOTER_HTML_INNER = f'Forwarded by <a href="{_LINK}">Hail.so</a>'
_SENT_FOOTER_HTML_INNER = (
    f'Sent via <a href="{_LINK}">Hail.so</a>, an AI communication platform.'
)


def _html_footer(inner: str) -> str:
    """The one <p> wrapper both footers share — style changes land here once."""
    return (
        '<p style="margin-top:16px;font-size:12px;color:#8a8a8a;">' f"--<br>{inner}</p>"
    )


def _append(
    body_text: str | None,
    body_html: str | None,
    *,
    text_line: str,
    html_inner: str,
) -> tuple[str | None, str | None]:
    if body_text is not None:
        body_text = body_text + f"\n\n--\n{text_line}"
    if body_html is not None:
        body_html = body_html + _html_footer(html_inner)
    return body_text, body_html


def append_forwarded_footer(
    body_text: str | None, body_html: str | None
) -> tuple[str | None, str | None]:
    """Append the forwarded-mail branding footer to whichever parts exist."""
    return _append(
        body_text,
        body_html,
        text_line=_FORWARDED_FOOTER_TEXT,
        html_inner=_FORWARDED_FOOTER_HTML_INNER,
    )


def append_sent_footer(
    body_text: str | None, body_html: str | None
) -> tuple[str | None, str | None]:
    """Append the blended branding + AI-disclosure line for direct sends.

    Applied at the send boundary so the line always rides the wire
    message, never the stored row.
    """
    return _append(
        body_text,
        body_html,
        text_line=SENT_FOOTER_TEXT,
        html_inner=_SENT_FOOTER_HTML_INNER,
    )
