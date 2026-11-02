"""Object store access through short-lived presigned URLs (plan §7.8, §8.8).

Optional: when POOL_S3_ENDPOINT is not set, agents keep outputs locally and
only report their hashes. Objects are addressed by content hash inside a
per-group prefix, so caches and outputs never mix between groups.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from functools import lru_cache

from .config import Settings
from .timeutil import utcnow


@lru_cache(maxsize=4)
def _client(endpoint: str, region: str, access_key: str, secret_key: str):
    import boto3  # optional dependency: pool-cp[s3]
    from botocore.config import Config

    return boto3.client("s3", endpoint_url=endpoint, region_name=region, aws_access_key_id=access_key,
                        aws_secret_access_key=secret_key,
                        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}))


def object_key(group_id: uuid.UUID, sha256: str) -> str:
    return f"groups/{group_id}/sha256/{sha256}"


def presign_put(settings: Settings, group_id: uuid.UUID, sha256: str, size: int) -> tuple[str | None, datetime]:
    expires = utcnow() + timedelta(seconds=settings.presign_ttl_s)
    if not (settings.s3_endpoint and settings.s3_access_key and settings.s3_secret_key):
        return None, expires
    client = _client(settings.s3_endpoint, settings.s3_region, settings.s3_access_key, settings.s3_secret_key)
    url = client.generate_presigned_url(
        "put_object",
        Params={"Bucket": settings.s3_bucket, "Key": object_key(group_id, sha256), "ContentLength": size},
        ExpiresIn=settings.presign_ttl_s,
    )
    return url, expires


def presign_get(settings: Settings, group_id: uuid.UUID, sha256: str) -> str | None:
    if not (settings.s3_endpoint and settings.s3_access_key and settings.s3_secret_key):
        return None
    client = _client(settings.s3_endpoint, settings.s3_region, settings.s3_access_key, settings.s3_secret_key)
    return client.generate_presigned_url(
        "get_object", Params={"Bucket": settings.s3_bucket, "Key": object_key(group_id, sha256)},
        ExpiresIn=settings.presign_ttl_s,
    )
