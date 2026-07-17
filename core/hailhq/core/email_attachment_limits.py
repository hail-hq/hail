"""Shared size cap for outbound email attachments.

One constant enforced at two layers — the single-file upload endpoint
(POST /email-attachments) and the aggregate per-send check (POST
/emails) — so the error text a caller sees is identical everywhere
(API, MCP, CLI). Mirrors SES SendRawEmail's default hard limit.
"""

MAX_EMAIL_ATTACHMENT_BYTES = 10 * 1024 * 1024

ATTACHMENT_TOO_LARGE_DETAIL = (
    "attachment(s) too large — host the file externally and include a "
    "link in the body instead"
)

__all__ = ["MAX_EMAIL_ATTACHMENT_BYTES", "ATTACHMENT_TOO_LARGE_DETAIL"]
