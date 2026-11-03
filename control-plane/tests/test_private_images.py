"""Private images: a group's read-only registry credential stays on the server; agents get a short-lived
token for one image (decision of 3 November 2026). A fake registry speaks the token protocol of GHCR and
Docker Hub (401 + `WWW-Authenticate: Bearer realm=...`, then a token service with Basic auth)."""

from __future__ import annotations

import base64
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from conftest import gname
from fake_agent import share
from sqlalchemy import select

from pool_cp import registry
from pool_cp.models import GroupRegistryCredential
from pool_protocol import messages as pm

pytestmark = pytest.mark.asyncio

USER, SECRET = "lab-reader", "placeholder-read-only-token"  # synthetic test credentials


class FakeRegistry:
    def __init__(self, mode: str = "token") -> None:
        self.mode = mode
        self.token_requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

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
                    q = dict(urllib.parse.parse_qsl(url.query))
                    auth = self.headers.get("Authorization", "")
                    ok = auth == "Basic " + base64.b64encode(f"{USER}:{SECRET}".encode()).decode()
                    outer.token_requests.append({**q, "ok": ok})
                    if not ok:
                        self.send_response(401)
                        self.end_headers()
                        return
                    body = json.dumps({"token": f"tok:{q.get('scope', 'login')}", "expires_in": 300}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(404)
                self.end_headers()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.host = f"127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()


@pytest.fixture
def fake_registry():
    reg = FakeRegistry()
    yield reg
    reg.close()


def image(host: str, repo: str = "lab/app") -> str:
    return f"{host}/{repo}@sha256:" + "a" * 64


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
    ref = registry.parse_image(image(fake_registry.host))
    token, expires = registry.pull_token(ref, USER, SECRET)
    assert token == "tok:repository:lab/app:pull"
    assert fake_registry.token_requests[-1]["scope"] == "repository:lab/app:pull"
    assert fake_registry.token_requests[-1]["service"] == "fake-registry"
    with pytest.raises(registry.RegistryError, match="refused"):
        registry.pull_token(ref, USER, "wrong")


async def test_registries_without_token_auth_are_explained():
    for mode, words in (("basic", "token authentication"), ("public", "public")):
        reg = FakeRegistry(mode)
        try:
            with pytest.raises(registry.RegistryError, match=words):
                registry.check_credentials(reg.host, USER, SECRET)
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
            "spec": {"image": image(fake_registry.host), "command": ["true"]}}
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
            "spec": {"image": image(fake_registry.host), "command": ["true"]}}
    await owner.post(f"/api/v1/groups/{group}/jobs", spec, expect=201)
    offer = await agent.ready()
    assert agent.open_envelope(offer).registry_auth is False
    await agent.send(pm.Accept(attempt_id=offer.attempt_id, epoch=offer.epoch, tier="T2"))
    await agent.send(pm.PullAuthRequest(attempt_id=offer.attempt_id, epoch=offer.epoch))
    auth = await agent.recv_type(pm.PullAuth)
    assert auth.token is None and "no credentials" in auth.reason
