"""US territory numbers must receive the same registration protection."""

import uuid
from types import SimpleNamespace

import pytest
from hailhq.core.sms_registration_gate import is_us, us_registration_block


@pytest.mark.parametrize(
    "e164",
    [
        "+13405550100",
        "+17875550100",
        "+19395550100",
        "+16715550100",
        "+16705550100",
        "+16845550100",
    ],
)
def test_territory_destinations_require_domestic_registration(e164):
    assert is_us(e164)


@pytest.mark.parametrize(
    "e164", ["+14165550100", "+12845550100", "+12425550100", "+447700900123"]
)
def test_foreign_destinations_remain_outside_domestic_registration(e164):
    assert not is_us(e164)


@pytest.mark.asyncio
@pytest.mark.parametrize("country", ["US", "VI", "PR", "GU", "MP", "AS"])
async def test_unregistered_domestic_sender_is_blocked(country):
    number = SimpleNamespace(
        country_code=country, number_type="local", sms_registration_id=None
    )
    assert await us_registration_block(None, uuid.uuid4(), number, "+12125550123")


async def test_unverifiable_tollfree_is_refused_only_to_the_us_and_canada():
    from types import SimpleNamespace
    from uuid import uuid4

    from hailhq.core.sms_registration_gate import (
        TOLLFREE_UNVERIFIABLE_MESSAGE,
        us_registration_block,
    )

    number = SimpleNamespace(
        number_type="toll_free", country_code="US", provider="telnyx"
    )
    org = uuid4()
    assert (
        await us_registration_block(None, org, number, "+14155550100")
        == TOLLFREE_UNVERIFIABLE_MESSAGE
    )
    assert (
        await us_registration_block(None, org, number, "+16135550100")
        == TOLLFREE_UNVERIFIABLE_MESSAGE
    )
    assert await us_registration_block(None, org, number, "+447700900123") is None
