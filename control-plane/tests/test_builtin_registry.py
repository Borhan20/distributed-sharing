"""The built-in registry (docs/design/pool-run.md §3.1): its token service, pinning, computers' pull tokens and
clean-up. A fake registry checks every token's signature and access claims like CNCF Distribution does; the
real registry is in tests/e2e/test_pool_run.py."""

from __future__ import annotations

import base64
import hashlib
import json
import threading
import urllib.parse
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import jwt
import pytest
from conftest import gname
from fake_agent import share
from sqlalchemy import select, update

from pool_cp import builtin_registry
from pool_cp.models import RegistryImage
from pool_cp.registry import RegistryError
from pool_cp.registry_cleanup import clean_up
from pool_cp.timeutil import utcnow
from pool_protocol import messages as pm

pytestmark = pytest.mark.asyncio


class FakeBuiltinRegistry:
    """Distribution in token mode, reduced to what pool uses. `images[repo][tag] = manifest bytes`."""

    def __init__(self, issuer: builtin_registry.Issuer) -> None:
        self.issuer = issuer
        self.images: dict[str, dict[str, bytes]] = {}
        self.deleted: list[tuple[str, str]] = []
        self.denied: list[str] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def _allowed(self, kind: str, name: str, action: str) -> bool:
                auth = self.headers.get("Authorization", "")
                try:
                    claims = jwt.decode(auth.removeprefix("Bearer "), outer.issuer.cert.public_key(),
                                        algorithms=["ES256"], audience=outer.issuer.service,
                                        issuer=outer.issuer.issuer)
                except jwt.PyJWTError:
                    return False
                ok = any(a["type"] == kind and a["name"] == name and (action in a["actions"] or "*" in a["actions"])
                         for a in claims.get("access", []))
                if not ok:
                    outer.denied.append(f"{kind}:{name}:{action}")
                return ok

            def _reply(self, code: int, body: bytes = b"", headers: dict | None = None):
                self.send_response(code)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _route(self):
                url = urllib.parse.urlsplit(self.path)
                if url.path == "/v2/_catalog":
                    if not self._allowed("registry", "catalog", "*"):
                        return self._reply(401)
                    return self._reply(200, json.dumps({"repositories": sorted(outer.images)}).encode())
                repo, sep, rest = url.path.removeprefix("/v2/").rpartition("/manifests/")
                if sep:
                    action = "delete" if self.command == "DELETE" else "pull"
                    if not self._allowed("repository", repo, action):
                        return self._reply(401)
                    tags = outer.images.get(repo, {})
                    body = tags.get(rest) or next((b for b in tags.values() if outer.digest(b) == rest), None)
                    if body is None:
                        return self._reply(404)
                    if self.command == "DELETE":
                        outer.deleted.append((repo, rest))
                        outer.images[repo] = {t: b for t, b in tags.items() if outer.digest(b) != rest}
                        return self._reply(202)
                    return self._reply(200, body, {"Docker-Content-Digest": outer.digest(body)})
                repo, sep, _ = url.path.removeprefix("/v2/").rpartition("/tags/list")
                if sep:
                    if not self._allowed("repository", repo, "pull"):
                        return self._reply(401)
                    return self._reply(200, json.dumps({"name": repo, "tags": sorted(outer.images.get(repo, {}))})
                                       .encode())
                return self._reply(404)

            do_GET = do_HEAD = do_DELETE = _route  # noqa: N815

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @staticmethod
    def digest(body: bytes) -> str:
        return "sha256:" + hashlib.sha256(body).hexdigest()

    def push(self, repo: str, tag: str, body: bytes | None = None) -> str:
        body = body or json.dumps({"repo": repo, "tag": tag}).encode()
        self.images.setdefault(repo, {})[tag] = body
        return self.digest(body)


ADDRESS = "registry.pool.test"


@pytest.fixture
def builtin(server, settings, monkeypatch, tmp_path):
    """Turn the built-in registry on for the test server, backed by the fake registry."""
    issuer = builtin_registry.Issuer(tmp_path / "registry.key", tmp_path / "registry.crt", settings.registry_issuer,
                                     settings.registry_service, create=True)
    reg = FakeBuiltinRegistry(issuer)
    monkeypatch.setattr(settings, "registry_address", ADDRESS)
    monkeypatch.setattr(settings, "registry_internal_url", reg.url)
    state = server["app"].state  # set by hand: monkeypatch deletes Starlette State attributes on undo
    old, state.registry_issuer = state.registry_issuer, issuer
    yield reg
    state.registry_issuer = old
    reg.server.shutdown()


def basic(name: str, token: str) -> dict:
    return {"Authorization": "Basic " + base64.b64encode(f"{name}:{token}".encode()).decode()}


async def new_group(user, role: str = "member"):
    coord, other = await user(), await user()
    g = gname()
    await coord.post("/api/v1/groups", {"name": g, "display_name": "Lab"}, expect=201)
    inv = (await coord.post(f"/api/v1/groups/{g}/invites", {"role": role, "max_uses": 1}, expect=201)).json()
    await other.post("/api/v1/invites/accept", {"token": inv["token"]}, expect=200)
    return coord, other, g


# --------------------------------------------------------------------------- tokens


async def test_tokens_are_signed_for_the_registry_and_scoped():
    import tempfile
    from pathlib import Path

    d = Path(tempfile.mkdtemp())
    issuer = builtin_registry.Issuer(d / "k", d / "c", "pool-server", "pool-registry", create=True)
    token, _ = issuer.token("alice", [{"type": "repository", "name": "lab/alice/app", "actions": ["pull"]}])
    header = jwt.get_unverified_header(token)
    assert header["alg"] == "ES256" and header["x5c"], "the registry finds the certificate in x5c"
    claims = jwt.decode(token, issuer.cert.public_key(), algorithms=["ES256"], audience="pool-registry")
    assert claims["iss"] == "pool-server" and claims["access"][0]["name"] == "lab/alice/app"
    assert claims["exp"] - claims["iat"] == builtin_registry.TOKEN_TTL_S
    assert oct((d / "k").stat().st_mode & 0o777) == "0o600" and oct((d / "c").stat().st_mode & 0o777) == "0o644"
    again = builtin_registry.Issuer(d / "k", d / "c", "pool-server", "pool-registry", create=False)
    assert again.cert == issuer.cert, "the key and certificate are kept across restarts"
    (d / "c").unlink()  # e.g. the certificate's own volume is new
    renewed = builtin_registry.Issuer(d / "k", d / "c", "pool-server", "pool-registry", create=True)
    assert renewed.cert.public_key().public_numbers() == issuer.cert.public_key().public_numbers()
    assert builtin_registry.parse_scope("repository:lab/alice/app:push,pull") == (
        "repository", "lab/alice/app", {"push", "pull"})
    assert builtin_registry.owner("lab/alice/app") == ("lab", "alice") and builtin_registry.owner("lab/app") is None
    with pytest.raises(RegistryError, match="user name"):
        builtin_registry.namespace("lab", "a._b")


async def test_members_push_and_pull_only_their_own_space(user, http, builtin, settings):
    coord, member, g = await new_group(user)
    token = member.headers["Authorization"].removeprefix("Bearer ")
    mine, theirs = f"{g}/{member.handle}/app", f"{g}/{coord.handle}/app"
    q = {"service": settings.registry_service, "scope": [f"repository:{mine}:push,pull",
                                                         f"repository:{theirs}:pull", "registry:catalog:*",
                                                         f"repository:{mine}:delete"]}
    r = await http.get("/api/v1/registry/token", params=q, headers=basic(member.handle, token))
    assert r.status_code == 200, r.text
    from datetime import datetime
    issued = datetime.fromisoformat(r.json()["issued_at"])
    assert abs((issued - utcnow()).total_seconds()) < 30, "issued_at is now: Docker adds expires_in to it"
    claims = jwt.decode(r.json()["token"], builtin.issuer.cert.public_key(), algorithms=["ES256"],
                        audience=settings.registry_service)
    got: dict[str, set] = {}
    for a in claims["access"]:
        got.setdefault(a["name"], set()).update(a["actions"])
    assert got == {mine: {"pull", "push"}, theirs: set()}, "no catalog, no delete, nothing of other people"

    for headers, words in ((basic(member.handle, "not-a-token"), "not valid"), (basic(coord.handle, token), "must be"),
                           ({}, "sign in")):
        r = await http.get("/api/v1/registry/token", params=q, headers=headers)
        assert r.status_code == 401 and words in r.text and r.headers["www-authenticate"].startswith("Basic")
    r = await http.get("/api/v1/registry/token", params={**q, "service": "other"}, headers=basic(member.handle, token))
    assert r.status_code == 401


async def test_contributors_and_outsiders_get_nothing(user, http, builtin, settings):
    _, contributor, g = await new_group(user, role="contributor")  # shares computers, may not submit jobs
    outsider = await user()
    for who in (contributor, outsider):
        token = who.headers["Authorization"].removeprefix("Bearer ")
        r = await http.get("/api/v1/registry/token", headers=basic(who.handle, token),
                           params={"service": settings.registry_service,
                                   "scope": f"repository:{g}/{who.handle}/app:push,pull"})
        access = jwt.decode(r.json()["token"], options={"verify_signature": False})["access"]
        assert access == [{"type": "repository", "name": f"{g}/{who.handle}/app", "actions": []}]


async def test_where_to_push_and_the_retention(user, builtin, settings):
    coord, member, g = await new_group(user)
    info = await member.get(f"/api/v1/groups/{g}/registry")
    assert info == {"enabled": True, "address": ADDRESS, "repository_prefix": f"{ADDRESS}/{g}/{member.handle}",
                    "retention_days": settings.registry_retention_days, "group_retention_days": None,
                    "default_retention_days": settings.registry_retention_days}
    await member.req("PATCH", f"/api/v1/groups/{g}", json={"registry_retention_days": 7}, expect=403)
    await coord.req("PATCH", f"/api/v1/groups/{g}", json={"registry_retention_days": 7}, expect=200)
    assert (await member.get(f"/api/v1/groups/{g}/registry"))["retention_days"] == 7


# --------------------------------------------------------------------------- jobs and computers


async def test_jobs_use_only_my_own_images_pinned_from_the_registry(group_with_device, user, builtin, db):
    owner, g, agent = await group_with_device()
    inv = (await owner.post(f"/api/v1/groups/{g}/invites", {"role": "member", "max_uses": 1}, expect=201)).json()
    other = await user()
    await other.post("/api/v1/invites/accept", {"token": inv["token"]}, expect=200)
    repo = f"{g}/{owner.handle}/trainer"
    digest = builtin.push(repo, "3")

    r = await other.post(f"/api/v1/groups/{g}/jobs", {"image": f"{ADDRESS}/{repo}:3"}, expect=422)
    assert "your own images" in r.json()["detail"], "another member cannot run my image"
    r = await owner.post(f"/api/v1/groups/{g}/jobs", {"image": f"{ADDRESS}/{repo}:4"}, expect=422)
    assert "push it first" in r.json()["detail"]
    job = (await owner.post(f"/api/v1/groups/{g}/jobs", {"image": f"{ADDRESS}/{repo}:3", "args": "--epochs 2"},
                            expect=201)).json()
    assert job["image"] == f"{ADDRESS}/{repo}:3@{digest}"
    used = (await db.execute(select(RegistryImage).where(RegistryImage.repository == repo))).scalar_one()
    assert used.digest == digest and used.last_used_at is not None

    await agent.connect([share(g)])
    offer = await agent.ready()
    env = agent.open_envelope(offer)
    assert env.registry_auth is True and env.spec.args == ["--epochs", "2"]
    await agent.send(pm.Accept(attempt_id=offer.attempt_id, epoch=offer.epoch, tier="T2"))
    await agent.send(pm.PullAuthRequest(attempt_id=offer.attempt_id, epoch=offer.epoch))
    auth = await agent.recv_type(pm.PullAuth)
    assert auth.registry == ADDRESS and auth.reason is None
    claims = jwt.decode(auth.token, builtin.issuer.cert.public_key(), algorithms=["ES256"],
                        audience=builtin.issuer.service)
    assert claims["access"] == [{"type": "repository", "name": repo, "actions": ["pull"]}]
    assert claims["exp"] - claims["iat"] == builtin_registry.DEVICE_PULL_TTL_S, "long enough for a big image"
    r = await owner.post(f"/api/v1/groups/{g}/jobs", {"image": f"{ADDRESS}/{g}/{owner.handle}/../x/y:1"}, expect=422)
    assert "not a valid name" in r.json()["detail"]


async def test_allowlist_does_not_block_the_groups_own_registry(user, builtin):
    c = await user()
    g = gname()
    await c.post("/api/v1/groups", {"name": g, "display_name": "x", "registry_allowlist": ["ghcr.io/lab/"]},
                 expect=201)
    digest = builtin.push(f"{g}/{c.handle}/app", "1")
    await c.post(f"/api/v1/groups/{g}/jobs", {"image": f"{ADDRESS}/{g}/{c.handle}/app:1"}, expect=201)
    assert digest


# --------------------------------------------------------------------------- clean-up


async def test_clean_up_deletes_unused_images_and_orphans_but_never_what_a_job_needs(user, builtin, settings,
                                                                                         db_maker):
    coord, _, g = await new_group(user)
    await coord.req("PATCH", f"/api/v1/groups/{g}", json={"registry_retention_days": 10}, expect=200)
    fresh = builtin.push(f"{g}/{coord.handle}/fresh", "1")
    stale = builtin.push(f"{g}/{coord.handle}/stale", "1")
    queued = builtin.push(f"{g}/{coord.handle}/queued", "1")
    orphan = builtin.push(f"gone-{gname()}/someone/app", "1")
    await coord.post(f"/api/v1/groups/{g}/jobs", {"image": f"{ADDRESS}/{g}/{coord.handle}/queued:1"}, expect=201)
    reg = builtin_registry.Registry(settings, builtin.issuer)

    assert (await clean_up(db_maker, settings, reg)) == {"seen": 4, "deleted": 1}, "an orphan's image goes at once"
    assert [d for _, d in builtin.deleted] == [orphan]
    async with db_maker() as s:  # 11 days later: only the stale one has not been used
        await s.execute(update(RegistryImage).values(first_seen_at=utcnow() - timedelta(days=11),
                                                     last_used_at=None))
        await s.execute(update(RegistryImage).where(RegistryImage.digest == fresh)
                        .values(last_used_at=utcnow() - timedelta(days=2)))
        await s.commit()
    assert (await clean_up(db_maker, settings, reg))["deleted"] == 1
    assert [d for _, d in builtin.deleted] == [orphan, stale], "the queued job's image stays, however old"
    assert queued not in [d for _, d in builtin.deleted]
    async with db_maker() as s:
        tags = {r.digest: r.tag for r in (await s.execute(select(RegistryImage))).scalars()}
    assert tags[fresh] == "1", "tags are remembered, so the next scan skips the lookup"
    assert not builtin.denied, f"the server's own tokens were refused: {builtin.denied}"
