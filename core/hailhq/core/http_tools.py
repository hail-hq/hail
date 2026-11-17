"""Saved HTTP tools of an agent (``agent_http_tools``) and the secret that
signs their requests (``agents.http_tool_secret_encrypted``).

Header values and the secret are Fernet ciphertext under
HAIL_PROVIDER_SECRET_KEY (``provider_config.provider_cipher``), the same
posture as BYO provider keys. The runtime side is
``hailhq.core.agent_tools.http_tool``.
"""

from __future__ import annotations

import json
import secrets
from uuid import UUID

from hailhq.core.models import Agent, AgentHttpTool
from hailhq.core.provider_config import provider_cipher
from hailhq.core.schemas import HttpToolIn, HttpToolOut
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "load_http_tools",
    "new_tool_secret",
    "replace_http_tools",
]


def new_tool_secret() -> str:
    return "hts_" + secrets.token_urlsafe(24)


async def load_http_tools(
    db: AsyncSession, agent_ids: list[UUID]
) -> dict[UUID, list[HttpToolOut]]:
    """Each agent's tools in saved order, as responses show them."""
    if not agent_ids:
        return {}
    rows = (
        (
            await db.execute(
                select(AgentHttpTool)
                .where(AgentHttpTool.agent_id.in_(agent_ids))
                .order_by(AgentHttpTool.agent_id, AgentHttpTool.position)
            )
        )
        .scalars()
        .all()
    )
    out: dict[UUID, list[HttpToolOut]] = {}
    for row in rows:
        out.setdefault(row.agent_id, []).append(
            HttpToolOut(
                name=row.name,
                description=row.description,
                url=row.url,
                parameters=row.parameters,
                header_names=list(row.header_names),
                timeout_seconds=row.timeout_seconds,
                wait_message=row.wait_message,
            )
        )
    return out


async def replace_http_tools(
    db: AsyncSession, agent: Agent, items: list[HttpToolIn]
) -> str | None:
    """Replace the agent's tools with ``items``. Returns the plaintext signing
    secret when this save created it (the agent's first tools), else None.

    A tool with ``headers`` None keeps the saved headers of the tool with the
    same name (none on a new tool). Raises ``SecretKeyMissing`` when tools are
    given and HAIL_PROVIDER_SECRET_KEY is unset.
    """
    old = {
        row.name: row
        for row in (
            await db.execute(
                select(AgentHttpTool).where(AgentHttpTool.agent_id == agent.id)
            )
        ).scalars()
    }
    created: str | None = None
    rows: list[AgentHttpTool] = []
    if items:
        cipher = provider_cipher()
        if agent.http_tool_secret_encrypted is None:
            created = new_tool_secret()
            agent.http_tool_secret_encrypted = cipher.encrypt(created)
        for position, item in enumerate(items):
            if item.headers is None:
                prev = old.get(item.name)
                headers_encrypted = prev.headers_encrypted if prev else None
                header_names = list(prev.header_names) if prev else []
            elif item.headers:
                headers_encrypted = cipher.encrypt(json.dumps(item.headers))
                header_names = list(item.headers)
            else:
                headers_encrypted, header_names = None, []
            rows.append(
                AgentHttpTool(
                    agent_id=agent.id,
                    position=position,
                    name=item.name,
                    description=item.description,
                    url=item.url,
                    parameters=item.parameters,
                    headers_encrypted=headers_encrypted,
                    header_names=header_names,
                    timeout_seconds=item.timeout_seconds,
                    wait_message=item.wait_message,
                )
            )
    await db.execute(delete(AgentHttpTool).where(AgentHttpTool.agent_id == agent.id))
    db.add_all(rows)
    await db.flush()
    return created
