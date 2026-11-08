import asyncio
from unittest.mock import MagicMock

import pytest
from hailhq.core.s3_mail import S3MailClient


def _stub_client(payload: bytes) -> MagicMock:
    client = MagicMock()
    body = MagicMock()
    body.read.return_value = payload
    client.get_object.return_value = {"Body": body}
    client.generate_presigned_url.return_value = "https://signed.example/foo"
    return client


def test_constructor_rejects_empty_bucket():
    with pytest.raises(ValueError):
        S3MailClient(client=MagicMock(), bucket="")


def test_fetch_raw_returns_bytes():
    stub = _stub_client(b"raw bytes")
    client = S3MailClient(client=stub, bucket="hail-inbound")
    result = asyncio.run(client.fetch_raw("raw/abc"))
    assert result == b"raw bytes"
    stub.get_object.assert_called_with(Bucket="hail-inbound", Key="raw/abc")


def test_put_attachment_writes():
    stub = _stub_client(b"")
    client = S3MailClient(client=stub, bucket="hail-inbound")
    asyncio.run(
        client.put_attachment("attachments/e/1", b"pdfbytes", "application/pdf")
    )
    stub.put_object.assert_called_once()
    kwargs = stub.put_object.call_args.kwargs
    assert kwargs["Bucket"] == "hail-inbound"
    assert kwargs["Key"] == "attachments/e/1"
    assert kwargs["Body"] == b"pdfbytes"
    assert kwargs["ContentType"] == "application/pdf"


def test_presign_returns_url():
    stub = _stub_client(b"")
    client = S3MailClient(client=stub, bucket="hail-inbound")
    url = asyncio.run(client.presign_get("raw/abc", ttl_seconds=300))
    assert url == "https://signed.example/foo"
    stub.generate_presigned_url.assert_called_with(
        "get_object",
        Params={"Bucket": "hail-inbound", "Key": "raw/abc"},
        ExpiresIn=300,
    )


def test_delete_prefix_deletes_every_page():
    stub = MagicMock()
    stub.get_paginator.return_value.paginate.return_value = [
        {"Contents": [{"Key": "sms-evidence/o/a"}, {"Key": "sms-evidence/o/b"}]},
        {"Contents": [{"Key": "sms-evidence/o/c"}]},
        {},
    ]
    stub.delete_objects.return_value = {}
    client = S3MailClient(client=stub, bucket="hail-inbound")
    assert asyncio.run(client.delete_prefix("sms-evidence/o/")) == 3
    stub.get_paginator.return_value.paginate.assert_called_once_with(
        Bucket="hail-inbound", Prefix="sms-evidence/o/"
    )
    assert stub.delete_objects.call_count == 2


def test_delete_prefix_raises_when_s3_keeps_a_file():
    stub = MagicMock()
    stub.get_paginator.return_value.paginate.return_value = [
        {"Contents": [{"Key": "sms-evidence/o/a"}]},
    ]
    stub.delete_objects.return_value = {
        "Errors": [{"Key": "sms-evidence/o/a", "Code": "AccessDenied"}]
    }
    client = S3MailClient(client=stub, bucket="hail-inbound")
    with pytest.raises(RuntimeError, match="AccessDenied"):
        asyncio.run(client.delete_prefix("sms-evidence/o/"))


def test_delete_org_evidence_without_a_bucket_does_nothing(monkeypatch):
    from hailhq.core import sms_evidence

    monkeypatch.setattr(
        type(sms_evidence.settings), "hail_mail_bucket", property(lambda s: "")
    )
    assert (
        asyncio.run(sms_evidence.delete_org_evidence(__import__("uuid").uuid4())) == 0
    )
