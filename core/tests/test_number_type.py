from uuid import uuid4

from hailhq.core.schemas import NumberAcquireRequest


def test_national_is_an_accepted_number_type():
    req = NumberAcquireRequest(
        quote_id=uuid4(), country_code="JP", number_type="national"
    )
    assert req.number_type == "national"
