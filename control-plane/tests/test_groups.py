"""Groups, roles, invites, join requests and removal (plan §6.2-6.3)."""

import pytest
from conftest import gname, job_spec

pytestmark = pytest.mark.asyncio


async def test_requires_valid_token(http):
    assert (await http.get("/api/v1/me")).status_code == 401
    r = await http.get("/api/v1/me", headers={"Authorization": "Bearer not-a-jwt"})
    assert r.status_code == 401


async def test_create_group_and_duplicate(user):
    c = await user()
    name = gname()
    g = (await c.post("/api/v1/groups", {"name": name, "display_name": "Lab"}, expect=201)).json()
    assert g["my_role"] == "coordinator" and g["approval_rule"] == "coordinator-only"
    await c.post("/api/v1/groups", {"name": name, "display_name": "Lab"}, expect=409)
    await c.post("/api/v1/groups", {"name": "Bad Name", "display_name": "x"}, expect=422)


async def test_templates(user):
    c = await user()
    g = (await c.post("/api/v1/groups", {"name": gname(), "display_name": "H", "template": "hackathon"},
                      expect=201)).json()
    assert g["expires_at"] is not None and g["retention_days"] == 7
    s = (await c.post("/api/v1/groups", {"name": gname(), "display_name": "S", "template": "student-group"},
                      expect=201)).json()
    assert s["approval_rule"] == "any-k-members" and s["approval_k"] == 2 and s["probation_days"] == 7


async def test_non_member_sees_404(user):
    a, b = await user(), await user()
    name = gname()
    await a.post("/api/v1/groups", {"name": name, "display_name": "x"}, expect=201)
    await b.req("GET", f"/api/v1/groups/{name}", expect=404)
    await b.req("GET", f"/api/v1/groups/{name}/members", expect=404)


async def test_invite_flow_and_limits(user):
    coord, alice, bob = await user(), await user(), await user()
    name = gname()
    await coord.post("/api/v1/groups", {"name": name, "display_name": "x"}, expect=201)
    inv = (await coord.post(f"/api/v1/groups/{name}/invites", {"role": "submitter", "max_uses": 1},
                            expect=201)).json()
    assert inv["token"]
    joined = (await alice.post("/api/v1/invites/accept", {"token": inv["token"]}, expect=200)).json()
    assert joined["my_role"] == "submitter"
    await bob.post("/api/v1/invites/accept", {"token": inv["token"]}, expect=404)  # used up
    await alice.post("/api/v1/invites/accept", {"token": "x" * 20}, expect=404)
    # Listing never shows tokens again.
    listed = await coord.get(f"/api/v1/groups/{name}/invites")
    assert listed[0]["token"] is None and listed[0]["uses"] == 1
    # Submitters cannot manage invites.
    await alice.post(f"/api/v1/groups/{name}/invites", {"role": "member"}, expect=403)


async def test_role_permissions(user, enrolled):
    coord, contrib, sub = await user(), await user(), await user()
    name = gname()
    await coord.post("/api/v1/groups", {"name": name, "display_name": "x"}, expect=201)
    for who, role in ((contrib, "contributor"), (sub, "submitter")):
        tok = (await coord.post(f"/api/v1/groups/{name}/invites", {"role": role}, expect=201)).json()["token"]
        await who.post("/api/v1/invites/accept", {"token": tok}, expect=200)
    # contributor: share yes, submit no
    await contrib.post(f"/api/v1/groups/{name}/jobs", job_spec(), expect=403)
    dev = await enrolled(contrib)
    await contrib.req("PUT", f"/api/v1/devices/{dev.device_id}/shares/{name}", expect=200)
    # submitter: submit yes, share no
    await sub.post(f"/api/v1/groups/{name}/jobs", job_spec(), expect=201)
    dev2 = await enrolled(sub)
    await sub.req("PUT", f"/api/v1/devices/{dev2.device_id}/shares/{name}", expect=403)
    # nobody can share someone else's device
    await coord.req("PUT", f"/api/v1/devices/{dev.device_id}/shares/{name}", expect=404)
    # only managers read the audit log
    await sub.req("GET", f"/api/v1/groups/{name}/audit", expect=403)
    assert len(await coord.get(f"/api/v1/groups/{name}/audit")) >= 3


async def test_join_request_coordinator_only(user):
    coord, admin, member, joiner = await user(), await user(), await user(), await user()
    name = gname()
    await coord.post("/api/v1/groups", {"name": name, "display_name": "x"}, expect=201)
    for who, role in ((admin, "admin"), (member, "member")):
        tok = (await coord.post(f"/api/v1/groups/{name}/invites", {"role": role}, expect=201)).json()["token"]
        await who.post("/api/v1/invites/accept", {"token": tok}, expect=200)
    jr = (await joiner.post(f"/api/v1/groups/{name}/join-requests", {"message": "hi"}, expect=201)).json()
    await joiner.post(f"/api/v1/groups/{name}/join-requests", {"message": "again"}, expect=409)
    await member.post(f"/api/v1/groups/{name}/join-requests/{jr['id']}/votes", {"decision": "approve"}, expect=403)
    out = (await admin.post(f"/api/v1/groups/{name}/join-requests/{jr['id']}/votes", {"decision": "approve"},
                            expect=200)).json()
    assert out["state"] == "approved"
    assert (await joiner.get(f"/api/v1/groups/{name}"))["my_role"] == "member"


async def test_join_request_any_k_members(user):
    coord, m1, joiner = await user(), await user(), await user()
    name = gname()
    await coord.post("/api/v1/groups", {"name": name, "display_name": "x", "approval_rule": "any-k-members",
                                        "approval_k": 2}, expect=201)
    tok = (await coord.post(f"/api/v1/groups/{name}/invites", {"role": "member"}, expect=201)).json()["token"]
    await m1.post("/api/v1/invites/accept", {"token": tok}, expect=200)
    jr = (await joiner.post(f"/api/v1/groups/{name}/join-requests", {}, expect=201)).json()
    await joiner.post(f"/api/v1/groups/{name}/join-requests/{jr['id']}/votes", {"decision": "approve"}, expect=404)
    s1 = (await m1.post(f"/api/v1/groups/{name}/join-requests/{jr['id']}/votes", {"decision": "approve"},
                        expect=200)).json()
    assert s1["state"] == "pending" and s1["approvals"] == 1 and s1["needed"] == 2
    s2 = (await coord.post(f"/api/v1/groups/{name}/join-requests/{jr['id']}/votes", {"decision": "approve"},
                           expect=200)).json()
    assert s2["state"] == "approved"


async def test_join_request_majority_rejection(user):
    coord, m1, m2, joiner = await user(), await user(), await user(), await user()
    name = gname()
    await coord.post("/api/v1/groups", {"name": name, "display_name": "x", "approval_rule": "majority"}, expect=201)
    for who in (m1, m2):
        tok = (await coord.post(f"/api/v1/groups/{name}/invites", {"role": "member"}, expect=201)).json()["token"]
        await who.post("/api/v1/invites/accept", {"token": tok}, expect=200)
    jr = (await joiner.post(f"/api/v1/groups/{name}/join-requests", {}, expect=201)).json()
    s = (await m1.post(f"/api/v1/groups/{name}/join-requests/{jr['id']}/votes", {"decision": "reject"},
                       expect=200)).json()
    assert s["state"] == "pending" and s["needed"] == 2
    s = (await m2.post(f"/api/v1/groups/{name}/join-requests/{jr['id']}/votes", {"decision": "reject"},
                       expect=200)).json()
    assert s["state"] == "rejected"
    await joiner.req("GET", f"/api/v1/groups/{name}", expect=404)


async def test_last_coordinator_cannot_leave_and_role_change(user):
    coord, m = await user(), await user()
    name = gname()
    await coord.post("/api/v1/groups", {"name": name, "display_name": "x"}, expect=201)
    await coord.req("DELETE", f"/api/v1/groups/{name}/members/{coord.handle}", expect=409)
    await coord.req("PATCH", f"/api/v1/groups/{name}/members/{coord.handle}", json={"role": "member"}, expect=409)
    tok = (await coord.post(f"/api/v1/groups/{name}/invites", {"role": "member"}, expect=201)).json()["token"]
    await m.post("/api/v1/invites/accept", {"token": tok}, expect=200)
    await m.req("PATCH", f"/api/v1/groups/{name}/members/{coord.handle}", json={"role": "member"}, expect=403)
    await coord.req("PATCH", f"/api/v1/groups/{name}/members/{m.handle}", json={"role": "admin"}, expect=200)
    await m.req("DELETE", f"/api/v1/groups/{name}/members/{m.handle}", expect=204)  # leaving is always allowed


async def test_close_group_needs_confirmation(user):
    c = await user()
    name = gname()
    await c.post("/api/v1/groups", {"name": name, "display_name": "x"}, expect=201)
    await c.post(f"/api/v1/groups/{name}/jobs", job_spec(), expect=201)
    await c.post(f"/api/v1/groups/{name}/close", {}, expect=400)
    g = (await c.post(f"/api/v1/groups/{name}/close", {"confirm": True}, expect=200)).json()
    assert g["state"] == "closed"
    jobs = await c.get(f"/api/v1/groups/{name}/jobs")
    assert jobs[0]["tasks"].get("cancelled") == 1
    await c.post(f"/api/v1/groups/{name}/jobs", job_spec(), expect=409)


async def test_job_validation_and_rate_limit(user):
    c = await user()
    name = gname()
    await c.post("/api/v1/groups", {"name": name, "display_name": "x",
                                    "registry_allowlist": ["registry.example.org/"]}, expect=201)
    bad = job_spec(image="python 3.12")
    r = await c.post(f"/api/v1/groups/{name}/jobs", bad, expect=422)
    assert "image name" in r.text
    other = job_spec(image="evil.example.com/x@sha256:" + "a" * 64)
    await c.post(f"/api/v1/groups/{name}/jobs", other, expect=422)
    # A tag outside the allowlist is refused before the server contacts any registry.
    r = await c.post(f"/api/v1/groups/{name}/jobs", job_spec(image="python:3.12"), expect=422)
    assert "allowlist" in r.text
    arr = job_spec(shape="array", matrix={"seed": list(range(10)), "ds": ["a", "b", "c"]},
                   command=["run", "{{seed}}", "{{ds}}"])
    j = (await c.post(f"/api/v1/groups/{name}/jobs", arr, expect=201)).json()
    assert j["tasks"]["total"] == 30
    tasks = await c.get(f"/api/v1/jobs/{j['id']}/tasks")
    assert tasks[5]["params"] == {"seed": 1, "ds": "c"}
    codes = []
    for _ in range(30):
        codes.append((await c.post(f"/api/v1/groups/{name}/jobs", job_spec())).status_code)
    assert 429 in codes
