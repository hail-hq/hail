"""Telnyx plug-in for US A2P 10DLC registration: a brand, optional vetting,
then a campaign; numbers are linked to the campaign one by one.

A person (sole proprietor) brand waits for a PIN Telnyx texts to the
person's mobile. The person types it into Hail, ``confirm`` sends it to
Telnyx, and the campaign is then created as for a company. The PIN is never
stored or logged."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone

import httpx
from hailhq.core.providers.telnyx import TelnyxClient, path_id
from hailhq.core.providers.verification.base import (
    BRAND_REJECTED,
    CAMPAIGN_REJECTED,
    VALUE_NOT_ACCEPTED,
    DraftResult,
    Problem,
    ProviderStatus,
    Requirements,
    SmsRegistrationProvider,
    VerificationProviderError,
)
from hailhq.core.providers.verification.forms import display_name
from hailhq.core.providers.verification.sms_form import (
    campaign_text,
    field_problems,
    sms_requirements,
    sms_subject_name,
)

logger = logging.getLogger(__name__)

_EIN = re.compile(r"^\d{2}-?\d{7}$")
_OPT_OUT = "STOP,STOPALL,UNSUBSCRIBE,CANCEL,END,QUIT"
_HELP = "HELP,INFO"
_VERIFIED = {"VERIFIED", "VETTED_VERIFIED"}
# Texts Telnyx sends a person: the PIN (@OTP_PIN@ is replaced by Telnyx),
# then a notice once the PIN is confirmed.
_PIN_SMS = "Hail: your confirmation code is @OTP_PIN@. It expires in 24 hours."
_SUCCESS_SMS = "Hail: your number is confirmed. Reply STOP to opt out."
# A PIN is valid this long; past it, status() sends a new one.
_OTP_RESEND_AFTER = timedelta(hours=24)
_REFUSED = "The carrier refused a detail. Check your entries and try again."
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
_DUPLICATE = {
    "mobile_phone_number": "This mobile number is on too many registrations. Use another one.",
    "email": "This email is on too many registrations. Use another one.",
    "rep_phone": "This phone number is on too many registrations. Use another one.",
    "rep_email": "This email is on too many registrations. Use another one.",
}


def _policy_links(text: dict) -> dict:
    """Privacy and terms links for the campaign. Drafts filed before these
    fields existed carry no links; they are filed without them."""
    links = {}
    if text.get("privacy_policy_url"):
        links["privacyPolicyLink"] = text["privacy_policy_url"]
    if text.get("terms_url"):
        links["termsAndConditionsLink"] = text["terms_url"]
    return links


class TelnyxSmsRegistrationProvider(SmsRegistrationProvider):
    name = "telnyx"

    def __init__(self, api_key=None, client=None) -> None:
        self._api = TelnyxClient(api_key, client)

    async def requirements(
        self, country_code, number_type, subject_type
    ) -> Requirements:
        return sms_requirements(self.name, subject_type)

    def _validate(self, fields, address, subject_type) -> list[Problem]:
        problems = field_problems(fields, subject_type)
        mobile = (fields.get("mobile_phone_number") or "").strip()
        if subject_type == "person" and mobile and not mobile.startswith("+1"):
            problems.append(
                Problem(
                    field="mobile_phone_number",
                    message="Use a US or Canadian mobile number.",
                )
            )
        if (
            subject_type == "business"
            and fields.get("ein")
            and not _EIN.match(fields["ein"])
        ):
            problems.append(
                Problem(field="ein", message="Nine digits, like 12-3456789.")
            )
        if address is None:
            problems.append(Problem(field="address", message="An address is required."))
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
    ) -> DraftResult:
        subject_type = requirements.subject_type
        problems = self._validate(fields, address, subject_type)
        if problems:
            return DraftResult(refs={}, problems=problems)
        text = campaign_text(fields, subject_type)
        refs: dict = {
            "subject_type": subject_type,
            "campaign": text.__dict__,
            "messaging_profile_id": messaging_service_sid,
        }
        try:
            problems = await self._build(refs, fields, address, subject_type)
        except Exception as exc:
            await self.discard(refs)
            if isinstance(exc, httpx.HTTPStatusError):
                code = exc.response.status_code
                logger.warning(
                    "10DLC draft failed: status=%s codes=%s",
                    code,
                    _error_codes(exc.response),
                )
                if 400 <= code < 500:
                    return DraftResult(
                        refs={},
                        problems=_response_problems(exc.response, subject_type),
                    )
            raise VerificationProviderError("Telnyx request failed") from exc
        if problems:
            await self.discard(refs)
            return DraftResult(refs={}, problems=problems)
        return DraftResult(refs=refs)

    async def _build(self, refs, fields, address, subject_type) -> list[Problem]:
        """File the brand, wait for Telnyx to register it, then send the
        person's PIN or ask for the company's vetting. Returns the problems
        of a refused brand. A brand still registering after the wait is
        kept; the background pass sends the PIN or asks for vetting."""
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
                "ein": fields["ein"].replace("-", ""),
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
            if subject_type != "person":
                # False, not unset: rows filed before this flag existed
                # already asked for vetting and must not ask again.
                refs["vetting_requested"] = False
            return []
        if brand.get("failureReasons") or brand.get("status") != "OK":
            return _reason_problems(brand.get("failureReasons"), subject_type)
        if subject_type == "person":
            # No vetting for a sole proprietor; the PIN confirms the person.
            await self._send_pin(refs)
        else:
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
            except httpx.TransportError:
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
            f"/10dlc/brand/{refs['brand_id']}/vetting",
            json={"vettingProvider": "AEGIS", "vettingClass": "STANDARD"},
        )
        refs["vetting_requested"] = True

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
        if (
            refs.get("subject_type") == "person"
            and refs.get("identity_verified") is not True
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

    async def check(self, refs) -> list[Problem]:
        return []

    async def submit(self, refs) -> None:
        # The brand is registered when created; nothing more to send.
        return None

    async def status(self, refs) -> ProviderStatus:
        try:
            brand = await self._api.request("GET", f"/10dlc/brand/{refs['brand_id']}")
            if (
                brand.get("failureReasons")
                or brand.get("status") == "REGISTRATION_FAILED"
            ):
                return ProviderStatus(
                    state="rejected",
                    reason=BRAND_REJECTED,
                    raw_reason=str(brand.get("failureReasons") or brand["status"]),
                )
            if brand.get("status") == "REGISTRATION_PENDING":
                # No PIN or vetting until Telnyx has registered the brand.
                return ProviderStatus(state="pending")
            person = refs.get("subject_type") == "person"
            verified = brand.get("identityStatus") in _VERIFIED
            if person and verified:
                refs["identity_verified"] = True
            # After a confirmed PIN the brand read may still lag behind it.
            if not verified and not refs.get("identity_verified"):
                if person:
                    await self._resend_pin_if_old(refs)
                elif refs.get("vetting_requested") is False:
                    # The brand was still registering when it was filed.
                    await self._request_vetting(refs)
                return ProviderStatus(state="pending")
            if "campaign_id" not in refs:
                text = refs["campaign"]
                campaign = await self._api.request(
                    "POST",
                    "/10dlc/campaignBuilder",
                    json={
                        "brandId": refs["brand_id"],
                        "usecase": text["use_case"],
                        "description": text["description"],
                        "messageFlow": text["message_flow"],
                        "sample1": text["samples"][0],
                        "sample2": text["samples"][1],
                        "embeddedLink": text["has_links"],
                        "embeddedPhone": False,
                        "numberPool": False,
                        "ageGated": False,
                        "directLending": False,
                        "subscriberOptin": True,
                        "subscriberOptout": True,
                        "subscriberHelp": True,
                        "optoutKeywords": _OPT_OUT,
                        "helpKeywords": _HELP,
                        "helpMessage": "Reply STOP to opt out. For help contact hi@hail.so.",
                        "optinMessage": "You are opted in. Reply STOP to opt out, HELP for help.",
                        **_policy_links(text),
                    },
                )
                refs["campaign_id"] = campaign["campaignId"]
                return ProviderStatus(state="pending")
            campaign = await self._api.request(
                "GET", f"/10dlc/campaign/{refs['campaign_id']}"
            )
        except (httpx.HTTPError, KeyError) as exc:
            if isinstance(exc, httpx.HTTPStatusError):
                logger.warning(
                    "10DLC status failed: status=%s", exc.response.status_code
                )
            raise VerificationProviderError("Telnyx request failed") from exc
        status = (campaign.get("campaignStatus") or "").upper()
        if status in _APPROVED:
            return ProviderStatus(state="approved")
        if status in _REJECTED:
            return ProviderStatus(
                state="rejected",
                reason=CAMPAIGN_REJECTED,
                raw_reason=(
                    str(campaign["failureReasons"])
                    if campaign.get("failureReasons")
                    else None
                ),
            )
        return ProviderStatus(state="pending")

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
        """Unlink a number from its 10DLC campaign. Never raises. The
        number stays on the organization's messaging profile."""
        try:
            await self._api.request(
                "DELETE", f"/10dlc/phone_number_campaigns/{path_id(e164)}"
            )
        except httpx.HTTPStatusError as exc:
            logger.warning("10DLC unlink failed: status=%s", exc.response.status_code)
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("10DLC unlink failed: %s", type(exc).__name__)
        return False

    async def attach_number(self, refs, *, provider_resource_id, e164) -> None:
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
            raise VerificationProviderError("Telnyx request failed") from exc
        except (httpx.HTTPError, KeyError) as exc:
            raise VerificationProviderError("Telnyx request failed") from exc

    async def discard(self, refs) -> None:
        steps = (
            ("campaign", refs.get("campaign_id")),
            ("brand", refs.get("brand_id")),
        )
        for key, resource_id in steps:
            if not resource_id:
                continue
            try:
                await self._api.request("DELETE", f"/10dlc/{key}/{resource_id}")
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404:
                    logger.warning(
                        "10DLC discard failed: step=%s status=%s",
                        key,
                        exc.response.status_code,
                    )
            except Exception:
                logger.warning("10DLC discard failed: step=%s", key)


def _errors(response: httpx.Response) -> list[dict]:
    try:
        errors = response.json().get("errors") or []
    except (ValueError, AttributeError):
        return []
    if not isinstance(errors, list):
        return []
    return [e for e in errors if isinstance(e, dict)]


def _error_codes(response: httpx.Response) -> list:
    """Telnyx's error codes, for the log. Never its text: it can quote the
    customer's details."""
    return [e.get("code") for e in _errors(response)]


def _response_problems(response: httpx.Response, subject_type) -> list[Problem]:
    """One problem per form field Telnyx points at (``source.pointer``),
    else one neutral problem."""
    names = _FORM_FIELDS[subject_type]
    problems: list[Problem] = []
    for e in _errors(response):
        source = e.get("source")
        pointer = str(source.get("pointer") or "") if isinstance(source, dict) else ""
        field = names.get(pointer.rsplit("/", 1)[-1])
        if field and all(p.field != field for p in problems):
            problems.append(
                Problem(
                    field=field,
                    message=VALUE_NOT_ACCEPTED,
                    raw=e.get("detail") or e.get("title"),
                )
            )
    return problems or [Problem(field="", message=_safe_message(response))]


def _reason_problems(reasons, subject_type) -> list[Problem]:
    """A refused brand's ``failureReasons`` as problems on the form fields
    they name. A reused phone or email gets its own sentence."""
    names = _FORM_FIELDS[subject_type]
    problems: list[Problem] = []
    for reason in reasons or []:
        if not isinstance(reason, dict):
            continue
        text = str(reason.get("description") or "")
        for name in reason.get("fields") or []:
            field = names.get(name)
            if not field or any(p.field == field for p in problems):
                continue
            message = VALUE_NOT_ACCEPTED
            if "duplicate" in text.lower():
                message = _DUPLICATE.get(field, message)
            problems.append(Problem(field=field, message=message, raw=text or None))
    return problems or [Problem(field="", message=_REFUSED)]


def _safe_message(response: httpx.Response) -> str:
    """A short neutral sentence; never the carrier's own text."""
    low = " ".join(
        f"{e.get('title', '')} {e.get('detail', '')}" for e in _errors(response)
    ).lower()
    for words, text in (
        (("ein", "tax id", "registration number"), "Check the tax ID (EIN)."),
        (("website", "url"), "Check the website address."),
        (("address", "street", "postal", "zip"), "Check the address."),
        (("phone", "mobile"), "Check the phone number."),
    ):
        if any(re.search(rf"\b{w}\b", low) for w in words):
            return text
    return _REFUSED
