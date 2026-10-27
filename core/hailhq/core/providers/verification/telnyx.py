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
from hailhq.core.providers.telnyx import (
    TelnyxClient,
    path_id,
    rule_applies_to,
    subject_types_for,
)
from hailhq.core.providers.verification.base import (
    NOT_ACCEPTED,
    VALUE_NOT_ACCEPTED,
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
    SubmitRefused,
    UnsupportedSubjectType,
    VerificationProvider,
    VerificationProviderError,
)
from hailhq.core.providers.verification.forms import (
    address_complete,
    display_name,
    pick_option,
    validate_input,
)

logger = logging.getLogger(__name__)

_EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "application/pdf": "pdf"}
_UPLOAD_TIMEOUT_SECONDS = 60
# Telnyx refuses to submit a group with any value missing, even a rule tagged
# for the other kind of end user. Such a text rule is sent as this; a file or
# address rule is asked of everyone who may buy (see subject_types_for).
_NOT_APPLICABLE = "N/A"


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
        # A single word is both names: Telnyx requires the pair.
        payload["first_name"] = first or last
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

    async def _rules(self, country_code: str, number_type: str) -> list[dict]:
        payload = await self._api.request(
            "GET",
            "/regulatory_requirements",
            params={
                "filter[country_code]": country_code,
                "filter[phone_number_type]": number_type,
                "filter[action]": "ordering",
            },
        )
        return _rules_for(payload, country_code, number_type)

    async def _not_for(
        self, country_code: str, number_type: str, subject_type: SubjectType
    ) -> set[str]:
        """Ids of the text rules Telnyx tags for the other kind of end user."""
        rules = await self._rules(country_code, number_type)
        return {
            r["id"]
            for r in rules
            if r.get("field_type") == "textual" and not rule_applies_to(r, subject_type)
        }

    async def requirements(
        self, country_code: str, number_type: str, subject_type: SubjectType
    ) -> Requirements:
        try:
            rules = await self._rules(country_code, number_type)
        except httpx.HTTPError as exc:
            raise VerificationProviderError("Telnyx request failed") from exc
        allowed: tuple[SubjectType, ...] = subject_types_for(rules)  # type: ignore[assignment]
        if subject_type not in allowed:
            raise UnsupportedSubjectType(
                f"{subject_type} is not accepted for {number_type} numbers in "
                f"{country_code}",
                allowed=allowed,
            )
        base = {
            "provider": self.name,
            "country_code": country_code,
            "number_type": number_type,
            "subject_type": subject_type,
            "subject_types": allowed,
        }
        if not rules:
            return Requirements(required=False, **base)
        return Requirements(
            required=True,
            # Text rules tagged for the other kind are filled in by
            # create_draft, not asked.
            fields=tuple(
                _field_spec(r)
                for r in rules
                if r.get("field_type") == "textual" and rule_applies_to(r, subject_type)
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
        if requirements.address_required and not address_complete(address):
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
            # Read the group back: it lists every requirement with its id and
            # type, including the address ones the form only reported as
            # address_required. The create response is not relied on for that.
            group = await self._group(refs)
            not_applicable = await self._not_for(
                requirements.country_code,
                requirements.number_type,
                requirements.subject_type,
            )
            values: list[dict] = []
            address_id: str | None = None
            for req in group.get("regulatory_requirements") or []:
                req_id, kind = req["requirement_id"], req.get("field_type")
                if req_id in not_applicable:
                    values.append(
                        {"requirement_id": req_id, "field_value": _NOT_APPLICABLE}
                    )
                elif kind == "textual":
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
                    problems=[
                        Problem(
                            field="",
                            message=NOT_ACCEPTED,
                            raw=_rejection(exc.response),
                        )
                    ],
                )
            raise VerificationProviderError("Telnyx request failed") from exc
        except httpx.HTTPError as exc:
            # Timeouts and transport failures: the carrier, not the customer.
            await self.discard(refs)
            raise VerificationProviderError("Telnyx request failed") from exc
        except Exception:
            await self.discard(refs)
            raise
        try:
            problems = await self._problems(refs)
        except httpx.HTTPError as exc:
            await self.discard(refs)
            raise VerificationProviderError("Telnyx request failed") from exc
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
                # A 4 MB file is a 5 MB body; the default 20 s is too tight.
                timeout=_UPLOAD_TIMEOUT_SECONDS,
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
        requirement holds a value and no document was denied: Telnyx will
        not submit a group with any value missing."""
        group = await self._group(refs)
        problems: list[Problem] = []
        for req in group.get("regulatory_requirements") or []:
            req_id = req["requirement_id"]
            if not req.get("field_value"):
                problems.append(
                    Problem(field=req_id, message="This requirement is missing.")
                )
            elif req.get("status") == "declined":
                problems.append(Problem(field=req_id, message=VALUE_NOT_ACCEPTED))
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
        except httpx.HTTPStatusError as exc:
            # 422: Telnyx will not take this group for review as it is. It
            # also answers 422 for a group already sent, which is a success.
            if exc.response.status_code == 422:
                try:
                    state = (await self._group(refs)).get("status")
                except httpx.HTTPError as read_exc:
                    raise VerificationProviderError(
                        "Telnyx request failed"
                    ) from read_exc
                if state in ("pending-approval", "approved"):
                    return
                raise SubmitRefused(
                    [
                        Problem(
                            field="",
                            message=NOT_ACCEPTED,
                            raw=_rejection(exc.response),
                        )
                    ]
                ) from exc
            raise VerificationProviderError("Telnyx request failed") from exc
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

    def subject_name(
        self,
        requirements: Requirements,
        fields: dict[str, str],
        address: Address | None,
        documents: dict[str, DocumentInput],
    ) -> str | None:
        """The address's ``customer_name`` (Telnyx files it as the business
        name, or the first and last name). Telnyx's text fields are keyed by
        requirement id and some are free text ("Name, Business Name, Phone
        Number"), so without an address only a business's field whose label
        starts with "Company name" or "Business name" is used."""
        name = display_name(address.customer_name if address else None)
        if name or requirements.subject_type != "business":
            return name
        return next(
            (
                n
                for f in requirements.fields
                if f.label.lower().startswith(("company name", "business name"))
                and (n := display_name(fields.get(f.name)))
            ),
            None,
        )

    async def fetch_subject_name(
        self, refs: dict, subject_type: SubjectType
    ) -> str | None:
        """The name on the address the group holds."""
        if not refs.get("address_id"):
            return None
        try:
            addr = (
                await self._api.request(
                    "GET", f"/addresses/{path_id(refs['address_id'])}"
                )
            )["data"]
        except httpx.HTTPError as exc:
            raise VerificationProviderError("Telnyx request failed") from exc
        if subject_type == "business":
            return display_name(addr.get("business_name")) or display_name(
                addr.get("first_name"), addr.get("last_name")
            )
        return display_name(addr.get("first_name"), addr.get("last_name")) or (
            display_name(addr.get("business_name"))
        )

    async def purchase_handle(self, refs: dict) -> dict:
        return {"verification_id": refs["group_id"], "address_id": None}

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


def _rejection(response: httpx.Response) -> str | None:
    """Telnyx's own words for a 4xx, without the payload. Never logged or
    shown to the customer (see ``Problem.raw``)."""
    try:
        errors = response.json().get("errors") or []
    except ValueError:
        errors = []
    detail = next(
        (e.get("detail") or e.get("title") for e in errors if isinstance(e, dict)),
        None,
    )
    return detail
