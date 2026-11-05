"""Where work goes (docs/design/pool-decides.md §4.4, step D): fair share first, then the best fit."""

import secrets
import uuid

import pytest
from fake_agent import CAPS, FREE, share
from sqlalchemy import update

from pool_cp.models import Device

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
