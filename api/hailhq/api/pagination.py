"""Shared cursor-pagination shape for every list/stream route.

All cursor-paginated endpoints share one wire contract (the base64
``"<isoformat>|<uuid>"`` cursor from :mod:`hailhq.core.schemas`): filter
strictly past the cursor on a ``(timestamp, id)`` tuple, over-fetch by
one row, and emit ``next_cursor`` from the last returned row. This module
is the single implementation; routes supply their statement and column
pair and keep only endpoint-specific filtering.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from fastapi import status as http_status
from hailhq.core.schemas import decode_cursor, encode_cursor
from sqlalchemy import Select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["fetch_cursor_page"]


async def fetch_cursor_page(
    db: AsyncSession,
    stmt: Select,
    ts_col: Any,
    id_col: Any,
    *,
    cursor: str | None,
    limit: int,
    newest_first: bool = False,
    scalars: bool = True,
) -> tuple[list[Any], str | None]:
    """Execute ``stmt`` as one cursor page; return ``(rows, next_cursor)``.

    ``newest_first`` picks the walk direction: descending with a
    strictly-less cursor filter (list endpoints) or ascending with
    strictly-greater (event streams). ``scalars=False`` returns Row
    objects for union/multi-column selects; ``ts_col.key``/``id_col.key``
    must then name the labeled columns. An empty-string cursor is treated
    as absent; a malformed one maps to 400.
    """
    if cursor:
        try:
            cur_ts, cur_id = decode_cursor(cursor)
        except ValueError as exc:
            raise HTTPException(
                status_code=http_status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc
        if newest_first:
            stmt = stmt.where(tuple_(ts_col, id_col) < tuple_(cur_ts, cur_id))
        else:
            stmt = stmt.where(tuple_(ts_col, id_col) > tuple_(cur_ts, cur_id))

    if newest_first:
        stmt = stmt.order_by(ts_col.desc(), id_col.desc())
    else:
        stmt = stmt.order_by(ts_col.asc(), id_col.asc())

    result = await db.execute(stmt.limit(limit + 1))
    rows = list(result.scalars().all() if scalars else result.all())

    next_cursor: str | None = None
    if len(rows) > limit:
        last = rows[limit - 1]
        next_cursor = encode_cursor(
            getattr(last, ts_col.key), getattr(last, id_col.key)
        )
        rows = rows[:limit]
    return rows, next_cursor
