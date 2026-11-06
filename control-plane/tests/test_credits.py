"""Credits: share to earn, use to spend (docs/design/credits.md)."""

import asyncio
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from conftest import gname
from fake_agent import CAPS, share
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker

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
    await backdate(db, offer.attempt_id, hours)
    return await agent.commit(offer.attempt_id, offer.task_id, offer.epoch, exit_code, usage=usage)


async def backdate(db, attempt_id, hours):
    """As if the attempt started `hours` ago, after credits were turned on. Waits until the server has recorded
    the start: a start recorded after this would undo it (seen on a slow CI runner)."""
    for _ in range(200):
        started = (await db.execute(select(Attempt.started_at).where(Attempt.id == uuid.UUID(attempt_id)))).scalar()
        await db.rollback()  # a fresh view each time
        if started is not None:
            break
        await asyncio.sleep(0.05)
    await db.execute(update(Attempt).where(Attempt.id == uuid.UUID(attempt_id))
                     .values(started_at=text(f"now() - interval '{int(hours * 3600)} seconds'")))
    await db.execute(text("UPDATE groups SET credits_since = now() - interval '30 days' WHERE id = "
                          "(SELECT t.group_id FROM attempts a JOIN tasks t ON t.id = a.task_id WHERE a.id = :a)"),
                     {"a": uuid.UUID(attempt_id)})
    await db.commit()


def maker(db):
    return async_sessionmaker(db.bind, expire_on_commit=False)


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
    # The bigger try succeeds: the small try's charge is reversed in full. Nothing is made.
    offer = await agent.ready()
    assert (await run_for(db, agent, offer, hours=1, usage=used)).accepted
    hist = await coord.get(f"/api/v1/groups/{g}/credits/history")
    assert any("not charged" in (e["note"] or "") for e in hist)
    assert await balance(coord, g) == pytest.approx(19 - 1, abs=0.01), "only the good try is paid"
    assert await balance(owner, g) == pytest.approx(owner_after_oom - (19 - after_oom) + 1, abs=0.01)
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
    await backdate(db, offer.attempt_id, 2)
    a = await db.get(Attempt, uuid.UUID(offer.attempt_id))
    await db.refresh(a)
    t = await db.get(core.Task, a.task_id)
    assert await credits.charge_running(maker(db), utcnow()) == 1  # the hourly charge while it ran
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
    await coord.post(f"/api/v1/groups/{g}/jobs", job(time_limit="6h"), expect=201)
    assert (await run_for(db, agent, await agent.ready(), hours=4)).accepted  # owner earns 5
    other_group = gname()
    await owner.post("/api/v1/groups", {"name": other_group, "display_name": "B", "template": "dev-team"},
                     expect=201)
    body = {"from_group": g, "to_group": other_group}
    r = await owner.post("/api/v1/me/credits/move", {**body, "amount": 5}, expect=422)
    assert "does not take credits from" in r.json()["detail"], "no group takes moved credits unless it says so"
    await owner.req("PATCH", f"/api/v1/groups/{other_group}", 200, json={"credits_from": [g]})
    await owner.post("/api/v1/me/credits/move", {**body, "amount": 6}, expect=422)  # only 5 earned
    r = (await owner.post("/api/v1/me/credits/move", {**body, "amount": 5}, expect=200)).json()
    assert r["target"]["balance"] == 25 and r["target"]["earned"] == 5 and r["source"]["earned"] == 0
    await owner.req("PATCH", f"/api/v1/groups/{other_group}", 200, json={"credits_from": []})
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
    assert isinstance(await agent.ready(), pm.NoWork)  # it asked for work just now
    await credits.pay_standby(maker(db), utcnow(), heartbeat_s=15)
    later = utcnow() + timedelta(minutes=1)
    assert await credits.pay_standby(maker(db), later, heartbeat_s=600) == 1
    entries = (await db.execute(select(CreditEntry).where(CreditEntry.kind == "standby"))).scalars().all()
    assert entries and all(e.amount > 0 for e in entries)
    await agent.close()


async def test_cost():
    r = credits.rates(Group(credit_rates={"cpu_hour": 1, "gib_hours_per_credit": 4, "gpu_hour": 10}))
    # The design's example: 4 CPUs and 4 GiB for 40 minutes = (4 + 1) × 2/3 = 3.3 credits.
    assert float(credits.cost(r, cpu=4, memory_mb=4096, gpus=0, seconds=2400)) == pytest.approx(3.3333, abs=1e-3)
    assert credits.cost(r, cpu=1, memory_mb=0, gpus=1, seconds=3600) == 11
    # Rates outside the allowed ranges count as their nearest limit.
    assert credits.rates(Group(credit_rates={"cpu_hour": 1000, "gib_hours_per_credit": 1e-6, "gpu_hour": 1e9})) == \
        {"cpu_hour": 10.0, "gib_hours_per_credit": 0.5, "gpu_hour": 100.0}


# ---- from the security review of credits ----

async def total_made(db, g):
    """Credits that exist in a group = the ones legitimately made (start, grants, requests, earn while waiting)."""
    gid = (await db.execute(select(Group.id).where(Group.name == g))).scalar_one()
    balances = (await db.execute(select(func.sum(CreditBalance.balance)).where(CreditBalance.group_id == gid)))
    made = (await db.execute(select(func.sum(CreditEntry.amount)).where(
        CreditEntry.group_id == gid, CreditEntry.kind.in_(("start", "grant", "request", "standby")))))
    return balances.scalar_one(), made.scalar_one()


async def test_a_run_is_never_billed_past_its_time_limit(user, enrolled, db):
    coord, g = await credit_group(user)
    owner = await join(coord, g, user)
    agent = await computer(owner, g, enrolled)
    await coord.post(f"/api/v1/groups/{g}/jobs", job(time_limit="1h"), expect=201)
    assert (await run_for(db, agent, await agent.ready(), hours=48)).accepted
    # (1 CPU + 1 GiB / 4) × (1 hour + 60 s) = 1.27, not 48 hours' worth.
    assert await balance(coord, g) == pytest.approx(20 - 1.2708, abs=0.001)
    await agent.close()


async def test_a_task_past_its_time_limit_ends_on_the_server(user, enrolled, db, settings):
    coord, g = await credit_group(user)
    owner = await join(coord, g, user)
    agent = await computer(owner, g, enrolled)
    await coord.post(f"/api/v1/groups/{g}/jobs", job(time_limit="1h"), expect=201)
    offer = await agent.ready()
    await agent.send(pm.Accept(attempt_id=offer.attempt_id, epoch=offer.epoch, tier="T2"))
    await agent.send(pm.Started(attempt_id=offer.attempt_id, epoch=offer.epoch))
    await asyncio.sleep(0.05)
    await backdate(db, offer.attempt_id, 3)
    assert await core.enforce_time_limits(db, settings) == 1
    await db.commit()
    a = await db.get(Attempt, uuid.UUID(offer.attempt_id))
    await db.refresh(a)
    assert (a.state, a.end_reason) == ("lost", "time_limit") and await balance(coord, g) == 20
    await agent.close()


async def test_free_credits_come_back_free_and_debts_never_move(user, enrolled, db):
    coord, g = await credit_group(user)
    owner = await join(coord, g, user)
    agent = await computer(owner, g, enrolled)
    other = gname()
    await owner.post("/api/v1/groups", {"name": other, "display_name": "B", "template": "dev-team"}, expect=201)
    await owner.req("PATCH", f"/api/v1/groups/{other}", 200, json={"credits_from": [g]})
    # The coordinator spends their 20 free start credits and 20 more they do not have: a debt.
    await coord.post(f"/api/v1/groups/{g}/jobs", job(cpu=8, memory="16Gi", time_limit="6h"), expect=201)
    assert (await run_for(db, agent, await agent.ready(), hours=40 / 12 * 1)).accepted  # 12/h × 3.33 h = 40
    mine = await coord.get(f"/api/v1/groups/{g}/credits")
    theirs = await owner.get(f"/api/v1/groups/{g}/credits")
    assert mine["balance"] == pytest.approx(-20, abs=0.1) and mine["free"] == 0
    assert theirs["earned"] == pytest.approx(20, abs=0.1), "only what was paid with real credits can move"
    assert theirs["free"] == pytest.approx(40, abs=0.1), "their 20 start credits + 20 backed by a debt stay here"
    await owner.post("/api/v1/me/credits/move", {"from_group": g, "to_group": other, "amount": 25}, expect=422)
    # A lost task gives free credits back as free credits.
    await coord.post(f"/api/v1/groups/{g}/credits/give", {"to": owner.handle, "amount": 1}, expect=422)  # in debt
    m = await join(coord, g, user)
    await m.post(f"/api/v1/groups/{g}/jobs", job(), expect=201)
    offer = await agent.ready()
    await agent.send(pm.Accept(attempt_id=offer.attempt_id, epoch=offer.epoch, tier="T2"))
    await agent.send(pm.Started(attempt_id=offer.attempt_id, epoch=offer.epoch))
    await asyncio.sleep(0.05)
    await backdate(db, offer.attempt_id, 2)
    assert await credits.charge_running(maker(db), utcnow()) == 1
    assert (await m.get(f"/api/v1/groups/{g}/credits"))["free"] < 20
    await agent.send(pm.Abort(attempt_id=offer.attempt_id, epoch=offer.epoch, reason="owner_returned", detail=""))
    await asyncio.sleep(0.2)
    back = await m.get(f"/api/v1/groups/{g}/credits")
    assert back["balance"] == 20 and back["free"] == 20 and back["earned"] == 0
    balances, made = await total_made(db, g)
    assert balances == made, "no credits came from nowhere"
    await agent.close()


async def test_moved_credits_are_converted_by_the_cpu_rates(user, enrolled, db):
    coord, g = await credit_group(user)
    owner = await join(coord, g, user)
    agent = await computer(owner, g, enrolled)
    await coord.post(f"/api/v1/groups/{g}/jobs", job(memory="0.25Gi", time_limit="6h"), expect=201)
    assert (await run_for(db, agent, await agent.ready(), hours=1)).accepted
    other = gname()
    await owner.post("/api/v1/groups", {"name": other, "display_name": "B", "template": "dev-team"}, expect=201)
    await owner.req("PATCH", f"/api/v1/groups/{other}", 200,
                    json={"credits_from": [g], "credit_rates": {"cpu_hour": 2}})
    earned = (await owner.get(f"/api/v1/groups/{g}/credits"))["earned"]
    r = (await owner.post("/api/v1/me/credits/move", {"from_group": g, "to_group": other, "amount": earned},
                          expect=200)).json()
    assert r["target"]["earned"] == pytest.approx(earned * 2, abs=0.001), "1 CPU hour is worth 2 there"
    await agent.close()


async def test_settings_cannot_make_credits_from_nothing(user):
    coord, g = await credit_group(user)
    for bad in ({"credit_rates": {"cpu_hour": 1000}}, {"credit_rates": {"gib_hours_per_credit": 1e-6}},
                {"credit_start": 1_000_000}, {"credits_from": [g]}):
        await coord.req("PATCH", f"/api/v1/groups/{g}", 422, json=bad)
    # Start credits come once, when you join or credits are turned on: changing them later gives nobody more.
    await coord.req("PATCH", f"/api/v1/groups/{g}", 200, json={"credit_start": 100})
    assert await balance(coord, g) == 20
    r = gname()
    await coord.post("/api/v1/groups", {"name": r, "display_name": "R", "template": "research-team"}, expect=201)
    await coord.req("PATCH", f"/api/v1/groups/{r}", 200, json={"credit_start": 100, "credits_enabled": True})
    assert await balance(coord, r) == 100, "at most 100, for everyone who is a member when credits are turned on"


async def test_rates_changed_during_a_run_do_not_reprice_it(user, enrolled, db):
    coord, g = await credit_group(user)
    owner = await join(coord, g, user)
    agent = await computer(owner, g, enrolled)
    await coord.post(f"/api/v1/groups/{g}/jobs", job(time_limit="6h"), expect=201)
    offer = await agent.ready()
    await coord.req("PATCH", f"/api/v1/groups/{g}", 200, json={"credit_rates": {"cpu_hour": 0.1}})
    assert (await run_for(db, agent, offer, hours=2)).accepted
    assert await balance(coord, g) == pytest.approx(17.5, abs=0.01), "billed at the rates when it started"
    await agent.close()


async def test_wait_needs_credits_for_each_task_it_starts(user, enrolled, db):
    coord, g = await credit_group(user, out_of_credits="wait")
    m = await join(coord, g, user)  # 20 credits: 16 hours of 1 CPU + 1 GiB, or one hour of 16 such tasks
    await m.post(f"/api/v1/groups/{g}/jobs", job(cpu=8, memory="4Gi", parts=6), expect=201)  # 9 credits an hour
    agent = await computer(coord, g, enrolled)
    started = 0
    for _ in range(6):
        if isinstance(await agent.ready(), pm.Offer):
            started += 1
    assert started == 2, "20 credits cover an hour of two 9-credit tasks, not six"
    await agent.close()


async def test_earn_while_waiting_trusts_no_claimed_cores(user, enrolled, db):
    coord, g = await credit_group(user, standby_percent=100)
    owner = await join(coord, g, user)
    agent = await enrolled(owner)
    await owner.req("PUT", f"/api/v1/devices/{agent.device_id}/shares/{g}", expect=200)
    await agent.connect([share(g)], caps={**CAPS, "cpu_cores": 10**9})
    await agent.heartbeat([], shares=[share(g)])
    await credits.pay_standby(maker(db), utcnow(), 15)  # starts counting
    assert await credits.pay_standby(maker(db), utcnow() + timedelta(minutes=1), 600) == 0, \
        "it never asked for work: not paid"
    assert isinstance(await agent.ready(), pm.NoWork)
    assert await credits.pay_standby(maker(db), utcnow() + timedelta(minutes=2), 600) == 1
    paid = (await db.execute(select(func.max(CreditEntry.amount)).where(CreditEntry.kind == "standby"))).scalar_one()
    assert paid <= Decimal("0.5334"), "at most 16 cores for at most 2 minutes"
    await agent.close()


async def test_small_and_odd_amounts_and_ledger_rows(user, db):
    coord, g = await credit_group(user)
    m = await join(coord, g, user)
    await m.post(f"/api/v1/groups/{g}/credits/requests", {"amount": 0.00001, "reason": "x"}, expect=422)
    with pytest.raises(Exception, match="never changed"):
        await db.execute(text("UPDATE credit_entries SET amount = 1000"))
    await db.rollback()
