"""Unit tests for hail-mail prefix derivation."""

from __future__ import annotations

import uuid

from hailhq.core.hail_mail import org_prefix_from_id
from hailhq.core.schemas import LOCAL_PREFIX


def test_org_prefix_is_deterministic() -> None:
    oid = uuid.UUID("12345678-1234-5678-1234-567812345678")
    assert org_prefix_from_id(oid) == org_prefix_from_id(oid)


def test_org_prefix_accepts_str_and_uuid() -> None:
    oid = uuid.uuid4()
    assert org_prefix_from_id(oid) == org_prefix_from_id(str(oid))


def test_distinct_orgs_get_distinct_prefixes() -> None:
    a = uuid.UUID(int=1)
    b = uuid.UUID(int=2)
    assert org_prefix_from_id(a) != org_prefix_from_id(b)


def test_org_prefix_matches_local_prefix_regex() -> None:
    # The derived prefix is the org side of <user>+<org>@<base>; it must pass
    # the same charset/length rule the API enforces on explicit prefixes.
    for _ in range(50):
        prefix = org_prefix_from_id(uuid.uuid4())
        assert len(prefix) == 8
        assert LOCAL_PREFIX.match(prefix), prefix


def test_org_prefix_encoding_is_pinned() -> None:
    # Exact vectors pin the wire format so the TypeScript mirror in
    # hail-website (lib/org-mail-prefix.ts) derives the identical address:
    # base32(sha256(uuid_bytes)[:5]), lowercased, no padding.
    zero = uuid.UUID(bytes=b"\x00" * 16)
    assert org_prefix_from_id(zero) == "g5dqr77x"
    high = uuid.UUID(bytes=b"\xff\xff\xff\xff\xff" + b"\x00" * 11)
    assert org_prefix_from_id(high) == "icyggeuk"
