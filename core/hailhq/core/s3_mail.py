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
from hailhq.core.config import settings

__all__ = ["S3MailClient", "build_default_client"]


def build_default_client() -> Any:
    return boto3.client(
        "s3",
        region_name=settings.aws_region or None,
        aws_access_key_id=settings.aws_access_key_id or None,
        aws_secret_access_key=settings.aws_secret_access_key or None,
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

    async def put_attachment(self, key: str, payload: bytes, content_type: str) -> None:
        await asyncio.to_thread(
            self._client.put_object,
            Bucket=self._bucket,
            Key=key,
            Body=payload,
            ContentType=content_type,
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

    async def presign_get(self, key: str, *, ttl_seconds: int = 300) -> str:
        def _do() -> str:
            return self._client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._bucket, "Key": key},
                ExpiresIn=ttl_seconds,
            )

        return await asyncio.to_thread(_do)
