"""Telnyx plug-in for US A2P 10DLC registration: a brand, optional vetting,
then a campaign; numbers are linked to the campaign one by one.

A person (sole proprietor) brand waits for a PIN Telnyx texts to the
person's mobile. The person types it into Hail, ``confirm`` sends it to
Telnyx, and the campaign is then created as for a company. The PIN is never
stored or logged."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from types import MappingProxyType

import httpx
from hailhq.core.providers.sms.telnyx import TelnyxSmsProvider
from hailhq.core.providers.telnyx import TelnyxClient, path_id
from hailhq.core.providers.verification.base import (
    SMS_REFUSED,
    SMS_VALUE_REFUSED,
    DiscardFailures,
    DraftResult,
    Problem,
    ProviderStatus,
    RegistrationHoldsAnotherNumber,
    Requirements,
    SmsRegistrationProvider,
    VerificationProviderError,
)
from hailhq.core.providers.verification.forms import display_name, text_length
from hailhq.core.providers.verification.rejections import (
    TEXT_KEYS,
    carrier_texts,
    with_carrier_text,
)
from hailhq.core.providers.verification.sms_form import (
    NINE_DIGIT_COUNTRIES,
    campaign_text,
    sms_requirements,
    sms_subject_name,
    tax_number,
)
from hailhq.core.providers.verification.sms_review import (
    LEGACY_APPLICATION,
    LOW_VOLUME_FASTER_LANE,
    LOW_VOLUME_FASTER_LANE_FAILED,
    account_problem,
    application_validation_error,
    brand_rejected_reason,
    campaign_repair_reason,
    detail_fixes,
)

logger = logging.getLogger(__name__)

# Only a number in these states (Telnyx's ``assignmentStatus``) leaves a
# person's campaign free: a failed link and a number being unlinked. Any
# other value holds it, a missing or unknown one too, so a second number is
# never linked on a status Hail does not know.
_RELEASES_CAMPAIGN = frozenset({"FAILED_ASSIGNMENT", "PENDING_UNASSIGNMENT"})
# The link is done: the only status in which the number tried counts as
# already linked, and in which the caller may record another held number as
# the registration's own.
_LINKED = "ASSIGNED"
# GET /10dlc/phone_number_campaigns: 20 is the documented default and no
# maximum is documented, so every page is read.
_LIST_PAGE_SIZE = 20
# A guard against a listing that never ends (1,000 records).
_MAX_LIST_PAGES = 50

_OPT_OUT = "STOP,STOPALL,UNSUBSCRIBE,CANCEL,END,QUIT"
_HELP = "HELP,INFO"
# The STOP confirmation declared in the campaign filing. Telnyx answers STOP
# itself (its default text names no sender) and has no default HELP answer,
# so _set_replies makes the messaging profile's auto-replies the filed texts.
# Hail's own replies (settings.hail_sms_compliance_replies_enabled) stay off:
# on top of Telnyx's they would text the subscriber twice.
_OPT_OUT_MESSAGE = (
    "You are unsubscribed and will get no more messages from this workspace."
)
# Legacy opt-in text, retained only when reading an existing old campaign.
# New applications file only the actual response the customer supplies.
# Carriers reject a
# campaign whose opt-in text lacks the message frequency and the rates notice
# (TCR error 611).
_OPT_IN_MESSAGE = (
    "You are opted in. Msg frequency varies. Msg and data rates may apply. "
    "Reply HELP for help, STOP to opt out."
)
# Help contact for campaigns whose snapshot has no contact email (filed
# before it was kept) or one too long for the HELP reply.
_HELP_CONTACT = "hi@hail.so"
# Telnyx's maximum for helpMessage (Update campaign API schema); every filed
# reply is kept within it.
_TEXT_MAX = 255
# A sender name cut to fit stops at a word boundary only when that keeps at
# least this many characters; else it is cut mid-word.
_NAME_MIN = 20
# The messaging profile auto-replies set from the filing: Telnyx's op, the
# filed text it sends, its keywords. "info" is the API's name for HELP.
_AUTO_REPLIES = (
    ("stop", "optoutMessage", _OPT_OUT),
    ("info", "helpMessage", _HELP),
)
# Auto-replies apply by the subscriber's country; 10DLC is US texting.
_AUTO_REPLY_COUNTRY = "US"
_VERIFIED = {"VERIFIED", "VETTED_VERIFIED"}
# How long a low-volume company brand may stay not verified (and not
# UNVERIFIED) before Hail treats it as final: the faster lane or a hold.
# Also how long after a faster lane's vetting turns ACTIVE the brand may
# still read not verified before the row is held.
_IDENTITY_WAIT = timedelta(hours=24)
# Use cases filed with the picked purposes as sub-use-cases. MIXED takes 2
# to 5; LOW_VOLUME (low volume mixed) takes 1 to 5; the form allows 1 to 5.
_SUB_USE_CASES = frozenset({"MIXED", "SOLE_PROPRIETOR", "LOW_VOLUME"})
# Texts Telnyx sends a person: the PIN (@OTP_PIN@ is replaced by Telnyx),
# then a notice once the PIN is confirmed.
_PIN_SMS = "Hail: your confirmation code is @OTP_PIN@. It expires in 24 hours."
_SUCCESS_SMS = "Hail: your number is confirmed. Reply STOP to opt out."
# A PIN is valid this long; past it, status() sends a new one.
_OTP_RESEND_AFTER = timedelta(hours=24)
_WRONG_CODE = "That code is wrong or has expired. Ask for a new one and try again."
# A campaign is approved once the mobile carriers accept it; TCR_ACCEPTED
# alone is pending (the carriers have not reviewed it yet).
_APPROVED = {"MNO_ACCEPTED", "MNO_PROVISIONED", "ACTIVE"}
_REJECTED = {
    "TCR_FAILED",
    "TELNYX_FAILED",
    "MNO_REJECTED",
    "MNO_PROVISIONING_FAILED",
    "REJECTED",
    "FAILED",
}
# Telnyx registers a new brand in the background: for about 2 seconds the
# brand id is unknown to smsOtp, vetting and DELETE (404 or 400). Wait this
# many reads, this far apart, for it to settle.
_BRAND_READS = 10
_BRAND_READ_GAP = 1.0
# Telnyx brand fields, as the names of Hail's form fields.
_ADDRESS_FIELDS = {
    "street": "address.street",
    "city": "address.city",
    "state": "address.region",
    "postalCode": "address.postal_code",
    "country": "address.country_code",
}
_FORM_FIELDS = {
    "person": {
        "firstName": "first_name",
        "lastName": "last_name",
        "email": "email",
        "phone": "mobile_phone_number",
        "mobilePhone": "mobile_phone_number",
        **_ADDRESS_FIELDS,
    },
    "business": {
        "companyName": "legal_name",
        "displayName": "legal_name",
        "entityType": "business_type",
        "ein": "ein",
        "vertical": "industry",
        "website": "website",
        "email": "rep_email",
        "phone": "rep_phone",
        **_ADDRESS_FIELDS,
    },
}
# Campaign content and consent declarations filed from the application.
# Resource IDs, messaging-profile configuration and number pooling are not
# customer-editable evidence and must not turn operational errors into refusals.
_CAMPAIGN_FIELDS = {
    "usecase",
    "subUsecases",
    "description",
    "messageFlow",
    "sample1",
    "sample2",
    "sample3",
    "sample4",
    "sample5",
    "embeddedLink",
    "embeddedPhone",
    "ageGated",
    "directLending",
    "subscriberOptin",
    "subscriberOptout",
    "subscriberHelp",
    "optinKeywords",
    "optoutKeywords",
    "helpKeywords",
    "optinMessage",
    "optoutMessage",
    "helpMessage",
    "termsAndConditions",
    "privacyPolicyLink",
    "termsAndConditionsLink",
}
_APPLICATION_POINTERS = {
    f"/{field}" for field in _CAMPAIGN_FIELDS | set().union(*_FORM_FIELDS.values())
}
_DUPLICATE = {
    "mobile_phone_number": "This mobile number is on too many registrations. Use another one.",
    "email": "This email is on too many registrations. Use another one.",
    "rep_phone": "This phone number is on too many registrations. Use another one.",
    "rep_email": "This email is on too many registrations. Use another one.",
}


def _campaign_replies(brand: dict, refs: dict) -> dict:
    """The HELP, opt-in and STOP replies filed with a campaign, each opening
    with the sender's name: carrier reviewers want the sender named. The
    name is the brand's, else the one saved when the draft was filed. The
    HELP reply gives the customer's own contact email, or hi@hail.so when
    the name and that email do not fit in 255 characters."""
    name = (
        display_name(brand.get("displayName"))
        or display_name(brand.get("firstName"), brand.get("lastName"))
        or display_name(refs.get("subject_name"))
    )
    snapshot = refs.get("campaign")
    contact = (
        snapshot.get("contact_email") if isinstance(snapshot, dict) else None
    ) or _HELP_CONTACT
    help_text = f"Reply STOP to opt out. For help contact {contact}."
    if len(f"{name}: {help_text}" if name else help_text) > _TEXT_MAX:
        help_text = f"Reply STOP to opt out. For help contact {_HELP_CONTACT}."
    optin = _named(name, _OPT_IN_MESSAGE)
    if isinstance(snapshot, dict) and snapshot.get("schema_version", 1) >= 2:
        optin = snapshot.get("opt_in_message") or ""
    return {
        "helpMessage": _named(name, help_text),
        **({"optinMessage": optin} if optin else {}),
        # Required when subscriberOptout is true (Telnyx 10015).
        "optoutMessage": _named(name, _OPT_OUT_MESSAGE),
    }


def _named(name: str | None, text: str) -> str:
    """``text`` after "name: ", within 255 characters. The name is cut,
    never the text: at a word boundary when that keeps at least
    ``_NAME_MIN`` characters, else mid-word."""
    room = _TEXT_MAX - len(text) - len(": ")
    if name and len(name) > room:
        cut = name[: max(0, room)]
        if name[len(cut)] != " ":
            whole = cut.rsplit(" ", 1)[0].rstrip()
            if " " in cut and len(whole) >= min(_NAME_MIN, room):
                cut = whole
        name = cut.rstrip()
    return f"{name}: {text}" if name else text


def _filed_replies(campaign: dict, brand: dict, refs: dict) -> dict:
    """The replies Telnyx holds for a filed campaign (its record has
    ``helpMessage``, ``optinMessage`` and ``optoutMessage``). A text the
    record lacks is built as Hail files it."""
    built = _campaign_replies(brand, refs)
    return {
        key: text if isinstance(text, str) and text.strip() else built[key]
        for key, text in ((k, campaign.get(k)) for k in built)
    }


def _saved_replies(refs: dict) -> dict | None:
    """``refs['replies']`` when it holds a text for each auto-reply, else
    None."""
    replies = refs.get("replies")
    if not isinstance(replies, dict):
        return None
    for _, key, _ in _AUTO_REPLIES:
        text = replies.get(key)
        if not isinstance(text, str) or not text.strip():
            return None
    return replies


def _store_replies(refs: dict, campaign: dict, brand: dict) -> None:
    """Keep the texts filed with the campaign in ``refs['replies']``, the
    one source of the auto-replies. Texts already kept stay."""
    if _saved_replies(refs) is not None:
        return
    try:
        refs["replies"] = _filed_replies(campaign, brand, refs)
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        logger.warning(
            "10DLC filed replies not kept: campaign=%s %s",
            refs.get("campaign_id"),
            type(exc).__name__,
        )


def _policy_links(text: dict) -> dict:
    """File dedicated policy fields exactly as saved, without invented links."""
    links = {}
    if text.get("privacy_policy_url"):
        links["privacyPolicyLink"] = text["privacy_policy_url"]
    if text.get("terms_url"):
        links["termsAndConditionsLink"] = text["terms_url"]
    return links


class TelnyxSmsRegistrationProvider(SmsRegistrationProvider):
    name = "telnyx"
    campaign_ref = "campaign_id"
    # A low-volume brand Telnyx cannot verify can be vetted afterwards.
    faster_lane = True
    # subscriberOptin describes consent before sending, not keyword support.
    # Every self-service program requires consent and an actual confirmation.
    opt_in_message_required = True
    # Telnyx brand maximums. The person's first and last name are also
    # filed together as the brand name, under the same 100 (``_validate``).
    field_limits = MappingProxyType(
        {
            "legal_name": 100,
            "website": 100,
            "rep_email": 100,
            "first_name": 100,
            "last_name": 100,
            "email": 100,
            "opt_in_message": 255,
        }
    )

    def __init__(self, api_key=None, client=None) -> None:
        self._api = TelnyxClient(api_key, client)

    async def requirements(
        self, country_code, number_type, subject_type
    ) -> Requirements:
        return sms_requirements(self, subject_type)

    def validate_application(self, fields, address, subject_type) -> list[Problem]:
        problems = super().validate_application(fields, address, subject_type)
        first = (fields.get("first_name") or "").strip()
        last = (fields.get("last_name") or "").strip()
        if subject_type == "person" and text_length(f"{first} {last}") > 100:
            # Telnyx files both names as one brand name of at most 100.
            problems.append(
                Problem(
                    field="last_name",
                    message="First and last name together must fit in 100 characters.",
                )
            )
        # Outside the US and Canada, sender_problems runs the same check.
        issuer = (fields.get("registration_country") or "").strip().upper()
        if (
            subject_type == "business"
            and address
            and issuer in NINE_DIGIT_COUNTRIES
            and issuer != address.country_code.upper()
        ):
            problems.append(
                Problem(
                    field="registration_country",
                    message="This registration flow currently supports businesses whose registration country matches their address country. Contact support for this combination.",
                )
            )
        return problems

    async def create_draft(
        self,
        *,
        organization_id,
        contact_email,
        requirements,
        fields,
        address,
        documents,
        messaging_service_sid=None,
        kept_brand_id=None,
        new_brand_funded=False,
        low_volume=False,
    ) -> DraftResult:
        """``low_volume``: the API's choice for a company (no vetting). The
        API writes ``provider_refs.low_volume`` itself; the background pass
        reads it from there."""
        subject_type = requirements.subject_type
        # A company brand that gets vetting at filing.
        vetted = subject_type != "person" and not low_volume
        problems = self.validate_application(fields, address, subject_type)
        if problems:
            return DraftResult(refs={}, problems=problems)
        text = campaign_text(fields, subject_type)
        refs: dict = {
            "subject_type": subject_type,
            # Finds the organization's messaging profile later, when it
            # could not be found or made now.
            "organization_id": str(organization_id),
            # The sender name for the filed replies when the brand has none.
            "subject_name": sms_subject_name(fields, address, subject_type),
            "campaign": text.__dict__,
            "messaging_profile_id": messaging_service_sid,
        }
        if not messaging_service_sid:
            # The profile gets the filed STOP and HELP replies; the
            # organization's numbers find it by its name. Filing goes on
            # without it: _set_replies finds or makes it later.
            try:
                refs["messaging_profile_id"] = await TelnyxSmsProvider(
                    self._api
                ).ensure_messaging_service(organization_id, None)
            except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
                logger.warning(
                    "10DLC messaging profile not ready at filing: %s",
                    type(exc).__name__,
                )
        try:
            if (
                subject_type == "person"
                and kept_brand_id
                and await self._reuse_brand(refs, fields, kept_brand_id)
            ):
                return DraftResult(refs=refs)
            # Telnyx bills a new brand once made, so stop before _build.
            if kept_brand_id and not new_brand_funded:
                return DraftResult(refs={}, needs_new_brand_credit=True)
            problems = await self._build(refs, fields, address, subject_type, vetted)
        except Exception as exc:
            refused = _refused(exc)
            if refs.get("brand_id") and not refused:
                # Telnyx bills a brand once made, and a retry would file and
                # bill another: keep this one, whatever went wrong after it
                # (an outage, a body we could not read). The background pass
                # reads the brand, then sends the PIN or asks for vetting.
                logger.warning(
                    "10DLC step after the brand failed; brand kept: %s",
                    _failure(exc),
                )
                if vetted:
                    # False, not unset: rows filed before this flag existed
                    # already asked for vetting and must not ask again.
                    refs.setdefault("vetting_requested", False)
                return DraftResult(refs=refs)
            await self.discard_draft(refs)
            if refused:
                logger.warning(
                    "10DLC draft failed: status=%s errors=%s",
                    exc.response.status_code,
                    _error_log(exc.response),
                )
                return DraftResult(
                    refs={},
                    problems=_response_problems(exc.response, subject_type),
                    # Telnyx bills a brand once made, even one it refused.
                    billed_refs=_billed(refs),
                )
            raise VerificationProviderError("Telnyx request failed") from exc
        if problems:
            billed = _billed(refs)
            await self.discard_draft(refs)
            return DraftResult(refs={}, problems=problems, billed_refs=billed)
        return DraftResult(refs=refs)

    def keeps_brand(self, refs) -> bool:
        """A person's confirmed brand stays when its campaign needs repair
        or was filed: it is paid for, and the campaign is what the carriers refused.
        The next filing with the same details reuses it, with no new brand
        fee and no new PIN. A failed brand has no ``identity_verified``
        (``status``), and an unconfirmed brand cannot be reused."""
        return bool(
            refs.get("brand_id")
            and (refs.get("campaign_id") or refs.get("campaign_needs_repair"))
            and refs.get("subject_type") == "person"
            and refs.get("identity_verified") is True
        )

    async def discard_rejected(self, refs) -> dict:
        if self.keeps_brand(refs):
            brand_id = refs["brand_id"]
            await self.discard({k: v for k, v in refs.items() if k != "brand_id"})
            return {"kept_brand_id": brand_id}
        await self.discard(refs)
        return {}

    async def _reuse_brand(self, refs, fields, brand_id) -> bool:
        """Put the kept brand in refs when Telnyx still holds it, confirmed,
        for the same person, with no campaign. False files a new brand
        instead, or stops with a 402 when the credit does not cover one
        (``new_brand_funded``). A sole proprietor brand takes one campaign,
        and ``status`` would adopt one still listed (a failed campaign
        delete). A brand that does not fit stays at Telnyx, which refuses to
        delete a brand for 3 months after its campaign."""
        try:
            brand = await self._api.request("GET", f"/10dlc/brand/{path_id(brand_id)}")
            if (
                not _same_person(brand, fields)
                or brand.get("status") != "OK"
                or brand.get("failureReasons")
                or brand.get("identityStatus") not in _VERIFIED
            ):
                return False
            found = await self._api.request(
                "GET", "/10dlc/campaign", params={"brandId": brand_id}
            )
        except httpx.HTTPStatusError as exc:
            # A brand Telnyx no longer holds (404, 410) files a new one. Any
            # other answer raises: an outage, a 429 or an auth error must not
            # buy a second brand.
            status = exc.response.status_code
            if status in (404, 410):
                logger.warning("10DLC kept brand lookup failed: status=%s", status)
                return False
            raise
        if found.get("records"):
            return False
        refs["brand_id"] = brand_id
        refs["identity_verified"] = True
        # The fee rater bills no submission for a reused brand.
        refs["brand_reused"] = True
        return True

    async def _build(
        self, refs, fields, address, subject_type, vetted=True
    ) -> list[Problem]:
        """File the brand, wait for Telnyx to register it, then send the
        person's PIN or ask for the company's vetting. Returns the problems
        of a refused brand. A brand still registering after the wait is
        kept: Telnyx bills it anyway, and the background pass sends the PIN
        or asks for vetting. So is a brand when a later call fails without
        refusing it (``create_draft``)."""
        if subject_type == "person":
            mobile = fields["mobile_phone_number"].strip()
            display = f"{fields['first_name'].strip()} {fields['last_name'].strip()}"
            body = {
                "entityType": "SOLE_PROPRIETOR",
                "firstName": fields["first_name"].strip(),
                "lastName": fields["last_name"].strip(),
                "email": fields["email"].strip(),
                "phone": mobile,
                "mobilePhone": mobile,
                "vertical": "COMMUNICATION",
            }
        else:
            display = fields["legal_name"]
            body = {
                "entityType": fields["business_type"],
                "email": fields["rep_email"],
                "phone": fields["rep_phone"],
                "vertical": fields["industry"],
                "ein": tax_number(fields["ein"], fields.get("registration_country")),
                "website": fields["website"],
            }
        body.update(
            displayName=display,
            companyName=display,
            street=address.street,
            city=address.city,
            state=address.region,
            postalCode=address.postal_code,
            country=address.country_code,
        )
        brand = await self._api.request("POST", "/10dlc/brand", json=body)
        refs["brand_id"] = brand["brandId"]
        brand = await self._settled_brand(refs["brand_id"])
        if brand is None:
            logger.warning("10DLC brand still registering after the wait")
            if vetted:
                # See create_draft: False means "ask on the next pass".
                refs["vetting_requested"] = False
            return []
        if brand.get("failureReasons") or brand.get("status") != "OK":
            return _reason_problems(_reasons(brand.get("failureReasons")), subject_type)
        # A failure here keeps the brand unless Telnyx refused a value
        # (create_draft). ``otp_sent_at`` stays unset for a person.
        if subject_type == "person":
            # No vetting for a sole proprietor; the PIN confirms the person.
            await self._send_pin(refs)
        elif vetted:
            await self._request_vetting(refs)
        return []

    async def _settled_brand(self, brand_id) -> dict | None:
        """The brand once Telnyx has registered (status OK) or refused it;
        None if it is still registering after ``_BRAND_READS`` reads."""
        for _ in range(_BRAND_READS):
            await asyncio.sleep(_BRAND_READ_GAP)
            try:
                brand = await self._api.request(
                    "GET", f"/10dlc/brand/{path_id(brand_id)}"
                )
            except httpx.HTTPStatusError as exc:
                # 404: not registered yet. 429 and 5xx: try the next read
                # rather than delete a brand that may be fine.
                code = exc.response.status_code
                if code not in (404, 429) and code < 500:
                    raise
                continue
            except (httpx.RequestError, ValueError):
                # No answer, or a body that is not JSON: read again.
                continue
            if brand.get("failureReasons") or brand.get("status") in (
                "OK",
                "REGISTRATION_FAILED",
            ):
                return brand
        return None

    async def _request_vetting(self, refs) -> None:
        await self._api.request(
            "POST",
            f"/10dlc/brand/{path_id(refs['brand_id'])}/externalVetting",
            # POST .../vetting does not exist (Telnyx answers 404 "URL not
            # found"); the API reference documents externalVetting with an
            # external vetting provider id (Aegis) and class.
            json={"evpId": "AEGIS", "vettingClass": "STANDARD"},
        )
        refs["vetting_requested"] = True

    async def _request_vetting_once(self, refs) -> None:
        """Ask for vetting unless Telnyx already has a request: each one is
        billed. A request that timed out or got a 5xx may have reached it.
        Telnyx lists only ``ACTIVE`` records unless ``vettingStatus`` names
        others, so the read names every request still running or passed (as
        error 10012 does): a ``PENDING`` one is not asked for twice."""
        try:
            found = await self._api.request(
                "GET",
                f"/10dlc/brand/{path_id(refs['brand_id'])}/externalVetting",
                params={"vettingStatus": ["PENDING", "UNSCORE", "ACTIVE"]},
            )
        except httpx.HTTPStatusError as exc:
            # 404: none listed. Any other failure: ask on the next pass.
            if exc.response.status_code != 404:
                raise
            found = []
        # A bare list (API reference); a "data" list is read too.
        if isinstance(found, dict):
            found = found.get("data")
        if isinstance(found, list) and found:
            refs["vetting_requested"] = True
            return
        try:
            await self._request_vetting(refs)
        except httpx.HTTPStatusError as exc:
            # 10012: a vetting is in progress or passed in the last 180 days.
            if not any(str(e.get("code")) == "10012" for e in _errors(exc.response)):
                raise
            refs["vetting_requested"] = True

    async def _faster_lane_failed(self, refs) -> bool:
        """True once the vetting a bought faster lane asked for ended and
        the brand is still not verified (the caller read it): ``FAILED`` or
        ``EXPIRED`` at once; ``ACTIVE`` once ``_IDENTITY_WAIT`` has passed
        since it was vetted (or the lane was bought, with no ``vettedDate``),
        so a brand read that lags behind the vetting is not held. A missing
        or other status (``PENDING``, ``UNSCORE``) is still running. Never
        once a campaign is filed or the brand was verified. Telnyx lists
        only ``ACTIVE`` records unless ``vettingStatus`` names others."""
        if (
            not refs.get("faster_lane_at")
            or not refs.get("vetting_requested")
            or refs.get("campaign_id")
            # Verified once: a later read that lags behind holds nothing.
            or refs.get("brand_verified")
        ):
            return False
        try:
            found = await self._api.request(
                "GET",
                f"/10dlc/brand/{path_id(refs['brand_id'])}/externalVetting",
                params={"vettingStatus": ["ACTIVE", "FAILED", "EXPIRED"]},
            )
        except httpx.HTTPStatusError as exc:
            # 404: none listed. Any other failure: ask on the next pass.
            if exc.response.status_code != 404:
                raise
            return False
        if isinstance(found, dict):
            found = found.get("data")
        if not isinstance(found, list):
            return False
        now = datetime.now(timezone.utc)
        for record in found:
            if not isinstance(record, dict):
                continue
            status = record.get("vettingStatus")
            if status in ("FAILED", "EXPIRED"):
                return True
            if status == "ACTIVE":
                since = _iso_time(record.get("vettedDate")) or _iso_time(
                    refs["faster_lane_at"]
                )
                if since is not None and now - since >= _IDENTITY_WAIT:
                    return True
        return False

    async def _send_pin(self, refs) -> None:
        """Ask Telnyx to text the person a PIN. The caller saves refs."""
        sent = await self._api.request(
            "POST",
            f"/10dlc/brand/{refs['brand_id']}/smsOtp",
            json={"pinSms": _PIN_SMS, "successSms": _SUCCESS_SMS},
        )
        refs["otp_reference_id"] = sent.get("referenceId")
        refs["otp_sent_at"] = datetime.now(timezone.utc).isoformat()
        # A new code clears the wrong-code count (the API caps it).
        refs["confirm_attempts"] = 0

    async def _resend_pin_if_old(self, refs) -> None:
        """Send a new PIN when the last one is over a day old (or its time is
        unknown)."""
        sent = refs.get("otp_sent_at")
        now = datetime.now(timezone.utc)
        if sent and now - datetime.fromisoformat(sent) < _OTP_RESEND_AFTER:
            return
        await self._send_pin(refs)

    def confirmation(self, refs) -> str | None:
        # Only once the PIN text was sent (a brand still registering when
        # filed gets its PIN from the background pass).
        if (
            refs.get("subject_type") == "person"
            and refs.get("identity_verified") is not True
            and refs.get("otp_sent_at")
        ):
            return "code"
        return None

    async def confirm(self, refs, code) -> list[Problem]:
        try:
            await self._api.request(
                "PUT",
                f"/10dlc/brand/{refs['brand_id']}/smsOtp",
                json={"otpPin": code},
            )
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status == 400:
                return [Problem(field="code", message=_WRONG_CODE)]
            logger.warning("10DLC PIN confirm failed: status=%s", status)
            raise VerificationProviderError("Telnyx request failed") from None
        except (httpx.HTTPError, KeyError) as exc:
            logger.warning("10DLC PIN confirm failed: %s", type(exc).__name__)
            raise VerificationProviderError("Telnyx request failed") from None
        return []

    def in_review(self, refs) -> bool:
        # A company's brand vetting is a review too.
        return super().in_review(refs) or bool(refs.get("vetting_requested"))

    async def check(self, refs) -> list[Problem]:
        return []

    async def submit(self, refs) -> None:
        # The brand is registered when created; nothing more to send.
        return None

    async def status(self, refs, *, may_file_campaign: bool = True) -> ProviderStatus:
        try:
            brand = await self._api.request("GET", f"/10dlc/brand/{refs['brand_id']}")
            if (
                brand.get("failureReasons")
                or brand.get("status") == "REGISTRATION_FAILED"
            ):
                # A failed brand is not kept, even with a campaign filed.
                refs.pop("identity_verified", None)
                return ProviderStatus(
                    state="rejected",
                    reason=brand_rejected_reason(
                        _brand_fixes(
                            _reasons(brand.get("failureReasons")),
                            refs.get("subject_type") or "business",
                        ),
                        _reasons(brand.get("failureReasons")),
                    ),
                    raw_reason=str(brand.get("failureReasons") or brand["status"]),
                )
            if brand.get("status") == "REGISTRATION_PENDING":
                # No PIN or vetting until Telnyx has registered the brand.
                return ProviderStatus(state="pending")
            person = refs.get("subject_type") == "person"
            verified = brand.get("identityStatus") in _VERIFIED
            if person and verified:
                refs["identity_verified"] = True
            if verified and refs.get("low_volume"):
                # The networks verified the company: the faster lane is no
                # longer owed, so the credit checks stop counting its fee.
                refs["brand_verified"] = True
            if verified and refs.pop("faster_lane_failed", None):
                # Verified after the failed faster lane held the row: the
                # hold ends with no customer step. Nothing is filed on this
                # pass, which got no credit check (a held row needs none):
                # the next pass files the campaign once the credit covers it.
                refs.pop("needs_information", None)
                return ProviderStatus(state="pending")
            # After a confirmed PIN the brand read may still lag behind it.
            if not verified and not refs.get("identity_verified"):
                if refs.get("faster_lane_failed"):
                    # Still held: each pass only reads the brand. No new
                    # request and no fee.
                    return ProviderStatus(
                        state="pending", reason=LOW_VOLUME_FASTER_LANE_FAILED
                    )
                if person:
                    await self._resend_pin_if_old(refs)
                elif _faster_lane_owed(refs, brand):
                    if not refs.get("faster_lane_ok"):
                        # Held: the customer turns on the faster lane with
                        # an amendment, and the next pass asks for vetting.
                        refs["needs_information"] = True
                        return ProviderStatus(
                            state="pending", reason=LOW_VOLUME_FASTER_LANE
                        )
                    if not may_file_campaign:
                        # The credit does not cover the vetting yet: a later
                        # pass buys it (the API checks it like a campaign).
                        return ProviderStatus(state="pending")
                    # The fee rater charges the vetting from this mark, so it
                    # is written only once Telnyx has the request. A failed
                    # request leaves the lane owed: the next pass asks again,
                    # and finds a request that reached Telnyx (a timeout).
                    await self._request_vetting_once(refs)
                    refs["faster_lane_at"] = datetime.now(timezone.utc).isoformat()
                elif refs.get("vetting_requested") is False and _vetted(refs):
                    # The brand was still registering when it was filed, or
                    # the vetting call failed (Telnyx may have got it).
                    await self._request_vetting_once(refs)
                elif await self._faster_lane_failed(refs):
                    # Held: no other step can verify this brand. The
                    # customer can cancel and register again. Hail keeps
                    # reading the brand (``faster_lane_failed`` keeps the row
                    # polled): if Telnyx verifies it later, the hold ends.
                    refs["needs_information"] = True
                    refs["faster_lane_failed"] = True
                    return ProviderStatus(
                        state="pending", reason=LOW_VOLUME_FASTER_LANE_FAILED
                    )
                return ProviderStatus(state="pending")
            if "campaign_id" not in refs:
                # A campaign filed by an earlier call whose answer was lost
                # (a timeout): take it. Each campaign sent for review is paid.
                if await self.recover_campaign(refs, brand):
                    await self._set_replies(refs)
                    return ProviderStatus(state="pending")
                # No text once the account closed (its answers are deleted): held.
                text = refs.get("campaign") or {}
                if text.get("schema_version", 1) < 2 or not text.get("opt_in_message"):
                    refs["needs_information"] = True
                    return ProviderStatus(state="pending", reason=LEGACY_APPLICATION)
                if not may_file_campaign:
                    # The credit does not cover the campaign fee yet.
                    return ProviderStatus(state="pending")
                replies = _campaign_replies(brand, refs)
                campaign = await self._api.request(
                    "POST",
                    "/10dlc/campaignBuilder",
                    json={
                        "brandId": refs["brand_id"],
                        "usecase": text["use_case"],
                        **(
                            {"subUsecases": list(text["text_types"])}
                            if text["use_case"] in _SUB_USE_CASES
                            else {}
                        ),
                        "description": text["description"],
                        "messageFlow": text["message_flow"],
                        **{
                            f"sample{i}": sample
                            for i, sample in enumerate(text["samples"], 1)
                        },
                        "embeddedLink": text["has_links"],
                        "embeddedPhone": text["has_phone"],
                        "numberPool": False,
                        "ageGated": text["age_gated"],
                        "directLending": text["direct_lending"],
                        "subscriberOptin": True,
                        **(
                            {"optinKeywords": ",".join(text["opt_in_keywords"])}
                            if text.get("opt_in_keywords")
                            else {}
                        ),
                        "subscriberOptout": True,
                        "subscriberHelp": True,
                        "optoutKeywords": _OPT_OUT,
                        "helpKeywords": _HELP,
                        **replies,
                        "termsAndConditions": bool(text.get("terms_url")),
                        **_policy_links(text),
                    },
                )
                refs["campaign_id"] = campaign["campaignId"]
                refs["replies"] = replies
                await self._set_replies(refs)
                return ProviderStatus(state="pending")
            campaign = await self._api.request(
                "GET", f"/10dlc/campaign/{refs['campaign_id']}"
            )
            # Rows filed before the texts were kept take them from Telnyx.
            _store_replies(refs, campaign, brand)
            await self._set_replies(refs)
        except (httpx.HTTPError, KeyError) as exc:
            if isinstance(exc, httpx.HTTPStatusError):
                if _refused(exc) and exc.request.url.path.endswith("/campaignBuilder"):
                    # The paid brand stays: the customer fixes the answers on
                    # this same registration (PATCH .../application), then
                    # the background pass files the campaign again.
                    refs["campaign_needs_repair"] = True
                    refs["needs_information"] = True
                    errors = _errors(exc.response)
                    logger.warning("10DLC campaign refused; held for answers")
                    return ProviderStatus(
                        state="pending",
                        reason=campaign_repair_reason(errors),
                        raw_reason=str(errors),
                    )
                logger.warning(
                    "10DLC status failed: status=%s errors=%s",
                    exc.response.status_code,
                    _error_log(exc.response),
                )
            raise VerificationProviderError("Telnyx request failed") from exc
        status = (campaign.get("campaignStatus") or "").upper()
        if status in _APPROVED:
            return self._approved(refs)
        if status in _REJECTED:
            refs["campaign_accepted"] = False
            refs["runtime_ready"] = False
            return ProviderStatus(
                state="rejected",
                reason=campaign_repair_reason(_reasons(campaign.get("failureReasons"))),
                raw_reason=(
                    str(campaign["failureReasons"])
                    if campaign.get("failureReasons")
                    else None
                ),
            )
        return ProviderStatus(state="pending")

    def step(self, refs) -> str | None:
        if refs.get("campaign_accepted") and not refs.get("replies_set"):
            return "runtime_setup"
        return super().step(refs)

    def _approved(self, refs) -> ProviderStatus:
        """Keep polling an accepted campaign until required replies are set.

        A transient setup failure never undoes carrier acceptance and never
        enables sending. Retry only the idempotent profile configuration.
        """
        refs["campaign_accepted"] = True
        refs["runtime_ready"] = bool(refs.get("replies_set"))
        if refs["runtime_ready"]:
            return ProviderStatus(state="approved")
        return ProviderStatus(
            state="pending",
            reason="The campaign is accepted. Required STOP and HELP responses are still being configured; Hail will retry automatically.",
        )

    async def _set_replies(self, refs) -> None:
        """Make the STOP and HELP auto-replies of the organization's
        messaging profile the texts filed with the campaign
        (``refs['replies']``). Once per registration; a failure is logged
        and the next status() tries again. Never raises."""
        if refs.get("replies_set"):
            return
        replies = _saved_replies(refs)
        if replies is None:
            logger.warning(
                "10DLC auto-replies not set: no filed texts kept, campaign=%s",
                refs.get("campaign_id"),
            )
            return
        profile = await self._reply_profile(refs)
        if not profile:
            return
        path = f"/messaging_profiles/{path_id(profile)}/autoresp_configs"
        try:
            found = await self._api.request(
                "GET", path, params={"country_code": _AUTO_REPLY_COUNTRY}
            )
            # The list is already for US; match on the op alone, and update
            # the first config of an op rather than add another.
            ids: dict = {}
            for c in found.get("data") or []:
                if isinstance(c, dict) and c.get("op") and c.get("id"):
                    ids.setdefault(c["op"], c["id"])
            for op, key, keywords in _AUTO_REPLIES:
                body = {
                    "op": op,
                    "keywords": keywords.split(","),
                    "resp_text": replies[key],
                    "country_code": _AUTO_REPLY_COUNTRY,
                }
                if op in ids:
                    await self._api.request(
                        "PUT", f"{path}/{path_id(ids[op])}", json=body
                    )
                else:
                    await self._api.request("POST", path, json=body)
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "10DLC auto-replies not set: status=%s errors=%s",
                exc.response.status_code,
                _error_log(exc.response),
            )
            return
        except (
            httpx.HTTPError,
            AttributeError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            logger.warning("10DLC auto-replies not set: %s", type(exc).__name__)
            return
        refs["replies_set"] = True

    async def _reply_profile(self, refs) -> str | None:
        """The messaging profile the auto-replies go on. A filing without
        one (filed before the draft made it, or making it failed) finds or
        makes the organization's ``hail-<org>`` profile and keeps its id.
        None, logged, when that fails or no organization was saved."""
        profile = refs.get("messaging_profile_id")
        if profile:
            return profile
        org = refs.get("organization_id")
        if not org:
            logger.warning(
                "10DLC auto-replies not set: no messaging profile or"
                " organization saved, campaign=%s",
                refs.get("campaign_id"),
            )
            return None
        try:
            profile = await TelnyxSmsProvider(self._api).ensure_messaging_service(
                org, None
            )
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "10DLC auto-replies not set: profile status=%s",
                exc.response.status_code,
            )
            return None
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            logger.warning("10DLC auto-replies not set: profile %s", type(exc).__name__)
            return None
        refs["messaging_profile_id"] = profile
        return profile

    async def recover_campaign(self, refs, brand=None) -> bool:
        if self.campaign_filed(refs):
            return True
        try:
            found = await self._api.request(
                "GET", "/10dlc/campaign", params={"brandId": refs["brand_id"]}
            )
        except httpx.HTTPError as exc:
            raise VerificationProviderError("Campaign discovery unavailable") from exc
        existing = [
            item
            for item in found.get("records") or []
            if isinstance(item, dict) and item.get("campaignId")
        ]
        if existing:
            refs["campaign_id"] = existing[0]["campaignId"]
            _store_replies(refs, existing[0], brand or {})
            refs.pop("needs_information", None)
            refs.pop("faster_lane_failed", None)
            return True
        return False

    async def application_identity(self, refs, subject_type) -> dict | None:
        if not refs.get("brand_id"):
            return None
        try:
            brand = await self._api.request(
                "GET", f"/10dlc/brand/{path_id(refs['brand_id'])}"
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise VerificationProviderError(
                "Sender identity lookup unavailable"
            ) from exc
        except (httpx.HTTPError, KeyError) as exc:
            raise VerificationProviderError(
                "Sender identity lookup unavailable"
            ) from exc
        if brand.get("identityStatus") not in _VERIFIED or brand.get("failureReasons"):
            return None
        country = brand.get("country")
        fields = {
            "sender_country": country,
            "has_tax_id": "NO" if subject_type == "person" else "YES",
        }
        mapping = (
            {
                "first_name": "firstName",
                "last_name": "lastName",
                "email": "email",
                "mobile_phone_number": "mobilePhone",
            }
            if subject_type == "person"
            else {
                "legal_name": "companyName",
                "ein": "ein",
                "business_type": "entityType",
                "industry": "vertical",
                "website": "website",
                "rep_email": "email",
                "rep_phone": "phone",
            }
        )
        fields.update(
            {key: brand[value] for key, value in mapping.items() if brand.get(value)}
        )
        if subject_type == "business":
            fields["registration_country"] = country
        return {
            "fields": fields,
            "address": {
                "street": brand.get("street"),
                "city": brand.get("city"),
                "region": brand.get("state"),
                "postal_code": brand.get("postalCode"),
                "country_code": country,
            },
        }

    async def purchase_handle(self, refs) -> dict:
        return {}

    def subject_name(self, requirements, fields, address, documents) -> str | None:
        """``legal_name`` for a business, ``first_name`` and ``last_name``
        for a person, else the address's ``customer_name``."""
        return sms_subject_name(fields, address, requirements.subject_type)

    async def fetch_subject_name(self, refs, subject_type) -> str | None:
        """The brand: ``firstName`` and ``lastName`` for a person, else
        ``companyName`` (Hail files the same name as ``displayName``)."""
        if not refs.get("brand_id"):
            return None
        try:
            brand = await self._api.request(
                "GET", f"/10dlc/brand/{path_id(refs['brand_id'])}"
            )
        except httpx.HTTPError as exc:
            raise VerificationProviderError("Telnyx request failed") from exc
        if subject_type == "person":
            name = display_name(brand.get("firstName"), brand.get("lastName"))
            if name:
                return name
        return display_name(brand.get("companyName")) or display_name(
            brand.get("displayName")
        )

    async def detach_number(self, refs, *, provider_resource_id, e164) -> bool:
        """Unlink a number from its 10DLC campaign. Not linked (404) is
        done; any other failure raises. The number stays on the
        organization's messaging profile."""
        try:
            await self._api.request(
                "DELETE", f"/10dlc/phone_number_campaigns/{path_id(e164)}"
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                logger.warning(
                    "10DLC unlink failed: status=%s", exc.response.status_code
                )
                raise
        except json.JSONDecodeError:
            # A 2xx with a body that does not parse: the link is gone.
            pass
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("10DLC unlink failed: %s", type(exc).__name__)
            raise
        return False

    async def attach_number(self, refs, *, provider_resource_id, e164) -> None:
        """Link the number to the campaign. A person's campaign holds one
        number: another number already on it is refused, and the same
        number already linked (``ASSIGNED``) is a success (a link that timed
        out may have gone through). The same number in any other holding
        status (pending, a failed unlink, missing, unknown) is linked again,
        as if it were not listed; a 409 on that link is a success."""
        # No campaign id: the link below raises.
        if refs.get("subject_type") == "person" and refs.get("campaign_id"):
            held = await self._campaign_numbers(refs["campaign_id"])
            own = held.pop(_digits(e164), None)
            if own is not None and own[1] == _LINKED:
                return
            if held:
                logger.warning(
                    "10DLC: campaign %s already holds a number", refs["campaign_id"]
                )
                listed = ", ".join(
                    f"{number} ({status or 'no status'})"
                    for number, status in sorted(held.values())
                )
                message = (
                    "a person's registration holds one number: "
                    f"Telnyx has {listed} on the campaign"
                )
                if len(held) == 1:
                    digits, (_, status) = next(iter(held.items()))
                    if status == _LINKED:
                        # The caller records it as the registration's number
                        # when it is one of the organization's.
                        raise RegistrationHoldsAnotherNumber(message, "+" + digits)
                raise VerificationProviderError(message)
        try:
            await self._api.request(
                "POST",
                "/10dlc/phone_number_campaigns",
                json={"phoneNumber": e164, "campaignId": refs["campaign_id"]},
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 409:  # already linked
                return
            logger.warning("10DLC attach failed: status=%s", exc.response.status_code)
            raise VerificationProviderError(
                f"status {exc.response.status_code}: {_error_log(exc.response)}"
            ) from exc
        except (httpx.HTTPError, KeyError) as exc:
            raise VerificationProviderError("Telnyx request failed") from exc

    async def _campaign_numbers(
        self, campaign_id: str
    ) -> dict[str, tuple[str, str | None]]:
        """The numbers that hold the campaign, keyed by their digits, with
        their ``assignmentStatus``. A failed link (``FAILED_ASSIGNMENT``) and
        a number being unlinked (``PENDING_UNASSIGNMENT``) do not hold it;
        every other status does, a missing or unknown one too. Reads every
        page. Raises when Telnyx cannot be read, or serves a page of records
        it already served (a ``page`` it ignores), so a link is never sent
        blind."""
        held: dict[str, tuple[str, str | None]] = {}
        listed: set[tuple[str, str]] = set()
        seen = 0
        for page in range(1, _MAX_LIST_PAGES + 1):
            try:
                body = await self._api.request(
                    "GET",
                    "/10dlc/phone_number_campaigns",
                    params={
                        "filter[telnyx_campaign_id]": campaign_id,
                        "recordsPerPage": _LIST_PAGE_SIZE,
                        "page": page,
                    },
                )
                records = body.get("records") or []
                total = body.get("totalRecords")
            except httpx.HTTPStatusError as exc:
                logger.warning(
                    "10DLC campaign numbers not read: status=%s",
                    exc.response.status_code,
                )
                raise VerificationProviderError(
                    f"status {exc.response.status_code}: {_error_log(exc.response)}"
                ) from exc
            except (httpx.HTTPError, ValueError, AttributeError) as exc:
                raise VerificationProviderError("Telnyx request failed") from exc
            keys = {
                (str(r.get("phoneNumber")), str(r.get("campaignId")))
                for r in records
                if isinstance(r, dict)
            }
            if keys and keys <= listed:
                raise VerificationProviderError(
                    f"Telnyx page {page} of campaign numbers repeats an earlier page"
                )
            listed |= keys
            for record in records:
                if not isinstance(record, dict):
                    continue
                # Checked here too, in case Telnyx ignores the filter.
                if campaign_id not in (
                    record.get("campaignId"),
                    record.get("telnyxCampaignId"),
                ):
                    continue
                status = record.get("assignmentStatus")
                if status in _RELEASES_CAMPAIGN:
                    continue
                number = str(record.get("phoneNumber") or "")
                if _digits(number):
                    held[_digits(number)] = (number, status)
            seen += len(records)
            if not records or not isinstance(total, int) or seen >= total:
                return held
        raise VerificationProviderError(
            f"Telnyx lists more than {_MAX_LIST_PAGES} pages of campaign numbers"
        )

    async def discard(self, refs) -> None:
        steps = (
            ("campaign", refs.get("campaign_id")),
            ("brand", refs.get("brand_id")),
        )
        failures = DiscardFailures("telnyx 10DLC")
        for key, resource_id in steps:
            if not resource_id:
                continue
            try:
                await self._api.request("DELETE", f"/10dlc/{key}/{resource_id}")
            except Exception as exc:
                failures.add(exc, key)
        failures.raise_any("Telnyx discard failed")


def _same_person(brand: dict, fields: dict) -> bool:
    """The brand names the person on the form. Case, spacing and the format
    of the mobile do not count."""

    def text(value: str | None) -> str:
        return (value or "").strip().casefold()

    return _digits(brand.get("mobilePhone")) == _digits(
        fields.get("mobile_phone_number")
    ) and all(
        text(brand.get(key)) == text(fields.get(field))
        for key, field in (
            ("firstName", "first_name"),
            ("lastName", "last_name"),
            ("email", "email"),
        )
    )


def _digits(value: str | None) -> str:
    return re.sub(r"\D", "", value or "")


def _refused(exc: Exception) -> bool:
    """Recognize definite input refusals; auth/configuration stays retryable."""
    if not isinstance(exc, httpx.HTTPStatusError):
        return False
    errors = _errors(exc.response)
    # Application payload fields are top-level. Keep both brand and campaign
    # validation evidence without interpreting arbitrary operational paths.
    # Telnyx points at campaign fields as "/body/<field>" too (10015).
    known_field = any(
        (_pointer(error) or "").removeprefix("/body") in _APPLICATION_POINTERS
        for error in errors
    )
    # Every text key: the outage check reads the same words that get quoted.
    prose = " ".join(_all_text(error) for error in errors)
    if known_field:
        # An error on a form field is the customer's unless the text names
        # Hail's account; there "permission" is a form word, as on Twilio
        # ("Opt-in permission language missing"). An error off the form
        # that says "permission" still names Hail's account.
        off_form = " ".join(
            _all_text(e)
            for e in errors
            if (_pointer(e) or "").removeprefix("/body") not in _APPLICATION_POINTERS
        )
        return (
            exc.response.status_code in (400, 422)
            and not account_problem(prose)
            and "permission" not in off_form.lower()
        )
    return application_validation_error(
        exc.response.status_code, prose, known_field=known_field
    )


def _failure(exc: Exception) -> str:
    """A failure for the log: the status code, else the exception's type."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"status={exc.response.status_code}"
    return type(exc).__name__


def _vetted(refs: dict) -> bool:
    """A company brand that gets vetting: never a person's, and a
    low-volume company's only once its faster lane asked for it."""
    return refs.get("subject_type") != "person" and (
        not refs.get("low_volume") or bool(refs.get("faster_lane_at"))
    )


def _iso_time(value) -> datetime | None:
    """An ISO 8601 time as an aware datetime (UTC when it names no zone);
    None for anything else."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _faster_lane_owed(refs: dict, brand: dict) -> bool:
    """A low-volume company brand Telnyx did not verify, with no faster lane
    asked yet. ``UNVERIFIED`` is a verdict. Telnyx does not document which
    other values are final, so any other value that is not verified
    (``SELF_DECLARED``, a missing or new one) counts once it has lasted
    ``_IDENTITY_WAIT`` from the first pass that saw it
    (``identity_wait_since``): never pending forever. Never once a campaign
    is filed: a hold then could not be amended or polled again. The caller
    only asks for a registered brand that is not verified."""
    if (
        refs.get("subject_type") == "person"
        or not refs.get("low_volume")
        or refs.get("faster_lane_at")
        or refs.get("brand_verified")
        or refs.get("campaign_id")
    ):
        return False
    if brand.get("identityStatus") == "UNVERIFIED":
        return True
    now = datetime.now(timezone.utc)
    since = refs.setdefault("identity_wait_since", now.isoformat())
    return now - datetime.fromisoformat(since) >= _IDENTITY_WAIT


def _billed(refs: dict) -> dict:
    """What Telnyx bills of a draft that failed: the brand, once made."""
    if not refs.get("brand_id"):
        return {}
    return {"brand_id": refs["brand_id"], "subject_type": refs.get("subject_type")}


def _errors(response: httpx.Response) -> list[dict]:
    try:
        errors = response.json().get("errors") or []
    except (ValueError, AttributeError):
        return []
    if not isinstance(errors, list):
        return []
    return [e for e in errors if isinstance(e, dict)]


def _pointer(error: dict) -> str | None:
    """The field a Telnyx error names (``source.pointer``), if any."""
    source = error.get("source")
    pointer = source.get("pointer") if isinstance(source, dict) else None
    return str(pointer) if pointer else None


def _detail(error: dict) -> str | None:
    for key in ("detail", "title"):
        if isinstance(error.get(key), str) and error[key].strip():
            return error[key]
    return None


def _error_log(response: httpx.Response) -> list:
    """Telnyx's errors, for the log: code, field and text. The text can quote
    the customer's details; logged anyway (owner decision 2026-10-28), since
    the code alone does not say which field Telnyx refused."""
    return [
        {"code": e.get("code"), "field": _pointer(e), "detail": _detail(e)}
        for e in _errors(response)
    ]


def _response_problems(response: httpx.Response, subject_type) -> list[Problem]:
    """One problem per form field Telnyx points at (``source.pointer``),
    each with Telnyx's own words (a second error on the same field adds its
    words to that problem). Errors on no form field go in one general
    problem, so no reason is lost."""
    names = _FORM_FIELDS[subject_type]
    problems: list[Problem] = []
    unmatched: list[dict] = []
    for e in _errors(response):
        field = names.get((_pointer(e) or "").rsplit("/", 1)[-1])
        if not field:
            unmatched.append(e)
            continue
        problem = next((p for p in problems if p.field == field), None)
        text = _original_text(e)
        # A reused email or phone gets its own sentence, as at brand refusal.
        lead = _DUPLICATE.get(field) if "duplicate" in _all_text(e).lower() else None
        if problem is None:
            problems.append(
                Problem(
                    field=field,
                    message=with_carrier_text(
                        lead or SMS_VALUE_REFUSED, _one_line(text)
                    ),
                    raw=_one_line(text),
                )
            )
        else:
            _add_carrier_text(problem, text, lead)
    if not problems:
        originals = [
            t for t in map(_one_line, map(_original_text, _errors(response))) if t
        ]
        return [
            Problem(
                field="",
                message=_safe_message(response, subject_type),
                raw="; ".join(originals) or None,
            )
        ]
    if with_carrier_text("", unmatched):
        problems.append(_general_problem(unmatched, subject_type))
    return problems


def _add_carrier_text(
    problem: Problem, text: str | None, lead: str | None = None
) -> None:
    """Another carrier reason on the same field: its words join that
    problem, next to the field they name. The quote is built again from all
    of them, so the cap and tag cleanup cover the whole of it. ``lead``
    replaces Hail's sentence (a reused email or phone found later)."""
    # One reason per line: a reason's own "; " must not split it.
    texts = problem.raw.split("\n") if problem.raw else []
    text = _one_line(text)
    if text and text not in texts:
        texts.append(text)
    head = lead or problem.message.split(" The carrier said: ", 1)[0]
    problem.message = with_carrier_text(head, texts)
    problem.raw = "\n".join(texts) or None


def _field_names(reason: dict) -> list[str]:
    """The form fields a Telnyx reason names: a list, or one name as a string."""
    fields = reason.get("fields")
    if isinstance(fields, str):
        return [fields]
    if not isinstance(fields, (list, tuple)):
        return []
    return [f for f in fields if isinstance(f, str)]


def _one_line(text: str | None) -> str | None:
    """``text`` on one line: a problem's ``raw`` holds one reason per line,
    so a reason's own line breaks would split it on the way back."""
    return " ".join(text.split()) or None if isinstance(text, str) else None


def _reasons(value: object) -> object:
    """Telnyx ``failureReasons``: a list of dicts, a plain string, or that
    list sent as JSON text. JSON text is parsed, so its descriptions are
    quoted, not the raw payload."""
    if isinstance(value, str) and value.strip()[:1] in ("[", "{"):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _original_text(item: object) -> str | None:
    """The carrier's own text in one reason or error, field tag and all."""
    if isinstance(item, str):
        return item.strip() or None
    if isinstance(item, dict):
        # The same key the quote uses: the first whose text survives cleanup.
        return next(
            (
                item[k]
                for k in TEXT_KEYS
                if isinstance(item.get(k), str) and carrier_texts(item[k])
            ),
            None,
        )
    return None


def _all_text(item: object) -> str:
    """Every text key of one reason or error joined, for the detail match:
    Telnyx can name the detail in ``title`` and explain it in ``detail``."""
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        return " ".join(item[k] for k in TEXT_KEYS if isinstance(item.get(k), str))
    return ""


def _text_fixes(texts: list[str], subject_type: str) -> list[str]:
    """Hail's sentences for carrier texts on no form field: the reuse
    sentence for a reused email or phone, the detail sentence for the rest."""
    fixes = [f for t in texts if (f := _duplicate_by_text(t, subject_type))]
    # The detail sentence for every text, a reused one too ("Email duplicate
    # ...; EIN does not match"), minus the email fix the reuse one covers.
    details = [
        f
        for f in detail_fixes(texts)
        if not (fixes and f == "Check the email address.")
    ]
    return list(dict.fromkeys(fixes + details))


def _general_problem(unmatched: list, subject_type: str) -> Problem:
    """One problem for carrier reasons on no form field: Hail's sentence for
    each detail they name (else the generic one), then their words. ``raw``
    keeps the carrier's own text, so the audit row has what was shown."""
    originals = [t for t in map(_one_line, map(_original_text, unmatched)) if t]
    fixes = _text_fixes([_all_text(item) for item in unmatched], subject_type)
    return Problem(
        field="",
        message=with_carrier_text(" ".join(fixes) or SMS_REFUSED, unmatched),
        raw="; ".join(originals) or None,
    )


def _brand_fixes(reasons, subject_type) -> list[str]:
    """Hail's fixes for a brand refused after filing, as at filing: the
    reuse sentence for a reused email or phone, and the detail sentence
    (tax number, website, address...) for any other reason."""
    items = reasons if isinstance(reasons, list) else [reasons]
    # The text plus the fields it names, so a bare "Invalid value" on the
    # EIN field still gets the tax number sentence.
    # A duplicate the reuse sentence covers is left out; any other reason,
    # an unmapped duplicate too ("Duplicate EIN"), gets its detail fix.
    others = [
        f"{_all_text(r)} {_named_fields(r, subject_type)}"
        for r in items
        if not _duplicate_fixes([r], subject_type)
    ]
    fixes = _duplicate_fixes(reasons, subject_type)
    return fixes + [f for f in detail_fixes(others) if f not in fixes]


def _named_fields(reason: object, subject_type: str) -> str:
    """The form fields a reason lists, as words: Telnyx's name mapped to
    Hail's form field ("postalCode" is "address postal code"), else split
    at camelCase, dots and underscores."""
    if not isinstance(reason, dict):
        return ""
    names = _FORM_FIELDS.get(subject_type, {})
    return " ".join(
        re.sub(r"(?<=[a-z])(?=[A-Z])|[._]", " ", names.get(n, n)).lower()
        for n in _field_names(reason)
    )


def _duplicate_by_text(text: str, subject_type: str) -> str | None:
    """The reuse sentence for a duplicate reason that names no form field,
    read from its words, for the form this filing used (a person's mobile
    number, a company's representative phone)."""
    low = text.lower()
    if "duplicate" not in low:
        return None
    names = _FORM_FIELDS.get(subject_type, {})
    if re.search(r"\bemail\b", low):
        return _DUPLICATE.get(names.get("email", ""))
    if re.search(r"\b(?:mobile|phone)\b", low):
        return _DUPLICATE.get(names.get("phone", ""))
    return None


def _duplicate_fixes(reasons, subject_type) -> list[str]:
    """Hail's sentence for each reused phone or email in ``failureReasons``
    (the same sentences ``_reason_problems`` gives at filing)."""
    names = _FORM_FIELDS.get(subject_type, {})
    fixes: list[str] = []
    items = reasons if isinstance(reasons, list) else [reasons]
    for reason in items:
        if not isinstance(reason, dict):
            # A plain text reason names no field: read it by its words.
            fix = _duplicate_by_text(_all_text(reason), subject_type)
            if fix and fix not in fixes:
                fixes.append(fix)
            continue
        if "duplicate" not in _all_text(reason).lower():
            continue
        found = [_DUPLICATE.get(names.get(n) or "") for n in _field_names(reason)]
        found = [f for f in found if f] or [
            _duplicate_by_text(_all_text(reason), subject_type)
        ]
        for fix in found:
            if fix and fix not in fixes:
                fixes.append(fix)
    return fixes


def _reason_problems(reasons, subject_type) -> list[Problem]:
    """A refused brand's ``failureReasons`` as problems on the form fields
    they name, each with Telnyx's own words. A reused phone or email gets
    its own sentence."""
    names = _FORM_FIELDS[subject_type]
    problems: list[Problem] = []
    unmatched: list[object] = []
    items = reasons if isinstance(reasons, list) else [reasons]
    for reason in items:
        if not isinstance(reason, dict):
            unmatched.append(reason)
            continue
        # raw keeps the carrier's own text, field tag and all; the message
        # quotes it cleaned (with_carrier_text).
        original = _original_text(reason)
        shown = False
        for name in _field_names(reason):
            field = names.get(name)
            if not field:
                continue
            shown = True
            problem = next((p for p in problems if p.field == field), None)
            if problem is not None:
                duplicate = (
                    "duplicate" in _all_text(reason).lower() and field in _DUPLICATE
                )
                _add_carrier_text(
                    problem, original, _DUPLICATE[field] if duplicate else None
                )
                continue
            message = SMS_VALUE_REFUSED
            if "duplicate" in _all_text(reason).lower():
                message = _DUPLICATE.get(field, message)
            problems.append(
                Problem(
                    field=field,
                    message=with_carrier_text(message, _one_line(original)),
                    raw=_one_line(original),
                )
            )
        if not shown:
            # On no form field: its text goes in the general problem, so no
            # reason is lost.
            unmatched.append(reason)
    if not problems or with_carrier_text("", unmatched):
        problems.append(_general_problem(unmatched, subject_type))
    return problems


def _safe_message(response: httpx.Response, subject_type: str) -> str:
    """Hail's sentence for each detail Telnyx names (``_text_fixes``: a
    reused email or phone gets the reuse sentence), then Telnyx's words."""
    errors = _errors(response)
    fixes = _text_fixes([_all_text(e) for e in errors], subject_type)
    return with_carrier_text(" ".join(fixes) or SMS_REFUSED, errors)
