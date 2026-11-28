"""S3 client wrapper for mail MIME + attachment objects.

boto3 is sync; every call is dropped into ``asyncio.to_thread`` so
FastAPI handlers can ``await`` without blocking the event loop. Same
pattern as ``SesEmailProvider``. Backs both inbound (raw MIME + parsed
attachments) and outbound (uploaded attachment) storage in one bucket.
"""

from __future__ import annotations

import asyncio
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from hailhq.core.config import settings

__all__ = ["S3MailClient", "build_default_client"]


def build_default_client() -> Any:
    # Virtual-hosted addressing on the region's own endpoint: a presigned URL
    # for the global endpoint gets a redirect for a bucket outside
    # us-east-1, and a browser's CORS check fails on a redirect.
    return boto3.client(
        "s3",
        region_name=settings.aws_region or None,
        aws_access_key_id=settings.aws_access_key_id or None,
        aws_secret_access_key=settings.aws_secret_access_key or None,
        config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
    )


class S3MailClient:
    def __init__(self, *, client: Any | None = None, bucket: str) -> None:
        if not bucket:
            raise ValueError("S3MailClient requires a bucket")
        self._client = client if client is not None else build_default_client()
        self._bucket = bucket

    async def fetch_raw(self, key: str, *, max_bytes: int | None = None) -> bytes:
        if max_bytes is not None and max_bytes < 0:
            raise ValueError("The storage read limit must be nonnegative")

        def _do() -> bytes:
            resp = self._client.get_object(Bucket=self._bucket, Key=key)
            body = resp["Body"]
            try:
                size = resp.get("ContentLength")
                if max_bytes is not None and size is not None and size > max_bytes:
                    raise ValueError("Object exceeds the storage read limit")
                content = body.read() if max_bytes is None else body.read(max_bytes + 1)
                if max_bytes is not None and len(content) > max_bytes:
                    raise ValueError("Object exceeds the storage read limit")
                return content
            finally:
                body.close()

        return await asyncio.to_thread(_do)

    async def put_attachment(
        self, key: str, payload: bytes, content_type: str, *, encrypt: bool = False
    ) -> None:
        """Write one object. ``encrypt`` asks S3 to encrypt it at rest with
        its own keys (SSE-S3, ``AES256``), whatever the bucket default."""
        extra = {"ServerSideEncryption": "AES256"} if encrypt else {}
        await asyncio.to_thread(
            self._client.put_object,
            Bucket=self._bucket,
            Key=key,
            Body=payload,
            ContentType=content_type,
            **extra,
        )

    async def touch(
        self, key: str, content_type: str, *, encrypt: bool = False
    ) -> None:
        """Copy an object onto itself, so its age (and a lifecycle rule's
        clock) starts again. ``encrypt`` as in ``put_attachment``."""
        extra = {"ServerSideEncryption": "AES256"} if encrypt else {}
        await asyncio.to_thread(
            self._client.copy_object,
            Bucket=self._bucket,
            Key=key,
            CopySource={"Bucket": self._bucket, "Key": key},
            MetadataDirective="REPLACE",
            ContentType=content_type,
            **extra,
        )

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(
            self._client.delete_object, Bucket=self._bucket, Key=key
        )

    async def count_prefix(self, prefix: str, *, up_to: int) -> int:
        """How many objects are under ``prefix``, counting at most ``up_to``."""

        def _do() -> int:
            resp = self._client.list_objects_v2(
                Bucket=self._bucket, Prefix=prefix, MaxKeys=up_to
            )
            return int(resp.get("KeyCount", 0))

        return await asyncio.to_thread(_do)

    async def delete_prefix(self, prefix: str, *, keep: str | None = None) -> int:
        """Delete every object under ``prefix`` except ``keep``; returns how
        many."""

        def _do() -> int:
            deleted = 0
            pages = self._client.get_paginator("list_objects_v2").paginate(
                Bucket=self._bucket, Prefix=prefix
            )
            for page in pages:
                keys = [
                    {"Key": o["Key"]}
                    for o in page.get("Contents", [])
                    if o["Key"] != keep
                ]
                if keys:
                    resp = self._client.delete_objects(
                        Bucket=self._bucket, Delete={"Objects": keys, "Quiet": True}
                    )
                    # A 200 can still carry per-key failures; never count
                    # (or silently keep) a file S3 did not delete.
                    errors = resp.get("Errors") or []
                    if errors:
                        raise RuntimeError(
                            f"S3 could not delete {len(errors)} object(s) under "
                            f"{prefix}: {errors[0].get('Code')}"
                        )
                    deleted += len(keys)
            return deleted

        return await asyncio.to_thread(_do)

    async def head(self, key: str) -> dict[str, Any] | None:
        """The object's ``ContentLength`` and ``ContentType``, or None when
        it does not exist (404). S3 answers 403 instead for a missing key
        outside the caller's ``s3:ListBucket`` grant: that raises, so a
        missing right never reads as a missing file."""

        def _do() -> dict[str, Any] | None:
            try:
                resp = self._client.head_object(Bucket=self._bucket, Key=key)
            except ClientError as exc:
                code = str(exc.response.get("Error", {}).get("Code", ""))
                if code in ("404", "NoSuchKey", "NotFound"):
                    return None
                raise
            return {
                "ContentLength": int(resp.get("ContentLength", 0)),
                "ContentType": resp.get("ContentType", ""),
            }

        return await asyncio.to_thread(_do)

    async def fetch_head(self, key: str, length: int) -> bytes:
        """The object's first ``length`` bytes (fewer when it is shorter)."""

        def _do() -> bytes:
            resp = self._client.get_object(
                Bucket=self._bucket, Key=key, Range=f"bytes=0-{length - 1}"
            )
            body = resp["Body"]
            try:
                return body.read(length)
            finally:
                body.close()

        return await asyncio.to_thread(_do)

    async def copy(self, source: str, target: str, content_type: str) -> None:
        """Copy one object to a new key in the bucket, with ``content_type``."""
        await asyncio.to_thread(
            self._client.copy_object,
            Bucket=self._bucket,
            Key=target,
            CopySource={"Bucket": self._bucket, "Key": source},
            MetadataDirective="REPLACE",
            ContentType=content_type,
        )

    async def abort_multipart_uploads(self, prefix: str) -> int:
        """Abort every unfinished multipart upload under ``prefix``; returns
        how many. Needs ``s3:ListBucketMultipartUploads`` on the bucket."""

        def _do() -> int:
            aborted = 0
            pages = self._client.get_paginator("list_multipart_uploads").paginate(
                Bucket=self._bucket, Prefix=prefix
            )
            for page in pages:
                for upload in page.get("Uploads", []):
                    self._client.abort_multipart_upload(
                        Bucket=self._bucket,
                        Key=upload["Key"],
                        UploadId=upload["UploadId"],
                    )
                    aborted += 1
            return aborted

        return await asyncio.to_thread(_do)

    async def presign(
        self,
        operation: str,
        key: str,
        *,
        ttl_seconds: int,
        http_method: str,
        **params: Any,
    ) -> str:
        """A presigned URL for one S3 ``operation`` (the boto3 client method
        name, such as ``upload_part``) on ``key``. Signing needs no network
        call."""

        def _do() -> str:
            return self._client.generate_presigned_url(
                operation,
                Params={"Bucket": self._bucket, "Key": key, **params},
                ExpiresIn=ttl_seconds,
                HttpMethod=http_method,
            )

        return await asyncio.to_thread(_do)

    async def presign_get(self, key: str, *, ttl_seconds: int = 300) -> str:
        def _do() -> str:
            return self._client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._bucket, "Key": key},
                ExpiresIn=ttl_seconds,
            )

        return await asyncio.to_thread(_do)
