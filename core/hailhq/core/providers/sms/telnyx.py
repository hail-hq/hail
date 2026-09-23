"""SMS uses Telnyx Messaging v2, independently of the voice SIP trunk."""

from uuid import UUID

from hailhq.core.config import settings
from hailhq.core.providers.sms.base import ProviderSmsResult, SmsProvider
from hailhq.core.providers.telnyx import TelnyxClient
from hailhq.core.urls import join_url


class TelnyxSmsProvider(SmsProvider):
    def __init__(self, client: TelnyxClient | None = None):
        self.client = client or TelnyxClient()

    async def send_sms(
        self,
        from_e164: str,
        to_e164: str,
        body: str,
        status_callback_url: str | None = None,
    ) -> ProviderSmsResult:
        payload = {"from": from_e164, "to": to_e164, "text": body, "type": "SMS"}
        if status_callback_url:
            payload["webhook_url"] = status_callback_url
        data = (await self.client.request("POST", "/messages", json=payload))["data"]
        recipient = data["to"][0]
        errors = data.get("errors") or []
        status = recipient["status"]
        return ProviderSmsResult(
            provider_message_sid=data["id"],
            status=(
                "failed" if status in {"sending_failed", "delivery_failed"} else status
            ),
            segment_count=data.get("parts") or 1,
            error_code=str(errors[0]["code"]) if errors else None,
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
                "webhook_url": join_url(settings.hail_api_url, "sms/telnyx"),
            },
        )
        return result["data"]["id"]

    async def attach_number(
        self, messaging_service_sid: str, provider_resource_id: str
    ) -> None:
        resource_id = str(UUID(provider_resource_id))
        await self.client.request(
            "PATCH",
            f"/phone_numbers/{resource_id}/messaging",
            json={
                "messaging_profile_id": messaging_service_sid,
            },
        )
