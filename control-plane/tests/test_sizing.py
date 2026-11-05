"""pool chooses the sizes a job leaves out (docs/design/pool-decides.md §4.3, §4.5, §4.7, §10)."""

import asyncio
import uuid

import pytest
from fake_agent import CAPS, FREE, share
from sqlalchemy import select

from pool_cp.models import Attempt, UsageLedger
from pool_cp.scheduler import sizing
from pool_protocol import messages as pm

pytestmark = pytest.mark.asyncio

IMG = "registry.example.org/lab/sim:1@sha256:" + "a" * 64
IMG2 = "registry.example.org/lab/sim:2@sha256:" + "b" * 64  # a new version of the same image


def short(**extra) -> dict:
    return {"image": IMG, "command": ["sim"], **extra}


# The fake computer ends a task at once, so the server caps a run time at 60 s: keep runs under that.
def usage(peak=1400, cpu=0.5, wall=50.0, oom=False, samples=50) -> dict:
    return {"peak_memory_mb": peak, "cpu_seconds": cpu * wall, "peak_cpu": cpu, "wall_seconds": wall,
            "output_bytes": 10 << 20, "image_mb": 200, "oom_killed": oom, "samples": samples}


async def submit(c, g, body):
    return (await c.post(f"/api/v1/groups/{g}/jobs", body, expect=201)).json()


async def offer_spec(agent, offer):
    return agent.open_envelope(offer).spec


async def test_first_run_gets_all_free_memory_and_the_group_default_time(group_with_device, db):
    coord, g, agent = await group_with_device()
    job = await submit(coord, g, short())
    j = await coord.get(f"/api/v1/jobs/{job['id']}")
    assert j["sizing"]["first_run"] is True and set(j["sizing"]["auto"]) == {"cpu", "memory", "disk", "time_limit"}
    assert j["requested"]["memory_mb"] is None and j["requested"]["first_run"] is True
    assert any("first run of this image" in r for r in j["sizing"]["reasons"])
    assert j["requested"]["time_limit_s"] == 4 * 3600
    await agent.connect([share(g)])
    offer = await agent.ready()
    spec = await offer_spec(agent, offer)
    assert spec.resources.memory_mb == FREE["memory_mb"], "a first run gets all the computer's free memory"
    assert spec.resources.cpu == 1 and spec.max_runtime_s == 4 * 3600
    a = await db.get(Attempt, uuid.UUID(offer.attempt_id))
    assert a.resources["first_run"] is True and a.resources["memory_mb"] == FREE["memory_mb"]
    await agent.close()


async def test_the_next_run_is_sized_from_the_last_one(group_with_device, db):
    coord, g, agent = await group_with_device()
    await submit(coord, g, short())
    await agent.connect([share(g)])
    offer = await agent.ready()
    assert (await agent.run_task(offer, usage=usage(peak=1400, cpu=0.5))).accepted
    # A new version of the same image and command: sized from that run.
    job = await submit(coord, g, {**short(), "image": IMG2})
    j = await coord.get(f"/api/v1/jobs/{job['id']}")
    assert j["sizing"]["first_run"] is False and j["sizing"]["runs"] == 1
    assert j["requested"]["memory_mb"] == 1792, "1400 MiB × 1.25, rounded up to 64 MiB"
    assert j["requested"]["cpu"] == 1.0, "0.5 CPU on average × 1.2, rounded up to half a CPU"
    assert j["requested"]["time_limit_s"] == 1800, "3 × 50 s, but at least 30 min"
    assert any("used at most 1.4 GiB" in r for r in j["sizing"]["reasons"])
    offer = await agent.ready()
    assert (await offer_spec(agent, offer)).resources.memory_mb == 1792
    await agent.close()


async def test_a_run_that_used_all_its_cpu_gets_more(group_with_device):
    coord, g, agent = await group_with_device()
    await submit(coord, g, short(cpu=2))
    await agent.connect([share(g)])
    assert (await agent.run_task(await agent.ready(), usage=usage(cpu=1.95))).accepted
    j = await coord.get(f"/api/v1/jobs/{(await submit(coord, g, short()))['id']}")
    assert j["requested"]["cpu"] == 4 and any("used all of its 2 CPU" in r for r in j["sizing"]["reasons"])
    await agent.close()


async def test_sizes_you_give_are_kept(group_with_device):
    coord, g, agent = await group_with_device()
    job = await submit(coord, g, short(memory="3Gi", time_limit="2h"))
    j = await coord.get(f"/api/v1/jobs/{job['id']}")
    assert j["sizing"]["first_run"] is False and "memory" not in j["sizing"]["auto"]
    assert j["requested"]["memory_mb"] == 3072 and j["requested"]["time_limit_s"] == 7200
    assert "Memory 3 GiB: as you asked." in j["sizing"]["reasons"]
    await agent.connect([share(g)])
    assert (await offer_spec(agent, await agent.ready())).resources.memory_mb == 3072
    await agent.close()


async def test_learned_sizes_are_kept_per_person(group_with_device, user, db):
    coord, g, agent = await group_with_device()
    await submit(coord, g, short())
    await agent.connect([share(g)])
    assert (await agent.run_task(await agent.ready(), usage=usage())).accepted
    other = await user()
    invite = (await coord.post(f"/api/v1/groups/{g}/invites", {"role": "member"}, expect=201)).json()
    await other.post("/api/v1/invites/accept", {"token": invite["token"]}, expect=200)
    job = await submit(other, g, short())
    j = await other.get(f"/api/v1/jobs/{job['id']}")
    assert j["sizing"]["first_run"] is True, "another person's runs never size yours"
    await agent.close()


async def test_part_0_runs_first_and_sizes_the_other_parts(group_with_device, db):
    coord, g, agent = await group_with_device()
    job = await submit(coord, g, short(parts=3))
    await agent.connect([share(g)])
    first = await agent.ready()
    assert agent.open_envelope(first).spec.task_index == 0
    assert isinstance(await agent.ready(), pm.NoWork), "the other parts wait for part 0"
    waiting = await coord.get(f"/api/v1/jobs/{job['id']}/waiting")
    assert waiting["reasons"][0]["code"] == "first_part"
    assert (await agent.run_task(first, usage=usage(peak=500))).accepted
    j = await coord.get(f"/api/v1/jobs/{job['id']}")
    assert j["sizing"]["first_run"] is False and j["requested"]["memory_mb"] == 640
    assert "Part 0 used 500 MiB" in j["sizing"]["reasons"][-1]
    nxt = await agent.ready()
    spec = await offer_spec(agent, nxt)
    assert spec.task_index == 1 and spec.resources.memory_mb == 640
    await agent.close()


async def test_one_first_run_per_person_at_a_time(group_with_device):
    coord, g, agent = await group_with_device()
    a = await submit(coord, g, short())
    b = await submit(coord, g, {**short(), "image": "registry.example.org/lab/other:1@sha256:" + "c" * 64})
    await agent.connect([share(g)])
    first = await agent.ready()
    assert isinstance(await agent.ready(), pm.NoWork)
    waiting = await coord.get(f"/api/v1/jobs/{b['id']}/waiting")
    assert waiting["reasons"][0]["code"] == "one_first_run"
    assert (await agent.run_task(first, usage=usage())).accepted
    assert isinstance(await agent.ready(), pm.Offer), "the second first run starts when the first ends"
    assert a["id"] != b["id"]
    await agent.close()


async def test_out_of_memory_on_a_chosen_size_grows_it_without_counting_a_failure(group_with_device, db):
    coord, g, agent = await group_with_device()
    await submit(coord, g, short())
    await agent.connect([share(g)])
    assert (await agent.run_task(await agent.ready(), usage=usage(peak=1000))).accepted  # learn: 1280 MiB
    job = await submit(coord, g, {**short(), "retries": 0})
    memory = []
    for _ in range(sizing.MAX_GROWTH_STEPS):
        offer = await agent.ready()
        memory.append((await offer_spec(agent, offer)).resources.memory_mb)
        assert (await agent.run_task(offer, exit_code=137, usage=usage(peak=memory[-1], oom=True))).accepted
        a = await db.get(Attempt, uuid.UUID(offer.attempt_id))
        await db.refresh(a)
        assert (a.state, a.end_reason) == ("preempted", "oom_resized")
        led = (await db.execute(select(UsageLedger).where(UsageLedger.attempt_id == a.id))).scalar_one()
        assert not led.pool_paid, "it counts for fair share unless a bigger try succeeds (it never does here)"
    assert memory == [1280, 2560, 5120]
    tasks = await coord.get(f"/api/v1/jobs/{job['id']}/tasks")
    assert tasks[0]["state"] == "pending" and tasks[0]["failures"] == 0
    # The fourth time pool stops growing it: now it is the person's failure (retries: 0).
    offer = await agent.ready()
    assert (await offer_spec(agent, offer)).resources.memory_mb == 10240
    assert (await agent.run_task(offer, exit_code=137, usage=usage(peak=10240, oom=True))).accepted
    tasks = await coord.get(f"/api/v1/jobs/{job['id']}/tasks")
    assert tasks[0]["state"] == "failed" and "out of memory" in tasks[0]["result"]["error"]
    j = await coord.get(f"/api/v1/jobs/{job['id']}")
    assert "pool stopped growing it" in j["sizing"]["reasons"][-1]
    await agent.close()


async def test_a_first_run_out_of_memory_waits_for_a_bigger_computer(group_with_device, db, enrolled):
    coord, g, agent = await group_with_device()
    job = await submit(coord, g, short())
    await agent.connect([share(g)])
    # For its first minute a first run waits for a computer offering about the most memory (here 32 GiB).
    assert isinstance(await agent.ready(free={**FREE, "memory_mb": 4096}), pm.NoWork)
    offer = await agent.ready()
    assert (await agent.run_task(offer, exit_code=137, usage=usage(peak=32768, oom=True))).accepted
    j = await coord.get(f"/api/v1/jobs/{job['id']}")
    assert j["requested"]["memory_mb"] is None and j["requested"]["first_run"] is True, "all free memory, no number"
    assert "waits for a computer with more" in j["sizing"]["reasons"][-1]
    assert isinstance(await agent.ready(), pm.NoWork), "the same free memory is not enough"
    assert isinstance(await agent.ready(free={**FREE, "memory_mb": 49152}), pm.NoWork), \
        "claiming more free memory than the computer has does not help"
    waiting = await coord.get(f"/api/v1/jobs/{job['id']}/waiting")
    assert "ran out of memory with all the free memory" in waiting["reasons"][0]["message"]
    big = await enrolled(coord, "big")
    await coord.req("PUT", f"/api/v1/devices/{big.device_id}/shares/{g}", expect=200)
    await big.connect([share(g)], caps={**CAPS, "memory_mb": 65536})
    bigger = await big.ready(free={**FREE, "memory_mb": 49152})
    assert (await offer_spec(big, bigger)).resources.memory_mb == 49152
    await agent.close()
    await big.close()


async def test_out_of_time_on_a_chosen_limit_doubles_it(group_with_device, db):
    coord, g, agent = await group_with_device()
    job = await submit(coord, g, short(memory="1Gi"))
    await agent.connect([share(g)])
    offer = await agent.ready()
    await agent.send(pm.Accept(attempt_id=offer.attempt_id, epoch=offer.epoch, tier="T2"))
    await agent.send(pm.Started(attempt_id=offer.attempt_id, epoch=offer.epoch))
    await agent.send(pm.Commit(idempotency_key=f"{offer.task_id}:{offer.attempt_id}", task_id=offer.task_id,
                               attempt_id=offer.attempt_id, epoch=offer.epoch, exit_code=143,
                               error="max_runtime_exceeded (14400s)"))
    assert (await agent.recv_type(pm.CommitAck)).accepted
    j = await coord.get(f"/api/v1/jobs/{job['id']}")
    assert j["requested"]["time_limit_s"] == 8 * 3600 and "ran out of time at 4 h" in j["sizing"]["reasons"][-1]
    tasks = await coord.get(f"/api/v1/jobs/{job['id']}/tasks")
    assert tasks[0]["failures"] == 0 and tasks[0]["state"] == "pending"
    assert (await offer_spec(agent, await agent.ready())).max_runtime_s == 8 * 3600
    await agent.close()


async def test_the_coordinator_sets_the_group_default_time_limit(group_with_device, user):
    coord, g, agent = await group_with_device()
    r = (await coord.req("PATCH", f"/api/v1/groups/{g}", 200, json={"default_time_limit_s": 2 * 3600})).json()
    assert r["default_time_limit_s"] == 7200
    job = await submit(coord, g, short())
    assert (await coord.get(f"/api/v1/jobs/{job['id']}"))["requested"]["time_limit_s"] == 7200
    await coord.req("PATCH", f"/api/v1/groups/{g}", 422, json={"default_time_limit_s": 10})
    await coord.req("PATCH", f"/api/v1/groups/{g}", 422, json={"default_time_limit_s": None})


async def test_rules():
    assert sizing.repository("ghcr.io/lab/app:1@sha256:" + "a" * 64) == "ghcr.io/lab/app"
    assert sizing.repository("127.0.0.1:5000/g/u/app:run-1") == "127.0.0.1:5000/g/u/app"
    assert sizing.repository("python:3.12-slim") == "python"
    assert sizing.size_text(1792) == "1.8 GiB" and sizing.size_text(2048) == "2 GiB"
    assert sizing.size_text(500) == "500 MiB"
    assert sizing.time_text(5400) == "1 h 30 min" and sizing.time_text(600) == "10 min"


async def test_an_old_out_of_memory_run_does_not_count_after_a_good_one():
    def run(peak, memory, oom=False):
        return sizing.Run(usage={**usage(peak=peak, oom=oom)}, cpu=1, memory_mb=memory, command=[], args=[])

    newest_first = [run(3000, 8192), run(4096, 4096, oom=True)]
    sizes, why = sizing._from_runs(newest_first)
    # The highest use seen (4 GiB) × 1.25, not twice the old out-of-memory limit (8 GiB).
    assert sizes["memory_mb"] == 5120 and "used at most" in why["memory"]
    sizes, why = sizing._from_runs(list(reversed(newest_first)))
    assert sizes["memory_mb"] == 8192 and "ran out of memory" in why["memory"]


# ---- from the security review of step B ----

async def test_a_run_with_one_reading_does_not_break_sizing(group_with_device, db):
    """One reading means no CPU figure (avg_cpu None): learning and the next submit must still work."""
    coord, g, agent = await group_with_device()
    await submit(coord, g, short(parts=2))
    await agent.connect([share(g)])
    offer = await agent.ready()
    assert (await agent.run_task(offer, usage=usage(samples=1))).accepted, "the commit is not lost"
    job = await submit(coord, g, short())
    j = await coord.get(f"/api/v1/jobs/{job['id']}")
    assert j["sizing"]["first_run"] is False and j["requested"]["cpu"] == 1.0
    await agent.close()


async def test_other_parts_start_when_part_0_fails_without_a_result(group_with_device, db):
    coord, g, agent = await group_with_device()
    job = await submit(coord, g, short(parts=2, retries=0))
    await agent.connect([share(g)])
    first = await agent.ready()
    await agent.send(pm.Abort(attempt_id=first.attempt_id, epoch=first.epoch, reason="runtime_error",
                              detail="pull access denied"))
    await asyncio.sleep(0.2)
    nxt = await agent.ready()
    assert isinstance(nxt, pm.Offer) and agent.open_envelope(nxt).spec.task_index == 1, \
        "part 0 ended, so the other parts may start (as first runs)"
    assert job["id"]
    await agent.close()


async def test_a_first_run_gets_the_groups_share_not_the_devices_claim(group_with_device, db):
    coord, g, agent = await group_with_device()
    await submit(coord, g, short())
    small = {**share(g), "limits": {**share(g)["limits"], "memory_mb": 4096}}
    await agent.connect([small])
    offer = await agent.ready(free={**FREE, "memory_mb": 10**6})
    assert (await offer_spec(agent, offer)).resources.memory_mb == 4096
    await agent.close()


async def test_numbers_from_one_odd_device_or_capped_runs_are_not_believed():
    def run(peak, device, capped=()):
        return sizing.Run(usage={**usage(peak=peak), "capped": list(capped)}, cpu=1, memory_mb=1 << 20,
                          command=[], args=[], device_id=device)

    a, b = uuid.uuid4(), uuid.uuid4()
    sizes, _ = sizing._from_runs([run(10**6, a), run(1000, b), run(1200, b)])
    assert sizes["memory_mb"] == 1536, "the device far above the others is ignored: 1200 × 1.25"
    sizes, _ = sizing._from_runs([run(10**6, a, capped=["image_mb"]), run(1000, b)])
    assert sizes["memory_mb"] == 1280
    sizes, why = sizing._from_runs([run(1000, b)], sizing.Ceiling(memory_mb=1024))
    assert sizes["memory_mb"] == 1024 and "biggest computer" in why["memory"]


async def test_parts_out_of_memory_together_use_one_growth_step(group_with_device, db):
    coord, g, agent = await group_with_device()
    await submit(coord, g, short())
    await agent.connect([share(g)])
    assert (await agent.run_task(await agent.ready(), usage=usage(peak=1000))).accepted  # learn 1280 MiB
    job = await submit(coord, g, short(parts=2))
    one, two = await agent.ready(), await agent.ready()
    for offer in (one, two):
        assert (await offer_spec(agent, offer)).resources.memory_mb == 1280
    for offer in (one, two):
        assert (await agent.run_task(offer, exit_code=137, usage=usage(peak=1280, oom=True))).accepted
    j = await coord.get(f"/api/v1/jobs/{job['id']}")
    assert j["sizing"]["steps"] == 1 and j["requested"]["memory_mb"] == 2560
    tasks = await coord.get(f"/api/v1/jobs/{job['id']}/tasks")
    assert all(t["failures"] == 0 and t["state"] == "pending" for t in tasks)
    await agent.close()


async def test_tries_pool_caused_count_for_fair_share_until_the_bigger_size_works(group_with_device, db):
    coord, g, agent = await group_with_device()
    await submit(coord, g, short())
    await agent.connect([share(g)])
    assert (await agent.run_task(await agent.ready(), usage=usage(peak=1000))).accepted
    await submit(coord, g, short())
    small = await agent.ready()
    assert (await agent.run_task(small, exit_code=137, usage=usage(peak=1280, oom=True))).accepted
    led = (await db.execute(select(UsageLedger).where(UsageLedger.attempt_id == uuid.UUID(small.attempt_id))))
    assert led.scalar_one().pool_paid is False, "counted until a bigger try succeeds"
    assert (await agent.run_task(await agent.ready(), usage=usage(peak=2000))).accepted
    db.expire_all()
    led = (await db.execute(select(UsageLedger).where(UsageLedger.attempt_id == uuid.UUID(small.attempt_id))))
    assert led.scalar_one().pool_paid is True
    await agent.close()


async def test_reasons_are_for_the_submitter_and_managers(group_with_device, user):
    coord, g, agent = await group_with_device()
    other = await user()
    invite = (await coord.post(f"/api/v1/groups/{g}/invites", {"role": "member"}, expect=201)).json()
    await other.post("/api/v1/invites/accept", {"token": invite["token"]}, expect=200)
    job = await submit(other, g, short())
    assert "reasons" in (await other.get(f"/api/v1/jobs/{job['id']}"))["sizing"]
    assert "reasons" in (await coord.get(f"/api/v1/jobs/{job['id']}"))["sizing"]
    third = await user()
    invite = (await coord.post(f"/api/v1/groups/{g}/invites", {"role": "member"}, expect=201)).json()
    await third.post("/api/v1/invites/accept", {"token": invite["token"]}, expect=200)
    seen = (await third.get(f"/api/v1/jobs/{job['id']}"))["sizing"]
    assert "reasons" not in seen and seen["first_run"] is True
    assert all("reasons" not in (j["sizing"] or {}) for j in await third.get(f"/api/v1/groups/{g}/jobs"))
    await coord.post(f"/api/v1/groups/{g}/jobs", short(time_limit=0), expect=422)
