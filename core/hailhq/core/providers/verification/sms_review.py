"""Local SMS preflight and fixed, customer-safe repair guidance."""

import re

import phonenumbers
from hailhq.core.providers.verification.base import (
    CAMPAIGN_REJECTED,
    Address,
    Problem,
)
from hailhq.core.providers.verification.sms_form import tax_number
from hailhq.core.urls import canonical_url

LEGACY_APPLICATION = (
    "This application needs the current consent evidence and message examples. "
    "Review the saved details, complete the missing answers, and resubmit."
)


def sender_problems(
    fields: dict[str, str], address: Address | None, subject_type: str
) -> list[Problem]:
    problems = []
    country = fields.get("sender_country", "").strip().upper()
    if address and country and address.country_code.upper() != country:
        problems.append(
            Problem(
                field="address.country_code",
                message="Use the sender country selected in eligibility.",
            )
        )
    issuer = fields.get("registration_country", "").strip().upper()
    if (
        subject_type == "business"
        and address
        and issuer
        and issuer not in {"US", "CA"}
        and address.country_code.upper() != issuer
    ):
        # Carriers check a non-US/CA number against the address country.
        problems.append(
            Problem(
                field="registration_country",
                message="A tax number from outside the US and Canada must come from your address country.",
            )
        )
    if subject_type == "person":
        mobile = fields.get("mobile_phone_number", "").strip()
        if mobile:
            try:
                number = phonenumbers.parse(mobile)
                eligible = phonenumbers.is_possible_number(
                    number
                ) and phonenumbers.region_code_for_number(number) in {"US", "CA"}
            except phonenumbers.NumberParseException:
                eligible = False
            if not eligible:
                problems.append(
                    Problem(
                        field="mobile_phone_number",
                        message="Use your own US or Canadian mobile number, not a Hail or other internet phone number.",
                    )
                )
    return problems


def campaign_repair_reason(raw: object) -> str:
    """Match review topics; never interpolate provider text or personal data.

    Codes and descriptions differ across carriers. Ambiguous feedback retains
    general guidance; raw feedback belongs only in the protected audit record.
    """
    # Split field names into words first ("MessageSamples", "MESSAGE_SAMPLES"
    # and "subUsecases" hold "message samples" and "usecases"), so the word
    # start match below still finds them.
    text = re.sub(r"(?<=[a-z])(?=[A-Z])|_", " ", str(raw or "")).lower()
    rules = (
        (
            ("privacy", "30908"),
            "Review your privacy policy: make the public link accessible and explain how SMS consent and phone numbers are protected.",
        ),
        (
            ("terms", "30909"),
            "Review the public SMS terms: include the program, frequency, rates, support and opt-out instructions.",
        ),
        (
            (
                "opt-in",
                "opt in",
                "optin",
                "consent",
                "message flow",
                "messageflow",
                "30917",
                "30925",
            ),
            "Review recipient consent: provide the exact opt-in steps, disclosure and accessible redacted evidence for every method used.",
        ),
        (
            ("sample",),
            "Review the message examples: identify the sender and provide a representative example for every selected purpose.",
        ),
        (
            ("use case", "usecase"),
            "Review the selected purposes and describe the messages you actually send; make the examples match.",
        ),
        (
            ("website", "url", "link"),
            "Review the application links: make the exact opt-in page and policies publicly accessible, with redacted evidence for private flows.",
        ),
    )
    for words, message in rules:
        # Match at a word start: "terms" must not hit "determines", nor
        # "url" "curl", nor "link" "unlinked".
        if any(re.search(rf"\b{re.escape(word)}", text) for word in words):
            return message
    return CAMPAIGN_REJECTED


def application_validation_error(
    status: int | None, raw: object, *, known_field: bool = False
) -> bool:
    """Only definite field/content validation is a terminal application refusal.

    Auth, account configuration, billing and unknown provider errors must keep
    the filing retryable; HTTP 4xx by itself does not identify customer error.
    A provider may identify an allowlisted application field structurally, but
    that evidence must not override authentication or operational failures.
    """
    if status not in (400, 422):
        return False
    text = str(raw or "").lower()
    if any(
        word in text
        for word in (
            "credential",
            "api key",
            "authentication",
            "permission",
            "unauthorized",
            "forbidden",
            "account suspend",
            "account inactive",
            "insufficient balance",
            "messaging service",
            "configuration",
        )
    ):
        return False
    return known_field or any(
        re.search(rf"\b{re.escape(word)}\b", text)
        for word in (
            "privacy policy",
            "terms and conditions",
            "message flow",
            "messageflow",
            "sample message",
            "message sample",
            "message samples",
            "messagesamples",
            "opt-in",
            "optin",
            "use case",
            "usecase",
            "tax id",
            "ein",
            "registration number",
            "phone number",
            "mobile number",
            "first name",
            "last name",
            "email",
            "website",
            "postal",
            "street address",
        )
    )


_IDENTITY_FIELDS = {
    "person": (
        "sender_country",
        "has_tax_id",
        "first_name",
        "last_name",
        "email",
        "mobile_phone_number",
    ),
    "business": (
        "sender_country",
        "has_tax_id",
        "legal_name",
        "business_type",
        "registration_country",
        "ein",
        "industry",
        "website",
        "rep_name",
        "rep_email",
        "rep_phone",
        "rep_title",
    ),
}


def same_sender(
    original: dict, fields: dict[str, str], address: Address, subject_type: str
) -> bool:
    """Compare authoritative sender identity; missing core data fails closed."""
    previous = original.get("fields") or {}
    location = original.get("address") or {}
    minimum = (
        ("first_name", "last_name", "email", "mobile_phone_number")
        if subject_type == "person"
        else ("legal_name", "ein", "registration_country")
    )
    if any(not previous.get(key) for key in minimum) or any(
        not location.get(key)
        for key in ("street", "city", "region", "postal_code", "country_code")
    ):
        return False

    def normalized(key, value, source):
        text = " ".join(str(value or "").split()).casefold()
        if key == "ein":
            return tax_number(text, source.get("registration_country"))
        if key in {"mobile_phone_number", "rep_phone"}:
            return re.sub(r"\D", "", text)
        if key == "website":
            # The carrier may hold an http:// or slash-ended form of the
            # site; the form now asks for https://.
            return re.sub(r"^https?://", "", canonical_url(text))
        return text

    return all(
        normalized(key, previous[key], previous)
        == normalized(key, fields.get(key), fields)
        for key in _IDENTITY_FIELDS[subject_type]
        if key in previous
    ) and all(
        normalized(key, value, {}) == normalized(key, getattr(address, key, None), {})
        for key, value in location.items()
        if key != "customer_name"
    )
