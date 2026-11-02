"""Object store settings and presigned URLs (no network; the real store is tested in tests/e2e)."""

from __future__ import annotations

import base64
import hashlib
import uuid
from urllib.parse import parse_qs, urlsplit

import pytest

from pool_cp import storage
from pool_cp.config import Settings

KEYS = {"s3_access_key": "test-access", "s3_secret_key": "test-secret"}  # placeholders, not real credentials
OIDC = {"oidc_issuer": "https://idp.example.org/realms/pool", "oidc_audience": "pool-api"}


def dev(**kw) -> Settings:
    return Settings(dev_mode=True, **kw)


def test_store_is_off_by_default_and_with_empty_values():
    assert not storage.enabled(dev())
    s = dev(s3_endpoint="", s3_public_endpoint="", s3_access_key="", s3_secret_key="")  # as compose passes them
    assert not storage.enabled(s) and s.s3_endpoint is None


def test_public_endpoint_defaults_to_the_internal_one():
    s = dev(s3_endpoint="http://seaweedfs:8333", **KEYS)
    assert storage.enabled(s) and s.s3_public_endpoint == "http://seaweedfs:8333"


@pytest.mark.parametrize("kw, message", [
    ({"s3_endpoint": "http://seaweedfs:8333"}, "partly configured"),
    ({"s3_access_key": "a", "s3_secret_key": "b"}, "POOL_S3_ENDPOINT"),
    ({"s3_public_endpoint": "https://s3.example.org"}, "POOL_S3_ENDPOINT is not"),
    ({"s3_endpoint": "seaweedfs:8333", **KEYS}, "http(s) URL"),
    ({"s3_endpoint": "http://seaweedfs:8333", "s3_public_endpoint": "ftp://s3.example.org", **KEYS}, "http(s) URL"),
    ({"s3_endpoint": "http://user:placeholder@seaweedfs:8333", **KEYS}, "must not contain credentials"),
])
def test_bad_object_store_settings_are_refused(kw, message):
    with pytest.raises(ValueError, match=message.replace("(", r"\(").replace(")", r"\)")):
        dev(**kw)


def test_shared_server_needs_https_public_endpoint():
    base = {"s3_endpoint": "http://seaweedfs:8333", **KEYS, **OIDC}
    with pytest.raises(ValueError, match="must use https"):
        Settings(s3_public_endpoint="http://192.0.2.10:8333", **base)
    assert Settings(s3_public_endpoint="https://s3.example.org", **base).s3_public_endpoint
    # Loopback is allowed: agents accept plain http only from localhost dev profiles.
    assert Settings(s3_public_endpoint="http://127.0.0.1:8333", **base)
    assert Settings(s3_public_endpoint="http://localhost:8333", **base)


def test_presigned_put_is_signed_for_the_public_host_with_size_and_checksum():
    s = dev(s3_endpoint="http://seaweedfs:8333", s3_public_endpoint="https://s3.example.org", **KEYS)
    data = b"result\n"
    sha = hashlib.sha256(data).hexdigest()
    gid = uuid.uuid4()
    url, expires = storage.presign_put(s, gid, sha, len(data))
    u = urlsplit(url)
    assert (u.scheme, u.netloc) == ("https", "s3.example.org"), "agents must get the public address"
    assert u.path == f"/{s.s3_bucket}/groups/{gid}/sha256/{sha}"
    signed = parse_qs(u.query)["X-Amz-SignedHeaders"][0].split(";")
    assert {"host", "content-length", "x-amz-checksum-sha256"} <= set(signed)
    assert storage.checksum_header(sha) == base64.b64encode(hashlib.sha256(data).digest()).decode()


def test_presign_without_store_returns_no_url():
    url, _ = storage.presign_put(dev(), uuid.uuid4(), "0" * 64, 1)
    assert url is None and storage.presign_get(dev(), uuid.uuid4(), "0" * 64) is None
