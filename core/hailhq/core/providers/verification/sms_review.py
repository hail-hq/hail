"""Local SMS preflight and fixed, customer-safe repair guidance."""

import phonenumbers

from hailhq.core.providers.verification.base import (
    CAMPAIGN_REJECTED,
    Address,
    Problem,
)

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
    text = str(raw or "").lower()
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
            ("opt-in", "opt in", "consent", "message flow", "30917", "30925"),
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
        if any(word in text for word in words):
            return message
    return CAMPAIGN_REJECTED
