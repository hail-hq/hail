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
