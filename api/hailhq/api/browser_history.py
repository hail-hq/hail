"""Customer-visible onboarding history. Runtime prompts never leave the API."""

from datetime import datetime

from fastapi import HTTPException
from hailhq.core.email_mime import readable_email_body
from hailhq.core.models import Agent, Call, Email, EmailAttachment, EmailDomain, Sms
from sqlalchemy import Text, and_, cast, literal, or_, select


async def browser_history(db, organization_id, agent_id, before=None, before_key=None):
    agent = await db.get(Agent, agent_id)
    if agent is None or agent.organization_id != organization_id:
        raise HTTPException(404, "Agent not found")
    calls = select(Call.id).where(
        Call.organization_id == organization_id,
        Call.agent_id == agent_id,
        Call.transport == "browser",
    )
    domains = select(EmailDomain.id).where(
        EmailDomain.organization_id == organization_id, EmailDomain.kind == "hail_mail"
    )
    sources = [
        ("email", Email, Email.created_at, Email.email_domain_id.in_(domains)),
        (
            "sms",
            Sms,
            Sms.created_at,
            Sms.metadata_["call_id"].astext.in_(
                select(cast(Call.id, Text)).where(Call.id.in_(calls))
            ),
        ),
        (
            "voice",
            Call,
            Call.requested_at,
            and_(
                Call.id.in_(calls),
                Call.status.in_(["completed", "failed", "canceled"]),
                Call.transcript.is_not(None),
            ),
        ),
    ]
    result = []
    for channel, model, timestamp, scope in sources:
        key = literal(channel + ":") + cast(model.id, Text)
        query = select(model).where(model.organization_id == organization_id, scope)
        if before is not None:
            query = query.where(
                or_(timestamp < before, and_(timestamp == before, key < before_key))
            )
        rows = (
            (
                await db.execute(
                    query.order_by(timestamp.desc(), model.id.desc()).limit(51)
                )
            )
            .scalars()
            .all()
        )
        files = {}
        if channel == "email" and rows:
            attachments = (
                await db.execute(
                    select(
                        EmailAttachment.email_id,
                        EmailAttachment.filename,
                        EmailAttachment.size_bytes,
                    ).where(EmailAttachment.email_id.in_([row.id for row in rows]))
                )
            ).all()
            for email_id, name, size in attachments:
                files.setdefault(email_id, []).append({"name": name, "bytes": size})
        for row in rows:
            when = row.requested_at if channel == "voice" else row.created_at
            item = {
                "id": f"{channel}:{row.id}",
                "at": when.isoformat(),
                "channel": channel,
                "status": row.status,
            }
            if channel == "voice":
                turns = []
                for entry in (row.transcript or {}).get("items", []):
                    if entry.get("role") not in ("user", "assistant"):
                        continue
                    content = entry.get("content", [])
                    if isinstance(content, str):
                        content = [content]
                    body = " ".join(
                        part for part in content if isinstance(part, str)
                    ).strip()
                    if body:
                        turns.append(
                            {
                                "speaker": (
                                    "you" if entry["role"] == "user" else "agent"
                                ),
                                "text": body,
                            }
                        )
                item.update(
                    callId=str(row.id), direction="inbound", body="", turns=turns
                )
            elif channel == "email":
                item.update(
                    invocationId=row.metadata_.get("tool_invocation_id"),
                    direction=row.direction,
                    sender=row.from_address,
                    recipients=row.to_addresses,
                    subject=row.subject,
                    body=readable_email_body(row.body_text, row.body_html),
                    attachments=files.get(row.id, []),
                )
            else:
                item.update(
                    invocationId=row.metadata_.get("tool_invocation_id"),
                    direction=row.direction,
                    sender=row.from_e164,
                    recipients=[row.to_e164],
                    body=row.body,
                    callId=row.metadata_.get("call_id"),
                )
            result.append(item)
    result.sort(
        key=lambda item: (datetime.fromisoformat(item["at"]), item["id"]), reverse=True
    )
    page = result[:50]
    cursor = f'{page[-1]["at"]}|{page[-1]["id"]}' if len(result) > 50 else None
    return {"items": list(reversed(page)), "nextCursor": cursor}
