from hailhq.core.email_attachment_limits import (
    ATTACHMENT_TOO_LARGE_DETAIL,
    MAX_EMAIL_ATTACHMENT_BYTES,
)


def test_max_attachment_bytes_is_ten_megabytes():
    assert MAX_EMAIL_ATTACHMENT_BYTES == 10 * 1024 * 1024


def test_oversize_detail_mentions_a_link():
    assert "link" in ATTACHMENT_TOO_LARGE_DETAIL
