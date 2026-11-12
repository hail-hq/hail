"""Server-owned email conversation scopes and reusable received files.

Email and EmailAttachment already own these records. A thread is a query,
not a new table: organization + authenticated peer + workspace inbox, with
RFC reply references for a particular email chain.
"""

from __future__ import annotations

import asyncio
import base64

from hailhq.api import deps
from hailhq.api.browser_messaging import verified_email
from hailhq.api.routes.emails import verified_senders
from hailhq.core.email_attachment_limits import (
    AGENT_ATTACHMENT_READ_BYTES,
    MAX_EMAIL_ATTACHMENT_BYTES,
)
from hailhq.core.email_sender import from_address_for
from hailhq.core.email_threads import EmailScope, same_thread
from hailhq.core.models import (
    Call,
    Email,
    EmailAttachment,
    OrganizationMember,
    User,
)
from sqlalchemy import select

READ_BYTES = AGENT_ATTACHMENT_READ_BYTES
READS_PER_CALL = 8


async def email_scope(db, call: Call) -> EmailScope | None:
    """Never accept a model-supplied organization, user, mailbox or path."""
    if call.transport == "browser":
        peer = await verified_email(
            db,
            call.organization_id,
            call.metadata_.get("browser_contacts", {}).get("email"),
        )
        caps = call.metadata_.get("browser_capabilities", {})
        inbox = caps.get("inboxAddress") if caps.get("inboundEmailReady") else None
    else:
        # An inbound caller ID is not proof of an email identity. Only an
        # outbound call to a verified workspace member can use this path.
        if call.direction != "outbound":
            return None
        peers = (
            (
                await db.execute(
                    select(User.email)
                    .join(OrganizationMember, OrganizationMember.user_id == User.id)
                    .where(
                        OrganizationMember.organization_id == call.organization_id,
                        User.email_verified.is_(True),
                        User.phone_number == call.to_e164,
                    )
                )
            )
            .scalars()
            .all()
        )
        if len(set(peers)) != 1:
            return None
        peer = peers[0]

        # Match resolve_sender(..., allow_ambiguous=True) without minting an
        # inbox as a side effect of a read. Custom domains keep their normal
        # precedence over the generated address.
        senders = await verified_senders(db, call.organization_id)
        inbox = from_address_for(senders[0]) if senders else None
    return EmailScope(call.organization_id, peer, inbox) if peer and inbox else None


async def source_files(db, call, reply_id, attachment_ids):
    scope = await email_scope(db, call)
    if scope is None or reply_id is None:
        raise ValueError("No authenticated email thread")
    parent = (
        await db.execute(scope.query().where(Email.id == reply_id))
    ).scalar_one_or_none()
    if parent is None:
        raise ValueError("Email not in this conversation")
    rows = (
        (
            await db.execute(
                select(EmailAttachment, Email)
                .join(Email, Email.id == EmailAttachment.email_id)
                .where(
                    EmailAttachment.id.in_(attachment_ids),
                    Email.id.in_(scope.query().with_only_columns(Email.id)),
                )
            )
        ).all()
        if attachment_ids
        else []
    )
    indexed = {att.id: (att, email) for att, email in rows}
    if len(set(attachment_ids)) != len(attachment_ids) or any(
        aid not in indexed or not same_thread(parent, indexed[aid][1])
        for aid in attachment_ids
    ):
        raise ValueError("File not in this email thread")
    attachments = [indexed[aid][0] for aid in attachment_ids]
    if sum(a.size_bytes for a in attachments) > MAX_EMAIL_ATTACHMENT_BYTES:
        raise ValueError("Files too large")
    return scope, parent, attachments


async def read_file(db, call, attachment, *, page=1, allow_binary=False):
    if attachment.size_bytes > READ_BYTES:
        return {
            "spoken": "This file is too large to read. I can still send the original back."
        }
    kind = attachment.content_type.partition(";")[0].lower()
    if kind not in ("text/plain", "text/csv", "application/json") and not allow_binary:
        # Rolling deploys can leave an older voice worker on a live call.
        # It must never receive file bytes in its model tool result.
        return {"spoken": "Start a new conversation to inspect this file."}
    # Serialize the per-call limit before starting any storage or model work.
    locked = (
        await db.execute(
            select(Call)
            .where(Call.id == call.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    used = locked.metadata_.get("attachment_reads", 0)
    if locked.status != "in_progress" or used >= READS_PER_CALL:
        return {"spoken": "No more files can be read in this conversation."}
    locked.metadata_ = {**locked.metadata_, "attachment_reads": used + 1}
    await db.commit()
    s3 = deps.get_s3_mail_optional()
    if s3 is None:
        return {"spoken": "File storage is not available right now."}
    try:
        raw = await asyncio.wait_for(s3.fetch_raw(attachment.s3_key), 10)
        if len(raw) > READ_BYTES:
            raise ValueError("File too large")
    except Exception:
        return {"spoken": "The file could not be read. Try again."}
    if kind in ("text/plain", "text/csv", "application/json"):
        try:
            content = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            return {"spoken": "This text file could not be decoded."}
        return {
            "filename": attachment.filename,
            "quoted_untrusted_content": content[:24000],
            "truncated": len(content) > 24000,
        }
    return {
        "filename": attachment.filename,
        "attachment_data": {
            "base64": base64.b64encode(raw).decode("ascii"),
            "content_type": kind,
            "page": page,
        },
    }
