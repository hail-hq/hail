"""Carrier requirements sorted into one fixed list of categories.

Each carrier words its paperwork its own way. ``classify`` maps every
requirement to one of the categories below, so the console can tell a person
or a company what to bring without naming a carrier. Anything not known
becomes ``other`` and keeps the carrier's raw label.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from hailhq.core.providers.verification.base import VerificationRequirements

__all__ = [
    "LABELS",
    "Category",
    "StoredItem",
    "classify",
    "is_company_document",
    "paperwork",
]

logger = logging.getLogger(__name__)

Category = Literal[
    "id_document",
    "birth_date",
    "personal_details",
    "contact_details",
    "proof_of_address",
    "address_in_country",
    "address_in_area",
    "company_registration",
    "company_id",
    "company_name",
    "local_usage",
    "other",
]
StoredItem = str | dict[str, str]

LABELS: dict[str, str] = {
    "id_document": "ID card or passport",
    "birth_date": "Date of birth",
    "personal_details": "Nationality and job",
    "contact_details": "Name, email and phone",
    "proof_of_address": "Proof of address (bill or statement under 3 months)",
    "address_in_country": "An address in the country",
    "address_in_area": "An address in the number's area",
    "company_registration": "Company register extract or certificate",
    "company_id": "Company or tax number",
    "company_name": "Company legal name",
    "local_usage": "Proof you use the number locally",
    "other": "",
}
_ORDER = list(LABELS)

# An item that is asked but is not paperwork the customer must prepare.
_IGNORED = "ignored"
_NONE: tuple[str, ...] = ()

# Telnyx requirement ids are the same in every country.
_TELNYX_IDS: dict[str, tuple[str, ...]] = {
    "2708e569-696a-4fc7-9305-5fdb3eb9c7dd": ("contact_details",),
    "bf1cb27c-3693-41af-bbf6-ff4de8d41006": ("contact_details",),
    "d3641c72-734e-4421-8bca-510b4137c0c5": ("company_name",),
    "f8353843-d1fe-4ff3-85cf-9d4d63e67419": ("company_id",),
    "0b42b42a-d827-4b53-a2bb-8eeef919f798": ("company_id",),
    "1ce58926-d0d9-41c7-be1b-23b7bbbc572d": _NONE,
    "c34075fd-dde8-4e5d-b2d1-73155062e77a": _NONE,
    "0ba49bfa-8a0b-4e38-95ba-f1291507fad4": ("proof_of_address",),
    "b0197fa1-c2fd-4500-9875-2c658b2396eb": ("proof_of_address",),
    "809f526a-98b7-4a38-840e-b86d19bd738c": ("company_registration",),
    "c6ef8336-b094-4b04-8073-0f6fc4dd42b9": ("id_document",),
}

# Twilio machine names. A name that depends on the subject is in
# ``_TWILIO_BY_SUBJECT`` as (person, business).
_TWILIO_FIELDS: dict[str, tuple[str, ...]] = {
    "first_name": ("contact_details",),
    "last_name": ("contact_details",),
    "email": ("contact_details",),
    "phone_number": ("contact_details",),
    "birth_date": ("birth_date",),
    "birth_place": ("personal_details",),
    "nationality": ("personal_details",),
    "profession": ("personal_details",),
    "business_name": ("company_name",),
    "business_registration_number": ("company_id",),
    "enterprise_id_number": ("company_id",),
    "vat": ("company_id",),
    "comments": _NONE,
    "business_registration_identifier": _NONE,
    "business_website": _NONE,
    "business_identity": _NONE,
    "is_subassigned": _NONE,
}
_TWILIO_DOCS: dict[str, tuple[str, ...]] = {
    "proof_of_identity": ("id_document",),
    "birth_date_info": ("birth_date",),
    "type_no_and_issuing_country_of_official_id_info": ("id_document",),
    "individual_address_info": ("proof_of_address",),
    "individual_address_proof_info": ("proof_of_address",),
    "address_proof_info": ("proof_of_address",),
    "business_address_proof_info": ("proof_of_address",),
    "business_address": ("proof_of_address",),
    "registered_office_address_proof_info": ("proof_of_address",),
    "business_registration_number_info": ("company_registration",),
    "business_name_info": ("company_registration",),
}
_TWILIO_BY_SUBJECT = {"name_info": (("id_document",), ("company_registration",))}

# DIDWW field keys and proof slots (slot names end in _1, _2, ...).
_DIDWW_FIELDS: dict[str, tuple[str, ...]] = {
    "first_name": ("contact_details",),
    "last_name": ("contact_details",),
    "phone_number": ("contact_details",),
    "birth_date": ("birth_date",),
    "id_number": ("id_document",),
    "personal_tax_id": ("personal_details",),
    "company_name": ("company_name",),
    "company_reg_number": ("company_id",),
    "vat_id": ("company_id",),
    "service_description": _NONE,
    # The country is chosen with the number, not paperwork.
    "country": _NONE,
    "description": _NONE,
    "birth_country": ("personal_details",),
    "contact_email": ("contact_details",),
}
_DIDWW_SLOTS = {
    "identity_proof": (("id_document",), ("company_registration",)),
    "address_proof": (("proof_of_address",), ("proof_of_address",)),
}

_PERSON_DOC = re.compile(r"\bid\b|passport|\bdni\b|\bnie\b|government")
_COMPANY_ITEM = re.compile(
    r"company registration|business registration|registration certificate|"
    r"\bcif\b|company id|commercial regist"
)
_EITHER = re.compile(r"\bor\b")


def _either_or(label: str) -> bool:
    return bool(
        _EITHER.search(label)
        and _PERSON_DOC.search(label)
        and _COMPANY_ITEM.search(label)
    )


def _by_label(
    label: str, field_type: str, subject: str
) -> tuple[tuple[str, ...] | None, bool]:
    """(categories, is_either_or) from the label; None when unknown."""
    low = label.lower()
    document = field_type not in ("textual", "address")
    if _either_or(low):
        if "tax" in low:
            return (
                ("personal_details",) if subject == "person" else ("company_id",)
            ), True
        if subject == "person":
            return ("id_document",), True
        return ("company_registration",), True
    if "use case" in low or "website" in low:
        return _NONE, False
    if "contact" in low:
        return ("contact_details",), False
    if field_type == "address" or (not document and "address" in low):
        if re.search(r"area code|geographic|\barea\b", low):
            return ("address_in_area",), False
        return ("address_in_country",), False
    if "address" in low and document:
        found = ["proof_of_address"]
        if re.search(r"local activity|activity evidence", low):
            found.append("local_usage")
        return tuple(found), False
    if re.search(r"birth", low):
        return ("birth_date",), False
    if re.search(r"nationality|profession|occupation", low):
        return ("personal_details",), False
    if document:
        if _COMPANY_ITEM.search(low) or re.search(r"extract|registrar", low):
            return ("company_registration",), False
        if re.search(r"passport|\bid\b|identity|identification", low):
            return ("id_document",), False
        return None, False
    if re.search(r"company name|business name|legal name", low):
        return ("company_name",), False
    if re.search(r"\btax\b|\bnif\b|\bvat\b|\bcif\b", low):
        return (
            ("personal_details",) if subject == "person" else ("company_id",)
        ), False
    if re.search(r"registration number|company number|enterprise|company id", low):
        return ("company_id",), False
    if re.search(r"\bname\b|e-?mail|phone", low):
        return ("contact_details",), False
    return None, False


def _lookup(
    provider: str,
    key: str,
    label: str,
    field_type: str,
    subject: str,
) -> tuple[tuple[str, ...] | None, bool]:
    """Table by key first, then the label."""
    if provider == "telnyx" and key in _TELNYX_IDS:
        return _TELNYX_IDS[key], False
    if provider == "twilio":
        table = _TWILIO_FIELDS if field_type == "textual" else _TWILIO_DOCS
        if key in table:
            return table[key], False
        if key in _TWILIO_BY_SUBJECT and field_type != "textual":
            person, business = _TWILIO_BY_SUBJECT[key]
            return (person if subject == "person" else business), False
    if provider == "didww":
        if field_type == "textual" and key in _DIDWW_FIELDS:
            return _DIDWW_FIELDS[key], False
        slot = re.sub(r"_\d+$", "", key)
        if field_type != "textual" and slot in _DIDWW_SLOTS:
            person, business = _DIDWW_SLOTS[slot]
            return (person if subject == "person" else business), False
    return _by_label(label, field_type, subject)


def is_company_document(rule: dict) -> bool:
    """True for a raw Telnyx file rule that only a company can meet: it is
    not text, it is a company registration item, and it does not also offer
    an ID for a person."""
    if rule.get("field_type") == "textual":
        return False
    label = f"{rule.get('name') or ''} {rule.get('description') or ''}".strip()
    name = rule.get("name") or ""
    cats, either = _lookup(
        "telnyx",
        rule.get("id") or "",
        name or label,
        rule.get("field_type") or "",
        "business",
    )
    return not either and bool(cats) and "company_registration" in cats  # type: ignore[operator]


def classify(
    req: VerificationRequirements,
    provider: str,
    *,
    rules: list[dict] | None = None,
) -> list[StoredItem]:
    """Categories for one set of requirements: fixed order, no duplicates.

    ``rules`` are the raw Telnyx rules. Pass them so an address rule is
    sorted by its label (an address in the number's area or in the country).
    """
    subject = req.subject_type
    found: set[str] = set()
    others: list[str] = []

    def add(key: str, label: str, field_type: str) -> None:
        cats, _ = _lookup(provider, key, label, field_type, subject)
        if cats is None:
            if label not in others:
                others.append(label)
                logger.warning(
                    "unclassified requirement: %s",
                    label,
                    extra={
                        "provider": provider,
                        "country_code": req.country_code,
                        "number_type": req.number_type,
                        "label": label,
                    },
                )
            return
        found.update(cats)

    for slot in req.documents:
        # A merged slot is classified by the requirements it stands for.
        for name, label in slot.also or ((slot.name, slot.label),):
            add(name, label, "document")
    for field in req.fields:
        add(field.name, field.label, "textual")
    for rule in rules or []:
        if rule.get("field_type") == "address":
            add(rule.get("id") or "", rule.get("name") or "", "address")
    address_cats = {"proof_of_address", "address_in_country", "address_in_area"}
    if req.address_required and not (found & address_cats):
        found.add("address_in_country")

    items: list[StoredItem] = [c for c in _ORDER if c in found]
    items.extend({"other": label} for label in others)
    return items


def paperwork(items: list[StoredItem]) -> list[dict[str, str]]:
    """Items as ``{key, label}`` rows. An ``other`` item shows its raw label."""
    rows: list[dict[str, str]] = []
    for item in items:
        if isinstance(item, dict):
            rows.append({"key": "other", "label": item["other"]})
        else:
            rows.append({"key": item, "label": LABELS[item]})
    return rows
