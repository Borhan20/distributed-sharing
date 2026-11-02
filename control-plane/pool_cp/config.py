"""Settings, read from environment variables prefixed with POOL_ (e.g. POOL_DATABASE_URL)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

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

    # Object store (S3 API, e.g. SeaweedFS). Optional.
    s3_endpoint: str | None = None
    s3_region: str = "us-east-1"
    s3_access_key: str | None = None
    s3_secret_key: str | None = None
    s3_bucket: str = "pool-artifacts"
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
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
