from uuid import UUID

import httpx
import pytest
from hailhq.core.config import settings
from hailhq.core.providers.voice import CarrierRequestError
from hailhq.core.providers.voice.telnyx import place_number_order


@pytest.mark.parametrize(
    "body,request_id,codes,kept_id",
    [
        (
            {
                "errors": [
                    {
                        "code": "20003",
                        "title": "API Key forbidden",
                        "detail": "private applicant information",
                    }
                ]
            },
            "83cfaf11-a778-432a-872b-c6cf42ba2372",
            ("20003",),
            "83cfaf11-a778-432a-872b-c6cf42ba2372",
        ),
        (
            {
                "errors": [
                    {"code": 20003},
                    {"code": "20003"},
                    {"code": "private applicant information"},
                ]
            },
            "private applicant information",
            ("20003",),
            None,
        ),
        (
            {"errors": [None, "private applicant information", {"code": True}]},
            None,
            (),
            None,
        ),
        ({"errors": None}, None, (), None),
        (["private applicant information"], None, (), None),
        (None, None, (), None),
    ],
)
async def test_order_rejection_keeps_only_safe_diagnostics(
    monkeypatch, body, request_id, codes, kept_id
):
    attempts = []

    def handler(request):
        attempts.append(request)
        return httpx.Response(
            400,
            json=body,
            headers={"x-request-id": request_id} if request_id else {},
        )

    monkeypatch.setattr(settings, "telnyx_api_key", "test-key")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(
            "hailhq.core.providers.telnyx.get_http_client", lambda: client
        )
        with pytest.raises(CarrierRequestError) as caught:
            await place_number_order(
                UUID("16860bff-34f9-4c24-800a-b39a6f6a4355"),
                "+18665550100",
                None,
                ["voice"],
            )
    assert len(attempts) == 1
    assert caught.value.status == 400
    assert caught.value.codes == codes
    assert caught.value.request_id == kept_id
    assert "private applicant information" not in str(caught.value)
