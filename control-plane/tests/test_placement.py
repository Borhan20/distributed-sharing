"""Where work goes (docs/design/pool-decides.md §4.4, step D): fair share first, then the best fit."""

import secrets
import uuid

import pytest
from fake_agent import CAPS, FREE, share
from sqlalchemy import update

from pool_cp.models import Device
from pool_protocol import messages as pm

pytestmark = pytest.mark.asyncio


def img() -> tuple[str, str]:
    digest = "sha256:" + secrets.token_hex(32)
    return f"registry.example.org/lab/app:1@{digest}", digest


async def submit(c, g, image, **extra):
    body = {"image": image, "cpu": 1, "memory": "512Mi", "time_limit": "1h", **extra}
    return (await c.post(f"/api/v1/groups/{g}/jobs", body, expect=201)).json()


async def job_of(agent, offer) -> str:
    return agent.open_envelope(offer).job_id


async def member(coord, g, user):
    other = await user()
    inv = (await coord.post(f"/api/v1/groups/{g}/invites", {"role": "member"}, expect=201)).json()
    await other.post("/api/v1/invites/accept", {"token": inv["token"]}, expect=200)
    return other


async def test_a_gpu_computer_takes_gpu_work_first(group_with_device, user):
    coord, g, agent = await group_with_device()
    light = await member(coord, g, user)  # has used nothing: first by fair share
    cpu_job = await submit(light, g, img()[0])
    gpu_job = await submit(coord, g, img()[0], gpu=1)
    gpu_share = {**share(g), "limits": {**share(g)["limits"], "gpu": True}}
    await agent.connect([gpu_share], caps={**CAPS, "gpus": [{"vendor": "nvidia", "model": "x", "memory_mb": 8192}],
                                           "gpu_tiers": ["T1", "T2"]})
    offer = await agent.ready(free={**FREE, "gpus": 1})
    assert await job_of(agent, offer) == gpu_job["id"], "the GPU waits for GPU work, not the next CPU task"
    assert cpu_job["id"]
    await agent.close()


async def test_a_big_computer_takes_work_that_needs_one_first(group_with_device, user):
    coord, g, agent = await group_with_device()
    light = await member(coord, g, user)
    small = await submit(light, g, img()[0])
    big = await submit(coord, g, img()[0], memory="20Gi")
    await agent.connect([share(g)])
    offer = await agent.ready()  # 32 GiB free: the 20 GiB job needs a computer like this one
    assert await job_of(agent, offer) == big["id"]
    assert small["id"]
    await agent.close()


async def test_a_computer_that_has_the_image_takes_that_work_first(group_with_device):
    coord, g, agent = await group_with_device()
    first, _ = img()
    second, digest = img()
    await submit(coord, g, first)
    cached = await submit(coord, g, second)
    await agent.connect([share(g)])
    offer = await agent.ready(images=[digest])
    assert await job_of(agent, offer) == cached["id"], "no download: it starts at once"
    await agent.close()


async def test_a_computer_that_often_loses_work_takes_short_work_first(group_with_device, db):
    coord, g, agent = await group_with_device()
    long = await submit(coord, g, img()[0], time_limit="6h")
    short = await submit(coord, g, img()[0], time_limit="10m")
    await db.execute(update(Device).where(Device.id == uuid.UUID(agent.device_id)).values(reliability=0.3))
    await db.commit()
    await agent.connect([share(g)])
    offer = await agent.ready()
    assert await job_of(agent, offer) == short["id"]
    assert long["id"]
    await agent.close()


async def test_fair_share_still_decides_between_people(group_with_device, user):
    coord, g, agent = await group_with_device()
    light = await member(coord, g, user)
    await agent.connect([share(g)])
    used, digest = img()
    await submit(coord, g, used)
    assert (await agent.run_task(await agent.ready())).accepted  # coord has now used the computer
    mine = await submit(coord, g, img()[0])
    theirs = await submit(light, g, img()[0])
    offer = await agent.ready(images=[digest])
    assert await job_of(agent, offer) == theirs["id"], "who used least goes first; a cached image does not jump it"
    assert mine["id"]
    await agent.close()


# ---- from the security review of step D ----

async def used(db, g, who, hours: float, gpu_hours: float = 0):
    """Record use in the group's fair-share ledger, as if `who` ran things for that long."""
    from sqlalchemy import text as sql

    await db.execute(sql(
        "INSERT INTO usage_ledger (group_id, user_id, device_id, attempt_id, cpu_seconds, gpu_seconds, pool_paid) "
        "SELECT g.id, u.id, d.id, gen_random_uuid(), :cpu, :gpu, false FROM groups g, users u, devices d "
        "WHERE g.name = :g AND u.handle = :h LIMIT 1"), {"g": g, "h": who.handle, "cpu": hours * 3600,
                                                          "gpu": gpu_hours * 3600})
    await db.commit()


async def test_asking_for_more_memory_does_not_jump_someone_who_used_much_less(group_with_device, user, db):
    coord, g, agent = await group_with_device()
    light = await member(coord, g, user)
    await used(db, g, coord, hours=1000)
    small = await submit(light, g, img()[0])
    for size in ("1Gi", "4Gi", "16Gi"):  # a "size ladder" of memory nobody needs
        await submit(coord, g, img()[0], memory=size)
    await agent.connect([share(g)])
    assert await job_of(agent, await agent.ready()) == small["id"], "fair share first; best fit only breaks ties"
    await agent.close()


async def test_gpu_time_counts_in_fair_share(group_with_device, user, db):
    coord, g, agent = await group_with_device()
    gpu_user = await member(coord, g, user)
    await used(db, g, gpu_user, hours=0, gpu_hours=1)  # 1 GPU hour = 10 CPU hours
    await used(db, g, coord, hours=5)
    mine = await submit(coord, g, img()[0])
    await submit(gpu_user, g, img()[0])
    await agent.connect([share(g)])
    assert await job_of(agent, await agent.ready()) == mine["id"]
    await agent.close()


async def test_a_computer_still_gets_work_when_the_top_jobs_cannot_be_paid(user, enrolled, db):
    coord = await user()
    g = __import__("conftest").gname()
    await coord.post("/api/v1/groups", {"name": g, "display_name": "W", "template": "dev-team"}, expect=201)
    await coord.req("PATCH", f"/api/v1/groups/{g}", 200, json={"out_of_credits": "wait"})
    poor = await member(coord, g, user)
    rich = await member(coord, g, user)
    from sqlalchemy import text as sql

    await db.execute(sql("UPDATE credit_balances SET balance = 1, free = 1 WHERE user_id = "
                         "(SELECT id FROM users WHERE handle = :h)"), {"h": poor.handle})
    await db.commit()
    for _ in range(12):  # 20 GiB jobs: 6 credits an hour each, more than 1 credit
        await submit(poor, g, img()[0], memory="20Gi")
    ok = await submit(rich, g, img()[0])
    agent = await enrolled(coord)
    await coord.req("PUT", f"/api/v1/devices/{agent.device_id}/shares/{g}", expect=200)
    await agent.connect([share(g)])
    offer = await agent.ready()
    assert isinstance(offer, pm.Offer) and await job_of(agent, offer) == ok["id"]
    await agent.close()


async def test_a_bad_image_entry_is_dropped_not_the_request(group_with_device):
    coord, g, agent = await group_with_device()
    job = await submit(coord, g, img()[0])
    await agent.connect([share(g)])
    await agent.ws.send(__import__("json").dumps({"type": "ready", "msg_id": "m-1", "free": FREE,
                                                  "tiers": ["T1", "T2"], "images": ["not-a-digest"]}))
    offer = await agent.recv_type(pm.Offer, pm.NoWork)
    assert isinstance(offer, pm.Offer) and await job_of(agent, offer) == job["id"]
    await agent.close()
