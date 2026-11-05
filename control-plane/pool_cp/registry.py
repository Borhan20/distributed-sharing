"""Private container images: image names, and short-lived pull tokens (registry token authentication).

SECURITY-SENSITIVE: handles registry credentials. Needs security review (plan §8.10).

A group stores one read-only credential per registry (GHCR, Docker Hub, ...). For each task the server
exchanges it at the registry's token service for a bearer token that can only pull that one repository
and expires within minutes, and gives the agent only that token (protocol message pull_auth).
This is the registry token protocol Docker itself uses (`WWW-Authenticate: Bearer realm=...`).

Images named by a tag are pinned to a digest at submit time (`resolve_digest`), with a manifest HEAD request,
which Docker Hub does not count as a pull.

Every request checks its address first (`_check_address`): only public addresses unless the server admin
allows private networks (POOL_REGISTRY_PRIVATE_NETWORKS; default on in dev mode): submitters choose the
registry host, so without this check they could make the server probe its own network. Redirects are
followed only on the same host, so credentials never travel to another one; the one exception is an image's
config file on a CDN (`_blob`): one hop, https only, public addresses only, and no token. The address is
checked before connecting; a DNS answer that changes between the check and the connection (rebinding) is not
caught. Each lookup has one overall deadline (`_within`), so a registry that answers slowly cannot hold the
server's threads.
"""

from __future__ import annotations

import base64
import contextvars
import hashlib
import http.client
import ipaddress
import json
import re
import socket
import time
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


_MAX_BODY = 4 * 1024 * 1024  # token answers and manifests are small
_MANIFEST_TYPES = ", ".join((
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
))
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _check_address(url: str, allow_private: bool) -> None:
    """Refuse addresses the server should not be made to contact (see the module docstring)."""
    split = urllib.parse.urlsplit(url)
    if split.scheme not in ("http", "https") or not split.hostname:
        raise RegistryError("the registry sent an unusable address")
    if split.scheme == "http" and not _is_loopback(split.netloc):
        raise RegistryError("registries must use https (plain http only for one on this machine)")
    try:
        port = split.port or (443 if split.scheme == "https" else 80)
    except ValueError:
        raise RegistryError("the registry address has a bad port number") from None
    try:
        infos = socket.getaddrinfo(split.hostname, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError):
        raise RegistryError(f"cannot find the registry {split.hostname}") from None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%", 1)[0])
        if ip.is_link_local or ip.is_multicast or ip.is_unspecified:
            raise RegistryError(f"{split.hostname} is not a registry address this server contacts")
        if not ip.is_global and not allow_private:
            raise RegistryError(f"{split.hostname} is on a private network. A server admin can allow registries "
                                "there with POOL_REGISTRY_PRIVATE_NETWORKS=true")


class _SameHostRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        if urllib.parse.urlsplit(newurl).netloc != urllib.parse.urlsplit(req.full_url).netloc:
            raise RegistryError("the registry redirected to another host")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_SameHostRedirects)


# The end of the current lookup (time.monotonic()), set by `_within`; None: only each request's own timeout.
_deadline: contextvars.ContextVar[float | None] = contextvars.ContextVar("registry_deadline", default=None)
_CHUNK = 64 * 1024
_NETWORK_ERRORS = (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException, ValueError)


def _within(seconds: float):  # noqa: ANN202 (a decorator)
    """Give a whole lookup (all its requests) one deadline."""
    def wrap(fn):  # noqa: ANN001, ANN202
        def run(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            token = _deadline.set(time.monotonic() + seconds)
            try:
                return fn(*args, **kwargs)
            finally:
                _deadline.reset(token)
        return run
    return wrap


def _time_left(timeout: float) -> float:
    end = _deadline.get()
    if end is None:
        return timeout
    left = end - time.monotonic()
    if left <= 0:
        raise RegistryError("the registry is too slow to answer")
    return min(timeout, left)


def _read(r, limit: int) -> bytes:  # noqa: ANN001 (a response)
    """Read at most limit + 1 bytes, in chunks, stopping at the deadline (a slow trickle cannot hold us)."""
    out = bytearray()
    read = getattr(r, "read1", None) or r.read  # read1: what has arrived, without waiting for a full chunk
    while len(out) <= limit:
        _time_left(1.0)
        chunk = read(min(_CHUNK, limit + 1 - len(out)))
        if not chunk:
            break
        out += chunk
    return bytes(out)


def _get(url: str, headers: dict[str, str], timeout: float, *, allow_private: bool = False,
         method: str = "GET") -> tuple[int, dict[str, str], bytes]:
    _check_address(url, allow_private)
    req = urllib.request.Request(url, headers=headers, method=method)  # noqa: S310 (scheme checked above)
    try:
        with _opener.open(req, timeout=_time_left(timeout)) as r:
            return r.status, dict(r.headers), _read(r, _MAX_BODY)[:_MAX_BODY]
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), (_read(exc, _MAX_BODY) if exc.fp else b"") or b""
    except RegistryError:
        raise
    except _NETWORK_ERRORS as exc:
        raise RegistryError(f"cannot reach the registry: {getattr(exc, 'reason', exc)}") from None


def _header(headers: dict[str, str], name: str) -> str:
    return next((v for k, v in headers.items() if k.lower() == name), "")


def _challenge(api: str, timeout: float, allow_private: bool = False) -> dict[str, str] | None:
    """The token service's parameters (realm, service), or None if the registry needs no credentials."""
    status, headers, _ = _get(f"{api}/v2/", {}, timeout, allow_private=allow_private)
    if status == 200:
        return None
    header = _header(headers, "www-authenticate")
    if status != 401 or not header.lower().startswith("bearer "):
        raise RegistryError("this registry does not use token authentication (supported: GHCR, Docker Hub, "
                            "and registries that work like them)")
    params = dict(_PARAM_RE.findall(header))
    realm = params.get("realm", "")
    split = urllib.parse.urlsplit(realm)
    if split.scheme != "https" and not (split.scheme == "http" and _is_loopback(split.netloc)):
        raise RegistryError("the registry's token service is not https")
    return params


def _token_request(ref: ImageRef, params: dict[str, str], query: dict[str, str], basic: str | None,
                   timeout: float, allow_private: bool) -> tuple[int, bytes]:
    if params.get("service"):
        query = {**query, "service": params["service"]}
    headers = {"Authorization": f"Basic {basic}"} if basic else {}
    status, _, body = _get(f"{params['realm']}?{urllib.parse.urlencode(query)}", headers, timeout,
                           allow_private=allow_private)
    return status, body


def _basic(username: str, secret: str) -> str:
    return base64.b64encode(f"{username}:{secret}".encode()).decode()


def _read_token(ref: ImageRef, body: bytes) -> tuple[str, datetime]:
    try:
        data = json.loads(body)
    except ValueError:
        raise RegistryError(f"{ref.registry}'s token service sent an unreadable answer") from None
    token = data.get("token") or data.get("access_token") if isinstance(data, dict) else None
    if not token or not isinstance(token, str):
        raise RegistryError(f"{ref.registry}'s token service sent no token")
    try:
        lifetime = int(data.get("expires_in") or 60)
    except (TypeError, ValueError):
        lifetime = 60
    return token, utcnow() + timedelta(seconds=max(30, min(lifetime, 3600)))


def pull_token(ref: ImageRef, username: str, secret: str, *, timeout: float = 15,
               allow_private: bool = False) -> tuple[str, datetime]:
    """A bearer token that can pull ref.repository only, and its expiry. Blocking; run it in a thread."""
    params = _challenge(ref.api, timeout, allow_private)
    if params is None:
        raise RegistryError("this registry does not ask for credentials; its images are public")
    status, body = _token_request(ref, params, {"scope": f"repository:{ref.repository}:pull"},
                                  _basic(username, secret), timeout, allow_private)
    if status in (401, 403):
        raise RegistryError(f"{ref.registry} refused the stored credentials")
    if status != 200:
        raise RegistryError(f"{ref.registry}'s token service answered {status}")
    return _read_token(ref, body)


def _manifest(ref: ImageRef, reference: str, token: str | None, timeout: float, allow_private: bool,
              method: str) -> tuple[int, dict[str, str], bytes]:
    headers = {"Accept": _MANIFEST_TYPES}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return _get(f"{ref.api}/v2/{ref.repository}/manifests/{urllib.parse.quote(reference, safe=':')}", headers,
                timeout, allow_private=allow_private, method=method)


def can_read(ref: ImageRef, digest: str, token: str, *, timeout: float = 15, allow_private: bool = False) -> bool:
    """Whether this pull token really reads this image. Token services may hand out a token with no access
    instead of refusing, and an agent that already has the image locally would not notice (Docker 29 does
    not ask the registry for content it has), so the server checks before giving the token to a computer."""
    status, _, _ = _manifest(ref, digest, token, timeout, allow_private, "HEAD")
    return status == 200


def _token_for(ref: ImageRef, credentials: tuple[str, str] | None, timeout: float, allow_private: bool,
               name: str | None = None) -> str | None:
    """A pull token for the image's repository, if the registry asks for one."""
    params = _challenge(ref.api, timeout, allow_private)
    if params is None:
        return None
    status, body = _token_request(ref, params, {"scope": f"repository:{ref.repository}:pull"},
                                  _basic(*credentials) if credentials else None, timeout, allow_private)
    if status in (401, 403):
        raise RegistryError(f"{ref.registry} refused the stored credentials" if credentials else
                            f"{ref.registry} wants credentials to look up {name or ref.repository}")
    if status != 200:
        raise RegistryError(f"{ref.registry}'s token service answered {status}")
    token, _ = _read_token(ref, body)
    return token


@_within(30)
def resolve_digest(image: str, credentials: tuple[str, str] | None, *, timeout: float = 15,
                   allow_private: bool = False) -> str:
    """Pin a tag: 'ghcr.io/lab/app:1' -> 'ghcr.io/lab/app:1@sha256:...', the version the tag names right now.

    `credentials` (username, secret) are used when the registry asks for them; without them the lookup is
    anonymous, which works for public images. Blocking; run it in a thread.
    """
    ref = parse_image(image)
    name = image.split("@", 1)[0]
    last = name.rsplit("/", 1)[-1]
    tag = last.split(":", 1)[1] if ":" in last else "latest"
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", tag):
        raise RegistryError(f"{tag!r} is not a valid tag")
    if ":" not in last:
        name = f"{name}:latest"
    token = _token_for(ref, credentials, timeout, allow_private, name)
    status, headers, _ = _manifest(ref, tag, token, timeout, allow_private, "HEAD")
    if status in (401, 403, 404):
        if credentials:
            raise RegistryError(f"{name} was not found, or the stored token cannot read it. Check the name and "
                                "tag, and that the token's account can read this image.")
        raise RegistryError(f"{name} was not found, or it is private. Check the name and tag. If the image is "
                            f"private, add a read-only token for {ref.registry} under My images.")
    if status != 200:
        raise RegistryError(f"{ref.registry} answered {status} when looking up {name}")
    digest = _header(headers, "docker-content-digest")
    if not _DIGEST_RE.match(digest):
        # Some registries leave the header out of HEAD answers: fetch the manifest and hash it (that is what
        # the digest is). Docker Hub always sends it, so this GET never counts as a Docker Hub pull here.
        status, _, body = _manifest(ref, tag, token, timeout, allow_private, "GET")
        if status != 200 or not body:
            raise RegistryError(f"{ref.registry} sent no digest for {name}")
        digest = "sha256:" + hashlib.sha256(body).hexdigest()
    return f"{name}@{digest}"


def check_credentials(registry: str, username: str, secret: str, *, timeout: float = 15,
                      allow_private: bool = False) -> None:
    """Raise RegistryError if the registry refuses these credentials, the way `docker login` checks them:
    ask the token service with the credentials and no repository scope."""
    api, _ = _endpoints(registry)
    params = _challenge(api, timeout, allow_private)
    if params is None:
        raise RegistryError("this registry does not ask for credentials; its images are public")
    ref = ImageRef(registry=registry, repository="", api=api, auth_key="")
    status, _ = _token_request(ref, params, {"account": username}, _basic(username, secret), timeout, allow_private)
    if status in (401, 403):
        raise RegistryError(f"{registry} refused these credentials")
    if status != 200:
        raise RegistryError(f"{registry}'s token service answered {status}")


# --------------------------------------------------------------------------
# Reading an image (docs/design/pool-decides.md §4.1)
# --------------------------------------------------------------------------

_CONFIG_MAX = 1024 * 1024  # an image's config file is small
_REDIRECTS = (301, 302, 303, 307, 308)


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        return None  # the caller sees the redirect and decides


_no_redirects = urllib.request.build_opener(_NoRedirects)


def _fetch_once(url: str, headers: dict[str, str], timeout: float, allow_private: bool) -> tuple[int, dict, bytes]:
    _check_address(url, allow_private)
    req = urllib.request.Request(url, headers=headers)  # noqa: S310 (scheme checked above)
    try:
        with _no_redirects.open(req, timeout=_time_left(timeout)) as r:
            return r.status, dict(r.headers), _read(r, _CONFIG_MAX)
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), b""
    except RegistryError:
        raise
    except _NETWORK_ERRORS as exc:
        raise RegistryError(f"cannot reach the registry: {getattr(exc, 'reason', exc)}") from None


def _verified(body: bytes, digest: str, what: str) -> bytes:
    """Content is addressed by its digest: whoever served it, it must hash to that digest."""
    if len(body) > _CONFIG_MAX or "sha256:" + hashlib.sha256(body).hexdigest() != digest:
        raise RegistryError(f"the registry sent a {what} that does not match its digest")
    return body


def _blob(ref: ImageRef, digest: str, token: str | None, timeout: float, allow_private: bool) -> bytes:
    """A small blob (an image's config). Registries such as Docker Hub send blobs from another host (a CDN): one
    redirect is followed, only to a public https address, and without the registry's token."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    status, got, body = _fetch_once(f"{ref.api}/v2/{ref.repository}/blobs/{digest}", headers, timeout, allow_private)
    if status in _REDIRECTS:
        location = urllib.parse.urljoin(ref.api + "/", _header(got, "location"))
        split = urllib.parse.urlsplit(location)
        if split.scheme != "https":
            raise RegistryError("the registry redirected to an address that is not https")
        if split.username or split.password or any(ord(c) < 33 or ord(c) == 127 for c in location):
            raise RegistryError("the registry redirected to an address pool does not follow")
        status, _, body = _fetch_once(location, {}, timeout, allow_private=False)
    if status != 200:
        raise RegistryError(f"{ref.registry} answered {status} for the image's config")
    return _verified(body, digest, "config")


@dataclass
class ImageInfo:
    """What pool reads from an image before it runs (§4.1). Values of environment variables are never kept."""

    platforms: list[str]  # "linux/amd64", "linux/arm64/v8"
    cuda: bool  # built on an NVIDIA CUDA image
    ports: list[str]  # EXPOSE lines, e.g. "8000/tcp"
    healthcheck: bool
    size_mb: int  # compressed layers of the platform read

    def as_dict(self) -> dict:
        return {"platforms": self.platforms, "cuda": self.cuda, "ports": self.ports, "healthcheck": self.healthcheck,
                "size_mb": self.size_mb}


CUDA_ENV = ("CUDA_VERSION", "NVIDIA_REQUIRE_CUDA")  # set by NVIDIA's CUDA base images (checked 6 November 2026)


_ARCH_RE = re.compile(r"^[a-z0-9_]{1,16}$")
_PORT_RE = re.compile(r"^\d{1,5}(/(tcp|udp|sctp))?$")
_MAX_PLATFORMS = 32


def _platform(p: dict) -> str | None:
    """"linux/amd64" or "linux/arm64/v8"; None for anything that is not a plain platform."""
    if not isinstance(p, dict) or p.get("os") not in ("linux", "windows"):
        return None
    arch, variant = p.get("architecture"), p.get("variant")
    if not isinstance(arch, str) or not _ARCH_RE.match(arch):
        return None
    if variant is not None and (not isinstance(variant, str) or not _ARCH_RE.match(variant)):
        return None
    return f"{p['os']}/{arch}/{variant}" if variant else f"{p['os']}/{arch}"


def _json(body: bytes) -> dict:
    """Strict JSON: no Infinity or NaN, bounded nesting (a hostile manifest still matches its digest)."""
    def no_constants(name: str) -> None:
        raise RegistryError(f"the image's metadata holds {name}")
    try:
        doc = json.loads(body, parse_constant=no_constants)
    except (json.JSONDecodeError, RecursionError, UnicodeDecodeError) as exc:
        raise RegistryError("the image's metadata is not readable JSON") from exc
    if not isinstance(doc, dict):
        raise RegistryError("the image's metadata is not a JSON object")
    return doc


def image_info(get_manifest, get_blob, digest: str) -> ImageInfo:  # noqa: ANN001 (two fetch functions)
    """Read an image by its digest with `get_manifest(reference) -> bytes` and `get_blob(digest) -> bytes`."""
    doc = _json(_verified(get_manifest(digest), digest, "manifest"))
    platforms: list[str] = []
    if "manifests" in doc:  # an index: one image per platform (attestations have the platform "unknown")
        entries = [m for m in doc["manifests"] if isinstance(m, dict) and _platform(m.get("platform"))
                   and isinstance(m.get("digest"), str) and _DIGEST_RE.match(m["digest"])][:_MAX_PLATFORMS]
        if not entries:
            raise RegistryError("the image has no runnable platform")
        platforms = sorted({_platform(m["platform"]) for m in entries})
        chosen = next((m for m in entries if m["platform"].get("os") == "linux"
                       and m["platform"].get("architecture") == "amd64"), entries[0])
        doc = _json(_verified(get_manifest(chosen["digest"]), chosen["digest"], "manifest"))
    config_digest = (doc.get("config") or {}).get("digest", "") if isinstance(doc.get("config"), dict) else ""
    if not isinstance(config_digest, str) or not _DIGEST_RE.match(config_digest):
        raise RegistryError("the image's manifest names no config")
    config = _json(get_blob(config_digest))
    inner = config.get("config") if isinstance(config.get("config"), dict) else {}
    if not platforms:
        own = _platform(config)
        platforms = [own] if own else []
    env = inner.get("Env") if isinstance(inner.get("Env"), list) else []
    names = {e.split("=", 1)[0] for e in env if isinstance(e, str)}
    health = inner.get("Healthcheck") if isinstance(inner.get("Healthcheck"), dict) else {}
    test = health.get("Test") if isinstance(health.get("Test"), list) else []
    exposed = inner.get("ExposedPorts") if isinstance(inner.get("ExposedPorts"), dict) else {}
    layers = doc.get("layers") if isinstance(doc.get("layers"), list) else []
    size = sum(int(layer["size"]) for layer in layers
               if isinstance(layer, dict) and isinstance(layer.get("size"), int) and 0 <= layer["size"] < 1 << 50)
    return ImageInfo(platforms=platforms, cuda=any(n in names for n in CUDA_ENV),
                     ports=sorted(p for p in exposed if isinstance(p, str) and _PORT_RE.match(p))[:100],
                     healthcheck=bool(test) and test[0] != "NONE", size_mb=-(-size // (1 << 20)))


@_within(20)
def inspect_image(image: str, credentials: tuple[str, str] | None, *, timeout: float = 15,
                  allow_private: bool = False) -> ImageInfo:
    """Read a pinned image (name@sha256:...) from its registry. Blocking; run it in a thread."""
    ref = parse_image(image)
    digest = image.rsplit("@", 1)[1]
    token = _token_for(ref, credentials, timeout, allow_private)

    def manifest(reference: str) -> bytes:
        status, _, body = _manifest(ref, reference, token, timeout, allow_private, "GET")
        if status != 200:
            raise RegistryError(f"{ref.registry} answered {status} for the image's manifest")
        return body

    return image_info(manifest, lambda d: _blob(ref, d, token, timeout, allow_private), digest)

