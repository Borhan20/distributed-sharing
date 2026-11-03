from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
import threading
import time
import urllib.parse
import uuid
from collections.abc import AsyncIterator

import asyncpg
import httpx
import pytest
import uvicorn
from sqlalchemy.ext.asyncio import AsyncSession

from pool_cp.app import create_app
from pool_cp.config import Settings
from pool_cp.db import make_engine, make_sessionmaker
from pool_cp.migrate import upgrade

ADMIN_URL = os.environ.get("POOL_TEST_ADMIN_DSN", "postgresql://postgres@127.0.0.1:55432/postgres")
IMG = "registry.example.org/test/app@sha256:" + "b" * 64


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def database_url() -> str:
    name = f"pool_test_{uuid.uuid4().hex[:8]}"

    async def create() -> None:
        conn = await asyncpg.connect(ADMIN_URL)
        await conn.execute(f'CREATE DATABASE "{name}"')
        await conn.close()

    async def drop() -> None:
        conn = await asyncpg.connect(ADMIN_URL)
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await conn.close()

    asyncio.run(create())
    url = ADMIN_URL.rsplit("/", 1)[0].replace("postgresql://", "postgresql+asyncpg://") + f"/{name}"
    upgrade(url)
    yield url
    if not os.environ.get("POOL_TEST_KEEP_DB"):
        asyncio.run(drop())


@pytest.fixture(autouse=True, scope="session")
def no_internet_registries():
    """Tests talk only to registries on this machine (fake ones); a tag such as python:3.12 must never send
    a test to Docker Hub. The server thread shares this process, so the guard covers API calls too."""
    from pool_cp import registry

    real = registry._check_address

    def local_only(url: str, allow_private: bool) -> None:
        host = urllib.parse.urlsplit(url).hostname or ""
        try:
            public = ipaddress.ip_address(host).is_global
        except ValueError:  # a name: only localhost resolves without the network
            public = host != "localhost"
        if public:
            raise registry.RegistryError(f"tests must not contact {host}")
        real(url, allow_private)

    registry._check_address = local_only
    yield
    registry._check_address = real


@pytest.fixture(scope="session")
def settings(database_url, tmp_path_factory) -> Settings:
    return Settings(database_url=database_url, dev_mode=True, data_dir=tmp_path_factory.mktemp("cp-data"),
                    lease_ttl_s=60, heartbeat_interval_s=15, retry_base_s=0.05, retry_max_s=0.2,
                    server_admins=["root-admin"], submit_rate_per_minute=30,
                    enroll_rate_per_minute=100000)


@pytest.fixture(scope="session")
def server(settings):
    """A real uvicorn server in a thread (its own event loop), like production."""
    app = create_app(settings)
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", ws_max_size=2**20, lifespan="on")
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.time() + 20
    while not srv.started:
        if time.time() > deadline:
            raise RuntimeError("server did not start")
        time.sleep(0.05)
    yield {"url": f"http://127.0.0.1:{port}", "app": app}
    srv.should_exit = True
    thread.join(timeout=10)


@pytest.fixture(scope="session")
def base_url(server) -> str:
    return server["url"]


@pytest.fixture
async def http(base_url) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=base_url, timeout=20) as c:
        yield c


@pytest.fixture(scope="session")
async def db_maker(database_url):
    engine = make_engine(database_url)
    yield make_sessionmaker(engine)
    await engine.dispose()


@pytest.fixture
async def db(db_maker) -> AsyncIterator[AsyncSession]:
    async with db_maker() as s:
        yield s


class Client:
    """An authenticated API client for one synthetic user."""

    def __init__(self, http: httpx.AsyncClient, username: str, token: str) -> None:
        self.http = http
        self.username = username
        self.headers = {"Authorization": f"Bearer {token}"}

    async def req(self, method: str, path: str, expect: int | None = None, **kw) -> httpx.Response:
        r = await self.http.request(method, path, headers=self.headers, **kw)
        if expect is not None:
            assert r.status_code == expect, f"{method} {path}: {r.status_code} {r.text}"
        return r

    async def get(self, path, expect=200, **kw):
        return (await self.req("GET", path, expect, **kw)).json()

    async def post(self, path, json=None, expect=None, **kw):
        return await self.req("POST", path, expect, json=json, **kw)


@pytest.fixture
def user(http):
    async def make(name: str | None = None, mfa: bool = True) -> Client:
        name = name or f"u{uuid.uuid4().hex[:8]}"
        r = await http.post("/dev/token", json={"username": name, "mfa": mfa})
        assert r.status_code == 200, r.text
        c = Client(http, name, r.json()["access_token"])
        me = await c.get("/api/v1/me")
        c.handle = me["handle"]
        return c

    return make


def gname() -> str:
    return f"g-{uuid.uuid4().hex[:10]}"


def job_spec(name: str = "job", **spec) -> dict:
    base = {"image": IMG, "command": ["sh", "-c", "echo hi"], "resources": {"cpu": 1, "memory": "512Mi"},
            "isolation": {"min_tier": "T1"}, "retries": 2, "max_runtime": "10m"}
    base.update(spec)
    return {"apiVersion": "pool/v1alpha1", "kind": "Job", "metadata": {"name": name}, "spec": base}


@pytest.fixture
def enrolled(http, base_url):
    from fake_agent import FakeAgent

    async def make(owner: Client, name: str = "dev") -> FakeAgent:
        code = (await owner.post("/api/v1/devices/enrollment-codes", expect=201)).json()["code"]
        agent = FakeAgent(base_url, name)
        r = await agent.enroll(http, code)
        assert r.status_code == 200, r.text
        return agent

    return make


@pytest.fixture
def group_with_device(user, enrolled):
    """A coordinator, a group, and one enrolled device shared with it (not yet connected)."""

    async def make(**group_kw):
        coord = await user()
        name = gname()
        await coord.post("/api/v1/groups", {"name": name, "display_name": "Test", **group_kw}, expect=201)
        agent = await enrolled(coord)
        await coord.req("PUT", f"/api/v1/devices/{agent.device_id}/shares/{name}", expect=200)
        return coord, name, agent

    return make


@pytest.fixture
def cli(base_url, tmp_path, monkeypatch):
    """Run the `pool` CLI against the test server, signed in as a new user."""
    from typer.testing import CliRunner

    from pool_cli.main import app

    monkeypatch.setenv("POOL_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.delenv("POOL_TOKEN", raising=False)
    monkeypatch.delenv("POOL_SERVER", raising=False)
    runner = CliRunner()

    def run(*args, ok=True):
        res = runner.invoke(app, list(args), catch_exceptions=not ok)
        if ok:
            assert res.exit_code == 0, res.output
        return res

    run("login", "--server", base_url, "--dev-user", f"cli-{gname()}")
    run.server = base_url
    return run
