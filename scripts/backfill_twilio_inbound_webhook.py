"""Set the inbound text webhook on every Twilio messaging service Hail holds.

    uv run python scripts/backfill_twilio_inbound_webhook.py

Services created before the webhook was set never deliver inbound texts.
Run once after deploy. Safe to repeat: it only sets the same URL again.
Needs DATABASE_URL, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, HAIL_API_URL.
"""

from __future__ import annotations

import asyncio

from hailhq.core.db import _ensure_initialized, dispose_engine
from hailhq.core.models import PhoneNumber
from hailhq.core.providers.sms.twilio import TwilioSmsProvider
from sqlalchemy import select


async def main() -> None:
    async with _ensure_initialized()() as db:
        rows = (
            await db.execute(
                select(PhoneNumber.organization_id, PhoneNumber.messaging_service_sid)
                .where(
                    PhoneNumber.provider == "twilio",
                    PhoneNumber.messaging_service_sid.is_not(None),
                )
                .distinct()
            )
        ).all()
    provider = TwilioSmsProvider()
    failed = 0
    for organization_id, sid in rows:
        try:
            await provider.ensure_messaging_service(organization_id, sid)
            print(f"ok     {sid}")
        except Exception as exc:  # report and keep going
            failed += 1
            print(f"FAILED {sid}: {exc}")
    await dispose_engine()
    print(f"{len(rows) - failed} updated, {failed} failed")


if __name__ == "__main__":
    asyncio.run(main())
