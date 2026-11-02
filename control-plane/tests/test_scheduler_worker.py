"""Scheduler worker: one active leader, startup grace period, lease expiry, job roll-up."""

import asyncio
import uuid

import pytest
from conftest import job_spec
from fake_agent import share
from sqlalchemy import select, text, update

from pool_cp.models import Attempt, Task
from pool_cp.scheduler.__main__ import run
from pool_protocol import messages as pm

pytestmark = pytest.mark.asyncio


async def test_worker_expires_after_grace_and_single_leader(group_with_device, db, settings):
    coord, g, agent = await group_with_device()
    await coord.post(f"/api/v1/groups/{g}/jobs", job_spec(), expect=201)
    await agent.connect([share(g)])
    o = await agent.ready()
    await agent.send(pm.Accept(attempt_id=o.attempt_id, epoch=1, tier="T2"))
    await agent.heartbeat([(o.attempt_id, 1, "assigned")])
    await db.execute(update(Attempt).where(Attempt.id == uuid.UUID(o.attempt_id))
                     .values(lease_expires_at=text("now() - interval '5 seconds'")))
    await db.commit()

    fast = settings.model_copy(update={"lease_ttl_s": 1, "scheduler_interval_s": 0.1})
    ticks = {"a": 0, "b": 0}
    stop = asyncio.Event()
    leader = asyncio.create_task(run(stop, fast, on_tick=lambda: ticks.__setitem__("a", ticks["a"] + 1)))
    await asyncio.sleep(0.5)
    follower = asyncio.create_task(run(stop, fast, on_tick=lambda: ticks.__setitem__("b", ticks["b"] + 1)))
    # Within the 1 s grace period the expired lease must survive.
    a = await db.get(Attempt, uuid.UUID(o.attempt_id))
    await db.refresh(a)
    assert a.state == "assigned"
    for _ in range(50):
        await asyncio.sleep(0.1)
        await db.refresh(a)
        if a.state == "lost":
            break
    assert a.state == "lost"
    t = (await db.execute(select(Task).where(Task.id == uuid.UUID(o.task_id)))).scalar_one()
    assert t.state == "pending"
    assert ticks["a"] > 0 and ticks["b"] == 0  # the second worker waits for the lock
    stop.set()
    await asyncio.wait_for(asyncio.gather(leader, follower), 15)
    await agent.close()


async def test_api_alive_reflects_heartbeats(db, settings, server):
    from pool_cp.scheduler.core import api_alive

    assert await api_alive(db, settings)  # the test server writes heartbeats
    await db.execute(text("UPDATE service_heartbeats SET at = now() - interval '10 minutes'"))
    await db.commit()
    assert not await api_alive(db, settings)
    await db.execute(text("UPDATE service_heartbeats SET at = now()"))
    await db.commit()
    assert await api_alive(db, settings)
