"""SQL helpers for JSONB metadata columns."""

from __future__ import annotations

from typing import Any

from sqlalchemy import ColumnElement, Text, cast, func, literal, literal_column
from sqlalchemy.dialects.postgresql import JSONB

__all__ = ["merge_object"]


def merge_object(
    column: Any, fields: dict[str, str], *, drop: str | None = None
) -> ColumnElement[Any]:
    """``column`` with ``fields`` merged in, for an ``UPDATE ... SET``. ``drop``
    names one key removed first. ``||`` on a JSON ``null`` makes an array and
    the keys are lost, so a NULL or JSON ``null`` value starts from ``{}``."""
    current: ColumnElement[Any] = func.coalesce(
        func.nullif(column, literal_column("'null'::jsonb")),
        literal_column("'{}'::jsonb"),
    )
    if drop:
        current = current.op("-")(literal(drop, Text))
    return current.op("||")(cast(fields, JSONB))
