"""Superadmin gate for verification approval.

The superadmin role is built separately. Until it exists this dependency
denies everyone, so the approval routes cannot be used by accident. When the
role lands, replace the body of ``require_superadmin`` and nothing else.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException
from fastapi import status as http_status
from hailhq.api.deps import Principal, get_current_principal


async def require_superadmin(
    principal: Annotated[Principal, Depends(get_current_principal)],
) -> Principal:
    raise HTTPException(
        status_code=http_status.HTTP_403_FORBIDDEN,
        detail="superadmin access is not enabled",
    )
