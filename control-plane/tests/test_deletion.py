"""Deleting jobs and groups, removing computers from groups (decisions of 3 November 2026).

- A deletion keeps the audit log and, for each computer's owner, a "what ran here" line.
- A deleted group can be restored for 7 days; then it is purged.
- A computer a coordinator removed stays out until a coordinator or admin lets it back in.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from conftest import gname, job_spec
from fake_agent import share
from sqlalchemy import select, text

from pool_cp.deletion import purge_deleted_groups
from pool_cp.models import Group, RunHistory, UsageLedger
from pool_cp.timeutil import utcnow
from pool_protocol import messages as pm

pytestmark = pytest.mark.asyncio


async def new_group(owner) -> str:
    name = gname()
    await owner.post("/api/v1/groups", {"name": name, "display_name": "Test"}, expect=201)
    return name


async def join(coord, group: str, person, role: str = "member") -> None:
    inv = (await coord.post(f"/api/v1/groups/{group}/invites", {"role": role, "max_uses": 1}, expect=201)).json()
    await person.post("/api/v1/invites/accept", {"token": inv["token"]}, expect=200)


async def submit(client, group: str) -> str:
    return (await client.post(f"/api/v1/groups/{group}/jobs", job_spec(), expect=201)).json()["id"]


# --------------------------------------------------------------------------- jobs


async def test_submitter_deletes_a_finished_job_and_the_owner_keeps_its_history(group_with_device, db):
    owner, group, agent = await group_with_device()
    await agent.connect([share(group)])
    job_id = await submit(owner, group)
    offer = await agent.ready()
    assert (await agent.run_task(offer)).accepted
    r = (await owner.req("DELETE", f"/api/v1/jobs/{job_id}", expect=200)).json()
    assert r == {"deleted": True, "runs_kept_for_owners": 1, "files_deleted": 0}  # no file store in unit tests
    await owner.req("GET", f"/api/v1/jobs/{job_id}", expect=404)
    history = await owner.get(f"/api/v1/devices/{agent.device_id}/history")
    assert [(h["job_name"], h["job_deleted"], h["state"]) for h in history] == [("job", True, "succeeded")]
    usage = (await db.execute(select(UsageLedger).where(UsageLedger.attempt_id == uuid.UUID(offer.attempt_id))))
    assert usage.scalars().first() is not None, "usage stays, so deleting jobs cannot reset fair share"
    audit = await owner.get(f"/api/v1/groups/{group}/audit")
    assert any(e["action"] == "job.delete" and e["details"]["name"] == "job" for e in audit)


async def test_deleting_a_running_job_stops_its_work(group_with_device):
    owner, group, agent = await group_with_device()
    await agent.connect([share(group)])
    job_id = await submit(owner, group)
    offer = await agent.ready()
    await agent.send(pm.Accept(attempt_id=offer.attempt_id, epoch=offer.epoch, tier="T2"))
    await agent.send(pm.Started(attempt_id=offer.attempt_id, epoch=offer.epoch))
    await agent.heartbeat([(offer.attempt_id, offer.epoch, "running")])  # sync point: started is applied
    await owner.req("DELETE", f"/api/v1/jobs/{job_id}", expect=200)
    cancel = await agent.recv_type(pm.Cancel)
    assert cancel.attempt_id == offer.attempt_id


async def test_only_the_submitter_or_a_coordinator_deletes_a_job(user):
    coord, alice, bob = await user(), await user(), await user()
    group = await new_group(coord)
    await join(coord, group, alice)
    await join(coord, group, bob)
    job_id = await submit(alice, group)
    r = await bob.req("DELETE", f"/api/v1/jobs/{job_id}", expect=403)
    assert "role" in r.json()["detail"]
    outsider = await user()
    await outsider.req("DELETE", f"/api/v1/jobs/{job_id}", expect=404)
    await coord.req("DELETE", f"/api/v1/jobs/{job_id}", expect=200)


async def test_deleting_someone_elses_job_needs_two_step_sign_in(user, http, settings, monkeypatch):
    monkeypatch.setattr(settings, "require_mfa_for_admins", True)  # as on a shared server (off in dev mode)
    coord_pw_only = await user(mfa=False)
    group = await new_group(coord_pw_only)
    alice = await user()
    # Inviting needs MFA too, so let a second, MFA-signed-in session of the same coordinator do it.
    r = await http.post("/dev/token", json={"username": coord_pw_only.username, "mfa": True})
    from conftest import Client
    coord_mfa = Client(http, coord_pw_only.username, r.json()["access_token"])
    await join(coord_mfa, group, alice)
    job_id = await submit(alice, group)
    r = await coord_pw_only.req("DELETE", f"/api/v1/jobs/{job_id}", expect=403)
    assert "two-step sign-in" in r.json()["detail"]
    await coord_mfa.req("DELETE", f"/api/v1/jobs/{job_id}", expect=200)


# --------------------------------------------------------------------------- groups


async def test_delete_group_hides_it_and_restore_brings_it_back(user):
    coord = await user()
    group = await new_group(coord)
    job_id = await submit(coord, group)
    await coord.req("DELETE", f"/api/v1/groups/{group}", json={"confirm_name": "wrong"}, expect=400)
    gone = (await coord.req("DELETE", f"/api/v1/groups/{group}", json={"confirm_name": group}, expect=200)).json()
    assert gone["name"] == group
    await coord.req("GET", f"/api/v1/groups/{group}", expect=404)
    await coord.req("GET", f"/api/v1/jobs/{job_id}", expect=404)
    assert group not in [g["name"] for g in await coord.get("/api/v1/groups")]
    listed = [g["name"] for g in await coord.get("/api/v1/deleted-groups")]
    assert group in listed
    restored = (await coord.req("POST", f"/api/v1/groups/{group}/restore", expect=200)).json()
    assert restored["state"] == "active" and restored["my_role"] == "coordinator"
    tasks = await coord.get(f"/api/v1/jobs/{job_id}/tasks")
    assert {t["state"] for t in tasks} == {"cancelled"}, "work stopped by the deletion stays stopped"
    await coord.req("POST", f"/api/v1/groups/{group}/restore", expect=409)


async def test_only_the_coordinator_deletes_a_group(user):
    coord, alice = await user(), await user()
    group = await new_group(coord)
    await join(coord, group, alice, role="admin")
    await alice.req("DELETE", f"/api/v1/groups/{group}", json={"confirm_name": group}, expect=403)
    outsider = await user()
    await outsider.req("DELETE", f"/api/v1/groups/{group}", json={"confirm_name": group}, expect=404)
    assert group not in [g["name"] for g in await alice.get("/api/v1/deleted-groups")]


async def test_server_admin_restores_any_deleted_group(user):
    coord = await user()
    group = await new_group(coord)
    await coord.req("DELETE", f"/api/v1/groups/{group}", json={"confirm_name": group}, expect=200)
    admin = await user("root-admin")  # a server admin in the test settings
    assert group in [g["name"] for g in await admin.get("/api/v1/deleted-groups")]
    await admin.req("POST", f"/api/v1/groups/{group}/restore", expect=200)
    stranger = await user()
    await stranger.req("POST", f"/api/v1/groups/{group}/restore", expect=404)


async def test_purge_after_the_grace_period_keeps_owners_history_and_the_audit_log(group_with_device, db):
    owner, group, agent = await group_with_device()
    await agent.connect([share(group)])
    await submit(owner, group)
    offer = await agent.ready()
    assert (await agent.run_task(offer)).accepted
    await owner.req("DELETE", f"/api/v1/groups/{group}", json={"confirm_name": group}, expect=200)
    g = (await db.execute(select(Group).where(Group.name == group))).scalar_one()
    gid = g.id
    assert await purge_deleted_groups(db) == [] or gid not in await purge_deleted_groups(db)  # still in grace
    g.purge_after = utcnow() - timedelta(seconds=1)
    await db.commit()
    assert gid in await purge_deleted_groups(db)
    await db.commit()
    assert (await db.execute(select(Group).where(Group.id == gid))).scalar_one_or_none() is None
    kept = (await db.execute(select(RunHistory).where(RunHistory.group_name == group))).scalars().all()
    assert [r.attempt_id for r in kept] == [uuid.UUID(offer.attempt_id)]
    actions = (await db.execute(text("SELECT action FROM audit_events WHERE group_id = :g ORDER BY id"),
                                {"g": gid})).scalars().all()
    assert "group.delete" in actions and actions[-1] == "group.purge"
    history = await owner.get(f"/api/v1/devices/{agent.device_id}/history")
    assert history[0]["group"] == group and history[0]["job_deleted"] is True


# --------------------------------------------------------------------------- computers


async def test_coordinator_removes_a_computer_and_it_stays_out_until_allowed(user, enrolled):
    coord, owner = await user(), await user()
    group = await new_group(coord)
    await join(coord, group, owner)
    agent = await enrolled(owner)
    await agent.connect([share(group)])  # one-step sharing from the owner's policy
    dev = agent.device_id
    removed = (await coord.req("DELETE", f"/api/v1/groups/{group}/devices/{dev}", expect=200)).json()
    assert removed["owner"] == owner.handle
    device = await owner.get(f"/api/v1/devices/{dev}")
    assert device["removed_from"] == [group] and device["shared_with"] == [] and device["turned_off"] == []
    r = await owner.req("PUT", f"/api/v1/devices/{dev}/shares/{group}", expect=403)
    assert "removed this computer" in r.json()["detail"]
    await agent.heartbeat([], shares=[share(group)])  # the policy still offers the group
    job_id = await submit(coord, group)
    assert isinstance(await agent.ready(), pm.NoWork)
    reasons = (await coord.get(f"/api/v1/jobs/{job_id}/waiting"))["reasons"]
    assert "removed from this group by a coordinator" in reasons[0]["message"], reasons
    assert [d["id"] for d in await coord.get(f"/api/v1/groups/{group}/removed-devices")] == [dev]
    member = await user()
    await join(coord, group, member)
    await member.req("POST", f"/api/v1/groups/{group}/devices/{dev}/allow", expect=403)
    await coord.req("POST", f"/api/v1/groups/{group}/devices/{dev}/allow", expect=200)
    assert (await owner.get(f"/api/v1/devices/{dev}"))["shared_with"] == [group]
    assert isinstance(await agent.ready(), pm.Offer)


async def test_server_admin_revokes_a_lost_computer(user, enrolled):
    owner = await user()
    agent = await enrolled(owner)
    stranger = await user()
    await stranger.req("POST", f"/api/v1/devices/{agent.device_id}/revoke", expect=404)
    admin = await user("root-admin")
    revoked = (await admin.req("POST", f"/api/v1/devices/{agent.device_id}/revoke", expect=200)).json()
    assert revoked["state"] == "revoked"
