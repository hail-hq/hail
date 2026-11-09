"""Twilio plug-in for US A2P 10DLC registration under Hail's account (ISV
model): a customer profile, an A2P trust product, a brand, then a campaign
on the organization's messaging service. This module is the only place
that knows those words."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone

import requests
from hailhq.core.config import settings
from hailhq.core.providers.sms.base import SmsProvisioningError
from hailhq.core.providers.sms.twilio import TwilioSmsProvider
from hailhq.core.providers.verification.base import (
    BRAND_REJECTED,
    SMS_REFUSED,
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
    sms_requirements,
    sms_subject_name,
    tax_number,
)
from hailhq.core.providers.verification.sms_review import (
    LEGACY_APPLICATION,
    application_validation_error,
    campaign_repair_reason,
)
from twilio.base.exceptions import TwilioException, TwilioRestException
from twilio.rest import Client as TwilioClient

logger = logging.getLogger(__name__)

# Twilio's business_registration_identifier. Its per-country types (CN, ACN,
# CIN, ...) each name one kind of number, but the form takes a VAT or a
# company number, so every country outside the US and Canada files "Other".
# sender_problems keeps that country equal to the address country.
_REGISTRATION_IDS = {"US": "EIN", "CA": "CBN"}
_REGISTRATION_COUNTRIES = {v: k for k, v in _REGISTRATION_IDS.items()}

_SECONDARY_PROFILE_POLICY = "RNdfbf3fae0e1107f8aded0e7cead80bf5"
_STARTER_PROFILE_POLICY = "RN806dd6cd175f314e1f96a9727ee271f4"
_A2P_TRUST_POLICY = "RNb0d4771c2c98518d916a3d4cd70a8f8b"
_SOLE_PROP_TRUST_POLICY = "RN670d5d2e282a6130ae063b234b6019c8"
# A person's brand waits for them to confirm the code sent to their mobile.
# Past this age, the code is sent again.
_OTP_RESEND_AFTER = timedelta(hours=24)


# The SDK lets requests' own transport errors through: both are an outage.
_TWILIO_ERRORS = (TwilioException, requests.RequestException)


class _NotCompliant(TwilioRestException):
    """A Trust Hub evaluation found a detail of the form not compliant: a
    definite refusal of the customer's answers, whatever its wording."""


def _policy_links(text: dict) -> dict:
    """File dedicated policy fields exactly as saved, without invented links."""
    links = {}
    if text.get("privacy_policy_url"):
        links["privacy_policy_url"] = text["privacy_policy_url"]
    if text.get("terms_url"):
        links["terms_and_conditions_url"] = text["terms_url"]
    return links


class TwilioSmsRegistrationProvider(SmsRegistrationProvider):
    name = "twilio"
    campaign_ref = "campaign_sid"

    def __init__(
        self,
        account_sid=None,
        auth_token=None,
        client=None,
        primary_profile_sid=None,
        sms_provider=None,
    ) -> None:
        self._primary_profile_sid = (
            primary_profile_sid or settings.twilio_primary_profile_sid
        )
        if not self._primary_profile_sid:
            raise ValueError(
                "TwilioSmsRegistrationProvider needs TWILIO_PRIMARY_PROFILE_SID"
            )
        self.account_sid = account_sid or settings.twilio_account_sid
        token = auth_token or settings.twilio_auth_token
        if client is None:
            if not self.account_sid or not token:
                raise ValueError(
                    "TwilioSmsRegistrationProvider needs Twilio credentials"
                )
            client = TwilioClient(self.account_sid, token)
        self._client = client
        # Messaging services are made and filled by the SMS adapter, the
        # same code that sets up every number (sms_setup).
        self._sms = sms_provider or TwilioSmsProvider(
            self.account_sid, token, client=client
        )

    # -- requirements ------------------------------------------------------

    async def requirements(
        self, country_code, number_type, subject_type
    ) -> Requirements:
        return sms_requirements(self, subject_type)

    # -- draft ---------------------------------------------------------------

    def _is_hail_number(self, e164: str) -> bool:
        """True when the mobile the person gave is one of Hail's own numbers."""
        try:
            return bool(
                self._client.incoming_phone_numbers.list(phone_number=e164, limit=1)
            )
        except _TWILIO_ERRORS as exc:
            raise VerificationProviderError(
                "Could not check the mobile number. Try again."
            ) from exc

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
        problems = self.validate_application(fields, address, subject_type)
        mobile = fields.get("mobile_phone_number")
        if (
            subject_type == "person"
            and mobile
            and await asyncio.to_thread(self._is_hail_number, mobile)
        ):
            problems.append(
                Problem(
                    field="mobile_phone_number",
                    message="Use your own mobile number, not a Hail number.",
                )
            )
        if problems:
            return DraftResult(refs={}, problems=problems)
        text = campaign_text(fields, subject_type)
        refs: dict = {"subject_type": subject_type, "campaign": text.__dict__}
        person = subject_type == "person"
        try:
            # A company's campaign covers the organization's service. A
            # person's campaign may hold one number, so it gets a service of
            # its own that holds only that number.
            refs["messaging_service_sid"] = await self._sms.ensure_messaging_service(
                organization_id=organization_id,
                existing_sid=None if person else messaging_service_sid,
                name=_person_service_name(organization_id) if person else None,
            )
            await asyncio.to_thread(
                self._build,
                refs,
                organization_id,
                contact_email,
                fields,
                address,
                subject_type,
            )
        except Exception as exc:
            await self.discard(refs)
            if isinstance(exc, TwilioRestException):
                logger.warning(
                    "10DLC draft failed: status=%s code=%s", exc.status, exc.code
                )
                # A failed evaluation names a detail of the form, whatever its
                # wording; an account or configuration problem stays an outage.
                if application_validation_error(
                    exc.status, exc.msg, known_field=isinstance(exc, _NotCompliant)
                ):
                    return DraftResult(
                        refs={},
                        problems=[Problem(field="", message=_safe_message(exc.msg))],
                    )
            raise VerificationProviderError("Twilio request failed") from exc
        return DraftResult(refs=refs)

    def _build(
        self,
        refs,
        organization_id,
        contact_email,
        fields,
        address,
        subject_type,
    ) -> None:
        th = self._client.trusthub.v1
        person = subject_type == "person"
        friendly = f"hail-{organization_id}"
        profile_policy = (
            _STARTER_PROFILE_POLICY if person else _SECONDARY_PROFILE_POLICY
        )
        profile = th.customer_profiles.create(
            friendly_name=friendly,
            email=contact_email,
            policy_sid=profile_policy,
        )
        refs["profile_sid"] = profile.sid
        if person:
            # A person's starter profile: the person, their address, and
            # Hail's primary profile, evaluated and submitted like a company's.
            starter = th.end_users.create(
                type="starter_customer_profile_information",
                friendly_name=friendly,
                attributes={
                    "first_name": fields["first_name"],
                    "last_name": fields["last_name"],
                    "email": fields["email"],
                    "phone_number": fields["mobile_phone_number"],
                },
            )
            th.customer_profiles(
                profile.sid
            ).customer_profiles_entity_assignments.create(object_sid=starter.sid)
        else:
            biz = th.end_users.create(
                type="customer_profile_business_information",
                friendly_name=friendly,
                attributes={
                    "business_name": fields["legal_name"],
                    "business_type": _business_type(fields["business_type"]),
                    "business_registration_identifier": _REGISTRATION_IDS.get(
                        fields.get("registration_country", ""), "Other"
                    ),
                    "business_registration_number": tax_number(
                        fields["ein"], fields.get("registration_country")
                    ),
                    "business_industry": fields["industry"],
                    "business_identity": "direct_customer",
                    "business_regions_of_operation": "USA_AND_CANADA",
                    "website_url": fields["website"],
                },
            )
            th.customer_profiles(
                profile.sid
            ).customer_profiles_entity_assignments.create(object_sid=biz.sid)
            rep = th.end_users.create(
                type="authorized_representative_1",
                friendly_name=friendly,
                attributes={
                    "first_name": fields["rep_name"].split(" ")[0],
                    "last_name": " ".join(fields["rep_name"].split(" ")[1:])
                    or fields["rep_name"],
                    "email": fields["rep_email"],
                    "phone_number": fields["rep_phone"],
                    "business_title": fields["rep_title"],
                    "job_position": "Other",
                },
            )
            th.customer_profiles(
                profile.sid
            ).customer_profiles_entity_assignments.create(object_sid=rep.sid)
        addr = self._client.addresses.create(
            customer_name=address.customer_name,
            street=address.street,
            city=address.city,
            region=address.region,
            postal_code=address.postal_code,
            iso_country=address.country_code,
        )
        doc = th.supporting_documents.create(
            friendly_name=friendly,
            type="customer_profile_address",
            attributes={"address_sids": addr.sid},
        )
        th.customer_profiles(profile.sid).customer_profiles_entity_assignments.create(
            object_sid=doc.sid
        )
        th.customer_profiles(profile.sid).customer_profiles_entity_assignments.create(
            object_sid=self._primary_profile_sid
        )
        ev = th.customer_profiles(profile.sid).customer_profiles_evaluations.create(
            policy_sid=profile_policy
        )
        if ev.status != "compliant":
            raise _NotCompliant(400, "", msg=_evaluation_message(ev))
        th.customer_profiles(profile.sid).update(status="pending-review")

        trust = th.trust_products.create(
            friendly_name=friendly,
            email=contact_email,
            policy_sid=_SOLE_PROP_TRUST_POLICY if person else _A2P_TRUST_POLICY,
        )
        refs["trust_sid"] = trust.sid
        if person:
            eu = th.end_users.create(
                type="sole_proprietor_information",
                friendly_name=friendly,
                attributes={
                    "brand_name": f"{fields['first_name']} {fields['last_name']}",
                    "mobile_phone_number": fields["mobile_phone_number"],
                    "vertical": "COMMUNICATION",
                },
            )
        else:
            eu = th.end_users.create(
                type="us_a2p_messaging_profile_information",
                friendly_name=friendly,
                attributes={"company_type": _company_type(fields["business_type"])},
            )
        th.trust_products(trust.sid).trust_products_entity_assignments.create(
            object_sid=eu.sid
        )
        th.trust_products(trust.sid).trust_products_entity_assignments.create(
            object_sid=profile.sid
        )
        ev = th.trust_products(trust.sid).trust_products_evaluations.create(
            policy_sid=_SOLE_PROP_TRUST_POLICY if person else _A2P_TRUST_POLICY
        )
        if ev.status != "compliant":
            raise _NotCompliant(400, "", msg=_evaluation_message(ev))
        th.trust_products(trust.sid).update(status="pending-review")

        brand_kwargs = {
            "customer_profile_bundle_sid": profile.sid,
            "a2p_profile_bundle_sid": trust.sid,
        }
        if person:
            brand_kwargs["brand_type"] = "SOLE_PROPRIETOR"
        brand = self._client.messaging.v1.a2p.brand_registrations.create(**brand_kwargs)
        # A person's code is texted by Twilio when the brand is approved.
        refs["brand_sid"] = brand.sid

    # -- lifecycle -----------------------------------------------------------

    async def check(self, refs) -> list[Problem]:
        return []

    async def submit(self, refs) -> None:
        # Vetting starts when the brand is created; nothing more to send.
        return None

    async def status(self, refs, *, may_file_campaign: bool = True) -> ProviderStatus:
        try:
            return await asyncio.to_thread(self._status, refs, may_file_campaign)
        except _TWILIO_ERRORS as exc:
            raise VerificationProviderError("Twilio request failed") from exc

    def _status(self, refs, may_file_campaign: bool = True) -> ProviderStatus:
        brand = self._client.messaging.v1.a2p.brand_registrations(
            refs["brand_sid"]
        ).fetch()
        if brand.status in ("FAILED", "SUSPENDED"):
            return ProviderStatus(
                state="rejected",
                reason=BRAND_REJECTED,
                raw_reason=_brand_reason(brand),
            )
        if brand.status != "APPROVED":
            return ProviderStatus(state="pending")
        if refs.get("subject_type") == "person":
            if brand.identity_status != "VERIFIED":
                self._resend_otp_if_old(refs)
                return ProviderStatus(state="pending")
            refs["identity_verified"] = True
        svc = self._client.messaging.v1.services(refs["messaging_service_sid"])
        if "campaign_sid" not in refs:
            # Recover a paid submission whose response was lost before retrying.
            if self._recover_campaign(refs):
                return ProviderStatus(state="pending")
            text = refs["campaign"]
            if text.get("schema_version", 1) < 2:
                refs["needs_information"] = True
                return ProviderStatus(state="pending", reason=LEGACY_APPLICATION)
            if not may_file_campaign:
                # The credit does not cover the campaign fee yet.
                return ProviderStatus(state="pending")
            try:
                campaign = svc.us_app_to_person.create(
                    brand_registration_sid=refs["brand_sid"],
                    description=text["description"],
                    message_flow=text["message_flow"],
                    message_samples=text["samples"],
                    us_app_to_person_usecase=text["use_case"],
                    has_embedded_links=text["has_links"],
                    has_embedded_phone=text["has_phone"],
                    age_gated=text["age_gated"],
                    direct_lending=text["direct_lending"],
                    subscriber_opt_in=True,
                    **(
                        {"opt_in_message": text["opt_in_message"]}
                        if text.get("opt_in_message")
                        else {}
                    ),
                    **(
                        {"opt_in_keywords": text["opt_in_keywords"]}
                        if text.get("opt_in_keywords")
                        else {}
                    ),
                    **_policy_links(text),
                )
            except TwilioRestException as exc:
                if application_validation_error(exc.status, exc.msg):
                    # The paid brand stays: the customer fixes the answers on
                    # this same registration (PATCH .../application), then
                    # the background pass files the campaign again.
                    refs["campaign_needs_repair"] = True
                    refs["needs_information"] = True
                    logger.warning("A2P campaign refused; held for answers")
                    return ProviderStatus(
                        state="pending",
                        reason=campaign_repair_reason(exc.msg),
                        raw_reason=exc.msg,
                    )
                raise
            refs["campaign_sid"] = campaign.sid
            return ProviderStatus(state="pending")
        campaign = svc.us_app_to_person(refs["campaign_sid"]).fetch()
        if campaign.campaign_status == "VERIFIED":
            refs["campaign_accepted"] = True
            # Custom runtime responses are not configured or verified by this adapter.
            return ProviderStatus(state="approved")
        if campaign.campaign_status == "FAILED":
            errors = campaign.errors or []
            raw = "; ".join(
                e.get("description", "") for e in errors if isinstance(e, dict)
            )
            return ProviderStatus(
                state="rejected",
                reason=campaign_repair_reason(raw),
                raw_reason=raw or None,
            )
        return ProviderStatus(state="pending")

    def _recover_campaign(self, refs) -> bool:
        if self.campaign_filed(refs):
            return True
        existing = self._client.messaging.v1.services(
            refs["messaging_service_sid"]
        ).us_app_to_person.list(limit=20)
        matching = [
            campaign
            for campaign in existing
            if campaign.brand_registration_sid == refs["brand_sid"]
        ]
        if matching:
            refs["campaign_sid"] = matching[0].sid
            refs.pop("needs_information", None)
            return True
        return False

    async def recover_campaign(self, refs) -> bool:
        try:
            return await asyncio.to_thread(self._recover_campaign, refs)
        except _TWILIO_ERRORS as exc:
            raise VerificationProviderError("Campaign discovery unavailable") from exc

    async def application_identity(self, refs, subject_type) -> dict | None:
        def read():
            brand = self._client.messaging.v1.a2p.brand_registrations(
                refs["brand_sid"]
            ).fetch()
            if brand.status != "APPROVED" or (
                subject_type == "person" and brand.identity_status != "VERIFIED"
            ):
                return None
            if getattr(brand, "customer_profile_bundle_sid", None) != refs.get(
                "profile_sid"
            ):
                return None
            th = self._client.trusthub.v1
            users = {}
            address = None
            for item in th.customer_profiles(
                refs["profile_sid"]
            ).customer_profiles_entity_assignments.list():
                if item.object_sid.startswith("IT"):
                    user = th.end_users(item.object_sid).fetch()
                    users[user.type] = user.attributes or {}
                elif item.object_sid.startswith("RD"):
                    doc = th.supporting_documents(item.object_sid).fetch()
                    if doc.type == "customer_profile_address":
                        sid = (doc.attributes or {}).get("address_sids")
                        if isinstance(sid, list):
                            sid = sid[0] if len(sid) == 1 else None
                        if (
                            isinstance(sid, str)
                            and sid.startswith("AD")
                            and "," not in sid
                        ):
                            record = self._client.addresses(sid).fetch()
                            address = {
                                "street": record.street,
                                "city": record.city,
                                "region": record.region,
                                "postal_code": record.postal_code,
                                "country_code": record.iso_country,
                            }
            if not address:
                return None
            fields = {
                "sender_country": address["country_code"],
                "has_tax_id": "NO" if subject_type == "person" else "YES",
            }
            if subject_type == "person":
                person = users.get("starter_customer_profile_information", {})
                fields.update(
                    {
                        key: person.get(value)
                        for key, value in {
                            "first_name": "first_name",
                            "last_name": "last_name",
                            "email": "email",
                            "mobile_phone_number": "phone_number",
                        }.items()
                    }
                )
            else:
                business = users.get("customer_profile_business_information", {})
                identifier = business.get("business_registration_identifier")
                fields.update(
                    {
                        "legal_name": business.get("business_name"),
                        "ein": business.get("business_registration_number"),
                        "registration_country": (
                            address["country_code"]
                            if identifier == "Other"
                            else _REGISTRATION_COUNTRIES.get(identifier)
                        ),
                    }
                )
                representative = users.get("authorized_representative_1", {})
                if representative:
                    fields.update(
                        {
                            "rep_email": representative.get("email"),
                            "rep_phone": representative.get("phone_number"),
                        }
                    )
            return {"fields": fields, "address": address}

        try:
            return await asyncio.to_thread(read)
        except TwilioRestException as exc:
            if exc.status == 404:
                return None
            raise VerificationProviderError(
                "Sender identity lookup unavailable"
            ) from exc
        except (KeyError, AttributeError, TypeError):
            return None
        except Exception as exc:
            # A transport error (the SDK lets requests' exceptions through).
            raise VerificationProviderError(
                "Sender identity lookup unavailable"
            ) from exc

    def confirmation(self, refs) -> str | None:
        # The person answers the carrier's text; there is no code to enter.
        # Twilio texts only once the brand is approved (otp_sent_at).
        if (
            refs.get("subject_type") == "person"
            and refs.get("identity_verified") is not True
            and refs.get("otp_sent_at")
        ):
            return "reply"
        return None

    def _resend_otp_if_old(self, refs) -> None:
        """Send the person's confirmation code again when the last one is
        over a day old. Twilio texts the first code itself when the brand is
        approved, so the first poll that sees an approved, unconfirmed brand
        only notes the time. The caller saves refs."""
        now = datetime.now(timezone.utc)
        sent = refs.get("otp_sent_at")
        if not sent:
            refs["otp_sent_at"] = now.isoformat()
            return
        if now - datetime.fromisoformat(sent) < _OTP_RESEND_AFTER:
            return
        self._client.messaging.v1.a2p.brand_registrations(
            refs["brand_sid"]
        ).brand_registration_otps.create()
        refs["otp_sent_at"] = now.isoformat()

    async def purchase_handle(self, refs) -> dict:
        return {}

    def subject_name(self, requirements, fields, address, documents) -> str | None:
        """``legal_name`` for a business, ``first_name`` and ``last_name``
        for a person, else the address's ``customer_name``."""
        return sms_subject_name(fields, address, requirements.subject_type)

    async def fetch_subject_name(self, refs, subject_type) -> str | None:
        """The customer profile's end user: the business information's
        ``business_name``, or the person's ``first_name`` and ``last_name``."""
        if not refs.get("profile_sid"):
            return None
        wanted = (
            "customer_profile_business_information"
            if subject_type == "business"
            else "starter_customer_profile_information"
        )

        def run() -> str | None:
            th = self._client.trusthub.v1
            profile = th.customer_profiles(refs["profile_sid"])
            for item in profile.customer_profiles_entity_assignments.list():
                if not (item.object_sid or "").startswith("IT"):
                    continue  # documents and the primary profile
                end_user = th.end_users(item.object_sid).fetch()
                if end_user.type != wanted:
                    continue
                attrs = end_user.attributes or {}
                if subject_type == "business":
                    return display_name(attrs.get("business_name"))
                return display_name(attrs.get("first_name"), attrs.get("last_name"))
            return None

        try:
            return await asyncio.to_thread(run)
        except _TWILIO_ERRORS as exc:
            raise VerificationProviderError("Twilio request failed") from exc

    async def attach_number(self, refs, *, provider_resource_id, e164) -> None:
        """Put the number in the campaign's messaging service (moved from
        another service when it sits in one). A person's service holds only
        their one number: another number already in it is refused."""
        service = refs["messaging_service_sid"]
        if refs.get("subject_type") == "person":
            try:
                present = await asyncio.to_thread(
                    self._client.messaging.v1.services(service).phone_numbers.list,
                    limit=20,
                )
            except _TWILIO_ERRORS as exc:
                raise VerificationProviderError("Twilio request failed") from exc
            if any(getattr(p, "sid", None) != provider_resource_id for p in present):
                logger.warning("10DLC: service %s already holds a number", service)
                raise VerificationProviderError(
                    "a person's registration holds one number"
                )
        try:
            await self._sms.attach_number(
                messaging_service_sid=service,
                provider_resource_id=provider_resource_id,
            )
        except SmsProvisioningError as exc:
            raise VerificationProviderError(exc.detail) from exc

    async def detach_number(self, refs, *, provider_resource_id, e164) -> bool:
        """A company's campaign covers the organization's service, which the
        number stays in. A person's number leaves the person's own service;
        True tells the caller to set it up in the organization's again."""
        service = refs.get("messaging_service_sid")
        if refs.get("subject_type") != "person" or not service:
            return False
        try:
            await asyncio.to_thread(
                self._client.messaging.v1.services(service)
                .phone_numbers(provider_resource_id)
                .delete
            )
        except TwilioRestException as exc:
            if exc.status != 404:
                logger.warning(
                    "10DLC detach failed: status=%s code=%s", exc.status, exc.code
                )
                return False
        except Exception:
            logger.warning("10DLC detach failed")
            return False
        return True

    async def discard(self, refs) -> None:
        # A brand cannot be deleted through the API; it is left behind.
        ms = refs.get("messaging_service_sid")
        client = self._client
        steps = [
            (
                "campaign_sid",
                lambda: (
                    client.messaging.v1.services(ms)
                    .us_app_to_person(refs["campaign_sid"])
                    .delete()
                ),
            ),
            (
                "trust_sid",
                lambda: client.trusthub.v1.trust_products(refs["trust_sid"]).delete(),
            ),
            (
                "profile_sid",
                lambda: client.trusthub.v1.customer_profiles(
                    refs["profile_sid"]
                ).delete(),
            ),
        ]
        for key, step in steps:
            if not refs.get(key) or (key == "campaign_sid" and not ms):
                continue
            try:
                await asyncio.to_thread(step)
            except TwilioRestException as exc:
                if exc.status != 404:
                    logger.warning(
                        "10DLC discard failed: step=%s status=%s code=%s",
                        key,
                        exc.status,
                        exc.code,
                    )
            except Exception:
                logger.warning("10DLC discard failed: step=%s", key)


def _person_service_name(organization_id) -> str:
    """The messaging service of a person's campaign: one number only."""
    return f"hail-org-{organization_id}-person"


def _business_type(key: str) -> str:
    return {
        "PRIVATE_PROFIT": "Corporation",
        "PUBLIC_PROFIT": "Corporation",
        "NON_PROFIT": "Non-profit Corporation",
        "GOVERNMENT": "Government",
    }[key]


def _company_type(key: str) -> str:
    return {
        "PRIVATE_PROFIT": "private",
        "PUBLIC_PROFIT": "public",
        "NON_PROFIT": "non-profit",
        "GOVERNMENT": "government",
    }[key]


def _evaluation_message(ev) -> str:
    for result in getattr(ev, "results", None) or []:
        for field in result.get("fields", []) if isinstance(result, dict) else []:
            if not field.get("passed", True):
                return field.get("failure_reason") or "the carrier refused a detail"
    return "the carrier refused a detail"


def _brand_reason(brand) -> str | None:
    """The carrier's feedback on a refused brand. Kept in the audit log
    only (see ``ProviderStatus.raw_reason``)."""
    fb = getattr(brand, "brand_feedback", None) or []
    return "; ".join(str(f) for f in fb) or None


def _safe_message(msg) -> str:
    """A short neutral sentence; never the carrier's own text."""
    low = (msg or "").lower()
    for words, text in (
        (("ein", "tax id", "registration number"), "Check the business tax number."),
        (("website", "url"), "Check the website address."),
        (("address", "street", "postal", "zip"), "Check the address."),
        (("phone", "mobile"), "Check the phone number."),
    ):
        if any(re.search(rf"\b{w}\b", low) for w in words):
            return text
    return SMS_REFUSED
