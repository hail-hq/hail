"""CRUD for agents: the saved brain a number answers with.

POST   /agents          create
GET    /agents          list (newest first)
GET    /agents/{id}     detail
PATCH  /agents/{id}     partial update
DELETE /agents/{id}     delete; its numbers are unregistered for inbound first

Numbers pick their agent on ``PATCH /numbers/{id}`` (``voice_agent_id``,
``sms_agent_id``). ``POST /calls`` places outbound calls with ``agent_id``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi import status as http_status
from hailhq.api.audit import actor_of, write_audit_log
from hailhq.api.deps import Principal, get_current_principal
from hailhq.api.errors import unprocessable
from hailhq.api.ratelimit import GENERAL_RATE_LIMITED_RESPONSES
from hailhq.api.routes.calls import get_livekit_optional
from hailhq.core import inbound_routing
from hailhq.core.agent_tools.registry import all_tools
from hailhq.core.db import get_session, org_lock
from hailhq.core.livekit import LiveKitClient
from hailhq.core.models import Agent, PhoneNumber
from hailhq.core.schemas import (
    AgentCreate,
    AgentListResponse,
    AgentResponse,
    AgentUpdate,
)
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/agents", tags=["agents"], responses=GENERAL_RATE_LIMITED_RESPONSES
)

_CONFLICT = {409: {"description": "An agent with this name already exists."}}


async def _load_owned(db: AsyncSession, agent_id: UUID, org_id: UUID) -> Agent:
    agent = (
        await db.execute(
            select(Agent).where(Agent.id == agent_id, Agent.organization_id == org_id)
        )
    ).scalar_one_or_none()
    if agent is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="agent not found"
        )
    return agent


def _check_tools(tools: list[str] | None) -> None:
    """Unknown tool names fail here, not on the first call that uses the agent."""
    if tools is None:
        return
    known = {t.name for t in all_tools()}
    unknown = sorted(set(tools) - known)
    if unknown:
        raise unprocessable(
            f"unknown tools: {', '.join(unknown)}", loc=["body", "tools"]
        )


def _name_conflict() -> HTTPException:
    return HTTPException(
        status_code=http_status.HTTP_409_CONFLICT,
        detail="an agent with this name already exists",
    )


@router.post(
    "",
    response_model=AgentResponse,
    status_code=http_status.HTTP_201_CREATED,
    responses=_CONFLICT,
)
async def create_agent(
    body: AgentCreate,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> AgentResponse:
    """Create an agent. Route numbers to it with PATCH /numbers/{id}, or
    place calls with it via ``agent_id`` on POST /calls."""
    _check_tools(body.tools)
    agent = Agent(
        organization_id=principal.organization_id,
        name=body.name,
        system_prompt=body.system_prompt,
        first_message=body.first_message,
        ai_disclosure=body.ai_disclosure,
        ai_disclosure_line=body.ai_disclosure_line,
        voice_config=body.voice_config.model_dump(mode="json"),
        tools=body.tools,
        max_duration_seconds=body.max_duration_seconds,
        voice_enabled=body.voice_enabled,
        sms_enabled=body.sms_enabled,
        status=body.status,
    )
    db.add(agent)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise _name_conflict() from exc
    await db.refresh(agent)
    actor_user_id, actor_kind = actor_of(principal)
    await write_audit_log(
        organization_id=principal.organization_id,
        api_key_id=principal.api_key_id,
        action="agent.create",
        resource_type="agent",
        resource_id=agent.id,
        payload={"name": agent.name, "ai_disclosure": agent.ai_disclosure},
        actor_user_id=actor_user_id,
        actor_kind=actor_kind,
    )
    return AgentResponse.model_validate(agent)


@router.get("", response_model=AgentListResponse)
async def list_agents(
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> AgentListResponse:
    """List the organization's agents, newest first."""
    rows = (
        (
            await db.execute(
                select(Agent)
                .where(Agent.organization_id == principal.organization_id)
                .order_by(Agent.created_at.desc(), Agent.id.desc())
            )
        )
        .scalars()
        .all()
    )
    return AgentListResponse(items=[AgentResponse.model_validate(a) for a in rows])


@router.get("/{agent_id}", response_model=AgentResponse)
async def get_agent(
    agent_id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> AgentResponse:
    """Fetch one agent by id, including its instructions and voice settings."""
    return AgentResponse.model_validate(
        await _load_owned(db, agent_id, principal.organization_id)
    )


@router.patch("/{agent_id}", response_model=AgentResponse, responses=_CONFLICT)
async def update_agent(
    agent_id: UUID,
    body: AgentUpdate,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> AgentResponse:
    """Change some fields. Fields left out keep their value; ``null`` clears
    first_message, ai_disclosure_line, tools or max_duration_seconds. Live
    calls keep the settings they started with."""
    agent = await _load_owned(db, agent_id, principal.organization_id)
    changes = body.model_dump(exclude_unset=True)
    if "tools" in changes:
        _check_tools(changes["tools"])
    for field in (
        "name",
        "system_prompt",
        "ai_disclosure",
        "voice_config",
        "voice_enabled",
        "sms_enabled",
        "status",
    ):
        if field in changes and changes[field] is None:
            # Not nullable; a null here means "leave it". Storing None in the
            # JSONB voice_config column would write JSON null and break every
            # read of the agent and every call it answers.
            changes.pop(field)
    if "voice_config" in changes:
        changes["voice_config"] = body.voice_config.model_dump(mode="json")
    for field, value in changes.items():
        setattr(agent, field, value)
    if changes:
        agent.updated_at = datetime.now(timezone.utc)
        try:
            await db.commit()
        except IntegrityError as exc:
            await db.rollback()
            raise _name_conflict() from exc
        await db.refresh(agent)
        actor_user_id, actor_kind = actor_of(principal)
        await write_audit_log(
            organization_id=principal.organization_id,
            api_key_id=principal.api_key_id,
            action="agent.update",
            resource_type="agent",
            resource_id=agent.id,
            payload={"fields": sorted(changes)},
            actor_user_id=actor_user_id,
            actor_kind=actor_kind,
        )
    return AgentResponse.model_validate(agent)


@router.delete(
    "/{agent_id}",
    status_code=http_status.HTTP_204_NO_CONTENT,
    responses={
        502: {
            "description": "A number could not be taken off the inbound trunk; nothing was deleted."
        },
        503: {
            "description": "A number is registered for inbound calls and this server is not configured for them; nothing was deleted."
        },
    },
)
async def delete_agent(
    agent_id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    lk: Annotated[LiveKitClient | None, Depends(get_livekit_optional)],
) -> Response:
    """Delete an agent. Numbers that routed calls to it are unregistered
    for inbound and ring out again; numbers that routed texts to it go back
    to webhooks only."""
    agent = await _load_owned(db, agent_id, principal.organization_id)
    detached = 0
    while True:
        # The org lock is the one PATCH /numbers/{id} takes. Each pass holds
        # it from the lookup to its commit, and the final pass holds it from
        # "no numbers left" to the delete, so a number cannot be pointed at
        # this agent in between and be left registered with no agent.
        await org_lock(db, principal.organization_id)
        number = (
            await db.execute(
                select(PhoneNumber)
                .where(
                    PhoneNumber.organization_id == principal.organization_id,
                    or_(
                        PhoneNumber.voice_agent_id == agent.id,
                        PhoneNumber.sms_agent_id == agent.id,
                    ),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if number is None:
            break
        if number.voice_agent_id == agent.id:
            # LiveKit is only touched for a number registered for inbound
            # calls: a server without LiveKit settings still deletes agents.
            if number.inbound_registered_at is not None:
                if lk is None:
                    raise HTTPException(
                        status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail="inbound calls are not configured on this server",
                    )
                try:
                    await inbound_routing.unregister(db, lk, number)
                except inbound_routing.InboundRoutingError as exc:
                    # Numbers handled before this one are already committed as
                    # unregistered, so a retry does not redo (or misreport) them.
                    # Read e164 first: rollback expires the row, and reading an
                    # expired attribute on an async session raises.
                    e164 = number.e164
                    await db.rollback()
                    raise HTTPException(
                        status_code=http_status.HTTP_502_BAD_GATEWAY,
                        detail=f"could not unregister {e164} (stage: {exc.stage})",
                    ) from exc
            number.voice_agent_id = None
        if number.sms_agent_id == agent.id:
            number.sms_agent_id = None
        # Commit per number: the carrier and LiveKit work is already done.
        await db.commit()
        detached += 1
    await db.delete(agent)
    await db.commit()
    actor_user_id, actor_kind = actor_of(principal)
    await write_audit_log(
        organization_id=principal.organization_id,
        api_key_id=principal.api_key_id,
        action="agent.delete",
        resource_type="agent",
        resource_id=agent_id,
        payload={"numbers_detached": detached},
        actor_user_id=actor_user_id,
        actor_kind=actor_kind,
    )
    return Response(status_code=http_status.HTTP_204_NO_CONTENT)
