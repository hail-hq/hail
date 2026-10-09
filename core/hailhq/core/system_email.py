"""Hail-to-tenant system mail, sent as the org's own forwarder address.

Rows queue through the same ``OutboundForwardWorker`` as forwards, marked
``metadata.system_kind`` instead of ``metadata.forwarded_from``: the worker
sends them, does not meter them, and the forward limiter does not count
them.

Fixed copy only. The recipient of a confirm mail may be a stranger, so no
tenant-supplied text (org name, user name, subject) ever reaches the
subject or body — only the forwarder address, the recipient address, the
link, and the constant text below.
"""

from __future__ import annotations

from html import escape
from urllib.parse import quote
from uuid import UUID

from hailhq.core.config import settings
from hailhq.core.hail_mail import org_prefix_from_id
from hailhq.core.models import Email
from hailhq.core.urls import join_url
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "SYSTEM_KIND_FORWARD_CONFIRM",
    "SYSTEM_KIND_FORWARD_STOPPED",
    "confirm_url",
    "enqueue_system_email",
    "forwarder_address",
    "render_forward_confirm",
    "render_forward_stopped",
]

# Same string as forward_targets.SYSTEM_KIND_FORWARD_CONFIRM: that module
# counts queued confirm mails by it for the daily cap.
SYSTEM_KIND_FORWARD_CONFIRM = "forward_confirm"
SYSTEM_KIND_FORWARD_STOPPED = "forward_stopped"


def forwarder_address(
    organization_id: UUID, local_prefix_org: str | None, base_domain: str
) -> str:
    """``forwarder+<org>@<base>``. Custom-domain rows carry no org prefix, so
    fall back to the id-derived one every hail-mail address of the org uses."""
    prefix = local_prefix_org or org_prefix_from_id(organization_id)
    return f"forwarder+{prefix}@{base_domain}"


def confirm_url(raw_token: str) -> str:
    base = join_url(settings.hail_api_url, "v1/forward-targets/confirm")
    return f"{base}?token={quote(raw_token, safe='')}"


def _console_domains_url() -> str | None:
    if not settings.hail_base_url:
        return None
    return join_url(settings.hail_base_url, "console/domains")


def render_forward_confirm(
    *, address: str, forwarder: str, url: str
) -> tuple[str, str, str]:
    """(subject, body_text, body_html) for the confirm link mail."""
    subject = "Confirm email forwarding from Hail"
    text = (
        f"A Hail workspace wants to forward its inbound email to {address}.\n"
        f"Forwarded mail will arrive from {forwarder}.\n"
        "\n"
        "To accept, open this link and press the button:\n"
        f"{url}\n"
        "\n"
        "If you did not expect this, ignore this email. Nothing is forwarded "
        "until you confirm. The link expires in 7 days.\n"
    )
    html = (
        "<p>A Hail workspace wants to forward its inbound email to "
        f"<b>{escape(address)}</b>.<br>"
        f"Forwarded mail will arrive from <code>{escape(forwarder)}</code>.</p>"
        f'<p><a href="{escape(url, quote=True)}">Confirm forwarding</a></p>'
        "<p>If you did not expect this, ignore this email. Nothing is "
        "forwarded until you confirm. The link expires in 7 days.</p>"
    )
    return subject, text, html


def render_forward_stopped(*, address: str) -> tuple[str, str, str]:
    """(subject, body_text, body_html) for the owner notice after a complaint."""
    subject = "Hail stopped forwarding to one address"
    console = _console_domains_url()
    where_text = f"\nManage forwarding: {console}\n" if console else ""
    where_html = (
        f'<p><a href="{escape(console, quote=True)}">Manage forwarding</a></p>'
        if console
        else ""
    )
    text = (
        f"A forwarded email to {address} was reported as spam by its mailbox "
        "provider.\n"
        "Hail stopped forwarding to that address to protect email delivery for "
        "every workspace.\n"
        "\n"
        "Your inbound mail is still stored in Hail. To forward to this address "
        "again, send a new confirm link from the console and have the mailbox "
        "owner accept it.\n" + where_text
    )
    html = (
        f"<p>A forwarded email to <b>{escape(address)}</b> was reported as spam "
        "by its mailbox provider.<br>Hail stopped forwarding to that address to "
        "protect email delivery for every workspace.</p>"
        "<p>Your inbound mail is still stored in Hail. To forward to this "
        "address again, send a new confirm link from the console and have the "
        "mailbox owner accept it.</p>" + where_html
    )
    return subject, text, html


async def enqueue_system_email(
    db: AsyncSession,
    *,
    organization_id: UUID,
    email_domain_id: UUID,
    from_address: str,
    to: str,
    subject: str,
    body_text: str,
    body_html: str,
    kind: str,
) -> Email:
    """Queue one system mail for the forward worker. Flushes, does not commit."""
    email = Email(
        organization_id=organization_id,
        email_domain_id=email_domain_id,
        from_address=from_address,
        to_addresses=[to],
        subject=subject,
        body_text=body_text,
        body_html=body_html,
        status="queued",
        provider="ses",
        direction="outbound",
        metadata_={
            "system_kind": kind,
            "forward_headers": {"Auto-Submitted": "auto-generated"},
        },
    )
    db.add(email)
    await db.flush()
    return email
