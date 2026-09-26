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

__all__ = ["pick_option", "validate_input"]


def pick_option(slot: DocumentSlot, doc: DocumentInput) -> DocumentOption | None:
    """The option the customer chose. A slot with one option needs no choice."""
    if not doc.option and len(slot.options) == 1:
        return slot.options[0]
    return next((o for o in slot.options if o.key == doc.option), None)


def validate_input(
    requirements: Requirements,
    fields: dict[str, str],
    address: Address | None,
    documents: dict[str, DocumentInput],
) -> list[Problem]:
    """Everything we can check without calling the carrier."""
    problems: list[Problem] = []

    def check_field(spec: FieldSpec, value: str | None, where: str) -> None:
        value = (value or "").strip()
        if not value:
            if spec.required:
                problems.append(
                    Problem(field=where, message=f"{spec.label} is required.")
                )
            return
        if spec.pattern and not re.search(spec.pattern, value):
            problems.append(Problem(field=where, message=f"{spec.label} is not valid."))

    for spec in requirements.fields:
        check_field(spec, fields.get(spec.name), spec.name)

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
            check_field(spec, doc.fields.get(spec.name), f"{slot.name}.{spec.name}")

    if needs_address and (
        address is None
        or not all(
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
    ):
        problems.append(Problem(field="address", message="A full address is required."))
    return problems
