"""Matching, leases, fencing, retries and owner policy on the server side (plan §7.1-7.4)."""

import asyncio
import uuid

import pytest
from conftest import gname, job_spec
from fake_agent import FREE, share
from sqlalchemy import select, text, update

from pool_cp.models import Attempt, Task
from pool_cp.scheduler import core
from pool_protocol import messages as pm

pytestmark = pytest.mark.asyncio


async def submit(c, group, **spec):
    return (await c.post(f"/api/v1/groups/{group}/jobs", job_spec(**spec), expect=201)).json()


async def expire(db, attempt_id, settings):
    await db.execute(update(Attempt).where(Attempt.id == uuid.UUID(attempt_id))
                     .values(lease_expires_at=text("now() - interval '1 second'")))
    await db.commit()
    n = await core.expire_leases(db, settings)
    await db.commit()
    return n


async def rollup(db):
    await core.rollup_jobs(db)
    await db.commit()


async def test_single_job_end_to_end(group_with_device, db, settings):
    coord, g, agent = await group_with_device()
    job = await submit(coord, g, command=["echo", "hello"])
    await agent.connect([share(g)])
    offer = await agent.ready()
    assert isinstance(offer, pm.Offer) and offer.epoch == 1
    env = agent.open_envelope(offer)  # signature verified against the pinned key
    assert env.device_id == agent.device_id and env.group == g and env.submitter == coord.handle
    assert env.spec.command == ["echo", "hello"]
    ack = await agent.run_task(offer, logs="hello\n")
    assert ack.accepted
    await rollup(db)
    j = await coord.get(f"/api/v1/jobs/{job['id']}")
    assert j["state"] == "succeeded" and j["tasks"]["succeeded"] == 1
    logs = await coord.get(f"/api/v1/jobs/{job['id']}/tasks/0/logs")
    assert "".join(x["chunk"] for x in logs) == "hello\n"
    hist = await coord.get(f"/api/v1/devices/{agent.device_id}/history")
    assert hist[0]["submitter"] == coord.handle and hist[0]["state"] == "succeeded" and hist[0]["group"] == g
    usage = await coord.get(f"/api/v1/groups/{g}/usage")
    assert usage[0]["member"] == coord.handle and usage[0]["attempts"] == 1
    assert isinstance(await agent.ready(), pm.NoWork)
    await agent.close()


async def test_idempotent_commit(group_with_device):
    coord, g, agent = await group_with_device()
    await submit(coord, g)
    await agent.connect([share(g)])
    offer = await agent.ready()
    first = await agent.run_task(offer)
    again = await agent.commit(offer.attempt_id, offer.task_id, offer.epoch)
    assert first.accepted and again.accepted and again.reason == "duplicate"
    await agent.close()


async def test_stale_epoch_commit_rejected(group_with_device, user, enrolled, db, settings):
    """Plan §7.4 row 3: long network drop, task reassigned with epoch 2, late epoch-1 commit rejected."""
    coord, g, a1 = await group_with_device()
    a2 = await enrolled(coord, "second")
    await coord.req("PUT", f"/api/v1/devices/{a2.device_id}/shares/{g}", expect=200)
    job = await submit(coord, g)
    await a1.connect([share(g)])
    o1 = await a1.ready()
    await a1.send(pm.Accept(attempt_id=o1.attempt_id, epoch=1, tier="T2"))
    await a1.send(pm.Started(attempt_id=o1.attempt_id, epoch=1))
    await a1.heartbeat([(o1.attempt_id, 1, "running")])
    assert await expire(db, o1.attempt_id, settings) == 1  # a1 goes silent; lease runs out
    await a2.connect([share(g)])
    o2 = await a2.ready()
    assert isinstance(o2, pm.Offer) and o2.task_id == o1.task_id and o2.epoch == 2
    late = await a1.commit(o1.attempt_id, o1.task_id, 1)
    assert not late.accepted and late.reason in ("stale_epoch", "attempt_lost")
    hb = await a1.heartbeat([(o1.attempt_id, 1, "running")])
    assert o1.attempt_id in hb.stale  # a1 is told to stop
    assert (await a2.run_task(o2)).accepted
    await rollup(db)
    tasks = await coord.get(f"/api/v1/jobs/{job['id']}/tasks")
    assert tasks[0]["state"] == "succeeded" and tasks[0]["result"]["epoch"] == 2
    await a1.close()
    await a2.close()


async def test_progress_from_stale_attempt_gets_cancel(group_with_device, db, settings):
    coord, g, agent = await group_with_device()
    await submit(coord, g)
    await agent.connect([share(g)])
    o = await agent.ready()
    await agent.send(pm.Accept(attempt_id=o.attempt_id, epoch=1, tier="T2"))
    await agent.heartbeat([(o.attempt_id, 1, "assigned")])  # sync point: accept was processed
    assert await expire(db, o.attempt_id, settings) == 1
    await agent.send(pm.Progress(attempt_id=o.attempt_id, epoch=1, seq=0, log_chunk="late"))
    cancel = await agent.recv_type(pm.Cancel)
    assert cancel.attempt_id == o.attempt_id
    await agent.close()


async def test_retry_with_backoff_then_always_failing(group_with_device, enrolled, db, settings):
    coord, g, a1 = await group_with_device()
    a2 = await enrolled(coord, "second")
    await coord.req("PUT", f"/api/v1/devices/{a2.device_id}/shares/{g}", expect=200)
    job = await submit(coord, g, retries=3)
    await a1.connect([share(g)])
    await a2.connect([share(g)])
    o = await a1.ready()
    assert (await a1.run_task(o, exit_code=2)).accepted
    t = (await db.execute(select(Task).where(Task.id == uuid.UUID(o.task_id)))).scalar_one()
    assert t.state == "pending" and t.failures == 1
    await asyncio.sleep(0.3)  # backoff (tests use 0.05-0.2 s)
    o2 = await a2.ready()
    assert isinstance(o2, pm.Offer) and o2.epoch == 2
    await a2.run_task(o2, exit_code=1)
    await rollup(db)
    tasks = await coord.get(f"/api/v1/jobs/{job['id']}/tasks")
    # Failed on 2 different devices -> Failed at once, even with retries left (plan §7.4 last row).
    assert tasks[0]["state"] == "failed" and "2 different devices" in tasks[0]["result"]["reason"]
    assert (await coord.get(f"/api/v1/jobs/{job['id']}"))["state"] == "failed"
    await a1.close()
    await a2.close()


async def test_retries_exhausted_on_one_device(group_with_device, db):
    coord, g, agent = await group_with_device()
    job = await submit(coord, g, retries=1)
    await agent.connect([share(g)])
    for _ in range(2):
        o = None
        for _ in range(40):
            o = await agent.ready()
            if isinstance(o, pm.Offer):
                break
            await asyncio.sleep(0.05)
        assert isinstance(o, pm.Offer)
        await agent.run_task(o, exit_code=3)
    await rollup(db)
    tasks = await coord.get(f"/api/v1/jobs/{job['id']}/tasks")
    assert tasks[0]["state"] == "failed" and tasks[0]["failures"] == 2
    await agent.close()


async def test_decline_requeues_without_penalty_and_cools_down(group_with_device, enrolled, db):
    coord, g, a1 = await group_with_device()
    await submit(coord, g)
    await a1.connect([share(g)])
    o = await a1.ready()
    await a1.send(pm.Decline(attempt_id=o.attempt_id, epoch=o.epoch, reason="outside_window"))
    assert isinstance(await a1.ready(), pm.NoWork)  # not offered to the same device again right away
    a2 = await enrolled(coord, "second")
    await coord.req("PUT", f"/api/v1/devices/{a2.device_id}/shares/{g}", expect=200)
    await a2.connect([share(g)])
    o2 = await a2.ready()
    assert isinstance(o2, pm.Offer) and o2.epoch == 2
    t = (await db.execute(select(Task).where(Task.id == uuid.UUID(o.task_id)))).scalar_one()
    assert t.failures == 0
    await a1.close()
    await a2.close()


async def test_owner_return_abort_has_no_penalty(group_with_device, db):
    coord, g, agent = await group_with_device()
    await submit(coord, g, retries=0)
    await agent.connect([share(g)])
    o = await agent.ready()
    await agent.send(pm.Accept(attempt_id=o.attempt_id, epoch=1, tier="T2"))
    await agent.send(pm.Started(attempt_id=o.attempt_id, epoch=1))
    await agent.send(pm.Abort(attempt_id=o.attempt_id, epoch=1, reason="owner_returned"))
    o2 = await agent.ready()
    assert isinstance(o2, pm.Offer) and o2.epoch == 2  # retries=0 but no failure counted
    await agent.close()


async def test_shares_and_policy_filters(group_with_device, user):
    coord, g, agent = await group_with_device()
    await submit(coord, g)
    # Window not active right now -> nothing offered.
    await agent.connect([share(g, active=False)])
    assert isinstance(await agent.ready(), pm.NoWork)
    # Only accepts jobs from someone else.
    await agent.close()
    await agent.connect([share(g, accept_from=["someone-else"])])
    assert isinstance(await agent.ready(), pm.NoWork)
    # Resources too small.
    await agent.close()
    await agent.connect([share(g)])
    assert isinstance(await agent.ready(free={**FREE, "memory_mb": 100}), pm.NoWork)
    # Only T0 offered by the agent -> min_tier T1 job not matched.
    assert isinstance(await agent.ready(tiers=["T0"]), pm.NoWork)
    assert isinstance(await agent.ready(), pm.Offer)
    await agent.close()


async def test_probation_member_needs_opt_in(user, enrolled):
    coord, newbie = await user(), await user()
    g = gname()
    await coord.post("/api/v1/groups", {"name": g, "display_name": "x", "probation_days": 7}, expect=201)
    tok = (await coord.post(f"/api/v1/groups/{g}/invites", {"role": "member"}, expect=201)).json()["token"]
    await newbie.post("/api/v1/invites/accept", {"token": tok}, expect=200)
    await submit(newbie, g)
    agent = await enrolled(coord)
    await coord.req("PUT", f"/api/v1/devices/{agent.device_id}/shares/{g}", expect=200)
    await agent.connect([share(g, accept_new=False)])
    assert isinstance(await agent.ready(), pm.NoWork)
    await agent.close()
    await agent.connect([share(g, accept_new=True)])
    offer = await agent.ready()
    assert isinstance(offer, pm.Offer) and agent.open_envelope(offer).submitter_probation is True
    await agent.close()


async def test_group_priority_and_fair_share(user, enrolled):
    coord, heavy, light = await user(), await user(), await user()
    g1, g2 = gname(), gname()
    for g in (g1, g2):
        await coord.post("/api/v1/groups", {"name": g, "display_name": "x"}, expect=201)
    for who in (heavy, light):
        tok = (await coord.post(f"/api/v1/groups/{g2}/invites", {"role": "member"}, expect=201)).json()["token"]
        await who.post("/api/v1/invites/accept", {"token": tok}, expect=200)
    agent = await enrolled(coord)
    for g in (g1, g2):
        await coord.req("PUT", f"/api/v1/devices/{agent.device_id}/shares/{g}", expect=200)
    await submit(coord, g1, command=["g1"])
    await submit(heavy, g2, command=["heavy-1"])
    # Owner priority: g2 first.
    await agent.connect([share(g2), share(g1)])
    o = await agent.ready()
    assert agent.open_envelope(o).group == g2
    await agent.run_task(o)  # heavy now has usage in g2
    await submit(heavy, g2, command=["heavy-2"])
    await asyncio.sleep(0.01)
    await submit(light, g2, command=["light-1"])
    o = await agent.ready()
    env = agent.open_envelope(o)
    assert env.spec.command == ["light-1"]  # fair share beats submission order
    await agent.close()


async def test_concurrent_claims_never_share_a_task(user, enrolled):
    coord = await user()
    g = gname()
    await coord.post("/api/v1/groups", {"name": g, "display_name": "x"}, expect=201)
    await coord.post(f"/api/v1/groups/{g}/jobs",
                     job_spec(shape="array", matrix={"i": list(range(20))}, command=["t", "{{i}}"]), expect=201)
    agents = [await enrolled(coord, f"d{i}") for i in range(5)]
    for a in agents:
        await coord.req("PUT", f"/api/v1/devices/{a.device_id}/shares/{g}", expect=200)
        await a.connect([share(g)])

    async def grab(a):
        got = []
        for _ in range(8):
            o = await a.ready()
            if isinstance(o, pm.Offer):
                got.append(o.task_id)
        return got

    results = await asyncio.gather(*(grab(a) for a in agents))
    flat = [t for r in results for t in r]
    assert len(flat) == 20 and len(set(flat)) == 20
    for a in agents:
        await a.close()


async def test_confidential_needs_dedicated_device(group_with_device, user, http):
    coord, g, agent = await group_with_device()
    await submit(coord, g, data_class="confidential")
    await agent.connect([share(g)])
    assert isinstance(await agent.ready(), pm.NoWork)
    await coord.req("PATCH", f"/api/v1/devices/{agent.device_id}", json={"org_owned_dedicated": True}, expect=403)
    root = await user("root-admin")
    await root.req("PATCH", f"/api/v1/devices/{agent.device_id}", json={"org_owned_dedicated": True}, expect=200)
    assert isinstance(await agent.ready(tiers=["T1"]), pm.NoWork)  # confidential needs T2+
    assert isinstance(await agent.ready(), pm.Offer)
    await agent.close()


async def test_cancel_job_stops_attempt(group_with_device, db):
    coord, g, agent = await group_with_device()
    job = await submit(coord, g)
    await agent.connect([share(g)])
    o = await agent.ready()
    await agent.send(pm.Accept(attempt_id=o.attempt_id, epoch=1, tier="T2"))
    await agent.send(pm.Started(attempt_id=o.attempt_id, epoch=1))
    await agent.heartbeat([(o.attempt_id, 1, "running")])
    await coord.post(f"/api/v1/jobs/{job['id']}/cancel", expect=200)
    cancel = await agent.recv_type(pm.Cancel)
    assert cancel.attempt_id == o.attempt_id
    hb = await agent.heartbeat([(o.attempt_id, 1, "running")])
    assert o.attempt_id in hb.stale
    ack = await agent.commit(o.attempt_id, o.task_id, 1)
    assert not ack.accepted
    await agent.close()


async def test_reconnect_reconciles_running_attempts(group_with_device, db):
    coord, g, agent = await group_with_device()
    await submit(coord, g, shape="array", matrix={"i": [1, 2]}, command=["x", "{{i}}"])
    await agent.connect([share(g)])
    o1, o2 = await agent.ready(), await agent.ready()
    for o in (o1, o2):
        await agent.send(pm.Accept(attempt_id=o.attempt_id, epoch=1, tier="T2"))
        await agent.send(pm.Started(attempt_id=o.attempt_id, epoch=1))
    await agent.heartbeat([(o1.attempt_id, 1, "running"), (o2.attempt_id, 1, "running")])
    await agent.close()
    # Agent restarts and only o1 survived (plus a bogus id).
    bogus = str(uuid.uuid4())
    welcome = await agent.connect([share(g)], running=[{"attempt_id": o1.attempt_id, "epoch": 1, "state": "running"},
                                                       {"attempt_id": bogus, "epoch": 1, "state": "running"}])
    assert welcome.stale == [bogus]
    a2 = await db.get(Attempt, uuid.UUID(o2.attempt_id))
    await db.refresh(a2)
    assert a2.state == "lost"
    assert (await agent.commit(o1.attempt_id, o1.task_id, 1)).accepted
    await agent.close()


async def test_member_removal_cancels_jobs_and_unshares(user, enrolled):
    coord, m = await user(), await user()
    g = gname()
    await coord.post("/api/v1/groups", {"name": g, "display_name": "x"}, expect=201)
    tok = (await coord.post(f"/api/v1/groups/{g}/invites", {"role": "member"}, expect=201)).json()["token"]
    await m.post("/api/v1/invites/accept", {"token": tok}, expect=200)
    job = await submit(m, g)
    dev = await enrolled(m)
    await m.req("PUT", f"/api/v1/devices/{dev.device_id}/shares/{g}", expect=200)
    await coord.req("DELETE", f"/api/v1/groups/{g}/members/{m.handle}", expect=204)
    j = await coord.get(f"/api/v1/jobs/{job['id']}")
    assert j["tasks"].get("cancelled") == 1
    assert await coord.get(f"/api/v1/groups/{g}/devices") == []
    events = [e["action"] for e in await coord.get(f"/api/v1/groups/{g}/audit")]
    assert "member.remove" in events
