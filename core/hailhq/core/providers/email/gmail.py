"""Gmail REST adapter for connected accounts (send + ephemeral reads).

All calls go through httpx against the Gmail v1 REST surface — no Google
SDK. One ``GmailClient`` is built per request from the account row's
decrypted refresh token; the access token is cached on the instance so a
send that also resolves a thread pays for one token refresh, not two.
Nothing here persists anything: reads are pass-through by design (spec
§4 — ephemeral only).
"""

from __future__ import annotations

import base64
import time
from email.utils import getaddresses
from typing import Any

import httpx

from hailhq.core.providers.email.base import (
    EmailSender,
    ProviderAttachment,
    ProviderSendResult,
)
from hailhq.core.providers.email.gmail_oauth import (
    GmailReauthRequired,
    refresh_access_token,
)
from hailhq.core.providers.email.mime import build_raw_mime

__all__ = ["GmailApiError", "GmailAuthError", "GmailClient", "GmailEmailProvider"]

GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"

_SUMMARY_HEADERS = "From,To,Cc,Subject,Date,Message-ID,In-Reply-To"


class GmailApiError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"gmail api error {status}: {detail}")
        self.status = status
        self.detail = detail


class GmailAuthError(GmailApiError):
    """Grant revoked/expired — surface as reauth_required upstream."""


_shared_http_client: httpx.AsyncClient | None = None


def _shared_http() -> httpx.AsyncClient:
    """Process-wide ``httpx.AsyncClient`` for accounts that don't inject one.

    Lazily created on first use and reused across ``GmailClient`` instances
    so connections are pooled instead of a fresh (never-closed) client per
    request. Tests always inject their own client, so this path only runs
    in the real app.
    """
    global _shared_http_client
    if _shared_http_client is None:
        _shared_http_client = httpx.AsyncClient(timeout=30.0)
    return _shared_http_client


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


class GmailClient:
    def __init__(
        self, *, refresh_token: str, http: httpx.AsyncClient | None = None
    ) -> None:
        self._refresh_token = refresh_token
        self._http = http or _shared_http()
        self._access_token: str | None = None
        self._token_expiry = 0.0

    async def _token(self) -> str:
        if self._access_token is None or time.time() > self._token_expiry - 60:
            try:
                token, expires_in = await refresh_access_token(
                    refresh_token=self._refresh_token, http=self._http
                )
            except GmailReauthRequired as exc:
                raise GmailAuthError(401, str(exc)) from exc
            self._access_token = token
            self._token_expiry = time.time() + expires_in
        return self._access_token

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {await self._token()}"}
        try:
            resp = await self._http.request(
                method,
                f"{GMAIL_API}{path}",
                params=params,
                json=json_body,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise GmailApiError(502, str(exc)) from exc
        if resp.status_code == 401:
            raise GmailAuthError(401, resp.text)
        if resp.status_code >= 400:
            raise GmailApiError(resp.status_code, resp.text)
        return resp.json()

    async def get_profile(self) -> dict[str, Any]:
        return await self._request("GET", "/profile")

    async def send_message(
        self, *, raw: bytes, thread_id: str | None = None
    ) -> tuple[str, str]:
        body: dict[str, Any] = {"raw": _b64url(raw)}
        if thread_id:
            body["threadId"] = thread_id
        data = await self._request("POST", "/messages/send", json_body=body)
        return data["id"], data.get("threadId", "")

    async def find_thread_id(self, rfc822_message_id: str) -> str | None:
        data = await self._request(
            "GET",
            "/messages",
            params={"q": f"rfc822msgid:{rfc822_message_id}", "maxResults": 1},
        )
        messages = data.get("messages") or []
        return messages[0]["threadId"] if messages else None

    async def list_messages(
        self,
        *,
        q: str | None = None,
        max_results: int = 25,
        page_token: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        params: dict[str, Any] = {"maxResults": max_results}
        if q:
            params["q"] = q
        if page_token:
            params["pageToken"] = page_token
        data = await self._request("GET", "/messages", params=params)
        summaries: list[dict[str, Any]] = []
        for ref in data.get("messages") or []:
            meta = await self._request(
                "GET",
                f"/messages/{ref['id']}",
                params={
                    "format": "metadata",
                    "metadataHeaders": _SUMMARY_HEADERS.split(","),
                },
            )
            summaries.append(_parse_message(meta, include_body=False))
        return summaries, data.get("nextPageToken")

    async def get_message(self, message_id: str) -> dict[str, Any]:
        data = await self._request(
            "GET", f"/messages/{message_id}", params={"format": "full"}
        )
        return _parse_message(data, include_body=True)


def _headers_map(payload: dict[str, Any]) -> dict[str, str]:
    return {h["name"].lower(): h["value"] for h in payload.get("headers") or []}


def _bare_addresses(value: str | None) -> list[str]:
    """Bare addresses from a raw header value — display names stripped.

    ``getaddresses`` handles quoted display-name commas
    (``"Doe, John" <john@example.com>``), matching the codebase convention
    for these field names (see ``hailhq.core.email_mime``).
    """
    if not value:
        return []
    return [addr for _name, addr in getaddresses([value]) if addr]


def _walk_parts(part: dict[str, Any], out: dict[str, Any]) -> None:
    mime = part.get("mimeType", "")
    body = part.get("body") or {}
    filename = part.get("filename") or ""
    if filename and body.get("attachmentId"):
        out["attachments"].append(
            {
                "filename": filename,
                "content_type": mime,
                "size_bytes": body.get("size", 0),
                "attachment_id": body["attachmentId"],
            }
        )
    elif mime == "text/plain" and body.get("data") and out["body_text"] is None:
        out["body_text"] = _b64url_decode(body["data"]).decode("utf-8", "replace")
    elif mime == "text/html" and body.get("data") and out["body_html"] is None:
        out["body_html"] = _b64url_decode(body["data"]).decode("utf-8", "replace")
    for child in part.get("parts") or []:
        _walk_parts(child, out)


def _parse_message(data: dict[str, Any], *, include_body: bool) -> dict[str, Any]:
    payload = data.get("payload") or {}
    headers = _headers_map(payload)
    parsed: dict[str, Any] = {
        "id": data["id"],
        "thread_id": data.get("threadId", ""),
        "from_address": next(iter(_bare_addresses(headers.get("from"))), ""),
        "to_addresses": _bare_addresses(headers.get("to")),
        "cc_addresses": _bare_addresses(headers.get("cc")),
        "subject": headers.get("subject", ""),
        "date": headers.get("date", ""),
        "snippet": data.get("snippet", ""),
        "message_id": headers.get("message-id", ""),
    }
    if include_body:
        parsed.update(
            {
                "body_text": None,
                "body_html": None,
                "attachments": [],
                "in_reply_to": headers.get("in-reply-to"),
            }
        )
        _walk_parts(payload, parsed)
    return parsed


class GmailEmailProvider(EmailSender):
    """``EmailSender`` conformance for connected-account sends.

    Threading: an ``In-Reply-To`` entry in ``headers`` triggers a
    ``rfc822msgid:`` lookup so the send lands in the right Gmail thread.
    """

    def __init__(self, client: GmailClient) -> None:
        self._client = client

    async def send_email(
        self,
        *,
        from_address: str,
        to_addresses: list[str],
        subject: str,
        body_text: str | None,
        body_html: str | None,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        reply_to: str | None = None,
        headers: dict[str, str] | None = None,
        attachments: list[ProviderAttachment] | None = None,
    ) -> ProviderSendResult:
        if not to_addresses:
            raise ValueError("send_email requires at least one recipient")
        if body_text is None and body_html is None:
            raise ValueError("send_email requires body_text or body_html")

        clean_headers = {k: v for k, v in (headers or {}).items() if v}
        thread_id: str | None = None
        in_reply_to = clean_headers.get("In-Reply-To")
        if in_reply_to:
            thread_id = await self._client.find_thread_id(in_reply_to)

        raw = build_raw_mime(
            from_address=from_address,
            to_addresses=to_addresses,
            subject=subject,
            body_text=body_text,
            body_html=body_html,
            cc=cc,
            bcc=bcc,
            reply_to=reply_to,
            headers=clean_headers,
            attachments=attachments or [],
        )
        message_id, sent_thread = await self._client.send_message(
            raw=raw, thread_id=thread_id
        )
        return ProviderSendResult(
            provider_message_id=message_id,
            provider_thread_id=sent_thread or thread_id,
        )
