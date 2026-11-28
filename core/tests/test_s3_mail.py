import asyncio
import io
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


def test_delete_prefix_keeps_the_named_key():
    stub = MagicMock()
    stub.get_paginator.return_value.paginate.return_value = [
        {"Contents": [{"Key": "p/id.a"}, {"Key": "p/id.b"}]},
    ]
    stub.delete_objects.return_value = {}
    client = S3MailClient(client=stub, bucket="hail-inbound")
    assert asyncio.run(client.delete_prefix("p/id", keep="p/id.b")) == 1
    stub.delete_objects.assert_called_once_with(
        Bucket="hail-inbound", Delete={"Objects": [{"Key": "p/id.a"}], "Quiet": True}
    )


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


def test_count_prefix_reads_key_count():
    stub = MagicMock()
    stub.list_objects_v2.return_value = {"KeyCount": 7}
    client = S3MailClient(client=stub, bucket="hail-inbound")
    assert asyncio.run(client.count_prefix("sms-evidence/o/", up_to=100)) == 7
    stub.list_objects_v2.assert_called_once_with(
        Bucket="hail-inbound", Prefix="sms-evidence/o/", MaxKeys=100
    )


def test_fetch_raw_bounds_buffering_and_closes_body():
    stub = MagicMock()
    body = io.BytesIO(b"abcdef")
    stub.get_object.return_value = {"Body": body}
    client = S3MailClient(client=stub, bucket="hail-inbound")
    with pytest.raises(ValueError, match="limit"):
        asyncio.run(client.fetch_raw("file", max_bytes=3))
    assert body.closed


def test_fetch_raw_rejects_large_content_length_without_reading():
    stub = MagicMock()
    body = MagicMock()
    stub.get_object.return_value = {"Body": body, "ContentLength": 4}
    client = S3MailClient(client=stub, bucket="hail-inbound")
    with pytest.raises(ValueError, match="limit"):
        asyncio.run(client.fetch_raw("file", max_bytes=3))
    body.read.assert_not_called()
    body.close.assert_called_once()


def _real_client(region: str = "eu-central-1"):
    from unittest.mock import patch

    from hailhq.core import s3_mail

    with patch.multiple(
        s3_mail.settings,
        aws_region=region,
        aws_access_key_id="AKIDEXAMPLE",
        aws_secret_access_key="secret",
    ):
        return s3_mail.build_default_client()


def test_presign_signs_one_operation_offline():
    from urllib.parse import parse_qs, urlsplit

    client = S3MailClient(client=_real_client(), bucket="example-bucket")
    url = asyncio.run(
        client.presign(
            "upload_part",
            "knowledge-documents/o/u.a",
            ttl_seconds=120,
            http_method="PUT",
            UploadId="U1",
            PartNumber=2,
        )
    )
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    assert parts.netloc == "example-bucket.s3.eu-central-1.amazonaws.com"
    assert parts.path == "/knowledge-documents/o/u.a"
    assert query["uploadId"] == ["U1"] and query["partNumber"] == ["2"]
    assert query["X-Amz-Expires"] == ["120"]


def test_presign_signs_the_content_type():
    from urllib.parse import parse_qs, urlsplit

    client = S3MailClient(client=_real_client(), bucket="example-bucket")
    url = asyncio.run(
        client.presign(
            "create_multipart_upload",
            "knowledge-documents/o/u.a",
            ttl_seconds=60,
            http_method="POST",
            ContentType="application/pdf",
        )
    )
    query = parse_qs(urlsplit(url).query, keep_blank_values=True)
    assert "uploads" in query
    assert query["X-Amz-SignedHeaders"] == ["content-type;host"]


def test_head_treats_404_as_missing():
    from botocore.stub import Stubber

    raw = _real_client()
    with Stubber(raw) as stub:
        stub.add_client_error(
            "head_object", service_error_code="404", http_status_code=404
        )
        client = S3MailClient(client=raw, bucket="example-bucket")
        assert asyncio.run(client.head("knowledge-documents/o/u.a")) is None


def test_head_raises_on_403():
    """A missing storage right must not read as a missing file."""
    from botocore.exceptions import ClientError
    from botocore.stub import Stubber

    raw = _real_client()
    with Stubber(raw) as stub:
        stub.add_client_error(
            "head_object", service_error_code="403", http_status_code=403
        )
        client = S3MailClient(client=raw, bucket="example-bucket")
        with pytest.raises(ClientError):
            asyncio.run(client.head("k"))


def test_presign_binds_the_content_length():
    from urllib.parse import parse_qs, urlsplit

    client = S3MailClient(client=_real_client(), bucket="example-bucket")
    url = asyncio.run(
        client.presign(
            "upload_part",
            "knowledge-documents/o/u.a",
            ttl_seconds=60,
            http_method="PUT",
            UploadId="U1",
            PartNumber=1,
            ContentLength=5242880,
        )
    )
    query = parse_qs(urlsplit(url).query)
    assert query["X-Amz-SignedHeaders"] == ["content-length;host"]


def test_abort_multipart_uploads_aborts_each_under_the_prefix():
    from botocore.stub import Stubber

    raw = _real_client()
    with Stubber(raw) as stub:
        stub.add_response(
            "list_multipart_uploads",
            {
                "Uploads": [
                    {"Key": "knowledge-documents/o/u.a", "UploadId": "U1"},
                    {"Key": "knowledge-documents/o/u.b", "UploadId": "U2"},
                ]
            },
            {"Bucket": "example-bucket", "Prefix": "knowledge-documents/o/u"},
        )
        for key, upload_id in (
            ("knowledge-documents/o/u.a", "U1"),
            ("knowledge-documents/o/u.b", "U2"),
        ):
            stub.add_response(
                "abort_multipart_upload",
                {},
                {"Bucket": "example-bucket", "Key": key, "UploadId": upload_id},
            )
        client = S3MailClient(client=raw, bucket="example-bucket")
        assert (
            asyncio.run(client.abort_multipart_uploads("knowledge-documents/o/u")) == 2
        )
        stub.assert_no_pending_responses()


def test_head_reads_size_and_type():
    from botocore.stub import Stubber

    raw = _real_client()
    with Stubber(raw) as stub:
        stub.add_response(
            "head_object",
            {"ContentLength": 12, "ContentType": "text/plain"},
            {"Bucket": "example-bucket", "Key": "k"},
        )
        client = S3MailClient(client=raw, bucket="example-bucket")
        assert asyncio.run(client.head("k")) == {
            "ContentLength": 12,
            "ContentType": "text/plain",
        }


def test_head_raises_other_errors():
    from botocore.exceptions import ClientError
    from botocore.stub import Stubber

    raw = _real_client()
    with Stubber(raw) as stub:
        stub.add_client_error(
            "head_object", service_error_code="500", http_status_code=500
        )
        client = S3MailClient(client=raw, bucket="example-bucket")
        with pytest.raises(ClientError):
            asyncio.run(client.head("k"))


def test_fetch_head_reads_a_range():
    stub = _stub_client(b"%PDF-1.7")
    client = S3MailClient(client=stub, bucket="example-bucket")
    assert asyncio.run(client.fetch_head("k", 1024)) == b"%PDF-1.7"
    stub.get_object.assert_called_with(
        Bucket="example-bucket", Key="k", Range="bytes=0-1023"
    )


def test_copy_sets_the_type():
    stub = _stub_client(b"")
    client = S3MailClient(client=stub, bucket="example-bucket")
    asyncio.run(client.copy("a", "b", "text/csv"))
    stub.copy_object.assert_called_with(
        Bucket="example-bucket",
        Key="b",
        CopySource={"Bucket": "example-bucket", "Key": "a"},
        MetadataDirective="REPLACE",
        ContentType="text/csv",
    )
