"""Step 0 of docs/design/easy-onboarding.md: one-step sharing and plain-language waiting reasons."""

from __future__ import annotations

import pytest
from conftest import gname, job_spec
from fake_agent import share

from pool_protocol import messages as pm

pytestmark = pytest.mark.asyncio


async def new_group(owner) -> str:
    name = gname()
    await owner.post("/api/v1/groups", {"name": name, "display_name": "Test"}, expect=201)
    return name


async def submit(client, group: str, **spec) -> str:
    return (await client.post(f"/api/v1/groups/{group}/jobs", job_spec(**spec), expect=201)).json()["id"]


async def waiting(client, job_id: str) -> list[dict]:
    return (await client.get(f"/api/v1/jobs/{job_id}/waiting"))["reasons"]


# --------------------------------------------------------------------------
# One-step sharing
# --------------------------------------------------------------------------


async def test_local_policy_shares_the_device_without_a_web_step(user, enrolled):
    owner = await user()
    group = await new_group(owner)
    agent = await enrolled(owner)  # no PUT /shares: only the local policy names the group
    await agent.connect([share(group)])
    device = await owner.get(f"/api/v1/devices/{agent.device_id}")
    assert device["shared_with"] == [group] and device["turned_off"] == []
    job_id = await submit(owner, group)
    offer = await agent.ready()
    assert isinstance(offer, pm.Offer), offer
    assert agent.open_envelope(offer).job_id == job_id
    audit = await owner.get(f"/api/v1/groups/{group}/audit")
    assert any(e["action"] == "device.share" and e["actor_type"] == "device"
               and (e.get("details") or {}).get("source") == "local_policy" for e in audit), audit


async def test_policy_cannot_share_into_a_group_the_owner_is_not_in(user, enrolled):
    stranger, coordinator = await user(), await user()
    group = await new_group(coordinator)
    agent = await enrolled(stranger)
    await agent.connect([share(group)])
    assert (await stranger.get(f"/api/v1/devices/{agent.device_id}"))["shared_with"] == []
    await submit(coordinator, group)
    assert isinstance(await agent.ready(), pm.NoWork)


async def test_switched_off_on_the_server_stays_off(user, enrolled):
    owner = await user()
    group = await new_group(owner)
    agent = await enrolled(owner)
    await agent.connect([share(group)])
    await owner.req("DELETE", f"/api/v1/devices/{agent.device_id}/shares/{group}", expect=200)
    await agent.heartbeat([], shares=[share(group)])  # the policy still offers the group
    device = await owner.get(f"/api/v1/devices/{agent.device_id}")
    assert device["shared_with"] == [] and device["turned_off"] == [group]
    await submit(owner, group)
    assert isinstance(await agent.ready(), pm.NoWork)
    await owner.req("DELETE", f"/api/v1/devices/{agent.device_id}/shares/{group}", expect=404)
    await owner.req("PUT", f"/api/v1/devices/{agent.device_id}/shares/{group}", expect=200)
    assert isinstance(await agent.ready(), pm.Offer)


# --------------------------------------------------------------------------
# Waiting reasons
# --------------------------------------------------------------------------


async def test_no_computer_shared(user):
    owner = await user()
    group = await new_group(owner)
    job_id = await submit(owner, group)
    (reason,) = await waiting(owner, job_id)
    assert reason["code"] == "no_devices"
    assert reason["message"].startswith("No computer is shared with this group")
    assert f"pool-agent share {group}" in reason["hint"]


async def test_shared_but_never_connected_is_offline(group_with_device):
    owner, group, _agent = await group_with_device()
    job_id = await submit(owner, group)
    (reason,) = await waiting(owner, job_id)
    assert reason["code"] == "offline" and reason["message"].startswith("1 computer is shared")


async def test_online_but_policy_excludes_the_group(group_with_device):
    owner, group, agent = await group_with_device()
    await agent.connect([share("some-other-group")])
    job_id = await submit(owner, group)
    assert [r["code"] for r in await waiting(owner, job_id)] == ["not_in_policy"]


async def test_outside_sharing_hours(group_with_device):
    owner, group, agent = await group_with_device()
    await agent.connect([share(group, active=False)])
    job_id = await submit(owner, group)
    assert [r["code"] for r in await waiting(owner, job_id)] == ["not_available_now"]


async def test_job_bigger_than_any_offer(group_with_device):
    owner, group, agent = await group_with_device()
    await agent.connect([share(group)])  # 8 cores at 100%, 32 GiB device memory
    job_id = await submit(owner, group, resources={"cpu": 16, "memory": "512Mi"})
    (reason,) = await waiting(owner, job_id)
    assert reason["code"] == "too_big"
    assert "16 CPU (most offered: 8 CPU)" in reason["message"], reason
    assert "memory" not in reason["message"]


async def test_fits_somewhere_then_nothing_left_to_explain(group_with_device):
    owner, group, agent = await group_with_device()
    await agent.connect([share(group)])
    job_id = await submit(owner, group)
    assert [r["code"] for r in await waiting(owner, job_id)] == ["waiting_for_room"]
    offer = await agent.ready()
    ack = await agent.run_task(offer)
    assert ack.accepted
    result = await owner.get(f"/api/v1/jobs/{job_id}/waiting")
    assert result == {"pending_tasks": 0, "reasons": []}


async def test_failed_try_is_explained_while_waiting_to_retry(group_with_device):
    owner, group, agent = await group_with_device()
    await agent.connect([share(group)])
    job_id = await submit(owner, group, retries=3)
    ack = await agent.run_task(await agent.ready(), exit_code=3, logs="boom\n")
    assert ack.accepted
    reasons = await waiting(owner, job_id)
    # Test settings retry after ~0.05 s, so the backoff may already be over.
    assert [r["code"] for r in reasons] in (["retry_backoff", "last_failure"], ["waiting_for_room", "last_failure"])
    assert reasons[-1]["message"] == "The last try on dev ended: exit 3."


async def test_waiting_reasons_are_for_group_members_only(user, group_with_device):
    owner, group, _agent = await group_with_device()
    job_id = await submit(owner, group)
    outsider = await user()
    await outsider.req("GET", f"/api/v1/jobs/{job_id}/waiting", expect=404)


# --------------------------------------------------------------------------
# Downloading outputs (the store itself is tested in tests/e2e/test_object_store.py)
# --------------------------------------------------------------------------


async def test_download_needs_a_file_store_and_a_known_output(user, group_with_device):
    owner, group, agent = await group_with_device()
    await agent.connect([share(group)])
    job_id = await submit(owner, group)
    assert (await agent.run_task(await agent.ready())).accepted  # fake agent reports result.txt
    base = f"/api/v1/jobs/{job_id}/tasks/0/download"
    r = await owner.req("GET", base, params={"path": "result.txt"}, expect=409)
    assert "no file store" in r.json()["detail"]
    await owner.req("GET", base, params={"path": "other.txt"}, expect=404)
    await owner.req("GET", f"/api/v1/jobs/{job_id}/tasks/7/download", params={"path": "result.txt"}, expect=404)
    outsider = await user()
    await outsider.req("GET", base, params={"path": "result.txt"}, expect=404)


# --------------------------------------------------------------------------
# Server install (step 1): public UI config and who may create groups
# --------------------------------------------------------------------------


async def test_public_config_tells_the_ui_how_to_sign_in(http):
    r = await http.get("/api/v1/config")  # no token needed
    assert r.status_code == 200 and r.json() == {"auth": "dev"}


async def test_public_config_in_oidc_mode(settings):
    import httpx

    from pool_cp.app import create_app
    from pool_cp.config import Settings

    oidc = Settings(**{**settings.model_dump(), "dev_mode": False, "oidc_issuer": "https://pool.example.org/auth/realms/pool",
                       "oidc_audience": "pool-api", "autocreate_keys": True})
    app = create_app(oidc)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/v1/config")
    assert r.json() == {"auth": "oidc", "oidc_authority": "https://pool.example.org/auth/realms/pool",
                        "oidc_client_id": "pool-web"}


async def test_group_creation_can_be_limited_to_server_admins(user, settings, monkeypatch):
    monkeypatch.setattr(settings, "group_creators", "admins")
    member = await user()
    assert (await member.get("/api/v1/me"))["can_create_groups"] is False
    r = await member.post("/api/v1/groups", {"name": gname(), "display_name": "x"}, expect=403)
    assert "only server admins create groups" in r.json()["detail"]
    admin = await user("root-admin")  # listed in the test settings' server_admins
    assert (await admin.get("/api/v1/me"))["can_create_groups"] is True
    await admin.post("/api/v1/groups", {"name": gname(), "display_name": "x"}, expect=201)
