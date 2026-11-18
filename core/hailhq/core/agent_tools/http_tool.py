"""Customer HTTP tools: the agent calls the customer's API mid-conversation.

Each saved tool (``agent_http_tools``) becomes one ToolSpec bound to the
conversation. Hail POSTs the model's arguments as JSON, signed like a
webhook (``X-Hail-Signature: t=<unix>,v1=<hex>`` over ``f"{t}.{body}"``,
``hailhq.core.webhooks.sign_payload``) with the agent's tool secret, and
gives the answer back to the model as text.

SSRF: the host must resolve to public addresses only. The check runs inside
the connection's resolver, so the address Hail checks is the address it
connects to (no DNS-rebinding window). An IP-literal host skips the resolver
in aiohttp, so it is checked first. Redirects are not followed.

``execute`` never raises: a timeout, refusal, or error comes back as one
short sentence for the model, so a broken customer API never ends a call.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

import aiohttp
from aiohttp.abc import AbstractResolver, ResolveResult
from aiohttp.helpers import is_ip_address
from aiohttp.resolver import DefaultResolver
from cryptography.fernet import InvalidToken
from hailhq.core.agent_tools.spec import ToolContext, ToolSpec
from hailhq.core.db import session_scope
from hailhq.core.models import Agent, AgentHttpTool
from hailhq.core.provider_config import provider_cipher
from hailhq.core.secret_cipher import SecretKeyMissing
from hailhq.core.url_guard import ip_is_public
from hailhq.core.webhooks import sign_payload
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from yarl import URL

logger = logging.getLogger("hailhq.core.agent_tools")

# Bytes read from the customer's answer, and characters the model gets.
READ_CAP_BYTES = 64 * 1024
MODEL_CAP_CHARS = 4000
ERROR_BODY_CHARS = 300
USER_AGENT = "Hail-Agent-Tool/1"

BLOCKED = "That tool's address is not allowed, so it was not called."
UNREACHABLE = "That tool could not be reached."
NO_CONTENT = "Done. The tool returned no content."


@dataclass(frozen=True)
class Conversation:
    """What the request says it is about. ``id`` is the call, the inbound
    text, or the inbound email the agent is answering."""

    channel: Literal["call", "sms", "email"]
    id: uuid.UUID
    agent_id: uuid.UUID
    organization_id: uuid.UUID


@dataclass(frozen=True)
class HttpTool:
    name: str
    description: str
    url: str
    parameters: dict[str, Any]
    headers: dict[str, str]
    timeout_seconds: int
    wait_message: str | None
    secret: str


class BlockedAddressError(OSError):
    """The host resolves to a private, loopback, or link-local address."""


class PublicOnlyResolver(AbstractResolver):
    """Resolves like aiohttp's default, then refuses a host when any of its
    addresses is not public."""

    def __init__(self) -> None:
        self._inner = DefaultResolver()

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list[ResolveResult]:
        infos = await self._inner.resolve(host, port, family)
        for info in infos:
            if not ip_is_public(info["host"]):
                raise BlockedAddressError(
                    f"{host} resolves to non-public address {info['host']}"
                )
        return infos

    async def close(self) -> None:
        await self._inner.close()


def _ip_literal_blocked(url: str) -> bool:
    """True for a host aiohttp treats as an IP address (it then skips the
    resolver) that is not public. Short and decimal IPv4 forms (``127.1``,
    ``2130706433``) count: the OS reads them as addresses too."""
    host = URL(url).raw_host or ""
    if not is_ip_address(host):
        return False  # a name: PublicOnlyResolver checks it at connect time
    try:
        infos = socket.getaddrinfo(host, None, flags=socket.AI_NUMERICHOST)
    except OSError:
        return True
    return any(not ip_is_public(info[4][0]) for info in infos)


def _clip(text: str, cap: int) -> str:
    return text if len(text) <= cap else text[:cap] + " [truncated]"


def _answer_text(raw: bytes, content_type: str) -> str:
    text = raw.decode("utf-8", errors="replace").strip()
    if "json" in content_type.lower():
        try:
            text = json.dumps(
                json.loads(text), separators=(",", ":"), ensure_ascii=False
            )
        except ValueError:
            pass
    return text


def request_body(
    tool: HttpTool, conv: Conversation, args: dict[str, Any]
) -> tuple[str, bytes]:
    """``(request id, body)``. The id is new for every request."""
    request_id = str(uuid.uuid4())
    payload = {
        "id": request_id,
        "type": "tool.call",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "tool": tool.name,
        "arguments": args,
        "agent_id": str(conv.agent_id),
        "organization_id": str(conv.organization_id),
        "channel": conv.channel,
        "call_id": str(conv.id) if conv.channel == "call" else None,
        "sms_id": str(conv.id) if conv.channel == "sms" else None,
        "email_id": str(conv.id) if conv.channel == "email" else None,
    }
    return request_id, json.dumps(payload, separators=(",", ":")).encode()


async def call_http_tool(
    tool: HttpTool, conv: Conversation, args: dict[str, Any]
) -> str:
    """POST one signed request and return text for the model. Never raises."""
    try:
        blocked = _ip_literal_blocked(tool.url)
    except ValueError:
        logger.info("http tool %s has a url that cannot be parsed", tool.name)
        return UNREACHABLE
    if blocked:
        logger.warning("http tool %s refused: non-public address", tool.name)
        return BLOCKED
    request_id, body = request_body(tool, conv, args)
    headers = {
        **tool.headers,
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        "X-Hail-Signature": sign_payload(body, tool.secret),
        "X-Hail-Tool": tool.name,
        "X-Hail-Request": request_id,
    }
    started = time.monotonic()
    status: int | None = None
    try:
        async with asyncio.timeout(tool.timeout_seconds):
            connector = aiohttp.TCPConnector(resolver=PublicOnlyResolver())
            async with (
                aiohttp.ClientSession(connector=connector) as session,
                session.post(
                    tool.url, data=body, headers=headers, allow_redirects=False
                ) as resp,
            ):
                status = resp.status
                content_type = resp.headers.get("Content-Type", "")
                chunks: list[bytes] = []
                size = 0
                async for chunk in resp.content.iter_chunked(8192):
                    chunks.append(chunk)
                    size += len(chunk)
                    if size >= READ_CAP_BYTES:
                        break
        raw = b"".join(chunks)[:READ_CAP_BYTES]
    except TimeoutError:
        logger.info("http tool %s timed out after %ss", tool.name, tool.timeout_seconds)
        return f"That tool did not answer within {tool.timeout_seconds} seconds."
    except aiohttp.ClientConnectorError as exc:
        if isinstance(exc.os_error, BlockedAddressError):
            logger.warning("http tool %s refused: %s", tool.name, exc.os_error)
            return BLOCKED
        logger.info("http tool %s unreachable: %s", tool.name, type(exc).__name__)
        return UNREACHABLE
    except BlockedAddressError as exc:
        logger.warning("http tool %s refused: %s", tool.name, exc)
        return BLOCKED
    except Exception as exc:
        logger.info("http tool %s failed: %s", tool.name, type(exc).__name__)
        return UNREACHABLE
    logger.info(
        "http tool %s answered %s in %.0f ms",
        tool.name,
        status,
        (time.monotonic() - started) * 1000,
    )
    text = _answer_text(raw, content_type)
    if not 200 <= status < 300:
        detail = f": {_clip(text, ERROR_BODY_CHARS)}" if text else "."
        return f"That tool failed with HTTP {status}{detail}"
    if not text:
        return NO_CONTENT
    return _clip(text, MODEL_CAP_CHARS)


async def _always(_org: uuid.UUID, _session: AsyncSession) -> bool:
    return True


def tool_spec(tool: HttpTool, conv: Conversation) -> ToolSpec:
    async def execute(_ctx: ToolContext, args: dict[str, Any]) -> str:
        return await call_http_tool(tool, conv, args)

    return ToolSpec(
        name=tool.name,
        description=tool.description,
        parameters=tool.parameters,
        risk_tier="customer_api",
        is_available=_always,
        execute=execute,
        wait_message=tool.wait_message,
    )


async def load_runtime_http_tools(
    session: AsyncSession, *, agent_id: uuid.UUID, organization_id: uuid.UUID
) -> list[HttpTool]:
    """The agent's saved tools with headers and secret decrypted. An agent of
    another organization has none. Undecryptable rows are left out."""
    rows = (
        await session.execute(
            select(AgentHttpTool, Agent.http_tool_secret_encrypted)
            .join(Agent, Agent.id == AgentHttpTool.agent_id)
            .where(
                AgentHttpTool.agent_id == agent_id,
                Agent.organization_id == organization_id,
            )
            .order_by(AgentHttpTool.position)
        )
    ).all()
    if not rows:
        return []
    try:
        cipher = provider_cipher()
        secret_enc = rows[0][1]
        if secret_enc is None:
            logger.warning("agent %s has http tools but no secret", agent_id)
            return []
        secret = cipher.decrypt(secret_enc)
    except (SecretKeyMissing, InvalidToken):
        logger.warning("agent %s http tool secret cannot be read", agent_id)
        return []
    tools: list[HttpTool] = []
    for row, _ in rows:
        try:
            headers = (
                json.loads(cipher.decrypt(row.headers_encrypted))
                if row.headers_encrypted
                else {}
            )
        except (InvalidToken, ValueError):
            logger.warning(
                "agent %s http tool %s headers unreadable", agent_id, row.name
            )
            continue
        tools.append(
            HttpTool(
                name=row.name,
                description=row.description,
                url=row.url,
                parameters=row.parameters,
                headers=headers,
                timeout_seconds=row.timeout_seconds,
                wait_message=row.wait_message,
                secret=secret,
            )
        )
    return tools


async def http_tool_specs(session: AsyncSession, conv: Conversation) -> list[ToolSpec]:
    tools = await load_runtime_http_tools(
        session, agent_id=conv.agent_id, organization_id=conv.organization_id
    )
    return [tool_spec(t, conv) for t in tools]


async def load_http_tool_specs(conv: Conversation) -> list[ToolSpec]:
    """``http_tool_specs`` on its own short session. Never raises: a failure
    loses only the HTTP tools, never the conversation's other tools."""
    try:
        async with session_scope() as session:
            return await http_tool_specs(session, conv)
    except Exception:
        logger.exception("%s %s: http tools failed to load", conv.channel, conv.id)
        return []


__all__ = [
    "BLOCKED",
    "UNREACHABLE",
    "Conversation",
    "HttpTool",
    "PublicOnlyResolver",
    "call_http_tool",
    "http_tool_specs",
    "load_http_tool_specs",
    "load_runtime_http_tools",
    "request_body",
    "tool_spec",
]
