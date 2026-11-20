"""Unit tests for :mod:`hailhq.core.urls`.

These are critical-path now: every URL crossing the JS↔Python boundary
flows through these helpers. The doc invariant in ``CLAUDE.md`` forbids
ad-hoc ``rstrip("/")`` and f-string concatenation, so the helpers
themselves need to be airtight.
"""

from __future__ import annotations

import pytest
from hailhq.core.urls import canonical_url, join_url, same_request_url, url_variants


@pytest.mark.parametrize(
    ("left", "right", "same"),
    [
        ("https://example.com", "https://example.com/", True),
        ("https://EXAMPLE.com:443/", "https://example.com/", True),
        ("https://example.com/hooks", "https://example.com/hooks/", False),
        ("https://example.com/?q=a", "https://example.com/?q=a/", False),
        (
            "https://user:Password@example.com/",
            "https://user:password@example.com/",
            False,
        ),
        ("not a url", "not a url", False),
    ],
)
def test_same_request_url_preserves_credential_destination(left, right, same):
    assert same_request_url(left, right) is same


def test_canonical_url_strips_single_trailing_slash() -> None:
    assert canonical_url("https://hail.so/") == "https://hail.so"


def test_canonical_url_strips_multiple_trailing_slashes() -> None:
    # Pydantic only adds one, but be tolerant of malformed input.
    assert canonical_url("https://hail.so///") == "https://hail.so"


def test_canonical_url_leaves_no_slash_alone() -> None:
    assert canonical_url("https://hail.so") == "https://hail.so"


def test_canonical_url_preserves_path_with_trailing_segment() -> None:
    # Only the trailing slash is stripped; path segments stay intact.
    assert canonical_url("https://hail.so/api/auth/") == "https://hail.so/api/auth"


def test_url_variants_produces_both_forms_from_no_slash_input() -> None:
    assert url_variants("https://mcp.hail.so") == [
        "https://mcp.hail.so",
        "https://mcp.hail.so/",
    ]


def test_url_variants_produces_both_forms_from_with_slash_input() -> None:
    # Canonical-first regardless of input shape.
    assert url_variants("https://mcp.hail.so/") == [
        "https://mcp.hail.so",
        "https://mcp.hail.so/",
    ]


def test_url_variants_never_duplicates() -> None:
    # The two forms are always distinct, so callers can safely union them.
    v = url_variants("https://example.com")
    assert len(v) == 2
    assert len(set(v)) == 2


def test_join_url_no_double_slash_when_base_ends_in_slash() -> None:
    assert join_url("https://hail.so/", "jwks") == "https://hail.so/jwks"


def test_join_url_no_double_slash_when_path_starts_with_slash() -> None:
    assert join_url("https://hail.so", "/jwks") == "https://hail.so/jwks"


def test_join_url_no_double_slash_when_both_have_slashes() -> None:
    assert join_url("https://hail.so/", "/jwks") == "https://hail.so/jwks"


def test_join_url_no_slashes_either_side() -> None:
    assert join_url("https://hail.so", "jwks") == "https://hail.so/jwks"


def test_join_url_strips_only_leading_path_slashes() -> None:
    # Mid-path slashes are real path separators and must survive.
    assert (
        join_url("https://hail.so", "api/auth/jwks") == "https://hail.so/api/auth/jwks"
    )


def test_join_url_with_pathful_base() -> None:
    # Auth URL has /api/auth in the path; the JWKS suffix should land cleanly.
    assert (
        join_url("https://hail.so/api/auth", "jwks") == "https://hail.so/api/auth/jwks"
    )
