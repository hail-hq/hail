"""A standalone scheduler that sends the digest through Hail's public API."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlencode, urlsplit
from uuid import UUID

import httpx
from hailhq.api.auth import hash_key
from hailhq.core.schemas import EmailCreate
from hailhq.core.urls import canonical_url, join_url
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .report import REPORT_KIND, daily_window, format_report, next_due, render_html

logger = logging.getLogger(__name__)


class ReviewRequired(Exception):
    """A stable diagnostic code. Never include raw API bodies or secrets."""


class RetryLater(Exception):
    """A stable diagnostic code for a failure the next check can clear."""


class DigestSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="HAIL_ACTIVITY_DIGEST_", populate_by_name=True, extra="ignore"
    )
    enabled: bool = False
    api_url: str = "http://api:8080/v1"
    api_key: SecretStr = SecretStr("")
    from_address: str = Field(default="", validation_alias="HAIL_ACTIVITY_DIGEST_FROM")
    to_address: str = Field(default="", validation_alias="HAIL_ACTIVITY_DIGEST_TO")
    database_url: SecretStr = Field(
        default=SecretStr(""), validation_alias="DATABASE_URL"
    )

    @field_validator("api_url")
    @classmethod
    def normalize_api_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("Hail API URL must be an HTTP URL")
        return canonical_url(value)

    def validate_send(self):
        if not self.api_key.get_secret_value():
            raise ReviewRequired("api_key_missing")
        if not re.fullmatch(r"[^@\s,<>]+@hail\.so", self.to_address.lower()):
            raise ReviewRequired("recipient_invalid")
        # Stricter than EmailCreate, so a bad sender fails at startup.
        if not re.fullmatch(r"[^@\s,<>]+@[^@\s,<>]+\.[^@\s,<>]+", self.from_address):
            raise ReviewRequired("sender_invalid")


@dataclass(frozen=True)
class Result:
    status: str
    date: str | None = None
    email_id: str | None = None
    preview: tuple[str, str] | None = None

    def summary(self):
        return {
            "status": self.status,
            **({"date": self.date} if self.date else {}),
            **({"email_id": self.email_id} if self.email_id else {}),
        }


class DigestClient:
    def __init__(self, config: DigestSettings, http: httpx.AsyncClient):
        self.config = config
        self.http = http

    async def request(self, path: str, *, payload=None, key=None):
        response = await self.http.request(
            "POST" if payload is not None else "GET",
            join_url(self.config.api_url, path),
            headers={
                "Authorization": f"Bearer {self.config.api_key.get_secret_value()}",
                **({"Idempotency-Key": key} if key else {}),
            },
            json=payload,
            timeout=30,
        )
        if not response.is_success:
            if response.status_code < 500 and response.status_code != 429:
                # A retry does not clear these. A bad key stays bad, and cached
                # pre-send rejections can persist after a credit top-up.
                # A new key could duplicate an ambiguously accepted email.
                raise ReviewRequired(f"api_rejection_{response.status_code}")
            raise RetryLater(f"api_status_{response.status_code}")
        return response.json()

    async def organization(self) -> str:
        body = await self.request("whoami")
        return str(UUID(body["organization_id"]))

    async def verify_sender(self):
        cursor = None
        seen = set()
        sender = self.config.from_address.casefold()
        while True:
            params = {"limit": 200, **({"cursor": cursor} if cursor else {})}
            body = await self.request(f"email-domains?{urlencode(params)}")
            for item in body["items"]:
                if item["verification_status"] != "verified":
                    continue
                domain = item["domain"].casefold()
                if (item["kind"] == "hail_mail" and domain == sender) or (
                    item["kind"] == "custom" and domain == sender.split("@")[1]
                ):
                    return
            cursor = body.get("next_cursor")
            if not cursor:
                raise ReviewRequired("sender_not_verified")
            if cursor in seen:
                raise ReviewRequired("sender_pagination_invalid")
            seen.add(cursor)

    async def send(self, payload: EmailCreate, key: str) -> str:
        body = await self.request(
            "emails",
            payload=payload.model_dump(
                mode="json", by_alias=True, exclude_none=True, exclude_unset=True
            ),
            key=key,
        )
        if body["status"] not in ("queued", "sent", "delivered"):
            raise ReviewRequired("delivery_failed")
        return str(UUID(body["id"]))


class DigestService:
    def __init__(self, config: DigestSettings, repository, client: DigestClient):
        self.config = config
        self.repository = repository
        self.client = client

    async def run_once(self, now: datetime, *, preview=False) -> Result:
        if not preview and not self.config.enabled:
            return Result("disabled")
        window = daily_window(now)
        if window is None:
            return Result("before_schedule")
        raw_key = self.config.api_key.get_secret_value()
        reporter_hash = hash_key(raw_key) if raw_key else None
        suffix = hashlib.sha256(self.config.to_address.lower().encode()).hexdigest()[
            :20
        ]
        key = f"daily-activity-v1:{window.date}:{suffix}"
        org_id = None
        if not preview:
            self.config.validate_send()
            org_id = await self.client.organization()
        receipt, data = await self.repository.snapshot(
            window, reporter_hash, org_id, key
        )
        if receipt:
            age = (now - receipt.requested_at).total_seconds()
            if receipt.status == "queued" and age <= 900:
                # Not final yet. The worker keeps checking until the send
                # finishes or the stale limit below asks for review.
                return Result("pending", window.date, receipt.id)
            if receipt.status not in ("sent", "delivered"):
                raise ReviewRequired("previous_delivery_failed_or_stale")
            return Result("already_submitted", window.date, receipt.id)
        text = format_report(data, window)
        html = render_html(text, window)
        if preview:
            return Result("preview", window.date, preview=(text, html))
        await self.client.verify_sender()
        payload = EmailCreate.model_validate(
            {
                "from": self.config.from_address,
                "from_name": "Hail",
                "to": [self.config.to_address],
                "subject": f"Hail daily activity · {window.date}",
                "body_text": text,
                "body_html": html,
                "recipient_consent": True,
                "message_type": "informational",
                "consent_source": "The recipient requested this daily Hail activity report.",
                "metadata": {
                    "hail_report": REPORT_KIND,
                    "report_key": key,
                    "report_date": window.date,
                },
            }
        )
        email_id = await self.client.send(payload, key)
        return Result("submitted", window.date, email_id)


class DigestWorker:
    def __init__(
        self,
        service: DigestService,
        *,
        clock: Callable[[], datetime] | None = None,
        poll_interval=60.0,
        stop_event=None,
    ):
        self.service = service
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.poll_interval = poll_interval
        self.stop_event = stop_event or asyncio.Event()
        self.handled_date = None

    async def stop(self):
        self.stop_event.set()

    async def run_forever(self):
        while not self.stop_event.is_set():
            now = self.clock()
            window = daily_window(now)
            if window and window.date != self.handled_date:
                try:
                    async with asyncio.timeout(120):
                        result = await self.service.run_once(now)
                    if result.status in ("submitted", "already_submitted"):
                        self.handled_date = window.date
                        logger.info(
                            "activity_digest status=%s date=%s email_id=%s",
                            result.status,
                            window.date,
                            result.email_id,
                        )
                except ReviewRequired as error:
                    self.handled_date = window.date
                    logger.error(
                        "activity_digest requires_review date=%s code=%s",
                        window.date,
                        error,
                    )
                except RetryLater as error:
                    logger.error("activity_digest failed code=%s", error)
                except Exception as error:
                    # SQL exceptions and request reprs can contain private values.
                    logger.error("activity_digest failed type=%s", type(error).__name__)
            delay = min(
                self.poll_interval,
                max(0.001, (next_due(self.clock()) - self.clock()).total_seconds()),
            )
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
