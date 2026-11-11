"""Browser messaging capabilities, using the normal sender and policy gates.

The website supplies contacts from its authenticated account and an explicit
SMS consent control. The model never chooses a destination or sender.
"""

from fastapi import HTTPException
from hailhq.api.routes.emails import resolve_sender
from hailhq.core.carrier_routing import sms_route_configured
from hailhq.core.compliance_gate import check_email_allowed, check_sms_allowed
from hailhq.core.config import settings
from hailhq.core.email_sender import from_address_for
from hailhq.core.models import OrganizationMember, PhoneNumber, User
from hailhq.core.providers.email.ses import credentials_ready
from hailhq.core.sms_registration_gate import us_registration_block
from sqlalchemy import func, select


async def email_provider_ready():
    return settings.hail_browser_messaging_enabled and await credentials_ready()


def sms_provider_ready(provider):
    return settings.hail_browser_messaging_enabled and sms_route_configured(provider)


async def verified_email(db, org, address):
    if not address:
        return None
    return (
        await db.execute(
            select(User.email)
            .join(OrganizationMember, OrganizationMember.user_id == User.id)
            .where(
                OrganizationMember.organization_id == org,
                User.email_verified.is_(True),
                func.lower(User.email) == address.lower(),
            )
            .limit(1)
        )
    ).scalar_one_or_none()


async def sms_sender(db, org, recipient):
    """Owned numbers first, then eligible pool numbers. Never bind a pool row.

    Pool ownership stays NULL. Only the outbound Sms belongs to the trial
    workspace. Inbound routing continues to drop pool messages.
    """
    if not recipient:
        return None
    for pool in (False, True):
        candidates = (
            await db.execute(
                select(PhoneNumber)
                .where(
                    PhoneNumber.provisioning_state == "active",
                    PhoneNumber.capabilities.any("sms"),
                    PhoneNumber.is_pool.is_(pool),
                    (
                        PhoneNumber.organization_id.is_(None)
                        if pool
                        else PhoneNumber.organization_id == org
                    ),
                )
                .order_by(PhoneNumber.created_at)
                .limit(100)
            )
        ).scalars()
        for number in candidates:
            if not sms_provider_ready(number.provider):
                continue
            if await us_registration_block(db, org, number, recipient) is None:
                return number
    return None


async def capabilities(
    db, org, *, email_recipient=None, sms_recipient=None, recipient_consent=False
):
    email = await verified_email(db, org, email_recipient)
    sender = None
    email_reason = "Verify your account email before testing email."
    try:
        sender = await resolve_sender(db, org, None, allow_ambiguous=True)
    except HTTPException:
        email_reason = "Your workspace email is not ready yet."
    provider_ready = await email_provider_ready()
    email_ready = bool(
        sender and email and settings.hail_internal_secret and provider_ready
    )
    if sender and not provider_ready:
        email_reason = "Email sending is not configured for this test."
    if email_ready:
        email_ready = (await check_email_allowed(db, org, [email])).allowed
        email_reason = (
            None
            if email_ready
            else "Email is not available for this contact right now."
        )
    sms = sms_recipient if recipient_consent else None
    number = await sms_sender(db, org, sms)
    sms_ready = bool(
        number and settings.hail_internal_secret and sms_provider_ready(number.provider)
    )
    if sms_ready:
        sms_ready = (await check_sms_allowed(db, org, sms)).allowed
    if not settings.hail_browser_messaging_enabled:
        email_reason = "Test messaging is off in this preview."
    return {
        "inboxAddress": from_address_for(sender, None) if sender else None,
        "inboundEmailReady": bool(
            sender
            and settings.hail_inbound_enabled
            and settings.hail_inbound_hmac_secret
            and settings.hail_mail_bucket
            and (sender.kind == "hail_mail" or sender.inbound_enabled)
        ),
        "emailSendReady": email_ready,
        "smsSendReady": sms_ready,
        "smsReceiveReady": bool(number and not number.is_pool and number.sms_agent_id),
        "outboundSender": number.e164 if number and sms_ready else None,
        "emailRecipient": email,
        "smsRecipient": sms,
        "emailReason": email_reason,
        "smsReason": (
            "Test messaging is off in this preview."
            if not settings.hail_browser_messaging_enabled
            else (
                None
                if sms_ready
                else (
                    "Enter your mobile number and agree to receive test texts."
                    if not sms
                    else "No configured, eligible text sender is available for your number yet."
                )
            )
        ),
        "attachmentReadTypes": ["text/plain", "text/csv", "application/json"],
    }
