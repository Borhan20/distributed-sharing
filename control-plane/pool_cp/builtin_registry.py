"""The server's built-in container registry (docs/design/pool-run.md §3.1).

SECURITY-SENSITIVE: issues registry access. Needs security review (plan §8.10).

A CNCF Distribution registry (Apache-2.0) runs next to the server in token-auth mode, like GHCR and Docker
Hub. It trusts one certificate (registry.crt in the data dir); this module holds the matching key and signs
its short-lived tokens:

- members get push and pull for `<group>/<handle>/...` in groups where they may submit jobs (api/registry.py),
- computers get pull for exactly one image of the task they run (agent_ws.py),
- the server itself reads and cleans up the registry (retention, below).

Images are named `<registry address>/<group>/<handle>/<name>:<tag>`. The address is what both members' Docker
and the computers' Docker reach (POOL_REGISTRY_ADDRESS); the server reaches the registry at
POOL_REGISTRY_INTERNAL_URL, a trusted address set by the admin, so its requests do not go through the public-
address guard in registry.py.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import logging
import os
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import jwt
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from .config import Settings
from .registry import (
    ImageInfo,
    RegistryError,
    _is_loopback,
    image_info,
    normalize_registry,
    parse_image,
)

log = logging.getLogger("pool_cp.builtin_registry")

TOKEN_TTL_S = 300  # members' push and pull tokens; Docker asks again when one runs out
# Computers use their token for a whole pull, and Docker does not ask for a new one in the middle: long enough
# for a large image on a slow line. Still pull-only, for one repository.
DEVICE_PULL_TTL_S = 1800
# One path component of a repository name, as Distribution accepts it.
_COMPONENT_RE = re.compile(r"^[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*$")
_MANIFEST_TYPES = ", ".join((
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
))


def enabled(settings: Settings) -> bool:
    return bool(settings.registry_address)


def address(settings: Settings) -> str:
    return normalize_registry(settings.registry_address or "")


def is_builtin(settings: Settings, image: str) -> bool:
    if not enabled(settings):
        return False
    try:
        return parse_image(image).registry == address(settings)
    except RegistryError:
        return False


def namespace(group: str, handle: str) -> str:
    """The repository prefix a member may push to in a group: '<group>/<handle>'."""
    for part, what in ((group, "group name"), (handle, "user name")):
        if not _COMPONENT_RE.match(part):
            raise RegistryError(f"the {what} {part!r} cannot be part of an image name in the group's registry "
                                "(letters and digits, separated by single dots, dashes or underscores)")
    return f"{group}/{handle}"


def owner(repository: str) -> tuple[str, str] | None:
    """('ml-lab', 'alice') for 'ml-lab/alice/app'; None for names outside any member's space."""
    parts = repository.split("/")
    return (parts[0], parts[1]) if len(parts) >= 3 else None


def tag_of(image: str) -> str | None:
    """'host/g/u/app:run-1@sha256:...' -> 'run-1'."""
    last = image.split("@", 1)[0].rsplit("/", 1)[-1]
    return last.split(":", 1)[1] if ":" in last else None


def valid_repository(repository: str) -> bool:
    return all(_COMPONENT_RE.match(p) for p in repository.split("/")) and len(repository) <= 255


class Issuer:
    """The registry's token signer: an EC P-256 key and a self-signed certificate the registry trusts."""

    def __init__(self, key_file: Path, cert_file: Path, issuer: str, service: str, create: bool) -> None:
        self.issuer, self.service = issuer, service
        key_file, cert_file = Path(key_file), Path(cert_file)
        if key_file.exists() and cert_file.exists():
            self.key = serialization.load_pem_private_key(key_file.read_bytes(), password=None)
            self.cert = x509.load_pem_x509_certificate(cert_file.read_bytes())
        elif create:
            key = serialization.load_pem_private_key(key_file.read_bytes(), password=None) \
                if key_file.exists() else self._new_key(key_file)
            self.key, self.cert = key, self._new_cert(key, cert_file)
        else:
            raise RuntimeError(f"registry token key {key_file} or certificate {cert_file} is missing")
        self._x5c = [base64.b64encode(self.cert.public_bytes(serialization.Encoding.DER)).decode()]

    @staticmethod
    def _new_key(key_file: Path):  # noqa: ANN205
        key = ec.generate_private_key(ec.SECP256R1())
        key_file.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                      serialization.NoEncryption()))
        return key

    @staticmethod
    def _new_cert(key, cert_file: Path):  # noqa: ANN001, ANN205
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "pool registry token issuer")])
        now = dt.datetime.now(dt.UTC)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(minutes=5))
                .not_valid_after(now + dt.timedelta(days=3650))
                .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                .sign(key, hashes.SHA256()))
        cert_file.parent.mkdir(parents=True, exist_ok=True)
        cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        cert_file.chmod(0o644)  # the registry container reads it (deploy/compose.yaml)
        return cert

    def token(self, subject: str, access: list[dict], ttl_s: int = TOKEN_TTL_S) -> tuple[str, dt.datetime]:
        now = int(time.time())
        claims = {"iss": self.issuer, "sub": subject, "aud": self.service, "iat": now, "nbf": now - 10,
                  "exp": now + ttl_s, "jti": secrets.token_hex(12), "access": access}
        return (jwt.encode(claims, self.key, algorithm="ES256", headers={"x5c": self._x5c}),
                dt.datetime.fromtimestamp(now + ttl_s, dt.UTC))


def parse_scope(scope: str) -> tuple[str, str, set[str]] | None:
    """'repository:ml-lab/alice/app:push,pull' -> ('repository', 'ml-lab/alice/app', {'push', 'pull'})."""
    kind, _, rest = scope.partition(":")
    name, _, actions = rest.rpartition(":")
    if not kind or not name:
        return None
    return kind, name, {a for a in actions.split(",") if a}


@dataclass
class Registry:
    """The server's own client for the registry, at the trusted internal address."""

    settings: Settings
    issuer: Issuer
    timeout: float = 15

    @property
    def base(self) -> str:
        if self.settings.registry_internal_url:
            return self.settings.registry_internal_url.rstrip("/")
        host = address(self.settings)
        return f"{'http' if _is_loopback(host) else 'https'}://{host}"  # never tokens in clear over a network

    def _request(self, method: str, path: str, scopes: list[dict], headers: dict | None = None):  # noqa: ANN202
        token, _ = self.issuer.token("pool-server", scopes, ttl_s=60)
        req = urllib.request.Request(self.base + path, method=method,  # noqa: S310 (admin-set address)
                                     headers={"Authorization": f"Bearer {token}", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:  # noqa: S310
                return r.status, dict(r.headers), r.read(4 * 1024 * 1024)
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers or {}), b""
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RegistryError(f"the group's registry is not reachable: {getattr(exc, 'reason', exc)}") from None

    def digest(self, repository: str, reference: str) -> str | None:
        status, headers, _ = self._request(
            "HEAD", f"/v2/{repository}/manifests/{urllib.parse.quote(reference, safe=':')}",
            [{"type": "repository", "name": repository, "actions": ["pull"]}], {"Accept": _MANIFEST_TYPES})
        if status != 200:
            return None
        return next((v for k, v in headers.items() if k.lower() == "docker-content-digest"), None)

    def inspect(self, image: str) -> ImageInfo:
        """Read a pinned image in the group's registry (docs/design/pool-decides.md §4.1)."""
        ref = parse_image(image)
        digest = image.rsplit("@", 1)[1]
        scopes = [{"type": "repository", "name": ref.repository, "actions": ["pull"]}]

        def get(path: str, accept: str | None = None) -> bytes:
            status, _, body = self._request("GET", path, scopes, {"Accept": accept} if accept else None)
            if status != 200:
                raise RegistryError(f"the group's registry answered {status}")
            return body

        return image_info(lambda d: get(f"/v2/{ref.repository}/manifests/{d}", _MANIFEST_TYPES),
                          lambda d: get(f"/v2/{ref.repository}/blobs/{d}"), digest)

    def pin(self, image: str) -> str:
        """'host/ml-lab/alice/app:3' -> 'host/ml-lab/alice/app:3@sha256:...'."""
        ref = parse_image(image)
        name = image.split("@", 1)[0]
        last = name.rsplit("/", 1)[-1]
        if ":" not in last:
            name += ":latest"
        tag = name.rsplit(":", 1)[1]
        digest = self.digest(ref.repository, tag)
        if not digest:
            raise RegistryError(f"{name} is not in the group's registry: push it first (`pool run` does)")
        return f"{name}@{digest}"

    def repositories(self) -> list[str]:
        out: list[str] = []
        last = ""
        while True:
            q = "?n=1000" + (f"&last={urllib.parse.quote(last)}" if last else "")
            status, _, body = self._request("GET", "/v2/_catalog" + q,
                                            [{"type": "registry", "name": "catalog", "actions": ["*"]}])
            if status != 200:
                raise RegistryError(f"the registry's catalog answered {status}")
            page = json.loads(body or b"{}").get("repositories") or []
            out += page
            if len(page) < 1000:
                return out
            last = page[-1]

    def tags(self, repository: str) -> list[str]:
        status, _, body = self._request("GET", f"/v2/{repository}/tags/list",
                                        [{"type": "repository", "name": repository, "actions": ["pull"]}])
        return (json.loads(body or b"{}").get("tags") or []) if status == 200 else []

    def delete(self, repository: str, digest: str) -> bool:
        status, _, _ = self._request("DELETE", f"/v2/{repository}/manifests/{digest}",
                                     [{"type": "repository", "name": repository, "actions": ["pull", "delete"]}])
        return status in (202, 404)
