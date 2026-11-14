"""NANP toll-free verification, separate from paid A2P 10DLC registration.

Source checked 2026-11-14: Twilio's TFV API onboarding and approval guidance.
Links and evidence are sent to the carrier; Hail never fetches arbitrary URLs.
"""

import hashlib
import json
import re

from hailhq.core.providers.verification.base import (
    FieldOption,
    FieldSpec,
    Problem,
    ProviderStatus,
    Requirements,
    SmsRegistrationProvider,
    VerificationProviderError,
)
from hailhq.core.providers.verification.sms_form import (
    _is_public_https_url,
    campaign_text,
    picked,
    sms_requirements,
    sms_subject_name,
    tax_number,
)
from hailhq.core.providers.verification.sms_review import campaign_repair_reason
from twilio.rest.messaging.v1.tollfree_verification import TollfreeVerificationInstance

KIND = "toll_free"
_METHODS = {
    "WEBSITE": ("WEB_FORM", "website_evidence_url"),
    "PRIVATE_FORM": ("WEB_FORM", "private_form_evidence_url"),
    "VERBAL": ("VERBAL", "verbal_evidence_url"),
    "PAPER": ("PAPER_FORM", "paper_evidence_url"),
    "KEYWORD": ("VIA_TEXT", "keyword_evidence_url"),
    "QR": ("MOBILE_QR_CODE", "qr_evidence_url"),
}
_CATEGORIES = {
    "CUSTOMER_CARE": "CUSTOMER_CARE",
    "ACCOUNT_NOTIFICATION": "ACCOUNT_NOTIFICATIONS",
    "DELIVERY_NOTIFICATION": "DELIVERY_NOTIFICATIONS",
    "2FA": "TWO_FACTOR_AUTHENTICATION",
    "FRAUD_ALERT": "FRAUD_ALERT_MESSAGING",
    "MARKETING": "MARKETING",
    "HIGHER_EDUCATION": "HIGHER_EDUCATION",
    "PUBLIC_SERVICE_ANNOUNCEMENT": "PUBLIC_SERVICE_ANNOUNCEMENT",
}
_AUTHORITIES = tuple(
    FieldOption(key=value, label=value)
    for key, value in vars(
        TollfreeVerificationInstance.BusinessRegistrationAuthority
    ).items()
    if not key.startswith("_")
)


def requirements(provider, country_code, subject_type) -> Requirements:
    base = sms_requirements(provider, subject_type)
    fields = []
    for spec in base.fields:
        if spec.name == "consent_methods":
            spec = spec.model_copy(
                update={"options": tuple(o for o in spec.options if o.key in _METHODS)}
            )
        elif spec.name == "text_types":
            spec = spec.model_copy(
                update={
                    "options": tuple(o for o in spec.options if o.key in _CATEGORIES)
                }
            )
        elif spec.name == "website":
            spec = spec.model_copy(
                update={"label": "Business website or public business social page"}
            )
        elif spec.name == "verbal_evidence_url":
            spec = spec.model_copy(
                update={
                    "help": "Provide a public redacted document or image with the full consent script."
                }
            )
        elif spec.name == "mobile_phone_number":
            spec = spec.model_copy(
                update={
                    "label": "Contact phone number",
                    "help": "Use the end business contact's phone number.",
                }
            )
        fields.append(spec)
    fields.extend(
        (
            FieldSpec(
                name="registration_kind",
                label="Registration type",
                options=(FieldOption(key=KIND, label="Toll-free texting"),),
            ),
            FieldSpec(
                name="message_volume",
                label="Estimated texts per month",
                pattern=r"^[1-9][0-9]{0,8}$",
                max_length=9,
            ),
            FieldSpec(
                name="doing_business_as",
                label="Business name customers know",
                required=False,
                max_length=255,
                help="Enter your trading name when it differs from your legal name.",
            ),
            FieldSpec(
                name="business_registration_authority",
                label="Business registration authority",
                options=_AUTHORITIES,
                required=subject_type == "business",
            ),
        )
    )
    if subject_type == "person":
        fields.append(
            FieldSpec(
                name="website",
                label="Business website or public business social page",
                kind="url",
            )
        )
    return base.model_copy(
        update={
            "country_code": country_code,
            "number_type": KIND,
            "fields": tuple(fields),
        }
    )


def problems(provider, fields, address, subject_type) -> list[Problem]:
    # Reuse the common consent and sender checks, then apply the TFV form.
    result = SmsRegistrationProvider.validate_application(
        provider, fields, address, subject_type
    )
    for spec in requirements(provider, "US", subject_type).fields:
        value = fields.get(spec.name, "").strip()
        if spec.required and not value:
            result.append(Problem(field=spec.name, message="This is required."))
        elif value and spec.options:
            values = picked(value) if spec.kind == "choices" else [value]
            if any(v not in {o.key for o in spec.options} for v in values):
                result.append(Problem(field=spec.name, message="Pick from the list."))
        elif value and spec.pattern and not re.fullmatch(spec.pattern, value):
            result.append(
                Problem(
                    field=spec.name,
                    message=(
                        "Enter a positive whole number of texts."
                        if spec.name == "message_volume"
                        else "Check the format of this answer."
                    ),
                )
            )
        elif value and spec.kind == "url" and not _is_public_https_url(value):
            result.append(
                Problem(field=spec.name, message="Enter a full public https:// link.")
            )
    for method in picked(fields.get("consent_methods")):
        if method in _METHODS:
            evidence = _METHODS[method][1]
            if not fields.get(evidence, "").strip():
                result.append(
                    Problem(
                        field=evidence,
                        message="Provide a public link to redacted evidence for this consent method.",
                    )
                )
    # The shared checks above may already report the same field.
    return list({(p.field, p.message): p for p in result}.values())


def payload(fields, address, subject_type, contact_email, provider_resource_id):
    text = campaign_text(fields, subject_type)
    methods = picked(fields.get("consent_methods"))
    name = sms_subject_name(fields, address, subject_type)
    contact = (
        (fields["first_name"], fields["last_name"])
        if subject_type == "person"
        else (
            fields["rep_name"].split(" ")[0],
            " ".join(fields["rep_name"].split(" ")[1:]) or fields["rep_name"],
        )
    )
    result = {
        "business_name": name,
        "business_website": fields["website"],
        "notification_email": contact_email,
        "business_contact_first_name": contact[0],
        "business_contact_last_name": contact[1],
        "business_contact_email": text.contact_email,
        "business_contact_phone": fields[
            "mobile_phone_number" if subject_type == "person" else "rep_phone"
        ],
        "business_street_address": address.street,
        "business_city": address.city,
        "business_state_province_region": address.region,
        "business_postal_code": address.postal_code,
        "business_country": address.country_code,
        "business_type": (
            "SOLE_PROPRIETOR" if subject_type == "person" else fields["business_type"]
        ),
        "use_case_categories": [_CATEGORIES[p] for p in text.text_types],
        "use_case_summary": text.description,
        "production_message_sample": "\n\n".join(text.samples),
        "opt_in_image_urls": list(
            dict.fromkeys(fields[_METHODS[m][1]].strip() for m in methods)
        ),
        # The API accepts one enum. Every selected method is documented in
        # additional_information and has its own evidence URL.
        "opt_in_type": _METHODS[methods[0]][0],
        "message_volume": fields["message_volume"],
        "tollfree_phone_number_sid": provider_resource_id,
        "additional_information": text.message_flow,
        "privacy_policy_url": text.privacy_policy_url,
        "terms_and_conditions_url": text.terms_url,
        "age_gated_content": text.age_gated,
    }
    if subject_type == "business":
        result.update(
            business_registration_number=tax_number(
                fields["ein"], fields["registration_country"]
            ),
            business_registration_country=fields["registration_country"],
            business_registration_authority=fields["business_registration_authority"],
        )
    for key, value in (
        ("doing_business_as", fields.get("doing_business_as")),
        ("opt_in_confirmation_message", text.opt_in_message),
        ("opt_in_keywords", text.opt_in_keywords),
    ):
        if value:
            result[key] = value
    return result


def status(client, refs) -> ProviderStatus:
    record = client.messaging.v1.tollfree_verifications(
        refs["tollfree_verification_sid"]
    ).fetch()
    if record.tollfree_phone_number_sid != refs["provider_resource_id"]:
        raise VerificationProviderError(
            "Registration number does not match. Contact support."
        )
    if record.status == "TWILIO_APPROVED":
        refs["campaign_accepted"] = True
        return ProviderStatus(state="approved")
    if record.status == "TWILIO_REJECTED":
        raw = "; ".join(
            str(v) for v in (record.rejection_reason, record.error_code) if v
        )
        if record.edit_allowed:
            refs["needs_information"] = True
            refs["campaign_needs_repair"] = True
            return ProviderStatus(
                state="pending",
                reason=campaign_repair_reason(raw),
                raw_reason=raw or None,
            )
        return ProviderStatus(
            state="rejected", reason=campaign_repair_reason(raw), raw_reason=raw or None
        )
    return ProviderStatus(state="pending")


def amend(client, refs, fields, address, subject_type):
    resource = client.messaging.v1.tollfree_verifications(
        refs["tollfree_verification_sid"]
    )
    record = resource.fetch()
    if record.tollfree_phone_number_sid != refs["provider_resource_id"]:
        raise VerificationProviderError(
            "The registration covers a different number. Contact support."
        )
    data = payload(
        fields,
        address,
        subject_type,
        record.notification_email,
        refs["provider_resource_id"],
    )
    data.pop("tollfree_phone_number_sid")
    if record.status in {"PENDING_REVIEW", "IN_REVIEW", "TWILIO_APPROVED"} and all(
        getattr(record, key, None) == value for key, value in data.items()
    ):
        # The previous update succeeded but its response or local commit was
        # lost. Recover only exact saved answers on this same carrier request.
        return
    if record.status != "TWILIO_REJECTED" or not record.edit_allowed:
        raise VerificationProviderError(
            "The carrier is not accepting changes to this registration. Contact support."
        )
    resource.update(
        **data, edit_reason="Updated recipient consent evidence and program details"
    )


def create(client, organization_id, data):
    # A response can be lost after a successful create. Recover only the
    # exact submitted answers, owned number and organization on a retry.
    fingerprint = hashlib.sha256(
        json.dumps([str(organization_id), data], sort_keys=True).encode()
    ).hexdigest()
    reference = "hail-tfv-" + fingerprint
    existing = client.messaging.v1.tollfree_verifications.list(
        tollfree_phone_number_sid=data["tollfree_phone_number_sid"], limit=2
    )
    if existing:
        if len(existing) == 1 and existing[0].external_reference_id == reference:
            return existing[0]
        raise VerificationProviderError(
            "This number already has a carrier verification. Contact support to recover it."
        )
    return client.messaging.v1.tollfree_verifications.create(
        **data, external_reference_id=reference
    )
