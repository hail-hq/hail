"""The agent-tool registry. New modality = add one module + one line here."""

from __future__ import annotations

from hailhq.core.agent_tools import (
    email_caller,
    end_call,
    list_contacts,
    read_email,
    send_dtmf,
    send_email,
    send_sms,
    thread_history,
    transfer_call,
)
from hailhq.core.agent_tools.spec import ToolSpec


def all_tools() -> tuple[ToolSpec, ...]:
    return (
        end_call.SPEC,
        send_dtmf.SPEC,
        transfer_call.SPEC,
        list_contacts.SPEC,
        send_sms.SPEC,
        send_email.SPEC,
        thread_history.SPEC,
        read_email.SPEC,
    )


def nameable_tools() -> frozenset[str]:
    """Names a ``tools`` list may hold. ``email_caller`` is not in
    ``all_tools()`` (a null list must not turn it on), but a list may name it
    to keep it, or leave it out to drop it for one call."""
    return frozenset(t.name for t in all_tools()) | {email_caller.SPEC.name}


__all__ = ["all_tools", "nameable_tools"]
