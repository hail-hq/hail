"""Browser messaging capabilities, using the normal sender and policy gates.

The website supplies contacts from its authenticated account and an explicit
SMS consent control. The model never chooses a destination or sender.
"""

from fastapi import HTTPException
from hailhq.api.routes.emails import resolve_hail_mail_sender
from hailhq.core.carrier_routing import sms_route_configured
from hailhq.core.compliance_gate import check_email_allowed, check_sms_allowed
from hailhq.core.config import settings
from hailhq.core.email_sender import from_address_for
from hailhq.core.models import OrganizationMember, PhoneNumber, User
from hailhq.core.providers.email.ses import credentials_ready
from hailhq.core.sms_registration_gate import is_us, us_registration_block
from sqlalchemy import func, select


async def email_provider_ready():
    return await credentials_ready()


def sms_provider_ready(provider):
    return sms_route_configured(provider)


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


async def sms_sender(db, org, recipient, *, claim=False, call=None):
    """Owned numbers first, then eligible available pool numbers.

    claim=True locks a free pool row; lease_browser_sms sets its call FK.
    Existing calls may send only from their fixed owned or reserved number.
    Pool ownership always stays NULL.
    """
    if not recipient:
        return None
    if call is not None and not claim:
        from hailhq.core.browser_voice import browser_sms_number

        number = await browser_sms_number(db, call)
        return (
            number
            if number is not None and sms_provider_ready(number.provider)
            else None
        )
    for pool in (False, True):
        if pool and claim:
            from hailhq.core.pool import claim_pool_number

            excluded = []
            while len(excluded) < 100:
                number = await claim_pool_number(
                    db, capability="sms", exclude_ids=tuple(excluded)
                )
                if number is None:
                    return None
                excluded.append(number.id)
                if (
                    sms_provider_ready(number.provider)
                    and await us_registration_block(db, org, number, recipient) is None
                ):
                    return number
            return None
        candidates = (
            await db.execute(
                select(PhoneNumber)
                .where(
                    PhoneNumber.provisioning_state == "active",
                    PhoneNumber.capabilities.any("sms"),
                    PhoneNumber.is_pool.is_(pool),
                    PhoneNumber.reserved_call_id.is_(None),
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
        sender = await resolve_hail_mail_sender(db, org)
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
        "smsReceiveReady": bool(number and not number.is_pool),
        "inboundNumber": number.e164 if number and not number.is_pool else None,
        "outboundSender": (
            number.e164 if number and sms_ready and not number.is_pool else None
        ),
        "emailRecipient": email,
        "smsRecipient": sms,
        "emailReason": email_reason,
        "smsReason": (
            None
            if sms_ready
            else (
                "Enter your mobile number and agree to receive test texts."
                if not sms
                else (
                    "Texting this U.S. mobile needs an approved sender. You can try email instead."
                    if is_us(sms) and number is None
                    else "Texts are not available for this number yet. You can try email instead."
                )
            )
        ),
        "attachmentReadTypes": ["text/plain", "text/csv", "application/json"],
    }


async def lease_browser_sms(db, call):
    """Claim after Call.flush; only this call may use a pooled sender."""
    from hailhq.core.browser_voice import browser_sms_number

    caps = dict(call.metadata_.get("browser_capabilities", {}))
    recipient = call.metadata_.get("browser_contacts", {}).get("sms")
    if not caps.get("smsSendReady"):
        return None
    number = await sms_sender(db, call.organization_id, recipient, claim=True)
    if number is not None:
        if number.is_pool:
            number.reserved_call_id = call.id
        call.metadata_ = {**call.metadata_, "browser_sms_number_id": str(number.id)}
        caps.update(
            smsSendReady=True,
            smsReceiveReady=True,
            inboundNumber=number.e164,
            outboundSender=number.e164,
            smsReason=None,
        )
    else:
        caps.update(
            smsSendReady=False,
            smsReceiveReady=False,
            inboundNumber=None,
            outboundSender=None,
        )
        if recipient:
            caps["smsReason"] = "No eligible text number is free. Try again shortly."
    call.metadata_ = {**call.metadata_, "browser_capabilities": caps}
    await db.flush()
    return await browser_sms_number(db, call)


async def live_browser_capabilities(db, call):
    from hailhq.core.browser_voice import browser_sms_number

    caps = dict(call.metadata_.get("browser_capabilities", {}))
    number = await browser_sms_number(db, call)
    live = call.status == "in_progress" and number is not None
    caps.update(
        smsReceiveReady=live,
        inboundNumber=number.e164 if number else None,
        outboundSender=number.e164 if number else None,
        smsSendReady=bool(
            number
            and sms_provider_ready(number.provider)
            and call.status == "in_progress"
            and caps.get("smsSendReady")
        ),
    )
    return caps
