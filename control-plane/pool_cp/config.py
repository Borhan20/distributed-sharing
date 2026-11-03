"""Settings, read from environment variables prefixed with POOL_ (e.g. POOL_DATABASE_URL)."""

from __future__ import annotations

import ipaddress
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="POOL_", env_file=None, extra="ignore")

    database_url: str = "postgresql+asyncpg://pool@127.0.0.1:5432/pool"
    public_url: str = "http://127.0.0.1:8000"

    # Development mode: enables the built-in dev identity provider (/dev/*) and
    # auto-creates the CA and signing keys. NEVER enable on a shared server.
    dev_mode: bool = False

    # OIDC (plan §8.6). In dev mode the built-in dev IdP is used instead.
    oidc_issuer: str | None = None
    oidc_audience: str | None = None
    oidc_jwks_url: str | None = None
    oidc_username_claim: str = "preferred_username"
    require_mfa_for_admins: bool | None = None  # default: True unless dev_mode
    server_admins: list[str] = Field(default_factory=list)  # user handles
    # The OIDC client the web UI signs in with (public, PKCE). Served to the UI by GET /api/v1/config.
    oidc_web_client_id: str = "pool-web"
    # Who may create groups. "admins" (server admins only) suits a server open to self-registration,
    # so strangers cannot create groups and use its storage.
    group_creators: Literal["anyone", "admins"] = "anyone"
    # A deleted group can be restored for this long; then its data is purged.
    group_delete_grace_days: int = 7

    # Keys. Default location: <data_dir>/ca.key, ca.crt, signing.key
    data_dir: Path = Path("./var")
    ca_key_file: Path | None = None
    ca_cert_file: Path | None = None
    signing_key_file: Path | None = None
    autocreate_keys: bool | None = None  # default: dev_mode

    cert_ttl_hours: int = 24
    enrollment_code_ttl_s: int = 600
    heartbeat_interval_s: int = 15
    lease_ttl_s: int = 60
    scheduler_interval_s: float = 2.0
    api_heartbeat_s: float = 5.0  # API liveness row; the scheduler pauses expiry when it is 3x stale

    retry_base_s: float = 10.0
    retry_max_s: float = 600.0
    always_fail_devices: int = 2
    decline_cooldown_s: int = 300
    submit_rate_per_minute: int = 30
    enroll_rate_per_minute: int = 10  # per client address, unauthenticated endpoint
    max_ws_messages_per_s: int = 200

    # Object store (S3 API, e.g. SeaweedFS). Optional: set endpoint, access key and
    # secret key together, or none of them.
    # s3_endpoint is the address the control plane itself uses (e.g. http://seaweedfs:8333
    # inside compose). s3_public_endpoint is the address written into the presigned URLs
    # that agents use; the signature covers the host, so it must be the name agents reach
    # the store by. It defaults to s3_endpoint.
    s3_endpoint: str | None = None
    s3_public_endpoint: str | None = None
    s3_region: str = "us-east-1"
    s3_access_key: str | None = None
    s3_secret_key: str | None = None
    s3_bucket: str = "pool-artifacts"
    s3_create_bucket: bool = True  # create the bucket at startup if it is missing
    presign_ttl_s: int = 900

    @model_validator(mode="after")
    def _defaults(self) -> Settings:
        if self.require_mfa_for_admins is None:
            self.require_mfa_for_admins = not self.dev_mode
        if self.autocreate_keys is None:
            self.autocreate_keys = self.dev_mode
        self.ca_key_file = self.ca_key_file or self.data_dir / "ca.key"
        self.ca_cert_file = self.ca_cert_file or self.data_dir / "ca.crt"
        self.signing_key_file = self.signing_key_file or self.data_dir / "signing.key"
        if not self.dev_mode and not (self.oidc_issuer and self.oidc_audience):
            raise ValueError("POOL_OIDC_ISSUER and POOL_OIDC_AUDIENCE are required unless POOL_DEV_MODE=true")
        self._check_object_store()
        return self

    def _check_object_store(self) -> None:
        # Empty strings come from compose (`${POOL_S3_ENDPOINT:-}`); treat them as unset.
        for name in ("s3_endpoint", "s3_public_endpoint", "s3_access_key", "s3_secret_key"):
            if getattr(self, name) == "":
                setattr(self, name, None)
        required = {"POOL_S3_ENDPOINT": self.s3_endpoint, "POOL_S3_ACCESS_KEY": self.s3_access_key,
                    "POOL_S3_SECRET_KEY": self.s3_secret_key}
        if not any(required.values()):
            if self.s3_public_endpoint:
                raise ValueError("POOL_S3_PUBLIC_ENDPOINT is set but POOL_S3_ENDPOINT is not")
            return
        missing = [k for k, v in required.items() if not v]
        if missing:
            # A half-configured store would silently keep every output on the devices.
            raise ValueError(f"object store is partly configured; also set {', '.join(missing)}")
        self.s3_public_endpoint = self.s3_public_endpoint or self.s3_endpoint
        for name, value in (("POOL_S3_ENDPOINT", self.s3_endpoint),
                            ("POOL_S3_PUBLIC_ENDPOINT", self.s3_public_endpoint)):
            u = urlsplit(value)
            if u.username or u.password:  # checked first so the error never echoes a password
                raise ValueError(f"{name} must not contain credentials; use POOL_S3_ACCESS_KEY/SECRET_KEY")
            if u.scheme not in ("http", "https") or not u.hostname or u.query or u.fragment:
                raise ValueError(f"{name} must be an http(s) URL like https://s3.example.org, not {value!r}")
        # Agents refuse plain-http upload URLs unless they were enrolled against a
        # localhost dev server, so a shared server must hand out https URLs.
        public = urlsplit(self.s3_public_endpoint)
        if not self.dev_mode and public.scheme != "https" and not _is_loopback(public.hostname):
            raise ValueError("POOL_S3_PUBLIC_ENDPOINT must use https: agents refuse plain-http upload URLs "
                             "(put a TLS reverse proxy in front of the object store)")


def _is_loopback(host: str | None) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host or "").is_loopback
    except ValueError:
        return False


@lru_cache
def get_settings() -> Settings:
    return Settings()
