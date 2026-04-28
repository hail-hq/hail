"""Local validation of typed resource ids (`<type>:<uuid>`)."""

from __future__ import annotations

from uuid import UUID

import pytest

from hail import HailMalformedResourceId, parse_resource_id


def test_parse_resource_id_call_uuid_ok() -> None:
    raw = "call:abc12345-6789-4abc-9def-012345678901"
    rtype, rid = parse_resource_id(raw)
    assert rtype == "call"
    assert isinstance(rid, UUID)
    assert str(rid) == "abc12345-6789-4abc-9def-012345678901"


def test_parse_resource_id_unknown_type_raises() -> None:
    with pytest.raises(HailMalformedResourceId) as exc:
        parse_resource_id("sms:abc12345-6789-4abc-9def-012345678901")
    assert "unsupported resource type 'sms'" in str(exc.value)


def test_parse_resource_id_malformed_raises() -> None:
    # Missing colon altogether.
    with pytest.raises(HailMalformedResourceId) as exc:
        parse_resource_id("abc12345-6789-4abc-9def-012345678901")
    assert "missing ':'" in str(exc.value)

    # Empty type.
    with pytest.raises(HailMalformedResourceId):
        parse_resource_id(":abc12345-6789-4abc-9def-012345678901")

    # Empty id.
    with pytest.raises(HailMalformedResourceId):
        parse_resource_id("call:")

    # Bogus uuid.
    with pytest.raises(HailMalformedResourceId) as exc:
        parse_resource_id("call:not-a-uuid")
    assert "invalid uuid" in str(exc.value)
