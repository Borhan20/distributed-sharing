"""Credits: share to earn, use to spend (docs/design/credits.md)."""

import asyncio
import uuid
from datetime import timedelta

import pytest
from conftest import gname
from fake_agent import share
from sqlalchemy import select, text, update

from pool_cp import credits
from pool_cp.models import Attempt, CreditBalance, CreditEntry, Group
from pool_cp.scheduler import core
from pool_cp.timeutil import utcnow
from pool_protocol import messages as pm

pytestmark = pytest.mark.asyncio

IMG = "registry.example.org/lab/app:1@sha256:" + "d" * 64


async def credit_group(user, **settings):
    coord = await user()
    g = gname()
    await coord.post("/api/v1/groups", {"name": g, "display_name": "Credits", "template": "dev-team"}, expect=201)
    if settings:
        await coord.req("PATCH", f"/api/v1/groups/{g}", 200, json=settings)
    return coord, g


async def join(coord, g, user, role="member"):
    other = await user()
    inv = (await coord.post(f"/api/v1/groups/{g}/invites", {"role": role}, expect=201)).json()
    await other.post("/api/v1/invites/accept", {"token": inv["token"]}, expect=200)
    return other


async def computer(owner, g, enrolled):
    agent = await enrolled(owner)
    await owner.req("PUT", f"/api/v1/devices/{agent.device_id}/shares/{g}", expect=200)
    await agent.connect([share(g)])
    return agent


async def balance(c, g):
    return (await c.get(f"/api/v1/groups/{g}/credits"))["balance"]


async def run_for(db, agent, offer, hours=1.0, exit_code=0, usage=None):
    """Run a task as if it took `hours`."""
    await agent.send(pm.Accept(attempt_id=offer.attempt_id, epoch=offer.epoch, tier="T2"))
    await agent.send(pm.Started(attempt_id=offer.attempt_id, epoch=offer.epoch))
    await agent.send(pm.Progress(attempt_id=offer.attempt_id, epoch=offer.epoch, seq=0, log_chunk="x"))
    await asyncio.sleep(0.05)
    await db.execute(update(Attempt).where(Attempt.id == uuid.UUID(offer.attempt_id))
                     .values(started_at=text(f"now() - interval '{int(hours * 3600)} seconds'")))
    await db.commit()
    return await agent.commit(offer.attempt_id, offer.task_id, offer.epoch, exit_code, usage=usage)


def job(**extra):
    return {"image": IMG, "command": ["run"], "cpu": 1, "memory": "1Gi", "time_limit": "2h", **extra}


async def test_templates_and_start_credits(user):
    coord, g = await credit_group(user)
    grp = await coord.get(f"/api/v1/groups/{g}")
    assert grp["credits_enabled"] is True and grp["credit_start"] == 20
    assert await balance(coord, g) == 20
    other = await user()
    r = gname()
    await other.post("/api/v1/groups", {"name": r, "display_name": "R", "template": "research-team"}, expect=201)
    assert (await other.get(f"/api/v1/groups/{r}/credits"))["enabled"] is False


async def test_the_owner_earns_what_the_user_pays_and_your_own_computer_is_free(user, enrolled, db):
    coord, g = await credit_group(user)
    owner = await join(coord, g, user)
    agent = await computer(owner, g, enrolled)
    await coord.post(f"/api/v1/groups/{g}/jobs", job(), expect=201)
    offer = await agent.ready()
    assert (await run_for(db, agent, offer, hours=1)).accepted
    # (1 CPU + 1 GiB / 4) × 1 hour = 1.25 credits.
    assert await balance(coord, g) == pytest.approx(18.75, abs=0.01)
    assert await balance(owner, g) == pytest.approx(21.25, abs=0.01)
    hist = await owner.get(f"/api/v1/groups/{g}/credits/history")
    assert hist[0]["kind"] == "earn" and hist[0]["counterparty"] == coord.handle
    # The owner's own job on their own computer: free.
    await owner.post(f"/api/v1/groups/{g}/jobs", job(), expect=201)
    before = await balance(owner, g)
    assert (await run_for(db, agent, await agent.ready(), hours=1)).accepted
    assert await balance(owner, g) == before
    await agent.close()


async def test_sizes_pool_chose_are_paid_by_use_and_pool_pays_its_own_retries(user, enrolled, db):
    coord, g = await credit_group(user)
    owner = await join(coord, g, user)
    agent = await computer(owner, g, enrolled)
    j = (await coord.post(f"/api/v1/groups/{g}/jobs", {"image": IMG, "command": ["run"]}, expect=201)).json()
    assert j["credits"]["expected"] is None and j["credits"]["most"] > 0
    offer = await agent.ready()  # a first run: all the free memory of the computer
    used = {"peak_memory_mb": 2048, "cpu_seconds": 1800, "peak_cpu": 0.5, "wall_seconds": 3600, "samples": 3600}
    assert (await run_for(db, agent, offer, hours=1, usage=used)).accepted
    # Paid by use: (0.5 CPU + 2 GiB / 4) × 1 hour = 1 credit, not for the whole computer it held.
    assert await balance(coord, g) == pytest.approx(19, abs=0.01)
    # Next run, learned size; it runs out of memory. Until a bigger try succeeds, the person pays for it.
    await coord.post(f"/api/v1/groups/{g}/jobs", {"image": IMG, "command": ["run"]}, expect=201)
    offer = await agent.ready()
    oom = {**used, "oom_killed": True}
    assert (await run_for(db, agent, offer, hours=1, exit_code=137, usage=oom)).accepted
    after_oom = await balance(coord, g)
    assert after_oom < 19, "failing on purpose makes no free credits"
    owner_after_oom = await balance(owner, g)
    # The bigger try succeeds: pool pays the small try back to the person; the owner keeps what they earned.
    offer = await agent.ready()
    assert (await run_for(db, agent, offer, hours=1, usage=used)).accepted
    hist = await coord.get(f"/api/v1/groups/{g}/credits/history")
    assert "pool_retry" in [e["kind"] for e in hist]
    assert await balance(coord, g) == pytest.approx(19 - 1, abs=0.01), "only the good try is paid"
    assert await balance(owner, g) > owner_after_oom
    await agent.close()


async def test_nothing_is_charged_when_the_owner_takes_the_computer_back(user, enrolled, db):
    coord, g = await credit_group(user)
    owner = await join(coord, g, user)
    agent = await computer(owner, g, enrolled)
    await coord.post(f"/api/v1/groups/{g}/jobs", job(), expect=201)
    offer = await agent.ready()
    await agent.send(pm.Accept(attempt_id=offer.attempt_id, epoch=offer.epoch, tier="T2"))
    await agent.send(pm.Started(attempt_id=offer.attempt_id, epoch=offer.epoch))
    await asyncio.sleep(0.05)
    await db.execute(update(Attempt).where(Attempt.id == uuid.UUID(offer.attempt_id))
                     .values(started_at=text("now() - interval '2 hours'")))
    await db.commit()
    a = await db.get(Attempt, uuid.UUID(offer.attempt_id))
    await db.refresh(a)
    t = await db.get(core.Task, a.task_id)
    assert await credits.charge_running(db, utcnow()) == 1  # the hourly charge while it ran
    await db.commit()
    assert await balance(coord, g) < 20
    await agent.send(pm.Abort(attempt_id=offer.attempt_id, epoch=offer.epoch, reason="owner_returned", detail=""))
    await asyncio.sleep(0.2)
    assert await balance(coord, g) == 20, "the lost part is not paid for: the hourly charge comes back"
    assert t is not None
    await agent.close()


async def test_out_of_credits_runs_last_or_waits(user, enrolled, db):
    coord, g = await credit_group(user)
    broke = await join(coord, g, user)
    rich = await join(coord, g, user)
    gid = (await db.execute(select(Group.id).where(Group.name == g))).scalar_one()
    await broke.get(f"/api/v1/groups/{g}/credits")
    bid = (await db.execute(text("SELECT id FROM users WHERE handle = :h"), {"h": broke.handle})).scalar_one()
    await db.execute(update(CreditBalance).where(CreditBalance.group_id == gid, CreditBalance.user_id == bid)
                     .values(balance=0, free=0))
    await db.commit()
    first = (await broke.post(f"/api/v1/groups/{g}/jobs", job(name="broke"), expect=201)).json()
    second = (await rich.post(f"/api/v1/groups/{g}/jobs", job(name="rich"), expect=201)).json()
    agent = await computer(coord, g, enrolled)
    offer = await agent.ready()
    assert offer.task_id in {t["id"] for t in await rich.get(f"/api/v1/jobs/{second['id']}/tasks")}, \
        "members with credits go first"
    assert (await agent.run_task(offer)).accepted
    offer = await agent.ready()
    assert isinstance(offer, pm.Offer), "lowest priority: it still runs when nobody with credits waits"
    assert (await agent.run_task(offer)).accepted
    await coord.req("PATCH", f"/api/v1/groups/{g}", 200, json={"out_of_credits": "wait"})
    await broke.post(f"/api/v1/groups/{g}/jobs", job(name="again"), expect=201)
    assert isinstance(await agent.ready(), pm.NoWork), "wait: no credits, no run"
    assert first["id"]
    await agent.close()


async def test_grants_and_requests(user):
    coord, g = await credit_group(user)
    m = await join(coord, g, user)
    r = await coord.post(f"/api/v1/groups/{g}/credits/grant", {"to": m.handle, "amount": 30, "reason": "thesis"},
                         expect=200)
    assert r.json()["balance"] == 50 and r.json()["free"] == 50
    await coord.post(f"/api/v1/groups/{g}/credits/grant", {"to": coord.handle, "amount": 5, "reason": "me"},
                     expect=422)
    await m.post(f"/api/v1/groups/{g}/credits/grant", {"to": m.handle, "amount": 5, "reason": "x"}, expect=403)
    req = (await m.post(f"/api/v1/groups/{g}/credits/requests", {"amount": 40, "reason": "experiments"},
                        expect=201)).json()
    await m.post(f"/api/v1/groups/{g}/credits/requests", {"amount": 1, "reason": "again"}, expect=409)
    assert [x["id"] for x in await coord.get(f"/api/v1/groups/{g}/credits/requests")] == [req["id"]]
    await m.post(f"/api/v1/groups/{g}/credits/requests/{req['id']}/approve", {}, expect=403)
    await coord.post(f"/api/v1/groups/{g}/credits/requests/{req['id']}/approve", {"amount": 50}, expect=422)
    ok = (await coord.post(f"/api/v1/groups/{g}/credits/requests/{req['id']}/approve", {"amount": 25},
                           expect=200)).json()
    assert ok["state"] == "approved" and ok["granted"] == 25 and await balance(m, g) == 75
    await coord.post(f"/api/v1/groups/{g}/credits/requests/{req['id']}/approve", {}, expect=409)
    nxt = (await m.post(f"/api/v1/groups/{g}/credits/requests", {"amount": 5, "reason": "more"}, expect=201)).json()
    assert (await coord.post(f"/api/v1/groups/{g}/credits/requests/{nxt['id']}/decline", expect=200)).json()[
        "state"] == "declined"
    audit = await coord.get(f"/api/v1/groups/{g}/audit")
    assert {"credits.grant", "credits.approve", "credits.decline"} <= {a["action"] for a in audit}


async def test_giving_keeps_free_credits_free_and_never_overdraws(user):
    coord, g = await credit_group(user)
    m = await join(coord, g, user)
    await coord.post(f"/api/v1/groups/{g}/credits/give", {"to": m.handle, "amount": 5}, expect=200)
    mine = await m.get(f"/api/v1/groups/{g}/credits")
    assert mine["balance"] == 25 and mine["free"] == 25, "start credits given stay free"
    await coord.post(f"/api/v1/groups/{g}/credits/give", {"to": m.handle, "amount": 16}, expect=422)
    await coord.post(f"/api/v1/groups/{g}/credits/give", {"to": coord.handle, "amount": 1}, expect=422)
    await coord.post(f"/api/v1/groups/{g}/credits/give", {"to": "nobody-here", "amount": 1}, expect=404)
    # Two gives at the same moment that together are more than the balance: one is refused.
    results = await asyncio.gather(*(m.req("POST", f"/api/v1/groups/{g}/credits/give",
                                           json={"to": coord.handle, "amount": 15}) for _ in range(2)))
    assert sorted(r.status_code for r in results) == [200, 422]
    assert await balance(m, g) == 10


async def test_only_earned_credits_move_and_both_groups_must_allow_it(user, enrolled, db):
    coord, g = await credit_group(user)
    owner = await join(coord, g, user)
    agent = await computer(owner, g, enrolled)
    await coord.post(f"/api/v1/groups/{g}/jobs", job(), expect=201)
    assert (await run_for(db, agent, await agent.ready(), hours=4)).accepted  # owner earns 5
    other_group = gname()
    await owner.post("/api/v1/groups", {"name": other_group, "display_name": "B", "template": "dev-team"},
                     expect=201)
    body = {"from_group": g, "to_group": other_group}
    await owner.post("/api/v1/me/credits/move", {**body, "amount": 6}, expect=422)  # only 5 earned
    r = (await owner.post("/api/v1/me/credits/move", {**body, "amount": 5}, expect=200)).json()
    assert r["target"]["balance"] == 25 and r["target"]["earned"] == 5 and r["source"]["earned"] == 0
    await owner.req("PATCH", f"/api/v1/groups/{other_group}", 200, json={"credits_in": False})
    await owner.post("/api/v1/me/credits/move", {"from_group": g, "to_group": other_group, "amount": 1},
                     expect=422)
    await coord.post("/api/v1/me/credits/move", {"from_group": g, "to_group": other_group, "amount": 1},
                     expect=404)  # not a member of the other group
    await agent.close()


async def test_member_balances_are_for_managers_only(user):
    coord, g = await credit_group(user)
    m = await join(coord, g, user)
    rows = await coord.get(f"/api/v1/groups/{g}/credits/members")
    assert {r["member"] for r in rows} == {coord.handle, m.handle}
    await m.req("GET", f"/api/v1/groups/{g}/credits/members", 403)


async def test_earn_while_waiting(user, enrolled, db):
    coord, g = await credit_group(user, standby_percent=10)
    owner = await join(coord, g, user)
    agent = await computer(owner, g, enrolled)
    await agent.heartbeat([], shares=[share(g)])
    await credits.pay_standby(db, utcnow(), heartbeat_s=15)
    await db.commit()
    later = utcnow() + timedelta(minutes=1)
    assert await credits.pay_standby(db, later, heartbeat_s=600) == 1
    await db.commit()
    entries = (await db.execute(select(CreditEntry).where(CreditEntry.kind == "standby"))).scalars().all()
    assert entries and all(e.amount > 0 for e in entries)
    await agent.close()


async def test_cost():
    g = Group(credit_rates={"cpu_hour": 1, "gib_hours_per_credit": 4, "gpu_hour": 10})
    # The design's example: 4 CPUs and 4 GiB for 40 minutes = (4 + 1) × 2/3 = 3.3 credits.
    assert float(credits.cost(g, cpu=4, memory_mb=4096, gpus=0, seconds=2400)) == pytest.approx(3.3333, abs=1e-3)
    assert credits.cost(g, cpu=1, memory_mb=0, gpus=1, seconds=3600) == 11
