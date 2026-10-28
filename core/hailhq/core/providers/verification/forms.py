"""Form checks shared by every carrier plug-in: everything a plug-in can
verify about the customer's input before it calls the carrier."""

from __future__ import annotations

import re

from hailhq.core.providers.verification.base import (
    Address,
    DocumentInput,
    DocumentOption,
    DocumentSlot,
    FieldSpec,
    Problem,
    Requirements,
)

__all__ = [
    "address_complete",
    "check_field",
    "cut_text",
    "display_name",
    "length_problem",
    "length_problems",
    "pick_option",
    "text_length",
    "validate_input",
]

# A stored name longer than this is cut. Names are for telling
# verifications apart, not a legal record.
_NAME_MAX = 200


def display_name(*parts: str | None) -> str | None:
    """The words of the parts joined by single spaces, or None when all are
    empty. "Ada  Lovelace" and "Ada Lovelace" give the same name, so the
    duplicate check and the unique index see them as one."""
    name = " ".join(" ".join(p for p in parts if p).split())
    return name[:_NAME_MAX].rstrip() or None


def text_length(value: str) -> int:
    """Length in UTF-16 code units, as the carriers and the browser's
    ``maxLength`` count it: a character outside the BMP (most emoji) counts
    as 2, so a value the form accepts is never too long for the carrier."""
    return len(value) + sum(ord(c) > 0xFFFF for c in value)


def cut_text(value: str, max_units: int) -> str:
    """``value`` cut to at most ``max_units`` UTF-16 code units, counted as
    ``text_length`` counts them. Cut between characters: never half an
    emoji."""
    units = 0
    for i, c in enumerate(value):
        units += 2 if ord(c) > 0xFFFF else 1
        if units > max_units:
            return value[:i]
    return value


def length_problem(spec: FieldSpec, value: str) -> str | None:
    """The message for a value over the field's ``max_length``, else None."""
    if spec.max_length and text_length(value) > spec.max_length:
        return f"Use at most {spec.max_length} characters."
    return None


def length_problems(
    requirements: Requirements,
    fields: dict[str, str],
    documents: dict[str, DocumentInput],
) -> list[Problem]:
    """Values over their field's ``max_length``: the form's fields and the
    fields of each document type chosen. The API runs this for every
    carrier before the plug-in sees the form; the plug-in reports anything
    else (a missing value, a document type not chosen)."""
    problems: list[Problem] = []

    def check(spec: FieldSpec, value: str | None, where: str) -> None:
        too_long = length_problem(spec, (value or "").strip())
        if too_long:
            problems.append(Problem(field=where, message=too_long))

    for spec in requirements.fields:
        check(spec, fields.get(spec.name), spec.name)
    for slot in requirements.documents:
        doc = documents.get(slot.name)
        option = pick_option(slot, doc) if doc is not None else None
        for spec in option.fields if option is not None else ():
            check(spec, doc.fields.get(spec.name), f"{slot.name}.{spec.name}")
    return problems


def check_field(spec: FieldSpec, value: str | None, where: str) -> list[Problem]:
    """A value's problems: missing when required; else too long and not
    matching the field's pattern, both when both fail."""
    value = (value or "").strip()
    if not value:
        if spec.required:
            return [Problem(field=where, message=f"{spec.label} is required.")]
        return []
    problems: list[Problem] = []
    too_long = length_problem(spec, value)
    if too_long:
        problems.append(Problem(field=where, message=too_long))
    if spec.pattern and not re.search(spec.pattern, value):
        problems.append(Problem(field=where, message=f"{spec.label} is not valid."))
    return problems


def pick_option(slot: DocumentSlot, doc: DocumentInput) -> DocumentOption | None:
    """The option the customer chose. A slot with one option needs no choice."""
    if not doc.option and len(slot.options) == 1:
        return slot.options[0]
    return next((o for o in slot.options if o.key == doc.option), None)


def address_complete(address: Address | None) -> bool:
    """True when every part of the address is filled in."""
    return address is not None and all(
        v.strip()
        for v in (
            address.customer_name,
            address.street,
            address.city,
            address.region,
            address.postal_code,
            address.country_code,
        )
    )


def validate_input(
    requirements: Requirements,
    fields: dict[str, str],
    address: Address | None,
    documents: dict[str, DocumentInput],
) -> list[Problem]:
    """Everything we can check without calling the carrier."""
    problems: list[Problem] = []
    for spec in requirements.fields:
        problems += check_field(spec, fields.get(spec.name), spec.name)

    needs_address = False
    for slot in requirements.documents:
        doc = documents.get(slot.name)
        if doc is None:
            if slot.needs_input:
                problems.append(
                    Problem(field=slot.name, message=f"{slot.label} is required.")
                )
            elif slot.options:
                needs_address = needs_address or slot.options[0].needs_address
            continue
        option = pick_option(slot, doc)
        if option is None:
            problems.append(
                Problem(
                    field=slot.name, message=f"Choose a document type for {slot.label}."
                )
            )
            continue
        needs_address = needs_address or option.needs_address
        if option.file_required and doc.file is None:
            problems.append(
                Problem(field=slot.name, message=f"Upload a file for {slot.label}.")
            )
        for spec in option.fields:
            problems += check_field(
                spec, doc.fields.get(spec.name), f"{slot.name}.{spec.name}"
            )

    if needs_address and not address_complete(address):
        problems.append(Problem(field="address", message="A full address is required."))
    return problems
