"""Consent evidence for US SMS registration.

POST /verifications/sms/evidence - upload a screenshot, get back a public link.
GET  /verifications/sms/evidence/{organization_id}/{token} - public: carrier
reviewers open it.

Carriers take consent evidence only as a public link inside the campaign
text; they have no upload field. So Hail keeps the file and gives the link.
The random token in the link is the only credential. Bytes live in the
shared mail bucket under sms-evidence/{organization_id}/{token}; no table
tracks them. The retention sweep deletes an organization's folder 12 months
after it closes (hailhq.core.retention).
"""

from __future__ import annotations

import ipaddress
import re
import secrets
from typing import Annotated
from urllib.parse import urlparse
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi import status as http_status
from fastapi.responses import RedirectResponse
from hailhq.api.deps import Principal, get_current_principal
from hailhq.api.errors import unprocessable
from hailhq.api.multipart import parse_capped_form
from hailhq.api.ratelimit import GENERAL_RATE_LIMITED_RESPONSES, exempt_prefix
from hailhq.core.config import settings
from hailhq.core.s3_mail import S3MailClient
from hailhq.core.sms_evidence import SMS_EVIDENCE_PREFIX
from hailhq.core.urls import join_url
from pydantic import BaseModel, Field
from starlette.datastructures import UploadFile

router = APIRouter(
    prefix="/verifications/sms/evidence",
    tags=["verifications"],
    responses=GENERAL_RATE_LIMITED_RESPONSES,
)

# The link is its own credential, like an unsubscribe link. Reviewers share
# the proxy's IP with all anonymous traffic, so the public GET is not limited.
# It only signs a redirect: no storage call and no file bytes per request.
exempt_prefix("/verifications/sms/evidence/")

MAX_EVIDENCE_BYTES = 10 * 1024 * 1024
# Room for the multipart boundaries and part headers around the file.
_MAX_BODY_BYTES = MAX_EVIDENCE_BYTES + 64 * 1024
_TOKEN = re.compile(r"[A-Za-z0-9_-]{32}")
# The file's first bytes decide its type, never the name or the header the
# client sent. Only these types are kept or served: no HTML or SVG on the
# API's origin.
_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"%PDF-", "application/pdf"),
)
_UPLOAD_BODY = {
    "requestBody": {
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "required": ["file"],
                    "properties": {
                        "file": {
                            "type": "string",
                            "format": "binary",
                            "description": "A PNG, JPG or PDF of 10 MB or less.",
                        }
                    },
                }
            }
        },
    }
}


class EvidenceUploadResponse(BaseModel):
    url: str = Field(
        description="Public link to the file. Put it in an `*_evidence_url` field."
    )
    content_type: str = Field(
        description="`image/png`, `image/jpeg` or `application/pdf`, read from the file."
    )
    size_bytes: int = Field(description="File size in bytes.")


def get_evidence_store() -> S3MailClient | None:
    """The mail bucket, or None when this install has no bucket."""
    bucket = settings.hail_mail_bucket
    return S3MailClient(bucket=bucket) if bucket else None


def _sniff(data: bytes) -> str | None:
    return next((kind for sig, kind in _SIGNATURES if data.startswith(sig)), None)


async def _read_upload(request: Request) -> bytes:
    """The one file part, read with the body capped before parsing."""
    form = await parse_capped_form(
        request, max_bytes=_MAX_BODY_BYTES, max_files=1, max_fields=1
    )
    try:
        upload = form.get("file")
        if not isinstance(upload, UploadFile):
            raise unprocessable(
                "Send the file in a part named file.", loc=["body", "file"]
            )
        data = await upload.read()
    finally:
        await form.close()
    if len(data) > MAX_EVIDENCE_BYTES:
        raise unprocessable("The file must be 10 MB or smaller.", loc=["body", "file"])
    return data


def _public_base() -> str | None:
    """HAIL_API_URL when carrier reviewers can reach it, else None."""
    parsed = urlparse(settings.hail_api_url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host or host == "localhost" or "." not in host:
        return None
    try:
        if not ipaddress.ip_address(host).is_global:
            return None
    except ValueError:
        pass
    return settings.hail_api_url


@router.post(
    "",
    response_model=EvidenceUploadResponse,
    status_code=http_status.HTTP_201_CREATED,
    operation_id="upload_sms_evidence",
    openapi_extra=_UPLOAD_BODY,
)
async def upload_sms_evidence(
    request: Request,
    principal: Annotated[Principal, Depends(get_current_principal)],
    store: Annotated[S3MailClient | None, Depends(get_evidence_store)],
) -> EvidenceUploadResponse:
    """Upload a consent screenshot and get back a public link.

    Put the returned `url` in an SMS registration field that asks for an
    evidence URL (for example `website_evidence_url`). Carrier reviewers open
    it without signing in, so anyone with the link can see the file. Remove
    personal information first. Pasting your own public link still works.
    """
    base = _public_base()
    if store is None or base is None:
        # Reviewers open the link from outside: a local address never works.
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "This server cannot store files for carrier reviewers. It needs a "
                "mail bucket and a public https HAIL_API_URL. Paste a public link instead."
            ),
        )
    data = await _read_upload(request)
    content_type = _sniff(data)
    if content_type is None:
        raise unprocessable("Upload a PNG, JPG or PDF.", loc=["body", "file"])
    org = principal.organization_id
    token = secrets.token_urlsafe(24)
    await store.put_attachment(
        f"{SMS_EVIDENCE_PREFIX}{org}/{token}", data, content_type
    )
    return EvidenceUploadResponse(
        url=join_url(base, f"v1/verifications/sms/evidence/{org}/{token}"),
        content_type=content_type,
        size_bytes=len(data),
    )


@router.get(
    "/{organization_id}/{token}",
    response_class=RedirectResponse,
    status_code=http_status.HTTP_302_FOUND,
    operation_id="get_sms_evidence",
    responses={
        302: {"description": "Redirect to a short-lived link to the file."},
        404: {"description": "The link is malformed."},
    },
)
async def get_sms_evidence(
    organization_id: UUID,
    token: str,
    store: Annotated[S3MailClient | None, Depends(get_evidence_store)],
) -> RedirectResponse:
    """Public: the file behind a link from `POST /verifications/sms/evidence`.

    No sign-in. Carrier reviewers open this link. It redirects to a link that
    works for 10 minutes; open this link again for a new one.
    """
    if store is None or not _TOKEN.fullmatch(token):
        raise HTTPException(status_code=404, detail="Not found")
    # The file is served from storage, never from the API's origin, and the
    # API never streams it. Signing is local: no storage call per view.
    url = await store.presign_get(
        f"{SMS_EVIDENCE_PREFIX}{organization_id}/{token}", ttl_seconds=600
    )
    return RedirectResponse(
        url,
        status_code=http_status.HTTP_302_FOUND,
        headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"},
    )


__all__ = ["get_evidence_store", "router"]
