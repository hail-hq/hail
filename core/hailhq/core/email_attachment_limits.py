"""Shared size cap for outbound email attachments.

One constant enforced at two layers — the single-file upload endpoint
(POST /email-attachments) and the aggregate per-send check (POST
/emails) — so the error text a caller sees is identical everywhere
(API, MCP, CLI).

SESv2 hard-caps the wire message at 40MB *after* base64 encoding
(not adjustable). This cap is on raw bytes before encoding; base64 plus
MIME line breaks inflate by ~1.37×, so 25MB raw ≈ 34MB encoded, leaving
headroom for bodies, the branding footer, and headers. Note SES
bandwidth-throttles messages over 10MB.
"""

MAX_EMAIL_ATTACHMENT_BYTES = 25 * 1024 * 1024

ATTACHMENT_TOO_LARGE_DETAIL = (
    "attachment(s) too large — host the file externally and include a "
    "link in the body instead"
)

__all__ = ["ATTACHMENT_TOO_LARGE_DETAIL", "MAX_EMAIL_ATTACHMENT_BYTES"]
