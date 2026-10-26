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
    "BRAND_REJECTED",
    "CAMPAIGN_REJECTED",
    "DETAILS_REJECTED",
    "NOT_ACCEPTED",
    "VALUE_NOT_ACCEPTED",
    "Address",
    "DocumentInput",
    "DocumentOption",
    "DocumentSlot",
    "DraftResult",
    "FieldSpec",
    "Problem",
    "ProviderStatus",
    "Requirements",
    "SmsRegistrationProvider",
    "SubjectType",
    "SubmitRefused",
    "UnsupportedSubjectType",
    "UploadedFile",
    "VerificationProvider",
    "VerificationProviderError",
    "VerificationRequirements",
]

SubjectType = Literal["person", "business"]
FieldKind = Literal["text", "email", "phone", "url"]


class VerificationProviderError(Exception):
    """The carrier failed in a way the customer cannot fix (outage, auth)."""


class SubmitRefused(VerificationProviderError):
    """The carrier refused to take the draft for review. Retrying the same
    draft cannot help, so the customer must start again."""

    def __init__(self, problems: list[Problem]) -> None:
        super().__init__("the carrier refused the submission")
        self.problems = problems


class UnsupportedSubjectType(ValueError):
    """The carrier does not accept this subject type for the country/type.

    `allowed` lists the subject types it does accept."""

    def __init__(self, message: str, allowed: tuple[SubjectType, ...] = ()) -> None:
        super().__init__(message)
        self.allowed = allowed


class FieldOption(BaseModel):
    """One allowed value for a field with a fixed set of answers."""

    model_config = ConfigDict(frozen=True)

    key: str = Field(description="Value to send.")
    label: str = Field(description="Label to show the customer.")


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
    options: tuple[FieldOption, ...] | None = Field(
        default=None,
        description="Allowed values, when the field is a choice. Show a select, not a text box.",
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


class VerificationRequirements(BaseModel):
    """What the carrier needs for one (country, number type, subject type),
    as the public API shows it: without the carrier's name."""

    model_config = ConfigDict(frozen=True)

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


class Requirements(VerificationRequirements):
    """Internal requirements: the public form plus the carrier it is for."""

    provider: str = Field(description="The carrier these requirements come from.")

    @property
    def version(self) -> str:
        """Stable hash of the form definition, stored with a verification so a
        later change to the carrier's rules is visible. Hashes the internal
        model (with ``provider``) and sorts keys, so the value is the same
        as before the public model was split off."""
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


# Hail's own wording for a carrier refusal. The carrier's text goes in
# ``Problem.raw`` / ``ProviderStatus.raw_reason`` for the operator.
NOT_ACCEPTED = (
    "The carrier did not accept the details. Check your entries and try again."
)
VALUE_NOT_ACCEPTED = "The carrier did not accept this value."
DETAILS_REJECTED = "The carrier rejected the details. Start again."
BRAND_REJECTED = (
    "The carrier rejected the brand registration. Check the business or "
    "person details and start again."
)
CAMPAIGN_REJECTED = (
    "The carrier rejected the campaign. Check the use case, description, "
    "sample messages and links, and start again."
)


class Problem(BaseModel):
    """A per-field problem the customer can fix. ``field`` is a neutral name
    (a field name, a slot name, or ``address``), or ``""`` for a general one.

    ``message`` is Hail's own wording and is shown to the customer. ``raw`` is
    the carrier's own text, if any. It is never logged and never sent to the
    customer: a background refusal keeps it in the audit row, a superadmin
    approve shows it to the superadmin, and a refusal at creation drops it."""

    field: str
    message: str
    raw: str | None = None


class DraftResult(BaseModel):
    """Outcome of ``create_draft``. ``refs`` are opaque carrier IDs (no
    personal data). ``problems`` is empty when the draft is ready to submit."""

    refs: dict
    problems: list[Problem] = Field(default_factory=list)


class ProviderStatus(BaseModel):
    """``draft``: the carrier has the record but it was never submitted.
    ``pending``: submitted and under review."""

    state: Literal["draft", "pending", "approved", "rejected"]
    # Shown to the customer: Hail's own wording.
    reason: str | None = None
    # The carrier's own text for a rejection. Kept in the audit log only;
    # never logged, never shown to the customer.
    raw_reason: str | None = None


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
        contact_email: str,
        requirements: Requirements,
        fields: dict[str, str],
        address: Address | None,
        documents: dict[str, DocumentInput],
    ) -> DraftResult:
        """Create the carrier-side record as a draft and check it. Leaves
        nothing behind if it raises.

        ``contact_email`` is the operator's address for the carrier's notices
        about this record. It is never the customer's; the customer's own
        details go only in ``fields``."""

    @abstractmethod
    async def check(self, refs: dict, subject_type: SubjectType) -> list[Problem]:
        """Re-check a draft. Empty list when ready to submit."""

    @abstractmethod
    async def submit(self, refs: dict) -> None:
        """Send the draft to the carrier for review."""

    @abstractmethod
    async def status(self, refs: dict) -> ProviderStatus:
        """The carrier's current view of a submitted record."""

    @abstractmethod
    async def purchase_handle(self, refs: dict) -> dict:
        """The carrier ids the purchase call needs for this record:
        ``{"verification_id": str, "address_id": str | None}``."""

    @abstractmethod
    async def discard(self, refs: dict) -> None:
        """Delete a draft. Best effort; never raises."""

    def subject_name(
        self,
        requirements: Requirements,
        fields: dict[str, str],
        address: Address | None,
        documents: dict[str, DocumentInput],
    ) -> str | None:
        """The name of the person or company this verification is for, taken
        from what the customer typed in the form. No carrier call. None when
        the form holds no name. Hail stores it and shows it to the customer;
        it is never logged."""
        return None

    async def fetch_subject_name(
        self, refs: dict, subject_type: SubjectType
    ) -> str | None:
        """The same name, read back from the carrier's record (``refs``).
        Used once, by the operator backfill for rows made before Hail kept
        the name. None when the carrier record holds no name."""
        return None


class SmsRegistrationProvider(VerificationProvider):
    """A carrier's US A2P 10DLC registration, driven through the same
    lifecycle as number verification. ``purpose`` is 'sms'.

    ``create_draft`` takes one extra keyword: the organization's messaging
    service (Twilio) or messaging profile (Telnyx) id, when one exists.
    ``status`` creates the campaign once the brand is approved and reports
    'approved' only when the campaign is approved. ``attach_number`` puts
    one of the organization's US long codes on the approved campaign;
    ``detach_number`` takes it off."""

    purpose = "sms"

    @abstractmethod
    async def attach_number(
        self, refs: dict, *, provider_resource_id: str, e164: str
    ) -> None:
        """Attach a number to the approved campaign. Idempotent. A plug-in
        that keeps the campaign on a messaging service stores its id in
        ``refs['messaging_service_sid']``; the number is then in it."""

    async def detach_number(
        self, refs: dict, *, provider_resource_id: str, e164: str
    ) -> bool:
        """Take a number off the campaign (before a release, or when the
        registration is cancelled). Best effort; never raises. True when the
        number also left its messaging service, so the caller clears
        ``messaging_service_sid`` and may set the number up again."""
        return False

    async def confirm(self, refs: dict, code: str) -> list[Problem]:
        """Submit the confirmation code the carrier texted to the person.
        Empty list on success. A wrong or expired code is a Problem on
        field 'code'. Carriers that confirm by reply (not by code) raise
        NotImplementedError."""
        raise NotImplementedError

    def confirmation(self, refs: dict) -> str | None:
        """How the person confirms identity while the brand waits:
        'code' (type a code into Hail), 'reply' (answer the carrier's
        text), or None (no confirmation pending)."""
        return None
