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

    async def fetch_raw(self, key: str) -> bytes:
        def _do() -> bytes:
            resp = self._client.get_object(Bucket=self._bucket, Key=key)
            return resp["Body"].read()

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

    async def presign_get(self, key: str, *, ttl_seconds: int = 300) -> str:
        def _do() -> str:
            return self._client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._bucket, "Key": key},
                ExpiresIn=ttl_seconds,
            )

        return await asyncio.to_thread(_do)
