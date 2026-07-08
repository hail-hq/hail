"""Shared error-response helpers.

FastAPI auto-documents every route's 422 response in the OpenAPI spec as
``HTTPValidationError`` (a list of ``{loc, msg, type}`` objects) — the shape
its own request-parsing validation errors take. Generated strict clients
(the Go CLI) unmarshal 422 bodies expecting exactly that shape. A route
raising its own business-rule 422 with a plain string ``detail`` breaks
that contract: the CLI crashes on unmarshal instead of surfacing the
message. Use :func:`unprocessable` for any hand-raised 422 so it matches
what the spec already promises.
"""

from __future__ import annotations

from fastapi import HTTPException
from fastapi import status as http_status


def unprocessable(msg: str, loc: list[str] | None = None) -> HTTPException:
    """422 shaped as FastAPI's own HTTPValidationError.

    ``loc`` defaults to ``["body"]`` — set it to the specific field/param
    path when there is one (e.g. ``["body", "recipient_consent"]``).
    """
    return HTTPException(
        status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail=[{"loc": loc or ["body"], "msg": msg, "type": "value_error"}],
    )


__all__ = ["unprocessable"]
