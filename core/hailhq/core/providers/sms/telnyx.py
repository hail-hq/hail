"""SMS uses Telnyx Messaging v2, independently of the voice SIP trunk."""

from uuid import UUID

import httpx
from hailhq.core.config import settings
from hailhq.core.providers.sms.base import ProviderSmsResult, SmsProvider
from hailhq.core.providers.telnyx import TelnyxClient, path_id
from hailhq.core.urls import join_url


def _rejection_code(exc: httpx.HTTPStatusError) -> str | None:
    """Telnyx error code when the API rejected this message (HTTP 400/422)."""
    if exc.response.status_code not in (400, 422):
        return None
    try:
        body = exc.response.json()
    except ValueError:
        return None
    errors = body.get("errors") if isinstance(body, dict) else None
    if not errors or not isinstance(errors[0], dict) or "code" not in errors[0]:
        return None
    return str(errors[0]["code"])


class TelnyxSmsProvider(SmsProvider):
    # Telnyx: "messaging_profile_id is required when sending with an
    # alphanumeric sender ID" (POST /v2/messages). A number carries its own
    # profile; a sender ID has no number, so the profile must be named.
    sender_id_needs_messaging_service = True

    def __init__(self, client: TelnyxClient | None = None):
        self.client = client or TelnyxClient()

    async def send_sms(
        self,
        from_e164: str,
        to_e164: str,
        body: str,
        status_callback_url: str | None = None,
        messaging_service_sid: str | None = None,
    ) -> ProviderSmsResult:
        payload = {"from": from_e164, "to": to_e164, "text": body, "type": "SMS"}
        if status_callback_url:
            payload["webhook_url"] = status_callback_url
        if messaging_service_sid:
            payload["messaging_profile_id"] = messaging_service_sid
        try:
            data = (await self.client.request("POST", "/messages", json=payload))[
                "data"
            ]
        except httpx.HTTPStatusError as exc:
            # SmsProvider contract: a carrier rejection of this message (invalid
            # destination, blocked recipient) comes back as a failed result.
            # Auth, rate-limit and 5xx errors stay transport failures.
            code = _rejection_code(exc)
            if code is None:
                raise
            return ProviderSmsResult(
                provider_message_sid=None,
                status="failed",
                segment_count=0,
                error_code=code,
            )
        recipient = data["to"][0]
        errors = data.get("errors") or []
        status = recipient["status"]
        return ProviderSmsResult(
            provider_message_sid=data["id"],
            status=(
                "failed" if status in {"sending_failed", "delivery_failed"} else status
            ),
            segment_count=data.get("parts") or 1,
            error_code=(
                str(errors[0]["code"]) if errors and "code" in errors[0] else None
            ),
        )

    async def ensure_messaging_service(
        self, organization_id: UUID, existing_sid: str | None
    ) -> str:
        if existing_sid:
            return existing_sid
        # Profiles are org-specific: no sharing of opt-outs or sender pools.
        name = f"hail-{organization_id}"
        result = await self.client.request(
            "GET", "/messaging_profiles", params={"filter[name]": name}
        )
        matches = [p for p in result["data"] if p.get("name") == name]
        if len(matches) > 1:
            raise ValueError("Multiple Telnyx profiles for this organization")
        if matches:
            return matches[0]["id"]
        result = await self.client.request(
            "POST",
            "/messaging_profiles",
            json={
                "name": name,
                "enabled": True,
                # Required by Telnyx; ["*"] allows every destination.
                "whitelisted_destinations": ["*"],
                "webhook_url": join_url(settings.hail_api_url, "sms/telnyx"),
            },
        )
        return result["data"]["id"]

    async def delete_organization_services(
        self, organization_id: UUID, service_ids: set[str]
    ) -> None:
        name = f"hail-{organization_id}"
        found = await self.client.request(
            "GET", "/messaging_profiles", params={"filter[name]": name}
        )
        ids = service_ids | {p["id"] for p in found["data"] if p.get("name") == name}
        for profile_id in ids:
            try:
                await self.client.request(
                    "DELETE", f"/messaging_profiles/{path_id(profile_id)}"
                )
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404:
                    raise

    async def attach_number(
        self, messaging_service_sid: str, provider_resource_id: str
    ) -> None:
        resource_id = path_id(provider_resource_id)
        await self.client.request(
            "PATCH",
            f"/phone_numbers/{resource_id}/messaging",
            json={
                "messaging_profile_id": messaging_service_sid,
            },
        )
