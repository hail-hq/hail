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
from hailhq.core.url_guard import UnsafeUrlError, connect_target
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from yarl import URL

__all__ = [
    "HttpToolHeadersNeeded",
    "load_http_tools",
    "new_tool_secret",
    "replace_http_tools",
]


class HttpToolHeadersNeeded(ValueError):
    """A tool moved to another origin with ``headers`` null: the saved
    headers are not sent to a new host, so they must be given again."""

    def __init__(self, index: int, name: str) -> None:
        super().__init__(
            f"{name}: the url points to a new host, so the saved headers are "
            "not kept. Send headers again ({} for none)."
        )
        self.index = index


def _origin(
    url: str,
) -> tuple[str, str, int, str, str | None, int | None] | None:
    """Scheme, host and port with the canonical ASCII (IDNA 2008) host and
    the default port filled in, so ``faß.example`` and
    ``xn--fa-hia.example:443`` are one origin. The scheme, host and port as
    yarl reads them are part of it too: aiohttp connects with yarl, whose parser
    differs from httpx's, so two urls are one origin only when both parsers
    agree. None for a url that cannot be parsed."""
    try:
        parsed, host, port = connect_target(url)
        connect = URL(url)
    except (UnsafeUrlError, ValueError):
        return None
    return parsed.scheme, host, port, connect.scheme, connect.raw_host, connect.port


def _same_origin(saved_url: str, new_url: str) -> bool:
    saved = _origin(saved_url)
    return saved is not None and saved == _origin(new_url)


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
    same name (none on a new tool), only while its URL keeps the same origin
    (scheme, host, port); otherwise ``HttpToolHeadersNeeded``. Raises
    ``SecretKeyMissing`` when tools are given and HAIL_PROVIDER_SECRET_KEY is
    unset. Nothing is written before either error.
    """
    if items:
        # Lock the agent row (a no-op if this transaction already updated
        # it), then read the secret and the saved tools: two saves at once
        # must not each make (and return) a different secret, or carry
        # headers over from a list the other one replaced.
        await db.refresh(agent, ["http_tool_secret_encrypted"], with_for_update=True)
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
                if prev is None or prev.headers_encrypted is None:
                    headers_encrypted, header_names = None, []
                elif not _same_origin(prev.url, item.url):
                    # Keeping them would send the saved secrets to whoever
                    # runs the new host.
                    raise HttpToolHeadersNeeded(position, item.name)
                else:
                    headers_encrypted = prev.headers_encrypted
                    header_names = list(prev.header_names)
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
