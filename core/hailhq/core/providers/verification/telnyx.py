"""Telnyx plug-in: verification through Telnyx requirement groups.

This is the only module that knows Telnyx's words for it: regulatory
requirements, requirement groups, documents, addresses. The carrier's own
rules (``GET /regulatory_requirements``) drive the form, so a new country
needs no code here.

A requirement group is created under ``customer_reference = hail-<org>``;
that is how ``telnyx_offers`` finds the approved group when it quotes, and
what ``place_number_order`` attaches to the order.
"""

from __future__ import annotations

import base64
import logging
import re

import httpx
from hailhq.core.providers.telnyx import TelnyxClient, path_id
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
    VerificationProvider,
    VerificationProviderError,
)
from hailhq.core.providers.verification.forms import pick_option, validate_input

logger = logging.getLogger(__name__)

_EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "application/pdf": "pdf"}
_SUBJECT_TYPES: tuple[SubjectType, ...] = ("person", "business")


def _customer_reference(organization_id: str) -> str:
    return f"hail-{organization_id}"


def _field_kind(name: str) -> str:
    lowered = name.lower()
    if "email" in lowered:
        return "email"
    if "website" in lowered or lowered.endswith("url"):
        return "url"
    return "text"


def _pattern(criteria: dict) -> str | None:
    regex = criteria.get("regex")
    if not regex:
        return None
    try:
        re.compile(regex)
    except re.error:
        return None
    return regex


def _options(criteria: dict) -> tuple[FieldOption, ...] | None:
    values = criteria.get("acceptable_values") or []
    if not values:
        return None
    return tuple(FieldOption(key=str(v), label=str(v)) for v in values)


def _field_spec(req: dict) -> FieldSpec:
    """One textual requirement as a form field. The field name is the
    requirement id, so the draft can send it back as ``requirement_id``."""
    criteria = req.get("acceptance_criteria") or {}
    options = _options(criteria)
    help_text = req.get("description") or ""
    example = req.get("example")
    if example and not options:
        help_text = f"{help_text} Example: {example}".strip()
    return FieldSpec(
        name=req["id"],
        label=req.get("name") or req["id"],
        kind=_field_kind(req.get("name") or ""),  # type: ignore[arg-type]
        help=help_text,
        pattern=_pattern(criteria),
        options=options,
        required=True,
    )


def _document_slot(req: dict) -> DocumentSlot:
    """One document requirement as a slot with a single file option."""
    return DocumentSlot(
        name=req["id"],
        label=req.get("name") or req["id"],
        help=req.get("description") or "",
        options=(
            DocumentOption(
                key="file", label=req.get("name") or "File", file_required=True
            ),
        ),
    )


def _rules_for(payload: dict, country_code: str, number_type: str) -> list[dict]:
    """The requirement list for exactly this country and type, or []."""
    for row in payload.get("data", []):
        if (
            row.get("country_code") == country_code
            and row.get("phone_number_type") == number_type
            and row.get("action") == "ordering"
        ):
            return list(row.get("regulatory_requirements") or [])
    return []


def _address_payload(address: Address, subject_type: SubjectType) -> dict:
    """Telnyx wants either a business name or a first and last name."""
    payload = {
        "street_address": address.street,
        "locality": address.city,
        "administrative_area": address.region,
        "postal_code": address.postal_code,
        "country_code": address.country_code,
        "address_book": False,
        "validate_address": False,
    }
    name = address.customer_name.strip()
    if subject_type == "business":
        payload["business_name"] = name
    else:
        first, _, last = name.rpartition(" ")
        if not first:
            first, last = last, last
        payload["first_name"] = first
        payload["last_name"] = last
    return payload


class TelnyxVerificationProvider(VerificationProvider):
    name = "telnyx"

    def __init__(
        self, api_key: str | None = None, client: httpx.AsyncClient | None = None
    ) -> None:
        # TelnyxClient raises ValueError without a key, which the registry
        # reads as "not configured".
        self._api = TelnyxClient(api_key, client)

    # -- requirements ------------------------------------------------------

    async def requirements(
        self, country_code: str, number_type: str, subject_type: SubjectType
    ) -> Requirements:
        try:
            payload = await self._api.request(
                "GET",
                "/regulatory_requirements",
                params={
                    "filter[country_code]": country_code,
                    "filter[phone_number_type]": number_type,
                    "filter[action]": "ordering",
                },
            )
        except httpx.HTTPError as exc:
            raise VerificationProviderError("Telnyx request failed") from exc
        rules = _rules_for(payload, country_code, number_type)
        # Telnyx does not distinguish a person from a business here; the same
        # form applies to both, so no subject type is ever refused.
        base = dict(
            provider=self.name,
            country_code=country_code,
            number_type=number_type,
            subject_type=subject_type,
            subject_types=_SUBJECT_TYPES,
        )
        if not rules:
            return Requirements(required=False, **base)
        return Requirements(
            required=True,
            fields=tuple(
                _field_spec(r) for r in rules if r.get("field_type") == "textual"
            ),
            documents=tuple(
                _document_slot(r) for r in rules if r.get("field_type") == "document"
            ),
            address_required=any(r.get("field_type") == "address" for r in rules),
            **base,
        )

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
        problems = validate_input(requirements, fields, address, documents)
        # Telnyx lists the address as its own requirement, not as part of a
        # document, so the shared check above never asks for it.
        if requirements.address_required and (
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
            problems.append(
                Problem(field="address", message="A full address is required.")
            )
        if problems:
            return DraftResult(refs={}, problems=problems)
        refs: dict = {"document_ids": []}
        reference = _customer_reference(organization_id)
        try:
            group = (
                await self._api.request(
                    "POST",
                    "/requirement_groups",
                    json={
                        "country_code": requirements.country_code,
                        "phone_number_type": requirements.number_type,
                        "action": "ordering",
                        "customer_reference": reference,
                    },
                )
            )["data"]
            refs["group_id"] = group["id"]
            # The group lists every requirement with its id and type, including
            # the address ones the form only reported as address_required.
            values: list[dict] = []
            address_id: str | None = None
            for req in group.get("regulatory_requirements") or []:
                req_id, kind = req["requirement_id"], req.get("field_type")
                if kind == "textual":
                    value = (fields.get(req_id) or "").strip()
                    if value:
                        values.append({"requirement_id": req_id, "field_value": value})
                elif kind == "address":
                    if address is None:
                        continue
                    if address_id is None:
                        created = (
                            await self._api.request(
                                "POST",
                                "/addresses",
                                json={
                                    **_address_payload(
                                        address, requirements.subject_type
                                    ),
                                    "customer_reference": reference,
                                },
                            )
                        )["data"]
                        address_id = refs["address_id"] = created["id"]
                    values.append({"requirement_id": req_id, "field_value": address_id})
                elif kind == "document":
                    slot = next(
                        (s for s in requirements.documents if s.name == req_id), None
                    )
                    doc = documents.get(req_id)
                    if slot is None or doc is None or doc.file is None:
                        continue
                    option = pick_option(slot, doc)
                    assert option is not None  # validated in validate_input
                    document_id = await self._upload(reference, doc)
                    refs["document_ids"].append(document_id)
                    values.append(
                        {"requirement_id": req_id, "field_value": document_id}
                    )
            await self._api.request(
                "PATCH",
                f"/requirement_groups/{path_id(refs['group_id'])}",
                json={"regulatory_requirements": values},
            )
        except httpx.HTTPStatusError as exc:
            await self.discard(refs)
            if 400 <= exc.response.status_code < 500:
                return DraftResult(
                    refs={},
                    problems=[Problem(field="", message=_rejection(exc.response))],
                )
            raise VerificationProviderError("Telnyx request failed") from exc
        except Exception:
            await self.discard(refs)
            raise
        problems = await self._problems(refs)
        if problems:
            await self.discard(refs)
            return DraftResult(refs={}, problems=problems)
        return DraftResult(refs=refs)

    async def _upload(self, reference: str, doc: DocumentInput) -> str:
        """Send file bytes to Telnyx. A fixed file name is used so a customer's
        own file name (often their name) never leaves hail."""
        assert doc.file is not None
        ext = _EXTENSIONS.get(doc.file.content_type, "bin")
        created = (
            await self._api.request(
                "POST",
                "/documents",
                json={
                    "file": base64.b64encode(doc.file.data).decode(),
                    "filename": f"document.{ext}",
                    "customer_reference": reference,
                },
            )
        )["data"]
        return created["id"]

    # -- check / submit / status ------------------------------------------

    async def _group(self, refs: dict) -> dict:
        return (
            await self._api.request(
                "GET", f"/requirement_groups/{path_id(refs['group_id'])}"
            )
        )["data"]

    async def _problems(self, refs: dict) -> list[Problem]:
        """Telnyx has no evaluation call. A draft is complete when every
        requirement holds a value and no document was denied."""
        group = await self._group(refs)
        problems: list[Problem] = []
        for req in group.get("regulatory_requirements") or []:
            req_id = req["requirement_id"]
            if not req.get("field_value"):
                problems.append(
                    Problem(field=req_id, message="This requirement is missing.")
                )
            elif req.get("status") == "declined":
                problems.append(
                    Problem(field=req_id, message="The carrier declined this value.")
                )
        return problems

    async def check(self, refs: dict) -> list[Problem]:
        try:
            return await self._problems(refs)
        except httpx.HTTPError as exc:
            raise VerificationProviderError("Telnyx request failed") from exc

    async def submit(self, refs: dict) -> None:
        try:
            await self._api.request(
                "POST",
                f"/requirement_groups/{path_id(refs['group_id'])}/submit_for_approval",
            )
        except httpx.HTTPError as exc:
            raise VerificationProviderError("Telnyx request failed") from exc

    async def status(self, refs: dict) -> ProviderStatus:
        try:
            group = await self._group(refs)
        except httpx.HTTPError as exc:
            raise VerificationProviderError("Telnyx request failed") from exc
        state = group.get("status")
        if state == "approved":
            return ProviderStatus(state="approved")
        if state in ("declined", "expired"):
            declined = [
                r["requirement_id"]
                for r in group.get("regulatory_requirements") or []
                if r.get("status") in ("declined", "expired")
            ]
            reason = (
                "The carrier declined the details. Start again."
                if state == "declined"
                else "The carrier's approval expired. Start again."
            )
            if declined:
                reason += f" Requirements: {', '.join(declined)}."
            return ProviderStatus(state="rejected", reason=reason)
        if state == "pending-approval":
            return ProviderStatus(state="pending")
        return ProviderStatus(state="draft")

    async def purchase_handle(self, refs: dict) -> dict:
        return {"requirement_group_id": refs["group_id"]}

    # -- discard -----------------------------------------------------------

    async def discard(self, refs: dict) -> None:
        paths = []
        if refs.get("group_id"):
            paths.append(f"/requirement_groups/{path_id(refs['group_id'])}")
        paths.extend(f"/documents/{path_id(d)}" for d in refs.get("document_ids", []))
        if refs.get("address_id"):
            paths.append(f"/addresses/{path_id(refs['address_id'])}")
        for path in paths:
            try:
                await self._api.request("DELETE", path)
            except Exception:  # best effort; a stray draft is harmless
                logger.warning("telnyx verification cleanup step failed", exc_info=True)


def _rejection(response: httpx.Response) -> str:
    """Telnyx's own words for a 4xx, without the payload."""
    try:
        errors = response.json().get("errors") or []
    except ValueError:
        errors = []
    detail = next(
        (e.get("detail") or e.get("title") for e in errors if isinstance(e, dict)),
        None,
    )
    return detail or "The carrier did not accept the details."
