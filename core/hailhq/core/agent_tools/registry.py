"""The agent-tool registry. New modality = add one module + one line here."""

from __future__ import annotations

from hailhq.core.agent_tools import (
    end_call,
    list_contacts,
    send_dtmf,
    send_email,
    send_sms,
    thread_history,
)
from hailhq.core.agent_tools.spec import ToolSpec


def all_tools() -> tuple[ToolSpec, ...]:
    return (
        end_call.SPEC,
        send_dtmf.SPEC,
        list_contacts.SPEC,
        send_sms.SPEC,
        send_email.SPEC,
        thread_history.SPEC,
    )


__all__ = ["all_tools"]
