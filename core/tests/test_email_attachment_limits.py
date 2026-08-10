from hailhq.core.email_attachment_limits import (
    ATTACHMENT_TOO_LARGE_DETAIL,
    MAX_EMAIL_ATTACHMENT_BYTES,
)


def test_cap_after_base64_inflation_fits_ses_forty_megabyte_limit():
    # SESv2 rejects messages over 40MB *after* base64 encoding (×4/3 plus
    # MIME line breaks ≈ ×1.37). The raw-bytes cap must leave headroom for
    # bodies, footer, and headers on a maxed-out send.
    assert MAX_EMAIL_ATTACHMENT_BYTES * 1.37 < 40 * 1024 * 1024


def test_oversize_detail_mentions_a_link():
    assert "link" in ATTACHMENT_TOO_LARGE_DETAIL
