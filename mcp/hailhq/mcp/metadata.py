"""Server metadata sent in the MCP ``initialize`` and ``server/discover`` results.

The remote app (``server.py``) and the stdio entry point (``stdio.py``) both
use it, so every client sees the same title, description, instructions and
website.
"""

from __future__ import annotations

import json
import logging
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from hailhq.core.config import settings
from hailhq.core.urls import join_url, url_variants

TITLE = "Hail"

DESCRIPTION = (
    "Phone numbers, voice calls, text messages and email for AI agents. "
    "Place and answer calls, send and receive texts and mail, and buy numbers."
)

# Clients put this text into the agent's context, and some cut it at 2 KB.
# Keep it short, with the rules that prevent harm or spend first.
INSTRUCTIONS = """\
Hail gives an AI agent phone numbers, voice calls, text messages and email.

Rules:
- place_call, send_sms and send_email need recipient_consent=true. Set it only \
when the user confirms the recipient agreed to be contacted. Marketing also \
needs consent_source.
- acquire_number charges the workspace at once. Call quote_numbers, show the \
user the price, and buy only after they agree.
- release_number and delete_agent cannot be undone. Ask the user first.
- submit_verification files with the carrier and cannot be undone. A number \
registration holds the setup fee and first month; US local texting has carrier fees. \
Show the user the details and file only after they agree.
- To retry a send or a purchase, pass the idempotency_key it returned, so the \
retry does not send or buy twice.

Start here:
1. whoami shows the workspace.
2. list_numbers and list_email_domains show what it can send from. To get a \
number, use quote_numbers, then acquire_number. Some numbers need \
get_verification_requirements and submit_verification first.
3. Send with place_call, send_sms or send_email. Follow progress with \
get_call, get_sms, get_email or get_events.
4. To answer incoming calls, texts or mail, create_agent, then route_number \
or route_email_domain.

Lists return next_cursor: pass it as cursor to get the next page. A failure \
returns {"error": ...} with the reason. Fix the input from that reason \
instead of guessing.
"""


def website_url() -> str | None:
    """Public page about Hail's MCP server, or None without a site URL."""
    if not settings.hail_base_url:
        return None
    return join_url(settings.hail_base_url, "mcp")


def server_metadata() -> dict[str, str | None]:
    """The MCPServer keyword arguments both entry points send."""
    return {
        "title": TITLE,
        "description": DESCRIPTION,
        "version": server_version(),
        "instructions": INSTRUCTIONS,
        "website_url": website_url(),
    }


def server_version() -> str:
    """This service's package version. The SDK sends an empty one otherwise."""
    try:
        return version("hailhq-mcp")
    except PackageNotFoundError:
        return "0.0.0"


def registry_manifest_for(resource_url: str) -> bytes | None:
    """Read the bundled registry manifest if it describes this MCP endpoint."""
    path = Path(__file__).with_name("server.json")
    try:
        if not path.is_file():
            # Editable installs use the registry file in the checkout.
            path = Path(__file__).resolve().parents[2] / "server.json"
        content = path.read_bytes()
        resource_urls = url_variants(resource_url)
        matches = any(
            remote["url"] in resource_urls for remote in json.loads(content)["remotes"]
        )
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        # A public probe can retry, so keep per-request failures at debug level.
        logging.getLogger(__name__).debug(
            "Could not load the MCP registry manifest", exc_info=True
        )
        return None
    return content if matches else None
