"""SES Receipt Rule Lambda — translates an SES event into a signed POST.

Pure stdlib. Deploy artifact is a zip of this single file. The Terraform
module under ``infra/terraform/`` packages and uploads it.

Env vars (set by the Terraform module):

* ``HAIL_API_URL``           — Base URL of the Hail API (no trailing slash).
* ``HAIL_INBOUND_BUCKET``    — S3 bucket SES wrote raw MIME into.
* ``HAIL_INBOUND_HMAC_SECRET`` — Shared secret with the API.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import urllib.request

API_PATH = "/internal/ses-events"


def _verdict(receipt: dict, name: str) -> str | None:
    v = receipt.get(name) or {}
    return v.get("status")


def _make_payload(record: dict) -> dict:
    mail = record["mail"]
    receipt = record["receipt"]
    return {
        "message_id": mail["messageId"],
        "envelope_from": mail["source"],
        "recipients": list(receipt.get("recipients") or []),
        "verdicts": {
            "spam": _verdict(receipt, "spamVerdict"),
            "virus": _verdict(receipt, "virusVerdict"),
            "spf": _verdict(receipt, "spfVerdict"),
            "dkim": _verdict(receipt, "dkimVerdict"),
            "dmarc": _verdict(receipt, "dmarcVerdict"),
        },
        "s3_bucket": os.environ["HAIL_INBOUND_BUCKET"],
        "s3_key": f"raw/{mail['messageId']}",
        "timestamp": mail.get("timestamp"),
    }


def handler(event: dict, _context) -> dict:
    record = event["Records"][0]["ses"]
    payload = _make_payload(record)
    body = json.dumps(payload, separators=(",", ":")).encode()

    secret = os.environ["HAIL_INBOUND_HMAC_SECRET"].encode()
    sig = hmac.new(secret, body, hashlib.sha256).hexdigest()
    # Ad-hoc join is a deliberate exception to the hailhq.core.urls invariant:
    # the Lambda is stdlib-only (no hailhq import), the URL never crosses into
    # a comparison, and the tfvars/docs both pin "no trailing slash".
    url = os.environ["HAIL_API_URL"].rstrip("/") + API_PATH

    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Hail-Signature": f"sha256={sig}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        resp.read()
    return {"status": "ok"}
