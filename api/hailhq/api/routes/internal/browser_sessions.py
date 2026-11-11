"""Website-only browser sessions. Raw-body HMAC authenticates every control.

The website checks membership/admin permissions. No public client can choose
an organization, price, billing mode, tool set, or duration via this route.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from hailhq.api.routes.calls import get_livekit
from hailhq.api.routes.internal.auth import verify_internal_request
from hailhq.core.billing import get_ledger_cents
from hailhq.core.browser_voice import (
    BROWSER_CONCURRENCY,
    SETUP_SECONDS,
    TRIAL_CONCURRENCY,
    finish_browser_call,
    read_browser_budget,
    session_charge_cents,
    trial_remaining_ms,
)
from hailhq.core.config import settings
from hailhq.core.db import get_session, org_lock
from hailhq.core.livekit import LiveKitClient
from hailhq.core.models import AccountCredit, Agent, Call
from hailhq.core.schemas import TERMINAL_CALL_STATUSES
from livekit import api
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(
    prefix="/internal/browser-sessions",
    include_in_schema=False,
    dependencies=[Depends(verify_internal_request)],
)


class Start(BaseModel):
    trial_only: bool = False
    organization_id: UUID
    agent_id: UUID
    request_id: UUID
    rate_cents_per_minute: Decimal = Field(
        gt=0, le=10000, max_digits=8, decimal_places=3
    )


class Control(BaseModel):
    organization_id: UUID
    id: UUID


class BudgetRequest(BaseModel):
    organization_id: UUID


async def budget_for(db, organization_id, current=None):
    return await read_browser_budget(db, organization_id, current)


@router.post("/budget")
async def budget(
    body: BudgetRequest, db: Annotated[AsyncSession, Depends(get_session)]
):
    return await budget_for(db, body.organization_id)


def view(call: Call) -> dict:
    return {
        "id": str(call.id),
        "room": call.livekit_room,
        "status": call.status,
        "trial": bool(call.metadata_.get("browser_trial")),
        "seconds": call.max_duration_seconds,
        "used_ms": call.metadata_.get("browser_used_ms"),
        "ready_at": call.answered_at.isoformat() if call.answered_at else None,
        "transcript": call.transcript,
        "rate_cents_per_minute": call.metadata_.get("browser_rate_cents"),
    }


async def delete_browser_room(lk, room_name):
    try:
        await lk.delete_room(room_name)
    except api.TwirpError as exc:
        if exc.code != "not_found":
            raise


async def scoped(db, body):
    call = await db.get(Call, body.id)
    if (
        call is None
        or call.transport != "browser"
        or call.organization_id != body.organization_id
    ):
        raise HTTPException(404, "Browser conversation not found.")
    return call


@router.post("")
async def start(
    body: Start,
    db: Annotated[AsyncSession, Depends(get_session)],
    lk: Annotated[LiveKitClient, Depends(get_livekit)],
):
    # Transaction-scoped global admission lock: all API replicas see the same
    # active reservations before any LiveKit side effects are initiated.
    agent = await db.get(Agent, body.agent_id)
    if agent is None or agent.organization_id != body.organization_id:
        raise HTTPException(404, "Agent not found.")
    await org_lock(db, body.organization_id)
    await db.execute(text("SELECT pg_advisory_xact_lock(737492811)"))
    prior = await db.get(Call, body.request_id)
    if prior is not None:
        raise HTTPException(
            409,
            "This start request has already been used. Check the conversation before retrying.",
        )
    active = (
        (
            await db.execute(
                select(Call).where(
                    Call.transport == "browser",
                    Call.status.not_in(TERMINAL_CALL_STATUSES),
                )
            )
        )
        .scalars()
        .all()
    )
    if any(c.organization_id == body.organization_id for c in active):
        raise HTTPException(
            409, "A browser conversation is already running in this workspace."
        )
    history = (
        (
            await db.execute(
                select(Call).where(
                    Call.transport == "browser",
                    Call.organization_id == body.organization_id,
                )
            )
        )
        .scalars()
        .all()
    )
    balance = await get_ledger_cents(db, body.organization_id)
    has_paid = (
        await db.execute(
            select(AccountCredit.id)
            .where(
                AccountCredit.organization_id == body.organization_id,
                AccountCredit.channel == "credit",
                AccountCredit.kind == "credit",
            )
            .limit(1)
        )
    ).scalar_one_or_none() is not None
    trial = balance <= 0 and not has_paid
    if balance <= 0 and not trial:
        raise HTTPException(402, "Add credits to continue testing your agent.")
    if body.trial_only and not trial:
        raise HTTPException(
            409, "Your balance changed. Refresh before starting a paid conversation."
        )
    seconds = (
        min(60, trial_remaining_ms(history) // 1000)
        if trial
        else min(600, int(balance * 60 / body.rate_cents_per_minute))
    )
    if seconds <= 0:
        raise HTTPException(402, "Your free minute is used. Add credits to continue.")
    if len(active) >= BROWSER_CONCURRENCY or (
        trial
        and sum(bool(c.metadata_.get("browser_trial")) for c in active)
        >= TRIAL_CONCURRENCY
    ):
        raise HTTPException(
            429, "All browser test slots are busy. Please try again shortly."
        )
    reserved = session_charge_cents(
        seconds * 1000, str(body.rate_cents_per_minute), seconds
    )
    call = Call(
        id=body.request_id,
        organization_id=body.organization_id,
        agent_id=agent.id,
        transport="browser",
        direction="inbound",
        status="queued",
        provider="browser",
        voice_config=agent.voice_config or {},
        initial_prompt=agent.system_prompt,
        max_duration_seconds=seconds,
        livekit_room=f"hail-{body.request_id}",
        metadata_={
            "browser_trial": trial,
            "browser_rate_cents": str(body.rate_cents_per_minute),
            "browser_reserved_cents": str(reserved),
            "recording_enabled": False,
        },
    )
    db.add(call)
    if not trial:
        db.add(
            AccountCredit(
                organization_id=body.organization_id,
                kind="debit",
                channel="voice",
                amount_cents=-reserved,
                source="browser_voice_reservation",
                qty=seconds * 1000,
                ref=f"browser-reserve:{call.id}",
            )
        )
    await db.commit()
    try:
        await lk.create_room(call.id)
        # Room creation helper uses hail-<id>; retain that exact opaque name.
        call.livekit_room = f"hail-{call.id}"
        await db.commit()
        await lk.dispatch_agent(
            room_name=call.livekit_room,
            agent_name="hail-voicebot",
            metadata={"transport": "browser", "call_id": str(call.id)},
        )
        token = (
            api.AccessToken(settings.livekit_api_key, settings.livekit_api_secret)
            .with_identity(f"browser-{uuid4()}")
            .with_ttl(timedelta(seconds=SETUP_SECONDS))
            .with_grants(
                api.VideoGrants(
                    room_join=True,
                    room=call.livekit_room,
                    can_publish=True,
                    can_subscribe=True,
                    can_publish_data=False,
                    can_publish_sources=["microphone"],
                    can_update_own_metadata=False,
                )
            )
            .to_jwt()
        )
        return {
            **view(call),
            "token": token,
            "url": settings.livekit_url,
            "budget": await budget_for(db, call.organization_id, call),
        }
    except Exception:
        try:
            await delete_browser_room(lk, call.livekit_room)
        finally:
            await finish_browser_call(db, call.id, failed=True)
            await db.commit()
        raise HTTPException(
            503, "The browser conversation could not start. Please try again."
        ) from None


@router.post("/read")
async def read(body: Control, db: Annotated[AsyncSession, Depends(get_session)]):
    call = await scoped(db, body)
    return {**view(call), "budget": await budget_for(db, call.organization_id, call)}


@router.post("/stop")
async def stop(
    body: Control,
    db: Annotated[AsyncSession, Depends(get_session)],
    lk: Annotated[LiveKitClient, Depends(get_livekit)],
):
    call = await scoped(db, body)
    if call.status not in TERMINAL_CALL_STATUSES:
        # Only settle after the room is gone; a teardown failure keeps its
        # reservation, preventing another start while audio may still run.
        media_ended_at = datetime.now(timezone.utc)
        await delete_browser_room(lk, call.livekit_room)
        await finish_browser_call(db, call.id, ended_at=media_ended_at)
        await db.commit()
    return {**view(call), "budget": await budget_for(db, call.organization_id, call)}
