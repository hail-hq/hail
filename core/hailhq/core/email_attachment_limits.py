"""Shared limits and byte cursors for email attachments.

One constant enforced at two layers — the single-file upload endpoint
(POST /email-attachments) and the aggregate per-send check (POST
/emails) — so the error text a caller sees is identical everywhere
(API, MCP, CLI).

SESv2 hard-caps the wire message at 40MB *after* base64 encoding
(not adjustable). This cap is on raw bytes before encoding; base64 plus
MIME line breaks inflate by ~1.37×, so 25MB raw ≈ 34MB encoded, leaving
headroom for bodies and headers. Note SES bandwidth-throttles messages
over 10MB.
"""

MAX_EMAIL_ATTACHMENT_BYTES = 25 * 1024 * 1024

ATTACHMENT_TOO_LARGE_DETAIL = (
    "attachment(s) too large — host the file externally and include a "
    "link in the body instead"
)

# File inspection is narrower than forwarding original stored bytes.
AGENT_ATTACHMENT_READ_BYTES = 10 * 1024 * 1024
AGENT_ATTACHMENT_READ_TYPES = (
    "text/plain",
    "text/csv",
    "application/json",
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/gif",
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
)

AGENT_ATTACHMENT_TEXT_BYTES = 24000
AGENT_ATTACHMENT_DECODED_BYTES = 4 * 1024 * 1024


def text_chunk(
    text: str, offset: int = 0, budget: int = AGENT_ATTACHMENT_TEXT_BYTES
) -> tuple[str, int | None]:
    """Read a UTF-8 byte range, preserving complete characters and byte cursors."""
    if budget <= 0:
        raise ValueError("The text reading budget must be positive")
    encoded = text.encode("utf-8")
    if offset < 0 or offset > len(encoded):
        raise ValueError("That text offset is not in the file")
    try:
        encoded[:offset].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("That text offset is inside a UTF-8 character") from exc
    chunk = encoded[offset : offset + budget].decode("utf-8", errors="ignore")
    if not chunk and offset < len(encoded):
        raise ValueError("The text reading budget cannot fit the next character")
    next_offset = offset + len(chunk.encode("utf-8"))
    return chunk, next_offset if next_offset < len(encoded) else None


__all__ = [
    "AGENT_ATTACHMENT_DECODED_BYTES",
    "AGENT_ATTACHMENT_READ_BYTES",
    "AGENT_ATTACHMENT_READ_TYPES",
    "AGENT_ATTACHMENT_TEXT_BYTES",
    "ATTACHMENT_TOO_LARGE_DETAIL",
    "MAX_EMAIL_ATTACHMENT_BYTES",
    "text_chunk",
]
