import uuid

import pytest
from fastapi import HTTPException
from hailhq.api.deps import Principal
from hailhq.api.superadmin import require_superadmin


def _p(**over):
    base = {
        "auth_kind": "jwt",
        "api_key_id": None,
        "user_id": uuid.uuid4(),
        "organization_id": uuid.uuid4(),
        "scopes": ["*"],
        "superadmin": False,
    }
    return Principal(**{**base, **over})


async def test_jwt_superadmin_passes():
    p = _p(superadmin=True)
    assert await require_superadmin(p) is p


@pytest.mark.parametrize(
    "over",
    [
        {},
        {"auth_kind": "apikey", "api_key_id": uuid.uuid4(), "superadmin": True},
        {"auth_kind": "shared", "user_id": None, "superadmin": True},
    ],
)
async def test_everyone_else_is_403(over):
    with pytest.raises(HTTPException) as exc:
        await require_superadmin(_p(**over))
    assert exc.value.status_code == 403
