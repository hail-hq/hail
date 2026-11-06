"""The tools of an email run: what an agent can do while it answers an email.

Every tool the agent allows that works outside a call. Each send names a member
from the directory, and the email-run routes in the API resolve the address.
The thread is already in the chat, so ``thread_history`` is not here.
"""

from __future__ import annotations

import logging

from hailhq.core.agent_tools import list_contacts, send_email, send_sms
from hailhq.core.agent_tools.spec import ToolSpec
from hailhq.core.models import Agent
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("hailhq.core.agent_tools")


async def email_run_tools(agent: Agent, session: AsyncSession) -> list[ToolSpec]:
    """``agents.tools``: NULL means every tool, a list names the allowed ones.

    A tool whose availability check fails is left out, and the reply still
    goes: same posture as the voicebot's ``build_agent_tools``.
    """
    specs: list[ToolSpec] = []
    for spec in (
        list_contacts.EMAIL_RUN_SPEC,
        send_email.EMAIL_RUN_SPEC,
        send_sms.EMAIL_RUN_SPEC,
    ):
        if agent.tools is not None and spec.name not in agent.tools:
            continue
        try:
            if await spec.is_available(agent.organization_id, session):
                specs.append(spec)
        except Exception:
            logger.exception("availability check failed for tool %s", spec.name)
            # A failed statement aborts the transaction; roll back so the
            # next check and the caller's later reads still work.
            await session.rollback()
    return specs


__all__ = ["email_run_tools"]
