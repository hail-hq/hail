"""The uvicorn access log never prints a ``token`` query value."""

from __future__ import annotations

import logging

import hailhq.api.main  # noqa: F401  (installs the filter)
from hailhq.api.access_log import RedactTokenFilter


def _record(path: str) -> logging.LogRecord:
    return logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("10.0.0.1:5000", "PUT", path, "1.1", 200),
        None,
    )


def test_token_query_value_is_redacted() -> None:
    record = _record("/v1/uploads/abc?token=SECRET-tok_en&x=1")
    assert RedactTokenFilter().filter(record) is True
    message = record.getMessage()
    assert "SECRET" not in message
    assert "/v1/uploads/abc?token=[redacted]&x=1" in message


def test_other_queries_are_kept() -> None:
    record = _record("/v1/calls?cursor=abc&mytoken=1")
    RedactTokenFilter().filter(record)
    assert "/v1/calls?cursor=abc&mytoken=1" in record.getMessage()


def test_the_filter_is_on_the_uvicorn_access_logger() -> None:
    filters = logging.getLogger("uvicorn.access").filters
    assert any(isinstance(f, RedactTokenFilter) for f in filters)
