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
from hailhq.core.providers.verification import twilio_tollfree
from hailhq.core.providers.verification.base import (
    SMS_REFUSED,
    DiscardFailures,
    DraftResult,
    Problem,
    ProviderStatus,
    Requirements,
    SmsRegistrationProvider,
    VerificationProviderError,
)
from hailhq.core.providers.verification.forms import display_name
from hailhq.core.providers.verification.rejections import with_carrier_text
from hailhq.core.providers.verification.sms_form import (
    campaign_text,
    sms_requirements,
    sms_subject_name,
    tax_number,
)
from hailhq.core.providers.verification.sms_review import (
    LEGACY_APPLICATION,
    LOW_VOLUME_NEEDS_NEW_FILING,
    application_validation_error,
    brand_rejected_reason,
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


def _log_failure(operation: str, exc: Exception) -> None:
    """Keep support diagnostics without request bodies, contacts or secrets."""
    logger.error(
        "SMS verification failed: operation=%s exception=%s status=%s code=%s",
        operation,
        type(exc).__name__,
        exc.status if isinstance(exc, TwilioRestException) else None,
        exc.code if isinstance(exc, TwilioRestException) else None,
    )


class _NotCompliant(TwilioRestException):
    """A Trust Hub evaluation found a detail of the form not compliant: a
    definite refusal of the customer's answers, whatever its wording.
    ``reasons`` holds each failed field's text."""

    def __init__(self, reasons: list[str]) -> None:
        super().__init__(400, "", msg="; ".join(reasons))
        self.reasons = reasons


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
        if number_type == "toll_free" and country_code in {"US", "CA"}:
            return twilio_tollfree.requirements(self, country_code, subject_type)
        if number_type != "local" or country_code != "US":
            raise ValueError(
                "SMS registration supports US local and US/CA toll-free numbers"
            )
        if not self._primary_profile_sid:
            raise ValueError(
                "TwilioSmsRegistrationProvider needs TWILIO_PRIMARY_PROFILE_SID for 10DLC"
            )
        return sms_requirements(self, subject_type)

    def validate_application(self, fields, address, subject_type):
        if fields.get("registration_kind") == "toll_free":
            return twilio_tollfree.problems(self, fields, address, subject_type)
        return super().validate_application(fields, address, subject_type)

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
        provider_resource_id=None,
        e164=None,
        low_volume=False,
    ) -> DraftResult:
        """``low_volume``: the API's choice for a company (no secondary
        vetting). The API writes ``provider_refs.low_volume`` itself."""
        subject_type = requirements.subject_type
        if requirements.number_type == "toll_free":
            return await self._create_tollfree_draft(
                requirements,
                fields,
                address,
                contact_email,
                provider_resource_id,
                e164,
                organization_id,
            )
        if not self._primary_profile_sid:
            raise VerificationProviderError(
                "Local texting registration is not configured."
            )
        if fields.get("registration_kind") == "toll_free":
            return DraftResult(
                refs={},
                problems=[
                    Problem(
                        field="registration_kind",
                        message="Use the form for this number type.",
                    )
                ],
            )
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
                low_volume and not person,
            )
        except Exception as exc:
            _log_failure("draft", exc)
            await self.discard_draft(refs)
            if isinstance(exc, TwilioRestException):
                logger.warning(
                    "10DLC draft failed: status=%s code=%s", exc.status, exc.code
                )
                # A failed evaluation names a detail of the form, whatever its
                # wording; an account or configuration problem stays an outage.
                if _refused_answers(exc):
                    return DraftResult(
                        refs={},
                        problems=[
                            Problem(
                                field="",
                                message=_safe_message(
                                    exc.reasons
                                    if isinstance(exc, _NotCompliant)
                                    else exc.msg
                                ),
                                raw=(
                                    "; ".join(exc.reasons)
                                    if isinstance(exc, _NotCompliant)
                                    else exc.msg
                                )
                                or None,
                            )
                        ],
                    )
            raise VerificationProviderError("Twilio request failed") from exc
        return DraftResult(refs=refs)

    async def _create_tollfree_draft(
        self,
        requirements,
        fields,
        address,
        contact_email,
        provider_resource_id,
        e164,
        organization_id,
    ):
        problems = twilio_tollfree.problems(
            self, fields, address, requirements.subject_type
        )
        if not provider_resource_id or not e164:
            problems.append(
                Problem(field="number_id", message="Choose your toll-free number.")
            )
        if problems:
            return DraftResult(refs={}, problems=problems)
        try:
            record = await asyncio.to_thread(
                twilio_tollfree.create,
                self._client,
                organization_id,
                twilio_tollfree.payload(
                    fields,
                    address,
                    requirements.subject_type,
                    contact_email,
                    provider_resource_id,
                ),
            )
        except VerificationProviderError as exc:
            # The number already has a carrier request Hail cannot recover.
            # Say so: retrying the same filing can never succeed.
            return DraftResult(
                refs={}, problems=[Problem(field="number_id", message=str(exc))]
            )
        except Exception as exc:
            _log_failure("tollfree_create", exc)
            if isinstance(exc, TwilioRestException) and application_validation_error(
                exc.status, exc.msg
            ):
                return DraftResult(
                    refs={},
                    problems=[
                        Problem(field="", message=campaign_repair_reason(exc.msg))
                    ],
                )
            raise VerificationProviderError(
                "Texting registration is unavailable. Try again."
            ) from exc
        return DraftResult(
            refs={
                "registration_kind": "toll_free",
                "tollfree_verification_sid": record.sid,
                "provider_resource_id": provider_resource_id,
                "e164": e164,
                "subject_type": requirements.subject_type,
            }
        )

    def _build(
        self,
        refs,
        organization_id,
        contact_email,
        fields,
        address,
        subject_type,
        low_volume=False,
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
            raise _NotCompliant(_evaluation_reasons(ev))
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
            raise _NotCompliant(_evaluation_reasons(ev))
        th.trust_products(trust.sid).update(status="pending-review")

        brand_kwargs = {
            "customer_profile_bundle_sid": profile.sid,
            "a2p_profile_bundle_sid": trust.sid,
        }
        if person:
            brand_kwargs["brand_type"] = "SOLE_PROPRIETOR"
        elif low_volume:
            # A Low-Volume Standard brand: no secondary vetting, so no trust
            # score and the lowest daily limits.
            brand_kwargs["skip_automatic_sec_vet"] = True
        brand = self._client.messaging.v1.brand_registrations.create(**brand_kwargs)
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
            _log_failure("provider_request", exc)
            raise VerificationProviderError("Twilio request failed") from exc

    def _status(self, refs, may_file_campaign: bool = True) -> ProviderStatus:
        if refs.get("registration_kind") == "toll_free":
            return twilio_tollfree.status(self._client, refs)
        brand = self._client.messaging.v1.brand_registrations(refs["brand_sid"]).fetch()
        if brand.status in ("FAILED", "SUSPENDED"):
            return ProviderStatus(
                state="rejected",
                reason=_brand_customer_reason(brand),
                raw_reason=_brand_reason(brand),
            )
        if brand.status != "APPROVED":
            return ProviderStatus(state="pending")
        if refs.get("subject_type") == "person":
            if brand.identity_status != "VERIFIED":
                self._resend_otp_if_old(refs)
                return ProviderStatus(state="pending")
            refs["identity_verified"] = True
        elif (
            refs.get("low_volume")
            and brand.identity_status == "UNVERIFIED"
            and "campaign_sid" not in refs
        ):
            # The carrier could not verify the company. Twilio's API cannot
            # add vetting to a Low-Volume Standard brand (its Vettings
            # resource files Campaign Verify tokens only), so the faster
            # lane is a new registration. Held for the customer.
            refs["needs_information"] = True
            return ProviderStatus(state="pending", reason=LOW_VOLUME_NEEDS_NEW_FILING)
        svc = self._client.messaging.v1.services(refs["messaging_service_sid"])
        if "campaign_sid" not in refs:
            # Recover a paid submission whose response was lost before retrying.
            if self._recover_campaign(refs):
                return ProviderStatus(state="pending")
            # No text once the account closed (its answers are deleted): held.
            text = refs.get("campaign") or {}
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
            # For the audit row: what the topic match read, codes included.
            raw = "; ".join(
                ": ".join(
                    ", ".join(map(str, v)) if isinstance(v, list) else str(v)
                    for v in (
                        e.get("error_code"),
                        e.get("description"),
                        e.get("fields"),
                    )
                    if v
                )
                for e in errors
                if isinstance(e, dict)
            )
            return ProviderStatus(
                state="rejected",
                # The topic match reads every error (codes included); the
                # quote skips Twilio's "General Error" placeholder.
                reason=campaign_repair_reason(
                    [e for e in errors if isinstance(e, dict)],
                    quote=[
                        e
                        for e in errors
                        if isinstance(e, dict)
                        and not _placeholder(e.get("description"))
                    ],
                ),
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
            brand = self._client.messaging.v1.brand_registrations(
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
        except (KeyError, AttributeError, TypeError) as exc:
            _log_failure("application_identity", exc)
            return None
        except Exception as exc:
            # A transport error (the SDK lets requests' exceptions through).
            raise VerificationProviderError(
                "Sender identity lookup unavailable"
            ) from exc

    def confirmation(self, refs) -> str | None:
        if refs.get("registration_kind") == "toll_free":
            return None
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
        self._client.messaging.v1.brand_registrations(
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
            _log_failure("provider_request", exc)
            raise VerificationProviderError("Twilio request failed") from exc

    async def attach_number(self, refs, *, provider_resource_id, e164) -> None:
        """Put the number in the campaign's messaging service (moved from
        another service when it sits in one). A person's service holds only
        their one number: another number already in it is refused."""
        if refs.get("registration_kind") == "toll_free":
            if (
                refs.get("provider_resource_id") != provider_resource_id
                or refs.get("e164") != e164
            ):
                raise VerificationProviderError(
                    "This registration covers a different number."
                )
            return
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
        True tells the caller to set it up in the organization's again. Not
        in the service (404, 410) is done; any other failure raises."""
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
            if exc.status not in (404, 410):
                logger.warning(
                    "10DLC detach failed: status=%s code=%s", exc.status, exc.code
                )
                raise
        except Exception as exc:
            logger.warning("10DLC detach failed: %s", type(exc).__name__)
            raise
        return True

    def step(self, refs):
        if refs.get("registration_kind") == "toll_free" and refs.get(
            "needs_information"
        ):
            return "needs_information"
        return super().step(refs)

    async def amend_filed_application(self, refs, fields, address, subject_type):
        if refs.get("registration_kind") != "toll_free":
            return await super().amend_filed_application(
                refs, fields, address, subject_type
            )
        try:
            await asyncio.to_thread(
                twilio_tollfree.amend, self._client, refs, fields, address, subject_type
            )
        except _TWILIO_ERRORS as exc:
            _log_failure("tollfree_amend", exc)
            raise VerificationProviderError(
                "Could not update registration. Try again."
            ) from exc

    def campaign_filed(self, refs):
        return bool(
            (refs or {}).get("tollfree_verification_sid")
        ) or super().campaign_filed(refs)

    async def discard(self, refs) -> None:
        if refs.get("registration_kind") == "toll_free":
            # Already gone (404) is done. A refusal raises DiscardRefused:
            # the caller keeps the ids for a delete by hand.
            failures = DiscardFailures("twilio toll-free")
            try:
                await asyncio.to_thread(
                    self._client.messaging.v1.tollfree_verifications(
                        refs["tollfree_verification_sid"]
                    ).delete
                )
            except Exception as exc:
                failures.add(exc, "tollfree_verification")
            failures.raise_any("Could not cancel registration. Try again.")
            return
        # A brand cannot be deleted through the API; it is left behind.
        failures = DiscardFailures("twilio 10DLC")
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
            except Exception as exc:
                failures.add(exc, key)
        failures.raise_any("Could not cancel registration. Try again.")


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


def _refused_answers(exc: TwilioRestException) -> bool:
    """A refusal of the customer's answers, not an outage. A failed
    evaluation is one unless a failed field's own text names an account or
    configuration problem (each field is checked alone): that stays an
    outage, retried, since no answer the customer changes can fix it."""
    if isinstance(exc, _NotCompliant):
        return all(
            application_validation_error(exc.status, text, known_field=True)
            for text in exc.reasons or [""]
        )
    return application_validation_error(exc.status, exc.msg)


def _evaluation_reasons(ev) -> list[str]:
    """The carrier's text for every failed field (empty when it gave none)."""
    said: list[str] = []
    for result in getattr(ev, "results", None) or []:
        for field in result.get("fields", []) if isinstance(result, dict) else []:
            text = field.get("failure_reason")
            if not field.get("passed", True) and text and text not in said:
                said.append(text)
    return said


# Hail's fix for each brand refusal Twilio documents. By error code: the
# error pages (https://www.twilio.com/docs/api/errors/<code>). By text: the
# email and address reasons in Twilio's "Troubleshooting Sole Proprietor
# Brand Registration Failures" guide, which all come under general codes
# (30701, 30794). Any other refusal shows Twilio's own words alone.
_BRAND_FIXES_BY_CODE: dict[int, str] = {
    30795: "The tax number does not match the legal company name. Type both "
    "exactly as they appear on your tax documents.",
    30796: "The company type or stock details do not match public records. "
    "Give stock details only for a publicly traded company.",
    21736: "The carrier could not link the contact email's domain to your "
    "business. Use an email address on your business's own domain.",
    21738: "The carrier could not deliver mail to the contact email. Use an "
    "address that receives mail.",
    21740: "The carrier needs a contact email on your business's own domain, "
    "not a free email provider.",
}
_BRAND_FIXES_BY_TEXT: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        ("invalid address syntax",),
        "The email address is not written correctly. Check it for typos.",
    ),
    (
        ("unknown email domain",),
        "The email address's domain does not exist. Check the part after the @.",
    ),
    (
        ("disposable address",),
        (
            "The carrier does not accept temporary or throwaway email "
            "addresses. Use a lasting email address that you own."
        ),
    ),
    (
        ("email is invalid",),
        (
            "The carrier could not confirm the email address. Use one that "
            "receives mail."
        ),
    ),
    (
        ("not a valid us or canada address",),
        "US texting registration needs a US or Canadian address.",
    ),
    (
        (
            "invalid postal code",
            "invalid street",
            "invalid locality",
            "invalid region",
            "invalid house number",
        ),
        (
            "The address does not match a known postal address. Check the "
            "street, city, state and postal code."
        ),
    ),
)


def _brand_customer_reason(brand) -> str:
    """Why the brand failed, for the customer: Hail's fix for each refusal
    Twilio documents, then Twilio's own words. Twilio puts both in
    ``errors`` (``brand_feedback`` holds only feedback ids)."""
    errors = [e for e in getattr(brand, "errors", None) or [] if isinstance(e, dict)]
    fixes: list[str] = []
    for error in errors:
        text = str(error.get("description") or "").lower()
        code = str(error.get("error_code") or "").strip()
        fix = _BRAND_FIXES_BY_CODE.get(int(code)) if code.isdecimal() else None
        if fix is None:
            fix = next(
                (
                    f
                    for words, f in _BRAND_FIXES_BY_TEXT
                    if any(w in text for w in words)
                ),
                None,
            )
        if fix and fix not in fixes:
            fixes.append(fix)
    # Twilio's "General Error" placeholder says nothing: never quote it.
    errors = [e for e in errors if not _placeholder(e.get("description"))]
    said: object = errors
    if not with_carrier_text("", errors):
        # No text in errors: Twilio's failure_reason, unless it is only
        # its placeholder.
        failure = str(getattr(brand, "failure_reason", None) or "")
        said = "" if _placeholder(failure) else failure
    return brand_rejected_reason(
        fixes, said, suspended=getattr(brand, "status", None) == "SUSPENDED"
    )


def _placeholder(text: object) -> bool:
    return str(text or "").strip().rstrip(".").lower() == "general error"


def _brand_reason(brand) -> str | None:
    """Everything Twilio said about a refused brand, for the audit row
    (see ``ProviderStatus.raw_reason``)."""
    parts = [
        ": ".join(str(v) for v in (e.get("error_code"), e.get("description")) if v)
        for e in getattr(brand, "errors", None) or []
        if isinstance(e, dict) and (e.get("error_code") or e.get("description"))
    ]
    parts += [str(f) for f in getattr(brand, "brand_feedback", None) or []]
    if getattr(brand, "failure_reason", None):
        parts.append(str(brand.failure_reason))
    return "; ".join(parts) or None


def _safe_message(msg) -> str:
    """Hail's short sentence for each detail ``msg`` names (one per text,
    without repeats), then the carrier's own words. ``msg`` is one text or
    a list of them."""
    fixes: list[str] = []
    for one in msg if isinstance(msg, list) else [msg or ""]:
        # "email address" is the email, not the postal address.
        low = re.sub(r"\bemail address\b", "email", str(one).lower())
        for words, text in (
            (("email",), "Check the email address."),
            (
                ("ein", "tax id", "registration number"),
                "Check the business tax number.",
            ),
            (("website", "url"), "Check the website address."),
            (("address", "street", "postal", "zip"), "Check the address."),
            (("phone", "mobile"), "Check the phone number."),
        ):
            if any(re.search(rf"\b{w}\b", low) for w in words) and text not in fixes:
                fixes.append(text)
    return with_carrier_text(" ".join(fixes) or SMS_REFUSED, msg)
