"""Operator changes to the US trial texting numbers, for
``scripts/ops/sms_trial_numbers.py``. Each change locks the number's row,
checks it, and returns the lines that describe it; it writes only with
``apply`` and never commits (the script does). Runbook:
docs/operations/sms-trial-numbers.md."""

from __future__ import annotations

from datetime import UTC, datetime

from hailhq.core.models import Call, CarrierVerification, PhoneNumber
from hailhq.core.schemas import TERMINAL_CALL_STATUSES
from hailhq.core.sms_trial import (
    APPROVED_AT_KEY,
    PAUSED_KEY,
    POOL_PURPOSE,
    confirms_tollfree_approval,
    ready_trial_number_ids,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


class TrialPoolError(Exception):
    """The number cannot take this change; the message says why."""


async def _locked(db: AsyncSession, e164: str) -> PhoneNumber:
    number = (
        await db.execute(
            select(PhoneNumber)
            .where(
                PhoneNumber.e164 == e164,
                PhoneNumber.provisioning_state.not_in(("released", "failed")),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if number is None:
        raise TrialPoolError(f"{e164}: no live number with this E.164.")
    return number


def _check_can_send_trial_texts(number: PhoneNumber) -> None:
    problems = []
    if number.country_code != "US" or number.number_type != "toll_free":
        problems.append("not a US toll-free number")
    if number.provisioning_state != "active":
        problems.append(f"state is {number.provisioning_state}, not active")
    if "sms" not in number.capabilities:
        problems.append("cannot text")
    if not number.provider_resource_id:
        problems.append("has no carrier id")
    if not confirms_tollfree_approval(number.provider):
        problems.append(
            f"{number.provider} cannot confirm toll-free approval, so it would "
            "never be ready"
        )
    if problems:
        raise TrialPoolError(f"{number.e164}: " + "; ".join(problems) + ".")


async def _check_not_on_a_call(db: AsyncSession, number: PhoneNumber) -> None:
    """A number held by a call (pool reservation) or texting for a live
    browser session cannot change hands now."""
    live = (
        await db.execute(
            select(Call.id)
            .where(
                Call.status.not_in(TERMINAL_CALL_STATUSES),
                Call.metadata_["browser_sms_number_id"].astext == str(number.id),
            )
            .limit(1)
        )
    ).first()
    if number.reserved_call_id is not None or live is not None:
        raise TrialPoolError(f"{number.e164}: on a call now. Try again after it ends.")


def _trial_number(number: PhoneNumber) -> None:
    if not number.is_pool or number.pool_purpose != POOL_PURPOSE:
        raise TrialPoolError(f"{number.e164}: not a trial texting number.")


def _without(metadata: dict, *keys: str) -> dict:
    return {k: v for k, v in metadata.items() if k not in keys}


async def mark(db: AsyncSession, e164: str, *, apply: bool) -> list[str]:
    """A call pool number becomes a trial texting number."""
    number = await _locked(db, e164)
    if not number.is_pool:
        raise TrialPoolError(
            f"{e164}: belongs to workspace {number.organization_id}. "
            "Use move to put it in the pool."
        )
    if number.pool_purpose == POOL_PURPOSE:
        raise TrialPoolError(f"{e164}: already a trial texting number.")
    await _check_not_on_a_call(db, number)
    _check_can_send_trial_texts(number)
    if apply:
        number.pool_purpose = POOL_PURPOSE
        await db.flush()
    return [f"mark {e164}: leaves the call pool and sends only trial texts."]


async def unmark(db: AsyncSession, e164: str, *, apply: bool) -> list[str]:
    """A trial texting number goes back to the shared pool. A number that
    cannot make calls is never claimed for one (``claim_pool_number`` with
    ``capability="voice"``)."""
    number = await _locked(db, e164)
    _trial_number(number)
    if apply:
        number.pool_purpose = None
        number.provisioning_metadata = _without(
            number.provisioning_metadata, APPROVED_AT_KEY, PAUSED_KEY
        )
        await db.flush()
    if "voice" in number.capabilities:
        joins = "outbound calls and browser sessions of workspaces with no number"
    else:
        joins = "browser texts only: it cannot make calls"
    return [
        f"unmark {e164}: stops sending trial texts and joins the shared pool ({joins})."
    ]


async def pause(db: AsyncSession, e164: str, *, apply: bool) -> list[str]:
    """Stop trial texts from the number at once. It stays a trial number,
    so it never goes back to the call pool."""
    number = await _locked(db, e164)
    _trial_number(number)
    if PAUSED_KEY in number.provisioning_metadata:
        raise TrialPoolError(f"{e164}: already paused.")
    if apply:
        number.provisioning_metadata = {
            **number.provisioning_metadata,
            PAUSED_KEY: datetime.now(UTC).isoformat(),
        }
        await db.flush()
    return [f"pause {e164}: stops sending trial texts until resume."]


async def resume(db: AsyncSession, e164: str, *, apply: bool) -> list[str]:
    """Send trial texts from a paused number again (once it is ready)."""
    number = await _locked(db, e164)
    _trial_number(number)
    if PAUSED_KEY not in number.provisioning_metadata:
        raise TrialPoolError(f"{e164}: not paused.")
    if apply:
        number.provisioning_metadata = _without(
            number.provisioning_metadata, PAUSED_KEY
        )
        await db.flush()
    return [f"resume {e164}: sends trial texts again while it is ready."]


async def move(db: AsyncSession, e164: str, *, apply: bool) -> list[str]:
    """A workspace's number leaves the workspace and becomes a trial texting
    pool number."""
    number = await _locked(db, e164)
    if number.is_pool:
        raise TrialPoolError(f"{e164}: already a pool number. Use mark.")
    _check_can_send_trial_texts(number)
    await _check_not_on_a_call(db, number)
    if number.sms_registration_id is not None:
        registration = await db.get(CarrierVerification, number.sms_registration_id)
        if registration is not None and registration.number_id != number.id:
            # Cancel and account close keep only a registration that names
            # this number (registration_trial_number).
            raise TrialPoolError(
                f"{e164}: on the texting registration of another number "
                f"({registration.id}). Move only a number with its own "
                "toll-free registration."
            )
    if number.voice_agent_id is not None or number.sms_agent_id is not None:
        raise TrialPoolError(
            f"{e164}: an agent answers it. Remove the agent from the number first."
        )
    lines = [
        (
            f"move {e164}: leaves workspace {number.organization_id}, joins the "
            "pool and sends only trial texts."
        ),
    ]
    if number.sms_registration_id is not None:
        lines.append(
            f"  unlinks texting registration {number.sms_registration_id} "
            "(the row stays in the workspace). Hail then refuses to cancel it, "
            "and an account close keeps it: a cancel would delete the "
            "toll-free verification at the carrier."
        )
    if apply:
        number.organization_id = None
        number.is_pool = True
        number.pool_purpose = POOL_PURPOSE
        number.sms_registration_id = None
        await db.flush()
    return lines


async def status(db: AsyncSession) -> list[str]:
    """Each trial texting number, whether it is ready, and which one sends."""
    numbers = (
        (
            await db.execute(
                select(PhoneNumber)
                .where(
                    PhoneNumber.is_pool.is_(True),
                    PhoneNumber.pool_purpose == POOL_PURPOSE,
                )
                .order_by(PhoneNumber.created_at, PhoneNumber.id)
            )
        )
        .scalars()
        .all()
    )
    ready = await ready_trial_number_ids(db)
    lines = []
    for n in numbers:
        approved = n.provisioning_metadata.get(APPROVED_AT_KEY)
        if PAUSED_KEY in n.provisioning_metadata:
            state = "paused"
        elif n.id in ready:
            state = f"ready (last carrier yes {approved})"
        elif approved:
            state = f"not ready (last carrier yes {approved}, too old or inactive)"
        else:
            state = "not ready (no carrier approval)"
        sends = ", sends now" if ready and ready[0] == n.id else ""
        lines.append(f"{n.e164} {n.provider} {n.provisioning_state}: {state}{sends}")
    if not lines:
        lines.append("No trial texting numbers.")
    return lines
