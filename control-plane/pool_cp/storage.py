"""Object store access through short-lived presigned URLs (plan §7.8, §8.8).

Optional: when POOL_S3_ENDPOINT is not set, agents keep outputs locally and
only report their hashes. Objects are addressed by content hash inside a
per-group prefix, so caches and outputs never mix between groups.

Two addresses (see config.py): the control plane talks to the store at
s3_endpoint, and signs URLs for s3_public_endpoint, the name agents use. A
SigV4 signature covers the host, so a URL signed for one name is rejected when
sent to another.

Upload URLs also sign the object's SHA-256 (x-amz-checksum-sha256), so the
store rejects a body that does not match the hash it is stored under. Without
this, a device could overwrite another task's content-addressed output.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import uuid
from datetime import datetime, timedelta
from functools import lru_cache

from .config import Settings
from .timeutil import utcnow

log = logging.getLogger("pool_cp.storage")


def enabled(settings: Settings) -> bool:
    return bool(settings.s3_endpoint and settings.s3_access_key and settings.s3_secret_key)


@lru_cache(maxsize=8)
def _client(endpoint: str, region: str, access_key: str, secret_key: str):
    import boto3  # optional dependency: pool-cp[s3]
    from botocore.config import Config

    return boto3.client("s3", endpoint_url=endpoint, region_name=region, aws_access_key_id=access_key,
                        aws_secret_access_key=secret_key,
                        config=Config(signature_version="s3v4", s3={"addressing_style": "path"},
                                      connect_timeout=5, read_timeout=15, retries={"max_attempts": 2}))


def _signing_client(settings: Settings):
    """Client for presigning only: it never connects, so the public name need not resolve here."""
    return _client(settings.s3_public_endpoint or settings.s3_endpoint, settings.s3_region,
                   settings.s3_access_key, settings.s3_secret_key)


def _internal_client(settings: Settings):
    return _client(settings.s3_endpoint, settings.s3_region, settings.s3_access_key, settings.s3_secret_key)


def object_key(group_id: uuid.UUID, sha256: str) -> str:
    return f"groups/{group_id}/sha256/{sha256}"


def checksum_header(sha256_hex: str) -> str:
    """Value of x-amz-checksum-sha256: base64 of the raw digest."""
    return base64.b64encode(bytes.fromhex(sha256_hex)).decode()


def presign_put(settings: Settings, group_id: uuid.UUID, sha256: str, size: int) -> tuple[str | None, datetime]:
    expires = utcnow() + timedelta(seconds=settings.presign_ttl_s)
    if not enabled(settings):
        return None, expires
    url = _signing_client(settings).generate_presigned_url(
        "put_object",
        Params={"Bucket": settings.s3_bucket, "Key": object_key(group_id, sha256), "ContentLength": size,
                "ChecksumSHA256": checksum_header(sha256)},
        ExpiresIn=settings.presign_ttl_s,
    )
    return url, expires


def presign_get(settings: Settings, group_id: uuid.UUID, sha256: str, filename: str | None = None) -> str | None:
    """Short-lived download URL. With filename, the browser saves the file under that name."""
    if not enabled(settings):
        return None
    params = {"Bucket": settings.s3_bucket, "Key": object_key(group_id, sha256)}
    if filename:
        safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in filename)[:200] or "output"
        params["ResponseContentDisposition"] = f'attachment; filename="{safe}"'
    return _signing_client(settings).generate_presigned_url("get_object", Params=params,
                                                             ExpiresIn=settings.presign_ttl_s)


def delete_objects(settings: Settings, group_id: uuid.UUID, sha256s: list[str]) -> int:
    """Delete these objects of a group (blocking; call it from a thread). Returns how many were requested."""
    if not enabled(settings) or not sha256s:
        return 0
    client = _internal_client(settings)
    for i in range(0, len(sha256s), 1000):  # the S3 API takes at most 1000 keys per request
        batch = [{"Key": object_key(group_id, sha)} for sha in sha256s[i:i + 1000]]
        client.delete_objects(Bucket=settings.s3_bucket, Delete={"Objects": batch, "Quiet": True})
    return len(sha256s)


def delete_group_objects(settings: Settings, group_id: uuid.UUID) -> int:
    """Delete every stored object of a group (blocking; call it from a thread)."""
    if not enabled(settings):
        return 0
    client = _internal_client(settings)
    deleted = 0
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=settings.s3_bucket,
                                                                  Prefix=f"groups/{group_id}/"):
        keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
        if keys:
            client.delete_objects(Bucket=settings.s3_bucket, Delete={"Objects": keys, "Quiet": True})
            deleted += len(keys)
    return deleted


def object_exists(settings: Settings, group_id: uuid.UUID, sha256: str) -> bool:
    """Whether the store holds this object (blocking; call it from a thread)."""
    from botocore.exceptions import ClientError

    try:
        _internal_client(settings).head_object(Bucket=settings.s3_bucket, Key=object_key(group_id, sha256))
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


def copy_object(settings: Settings, group_id: uuid.UUID, sha256: str, out, max_bytes: int) -> int:  # noqa: ANN001
    """Write a stored object into the file-like `out`, checking its SHA-256 (blocking; call it from a thread).

    Returns the number of bytes written. Raises LookupError if the object is missing, ValueError if it is
    larger than max_bytes or does not match its hash (then `out` may hold part of it).
    """
    import hashlib

    from botocore.exceptions import ClientError

    try:
        obj = _internal_client(settings).get_object(Bucket=settings.s3_bucket, Key=object_key(group_id, sha256))
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
            raise LookupError(sha256) from None
        raise
    digest, size = hashlib.sha256(), 0
    for chunk in obj["Body"].iter_chunks(1024 * 1024):
        size += len(chunk)
        if size > max_bytes:
            raise ValueError("object is larger than allowed")
        digest.update(chunk)
        out.write(chunk)
    if digest.hexdigest() != sha256:
        raise ValueError("object does not match its hash")
    return size


def ensure_bucket(settings: Settings) -> str:
    """Make sure the bucket exists. Returns "exists" or "created"; raises on any other outcome.

    Blocking (boto3); call it from a thread.
    """
    from botocore.exceptions import ClientError

    client = _internal_client(settings)
    try:
        client.head_bucket(Bucket=settings.s3_bucket)
        return "exists"
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") not in ("404", "NoSuchBucket", "NotFound"):
            raise
    if not settings.s3_create_bucket:
        raise RuntimeError(f"bucket {settings.s3_bucket!r} does not exist and POOL_S3_CREATE_BUCKET is false")
    try:
        client.create_bucket(Bucket=settings.s3_bucket)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "BucketAlreadyOwnedByYou":
            raise
        return "exists"
    return "created"


async def keep_ensuring_bucket(settings: Settings, status: dict, *, first_delay_s: float = 1.0,
                               max_delay_s: float = 60.0) -> None:
    """Retry ensure_bucket until it succeeds (the store may start after the API).

    status["object_store"] is "ready" once done, otherwise "not ready"; /readyz reports it.
    """
    delay = first_delay_s
    while True:
        try:
            outcome = await asyncio.to_thread(ensure_bucket, settings)
        except Exception as exc:  # store down, wrong keys, missing permission: keep trying, say why
            status["object_store"] = "not ready"
            log.warning("object store not ready (bucket %r at %s): %s; retrying in %.0fs",
                        settings.s3_bucket, settings.s3_endpoint, exc, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, max_delay_s)
            continue
        status["object_store"] = "ready"
        log.info("object store ready: bucket %r %s", settings.s3_bucket, outcome)
        return
