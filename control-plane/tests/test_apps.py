"""Apps: jobs that listen on ports, reached only through the server's tunnel (docs/design/pool-run.md step 2)."""

import asyncio
import uuid
from contextlib import AsyncExitStack

import pytest
import websockets
from fake_agent import share
from sqlalchemy import func, select, update

from pool_cp.api import apps
from pool_cp.models import Attempt, AuditEvent
from pool_cp.registry import ImageInfo
from pool_protocol import messages as pm

pytestmark = pytest.mark.asyncio

IMG = "registry.example.org/lab/web:1@sha256:" + "f" * 64


def app_share(group: str, hours: float = 8) -> dict:
    return {**share(group), "allow_apps": True, "app_hours": hours}


async def start_app(coord, g, agent, **extra):
    """Submit an app and run it on the fake computer (started, not ended). Returns (job, offer)."""
    job = (await coord.post(f"/api/v1/groups/{g}/jobs",
                            {"image": IMG, "port": 8000, "memory": "1Gi", **extra}, expect=201)).json()
    offer = await agent.ready()
    assert isinstance(offer, pm.Offer)
    await agent.send(pm.Accept(attempt_id=offer.attempt_id, epoch=offer.epoch, tier="T2"))
    await agent.send(pm.Started(attempt_id=offer.attempt_id, epoch=offer.epoch))
    await asyncio.sleep(0.1)
    return job, offer


def ws_url(base_url: str, path: str) -> str:
    return base_url.replace("http", "ws", 1) + path


def agent_side(base_url: str, msg: pm.TunnelOpen, secret: str | None = None):
    """The computer's side of a tunnel: the one-time secret goes in a header, never in the URL."""
    return websockets.connect(ws_url(base_url, f"/api/v1/agent/tunnels/{msg.tunnel_id}"),
                              additional_headers={"X-Pool-Tunnel-Secret": secret or msg.secret})


async def test_an_app_waits_for_a_computer_that_allows_apps(group_with_device):
    coord, g, agent = await group_with_device()
    job = (await coord.post(f"/api/v1/groups/{g}/jobs", {"image": IMG, "port": 8000}, expect=201)).json()
    assert job["ports"] == [8000]
    await agent.connect([share(g)])
    assert isinstance(await agent.ready(), pm.NoWork), "apps only where the owner allows them"
    await agent.heartbeat([], shares=[share(g)])
    w = await coord.get(f"/api/v1/jobs/{job['id']}/waiting")
    assert w["reasons"][0]["code"] == "apps" and "--allow-apps" in w["reasons"][0]["hint"]
    await agent.heartbeat([], shares=[app_share(g, hours=4)])
    assert isinstance(await agent.ready(), pm.NoWork), "an app runs up to 8 h: 4 h allowed is not enough"
    await agent.heartbeat([], shares=[app_share(g)])
    offer = await agent.ready()
    spec = agent.open_envelope(offer).spec
    assert spec.ports == [8000] and spec.max_runtime_s == 8 * 3600
    await agent.close()


async def test_the_image_decides_unless_the_job_does(group_with_device, monkeypatch):
    info = ImageInfo(platforms=["linux/amd64"], cuda=False, ports=["80/tcp", "9000/udp"], healthcheck=False,
                     size_mb=10)
    monkeypatch.setattr("pool_cp.api.jobs.inspect_image", lambda image, cred, **kw: info)
    coord, g, agent = await group_with_device()
    exposed = "registry.example.org/lab/nginx:1@sha256:" + uuid.uuid4().hex * 2
    job = (await coord.post(f"/api/v1/groups/{g}/jobs", {"image": exposed}, expect=201)).json()
    assert job["ports"] == [80], "TCP ports from EXPOSE"
    job = (await coord.post(f"/api/v1/groups/{g}/jobs", {"image": exposed, "ports": []}, expect=201)).json()
    assert job["ports"] is None, "ports: [] keeps a program"
    job = (await coord.post(f"/api/v1/groups/{g}/jobs", {"image": exposed, "parts": 2}, expect=201)).json()
    assert job["ports"] is None, "parts: a program"
    await coord.post(f"/api/v1/groups/{g}/jobs", {"image": exposed, "port": 80, "parts": 2}, expect=422)


async def test_a_tunnel_carries_bytes_both_ways(group_with_device, base_url):
    coord, g, agent = await group_with_device()
    await agent.connect([app_share(g)])
    job, offer = await start_app(coord, g, agent)

    async def person() -> bytes:
        async with websockets.connect(ws_url(base_url, f"/api/v1/jobs/{job['id']}/tunnel?port=8000"),
                                      additional_headers=coord.headers) as ws:
            await ws.send(b"GET / HTTP/1.0\r\n\r\n")
            return await ws.recv()

    user = asyncio.create_task(person())
    msg = await agent.recv_type(pm.TunnelOpen)
    assert msg.attempt_id == offer.attempt_id and msg.port == 8000
    async with agent_side(base_url, msg) as t:
        assert await t.recv() == b"GET / HTTP/1.0\r\n\r\n"
        await t.send(b"HTTP/1.0 200 OK\r\n\r\nhello")
        assert await asyncio.wait_for(user, 10) == b"HTTP/1.0 200 OK\r\n\r\nhello"
    await agent.close()


async def test_only_the_person_who_started_it_can_open_it(group_with_device, user, base_url):
    coord, g, agent = await group_with_device()
    await agent.connect([app_share(g)])
    job, _ = await start_app(coord, g, agent)
    other = await user()
    inv = (await coord.post(f"/api/v1/groups/{g}/invites", {"role": "admin"}, expect=201)).json()
    await other.post("/api/v1/invites/accept", {"token": inv["token"]}, expect=200)
    url = ws_url(base_url, f"/api/v1/jobs/{job['id']}/tunnel?port=8000")
    for headers in (other.headers, {}, {"Authorization": "Bearer nonsense"}):
        with pytest.raises(websockets.exceptions.InvalidStatus):
            async with websockets.connect(url, additional_headers=headers):
                pass
    with pytest.raises(websockets.exceptions.InvalidStatus):
        async with websockets.connect(url.replace("port=8000", "port=22"), additional_headers=coord.headers):
            pass
    # A guessed tunnel id or secret is refused.
    with pytest.raises(websockets.exceptions.InvalidStatus):
        async with agent_side(base_url, pm.TunnelOpen(tunnel_id="guess", attempt_id="a", port=1, secret="x" * 40)):
            pass
    await agent.close()


async def test_a_wrong_secret_does_not_use_up_the_tunnel(group_with_device, base_url):
    """Security review of step 2: a wrong secret once took the tunnel away from the real computer."""
    coord, g, agent = await group_with_device()
    await agent.connect([app_share(g)])
    job, _ = await start_app(coord, g, agent)

    async def person() -> bytes:
        async with websockets.connect(ws_url(base_url, f"/api/v1/jobs/{job['id']}/tunnel?port=8000"),
                                      additional_headers=coord.headers) as ws:
            return await ws.recv()

    user = asyncio.create_task(person())
    msg = await agent.recv_type(pm.TunnelOpen)
    with pytest.raises(websockets.exceptions.InvalidStatus):
        async with agent_side(base_url, msg, secret="x" * 43):
            pass
    with pytest.raises(websockets.exceptions.InvalidStatus):  # in the URL: not accepted
        async with websockets.connect(ws_url(base_url, f"/api/v1/agent/tunnels/{msg.tunnel_id}?secret={msg.secret}")):
            pass
    async with agent_side(base_url, msg) as t:
        await t.send(b"still works")
        assert await asyncio.wait_for(user, 10) == b"still works"
    await agent.close()


async def test_limits_hold_when_many_open_at_once(group_with_device, base_url, monkeypatch, db):
    """Security review of step 2: opened at the same moment, tunnels all passed the per-person limit."""
    monkeypatch.setattr(apps, "AGENT_ANSWER_S", 2.0)
    coord, g, agent = await group_with_device()
    await agent.connect([app_share(g)])
    job, _ = await start_app(coord, g, agent)
    url = ws_url(base_url, f"/api/v1/jobs/{job['id']}/tunnel?port=8000")

    async def one() -> str:
        try:
            async with websockets.connect(url, additional_headers=coord.headers, open_timeout=10):
                return "open"
        except websockets.exceptions.InvalidStatus:
            return "refused"

    results = await asyncio.gather(*[one() for _ in range(apps.MAX_TUNNELS_PER_PERSON + 15)])
    asked = 0
    while True:  # the computer was asked once per counted tunnel, never more
        try:
            await agent.recv_type(pm.TunnelOpen, timeout=0.5)
            asked += 1
        except (TimeoutError, AssertionError):
            break
    assert asked == apps.MAX_TUNNELS_PER_PERSON and results.count("refused") == len(results), (asked, results)
    rows = (await db.execute(select(func.count()).select_from(AuditEvent)
                             .where(AuditEvent.action == "app.tunnel",
                                    AuditEvent.target_id == job["id"]))).scalar_one()
    assert rows == 1, "one audit row per person, app and port every 10 minutes, with the number of connections"
    # New tunnels per minute are limited too.
    monkeypatch.setattr(apps, "OPENS_PER_MINUTE", 1)
    with pytest.raises(websockets.exceptions.InvalidStatus):
        async with websockets.connect(url, additional_headers=coord.headers):
            pass
    await agent.close()


async def test_an_open_tunnel_closes_when_the_app_stops_running(group_with_device, base_url, monkeypatch, db):
    """Security review of step 2: an open tunnel outlived the app (and the member) as long as both sides lived."""
    monkeypatch.setattr(apps, "RECHECK_S", 0.2)
    coord, g, agent = await group_with_device()
    await agent.connect([app_share(g)])
    job, offer = await start_app(coord, g, agent)
    async with AsyncExitStack() as stack:

        async def person() -> int | None:
            async with websockets.connect(ws_url(base_url, f"/api/v1/jobs/{job['id']}/tunnel?port=8000"),
                                          additional_headers=coord.headers) as ws:
                await ws.send(b"one")
                await asyncio.wait_for(ws.wait_closed(), 10)
                return ws.close_code

        user_task = asyncio.create_task(person())
        msg = await agent.recv_type(pm.TunnelOpen)
        t = await stack.enter_async_context(agent_side(base_url, msg))
        assert await t.recv() == b"one"
        await db.execute(update(Attempt).where(Attempt.id == uuid.UUID(offer.attempt_id)).values(state="lost"))
        await db.commit()
        assert await asyncio.wait_for(user_task, 10) is not None, "the tunnel is closed by the server"
        await asyncio.wait_for(t.wait_closed(), 10)
    await agent.close()


async def test_a_tunnel_to_an_app_that_is_not_running_is_refused(group_with_device, base_url):
    coord, g, agent = await group_with_device()
    job = (await coord.post(f"/api/v1/groups/{g}/jobs", {"image": IMG, "port": 8000}, expect=201)).json()
    with pytest.raises(websockets.exceptions.InvalidStatus):
        async with websockets.connect(ws_url(base_url, f"/api/v1/jobs/{job['id']}/tunnel?port=8000"),
                                      additional_headers=coord.headers):
            pass
