"""Local SMS preflight and repair guidance, with the carrier's own words."""

import re

import phonenumbers
from hailhq.core.providers.verification.base import (
    BRAND_REJECTED,
    CAMPAIGN_REJECTED,
    Address,
    Problem,
)
from hailhq.core.providers.verification.rejections import (
    FIX_AND_START_AGAIN,
    FIX_THESE_AND_START_AGAIN,
    START_AGAIN,
    TEXT_KEYS,
    with_carrier_text,
)
from hailhq.core.providers.verification.sms_form import (
    NINE_DIGIT_COUNTRIES,
    tax_number,
)
from hailhq.core.urls import canonical_url

LEGACY_APPLICATION = (
    "This application needs the current consent evidence and message examples. "
    "Review the saved details, complete the missing answers, and resubmit."
)
# A low-volume company brand the carrier could not verify. The customer
# can turn on the faster lane: Hail then sends the extra company details.
LOW_VOLUME_FASTER_LANE = (
    "The networks need more details about your company before you can text. "
    "Turn on the faster lane in your answers and save. "
    "We then send the details for you. This step has its own fee."
)
# The same, on a carrier where the faster lane needs a new registration.
LOW_VOLUME_NEEDS_NEW_FILING = (
    "The networks need more details about your company before you can text. "
    "This needs a new registration that sends more than 2,000 texts a day. "
    "Cancel this registration, then register again."
)
# The faster lane ran and the networks still could not verify the company.
# The company details cannot change on a filed brand: only a new
# registration can try again. Hail keeps checking this one meanwhile.
LOW_VOLUME_FASTER_LANE_FAILED = (
    "The networks could not verify your company, even with the extra details. "
    "Check that your company name, tax number and address match your official records. "
    "Then cancel this registration and register again. "
    "We keep checking, and continue on our own if the networks verify your company later."
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
        and issuer not in NINE_DIGIT_COUNTRIES
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


# Hail's sentence for each detail a carrier's refusal at filing names.
_ADDRESS_FIX = "Check the address."
_DETAIL_FIXES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("email",), "Check the email address."),
    (("ein", "tax id", "registration number"), "Check the business tax number."),
    (("website", "url"), "Check the website address."),
    (("address", "street", "postal", "zip"), _ADDRESS_FIX),
    (("phone", "mobile"), "Check the phone number."),
)


# The postal address in a text that is also about the email.
_POSTAL_IN_EMAIL_TEXT = re.compile(
    r"\b(?:street|postal|zip)\b"
    r"|\b(?:business|mailing|physical|home|street|postal|billing) address\b"
)


def detail_fixes(texts: list[str]) -> list[str]:
    """Hail's sentence for every detail the carrier's ``texts`` name, each
    text on its own, without repeats. "web address" is the website. In a
    text about the email, a bare "address" is the email's ("suspected
    disposable address", "invalid address syntax"): the postal address
    fix then needs street, postal, zip or a postal kind of address
    ("business address", "mailing address")."""
    fixes: list[str] = []
    for one in texts:
        low = re.sub(r"\bweb address\b", "website", str(one or "").lower())
        about_email = re.search(r"\bemail\b", low) is not None
        for words, fix in _DETAIL_FIXES:
            if fix == _ADDRESS_FIX and about_email:
                if fix not in fixes and _POSTAL_IN_EMAIL_TEXT.search(low):
                    fixes.append(fix)
                continue
            if fix not in fixes and any(re.search(rf"\b{w}\b", low) for w in words):
                fixes.append(fix)
    return fixes


# Hail's lead sentence for a refused brand with no fix of Hail's own.
BRAND_REJECTED_LEAD = "The 10DLC registry rejected the registration."


# A suspended brand: a compliance action a new filing cannot fix. Twilio
# warns that texting the same way from a new brand can close the account.
BRAND_SUSPENDED_LEAD = "The carrier suspended your US texting registration."
BRAND_SUSPENDED_NEXT = (
    "Email hi@hail.so and we will look into it. Do not file a new registration."
)


def brand_rejected_reason(
    fixes: list[str], raw: object, *, suspended: bool = False
) -> str:
    """Why a brand was refused: Hail's fixes (or its lead sentence), then
    the carrier's own words, then the next step, worded as
    ``customer_reason`` words it. No fix and no carrier text gives the
    generic ``BRAND_REJECTED``. A suspended brand asks the customer to
    contact Hail instead of filing again."""
    if suspended:
        return f"{with_carrier_text(BRAND_SUSPENDED_LEAD, raw)} {BRAND_SUSPENDED_NEXT}"
    said = bool(with_carrier_text("", raw))
    if not fixes and not said:
        return BRAND_REJECTED
    if fixes and not said:
        return f"{' '.join(fixes)} {START_AGAIN}"
    body = with_carrier_text(" ".join(fixes) or BRAND_REJECTED_LEAD, raw)
    end = FIX_THESE_AND_START_AGAIN if fixes else FIX_AND_START_AGAIN
    return f"{body} {end}"


def _topic_text(raw: object) -> str:
    """What the topic match reads in ``raw``: the carrier's text (with its
    field tag), its error codes, the fields it names (``fields``, ``field``)
    and the field it points at (``source.pointer``). Nested lists are read
    too. Never other key names or links."""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, (list, tuple)):
        return " ".join(_topic_text(item) for item in raw)
    if not isinstance(raw, dict):
        return ""
    parts: list[str] = []
    for key in (*TEXT_KEYS, "code", "error_code"):
        if isinstance(raw.get(key), (str, int)):
            parts.append(str(raw[key]))
    for key in ("fields", "field"):
        if isinstance(raw.get(key), (str, list, tuple)):
            parts.append(_topic_text(raw[key]))
    source = raw.get("source")
    if isinstance(source, dict) and isinstance(source.get("pointer"), str):
        parts.append(source["pointer"])
    return " ".join(parts)


def campaign_repair_reason(raw: object, *, quote: object = ...) -> str:
    """Hail's guidance for the review topic ``raw`` names, then the carrier's
    own words (``with_carrier_text``). Codes and descriptions differ across
    carriers, so a topic match is a guess: the carrier's text says what was
    really wrong. ``raw`` is a string, a dict or a list of them; pass the
    carrier's structure, never ``str()`` of it. The topic match reads only
    its text, error codes and field pointers (``_topic_text``), never key
    names or links. ``quote``, when given, is the part of ``raw`` to quote
    (its text without a bare error code).
    """
    said = raw if quote is ... else quote
    # Split field names into words first ("MessageSamples", "MESSAGE_SAMPLES"
    # and "subUsecases" hold "message samples" and "usecases"), so the word
    # start match below still finds them.
    text = re.sub(r"(?<=[a-z])(?=[A-Z])|_", " ", _topic_text(raw)).lower()
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
        # A numeric code matches as a whole word: "309081" is not 30908.
        if any(
            re.search(rf"\b{re.escape(word)}" + (r"\b" if word.isdigit() else ""), text)
            for word in words
        ):
            return with_carrier_text(message, said)
    return with_carrier_text(CAMPAIGN_REJECTED, said)


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
