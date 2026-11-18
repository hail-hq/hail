"""SMS compliance keywords, shared by inbound ingest and the US texting form.

The keyword must be the whole message. REVOKE and OPT OUT follow the FCC rule
(2025) that any reasonable opt-out word revokes consent.
"""

from __future__ import annotations

import re

STOP_KEYWORDS = frozenset(
    {
        "STOP",
        "STOPALL",
        "UNSUBSCRIBE",
        "CANCEL",
        "END",
        "QUIT",
        "REVOKE",
        "OPTOUT",
        "OPT OUT",
        "OPT-OUT",
    }
)
START_KEYWORDS = frozenset({"START", "YES", "UNSTOP"})
HELP_KEYWORDS = frozenset({"HELP", "INFO"})

_ENDS = re.compile(r"^[\W_]+|[\W_]+$")


def exact_keyword(body: str) -> str:
    """Upper case, outer spaces trimmed, inner spaces squeezed."""
    return " ".join(body.upper().split())


def loose_keyword(body: str) -> str:
    """Like exact_keyword, and punctuation or emoji at the ends dropped, so
    "Stop." and "STOP!" count. Used for STOP only: widening START or HELP
    would lift opt-outs on "Yes." or hide "Help?" from the text agent."""
    return exact_keyword(_ENDS.sub("", body))


def is_stop(body: str) -> bool:
    return loose_keyword(body) in STOP_KEYWORDS
