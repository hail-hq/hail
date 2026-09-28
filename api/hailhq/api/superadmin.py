"""Superadmin gate for verification approval.

Only a console session the website minted with ``superadmin: true`` passes
``require_superadmin``. API keys and the shared key are never superadmins,
whatever the claim.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException
from fastapi import status as http_status
from hailhq.api.deps import Principal, get_current_principal


async def require_superadmin(
    principal: Annotated[Principal, Depends(get_current_principal)],
) -> Principal:
    """Only a console session the website minted with superadmin: true.
    API keys and the shared key are never superadmins, whatever the claim."""
    if principal.auth_kind == "jwt" and principal.superadmin:
        return principal
    raise HTTPException(
        status_code=http_status.HTTP_403_FORBIDDEN,
        detail="superadmin access required",
    )
