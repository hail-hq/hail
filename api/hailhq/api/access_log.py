"""Keep credentials in query strings out of the uvicorn access log.

An upload link (``PUT /v1/uploads/{id}?token=...``) and an unsubscribe link
(``/unsubscribe?token=...``) carry their only credential in the query. The
access log line prints the request path with its query, so the ``token``
value is replaced before the line is written. Telemetry drops queries on
its own (``hailhq.core.telemetry``).
"""

from __future__ import annotations

import logging
import re

_TOKEN = re.compile(r"([?&])token=[^&\s]*")


class RedactTokenFilter(logging.Filter):
    """Replace ``token=<value>`` in the request path of an access log line."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        # uvicorn: (client_addr, method, full_path, http_version, status_code)
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            record.args = (
                *args[:2],
                _TOKEN.sub(r"\1token=[redacted]", args[2]),
                *args[3:],
            )
        return True


def install() -> None:
    """Add the filter to uvicorn's access logger once."""
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, RedactTokenFilter) for f in access.filters):
        access.addFilter(RedactTokenFilter())


__all__ = ["RedactTokenFilter", "install"]
