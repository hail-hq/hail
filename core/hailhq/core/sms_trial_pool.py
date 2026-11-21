"""Operator changes to the US trial texting numbers, for
``scripts/ops/sms_trial_numbers.py``. Each change locks the number's row,
checks it, and returns the lines that describe it; it writes only with
``apply`` and never commits (the script does). Runbook:
docs/operations/sms-trial-numbers.md."""

from __future__ import annotations

from hailhq.core.models import PhoneNumber
from hailhq.core.sms_trial import APPROVED_AT_KEY, POOL_PURPOSE, trial_sender
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
    if problems:
        raise TrialPoolError(f"{number.e164}: " + "; ".join(problems) + ".")


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
    if number.reserved_call_id is not None:
        raise TrialPoolError(f"{e164}: on a call now. Try again after it ends.")
    _check_can_send_trial_texts(number)
    if apply:
        number.pool_purpose = POOL_PURPOSE
        await db.flush()
    return [f"mark {e164}: leaves the call pool and sends only trial texts."]


async def unmark(db: AsyncSession, e164: str, *, apply: bool) -> list[str]:
    """A trial texting number goes back to the shared call pool."""
    number = await _locked(db, e164)
    if not number.is_pool or number.pool_purpose != POOL_PURPOSE:
        raise TrialPoolError(f"{e164}: not a trial texting number.")
    if "voice" not in number.capabilities:
        # The call pool claims numbers for calls with no capability check.
        raise TrialPoolError(
            f"{e164}: cannot make calls, so it cannot join the call pool."
        )
    if apply:
        number.pool_purpose = None
        number.provisioning_metadata = {
            k: v
            for k, v in number.provisioning_metadata.items()
            if k != APPROVED_AT_KEY
        }
        await db.flush()
    return [
        (
            f"unmark {e164}: stops sending trial texts and joins the shared call "
            "pool (outbound calls and browser sessions of workspaces with no number)."
        )
    ]


async def move(db: AsyncSession, e164: str, *, apply: bool) -> list[str]:
    """A workspace's number leaves the workspace and becomes a trial texting
    pool number."""
    number = await _locked(db, e164)
    if number.is_pool:
        raise TrialPoolError(f"{e164}: already a pool number. Use mark.")
    _check_can_send_trial_texts(number)
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
            "(the row stays in the workspace). Do not cancel that registration: "
            "a cancel deletes the toll-free verification at the carrier."
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
    sender = await trial_sender(db)
    lines = []
    for n in numbers:
        approved = n.provisioning_metadata.get(APPROVED_AT_KEY)
        ready = f"ready (approved, checked {approved})" if approved else "not ready"
        sends = ", sends now" if sender is not None and sender.id == n.id else ""
        lines.append(f"{n.e164} {n.provider} {n.provisioning_state}: {ready}{sends}")
    if not lines:
        lines.append("No trial texting numbers.")
    return lines
