"""Contact wire ids: a manual contact's uuid, or ``member:<user uuid>`` for
an org member. GET /contacts returns these; agent handover lists take them.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

MEMBER_ID_PREFIX = "member:"

ContactKind = Literal["contact", "member"]


def parse_contact_id(wire: str) -> tuple[ContactKind, UUID]:
    """Split a wire id into its kind and uuid. Raises ValueError."""
    if wire.startswith(MEMBER_ID_PREFIX):
        return "member", UUID(wire[len(MEMBER_ID_PREFIX) :])
    return "contact", UUID(wire)


def contact_wire_id(kind: ContactKind, value: UUID) -> str:
    return f"{MEMBER_ID_PREFIX}{value}" if kind == "member" else str(value)


def normalize_contact_id(wire: str) -> str:
    """Canonical form (lowercase uuid). Raises ValueError."""
    return contact_wire_id(*parse_contact_id(wire))


__all__ = [
    "MEMBER_ID_PREFIX",
    "ContactKind",
    "contact_wire_id",
    "normalize_contact_id",
    "parse_contact_id",
]
