"""Verification for Twilio's inbound webhook signature scheme.

Twilio signs `X-Twilio-Signature` as base64(HMAC-SHA1(auth_token, url +
sorted-concatenated-form-params)) — this is Twilio's own scheme, distinct
from the repo's `hailhq.core.hmac_signing` (HMAC-SHA256 over a raw JSON
body), which is used for Hail-to-Hail internal signing (Lambda -> API,
website -> API). Do not conflate the two.

The `url` passed to `RequestValidator.validate` must be the exact public
URL Twilio POSTed to, including scheme and host — get this from
`hailhq.core.urls.canonical_url` composed with the request path, not
`request.url` directly, since a reverse proxy can rewrite scheme/host in
ways that break the signature check (reconstruct the public URL with the
`hailhq.core.urls` helpers — `canonical_url` exists and is used by
`hailhq.core.url_guard`; there is no `ses_events.py` in this repo).
"""

from __future__ import annotations

from twilio.request_validator import RequestValidator

__all__ = ["verify_twilio_signature"]


def verify_twilio_signature(
    url: str, params: dict[str, str], signature: str | None, auth_token: str
) -> bool:
    if not signature:
        return False
    validator = RequestValidator(auth_token)
    return validator.validate(url, params, signature)
