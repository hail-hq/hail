"""Carrier-neutral verification: the shapes and interface every carrier
plug-in implements.

Some countries require the number holder to be verified with the carrier
before a number can be bought. A plug-in turns the carrier's own rules into
the neutral ``Requirements`` below, builds the carrier-side record from the
customer's input, and reports its status. Nothing here names a carrier
concept (bundle, end user, requirement group). Those stay inside a plug-in.
"""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field

__all__ = [
    "Address",
    "DocumentInput",
    "DocumentOption",
    "DocumentSlot",
    "DraftResult",
    "FieldSpec",
    "Problem",
    "ProviderStatus",
    "Requirements",
    "SubjectType",
    "UnsupportedSubjectType",
    "UploadedFile",
    "VerificationProvider",
    "VerificationProviderError",
]

SubjectType = Literal["person", "business"]
FieldKind = Literal["text", "email", "phone", "url"]


class VerificationProviderError(Exception):
    """The carrier failed in a way the customer cannot fix (outage, auth)."""


class UnsupportedSubjectType(ValueError):
    """The carrier does not accept this subject type for the country/type."""


class FieldSpec(BaseModel):
    """One value the customer types in."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(description="Key to send this value under in `fields`.")
    label: str = Field(description="Label to show the customer.")
    kind: FieldKind = Field(
        default="text",
        description="Input kind: 'text', 'email', 'phone', or 'url'.",
    )
    help: str = Field(default="", description="Help text from the carrier.")
    pattern: str | None = Field(
        default=None,
        description="Regular expression the value must match, when the carrier gives one.",
    )
    required: bool = Field(
        default=True, description="False when the field is optional."
    )


class DocumentOption(BaseModel):
    """One accepted way to fill a document slot (e.g. passport or ID card)."""

    model_config = ConfigDict(frozen=True)

    key: str = Field(description="Value to send as `option` for this document type.")
    label: str = Field(description="Label to show the customer, e.g. 'Passport'.")
    file_required: bool = Field(
        default=True,
        description="True when the customer must upload a file for this option.",
    )
    fields: tuple[FieldSpec, ...] = Field(
        default=(),
        description="Extra values this document needs. Send them in the slot's `fields`.",
    )
    # Names of the subject's own fields the plug-in copies onto the document
    # (the carrier requires they match). The customer is not asked again.
    copies: tuple[str, ...] = Field(
        default=(),
        description="Names of the customer's own fields copied onto this document automatically.",
    )
    needs_address: bool = Field(
        default=False,
        description="True when this document refers to the address the customer gave.",
    )


class DocumentSlot(BaseModel):
    """A required proof (identity, address, registration) with its options."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(
        description="Key for this slot in `documents` and in the `file.<name>` part."
    )
    label: str = Field(
        description="Label to show the customer, e.g. 'Proof of identity'."
    )
    help: str = Field(default="", description="Help text from the carrier.")
    options: tuple[DocumentOption, ...] = Field(
        description="The accepted ways to fill this slot. The customer picks one."
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def needs_input(self) -> bool:
        """False when there is nothing to ask: one option, no file, no extra
        values. The UI hides such a slot; the plug-in still fills it."""
        return len(self.options) > 1 or any(
            o.file_required or o.fields for o in self.options
        )


class Requirements(BaseModel):
    """What a carrier needs for one (country, number type, subject type)."""

    model_config = ConfigDict(frozen=True)

    provider: str = Field(description="The carrier these requirements come from.")
    country_code: str = Field(description="ISO alpha-2 country code.")
    number_type: str = Field(
        description="'local', 'mobile', 'toll_free', or 'national'."
    )
    subject_type: SubjectType = Field(description="'person' or 'business'.")
    required: bool = Field(
        description="False when the carrier needs no verification for this combination."
    )
    fields: tuple[FieldSpec, ...] = Field(
        default=(), description="Values to collect about the person or business."
    )
    documents: tuple[DocumentSlot, ...] = Field(
        default=(),
        description="Documents to collect. Skip slots with `needs_input` false.",
    )
    address_required: bool = Field(
        default=False, description="True when an address must be collected."
    )
    subject_types: tuple[SubjectType, ...] = Field(
        default=(),
        description="Subject types the carrier accepts here, so you can offer the choice.",
    )

    @property
    def version(self) -> str:
        """Stable hash of the form definition, stored with a verification so a
        later change to the carrier's rules is visible."""
        body = self.model_dump(mode="json", exclude={"subject_types"})
        raw = json.dumps(body, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()[:16]


class UploadedFile(BaseModel):
    """File bytes held in memory only. Never written to disk or the database."""

    model_config = ConfigDict(frozen=True, repr=False)

    filename: str
    content_type: str
    data: bytes

    def __repr__(self) -> str:  # never leak bytes or names into logs
        return f"UploadedFile({self.content_type}, {len(self.data)} bytes)"


class DocumentInput(BaseModel):
    option: str
    fields: dict[str, str] = Field(default_factory=dict)
    file: UploadedFile | None = None


class Address(BaseModel):
    customer_name: str
    street: str
    city: str
    region: str
    postal_code: str
    country_code: str


class Problem(BaseModel):
    """A per-field problem the customer can fix. ``field`` is a neutral name
    (a field name, a slot name, or ``address``), or ``""`` for a general one."""

    field: str
    message: str


class DraftResult(BaseModel):
    """Outcome of ``create_draft``. ``refs`` are opaque carrier IDs (no
    personal data). ``problems`` is empty when the draft is ready to submit."""

    refs: dict
    problems: list[Problem] = Field(default_factory=list)


class ProviderStatus(BaseModel):
    state: Literal["pending", "approved", "rejected"]
    reason: str | None = None


class VerificationProvider(ABC):
    """Carrier-side verification. One implementation per carrier."""

    name: str

    @abstractmethod
    async def requirements(
        self, country_code: str, number_type: str, subject_type: SubjectType
    ) -> Requirements:
        """The form the customer must fill for this combination."""

    @abstractmethod
    async def create_draft(
        self,
        *,
        organization_id: str,
        requirements: Requirements,
        fields: dict[str, str],
        address: Address | None,
        documents: dict[str, DocumentInput],
    ) -> DraftResult:
        """Create the carrier-side record as a draft and check it. Leaves
        nothing behind if it raises."""

    @abstractmethod
    async def check(self, refs: dict) -> list[Problem]:
        """Re-check a draft. Empty list when ready to submit."""

    @abstractmethod
    async def submit(self, refs: dict) -> None:
        """Send the draft to the carrier for review."""

    @abstractmethod
    async def status(self, refs: dict) -> ProviderStatus:
        """The carrier's current view of a submitted record."""

    @abstractmethod
    async def purchase_handle(self, refs: dict) -> dict:
        """Opaque values the carrier's purchase call needs for this record."""

    @abstractmethod
    async def discard(self, refs: dict) -> None:
        """Delete a draft. Best effort; never raises."""
