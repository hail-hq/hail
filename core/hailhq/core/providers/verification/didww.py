"""DIDWW plug-in: end-user registration through DIDWW API v3.

This is the only module that knows DIDWW's words for it: identities,
addresses, proofs, encrypted files, address requirements. DIDWW checks the
papers before purchase (``address_requirement_validations``) and approves
them after the DID is bought (``address_verifications``, filed by the
order reconciler in ``providers/voice/didww.py``).
"""

from __future__ import annotations

import asyncio
import logging

from didww.encrypt import Encrypt
from didww.exceptions import DidwwApiError
from hailhq.core.config import settings
from hailhq.core.providers.verification.base import (
    Address,
    DocumentInput,
    DocumentOption,
    DocumentSlot,
    DraftResult,
    FieldSpec,
    Problem,
    ProviderStatus,
    Requirements,
    SubjectType,
    UnsupportedSubjectType,
    VerificationProvider,
    VerificationProviderError,
)
from hailhq.core.providers.voice.didww import (
    approved_address_ref,
    carrier_status,
    didww_client,
    lookup_ids,
)

logger = logging.getLogger(__name__)

_SUBJECT_TO_DIDWW = {"person": "personal", "business": "business"}
_EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "application/pdf": "pdf"}

_BASE_FIELDS: dict[str, tuple[str, ...]] = {
    "person": ("first_name", "last_name"),
    "business": ("company_name",),
}
_LABELS = {
    "first_name": "First name",
    "last_name": "Last name",
    "birth_date": "Date of birth (YYYY-MM-DD)",
    "id_number": "ID document number",
    "personal_tax_id": "Personal tax number",
    "phone_number": "Phone number",
    "company_name": "Company name",
    "company_reg_number": "Company registration number",
    "vat_id": "VAT number",
    "service_description": "What the number is used for",
}
_KINDS = {"phone_number": "phone"}
# Identity attributes DIDWW accepts; everything else stays in Hail's form only.
_IDENTITY_ATTRS = frozenset(_LABELS) - {"service_description"}


def draft_address_ref(org: str, country: str, kind: str) -> str:
    return f"hail-draft:{org}:{country}:{kind}"


def _field(name: str) -> FieldSpec:
    return FieldSpec(name=name, label=_LABELS.get(name, name.replace("_", " ").capitalize()), kind=_KINDS.get(name, "text"))  # type: ignore[arg-type]


def _slots(
    prefix: str, qty: int, proof_types: list[dict], *, needs_address: bool
) -> list[DocumentSlot]:
    options = tuple(
        DocumentOption(
            key=p["id"], label=p["attributes"]["name"], needs_address=needs_address
        )
        for p in proof_types
    )
    if not options:
        return []
    return [
        DocumentSlot(
            name=f"{prefix}_{i + 1}",
            label=("Proof of address" if needs_address else "Proof of identity")
            + (f" {i + 1}" if qty > 1 else ""),
            options=options,
        )
        for i in range(qty)
    ]


def _problems(exc: DidwwApiError) -> list[Problem]:
    return [
        Problem(
            field="",
            message=e.get("detail") or e.get("title") or "Not accepted by the carrier.",
        )
        for e in exc.errors
    ] or [Problem(field="", message="Not accepted by the carrier.")]


class DidwwVerificationProvider(VerificationProvider):
    name = "didww"

    def __init__(self) -> None:
        if not settings.didww_api_key:
            raise ValueError("DIDWW_API_KEY is not set")
        self._client = didww_client()

    # -- requirements ----------------------------------------------------

    async def requirements(
        self, country_code: str, number_type: str, subject_type: SubjectType
    ) -> Requirements:
        return await asyncio.to_thread(
            self._requirements_sync, country_code, number_type, subject_type
        )

    def _requirement_row(
        self, country_code: str, number_type: str
    ) -> tuple[dict | None, dict]:
        country_id, types = lookup_ids(country_code)
        type_id = types.get(number_type)
        if not country_id or not type_id:
            return None, {}
        body = self._client.get(
            "address_requirements",
            params={
                "filter[country.id]": country_id,
                "filter[did_group_type.id]": type_id,
                "include": "personal_proof_types,business_proof_types,address_proof_types",
            },
        )
        rows = body.get("data", [])
        index = {(r["type"], r["id"]): r for r in body.get("included", [])}
        return (rows[0] if rows else None), index

    def _requirements_sync(
        self, country_code: str, number_type: str, subject_type: SubjectType
    ) -> Requirements:
        row, index = self._requirement_row(country_code, number_type)
        if row is None:
            return Requirements(
                provider=self.name,
                country_code=country_code,
                number_type=number_type,
                subject_type=subject_type,
                required=False,
            )
        attrs = row["attributes"]
        identity_type = attrs.get("identity_type", "any")
        offered: tuple[SubjectType, ...] = (
            ("business", "person")
            if identity_type == "any"
            else (("person",) if identity_type == "personal" else ("business",))
        )
        if subject_type not in offered:
            raise UnsupportedSubjectType(
                f"{subject_type} is not accepted for {number_type} numbers in {country_code}",
                allowed=offered,
            )
        side = _SUBJECT_TO_DIDWW[subject_type]
        names = list(_BASE_FIELDS[subject_type])
        for name in attrs.get(f"{side}_mandatory_fields", []):
            if name not in names:
                names.append(name)
        if attrs.get("service_description_required"):
            names.append("service_description")

        def proof_types(rel: str) -> list[dict]:
            return [
                index[("proof_types", r["id"])]
                for r in row["relationships"].get(rel, {}).get("data", [])
                if ("proof_types", r["id"]) in index
            ]

        documents = [
            *_slots(
                "identity_proof",
                attrs.get(f"{side}_proof_qty", 0),
                proof_types(f"{side}_proof_types"),
                needs_address=False,
            ),
            *_slots(
                "address_proof",
                attrs.get("address_proof_qty", 0),
                proof_types("address_proof_types"),
                needs_address=True,
            ),
        ]
        return Requirements(
            provider=self.name,
            country_code=country_code,
            number_type=number_type,
            subject_type=subject_type,
            required=True,
            fields=tuple(_field(n) for n in names),
            documents=tuple(documents),
            address_required=True,
            subject_types=offered,
        )

    # -- draft -----------------------------------------------------------

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
        return await asyncio.to_thread(
            self._create_draft_sync,
            organization_id,
            contact_email,
            requirements,
            fields,
            address,
            documents,
        )

    def _find_identity(
        self, organization_id: str, country_id: str, identity_type: str
    ) -> str | None:
        found = self._client.get(
            "identities",
            params={
                "filter[external_reference_id]": f"hail-{organization_id}",
                "page[size]": 50,
            },
        )["data"]
        for i in found:
            same_country = (
                i.get("relationships", {}).get("country", {}).get("data", {}).get("id")
                == country_id
            )
            if same_country and i["attributes"].get("identity_type") == identity_type:
                return i["id"]
        return None

    def _create_draft_sync(
        self,
        organization_id: str,
        contact_email: str,
        requirements: Requirements,
        fields: dict[str, str],
        address: Address | None,
        documents: dict[str, DocumentInput],
    ) -> DraftResult:
        if address is None:
            return DraftResult(
                refs={},
                problems=[Problem(field="address", message="An address is required.")],
            )
        row, _ = self._requirement_row(
            requirements.country_code, requirements.number_type
        )
        if row is None:
            raise VerificationProviderError("DIDWW requirement not found")
        country_id, _ = lookup_ids(requirements.country_code)
        clean = {k: v.strip() for k, v in fields.items() if v and v.strip()}
        identity_type = _SUBJECT_TO_DIDWW[requirements.subject_type]
        refs: dict = {
            "organization_id": organization_id,
            "country_code": requirements.country_code,
            "number_type": requirements.number_type,
            "requirement_id": row["id"],
            "proof_ids": [],
            "file_ids": [],
        }
        try:
            existing = self._find_identity(organization_id, country_id, identity_type)
            if existing:
                refs["identity_id"], refs["identity_created"] = existing, False
            else:
                identity = self._client.post(
                    "identities",
                    {
                        "data": {
                            "type": "identities",
                            "attributes": {
                                **{
                                    k: v
                                    for k, v in clean.items()
                                    if k in _IDENTITY_ATTRS
                                },
                                "identity_type": identity_type,
                                "external_reference_id": f"hail-{organization_id}",
                                "contact_email": contact_email,
                            },
                            "relationships": {
                                "country": {
                                    "data": {"id": country_id, "type": "countries"}
                                }
                            },
                        }
                    },
                )["data"]
                refs["identity_id"], refs["identity_created"] = identity["id"], True
            addr = self._client.post(
                "addresses",
                {
                    "data": {
                        "type": "addresses",
                        "attributes": {
                            "address": address.street,
                            "city_name": address.city,
                            "postal_code": address.postal_code,
                            "description": clean.get("service_description", ""),
                            "external_reference_id": draft_address_ref(
                                organization_id,
                                requirements.country_code,
                                requirements.number_type,
                            ),
                        },
                        "relationships": {
                            "country": {
                                "data": {"id": country_id, "type": "countries"}
                            },
                            "identity": {
                                "data": {
                                    "id": refs["identity_id"],
                                    "type": "identities",
                                }
                            },
                        },
                    }
                },
            )["data"]
            refs["address_id"] = addr["id"]
            keys = [
                k["attributes"]["key"] for k in self._client.get("public_keys")["data"]
            ]
            fingerprint = Encrypt.calculate_fingerprint(keys)
            for slot in requirements.documents:
                doc = documents.get(slot.name)
                if doc is None or doc.file is None:
                    continue
                option = next((o for o in slot.options if o.key == doc.option), None)
                if option is None:
                    self._discard_sync(refs)
                    return DraftResult(
                        refs={},
                        problems=[
                            Problem(
                                field=slot.name,
                                message="Pick one of the offered document types.",
                            )
                        ],
                    )
                ext = _EXTENSIONS.get(doc.file.content_type, "bin")
                file_id = self._client.upload_encrypted_file(
                    fingerprint,
                    Encrypt.encrypt_with_keys(doc.file.data, keys),
                    filename=f"document.{ext}",
                )
                refs["file_ids"].append(file_id)
                entity = (
                    {"id": refs["address_id"], "type": "addresses"}
                    if option.needs_address
                    else {"id": refs["identity_id"], "type": "identities"}
                )
                proof = self._client.post(
                    "proofs",
                    {
                        "data": {
                            "type": "proofs",
                            "relationships": {
                                "proof_type": {
                                    "data": {"id": option.key, "type": "proof_types"}
                                },
                                "entity": {"data": entity},
                                "files": {
                                    "data": [{"id": file_id, "type": "encrypted_files"}]
                                },
                            },
                        }
                    },
                )["data"]
                refs["proof_ids"].append(proof["id"])
            problems = self._validate(refs)
        except DidwwApiError as exc:
            self._discard_sync(refs)
            if carrier_status(exc) in (400, 422):
                return DraftResult(refs={}, problems=_problems(exc))
            raise VerificationProviderError("DIDWW request failed") from exc
        except Exception:
            self._discard_sync(refs)
            raise
        if problems:
            self._discard_sync(refs)
            return DraftResult(refs={}, problems=problems)
        return DraftResult(refs=refs)

    def _validate(self, refs: dict) -> list[Problem]:
        try:
            self._client.post(
                "address_requirement_validations",
                {
                    "data": {
                        "type": "address_requirement_validations",
                        "relationships": {
                            "address_requirement": {
                                "data": {
                                    "id": refs["requirement_id"],
                                    "type": "address_requirements",
                                }
                            },
                            "identity": {
                                "data": {
                                    "id": refs["identity_id"],
                                    "type": "identities",
                                }
                            },
                            "address": {
                                "data": {"id": refs["address_id"], "type": "addresses"}
                            },
                        },
                    }
                },
            )
        except DidwwApiError as exc:
            if carrier_status(exc) == 422:
                return _problems(exc)
            raise
        return []

    # -- lifecycle -------------------------------------------------------

    async def check(self, refs: dict) -> list[Problem]:
        try:
            return await asyncio.to_thread(self._validate, refs)
        except DidwwApiError as exc:
            raise VerificationProviderError("DIDWW request failed") from exc

    async def submit(self, refs: dict) -> None:
        """Approval is stamped on the address: discovery finds it by this
        reference and treats the organization as registered."""
        ref = approved_address_ref(
            refs["organization_id"], refs["country_code"], refs["number_type"]
        )

        def run() -> None:
            self._client.patch(
                f"addresses/{refs['address_id']}",
                {
                    "data": {
                        "id": refs["address_id"],
                        "type": "addresses",
                        "attributes": {"external_reference_id": ref},
                    }
                },
            )

        try:
            await asyncio.to_thread(run)
        except DidwwApiError as exc:
            raise VerificationProviderError("DIDWW request failed") from exc

    async def status(self, refs: dict) -> ProviderStatus:
        def run() -> ProviderStatus:
            addr = self._client.get(f"addresses/{refs['address_id']}")["data"]
            ref = addr["attributes"].get("external_reference_id") or ""
            return ProviderStatus(
                state="approved" if ref.startswith("hail:") else "draft"
            )

        try:
            return await asyncio.to_thread(run)
        except DidwwApiError as exc:
            raise VerificationProviderError("DIDWW request failed") from exc

    async def purchase_handle(self, refs: dict) -> dict:
        return {"identity_id": refs["identity_id"], "address_id": refs["address_id"]}

    async def discard(self, refs: dict) -> None:
        await asyncio.to_thread(self._discard_sync, refs)

    def _discard_sync(self, refs: dict) -> None:
        paths = [f"proofs/{p}" for p in refs.get("proof_ids", [])]
        paths += [f"encrypted_files/{f}" for f in refs.get("file_ids", [])]
        if refs.get("address_id"):
            paths.append(f"addresses/{refs['address_id']}")
        if refs.get("identity_id") and refs.get("identity_created"):
            paths.append(f"identities/{refs['identity_id']}")
        for path in paths:
            try:
                self._client.delete(path)
            except Exception:
                logger.warning("didww discard of %s failed", path, exc_info=True)
