"""The evidence form and truthful campaign text for US A2P 10DLC registration. Both
carrier plug-ins use it, so the customer sees one form whatever the carrier.
Carrier words stay out of labels and help."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from hailhq.core.providers.verification.base import (
    Address,
    FieldOption,
    FieldSpec,
    Problem,
    Requirements,
    SmsRegistrationProvider,
    SubjectType,
)
from hailhq.core.providers.verification.forms import (
    display_name,
    text_length,
)

_BUSINESS_TYPES = (
    FieldOption(key="PRIVATE_PROFIT", label="Private company"),
    FieldOption(key="PUBLIC_PROFIT", label="Publicly traded company"),
    FieldOption(key="NON_PROFIT", label="Non-profit"),
    FieldOption(key="GOVERNMENT", label="Government"),
)
_VERTICALS = (
    ("PROFESSIONAL", "Professional services"),
    ("REAL_ESTATE", "Real estate"),
    ("HEALTHCARE", "Healthcare"),
    ("HUMAN_RESOURCES", "Human resources"),
    ("ENERGY", "Energy"),
    ("ENTERTAINMENT", "Entertainment"),
    ("RETAIL", "Retail"),
    ("TRANSPORTATION", "Transportation"),
    ("AGRICULTURE", "Agriculture"),
    ("INSURANCE", "Insurance"),
    ("POSTAL", "Postal"),
    ("EDUCATION", "Education"),
    ("HOSPITALITY", "Hospitality"),
    ("FINANCIAL", "Financial"),
    ("POLITICAL", "Political"),
    ("GAMBLING", "Gambling"),
    ("LEGAL", "Legal"),
    ("CONSTRUCTION", "Construction"),
    ("NGO", "Non-governmental organization"),
    ("MANUFACTURING", "Manufacturing"),
    ("GOVERNMENT", "Government"),
    ("TECHNOLOGY", "Technology"),
    ("COMMUNICATION", "Communication"),
)
_YES_NO = (FieldOption(key="YES", label="Yes"), FieldOption(key="NO", label="No"))
# Carrier maximums, in characters: campaign description and each sample.
_DESCRIPTION_MAX = 4096
_SAMPLE_MAX = 1024
_FLOW_MAX = 2048  # carrier maximum for the opt-in description
_URL_MAX = 512  # per link
_MESSAGE_DATA_RATES = re.compile(
    r"\b(?:messages?|msgs?|messaging)\.?\s*(?:and|&|/|\+)\s*data\s+(?:rates?|charges?)\b",
    re.IGNORECASE,
)


def _answer(fields: dict[str, str], name: str) -> str:
    """Trim answers consistently in validation and the persisted filing text."""
    return fields.get(name, "").strip()


# New applications describe their actual consent journey. Never append claims
# about consent, disclosures or confirmation that the applicant did not supply.
_OPT_IN_MAX = 1024
# Carriers file at most 20 characters (Telnyx `ein`).
_TAX_NUMBER_MAX = 20
# US EINs and Canadian Business Numbers are nine digits.
NINE_DIGIT_COUNTRIES = frozenset({"US", "CA"})


# The VAT prefix to strip, per country: the ISO code before a digit, unless
# listed. Greece uses EL. An Austrian number keeps its U ("ATU12345678" files
# as "U12345678").
_VAT_PREFIXES = {"GR": r"(EL|GR)(?=[0-9])", "AT": r"AT(?=U[0-9])"}


def tax_number(value: str | None, country: str | None) -> str:
    """The business tax number as carriers file it: letters and digits,
    uppercase, without the VAT prefix of the issuing country.
    "SE 556677-8899 01" from Sweden files as "556677889901". A full Canadian
    Business Number files as its first 9 digits."""
    text = re.sub(r"[^0-9A-Za-z]", "", value or "").upper()
    code = (country or "").strip().upper()
    if code == "CA":
        full = re.fullmatch(r"([0-9]{9})[A-Z]{2}[0-9]{4}", text)
        return full.group(1) if full else text
    if code in NINE_DIGIT_COUNTRIES or not re.fullmatch(r"[A-Z]{2}", code):
        return text
    prefix = _VAT_PREFIXES.get(code, rf"{code}(?=[0-9])")
    return re.sub(rf"^{prefix}", "", text)


_ELIGIBILITY_FIELDS = (
    FieldSpec(
        name="sender_country",
        label="Country where the sender is based",
        pattern=r"^[A-Z]{2}$",
        help="Two-letter country code. This must match your address.",
    ),
    FieldSpec(
        name="has_tax_id",
        label="Does the sender have a business tax registration number?",
        options=_YES_NO,
        help="Answer from your actual registration, even if you work alone. A company with a tax or registration number from any country uses the business route.",
    ),
)

_BUSINESS_FIELDS = (
    FieldSpec(name="legal_name", label="Legal company name"),
    FieldSpec(name="business_type", label="Company type", options=_BUSINESS_TYPES),
    FieldSpec(
        name="registration_country",
        label="Country issuing the business tax number",
        pattern=r"^[A-Z]{2}$",
        max_length=2,
        help="Two letters, like US or SE.",
    ),
    FieldSpec(
        name="ein",
        label="Business tax number",
        pattern=r"^[0-9A-Za-z][0-9A-Za-z .\-/]*$",
        max_length=30,
        help="US: your EIN. Canada: your Business Number. Other countries: your VAT or company number.",
    ),
    FieldSpec(
        name="industry",
        label="Industry",
        options=tuple(FieldOption(key=k, label=v) for k, v in _VERTICALS),
    ),
    FieldSpec(
        name="website",
        label="Website",
        kind="url",
        max_length=_URL_MAX,
        help="Reviewers check that the site names your company.",
    ),
    FieldSpec(name="rep_name", label="Contact person name"),
    FieldSpec(name="rep_email", label="Contact person email", kind="email"),
    FieldSpec(name="rep_phone", label="Contact person phone", kind="phone"),
    FieldSpec(name="rep_title", label="Contact person job title"),
)
# What the texts are about. Both routes select one to five real purposes;
# companies with exactly one purpose file that specific use case.
_TEXT_TYPES = (
    FieldOption(key="CUSTOMER_CARE", label="Customer care and appointments"),
    FieldOption(key="ACCOUNT_NOTIFICATION", label="Account notices and reminders"),
    FieldOption(key="DELIVERY_NOTIFICATION", label="Delivery and shipping updates"),
    FieldOption(key="2FA", label="Login and verification codes"),
    FieldOption(key="SECURITY_ALERT", label="Security alerts"),
    FieldOption(key="FRAUD_ALERT", label="Fraud alerts"),
    FieldOption(key="MARKETING", label="Marketing and promotions"),
    FieldOption(key="POLLING_VOTING", label="Polls and surveys"),
    FieldOption(key="HIGHER_EDUCATION", label="School and college updates"),
    FieldOption(
        key="PUBLIC_SERVICE_ANNOUNCEMENT", label="Public service announcements"
    ),
)
_TEXT_TYPES_MIN = {"person": 1, "business": 1}
_TEXT_TYPES_MAX = 5


def text_types_field(subject_type: SubjectType) -> FieldSpec:
    least = _TEXT_TYPES_MIN[subject_type]
    return FieldSpec(
        name="text_types",
        label="What kinds of texts do you send?",
        kind="choices",
        options=_TEXT_TYPES,
        min_choices=least,
        max_choices=_TEXT_TYPES_MAX,
        help=(
            f"Pick {least} to {_TEXT_TYPES_MAX}. Reviewers check them against "
            "your sample messages."
        ),
    )


_PERSON_FIELDS = (
    FieldSpec(name="first_name", label="First name"),
    FieldSpec(name="last_name", label="Last name"),
    FieldSpec(name="email", label="Email", kind="email"),
    FieldSpec(
        name="mobile_phone_number",
        label="Your mobile number",
        kind="phone",
        help=(
            "You get a text on this number to confirm. Follow the instructions in "
            "that text. Use your own "
            "mobile, not a Hail number. A mobile can confirm three registrations "
            "in its lifetime."
        ),
    ),
)
_CONSENT_METHODS = (
    FieldOption(key="WEBSITE", label="Public website or app"),
    FieldOption(key="PRIVATE_FORM", label="Private or login-only form"),
    FieldOption(key="VERBAL", label="Verbal agreement"),
    FieldOption(key="PAPER", label="Paper form"),
    FieldOption(key="KEYWORD", label="Text a keyword"),
    FieldOption(key="QR", label="Scan a QR code"),
    FieldOption(key="INBOUND", label="Customer initiates a conversation"),
)
# Each selected method must have its own complete, reviewable journey.
_METHOD_FIELDS = {
    "WEBSITE": ("website_url", "website_evidence_url", "website_action"),
    "PRIVATE_FORM": (
        "private_form_url",
        "private_form_evidence_url",
        "private_form_action",
    ),
    "VERBAL": ("verbal_script", "verbal_evidence_url", "verbal_action"),
    "PAPER": ("paper_evidence_url", "paper_action"),
    "KEYWORD": (
        "keyword_keyword",
        "keyword_number",
        "keyword_evidence_url",
        "keyword_advertisement",
    ),
    "QR": ("qr_url", "qr_evidence_url", "qr_advertisement"),
    "INBOUND": ("inbound_number", "inbound_evidence_url", "inbound_advertisement"),
}
# Optional for its method. Carriers ask for the staff script and the
# confirmation text for verbal consent, not a picture of it.
_OPTIONAL_METHOD_FIELDS = frozenset({"verbal_evidence_url"})
_EVIDENCE_HELP = (
    "A public link to a screenshot of where recipients agree, with personal "
    "information removed. Carrier reviewers open it without signing in. No link? "
    "Upload the file with POST /verifications/sms/evidence and use the url it returns."
)
_METHOD_LABELS = {
    "url": "Exact enrollment page URL",
    "evidence_url": "Public redacted evidence URL",
    "action": "How and where the recipient affirmatively agrees",
    "script": "Full staff consent script",
    "keyword": "Exact opt-in keyword",
    "number": "Number recipients text (international format)",
    "advertisement": "Where recipients see the invitation and its exact wording",
}


def _method_spec(method: str, name: str) -> FieldSpec:
    suffix = name.removeprefix(method.lower() + "_")
    label = next(o.label for o in _CONSENT_METHODS if o.key == method)
    return FieldSpec(
        name=name,
        label=f"{label}: {_METHOD_LABELS[suffix]}",
        required=False,
        kind="url" if suffix.endswith("url") else "text",
        max_length=_URL_MAX if suffix.endswith("url") else 1024,
        help=(
            _EVIDENCE_HELP
            + (
                " Optional for verbal consent."
                if name in _OPTIONAL_METHOD_FIELDS
                else ""
            )
            if suffix == "evidence_url"
            else "Required for this consent method. Use real, accessible evidence with personal information removed; never publish recipient records or identity documents."
        ),
    )


_CAMPAIGN_FIELDS = (
    FieldSpec(
        name="program_name", label="Public sender or program name", max_length=100
    ),
    FieldSpec(
        name="campaign_description",
        label="Who receives your texts, what triggers them and what they contain",
        max_length=_DESCRIPTION_MAX,
        help="At least 40 characters describing the real messaging program.",
    ),
    FieldSpec(
        name="message_frequency",
        label="Exact frequency wording in the consent disclosure",
        max_length=120,
        help="Copy the actual cadence shown to recipients; this exact text must appear in the disclosure.",
    ),
    *(
        spec
        for i in range(1, 6)
        for spec in (
            FieldSpec(
                name=f"sample_{i}",
                label=f"Actual sample message {i}",
                max_length=_SAMPLE_MAX,
                required=i <= 2,
                help="At least 20 characters. Include the public sender name and actual opt-out instructions. Cover every selected purpose.",
            ),
            FieldSpec(
                name=f"sample_{i}_type",
                label=f"Purpose of sample {i}",
                options=_TEXT_TYPES,
                required=i <= 2,
            ),
        )
    ),
    FieldSpec(
        name="consent_methods",
        label="Every way recipients agree to texts",
        kind="choices",
        options=_CONSENT_METHODS,
        min_choices=1,
        max_choices=7,
    ),
    FieldSpec(
        name="opt_in_description",
        label="Actual recipient consent journey",
        max_length=_OPT_IN_MAX,
        help="Describe each step from seeing the invitation to agreeing. Website SMS controls must be optional and initially unchecked, separate from general terms.",
    ),
    FieldSpec(
        name="consent_disclosure",
        label="Exact SMS disclosure recipients see or hear",
        max_length=1024,
        help="Include your public sender name, purposes, frequency, message/data rates, STOP, HELP and policy links. Copy the actual disclosure; fix your enrollment flow before filing if anything is missing.",
    ),
    FieldSpec(
        name="disclosure_purposes_confirmed",
        label="Does this exact disclosure cover every selected messaging purpose?",
        options=_YES_NO,
        help="Confirm the disclosure actually shown to recipients includes all the purposes you selected. This is your attestation, not an automated website or legal compliance check.",
    ),
    FieldSpec(
        name="consent_records",
        label="How you retain evidence of permission",
        max_length=512,
        help="Describe the private record of the agreement, date, scope and disclosure version. Do not include recipient records here.",
    ),
    FieldSpec(
        name="consent_optional",
        label="Is SMS consent optional, separate and initially unselected?",
        options=_YES_NO,
    ),
    FieldSpec(
        name="marketing_consent",
        label="Do recipients separately give written agreement to promotional texts?",
        options=_YES_NO,
        required=False,
        help="Required for marketing. An inbound question or verbal agreement alone does not authorize recurring promotions.",
    ),
    FieldSpec(
        name="evidence_redacted",
        label="Is evidence public, current, and free of personal information?",
        options=_YES_NO,
    ),
    *(
        _method_spec(method, name)
        for method, names in _METHOD_FIELDS.items()
        for name in names
    ),
    FieldSpec(
        name="privacy_policy_url",
        label="Sender privacy policy URL",
        kind="url",
        max_length=_URL_MAX,
        help="Your public policy must explain that mobile numbers and SMS consent are not shared for third-party marketing, while accurately describing delivery service providers.",
    ),
    FieldSpec(
        name="terms_url",
        label="Sender SMS terms URL",
        kind="url",
        max_length=_URL_MAX,
        help="Public program terms: sender, purposes, frequency, rates, STOP, HELP and support contact.",
    ),
    FieldSpec(
        name="policies_confirmed",
        label="Do these live policies accurately cover this sender and program?",
        options=_YES_NO,
    ),
    FieldSpec(
        name="opt_in_message",
        label="Actual subscription confirmation",
        required=False,
        max_length=320,
        help="Enter the confirmation your existing system actually sends, including the public program name, enrollment, the exact frequency wording above, message and data rates, HELP and STOP. Required for keyword enrollment and when this form marks it required. Registration declares this response; it does not configure or send it.",
    ),
    FieldSpec(
        name="has_links", label="Do any messages contain links?", options=_YES_NO
    ),
    FieldSpec(
        name="has_phone",
        label="Do any messages contain phone numbers?",
        options=_YES_NO,
    ),
    FieldSpec(
        name="age_gated",
        label="Does the program require age verification?",
        options=_YES_NO,
    ),
    FieldSpec(
        name="age_verification_description",
        label="Actual age verification process",
        required=False,
        max_length=512,
        help="Required for age-gated programs. Describe the age threshold and how it is verified before enrollment.",
    ),
    FieldSpec(
        name="age_verification_evidence_url",
        label="Public redacted age-verification evidence URL",
        kind="url",
        required=False,
        max_length=_URL_MAX,
    ),
    FieldSpec(
        name="direct_lending",
        label="Does the sender offer direct lending?",
        options=_YES_NO,
    ),
    FieldSpec(
        name="restricted_content",
        label="Does the program involve restricted content?",
        options=(
            FieldOption(key="NONE", label="None of the listed categories"),
            FieldOption(key="RESTRICTED", label="A listed category or unsure"),
        ),
        help="Stop for review: cannabis/CBD, illegal drugs, gambling, sex/adult services, firearms, tobacco/vaping, high-risk financial offers (payday loans, debt relief/collection, get-rich-quick), third-party lead generation or purchased/shared consent. Age verification does not make prohibited traffic eligible.",
    ),
)


def _fields_for(
    provider: SmsRegistrationProvider, subject_type: SubjectType
) -> tuple[FieldSpec, ...]:
    """The shared form with the carrier's extra fields and limits."""
    subject = _PERSON_FIELDS if subject_type == "person" else _BUSINESS_FIELDS
    extra = tuple(
        f for f in provider.extra_fields(subject_type) if f.name != "text_types"
    )
    fields = (
        _ELIGIBILITY_FIELDS
        + subject
        + (text_types_field(subject_type),)
        + extra
        + _CAMPAIGN_FIELDS
    )
    limits = provider.field_limits
    result = []
    for spec in fields:
        updates = {}
        if spec.name in limits:
            updates["max_length"] = limits[spec.name]
        if spec.name == "opt_in_message" and provider.opt_in_message_required:
            updates["required"] = True
        result.append(spec.model_copy(update=updates) if updates else spec)
    return tuple(result)


def sms_requirements(
    provider: SmsRegistrationProvider, subject_type: SubjectType
) -> Requirements:
    return Requirements(
        provider=provider.name,
        country_code="US",
        number_type="local",
        subject_type=subject_type,
        required=True,
        fields=_fields_for(provider, subject_type),
        documents=(),
        address_required=True,
        subject_types=("person", "business"),
    )


def sms_subject_name(
    fields: dict[str, str], address: Address | None, subject_type: SubjectType
) -> str | None:
    """``legal_name`` for a business, ``first_name`` and ``last_name`` for a
    person, else the address's ``customer_name``."""
    if subject_type == "business":
        name = display_name(fields.get("legal_name"))
    else:
        name = display_name(fields.get("first_name"), fields.get("last_name"))
    return name or display_name(address.customer_name if address else None)


def field_problems(
    provider: SmsRegistrationProvider, fields: dict[str, str], subject_type: SubjectType
) -> list[Problem]:
    """The fields of ``provider``'s form: required values are present, the
    choices are allowed, and every link is an http(s) URL with a host. Both
    carrier plug-ins run this before anything is sent: the links are only
    filed later, with the campaign, where a carrier error is retried forever
    instead of shown to the customer. Lengths (``max_length``, links
    included) are checked by the API for every carrier
    (``forms.length_problems``)."""
    problems: list[Problem] = []
    for spec in _fields_for(provider, subject_type):
        value = (fields.get(spec.name) or "").strip()
        if not value:
            if spec.required:
                problems.append(Problem(field=spec.name, message="This is required."))
        elif spec.kind == "choices":
            problem = _choices_problem(spec, value)
            if problem:
                problems.append(Problem(field=spec.name, message=problem))
        elif spec.options and value not in {o.key for o in spec.options}:
            problems.append(Problem(field=spec.name, message="Pick from the list."))
        elif spec.kind == "url" and not _is_public_https_url(value):
            problems.append(
                Problem(
                    field=spec.name,
                    message="Enter the full link to a real public https:// page, without credentials or placeholders.",
                )
            )
    return problems + _application_problems(fields, subject_type)


def picked(value: str | None) -> list[str]:
    """The option keys of a 'choices' value, in order, without repeats."""
    keys = [k.strip() for k in (value or "").split(",")]
    return list(dict.fromkeys(k for k in keys if k))


def _choices_problem(spec: FieldSpec, value: str) -> str | None:
    keys = picked(value)
    allowed = {o.key for o in spec.options or ()}
    if any(k not in allowed for k in keys):
        return "Pick from the list."
    if spec.min_choices and len(keys) < spec.min_choices:
        return f"Pick at least {spec.min_choices}."
    if spec.max_choices and len(keys) > spec.max_choices:
        return f"Pick at most {spec.max_choices}."
    return None


def _is_public_https_url(value: str) -> bool:
    try:
        parts = urlparse(value)
        host = (parts.hostname or "").lower().rstrip(".")
        if (
            parts.scheme != "https"
            or not host
            or parts.username
            or parts.password
            or parts.port not in (None, 443)
        ):
            return False
        if re.search(r"\s|[<>\\]", value) or not re.fullmatch(
            r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", host
        ):
            return False
        if any(
            host == domain or host.endswith("." + domain)
            for domain in ("example.com", "example.org", "example.net")
        ) or host.endswith(
            (".example", ".test", ".invalid", ".localhost", ".local", ".internal")
        ):
            return False
        try:
            ipaddress.ip_address(host)
            return False  # evidence and policy links must use a public hostname
        except ValueError:
            return True
    except ValueError:
        return False


def _application_problems(
    fields: dict[str, str], subject_type: SubjectType
) -> list[Problem]:
    problems: list[Problem] = []

    def issue(name: str, message: str) -> None:
        problems.append(Problem(field=name, message=message))

    def val(name: str) -> str:
        return _answer(fields, name)

    country = val("sender_country")
    if country and not re.fullmatch(r"[A-Z]{2}", country):
        issue("sender_country", "Use a two-letter uppercase country code.")
    if subject_type == "person":
        if country and country not in {"US", "CA"}:
            issue(
                "sender_country",
                "The individual route supports senders based in the US or Canada only.",
            )
        if val("has_tax_id") == "YES":
            issue(
                "has_tax_id",
                "A sender with a business tax number must use the business route, even if working alone.",
            )
    elif val("has_tax_id") == "NO":
        issue(
            "has_tax_id",
            "The business route needs a business tax or registration number.",
        )
    issuer = val("registration_country")
    if subject_type == "business" and issuer and not re.fullmatch(r"[A-Z]{2}", issuer):
        issue("registration_country", "Use a two-letter uppercase country code.")
    if subject_type == "business" and val("ein"):
        number = tax_number(val("ein"), issuer)
        if issuer in NINE_DIGIT_COUNTRIES:
            if not re.fullmatch(r"[0-9]{9}", number):
                issue(
                    "ein",
                    (
                        "Enter your 9-digit Business Number, or the full 15 characters like 123456789RT0001."
                        if issuer == "CA"
                        else "Enter your 9-digit EIN."
                    ),
                )
        elif not (4 <= len(number) <= _TAX_NUMBER_MAX and re.search(r"[0-9]", number)):
            issue(
                "ein",
                f"Enter your VAT or company registration number: 4 to {_TAX_NUMBER_MAX} letters and digits.",
            )
    for name in (
        "consent_optional",
        "evidence_redacted",
        "policies_confirmed",
        "disclosure_purposes_confirmed",
    ):
        if val(name) == "NO":
            issue(name, "Make this true in your actual program before submitting.")
    if val("restricted_content") == "RESTRICTED":
        issue(
            "restricted_content",
            "This program needs a supported-use review before registration. Contact support with the actual use case.",
        )
    if val("age_gated") == "YES":
        for name in ("age_verification_description", "age_verification_evidence_url"):
            if not val(name):
                issue(
                    name,
                    "Describe and provide redacted evidence of the actual age verification before enrollment.",
                )
    if val("direct_lending") == "YES":
        issue(
            "direct_lending",
            "Direct lending needs a supported-use review before self-service registration. Contact support with the lender and exact products; high-risk lending is prohibited.",
        )
    purposes = picked(val("text_types"))
    methods = picked(val("consent_methods"))
    if "MARKETING" in purposes and val("marketing_consent") != "YES":
        issue(
            "marketing_consent",
            "Promotional messages require separate written agreement covering marketing.",
        )
    if "MARKETING" in purposes and any(m in methods for m in ("VERBAL", "INBOUND")):
        issue(
            "consent_methods",
            "Verbal permission or an inbound inquiry alone cannot enroll recipients for promotions. Describe the written marketing enrollment method.",
        )
    for method in methods:
        for name in _METHOD_FIELDS.get(method, ()):
            if not val(name):
                if name not in _OPTIONAL_METHOD_FIELDS:
                    issue(name, "This is required for the selected consent method.")
            elif name.endswith("_number") and not re.fullmatch(
                r"\+[1-9][0-9]{7,14}", val(name)
            ):
                issue(
                    name,
                    "Enter the actual destination number in international format, such as +12125550123.",
                )
    if val("keyword_keyword") and not re.fullmatch(
        r"[A-Za-z0-9]{1,20}", val("keyword_keyword")
    ):
        issue(
            "keyword_keyword",
            "Enter the single advertised keyword, using letters and digits.",
        )
    if "KEYWORD" in methods and not val("opt_in_message"):
        issue(
            "opt_in_message",
            "Keyword enrollment requires the actual confirmation your existing system sends. Set up that response before filing; registration does not configure it.",
        )
    if val("opt_in_message") and text_length(val("opt_in_message")) < 20:
        issue(
            "opt_in_message",
            "Use at least 20 characters with the actual subscription confirmation.",
        )
    confirmation = val("opt_in_message")
    if confirmation:
        missing: list[str] = []
        if (
            not val("program_name")
            or val("program_name").casefold() not in confirmation.casefold()
        ):
            missing.append("public sender/program name")
        if (
            not val("message_frequency")
            or val("message_frequency").casefold() not in confirmation.casefold()
        ):
            missing.append("exact message frequency")
        if not _MESSAGE_DATA_RATES.search(confirmation):
            missing.append("message and data rates")
        for keyword in ("HELP", "STOP"):
            if not re.search(rf"\b{keyword}\b", confirmation, re.IGNORECASE):
                missing.append(keyword + " instructions")
        if missing:
            issue(
                "opt_in_message",
                "Your actual subscription confirmation must include "
                + ", ".join(missing)
                + ". Update the real response before filing; Hail will not add this wording for you.",
            )
    if "KEYWORD" in methods and val("keyword_keyword").upper() in {
        "STOP",
        "STOPALL",
        "UNSUBSCRIBE",
        "CANCEL",
        "END",
        "QUIT",
        "HELP",
        "INFO",
    }:
        issue(
            "keyword_keyword",
            "Choose your advertised opt-in keyword. STOP/opt-out and HELP/info keywords cannot enroll recipients.",
        )
    for name, minimum in (("campaign_description", 40), ("opt_in_description", 40)):
        if val(name) and text_length(val(name)) < minimum:
            issue(
                name,
                f"Use at least {minimum} characters describing the actual program.",
            )
    covered: set[str] = set()
    for i in range(1, 6):
        name = f"sample_{i}"
        sample, purpose = val(name), val(name + "_type")
        if sample:
            if text_length(sample) < 20:
                issue(
                    name,
                    "Use at least 20 characters with the actual sender and message content.",
                )
            if not purpose or purpose not in purposes:
                issue(
                    name + "_type",
                    "Choose one of your selected messaging purposes for this sample.",
                )
            else:
                covered.add(purpose)
            if (
                val("program_name")
                and val("program_name").casefold() not in sample.casefold()
            ):
                issue(
                    name,
                    "Include the public sender or program name so recipients can identify you.",
                )
            if not re.search(r"\bSTOP\b", sample, re.IGNORECASE):
                issue(
                    name, "Include the actual STOP opt-out instruction in this sample."
                )
            if val("has_links") == "NO" and re.search(
                r"https?://|www\.|\b[a-z0-9-]+\.(?:com|org|net|io|co)(?:/|\b)",
                sample,
                re.IGNORECASE,
            ):
                issue(
                    "has_links",
                    "A sample contains a link. Answer Yes or correct the sample to match the real messages.",
                )
            if val("has_phone") == "NO" and any(
                len(re.sub(r"\D", "", number)) >= 10
                or (number.startswith("+") and len(re.sub(r"\D", "", number)) >= 8)
                for number in re.findall(r"(?<!\w)\+?\d[\d ().-]{6,}\d(?!\w)", sample)
            ):
                issue(
                    "has_phone",
                    "A sample contains a phone number. Answer Yes or correct the sample to match the real messages.",
                )
        elif purpose:
            issue(name, "Provide the sample for this purpose.")
    if purposes and set(purposes) - covered:
        issue(
            "text_types",
            "Provide a sample for every selected purpose: "
            + ", ".join(p for p in purposes if p not in covered)
            + ".",
        )
    disclosure = val("consent_disclosure")
    if disclosure:
        required = [
            val("program_name"),
            val("message_frequency"),
            "STOP",
            "HELP",
            val("privacy_policy_url"),
            val("terms_url"),
        ]
        if any(
            part and part.casefold() not in disclosure.casefold() for part in required
        ) or not _MESSAGE_DATA_RATES.search(disclosure):
            issue(
                "consent_disclosure",
                "The actual disclosure must identify your program and include your exact frequency wording, message and data rates, STOP, HELP and both policy links. Update the real flow before submitting.",
            )
    if methods and text_length(_structured_flow(fields)) > _FLOW_MAX:
        issue(
            "opt_in_description",
            "The complete consent narrative exceeds 2048 characters. Shorten descriptions while retaining every method, exact disclosure and evidence link; keep full scripts in the linked evidence.",
        )
    return problems


def _structured_flow(fields: dict[str, str]) -> str:
    parts = [fields.get("opt_in_description", "").strip()]
    for method in picked(fields.get("consent_methods")):
        detail = [
            f"{name.removeprefix(method.lower() + '_').replace('_', ' ')}: {fields.get(name, '').strip()}"
            for name in _METHOD_FIELDS.get(method, ())
            if fields.get(name, "").strip()
        ]
        parts.append(f"{method}: " + "; ".join(detail))
    for name, label in (
        ("consent_disclosure", "Disclosure"),
        ("message_frequency", "Frequency"),
        ("consent_records", "Consent records"),
        ("age_verification_description", "Age verification"),
        ("age_verification_evidence_url", "Age verification evidence"),
    ):
        if fields.get(name, "").strip():
            parts.append(f"{label}: {fields[name].strip()}")
    if _answer(fields, "consent_optional") == "YES":
        parts.append(
            "Applicant confirms SMS consent is optional, separate and initially unselected."
        )
    if _answer(fields, "marketing_consent") == "YES":
        parts.append(
            "Applicant confirms separate written agreement to promotional messages."
        )
    return "\n".join(p for p in parts if p)


@dataclass(frozen=True)
class CampaignText:
    use_case: str
    description: str
    message_flow: str
    samples: list[str]
    has_links: bool
    privacy_policy_url: str
    terms_url: str
    text_types: list[str]
    contact_email: str
    has_phone: bool = False
    age_gated: bool = False
    direct_lending: bool = False
    opt_in_message: str = ""
    opt_in_keywords: list[str] = field(default_factory=list)
    schema_version: int = 1


def campaign_text(fields: dict[str, str], subject_type: SubjectType) -> CampaignText:
    """Compile only applicant-supplied facts; reading old saved forms stays safe.

    New submissions must pass field_problems and length_problems first. In
    particular, never truncate evidence or pad text with invented statements.
    """
    purposes = picked(fields.get("text_types"))
    structured = bool(_answer(fields, "consent_methods"))
    return CampaignText(
        use_case=(
            "SOLE_PROPRIETOR"
            if subject_type == "person"
            else (purposes[0] if len(purposes) == 1 else "MIXED")
        ),
        description=fields["campaign_description"].strip(),
        message_flow=(
            _structured_flow(fields)
            if structured
            else fields["opt_in_description"].strip()
        ),
        samples=[
            fields[f"sample_{i}"].strip()
            for i in range(1, 6)
            if fields.get(f"sample_{i}", "").strip()
        ],
        has_links=_answer(fields, "has_links").upper() == "YES",
        privacy_policy_url=fields["privacy_policy_url"].strip(),
        terms_url=fields["terms_url"].strip(),
        text_types=purposes,
        contact_email=fields.get(
            "email" if subject_type == "person" else "rep_email", ""
        ).strip(),
        has_phone=_answer(fields, "has_phone").upper() == "YES",
        age_gated=_answer(fields, "age_gated").upper() == "YES",
        direct_lending=_answer(fields, "direct_lending").upper() == "YES",
        opt_in_message=fields.get("opt_in_message", "").strip(),
        opt_in_keywords=(
            [fields["keyword_keyword"].strip()]
            if "KEYWORD" in picked(fields.get("consent_methods"))
            and fields.get("keyword_keyword", "").strip()
            else []
        ),
        schema_version=2 if structured else 1,
    )
