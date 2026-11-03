"""Private images: a group's read-only registry credential stays on the server; agents get a short-lived
token for one image (decision of 3 November 2026). A fake registry speaks the token protocol of GHCR and
Docker Hub (401 + `WWW-Authenticate: Bearer realm=...`, then a token service with Basic auth)."""

from __future__ import annotations

import base64
import hashlib
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from conftest import gname
from fake_agent import share
from sqlalchemy import select

from pool_cp import registry
from pool_cp.models import GroupRegistryCredential, UserRegistryCredential
from pool_protocol import messages as pm

pytestmark = pytest.mark.asyncio

USER, SECRET = "lab-reader", "placeholder-read-only-token"  # synthetic test credentials


class FakeRegistry:
    """mode "token" (GHCR-like), "basic" or "public" (no credentials at all).

    In token mode the token service hands a token for any scope to valid credentials, like real ones may,
    but manifests answer only for repositories in `readable` (or `public`, for anonymous tokens). Images
    live in `manifests[repo][tag]`; their digest is the SHA-256 of the manifest body.
    """

    def __init__(self, mode: str = "token") -> None:
        self.mode = mode
        self.token_requests: list[dict] = []
        self.manifest_requests: list[tuple[str, str, str]] = []
        self.readable = {"lab/app"}
        self.public: set[str] = set()
        self.manifests: dict[str, dict[str, bytes]] = {"lab/app": {"1": b'{"schemaVersion":2,"v":1}'}}
        self.send_digest = True
        self.redirect_to: str | None = None
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def _manifest(self, head: bool):
                repo, _, ref = self.path.split("?", 1)[0].removeprefix("/v2/").partition("/manifests/")
                auth = self.headers.get("Authorization", "")
                outer.manifest_requests.append((self.command, repo, ref))
                ok = (outer.mode == "public" or auth == f"Bearer tok:repository:{repo}:pull" and repo in outer.readable
                      or auth == f"Bearer anon:repository:{repo}:pull" and repo in outer.public)
                tags = outer.manifests.get(repo, {})
                body = tags.get(ref) or next((b for b in tags.values()
                                              if "sha256:" + hashlib.sha256(b).hexdigest() == ref), None)
                if not ok or body is None:
                    self.send_response(401 if not ok else 404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/vnd.oci.image.manifest.v1+json")
                self.send_header("Content-Length", str(len(body)))
                if outer.send_digest:
                    self.send_header("Docker-Content-Digest", "sha256:" + hashlib.sha256(body).hexdigest())
                self.end_headers()
                if not head:
                    self.wfile.write(body)

            def do_HEAD(self):  # noqa: N802
                if "/manifests/" in self.path:
                    return self._manifest(head=True)
                self.send_response(404)
                self.end_headers()

            def do_GET(self):  # noqa: N802
                url = urllib.parse.urlsplit(self.path)
                if url.path == "/v2/":
                    if outer.mode == "public":
                        self.send_response(200)
                    else:
                        self.send_response(401)
                        realm = f"http://{self.headers['Host']}/token"
                        self.send_header("WWW-Authenticate", 'Basic realm="x"' if outer.mode == "basic"
                                         else f'Bearer realm="{realm}",service="fake-registry"')
                    self.end_headers()
                    return
                if url.path == "/token":
                    if outer.redirect_to:
                        self.send_response(302)
                        self.send_header("Location", outer.redirect_to)
                        self.end_headers()
                        return
                    q = dict(urllib.parse.parse_qsl(url.query))
                    auth = self.headers.get("Authorization", "")
                    ok = auth == "Basic " + base64.b64encode(f"{USER}:{SECRET}".encode()).decode()
                    outer.token_requests.append({**q, "ok": ok, "anonymous": not auth})
                    if not ok and auth:
                        self.send_response(401)
                        self.end_headers()
                        return
                    prefix = "tok" if ok else "anon"
                    body = json.dumps({"token": f"{prefix}:{q.get('scope', 'login')}", "expires_in": 300}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if "/manifests/" in url.path:
                    return self._manifest(head=False)
                self.send_response(404)
                self.end_headers()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.host = f"127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def digest(self, repo: str = "lab/app", tag: str = "1") -> str:
        return "sha256:" + hashlib.sha256(self.manifests[repo][tag]).hexdigest()

    def close(self) -> None:
        self.server.shutdown()


@pytest.fixture
def fake_registry():
    reg = FakeRegistry()
    yield reg
    reg.close()


def image(reg: FakeRegistry, repo: str = "lab/app", tag: str = "1") -> str:
    return f"{reg.host}/{repo}:{tag}@{reg.digest(repo, tag)}"


# --------------------------------------------------------------------------- names and tokens


async def test_image_names():
    hub = registry.parse_image("busybox@sha256:" + "a" * 64)
    assert (hub.registry, hub.repository, hub.api, hub.auth_key) == (
        "docker.io", "library/busybox", "https://registry-1.docker.io", "https://index.docker.io/v1/")
    org = registry.parse_image("docker.io/lab/app:1.2@sha256:" + "a" * 64)
    assert (org.registry, org.repository) == ("docker.io", "lab/app")
    gh = registry.parse_image("ghcr.io/lab/models/train@sha256:" + "a" * 64)
    assert (gh.registry, gh.repository, gh.api, gh.auth_key) == ("ghcr.io", "lab/models/train", "https://ghcr.io",
                                                                 "ghcr.io")
    local = registry.parse_image("127.0.0.1:5000/lab/app@sha256:" + "a" * 64)
    assert local.api == "http://127.0.0.1:5000", "plain http only for a registry on the same machine"
    assert registry.normalize_registry("https://GHCR.io/") == "ghcr.io"
    assert registry.normalize_registry("index.docker.io") == "docker.io"
    with pytest.raises(registry.RegistryError):
        registry.normalize_registry("not a host")


async def test_pull_token_is_scoped_to_one_repository(fake_registry):
    ref = registry.parse_image(image(fake_registry))
    token, expires = registry.pull_token(ref, USER, SECRET, allow_private=True)
    assert token == "tok:repository:lab/app:pull"
    assert fake_registry.token_requests[-1]["scope"] == "repository:lab/app:pull"
    assert fake_registry.token_requests[-1]["service"] == "fake-registry"
    with pytest.raises(registry.RegistryError, match="refused"):
        registry.pull_token(ref, USER, "wrong", allow_private=True)


async def test_registries_without_token_auth_are_explained():
    for mode, words in (("basic", "token authentication"), ("public", "public")):
        reg = FakeRegistry(mode)
        try:
            with pytest.raises(registry.RegistryError, match=words):
                registry.check_credentials(reg.host, USER, SECRET, allow_private=True)
        finally:
            reg.close()


# --------------------------------------------------------------------------- API


async def test_coordinator_stores_credentials_encrypted_and_members_only_see_the_registry(user, fake_registry, db):
    coord, member = await user(), await user()
    group = gname()
    await coord.post("/api/v1/groups", {"name": group, "display_name": "Lab"}, expect=201)
    inv = (await coord.post(f"/api/v1/groups/{group}/invites", {"role": "member", "max_uses": 1}, expect=201)).json()
    await member.post("/api/v1/invites/accept", {"token": inv["token"]}, expect=200)

    body = {"registry": fake_registry.host, "username": USER, "token": "wrong"}
    r = await coord.req("PUT", f"/api/v1/groups/{group}/registries", json=body, expect=400)
    assert "refused" in r.json()["detail"]
    body["token"] = SECRET
    await member.req("PUT", f"/api/v1/groups/{group}/registries", json=body, expect=403)
    saved = (await coord.req("PUT", f"/api/v1/groups/{group}/registries", json=body, expect=200)).json()
    assert saved["registry"] == fake_registry.host and saved["updated_by"] == coord.handle
    listed = await member.get(f"/api/v1/groups/{group}/registries")
    assert listed == [{**saved}] and SECRET not in json.dumps(listed)
    row = (await db.execute(select(GroupRegistryCredential))).scalars().all()
    stored = next(c for c in row if c.registry_host == fake_registry.host)
    assert SECRET.encode() not in bytes(stored.secret), "the secret is encrypted at rest"
    await member.req("DELETE", f"/api/v1/groups/{group}/registries/{fake_registry.host}", expect=403)
    await coord.req("DELETE", f"/api/v1/groups/{group}/registries/{fake_registry.host}", expect=204)
    assert await member.get(f"/api/v1/groups/{group}/registries") == []


# --------------------------------------------------------------------------- agents


async def test_agent_gets_a_token_for_its_private_image_only(group_with_device, fake_registry, enrolled, user):
    owner, group, agent = await group_with_device()
    await owner.req("PUT", f"/api/v1/groups/{group}/registries",
                    json={"registry": fake_registry.host, "username": USER, "token": SECRET}, expect=200)
    await agent.connect([share(group)])
    spec = {"apiVersion": "pool/v1alpha1", "kind": "Job", "metadata": {"name": "private"},
            "spec": {"image": image(fake_registry), "command": ["true"]}}
    await owner.post(f"/api/v1/groups/{group}/jobs", spec, expect=201)
    offer = await agent.ready()
    assert agent.open_envelope(offer).registry_auth is True
    await agent.send(pm.Accept(attempt_id=offer.attempt_id, epoch=offer.epoch, tier="T2"))
    await agent.send(pm.PullAuthRequest(attempt_id=offer.attempt_id, epoch=offer.epoch))
    auth = await agent.recv_type(pm.PullAuth)
    assert (auth.registry, auth.token, auth.reason) == (fake_registry.host, "tok:repository:lab/app:pull", None)
    audit = await owner.get(f"/api/v1/groups/{group}/audit")
    assert any(e["action"] == "image.pull_token" and e["details"]["repository"] == "lab/app" for e in audit)
    assert not any("tok:" in json.dumps(e) for e in audit), "tokens never reach the audit log"

    # Another computer cannot ask for this attempt's token.
    other = await enrolled(owner, "other")
    await other.connect([share(group)])
    await other.send(pm.PullAuthRequest(attempt_id=offer.attempt_id, epoch=offer.epoch))
    refused = await other.recv_type(pm.Cancel, pm.Error, pm.PullAuth)
    assert not isinstance(refused, pm.PullAuth), refused  # the server tells it to drop the attempt; no token


async def test_public_images_need_no_token_and_missing_credentials_are_explained(group_with_device, fake_registry):
    owner, group, agent = await group_with_device()
    await agent.connect([share(group)])
    spec = {"apiVersion": "pool/v1alpha1", "kind": "Job", "metadata": {"name": "nocreds"},
            "spec": {"image": image(fake_registry), "command": ["true"]}}
    await owner.post(f"/api/v1/groups/{group}/jobs", spec, expect=201)
    offer = await agent.ready()
    assert agent.open_envelope(offer).registry_auth is False
    await agent.send(pm.Accept(attempt_id=offer.attempt_id, epoch=offer.epoch, tier="T2"))
    await agent.send(pm.PullAuthRequest(attempt_id=offer.attempt_id, epoch=offer.epoch))
    auth = await agent.recv_type(pm.PullAuth)
    assert auth.token is None and "has a token" in auth.reason


# --------------------------------------------------------------------------- tags, own tokens (3 November 2026)


async def test_the_server_only_contacts_public_registry_addresses_by_default():
    for url, words in (("https://127.0.0.1:5000/v2/", "private network"), ("https://10.1.2.3/v2/", "private network"),
                       ("https://[fe80::1]/v2/", "not a registry address"),
                       ("https://169.254.169.254/v2/", "not a registry address"),
                       ("http://10.9.9.9/v2/", "https"), ("https://10.0.0.1:99999/v2/", "bad port")):
        with pytest.raises(registry.RegistryError, match=words):
            registry._check_address(url, allow_private=False)
    with pytest.raises(registry.RegistryError, match="not a registry address"):
        registry._check_address("https://169.254.169.254/v2/", allow_private=True), "metadata is never allowed"
    registry._check_address("http://127.0.0.1:5000/v2/", allow_private=True)


async def test_redirects_to_another_host_are_refused(fake_registry):
    fake_registry.redirect_to = "http://127.0.0.2:1/steal"
    with pytest.raises(registry.RegistryError, match="another host"):
        registry.check_credentials(fake_registry.host, USER, SECRET, allow_private=True)


async def test_tags_resolve_to_the_digest_they_name_now(fake_registry):
    tag = f"{fake_registry.host}/lab/app:1"
    assert registry.resolve_digest(tag, (USER, SECRET), allow_private=True) == f"{tag}@{fake_registry.digest()}"
    assert fake_registry.manifest_requests[-1][0] == "HEAD", "a HEAD request: Docker Hub does not count it as a pull"
    with pytest.raises(registry.RegistryError, match="private.*My images"):
        registry.resolve_digest(tag, None, allow_private=True)
    fake_registry.public.add("lab/app")
    assert registry.resolve_digest(tag, None, allow_private=True).endswith(fake_registry.digest())
    fake_registry.send_digest = False  # some registries leave the header out: hash the manifest instead
    assert registry.resolve_digest(tag, None, allow_private=True).endswith(fake_registry.digest())
    with pytest.raises(registry.RegistryError, match="not found"):
        registry.resolve_digest(f"{fake_registry.host}/lab/app:2", None, allow_private=True)
    with pytest.raises(registry.RegistryError, match="refused"):
        registry.resolve_digest(tag, (USER, "wrong"), allow_private=True)
    open_reg = FakeRegistry("public")
    try:
        open_reg.manifests["lab/app"]["latest"] = b"{}"
        assert registry.resolve_digest(f"{open_reg.host}/lab/app", None, allow_private=True) == (
            f"{open_reg.host}/lab/app:latest@sha256:" + hashlib.sha256(b"{}").hexdigest())
    finally:
        open_reg.close()


async def test_my_own_token_is_checked_encrypted_and_never_shown(user, fake_registry, db):
    me = await user()
    await me.req("PUT", "/api/v1/me/registries",
                 json={"registry": fake_registry.host, "username": USER, "token": "wrong"}, expect=400)
    saved = (await me.req("PUT", "/api/v1/me/registries",
                          json={"registry": fake_registry.host, "username": USER, "token": SECRET}, expect=200)).json()
    assert saved["registry"] == fake_registry.host and "token" not in saved
    assert await me.get("/api/v1/me/registries") == [saved]
    assert await (await user()).get("/api/v1/me/registries") == [], "each person sees only their own"
    rows = (await db.execute(select(UserRegistryCredential))).scalars().all()
    assert all(SECRET.encode() not in bytes(c.secret) for c in rows), "encrypted at rest"
    await me.req("DELETE", f"/api/v1/me/registries/{fake_registry.host}", expect=204)
    await me.req("DELETE", f"/api/v1/me/registries/{fake_registry.host}", expect=404)


async def test_a_tag_is_pinned_at_submit_with_my_token_and_only_my_jobs_use_it(group_with_device, fake_registry, user):
    owner, group, agent = await group_with_device()
    inv = (await owner.post(f"/api/v1/groups/{group}/invites", {"role": "member", "max_uses": 1}, expect=201)).json()
    other = await user()
    await other.post("/api/v1/invites/accept", {"token": inv["token"]}, expect=200)
    tag = f"{fake_registry.host}/lab/app:1"
    short = {"image": tag, "command": "run {{index}} {{count}}", "parts": 2, "cpu": 1, "memory": "256Mi"}

    r = await owner.post(f"/api/v1/groups/{group}/jobs", short, expect=422)
    assert "My images" in r.json()["detail"], "a private tag without any token is explained at submit time"
    await owner.req("PUT", "/api/v1/me/registries",
                    json={"registry": fake_registry.host, "username": USER, "token": SECRET}, expect=200)
    await agent.connect([share(group)])

    # Another member naming the same image gets no pull access through my token.
    theirs = (await other.post(f"/api/v1/groups/{group}/jobs", {"image": image(fake_registry), "command": ["true"]},
                               expect=201)).json()
    offer = await agent.ready()
    assert agent.open_envelope(offer).registry_auth is False
    await agent.send(pm.Accept(attempt_id=offer.attempt_id, epoch=offer.epoch, tier="T2"))
    await agent.send(pm.PullAuthRequest(attempt_id=offer.attempt_id, epoch=offer.epoch))
    auth = await agent.recv_type(pm.PullAuth)
    assert auth.token is None and "has a token" in auth.reason
    await other.post(f"/api/v1/jobs/{theirs['id']}/cancel", expect=200)

    job = (await owner.post(f"/api/v1/groups/{group}/jobs", short, expect=201)).json()
    assert job["image"] == f"{tag}@{fake_registry.digest()}" and job["name"] == "app" and job["tasks"]["total"] == 2
    detail = await owner.get(f"/api/v1/jobs/{job['id']}")
    assert detail["spec"]["as_submitted"]["image"] == tag
    offer = await agent.ready()
    env = agent.open_envelope(offer)
    assert env.registry_auth is True and env.spec.image == job["image"]
    assert env.spec.task_count == 2 and env.spec.command == ["run", str(env.spec.task_index), "2"]
    await agent.send(pm.Accept(attempt_id=offer.attempt_id, epoch=offer.epoch, tier="T2"))
    await agent.send(pm.PullAuthRequest(attempt_id=offer.attempt_id, epoch=offer.epoch))
    auth = await agent.recv_type(pm.PullAuth)
    assert auth.token == "tok:repository:lab/app:pull"
    audit = await owner.get(f"/api/v1/groups/{group}/audit")
    assert any(e["action"] == "image.pull_token" and e["details"]["credential"] == "submitter" for e in audit)


async def test_a_token_that_cannot_read_the_image_is_not_handed_out(group_with_device, fake_registry):
    owner, group, agent = await group_with_device()
    await owner.req("PUT", f"/api/v1/groups/{group}/registries",
                    json={"registry": fake_registry.host, "username": USER, "token": SECRET}, expect=200)
    # An image this computer may already have from another group: the group's account cannot read it.
    fake_registry.manifests["other-lab/secret"] = {"1": b'{"other":1}'}
    spec = {"image": image(fake_registry, "other-lab/secret"), "command": ["true"]}
    await owner.post(f"/api/v1/groups/{group}/jobs", spec, expect=201)
    await agent.connect([share(group)])
    offer = await agent.ready()
    assert agent.open_envelope(offer).registry_auth is True
    await agent.send(pm.Accept(attempt_id=offer.attempt_id, epoch=offer.epoch, tier="T2"))
    await agent.send(pm.PullAuthRequest(attempt_id=offer.attempt_id, epoch=offer.epoch))
    auth = await agent.recv_type(pm.PullAuth)
    assert auth.token is None and "cannot read this image" in auth.reason
