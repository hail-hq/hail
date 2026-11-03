"""Number types sold in a country, with the best option per subject.

Reads the bundled carrier catalog and the org's verifications. No carrier
call. ``option_key`` is the one ranking order; ``/quotes`` reuses it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, NamedTuple

from hailhq.core import requirement_categories, telephony_catalog
from hailhq.core.carrier_offer import (
    NumberOptionsResponse,
    OfferVerification,
    OptionVerification,
    PaperworkItem,
    SubjectOption,
    TypeOptions,
    cents,
)
from hailhq.core.number_offers import PROVIDERS
from hailhq.core.providers.verification import get_verification_provider

__all__ = [
    "SubjectPapers",
    "number_options",
    "offer_paperwork",
    "option_key",
    "option_status",
    "paperwork_from_block",
    "subject_accepted",
]

SUBJECTS = ("person", "business")
_STATUS_RANK = {"ready": 0, "in_review": 1, "needs_check": 2}
# States that mean the customer submitted it and it is under review (Hail's
# own check, being sent, or at the carrier). A draft was never submitted;
# rejected and cancelled are over.
_IN_REVIEW_STATES = ("awaiting_review", "submitting", "submitted")
_TYPE_ORDER = ("local", "mobile", "national", "toll_free")


def option_key(
    status: str,
    how: str,
    paperwork_count: int,
    monthly_cents: int,
    setup_cents: int,
    paperwork_known: bool = True,
) -> tuple[int, int, int, int, int]:
    """Sort key, best first: status, then buy before by request, then a known
    paper list before an unknown one, then fewer papers, then lower monthly
    plus setup."""
    return (
        _STATUS_RANK[status],
        0 if how == "buy" else 1,
        0 if paperwork_known else 1,
        paperwork_count,
        monthly_cents + setup_cents,
    )


@dataclass
class _Candidate:
    option: SubjectOption
    needs: bool

    @property
    def key(self) -> tuple[int, int, int, int, int]:
        o = self.option
        return option_key(
            o.status,
            o.how,
            len(o.paperwork),
            o.monthly_cents,
            o.setup_cents,
            o.paperwork_known,
        )


def subject_accepted(block: dict | None, subject: str) -> bool:
    """False when the catalog block says ``subject`` is not accepted. No
    block: unknown, so True."""
    if block is None:
        return True
    return bool((block.get(subject) or {}).get("accepted", False))


class SubjectPapers(NamedTuple):
    """What one catalog row asks of one subject."""

    items: list[PaperworkItem]
    known: bool  # the catalog lists the papers
    accepted: bool  # the catalog accepts this subject


def paperwork_from_block(
    block: dict | None, subject: str, needs: bool
) -> SubjectPapers:
    """No verification needed: no papers, list known, accepted. Needed but no
    block: unknown list, accepted."""
    if not needs:
        return SubjectPapers([], True, True)
    if block is None:
        return SubjectPapers([], False, True)
    side = block.get(subject) or {}
    items = requirement_categories.paperwork(side.get("items", []))
    return SubjectPapers(
        [PaperworkItem(**p) for p in items], True, subject_accepted(block, subject)
    )


def offer_paperwork(
    country_code: str, number_type: str, provider: str, subject: str, needs: bool
) -> SubjectPapers:
    """The papers one subject must give for one carrier row."""
    block = (
        telephony_catalog.requirements(country_code, number_type, provider)
        if needs
        else None
    )
    return paperwork_from_block(block, subject, needs)


def option_status(
    needs: bool,
    by_request: bool,
    subject: str,
    found: list[OfferVerification],
) -> tuple[str, OfferVerification | None]:
    if not needs:
        return "ready", None
    approved = [v for v in found if v.state == "approved"]
    if by_request:
        # Same rule as /quotes: any approval, of either subject, covers it.
        mine = [v for v in approved if v.subject_type == subject]
        if approved:
            return "ready", (mine or approved)[0]
    else:
        mine = [v for v in approved if v.subject_type == subject]
        if mine:
            return "ready", mine[0]
    pending = [
        v for v in found if v.state in _IN_REVIEW_STATES and v.subject_type == subject
    ]
    if pending:
        return "in_review", pending[0]
    return "needs_check", None


def _candidates(
    country_code: str,
    verifications: dict[tuple[str, str, str], list[OfferVerification]],
    sms_only: bool,
) -> dict[str, dict[str, list[_Candidate]]]:
    out: dict[str, dict[str, list[_Candidate]]] = {}
    for provider in PROVIDERS:
        for row in telephony_catalog.rows(country_code, provider):
            if row.get("available") is False:
                continue
            needs = bool(row.get("verification_required"))
            by_request = bool(row.get("by_request"))
            # Same as /quotes: no verification provider, no by-request row.
            if (needs or by_request) and get_verification_provider(provider) is None:
                continue
            caps = telephony_catalog.effective_capabilities(row, provider)
            if not caps or (sms_only and "sms" not in caps):
                continue
            nt = row["number_type"]
            block = (
                telephony_catalog.requirements(country_code, nt, provider)
                if needs
                else None
            )
            monthly = telephony_catalog.monthly_price(
                provider, cents(row["usd_per_month"]), caps
            )["monthly_cents"]
            setup = cents(row.get("setup_usd") or 0)
            found = verifications.get((provider, country_code, nt), [])
            for subject in SUBJECTS:
                papers = paperwork_from_block(block, subject, needs)
                if not papers.accepted:
                    continue
                status, v = option_status(needs, by_request, subject, found)
                option = SubjectOption(
                    allowed=True,
                    how="by_request" if by_request else "buy",
                    monthly_cents=monthly,
                    setup_cents=setup,
                    capabilities=caps,
                    paperwork=papers.items,
                    paperwork_known=papers.known,
                    status=status,
                    verification=(
                        OptionVerification(
                            id=v.id, subject_name=v.subject_name, state=v.state
                        )
                        if v
                        else None
                    ),
                    more_options=0,
                )
                out.setdefault(nt, {}).setdefault(subject, []).append(
                    _Candidate(option, needs)
                )
    return out


def _best(cands: list[_Candidate]) -> _Candidate:
    best = min(cands, key=lambda c: c.key)
    option = best.option.model_copy(update={"more_options": len(cands) - 1})
    return _Candidate(option, best.needs)


def number_options(
    country_code: str,
    verifications: dict[tuple[str, str, str], list[OfferVerification]],
    sms_only: bool = False,
) -> NumberOptionsResponse:
    cc = country_code.upper()
    by_type = _candidates(cc, verifications, sms_only)
    types: list[TypeOptions] = []
    needs_paperwork = False
    order = sorted(
        by_type, key=lambda t: (_TYPE_ORDER.index(t) if t in _TYPE_ORDER else 99, t)
    )
    for nt in order:
        per = by_type[nt]
        best = {s: _best(per[s]) for s in SUBJECTS if per.get(s)}
        if not best:
            continue
        reasons: dict[str, Literal["company_only", "person_only"]] = {
            "person": "company_only",
            "business": "person_only",
        }
        other = {"person": "business", "business": "person"}
        row: dict[str, SubjectOption] = {}
        for s in SUBJECTS:
            if s in best:
                row[s] = best[s].option
                if best[s].needs:
                    needs_paperwork = True
            else:
                row[s] = best[other[s]].option.model_copy(
                    update={
                        "allowed": False,
                        "status": "needs_check",
                        "reason": reasons[s],
                        "more_options": 0,
                        "verification": None,
                    }
                )
        types.append(TypeOptions(number_type=nt, for_=row))
    return NumberOptionsResponse(
        country_code=cc, needs_paperwork=needs_paperwork, types=types
    )
