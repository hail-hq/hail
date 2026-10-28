"""The form and fixed campaign text for US A2P 10DLC registration. Both
carrier plug-ins use it, so the customer sees one form whatever the carrier.
Carrier words stay out of labels and help."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from hailhq.core.providers.verification.base import (
    Address,
    FieldOption,
    FieldSpec,
    Problem,
    Requirements,
    SubjectType,
)
from hailhq.core.providers.verification.forms import display_name

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

_BUSINESS_FIELDS = (
    FieldSpec(name="legal_name", label="Legal company name"),
    FieldSpec(name="business_type", label="Company type", options=_BUSINESS_TYPES),
    FieldSpec(
        name="ein",
        label="US employer ID (EIN)",
        pattern=r"^\d{2}-?\d{7}$",
        help="Nine digits, as on your IRS letter.",
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
        help="The carrier checks that the site names your company.",
    ),
    FieldSpec(name="rep_name", label="Contact person name"),
    FieldSpec(name="rep_email", label="Contact person email", kind="email"),
    FieldSpec(name="rep_phone", label="Contact person phone", kind="phone"),
    FieldSpec(name="rep_title", label="Contact person job title"),
)
# What the texts are about, as the carriers' sub use cases. A person (sole
# proprietor) picks 1 to 5, a company (mixed use case) 2 to 5. Twilio files
# no sub use cases; its plug-in ignores the answer.
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
_TEXT_TYPES_MIN = {"person": 1, "business": 2}
_TEXT_TYPES_MAX = 5


def _text_types_field(subject_type: SubjectType) -> FieldSpec:
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
            "The carrier texts this number to confirm. Follow the instructions in "
            "that text. Use your own "
            "mobile, not a Hail number. A mobile can confirm three registrations "
            "in its lifetime."
        ),
    ),
)
_CAMPAIGN_FIELDS = (
    FieldSpec(
        name="campaign_description",
        label="What you send",
        help="One or two sentences. Example: appointment reminders and replies to customer questions.",
    ),
    FieldSpec(name="sample_1", label="Sample message 1"),
    FieldSpec(name="sample_2", label="Sample message 2"),
    FieldSpec(
        name="opt_in_description",
        label="How people agree to receive texts",
        help=(
            "Describe the step where people agree, for example a separate SMS box "
            "on your website, unchecked by default and not required. "
            "Reviewers open your site and check that the box is separate from the "
            "terms box and that frequency, rates, STOP and HELP appear next to it."
        ),
    ),
    FieldSpec(
        name="privacy_policy_url",
        label="Privacy policy link",
        kind="url",
        help=(
            "Public page that says mobile numbers and texting consent are not "
            "shared with third parties for marketing."
        ),
    ),
    FieldSpec(
        name="terms_url",
        label="Terms of service link",
        kind="url",
        help="Public page with your terms of service.",
    ),
    FieldSpec(name="has_links", label="Do your texts contain links?", options=_YES_NO),
)


def _fields_for(subject_type: SubjectType) -> tuple[FieldSpec, ...]:
    subject = _PERSON_FIELDS if subject_type == "person" else _BUSINESS_FIELDS
    return (
        subject
        + _CAMPAIGN_FIELDS[:1]
        + (_text_types_field(subject_type),)
        + _CAMPAIGN_FIELDS[1:]
    )


def sms_requirements(provider: str, subject_type: SubjectType) -> Requirements:
    return Requirements(
        provider=provider,
        country_code="US",
        number_type="local",
        subject_type=subject_type,
        required=True,
        fields=_fields_for(subject_type),
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


def field_problems(fields: dict[str, str], subject_type: SubjectType) -> list[Problem]:
    """Required values are present and every link is an http(s) URL with a
    host, at most _URL_MAX characters. Both carrier plug-ins run this before
    anything is sent: the links are only filed later, with the campaign, where
    a carrier error is retried forever instead of shown to the customer."""
    problems: list[Problem] = []
    for spec in _fields_for(subject_type):
        value = (fields.get(spec.name) or "").strip()
        if not value:
            if spec.required:
                problems.append(Problem(field=spec.name, message="This is required."))
        elif spec.kind == "choices":
            problem = _choices_problem(spec, value)
            if problem:
                problems.append(Problem(field=spec.name, message=problem))
        elif spec.kind == "url" and len(value) > _URL_MAX:
            problems.append(Problem(field=spec.name, message="Use a shorter link."))
        elif spec.kind == "url" and not _is_http_url(value):
            problems.append(
                Problem(
                    field=spec.name,
                    message="Enter the full link, starting with https://.",
                )
            )
    return problems


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


def _is_http_url(value: str) -> bool:
    parts = urlparse(value)
    return parts.scheme in ("http", "https") and bool(parts.netloc)


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


_OPT_OUT = " Reply STOP to opt out, HELP for help."
_FLOW_MAX = 2048  # carrier maximum for the opt-in description
# Per link. The fixed part of the flow is at most 1 + 137 (disclosures) + 17
# + _URL_MAX + 8 + _URL_MAX = 1187 characters, so the customer's own text
# always keeps at least 861 of the 2048.
_URL_MAX = 512
_DISCLOSURES = (
    "Consent is not a condition of purchase. Message frequency varies. "
    "Message and data rates may apply. Reply STOP to opt out, HELP for help."
)


def _at_least(text: str, n: int, pad: str) -> str:
    text = text.strip()
    return text if len(text) >= n else (text + " " + pad).strip()


def campaign_text(fields: dict[str, str], subject_type: SubjectType) -> CampaignText:
    """Carrier minimums: description 40+, message flow 40+, samples 20+."""
    description = _at_least(
        fields["campaign_description"],
        40,
        "Messages are sent only to people who agreed to receive them.",
    )
    privacy_policy_url = fields["privacy_policy_url"].strip()
    terms_url = fields["terms_url"].strip()
    # Reviewers reject a filing that lacks the consent disclosures and the
    # policy links, so they go on every filing after the customer's own text.
    # No punctuation right after a link: a reviewer who copies it must not get
    # a trailing dot.
    fixed = f" {_DISCLOSURES} Privacy policy: {privacy_policy_url} Terms: {terms_url}"
    own = fields["opt_in_description"].strip()[: _FLOW_MAX - len(fixed)]
    flow = (own + fixed).strip()
    samples = [
        _at_least(fields["sample_1"], 20, _OPT_OUT),
        _at_least(fields["sample_2"], 20, _OPT_OUT),
    ]
    return CampaignText(
        use_case="SOLE_PROPRIETOR" if subject_type == "person" else "MIXED",
        description=description,
        message_flow=flow,
        samples=samples,
        has_links=fields["has_links"].upper() == "YES",
        privacy_policy_url=privacy_policy_url,
        terms_url=terms_url,
        text_types=picked(fields.get("text_types")),
    )
