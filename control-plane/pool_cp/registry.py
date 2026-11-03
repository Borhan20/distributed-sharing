"""Private container images: image names, and short-lived pull tokens (registry token authentication).

SECURITY-SENSITIVE: handles registry credentials. Needs security review (plan §8.10).

A group stores one read-only credential per registry (GHCR, Docker Hub, ...). For each task the server
exchanges it at the registry's token service for a bearer token that can only pull that one repository
and expires within minutes, and gives the agent only that token (protocol message pull_auth).
This is the registry token protocol Docker itself uses (`WWW-Authenticate: Bearer realm=...`).
"""

from __future__ import annotations

import base64
import ipaddress
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta

from .timeutil import utcnow

DOCKER_HUB = "docker.io"
_HUB_ALIASES = {"docker.io", "index.docker.io", "registry-1.docker.io", "registry.hub.docker.com"}
_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?(:[0-9]{1,5})?$")
_PARAM_RE = re.compile(r'(\w+)="([^"]*)"')


class RegistryError(Exception):
    """A plain-language reason, shown to the job's members and in the agent's log."""


@dataclass(frozen=True)
class ImageRef:
    registry: str     # how credentials are filed: docker.io, ghcr.io, registry.example.org:5000
    repository: str   # library/busybox, lab/app
    api: str          # registry API base URL
    auth_key: str     # key in Docker's config "auths"


def _is_loopback(host: str) -> bool:
    name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    if name == "localhost":
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def normalize_registry(name: str) -> str:
    """'https://GHCR.io/' -> 'ghcr.io'; Docker Hub's many names -> 'docker.io'."""
    host = name.strip().lower()
    host = re.sub(r"^https?://", "", host).split("/", 1)[0]
    if host in _HUB_ALIASES:
        return DOCKER_HUB
    if not _HOST_RE.match(host):
        raise RegistryError(f"{name!r} is not a registry host name like ghcr.io or docker.io")
    return host


def _endpoints(registry: str) -> tuple[str, str]:
    if registry == DOCKER_HUB:
        return "https://registry-1.docker.io", "https://index.docker.io/v1/"
    scheme = "http" if _is_loopback(registry) else "https"  # plain http only for a registry on this machine
    return f"{scheme}://{registry}", registry


def parse_image(image: str) -> ImageRef:
    """docker.io/library/busybox@sha256:..., ghcr.io/lab/app:1@sha256:..., busybox@sha256:..."""
    name = image.split("@", 1)[0]
    first, _, rest = name.partition("/")
    if rest and ("." in first or ":" in first or first == "localhost"):
        registry, path = normalize_registry(first), rest
    else:
        registry, path = DOCKER_HUB, name
    last = path.rsplit("/", 1)
    if ":" in last[-1]:  # drop a tag; images are pinned by digest anyway
        path = "/".join([*last[:-1], last[-1].split(":", 1)[0]])
    if registry == DOCKER_HUB and "/" not in path:
        path = f"library/{path}"
    api, auth_key = _endpoints(registry)
    return ImageRef(registry=registry, repository=path, api=api, auth_key=auth_key)


def _get(url: str, headers: dict[str, str], timeout: float) -> tuple[int, dict[str, str], bytes]:
    if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
        raise RegistryError("the registry sent an unusable address")
    req = urllib.request.Request(url, headers=headers)  # noqa: S310 (scheme checked above)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read() or b""
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RegistryError(f"cannot reach the registry: {getattr(exc, 'reason', exc)}") from None


def _challenge(api: str, timeout: float) -> dict[str, str]:
    status, headers, _ = _get(f"{api}/v2/", {}, timeout)
    if status == 200:
        raise RegistryError("this registry does not ask for credentials; its images are public")
    header = next((v for k, v in headers.items() if k.lower() == "www-authenticate"), "")
    if status != 401 or not header.lower().startswith("bearer "):
        raise RegistryError("this registry does not use token authentication (supported: GHCR, Docker Hub, "
                            "and registries that work like them)")
    params = dict(_PARAM_RE.findall(header))
    realm = params.get("realm", "")
    split = urllib.parse.urlsplit(realm)
    if split.scheme != "https" and not (split.scheme == "http" and _is_loopback(split.netloc)):
        raise RegistryError("the registry's token service is not https")
    return params


def pull_token(ref: ImageRef, username: str, secret: str, *, timeout: float = 15) -> tuple[str, datetime]:
    """A bearer token that can pull ref.repository only, and its expiry. Blocking; run it in a thread."""
    params = _challenge(ref.api, timeout)
    query = {"scope": f"repository:{ref.repository}:pull"}
    if params.get("service"):
        query["service"] = params["service"]
    basic = base64.b64encode(f"{username}:{secret}".encode()).decode()
    status, _, body = _get(f"{params['realm']}?{urllib.parse.urlencode(query)}",
                           {"Authorization": f"Basic {basic}"}, timeout)
    if status in (401, 403):
        raise RegistryError(f"{ref.registry} refused the group's stored credentials")
    if status != 200:
        raise RegistryError(f"{ref.registry}'s token service answered {status}")
    try:
        data = json.loads(body)
    except ValueError:
        raise RegistryError(f"{ref.registry}'s token service sent an unreadable answer") from None
    token = data.get("token") or data.get("access_token")
    if not token:
        raise RegistryError(f"{ref.registry}'s token service sent no token")
    lifetime = int(data.get("expires_in") or 60)
    return token, utcnow() + timedelta(seconds=max(30, min(lifetime, 3600)))


def check_credentials(registry: str, username: str, secret: str, *, timeout: float = 15) -> None:
    """Raise RegistryError if the registry refuses these credentials, the way `docker login` checks them:
    ask the token service with the credentials and no repository scope."""
    api, _ = _endpoints(registry)
    params = _challenge(api, timeout)
    query = {"account": username}
    if params.get("service"):
        query["service"] = params["service"]
    basic = base64.b64encode(f"{username}:{secret}".encode()).decode()
    status, _, _ = _get(f"{params['realm']}?{urllib.parse.urlencode(query)}", {"Authorization": f"Basic {basic}"},
                        timeout)
    if status in (401, 403):
        raise RegistryError(f"{registry} refused these credentials")
    if status != 200:
        raise RegistryError(f"{registry}'s token service answered {status}")
