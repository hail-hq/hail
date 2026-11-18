"""CRUD for agents: the saved brain a number answers with.

POST   /agents          create
GET    /agents          list (newest first)
GET    /agents/{id}     detail
PATCH  /agents/{id}     partial update
DELETE /agents/{id}     delete; its numbers are unregistered for inbound first
POST   /agents/{id}/rotate-tool-secret   new HTTP tool signing secret, shown once

Numbers pick their agent on ``PATCH /numbers/{id}`` (``voice_agent_id``,
``sms_agent_id``). ``POST /calls`` places outbound calls with ``agent_id``.
"""

from __future__ import annotations

import asyncio
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
from hailhq.core.handover import (
    HandoverInvalid,
    HandoverItem,
    load_handover,
    prune_departed_members,
    replace_handover,
    validate_handover,
)
from hailhq.core.http_tools import (
    HttpToolHeadersNeeded,
    load_http_tools,
    new_tool_secret,
    replace_http_tools,
)
from hailhq.core.livekit import LiveKitClient
from hailhq.core.models import Agent, EmailDomain, PhoneNumber
from hailhq.core.provider_config import provider_cipher
from hailhq.core.schemas import (
    AgentCreate,
    AgentListResponse,
    AgentResponse,
    AgentUpdate,
    HandoverContactOut,
    HttpToolIn,
)
from hailhq.core.secret_cipher import SecretKeyMissing
from hailhq.core.url_guard import UnsafeUrlError, assert_public_https_url
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/agents", tags=["agents"], responses=GENERAL_RATE_LIMITED_RESPONSES
)

_CONFLICT = {
    409: {
        "description": (
            "An agent with this name already exists, or a handover contact "
            "changed while saving."
        )
    }
}


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


def _with_handover_tool(
    tools: list[str] | None, has_handover: bool
) -> list[str] | None:
    """Keep ``transfer_call`` in an explicit tools list exactly when the agent
    has handover contacts (the console applies the same rule). ``None``
    (every tool) is left alone. Returns a new list."""
    if tools is None:
        return None
    rest = [t for t in tools if t != "transfer_call"]
    if not has_handover:
        return rest
    return list(tools) if "transfer_call" in tools else [*tools, "transfer_call"]


async def _check_handover(
    db: AsyncSession,
    org_id: UUID,
    items,
    unchanged: frozenset[str] = frozenset(),
) -> list[HandoverItem]:
    parsed = [HandoverItem(i.contact_id, i.note.strip()) for i in items]
    for index, item in enumerate(parsed):
        if not item.note:
            raise unprocessable(
                "note must not be blank", loc=["body", "handover_contacts", index]
            )
    try:
        await validate_handover(db, org_id, parsed, unchanged)
    except HandoverInvalid as exc:
        loc: list = ["body", "handover_contacts"]
        if exc.index is not None:
            loc.append(exc.index)
        raise unprocessable(str(exc), loc=loc) from exc
    return parsed


async def _check_http_tools(items: list[HttpToolIn] | None) -> None:
    """Each URL must resolve to public addresses now; the agent checks again
    on every request. The lookups run side by side."""
    items = items or []
    results = await asyncio.gather(
        *(asyncio.to_thread(assert_public_https_url, item.url) for item in items),
        return_exceptions=True,
    )
    for index, result in enumerate(results):
        if isinstance(result, UnsafeUrlError):
            raise unprocessable(
                str(result), loc=["body", "http_tools", index, "url"]
            ) from result
        if isinstance(result, BaseException):
            raise result


def _no_secret_key() -> HTTPException:
    return HTTPException(
        status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="this server cannot store HTTP tools: HAIL_PROVIDER_SECRET_KEY is not set",
    )


async def _respond(
    db: AsyncSession, agents: list[Agent], *, http_tool_secret: str | None = None
) -> list[AgentResponse]:
    ids = [a.id for a in agents]
    links = await load_handover(db, ids)
    tools = await load_http_tools(db, ids)
    return [
        AgentResponse.model_validate(a).model_copy(
            update={
                "handover_contacts": [
                    HandoverContactOut(**link) for link in links.get(a.id, [])
                ],
                "http_tools": tools.get(a.id, []),
                "http_tool_secret": http_tool_secret,
            }
        )
        for a in agents
    ]


def _save_conflict(exc: IntegrityError) -> HTTPException:
    """409 for a failed save: a duplicate name, or a handover contact that was
    deleted or relinked by another request between the check and the commit."""
    if "agents_org_name_uq" in str(exc.orig):
        detail = "an agent with this name already exists"
    else:
        detail = "a handover contact changed while saving; try again"
    return HTTPException(status_code=http_status.HTTP_409_CONFLICT, detail=detail)


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
    await _check_http_tools(body.http_tools)
    handover = await _check_handover(
        db, principal.organization_id, body.handover_contacts
    )
    agent = Agent(
        organization_id=principal.organization_id,
        name=body.name,
        system_prompt=body.system_prompt,
        first_message=body.first_message,
        ai_disclosure=body.ai_disclosure,
        recording_enabled=body.recording_enabled,
        ai_disclosure_line=body.ai_disclosure_line,
        voice_config=body.voice_config.model_dump(mode="json"),
        tools=_with_handover_tool(body.tools, bool(handover)),
        max_duration_seconds=body.max_duration_seconds,
        handover_max_duration_seconds=body.handover_max_duration_seconds,
        voice_enabled=body.voice_enabled,
        sms_enabled=body.sms_enabled,
        email_enabled=body.email_enabled,
        status=body.status,
    )
    db.add(agent)
    try:
        await db.flush()
        await replace_handover(db, agent.id, handover)
        secret = await replace_http_tools(db, agent, body.http_tools)
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise _save_conflict(exc) from exc
    except SecretKeyMissing as exc:
        await db.rollback()
        raise _no_secret_key() from exc
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
    return (await _respond(db, [agent], http_tool_secret=secret))[0]


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
    return AgentListResponse(items=await _respond(db, list(rows)))


@router.get("/prompt-templates", include_in_schema=False)
async def get_prompt_templates(
    principal: Annotated[Principal, Depends(get_current_principal)],
) -> None:
    """Retired disclosure endpoint. Keep before /{agent_id} for old clients."""
    raise HTTPException(
        status_code=410, detail="Prompt templates are no longer available."
    )


@router.get("/{agent_id}", response_model=AgentResponse)
async def get_agent(
    agent_id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> AgentResponse:
    """Fetch one agent by id, including its instructions and voice settings."""
    agent = await _load_owned(db, agent_id, principal.organization_id)
    return (await _respond(db, [agent]))[0]


@router.patch("/{agent_id}", response_model=AgentResponse, responses=_CONFLICT)
async def update_agent(
    agent_id: UUID,
    body: AgentUpdate,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> AgentResponse:
    """Change some fields. Fields left out keep their value; ``null`` clears
    first_message, ai_disclosure_line, tools, max_duration_seconds or
    handover_max_duration_seconds. Live
    calls keep the settings they started with. ``handover_contacts`` replaces
    the list; ``[]`` clears it and ``null`` leaves it."""
    agent = await _load_owned(db, agent_id, principal.organization_id)
    changes = body.model_dump(exclude_unset=True)
    changes.pop("handover_contacts", None)
    changes.pop("http_tools", None)
    if "tools" in changes:
        _check_tools(changes["tools"])
    await _check_http_tools(body.http_tools)
    for field in (
        "name",
        "system_prompt",
        "ai_disclosure",
        "voice_config",
        "voice_enabled",
        "sms_enabled",
        "email_enabled",
        "status",
    ):
        if field in changes and changes[field] is None:
            # Not nullable; a null here means "leave it". Storing None in the
            # JSONB voice_config column would write JSON null and break every
            # read of the agent and every call it answers.
            changes.pop(field)
    if "voice_config" in changes:
        changes["voice_config"] = body.voice_config.model_dump(mode="json")
    # Validate before any attribute is set: the validation queries would
    # autoflush a pending name change outside the conflict handler below.
    items = None
    if body.handover_contacts is not None:
        # Contacts already linked are not re-checked: the console sends the
        # whole list on every save, and an old link whose number later went
        # on the DNC list must not block an unrelated edit. /handover checks
        # again before every dial.
        linked = frozenset(
            c["contact_id"]
            for c in (await load_handover(db, [agent.id])).get(agent.id, [])
        )
        items = await _check_handover(
            db, principal.organization_id, body.handover_contacts, linked
        )
    if "tools" in changes or items is not None:
        # Same rule as create, on the values the agent will have after this.
        tools = changes.get("tools", agent.tools)
        if items is not None:
            has_handover = bool(items)
        else:
            has_handover = bool((await load_handover(db, [agent.id])).get(agent.id))
        synced = _with_handover_tool(tools, has_handover)
        if synced != tools or "tools" in changes:
            changes["tools"] = synced
    for field, value in changes.items():
        setattr(agent, field, value)
    if items is not None:
        changes["handover_contacts"] = True
    if body.http_tools is not None:
        changes["http_tools"] = True
    secret = None
    if changes:
        agent.updated_at = datetime.now(timezone.utc)
        try:
            if items is not None:
                await replace_handover(db, agent.id, items)
            else:
                await prune_departed_members(db, agent.id)
            if body.http_tools is not None:
                secret = await replace_http_tools(db, agent, body.http_tools)
            await db.commit()
        except IntegrityError as exc:
            await db.rollback()
            raise _save_conflict(exc) from exc
        except SecretKeyMissing as exc:
            await db.rollback()
            raise _no_secret_key() from exc
        except HttpToolHeadersNeeded as exc:
            await db.rollback()
            raise unprocessable(
                str(exc), loc=["body", "http_tools", exc.index, "headers"]
            ) from exc
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
    return (await _respond(db, [agent], http_tool_secret=secret))[0]


@router.post(
    "/{agent_id}/rotate-tool-secret",
    response_model=AgentResponse,
    responses={503: {"description": "HAIL_PROVIDER_SECRET_KEY is not set."}},
)
async def rotate_tool_secret(
    agent_id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> AgentResponse:
    """Make a new secret for this agent's HTTP tool requests. The response's
    ``http_tool_secret`` is the only time it is shown. The old secret stops
    signing at once; live calls keep the one they started with."""
    agent = await _load_owned(db, agent_id, principal.organization_id)
    secret = new_tool_secret()
    try:
        agent.http_tool_secret_encrypted = provider_cipher().encrypt(secret)
    except SecretKeyMissing as exc:
        raise _no_secret_key() from exc
    agent.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(agent)
    actor_user_id, actor_kind = actor_of(principal)
    # Log the rotation only, never the secret.
    await write_audit_log(
        organization_id=principal.organization_id,
        api_key_id=principal.api_key_id,
        action="agent.rotate_tool_secret",
        resource_type="agent",
        resource_id=agent.id,
        payload={},
        actor_user_id=actor_user_id,
        actor_kind=actor_kind,
    )
    return (await _respond(db, [agent], http_tool_secret=secret))[0]


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
    to webhooks only. Addresses it answered stop getting replies."""
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
    # The foreign key clears the route. The audit row keeps the record.
    email_routes = await db.scalar(
        select(func.count(EmailDomain.id)).where(EmailDomain.email_agent_id == agent.id)
    )
    await db.delete(agent)
    await db.commit()
    actor_user_id, actor_kind = actor_of(principal)
    await write_audit_log(
        organization_id=principal.organization_id,
        api_key_id=principal.api_key_id,
        action="agent.delete",
        resource_type="agent",
        resource_id=agent_id,
        payload={"numbers_detached": detached, "email_routes_detached": email_routes},
        actor_user_id=actor_user_id,
        actor_kind=actor_kind,
    )
    return Response(status_code=http_status.HTTP_204_NO_CONTENT)
