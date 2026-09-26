"""Twilio plug-in: verification through Twilio Regulatory Compliance.

This is the only module that knows Twilio's words for it: bundles, end users,
supporting documents, evaluations. The carrier's own rules (the Regulations
API) drive the form, so a new country needs no code here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re

import requests
from hailhq.core.config import settings
from hailhq.core.providers.verification.base import (
    Address,
    DocumentInput,
    DocumentOption,
    DocumentSlot,
    DraftResult,
    FieldOption,
    FieldSpec,
    Problem,
    ProviderStatus,
    Requirements,
    SubjectType,
    UnsupportedSubjectType,
    VerificationProvider,
    VerificationProviderError,
)
from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client as TwilioClient

logger = logging.getLogger(__name__)

_UPLOAD_URL = (
    "https://numbers-upload.twilio.com/v2/RegulatoryCompliance/SupportingDocuments"
)
_UPLOAD_TIMEOUT_SECONDS = 60

_SUBJECT_TO_TWILIO = {"person": "individual", "business": "business"}
_TWILIO_TO_SUBJECT: dict[str, SubjectType] = {
    "individual": "person",
    "business": "business",
}
# Document types that are just the address the customer gave; no file needed.
_ADDRESS_ONLY_TYPES = {"individual_address", "business_address"}
_EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "application/pdf": "pdf"}


def _twilio_number_type(number_type: str) -> str:
    return number_type.replace("_", "-")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _field_kind(name: str) -> str:
    if name == "email":
        return "email"
    if name == "phone_number":
        return "phone"
    if name.endswith("website"):
        return "url"
    return "text"


def _pattern(constraint: str) -> str | None:
    """A plain regex out of a constraint like ``x['email'] =~ '^\\S+@\\S+$'``."""
    m = re.search(r"=~\s*'(.+)'\s*$", constraint)
    if not m:
        return None
    try:
        re.compile(m.group(1))
    except re.error:
        return None
    return m.group(1)


# Labels for the choice values Twilio's regulations use. Anything else is
# humanized from its key.
_OPTION_LABELS = {
    "YES": "Yes",
    "NO": "No",
    "OTHER": "Other",
    "DIRECT_CUSTOMER": "We use the number ourselves",
    "INDEPENDENT_SOFTWARE_VENDOR": "The number is part of a product we sell",
    "UK:CRN": "UK company number (CRN)",
    "US:EIN": "US employer ID (EIN)",
    "CA:CBN": "Canada business number (CBN)",
    "AU:ACN": "Australian company number (ACN)",
}
_CHOICE = re.compile(r"^\^\(([^()]+)\)\$$")


def _option_label(key: str) -> str:
    return _OPTION_LABELS.get(key) or key.replace("_", " ").capitalize()


def _options(pattern: str | None) -> tuple[FieldOption, ...] | None:
    """A fixed set of answers out of a pattern like ``^(YES|NO)$``."""
    if not pattern:
        return None
    m = _CHOICE.match(pattern)
    if not m:
        return None
    keys = m.group(1).split("|")
    if any(re.search(r"[\\.*+?\[\]{}]", k) for k in keys):
        return None
    return tuple(FieldOption(key=k, label=_option_label(k)) for k in keys)


def _field_spec(f: dict) -> FieldSpec:
    name = f["machine_name"]
    constraint = (f.get("constraint") or "").strip()
    pattern = _pattern(constraint)
    options = _options(pattern)
    # A choice field explains itself through its option labels; the carrier's
    # description only lists the raw keys again.
    help_text = "" if options else (f.get("description") or "")
    return FieldSpec(
        name=name,
        label=f.get("friendly_name") or name,
        kind=_field_kind(name),  # type: ignore[arg-type]
        help=help_text,
        pattern=pattern,
        options=options,
        required=bool(constraint),
    )


def _map_requirements(
    reg: dict,
) -> tuple[tuple[FieldSpec, ...], tuple[DocumentSlot, ...], bool]:
    """Twilio's requirement tree to the neutral form definition."""
    subject_fields: list[FieldSpec] = []
    for eu in reg.get("end_user", []):
        subject_fields.extend(_field_spec(f) for f in eu.get("detailed_fields", []))
    subject_names = {f.name for f in subject_fields}

    slots: list[DocumentSlot] = []
    address_required = False
    for group in reg.get("supporting_document", []):
        for doc in group:
            options: list[DocumentOption] = []
            for acc in doc.get("accepted_documents", []):
                detailed = acc.get("detailed_fields", [])
                names = {f["machine_name"] for f in detailed}
                needs_address = "address_sids" in names
                address_required = address_required or needs_address
                options.append(
                    DocumentOption(
                        key=acc["type"],
                        label=acc["name"],
                        file_required=acc["type"] not in _ADDRESS_ONLY_TYPES,
                        fields=tuple(
                            _field_spec(f)
                            for f in detailed
                            if f["machine_name"] not in subject_names
                            and f["machine_name"] != "address_sids"
                        ),
                        copies=tuple(sorted(n for n in names if n in subject_names)),
                        needs_address=needs_address,
                    )
                )
            slots.append(
                DocumentSlot(
                    name=doc.get("requirement_name") or _slug(doc["name"]),
                    label=doc["name"],
                    help=doc.get("description") or "",
                    options=tuple(options),
                )
            )
    return tuple(subject_fields), tuple(slots), address_required


def _pick_option(slot: DocumentSlot, doc: DocumentInput) -> DocumentOption | None:
    """The option the customer chose. A slot with one option needs no choice."""
    if not doc.option and len(slot.options) == 1:
        return slot.options[0]
    return next((o for o in slot.options if o.key == doc.option), None)


def _validate_input(
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
        option = _pick_option(slot, doc)
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


class TwilioVerificationProvider(VerificationProvider):
    name = "twilio"

    def __init__(
        self,
        account_sid: str | None = None,
        auth_token: str | None = None,
        client: TwilioClient | None = None,
    ) -> None:
        self.account_sid = account_sid or settings.twilio_account_sid
        token = auth_token or settings.twilio_auth_token
        if client is None:
            if not self.account_sid or not token:
                raise ValueError("TwilioVerificationProvider needs Twilio credentials")
            client = TwilioClient(self.account_sid, token)
        self._token = token
        self._client = client

    @property
    def _rc(self):
        return self._client.numbers.v2.regulatory_compliance

    # -- requirements ------------------------------------------------------

    async def requirements(
        self, country_code: str, number_type: str, subject_type: SubjectType
    ) -> Requirements:
        def run() -> Requirements:
            try:
                regs = self._rc.regulations.list(
                    iso_country=country_code,
                    number_type=_twilio_number_type(number_type),
                    include_constraints=True,
                    limit=50,
                )
            except TwilioRestException as exc:
                if exc.status == 404:
                    regs = []
                else:
                    raise
            usable = [
                r
                for r in regs
                if isinstance(r.requirements, dict) and any(r.requirements.values())
            ]
            if not usable:
                return Requirements(
                    provider=self.name,
                    country_code=country_code,
                    number_type=number_type,
                    subject_type=subject_type,
                    required=False,
                )
            offered = tuple(
                sorted(
                    {
                        _TWILIO_TO_SUBJECT[r.end_user_type]
                        for r in usable
                        if r.end_user_type in _TWILIO_TO_SUBJECT
                    }
                )
            )
            match = next(
                (
                    r
                    for r in usable
                    if r.end_user_type == _SUBJECT_TO_TWILIO[subject_type]
                ),
                None,
            )
            if match is None:
                raise UnsupportedSubjectType(
                    f"{subject_type} is not accepted for {number_type} numbers in {country_code}",
                    allowed=offered,
                )
            fields, documents, address_required = _map_requirements(match.requirements)
            return Requirements(
                provider=self.name,
                country_code=country_code,
                number_type=number_type,
                subject_type=subject_type,
                required=True,
                fields=fields,
                documents=documents,
                address_required=address_required,
                subject_types=offered,
            )

        return await asyncio.to_thread(run)

    # -- draft -------------------------------------------------------------

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
        problems = _validate_input(requirements, fields, address, documents)
        if problems:
            return DraftResult(refs={}, problems=problems)
        return await asyncio.to_thread(
            self._create_draft_sync,
            organization_id,
            contact_email,
            requirements,
            fields,
            address,
            documents,
        )

    def _create_draft_sync(
        self,
        organization_id: str,
        contact_email: str,
        requirements: Requirements,
        fields: dict[str, str],
        address: Address | None,
        documents: dict[str, DocumentInput],
    ) -> DraftResult:
        refs: dict = {
            "slots": {s.label: s.name for s in requirements.documents},
            "document_sids": [],
        }
        clean = {k: v.strip() for k, v in fields.items() if v and v.strip()}
        try:
            if requirements.address_required and address is not None:
                addr = self._client.addresses.create(
                    customer_name=address.customer_name,
                    street=address.street,
                    city=address.city,
                    region=address.region,
                    postal_code=address.postal_code,
                    iso_country=address.country_code,
                )
                refs["address_sid"] = addr.sid

            end_user = self._rc.end_users.create(
                friendly_name=f"hail-{organization_id}",
                type=_SUBJECT_TO_TWILIO[requirements.subject_type],
                attributes={
                    s.name: clean[s.name]
                    for s in requirements.fields
                    if s.name in clean
                },
            )
            refs["end_user_sid"] = end_user.sid

            for slot in requirements.documents:
                doc = documents.get(slot.name)
                if doc is None and not slot.options:
                    continue
                option = _pick_option(slot, doc) if doc else slot.options[0]
                assert option is not None  # validated in _validate_input
                attrs: dict = {k: clean[k] for k in option.copies if k in clean}
                if option.needs_address and refs.get("address_sid"):
                    attrs["address_sids"] = [refs["address_sid"]]
                if doc:
                    attrs.update(
                        {k: v.strip() for k, v in doc.fields.items() if v.strip()}
                    )
                name = f"hail-{organization_id}-{slot.name}"
                if option.file_required and doc and doc.file:
                    sid = self._upload(name, option.key, attrs, doc)
                else:
                    sid = self._rc.supporting_documents.create(
                        friendly_name=name, type=option.key, attributes=attrs
                    ).sid
                refs["document_sids"].append(sid)

            bundle = self._rc.bundles.create(
                friendly_name=f"hail-{organization_id}",
                email=contact_email,
                iso_country=requirements.country_code,
                number_type=_twilio_number_type(requirements.number_type),
                end_user_type=_SUBJECT_TO_TWILIO[requirements.subject_type],
            )
            refs["bundle_sid"] = bundle.sid
            for sid in [refs["end_user_sid"], *refs["document_sids"]]:
                self._rc.bundles(bundle.sid).item_assignments.create(object_sid=sid)
        except TwilioRestException as exc:
            self._discard_sync(refs)
            if exc.status == 400:
                return DraftResult(
                    refs={}, problems=[Problem(field="", message=exc.msg)]
                )
            raise VerificationProviderError("Twilio request failed") from exc
        except Exception:
            self._discard_sync(refs)
            raise

        try:
            problems = self._evaluate(refs)
        except TwilioRestException as exc:
            self._discard_sync(refs)
            raise VerificationProviderError("Twilio request failed") from exc
        except Exception:
            self._discard_sync(refs)
            raise
        if problems:
            self._discard_sync(refs)
            return DraftResult(refs={}, problems=problems)
        return DraftResult(refs=refs)

    def _upload(self, name: str, type_: str, attrs: dict, doc: DocumentInput) -> str:
        """Send file bytes to Twilio. A fixed file name is used so a customer's
        own file name (often their name) never leaves hail."""
        assert doc.file is not None
        ext = _EXTENSIONS.get(doc.file.content_type, "bin")
        resp = requests.post(
            _UPLOAD_URL,
            auth=(self.account_sid, self._token),
            data={"FriendlyName": name, "Type": type_, "Attributes": json.dumps(attrs)},
            files={"File": (f"document.{ext}", doc.file.data, doc.file.content_type)},
            timeout=_UPLOAD_TIMEOUT_SECONDS,
        )
        if resp.status_code == 400:
            raise TwilioRestException(400, _UPLOAD_URL, "Twilio rejected the document")
        if resp.status_code >= 400:
            raise VerificationProviderError(
                f"Twilio upload failed ({resp.status_code})"
            )
        return resp.json()["sid"]

    # -- check / submit / status ------------------------------------------

    def _evaluate(self, refs: dict) -> list[Problem]:
        ev = self._rc.bundles(refs["bundle_sid"]).evaluations.create()
        if ev.status == "compliant":
            return []
        slots: dict[str, str] = refs.get("slots", {})
        problems: list[Problem] = []
        for result in ev.results or []:
            if result.get("passed"):
                continue
            slot = slots.get(result.get("requirement_friendly_name", ""), "")
            for bad in result.get("invalid") or []:
                target = (bad.get("object_field") or "").split(".")[-1]
                field = f"{slot}.{target}" if slot and target else (slot or target)
                problems.append(
                    Problem(
                        field=field,
                        message=bad.get("failure_reason")
                        or f"{bad.get('friendly_name', 'A value')} is not valid.",
                    )
                )
            if not result.get("invalid"):
                problems.append(
                    Problem(
                        field=slot,
                        message=f"{result.get('requirement_friendly_name', 'A requirement')} is missing.",
                    )
                )
        return problems or [
            Problem(field="", message="The carrier did not accept the details.")
        ]

    async def check(self, refs: dict) -> list[Problem]:
        return await asyncio.to_thread(self._evaluate, refs)

    async def submit(self, refs: dict) -> None:
        await asyncio.to_thread(
            self._rc.bundles(refs["bundle_sid"]).update, status="pending-review"
        )

    async def status(self, refs: dict) -> ProviderStatus:
        bundle = await asyncio.to_thread(self._rc.bundles(refs["bundle_sid"]).fetch)
        if bundle.status == "draft":
            return ProviderStatus(state="draft")
        if bundle.status == "twilio-approved":
            return ProviderStatus(state="approved")
        if bundle.status == "twilio-rejected":
            reason = getattr(bundle, "failure_reason", None)
            return ProviderStatus(
                state="rejected",
                reason=reason or "The carrier rejected the details. Start again.",
            )
        return ProviderStatus(state="pending")

    async def purchase_handle(self, refs: dict) -> dict:
        handle = {"bundle_sid": refs["bundle_sid"]}
        if refs.get("address_sid"):
            handle["address_sid"] = refs["address_sid"]
        return handle

    # -- discard -----------------------------------------------------------

    def _discard_sync(self, refs: dict) -> None:
        steps = []
        if refs.get("bundle_sid"):
            steps.append(self._rc.bundles(refs["bundle_sid"]).delete)
        steps.extend(
            self._rc.supporting_documents(s).delete
            for s in refs.get("document_sids", [])
        )
        if refs.get("end_user_sid"):
            steps.append(self._rc.end_users(refs["end_user_sid"]).delete)
        if refs.get("address_sid"):
            steps.append(self._client.addresses(refs["address_sid"]).delete)
        for step in steps:
            try:
                step()
            except Exception:  # best effort; a stray draft is harmless
                logger.warning("twilio verification cleanup step failed", exc_info=True)

    async def discard(self, refs: dict) -> None:
        await asyncio.to_thread(self._discard_sync, refs)
