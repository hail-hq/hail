"""Tests for the carrier-verification tools.

``submit_verification`` moves personal data, so most tests here check what it
must NOT do: call the API without the attestation, accept a bad file, or put a
submitted value into a return value.
"""

from __future__ import annotations

import base64
import json
import re
from uuid import uuid4

import httpx
import pytest
import respx
from hailhq.mcp import tools
from hailhq.mcp.hail_client import HailClient

_BASE_URL = "http://hail-test"
_API_KEY = "test-key"
_MIB = 1024 * 1024

_NAME = "SENTINEL_NAME_123"
_STREET = "SENTINEL_STREET_456"
_B64 = "SENTINEL_B64"
_FIELD_VALUE = "SENTINEL_FIELD_789"
_SENTINELS = (_NAME, _STREET, _B64, _FIELD_VALUE)


@pytest.fixture()
async def client() -> HailClient:
    c = HailClient(base_url=_BASE_URL, api_key=_API_KEY)
    try:
        yield c
    finally:
        await c.aclose()


def _verification(vid: str | None = None, state: str = "submitted", **extra) -> dict:
    return {
        "id": vid or str(uuid4()),
        "provider": "didww",
        "country_code": "GB",
        "number_type": "mobile",
        "subject_type": "person",
        "state": state,
        "rejection_reason": None,
        "created_at": "2026-10-01T10:00:00+00:00",
        "updated_at": "2026-10-01T10:00:00+00:00",
        "submitted_at": "2026-10-01T10:00:01+00:00",
        "approved_at": None,
        **extra,
    }


def _requirements() -> dict:
    return {
        "provider": "didww",
        "country_code": "GB",
        "number_type": "mobile",
        "subject_type": "person",
        "required": True,
        "fields": [
            {
                "name": "first_name",
                "label": "First name",
                "kind": "text",
                "help": "",
                "pattern": None,
                "options": None,
                "required": True,
            }
        ],
        "documents": [
            {
                "name": "identity",
                "label": "Proof of identity",
                "help": "",
                "options": [
                    {
                        "key": "passport",
                        "label": "Passport",
                        "file_required": True,
                        "fields": [],
                        "copies": [],
                        "needs_address": False,
                    }
                ],
            }
        ],
        "address_required": True,
        "subject_types": ["person", "business"],
    }


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _file(
    slot: str = "identity", data: bytes = b"%PDF-1.4 bytes", ct="application/pdf"
):
    return {"slot": slot, "content_type": ct, "content_base64": _b64(data)}


def _kwargs(client: HailClient, **over) -> dict:
    base = {
        "client": client,
        "country_code": "GB",
        "number_type": "mobile",
        "fields": {"first_name": _FIELD_VALUE},
        "documents": {"identity": {"option": "passport", "fields": {}}},
        "files": [_file()],
        "attest_authorized": True,
        "address": {
            "customer_name": _NAME,
            "street": _STREET,
            "city": "London",
            "region": "LDN",
            "postal_code": "N1 1AA",
            "country_code": "GB",
        },
    }
    base.update(over)
    return base


def _assert_no_sentinel(value: object) -> None:
    text = json.dumps(value)
    for s in _SENTINELS:
        assert s not in text, s


# --------------------------------------------------------------------------- #
# get_verification_requirements
# --------------------------------------------------------------------------- #


@respx.mock
async def test_requirements_sends_query(client: HailClient) -> None:
    route = respx.get(f"{_BASE_URL}/verifications/requirements").mock(
        return_value=httpx.Response(200, json=_requirements())
    )
    result = await tools.get_verification_requirements(
        client=client, country_code="GB", number_type="mobile"
    )
    params = dict(route.calls[0].request.url.params)
    assert params == {
        "country_code": "GB",
        "number_type": "mobile",
        "subject_type": "person",
    }
    assert result["required"] is True
    assert result["documents"][0]["needs_input"] is True
    assert result["address_required"] is True


@respx.mock
async def test_requirements_sends_provider_and_subject_type(
    client: HailClient,
) -> None:
    route = respx.get(f"{_BASE_URL}/verifications/requirements").mock(
        return_value=httpx.Response(200, json=_requirements())
    )
    await tools.get_verification_requirements(
        client=client,
        country_code="GB",
        number_type="mobile",
        subject_type="business",
        provider="didww",
    )
    params = dict(route.calls[0].request.url.params)
    assert params["subject_type"] == "business"
    assert params["provider"] == "didww"


@respx.mock
async def test_requirements_maps_422(client: HailClient) -> None:
    respx.get(f"{_BASE_URL}/verifications/requirements").mock(
        return_value=httpx.Response(
            422,
            json={
                "detail": [{"loc": ["query", "number_type"], "msg": "x", "type": "t"}]
            },
        )
    )
    result = await tools.get_verification_requirements(
        client=client, country_code="GB", number_type="bogus"
    )
    assert "error" in result


# --------------------------------------------------------------------------- #
# list / get / cancel
# --------------------------------------------------------------------------- #


@respx.mock
async def test_list_verifications(client: HailClient) -> None:
    rows = [_verification(), _verification(state="approved")]
    route = respx.get(f"{_BASE_URL}/verifications").mock(
        return_value=httpx.Response(200, json=rows)
    )
    result = await tools.list_verifications(client=client)
    assert route.called
    assert [r["state"] for r in result["items"]] == ["submitted", "approved"]


@respx.mock
async def test_get_verification(client: HailClient) -> None:
    vid = str(uuid4())
    route = respx.get(f"{_BASE_URL}/verifications/{vid}").mock(
        return_value=httpx.Response(
            200, json=_verification(vid, state="rejected", rejection_reason="blurry")
        )
    )
    result = await tools.get_verification(client=client, verification_id=vid)
    assert route.called
    assert result["state"] == "rejected"
    assert result["rejection_reason"] == "blurry"


@respx.mock
async def test_get_verification_404(client: HailClient) -> None:
    vid = str(uuid4())
    respx.get(f"{_BASE_URL}/verifications/{vid}").mock(
        return_value=httpx.Response(404, json={"detail": "not found"})
    )
    result = await tools.get_verification(client=client, verification_id=vid)
    assert result == {"error": "resource not found"}


@respx.mock
async def test_cancel_verification(client: HailClient) -> None:
    vid = str(uuid4())
    route = respx.delete(f"{_BASE_URL}/verifications/{vid}").mock(
        return_value=httpx.Response(200, json=_verification(vid, state="cancelled"))
    )
    result = await tools.cancel_verification(client=client, verification_id=vid)
    assert route.called
    assert result["state"] == "cancelled"


@respx.mock
async def test_cancel_verification_409_under_review(client: HailClient) -> None:
    vid = str(uuid4())
    respx.delete(f"{_BASE_URL}/verifications/{vid}").mock(
        return_value=httpx.Response(
            409,
            json={"detail": "this verification is under review; wait for the result"},
        )
    )
    result = await tools.cancel_verification(client=client, verification_id=vid)
    assert result == {"error": "this verification is under review; wait for the result"}


# --------------------------------------------------------------------------- #
# submit_verification: the request
# --------------------------------------------------------------------------- #


def _parse_multipart(request: httpx.Request) -> dict[str, dict]:
    """Return {part name: {"body": bytes, "headers": str}} from a multipart body."""
    ctype = request.headers["content-type"]
    assert ctype.startswith("multipart/form-data")
    boundary = re.search(r"boundary=([^;]+)", ctype).group(1).encode()
    parts: dict[str, dict] = {}
    for chunk in request.read().split(b"--" + boundary):
        chunk = chunk.strip(b"\r\n")
        if not chunk or chunk == b"--":
            continue
        head, _, body = chunk.partition(b"\r\n\r\n")
        headers = head.decode()
        name = re.search(r'name="([^"]+)"', headers).group(1)
        parts[name] = {"body": body, "headers": headers}
    return parts


@respx.mock
async def test_submit_builds_multipart(client: HailClient) -> None:
    vid = str(uuid4())
    captured: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["parts"] = _parse_multipart(request)
        captured["headers"] = request.headers
        return httpx.Response(201, json=_verification(vid))

    respx.post(f"{_BASE_URL}/verifications").mock(side_effect=_handler)
    payload = b"\xff\xd8\xff fake jpeg \x00\x01"
    result = await tools.submit_verification(
        **_kwargs(
            client,
            files=[_file(data=payload, ct="image/jpeg")],
            provider="didww",
        )
    )
    assert result["id"] == vid
    assert result["state"] == "submitted"

    parts = captured["parts"]
    assert parts["country_code"]["body"] == b"GB"
    assert parts["number_type"]["body"] == b"mobile"
    assert parts["subject_type"]["body"] == b"person"
    assert parts["provider"]["body"] == b"didww"
    assert json.loads(parts["fields"]["body"]) == {"first_name": _FIELD_VALUE}
    assert json.loads(parts["address"]["body"])["customer_name"] == _NAME
    assert json.loads(parts["documents"]["body"]) == {
        "identity": {"option": "passport", "fields": {}}
    }
    file_part = parts["file.identity"]
    assert file_part["body"] == payload
    assert 'filename="upload"' in file_part["headers"]
    assert "Content-Type: image/jpeg" in file_part["headers"]
    assert "idempotency-key" not in captured["headers"]


@respx.mock
async def test_submit_omits_optional_parts(client: HailClient) -> None:
    captured: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["parts"] = _parse_multipart(request)
        return httpx.Response(201, json=_verification())

    respx.post(f"{_BASE_URL}/verifications").mock(side_effect=_handler)
    await tools.submit_verification(**_kwargs(client, address=None))
    assert "address" not in captured["parts"]
    assert "provider" not in captured["parts"]


@respx.mock
async def test_submit_returns_only_the_api_response(client: HailClient) -> None:
    respx.post(f"{_BASE_URL}/verifications").mock(
        return_value=httpx.Response(201, json=_verification())
    )
    result = await tools.submit_verification(**_kwargs(client))
    assert set(result) == {
        "id",
        "provider",
        "country_code",
        "number_type",
        "subject_type",
        "state",
        "rejection_reason",
        "created_at",
        "updated_at",
        "submitted_at",
        "approved_at",
    }
    _assert_no_sentinel(result)


# --------------------------------------------------------------------------- #
# submit_verification: guards that make no HTTP call
# --------------------------------------------------------------------------- #


@respx.mock
@pytest.mark.parametrize("attest", [False, None, "true", 1, "yes"])
async def test_submit_requires_exact_true_attestation(
    client: HailClient, attest
) -> None:
    result = await tools.submit_verification(
        **_kwargs(client, attest_authorized=attest)
    )
    assert "attest_authorized" in result["error"]
    assert not respx.calls.called
    _assert_no_sentinel(result)


_BAD_CASES = {
    "bad_content_type": {"files": [_file(ct="image/gif")]},
    "bad_base64": {
        "files": [
            {
                "slot": "identity",
                "content_type": "application/pdf",
                "content_base64": _B64 + "!!not base64!!",
            }
        ]
    },
    "base64_with_whitespace": {
        "files": [
            {
                "slot": "identity",
                "content_type": "application/pdf",
                "content_base64": _b64(b"abcdef")[:4] + "\n" + _b64(b"abcdef")[4:],
            }
        ]
    },
    "empty_file": {"files": [_file(data=b"")]},
    "oversize_file": {"files": [_file(data=b"a" * (10 * _MIB + 1))]},
    "missing_slot_in_documents": {"files": [_file(slot="proof_of_address")]},
    "duplicate_slot": {"files": [_file(), _file()]},
    "too_many_files": {
        "files": [_file(slot=f"s{i}") for i in range(21)],
        "documents": {f"s{i}": {"option": "x", "fields": {}} for i in range(21)},
    },
    "too_many_fields": {"fields": {f"f{i}": _FIELD_VALUE for i in range(21)}},
    "malformed_file_entry": {"files": [{"slot": "identity", "extra": _NAME}]},
    "file_not_a_dict": {"files": [_NAME]},
}


@respx.mock
@pytest.mark.parametrize("case", sorted(_BAD_CASES))
async def test_submit_local_validation_makes_no_call_and_leaks_nothing(
    client: HailClient, case: str
) -> None:
    result = await tools.submit_verification(**_kwargs(client, **_BAD_CASES[case]))
    assert set(result) == {"error"}
    assert result["error"]
    assert not respx.calls.called
    _assert_no_sentinel(result)


@respx.mock
async def test_submit_total_size_limit(client: HailClient) -> None:
    big = b"a" * (10 * _MIB)
    files = [_file(slot=f"s{i}", data=big) for i in range(4)]
    docs = {f"s{i}": {"option": "x", "fields": {}} for i in range(4)}
    result = await tools.submit_verification(
        **_kwargs(client, files=files, documents=docs)
    )
    assert "30" in result["error"]
    assert not respx.calls.called


@respx.mock
async def test_submit_accepts_exact_limits(client: HailClient) -> None:
    respx.post(f"{_BASE_URL}/verifications").mock(
        return_value=httpx.Response(201, json=_verification())
    )
    files = [_file(slot=f"s{i}", data=b"a" * (10 * _MIB)) for i in range(3)]
    docs = {f"s{i}": {"option": "x", "fields": {}} for i in range(3)}
    result = await tools.submit_verification(
        **_kwargs(client, files=files, documents=docs)
    )
    assert "error" not in result


@respx.mock
@pytest.mark.parametrize("ct", ["image/jpeg", "image/png", "application/pdf"])
async def test_submit_accepts_allowed_types(client: HailClient, ct: str) -> None:
    respx.post(f"{_BASE_URL}/verifications").mock(
        return_value=httpx.Response(201, json=_verification())
    )
    result = await tools.submit_verification(**_kwargs(client, files=[_file(ct=ct)]))
    assert "error" not in result


# --------------------------------------------------------------------------- #
# submit_verification: API errors
# --------------------------------------------------------------------------- #


@respx.mock
async def test_submit_422_returns_loc_and_msg_only(client: HailClient) -> None:
    respx.post(f"{_BASE_URL}/verifications").mock(
        return_value=httpx.Response(
            422,
            json={
                "detail": [
                    {
                        "loc": ["body", "first_name"],
                        "msg": "first_name is required",
                        "type": "verification_problem",
                        "input": _FIELD_VALUE,
                        "ctx": {"value": _NAME},
                    },
                    {
                        "loc": ["body", "identity"],
                        "msg": "document is unreadable",
                        "type": "verification_problem",
                    },
                ]
            },
        )
    )
    result = await tools.submit_verification(**_kwargs(client))
    assert result["problems"] == [
        {"loc": ["body", "first_name"], "msg": "first_name is required"},
        {"loc": ["body", "identity"], "msg": "document is unreadable"},
    ]
    assert "nothing was stored" in result["error"]
    _assert_no_sentinel(result)


@respx.mock
async def test_submit_422_string_detail_is_not_echoed_raw(client: HailClient) -> None:
    respx.post(f"{_BASE_URL}/verifications").mock(
        return_value=httpx.Response(422, json={"detail": f"bad {_NAME}"})
    )
    result = await tools.submit_verification(**_kwargs(client))
    assert "error" in result
    _assert_no_sentinel(result)


@respx.mock
async def test_submit_409_duplicate_surfaces_detail(client: HailClient) -> None:
    vid = str(uuid4())
    respx.post(f"{_BASE_URL}/verifications").mock(
        return_value=httpx.Response(
            409,
            json={"detail": f"a verification already exists (submitted); id {vid}"},
        )
    )
    result = await tools.submit_verification(**_kwargs(client))
    assert vid in result["error"]
    assert "already exists" in result["error"]


@respx.mock
async def test_submit_503(client: HailClient) -> None:
    respx.post(f"{_BASE_URL}/verifications").mock(
        return_value=httpx.Response(
            503, json={"detail": "carrier unavailable; try again later"}
        )
    )
    result = await tools.submit_verification(**_kwargs(client))
    assert result == {"error": "carrier unavailable; try again later"}


@respx.mock
async def test_submit_unexpected_status_hides_body(client: HailClient) -> None:
    respx.post(f"{_BASE_URL}/verifications").mock(
        return_value=httpx.Response(400, text=f"echo {_NAME} {_STREET}")
    )
    result = await tools.submit_verification(**_kwargs(client))
    assert "400" in result["error"]
    _assert_no_sentinel(result)


@respx.mock
async def test_submit_timeout_points_to_list_verifications(
    client: HailClient,
) -> None:
    respx.post(f"{_BASE_URL}/verifications").mock(
        side_effect=httpx.ReadTimeout(f"timeout {_NAME}")
    )
    result = await tools.submit_verification(**_kwargs(client))
    assert result == {
        "error": "request timed out; the submission may have gone through. "
        "Call list_verifications to check before submitting again."
    }


@respx.mock
async def test_submit_unexpected_exception_hides_text(client: HailClient) -> None:
    respx.post(f"{_BASE_URL}/verifications").mock(
        side_effect=ValueError(f"boom {_NAME} {_STREET}")
    )
    result = await tools.submit_verification(**_kwargs(client))
    assert set(result) == {"error"}
    assert "list_verifications" in result["error"]
    _assert_no_sentinel(result)
