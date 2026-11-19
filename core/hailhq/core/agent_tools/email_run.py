"""The tools of an email run: what an agent can do while it answers an email.

Every tool the agent allows that works outside a call. Each send names a member
from the directory, and the email-run routes in the API resolve the address.
Email history is retrieved with ``read_email``. A saved phone number does not
prove phone ownership, so email runs do not get ``thread_history``.
"""

from __future__ import annotations

import logging
import uuid

from hailhq.core.agent_tools import (
    list_contacts,
    read_email,
    send_email,
    send_sms,
)
from hailhq.core.agent_tools.http_tool import Conversation, http_tool_specs
from hailhq.core.agent_tools.spec import ToolSpec
from hailhq.core.models import Agent
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("hailhq.core.agent_tools")


async def email_run_tools(
    agent: Agent,
    session: AsyncSession,
    *,
    trust: str = "member",
    email_id: uuid.UUID | None = None,
) -> list[ToolSpec]:
    """``agents.tools``: NULL means every tool, a list names the allowed ones.

    A tool whose availability check fails is left out, and the reply still
    goes: same posture as the voicebot's ``build_agent_tools``.
    ``list_contacts`` is left out for a sender who is not a member: it would
    read the team directory back to an outsider. ``email_id`` (the email being
    answered) adds the agent's HTTP tools, bound to that email.
    """
    specs: list[ToolSpec] = []
    for spec in (
        read_email.SPEC,
        list_contacts.EMAIL_RUN_SPEC,
        send_email.EMAIL_RUN_SPEC,
        send_sms.EMAIL_RUN_SPEC,
    ):
        if agent.tools is not None and spec.name not in agent.tools:
            continue
        if spec is list_contacts.EMAIL_RUN_SPEC and trust != "member":
            continue
        try:
            if await spec.is_available(agent.organization_id, session):
                specs.append(spec)
        except Exception:
            logger.exception("availability check failed for tool %s", spec.name)
            # A failed statement aborts the transaction; roll back so the
            # next check and the caller's later reads still work.
            await session.rollback()
    if email_id is not None:
        # The agent's own HTTP tools; ``agents.tools`` names built-ins only.
        conv = Conversation(
            channel="email",
            id=email_id,
            agent_id=agent.id,
            organization_id=agent.organization_id,
        )
        try:
            specs.extend(await http_tool_specs(session, conv))
        except Exception:
            logger.exception("http tools failed to load for email %s", email_id)
            await session.rollback()
    return specs


__all__ = ["email_run_tools"]
