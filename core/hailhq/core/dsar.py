"""DSAR (Data Subject Access Request) tooling.

Lookup, export, and delete a recipient's data across channels, keyed by
their recipient identifier — an E.164 phone number or an email address.
Callable directly (e.g. from a REPL or a maintenance script), and exposed
as a small internal-only API surface at
``api/hailhq/api/routes/internal/dsar.py`` so it's usable in production
without shelling into one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import case, cast, exists, func, inspect as sa_inspect, or_, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.core.compliance_gate import normalize_recipient
from hailhq.core.models import AuditLog, Call, Email, Suppression

__all__ = [
    "DSARRecord",
    "DeletionSummary",
    "lookup_recipient",
    "export_recipient_data",
    "delete_recipient_data",
]


def _array_contains_ci(column, norm: str):
    """Case-insensitive membership test against a Postgres text[] column.

    ``to_addresses``/``cc_addresses``/``bcc_addresses`` aren't lowercased
    at write time beyond the domain (see ``schemas.py``'s
    ``_normalize_domain``), so an exact-match ``.any(norm)`` misses a
    stored mixed-case local part. ``norm`` is already fully lowercased by
    the caller (``normalize_recipient``).
    """
    unnested = func.unnest(column).table_valued("addr").render_derived()
    return exists(select(unnested.c.addr).where(func.lower(unnested.c.addr) == norm))


def _jsonb_array_contains_ci(payload, key: str, norm: str):
    """Case-insensitive membership test against a JSONB column's array-shaped
    value at ``key`` (``payload["to"]``/``["cc"]``/``["bcc"]`` on email audit
    rows, which preserve the stored local-part casing the same way
    ``Email.to_addresses``/etc. do — see ``_array_contains_ci`` above).

    Safe against non-array values (a missing key, JSON null, or a scalar
    string — call-channel audit rows have a scalar ``"to"``, not an array)
    via a CASE guard that normalizes anything non-array to an empty JSON
    array before unnesting: ``jsonb_array_elements_text`` raises
    ``cannot extract elements from a scalar`` on non-array input otherwise.
    """
    value = payload[key]
    safe_array = case(
        (func.jsonb_typeof(value) == "array", value), else_=cast([], JSONB)
    )
    elements = (
        func.jsonb_array_elements_text(safe_array).table_valued("elem").render_derived()
    )
    return exists(select(elements.c.elem).where(func.lower(elements.c.elem) == norm))


@dataclass
class DSARRecord:
    identifier: str
    calls: list[Call] = field(default_factory=list)
    emails: list[Email] = field(default_factory=list)
    suppressions: list[Suppression] = field(default_factory=list)
    audit_logs: list[AuditLog] = field(default_factory=list)


@dataclass
class DeletionSummary:
    identifier: str
    calls_scrubbed: int = 0
    emails_scrubbed: int = 0
    suppressions_preserved: int = 0


async def lookup_recipient(session: AsyncSession, identifier: str) -> DSARRecord:
    """Find every row referencing ``identifier`` across calls, emails,
    suppressions, and audit_log."""
    norm = normalize_recipient(identifier)

    calls = list(
        (await session.execute(select(Call).where(Call.to_e164 == norm)))
        .scalars()
        .all()
    )

    # Case-insensitive match against stored addresses: to_addresses/cc/bcc
    # aren't lowercased at write time (see api/hailhq/api/routes/emails.py),
    # unlike suppressions.recipient.
    emails = list(
        (
            await session.execute(
                select(Email).where(
                    or_(
                        _array_contains_ci(Email.to_addresses, norm),
                        _array_contains_ci(Email.cc_addresses, norm),
                        _array_contains_ci(Email.bcc_addresses, norm),
                    )
                )
            )
        )
        .scalars()
        .all()
    )

    suppressions = list(
        (
            await session.execute(
                select(Suppression).where(Suppression.recipient == norm)
            )
        )
        .scalars()
        .all()
    )

    # audit_log payloads carry "to" as either a bare string (call.create /
    # call.blocked) or a list of addresses (email.create / email.blocked) —
    # see api/hailhq/api/routes/{calls,emails}.py. ``.astext`` covers the
    # scalar shape (E.164 phone numbers, no case-insensitivity needed).
    # The array shape matches case-insensitively via
    # ``_jsonb_array_contains_ci``, mirroring the Email-table match above —
    # otherwise a mixed-case stored local part (e.g. "Alice@example.com")
    # would be found in ``emails`` but missed here. "cc"/"bcc" are
    # email-only, list-shaped, and absent entirely from call payloads — a
    # missing key evaluates to SQL NULL, which the CASE guard inside
    # ``_jsonb_array_contains_ci`` treats as non-array and safely doesn't
    # match rather than erroring.
    audit_logs = list(
        (
            await session.execute(
                select(AuditLog).where(
                    or_(
                        AuditLog.payload["to"].astext == norm,
                        _jsonb_array_contains_ci(AuditLog.payload, "to", norm),
                        _jsonb_array_contains_ci(AuditLog.payload, "cc", norm),
                        _jsonb_array_contains_ci(AuditLog.payload, "bcc", norm),
                    )
                )
            )
        )
        .scalars()
        .all()
    )

    return DSARRecord(
        identifier=norm,
        calls=calls,
        emails=emails,
        suppressions=suppressions,
        audit_logs=audit_logs,
    )


def _serialize(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _model_to_dict(obj: Any) -> dict[str, Any]:
    """Serialize a mapped row to a plain, JSON-able dict.

    Iterates ``column_attrs`` (not ``obj.__table__.columns``) so the
    python-side attribute name is used for ``getattr`` — needed for
    columns like ``Conversation.metadata_``/``Call.metadata_``, whose
    python attribute differs from the DB column name (``metadata``).
    The DB column name is used as the output dict key.
    """
    mapper = sa_inspect(obj).mapper
    return {
        attr.columns[0].name: _serialize(getattr(obj, attr.key))
        for attr in mapper.column_attrs
    }


async def export_recipient_data(session: AsyncSession, identifier: str) -> dict:
    """Same lookup as ``lookup_recipient``, serialized to a plain dict
    suitable for handing to a recipient who requests their data."""
    record = await lookup_recipient(session, identifier)
    return {
        "identifier": record.identifier,
        "calls": [_model_to_dict(c) for c in record.calls],
        "emails": [_model_to_dict(e) for e in record.emails],
        "suppressions": [_model_to_dict(s) for s in record.suppressions],
        "audit_logs": [_model_to_dict(a) for a in record.audit_logs],
    }


async def delete_recipient_data(
    session: AsyncSession, identifier: str
) -> DeletionSummary:
    """Scrub call transcripts and email body content for one recipient, on
    request (GDPR Art. 17 "right to erasure").

    Same content-only scrub semantics as
    ``hailhq.core.retention.purge_expired_data`` — row shells stay intact.

    Deliberately preserves two things, do not "fix" this to remove them:

    * Suppression rows are **kept**. Removing someone's do-not-contact
      entry because they asked to be forgotten would be perverse — it
      would re-expose them to being contacted again. A deletion request
      scrubs message *content*, not the fact that this recipient opted out.
    * ``audit_log`` rows are **kept**. Retained for legitimate security /
      legal-obligation purposes — a narrow exception GDPR Art. 17(3)
      itself carves out, not a loophole to skip deletion work elsewhere.
    """
    record = await lookup_recipient(session, identifier)

    calls_scrubbed = 0
    for call in record.calls:
        if call.transcript is not None:
            call.transcript = None
            calls_scrubbed += 1

    emails_scrubbed = 0
    for email in record.emails:
        if email.body_text or email.body_html or email.raw_s3_key:
            # emails_body_required CHECK needs body_text or body_html
            # non-NULL; "" satisfies that while still clearing content.
            email.body_text = ""
            email.body_html = None
            email.raw_s3_key = None
            emails_scrubbed += 1

    await session.commit()

    return DeletionSummary(
        identifier=record.identifier,
        calls_scrubbed=calls_scrubbed,
        emails_scrubbed=emails_scrubbed,
        suppressions_preserved=len(record.suppressions),
    )
