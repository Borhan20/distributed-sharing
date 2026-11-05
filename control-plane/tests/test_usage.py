"""What a run used: stored from the device's commit, capped at the task's size, shown on the job (pool-decides §4.2)."""

import json
import uuid

import pytest
from conftest import job_spec
from fake_agent import share

from pool_cp.models import Attempt
from pool_cp.scheduler import core
from pool_cp.scheduler.usage import bounded_usage, summarize
from pool_protocol import messages as pm

MEASURED = {"peak_memory_mb": 300, "cpu_seconds": 40.0, "peak_cpu": 0.9, "wall_seconds": 50.0,
            "output_bytes": 3, "image_mb": 120, "oom_killed": False, "samples": 48}


async def submit(c, group, **spec):
    return (await c.post(f"/api/v1/groups/{group}/jobs", job_spec(**spec), expect=201)).json()


@pytest.mark.asyncio
async def test_usage_is_stored_and_shown_next_to_the_size_asked_for(group_with_device, db):
    coord, g, agent = await group_with_device()
    job = await submit(coord, g)
    await agent.connect([share(g)])
    offer = await agent.ready()
    assert (await agent.run_task(offer, usage=MEASURED)).accepted
    a = await db.get(Attempt, uuid.UUID(offer.attempt_id))
    await db.refresh(a)
    assert a.usage["peak_memory_mb"] == 300 and a.usage["avg_cpu"] == 0.8 and a.usage["capped"] == []
    await core.rollup_jobs(db)
    await db.commit()
    j = await coord.get(f"/api/v1/jobs/{job['id']}")
    # Disk is left out of the test's job, so pool chose it: 1 GiB on a first run.
    assert j["requested"] == {"cpu": 1.0, "memory_mb": 512, "disk_mb": 1024, "gpus": 0, "time_limit_s": 600,
                              "first_run": False}
    assert j["used"]["peak_memory_mb"] == 300 and j["used"]["measured"] == 1 and j["used"]["max_wall_seconds"] <= 50
    tasks = await coord.get(f"/api/v1/jobs/{job['id']}/tasks")
    assert tasks[0]["usage"]["peak_memory_mb"] == 300
    listed = await coord.get(f"/api/v1/groups/{g}/jobs")
    assert listed[0]["used"] is None, "the job list stays cheap: no per-job usage query"
    await agent.close()


@pytest.mark.asyncio
async def test_a_device_cannot_claim_more_than_the_task_was_given(group_with_device, db):
    """A dishonest device may only make a run look smaller: every number is capped at the task's limits."""
    coord, g, agent = await group_with_device()
    await submit(coord, g)
    await agent.connect([share(g)])
    offer = await agent.ready()
    inflated = {**MEASURED, "peak_memory_mb": 10**6, "peak_cpu": 64, "cpu_seconds": 10**6, "wall_seconds": 10**6,
                "output_bytes": 2**40}
    assert (await agent.run_task(offer, usage=inflated)).accepted
    a = await db.get(Attempt, uuid.UUID(offer.attempt_id))
    await db.refresh(a)
    u = a.usage
    assert u["peak_memory_mb"] == 512 and u["peak_cpu"] == 1.0 and u["wall_seconds"] <= 61
    assert u["cpu_seconds"] <= u["wall_seconds"] * 1.0 and u["output_bytes"] == 1024 * 1024 * 1024
    assert set(u["capped"]) == {"peak_memory_mb", "peak_cpu", "cpu_seconds", "wall_seconds", "output_bytes"}
    await agent.close()


@pytest.mark.asyncio
async def test_out_of_memory_is_named_as_the_cause(group_with_device, db):
    coord, g, agent = await group_with_device()
    job = await submit(coord, g, retries=0)
    await agent.connect([share(g)])
    offer = await agent.ready()
    assert (await agent.run_task(offer, exit_code=137, usage={**MEASURED, "oom_killed": True})).accepted
    a = await db.get(Attempt, uuid.UUID(offer.attempt_id))
    await db.refresh(a)
    assert a.end_reason == "oom"
    tasks = await coord.get(f"/api/v1/jobs/{job['id']}/tasks")
    r = tasks[0]["result"]
    assert tasks[0]["state"] == "failed" and r["oom_killed"] is True and "out of memory" in r["error"]
    await agent.close()


@pytest.mark.asyncio
async def test_a_commit_without_usage_still_works(group_with_device, db):
    """Older agents send no usage; nothing changes for them."""
    coord, g, agent = await group_with_device()
    job = await submit(coord, g)
    await agent.connect([share(g)])
    offer = await agent.ready()
    assert (await agent.run_task(offer)).accepted
    j = await coord.get(f"/api/v1/jobs/{job['id']}")
    assert j["used"] is None
    await agent.close()


def test_bounded_usage_and_summary():
    raw = pm.RunUsage(**{**MEASURED, "oom_killed": True})
    assert bounded_usage(raw, cpu=1, memory_mb=512, disk_mb=1024, exit_code=0, observed_s=50)["oom_killed"] is False, \
        "a child killed while the program succeeded is not an out-of-memory failure"
    rows = [bounded_usage(pm.RunUsage(**{**MEASURED, "peak_memory_mb": m, "wall_seconds": w, "cpu_seconds": w / 2}),
                          cpu=2, memory_mb=4096, disk_mb=1024, exit_code=0, observed_s=1000)
            for m, w in ((100, 10), (900, 30))]
    s = summarize([*rows, None, {**MEASURED, "samples": 0, "oom_killed": True}])
    assert s["peak_memory_mb"] == 900 and s["max_wall_seconds"] == 30 and s["avg_cpu"] == 0.5 and s["measured"] == 2
    assert s["oom_killed"] == 1
    assert summarize([]) is None


@pytest.mark.asyncio
async def test_a_bad_measurement_never_costs_the_result(group_with_device, db):
    """A value out of range drops the measurement, not the commit: the exit code and outputs still count."""
    coord, g, agent = await group_with_device()
    job = await submit(coord, g)
    await agent.connect([share(g)])
    offer = await agent.ready()
    await agent.send(pm.Accept(attempt_id=offer.attempt_id, epoch=offer.epoch, tier="T2"))
    await agent.send(pm.Started(attempt_id=offer.attempt_id, epoch=offer.epoch))
    raw = {"type": "commit", "msg_id": "m-1", "idempotency_key": f"{offer.task_id}:{offer.attempt_id}",
           "task_id": offer.task_id, "attempt_id": offer.attempt_id, "epoch": offer.epoch, "exit_code": 0,
           "outputs": [], "error": None, "usage": {**MEASURED, "cpu_seconds": 1e15}}
    await agent.ws.send(json.dumps(raw))
    ack = await agent.recv_type(pm.CommitAck)
    assert ack.accepted
    tasks = await coord.get(f"/api/v1/jobs/{job['id']}/tasks")
    assert tasks[0]["state"] == "succeeded" and tasks[0]["usage"] is None
    await agent.close()


@pytest.mark.asyncio
async def test_the_database_summary_matches_and_small_cpu_overshoot_is_not_odd(group_with_device, db):
    coord, g, agent = await group_with_device()
    job = await submit(coord, g, matrix={"x": [1, 2, 3]})
    await agent.connect([share(g)])
    stored = []
    for peak, wall, oom in ((100, 10.0, False), (400, 30.0, False), (64, 1.0, True)):
        offer = await agent.ready()
        usage = {**MEASURED, "peak_memory_mb": peak, "wall_seconds": wall, "cpu_seconds": wall / 2,
                 "peak_cpu": 1.01, "oom_killed": oom}
        await agent.run_task(offer, exit_code=137 if oom else 0, usage=usage)
        a = await db.get(Attempt, uuid.UUID(offer.attempt_id))
        await db.refresh(a)
        assert a.usage["capped"] == [], "101% of one CPU is Docker's rounding, not an odd device"
        stored.append(a.usage)
    j = await coord.get(f"/api/v1/jobs/{job['id']}")
    assert j["used"] == summarize(stored)
    await agent.close()
