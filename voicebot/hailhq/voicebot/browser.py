"""Browser sessions share the voice pipeline, without SIP or sending tools.

LiveKit interfaces checked against installed Agents 1.8.8 and:
https://docs.livekit.io/agents/logic/sessions/
https://docs.livekit.io/frontends/reference/tokens-grants/
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from uuid import UUID

from hailhq.core.browser_voice import (
    SETUP_SECONDS,
    finish_browser_call,
    reserve_browser_preview,
)
from hailhq.core.db import session_scope
from hailhq.core.languages import SUPPORTED_LANGUAGES
from hailhq.core.models import Call
from hailhq.core.prompts import BROWSER_VOICE_PREAMBLE
from hailhq.core.schemas import TERMINAL_CALL_STATUSES
from hailhq.voicebot.pipeline import build_session, resolve_org_configs
from livekit.agents import Agent, function_tool

logger = logging.getLogger(__name__)


def public_transcript(history: dict) -> dict:
    """Only spoken user/assistant text. Exclude system messages and tool data."""
    return {
        "items": [
            {
                "role": item["role"],
                "content": [
                    part for part in item.get("content", []) if isinstance(part, str)
                ],
            }
            for item in history.get("items", [])
            if item.get("role") in ("user", "assistant")
        ]
    }


def browser_instructions(
    customer_instructions: str, language: str | None = None
) -> str:
    instruction = (
        f"Speak in {SUPPORTED_LANGUAGES[language].name} for this conversation."
        if language in SUPPORTED_LANGUAGES
        else ""
    )
    return (
        BROWSER_VOICE_PREAMBLE + "\n\n" + customer_instructions + "\n\n" + instruction
    )


def browser_language(config: dict, providers: dict) -> str | None:
    tts = providers.get("tts")
    language = config.get("language") or (tts.params.get("language") if tts else None)
    return language if language in SUPPORTED_LANGUAGES else None


def deadline_seconds(call) -> int:
    return max(1, min(600, call.max_duration_seconds))


async def run_browser_session(ctx, call_id: UUID) -> None:
    """A persisted Call is the authority; dispatch contains only its id."""
    session = None
    finish_lock = asyncio.Lock()
    finished = False
    deadline = None
    setup_deadline = None
    finalization = None
    media_started_at = None

    async def finish(*, failed=False):
        nonlocal finished
        async with finish_lock:
            if finished:
                return
            finished = True
            media_ended_at = datetime.now(timezone.utc)
            # Cut our local media path even when the remote room API fails.
            # Every cleanup is independent; none can bypass worker shutdown.
            try:
                try:
                    await asyncio.wait_for(ctx.room.disconnect(), timeout=2)
                except Exception:
                    logger.exception(
                        "browser media disconnect failed call_id=%s", call_id
                    )
                if session is not None:
                    try:
                        await asyncio.wait_for(session.aclose(), timeout=3)
                    except Exception:
                        logger.exception(
                            "browser model cleanup failed call_id=%s", call_id
                        )
                for attempt in range(2):
                    try:
                        await asyncio.wait_for(ctx.delete_room(), timeout=2)
                        break
                    except Exception:
                        logger.warning(
                            "browser room cleanup attempt=%s call_id=%s",
                            attempt + 1,
                            call_id,
                            exc_info=True,
                        )
                async with session_scope() as db:
                    call = await db.get(Call, call_id)
                    if call is not None and session is not None:
                        call.transcript = public_transcript(session.history.to_dict())
                    await finish_browser_call(
                        db,
                        call_id,
                        failed=failed,
                        ended_at=media_ended_at,
                        started_at=media_started_at,
                    )
                    await db.commit()
            finally:
                ctx.shutdown(reason="browser_ended")

    def begin_finish(*, failed=False):
        nonlocal finalization
        if finalization is None:
            finalization = asyncio.create_task(finish(failed=failed))
        return finalization

    async def wait_finish(*, failed=False):
        # A timer or the entrypoint may be cancelled by room shutdown. The
        # dedicated finalizer must finish settlement independently of either.
        await asyncio.shield(begin_finish(failed=failed))

    def schedule_finish(*, failed=False):
        begin_finish(failed=failed)

    async def shutdown():
        if deadline is not None:
            deadline.cancel()
        if setup_deadline is not None:
            setup_deadline.cancel()
        await wait_finish()

    ctx.add_shutdown_callback(shutdown)
    try:
        async with session_scope() as db:
            call = await db.get(Call, call_id)
            if (
                call is None
                or call.transport != "browser"
                or call.status in TERMINAL_CALL_STATUSES
            ):
                ctx.shutdown(reason="browser_not_active")
                return
            cfg = call.voice_config or {}
            prompt = call.initial_prompt or "Help the caller with their question."
            org_id = call.organization_id
            seconds = deadline_seconds(call)

        async def expire_setup():
            await asyncio.sleep(SETUP_SECONDS)
            await wait_finish(failed=True)

        setup_deadline = asyncio.create_task(expire_setup())
        await ctx.connect()
        participant = await asyncio.wait_for(ctx.wait_for_participant(), timeout=30)
        identity = participant.identity

        @ctx.room.on("participant_disconnected")
        def disconnected(p):
            if p.identity == identity:
                schedule_finish()

        # Load the same configured models and voice used by real calls.
        configs = await resolve_org_configs(org_id)
        if finished:
            return
        language = browser_language(cfg, configs)
        session = build_session(
            None,
            ctx.proc.userdata["vad"],
            org_cfgs=configs,
            voice_id_override=cfg.get("voice_id"),
            language=language,
        )

        async def preview(channel: str, recipient: str, body: str):
            action = {
                "channel": channel,
                "recipient": recipient,
                "body": body,
                "preview": True,
            }
            async with session_scope() as db:
                if not await reserve_browser_preview(db, call_id, action):
                    return "This preview limit is reached or the conversation has ended. Nothing was sent."
                await db.commit()
            await ctx.room.local_participant.publish_data(
                json.dumps(action), reliable=True, topic="hail.preview"
            )
            return (
                "Preview prepared. Nothing was sent. Tell the user this is a preview."
            )

        @function_tool
        async def preview_email(recipient: str, body: str) -> str:
            """Prepare an email preview. This never sends an email."""
            return await preview("email", recipient[:254], body[:2000])

        @function_tool
        async def preview_sms(recipient: str, body: str) -> str:
            """Prepare a text message preview. This never sends a text."""
            return await preview("sms", recipient[:40], body[:1000])

        @session.on("close")
        def closed(event):
            if not finished:
                schedule_finish(failed=getattr(event, "reason", None) == "error")

        # Starting the session and model construction are also bounded. The
        # live deadline is armed before greeting, and never controlled by JS.
        await asyncio.wait_for(
            session.start(
                agent=Agent(
                    instructions=browser_instructions(prompt, language),
                    tools=[preview_email, preview_sms],
                ),
                room=ctx.room,
                record=False,
            ),
            timeout=15,
        )
        if finished:
            await session.aclose()
            return
        media_started_at = datetime.now(timezone.utc)

        async def expire():
            await asyncio.sleep(seconds)
            await wait_finish()

        # Arm the live limit before waiting on any readiness DB transaction.
        deadline = asyncio.create_task(expire())
        setup_deadline.cancel()
        async with session_scope() as db:
            call = await db.get(Call, call_id, with_for_update=True)
            if call.status in TERMINAL_CALL_STATUSES:
                await session.aclose()
                ctx.shutdown(reason="browser_already_stopped")
                return
            call.status = "in_progress"
            call.started_at = call.answered_at = media_started_at
            await db.commit()

        if finished:
            return
        await ctx.room.local_participant.set_attributes({"hail.browser_ready": "true"})
        session.generate_reply(
            instructions="Briefly say you are an AI assistant, greet the person as this agent, then ask how you can help. Use the selected conversation language."
        )
    except Exception:
        logger.exception("browser session failed call_id=%s", call_id)
        await wait_finish(failed=True)
