"""The agent-tool registry. New modality = add one module + one line here."""

from __future__ import annotations

from hailhq.core.agent_tools import (
    end_call,
    hail_help,
    list_contacts,
    read_email,
    send_dtmf,
    send_email,
    send_sms,
    thread_history,
    transfer_call,
    wait_for_other_party,
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


def call_tools() -> tuple[ToolSpec, ...]:
    """Every built-in tool a call can have: the tools a `tools` list may
    name, plus the settings guide and the hold tool, which no list names."""
    return (*all_tools(), hail_help.SPEC, wait_for_other_party.SPEC)


__all__ = ["all_tools", "call_tools"]
