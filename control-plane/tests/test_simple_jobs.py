"""Short job files, parts and parts: auto (decisions of 3 November 2026)."""

from __future__ import annotations

import pytest
from conftest import gname
from fake_agent import share

pytestmark = pytest.mark.asyncio

IMG = "registry.example.org/lab/app:1@sha256:" + "b" * 64


async def test_short_form_with_parts_numbers_every_task(user):
    c = await user()
    g = gname()
    await c.post("/api/v1/groups", {"name": g, "display_name": "x"}, expect=201)
    job = (await c.post(f"/api/v1/groups/{g}/jobs", {"image": IMG, "parts": 3, "command": "sim --part {{index}}"},
                        expect=201)).json()
    assert (job["name"], job["shape"], job["tasks"]["total"]) == ("app", "array", 3)
    tasks = await c.get(f"/api/v1/jobs/{job['id']}/tasks")
    assert [t["index"] for t in tasks] == [0, 1, 2]
    detail = await c.get(f"/api/v1/jobs/{job['id']}")
    assert detail["spec"]["spec"]["parts"] == 3
    assert detail["spec"]["as_submitted"] == {"image": IMG, "parts": 3, "sizes": {}}, "sizes: the ones the person gave"
    r = await c.post(f"/api/v1/groups/{g}/jobs", {"image": IMG, "colour": "red"}, expect=422)
    assert "unknown field(s) colour" in r.json()["detail"]
    r = await c.post(f"/api/v1/groups/{g}/jobs", {"image": IMG, "group": "elsewhere"}, expect=422)
    assert "elsewhere" in r.json()["detail"]


async def test_auto_parts_fill_the_groups_computers(group_with_device, enrolled):
    owner, g, first = await group_with_device()
    second = await enrolled(owner, "second")
    await owner.req("PUT", f"/api/v1/devices/{second.device_id}/shares/{g}", expect=200)
    # Each fake computer offers 8 CPU and 32 GiB: 4 parts of 2 CPU each, so 8 on two computers.
    job = {"image": IMG, "parts": "auto", "cpu": 2, "memory": "1Gi", "disk": "1Gi"}
    r = (await owner.post(f"/api/v1/groups/{g}/jobs", job, expect=201)).json()
    assert r["tasks"]["total"] == 1, "nothing connected yet: the computers never reported their limits"
    await first.connect([share(g)])
    await second.connect([share(g)])
    r = (await owner.post(f"/api/v1/groups/{g}/jobs", job, expect=201)).json()
    assert r["tasks"]["total"] == 8
    note = (await owner.get(f"/api/v1/jobs/{r['id']}"))["spec"]["as_submitted"]["note"]
    assert "8" in note and "2 computers" in note and "available now" in note
    big = (await owner.post(f"/api/v1/groups/{g}/jobs", {**job, "cpu": 64}, expect=201)).json()
    assert big["tasks"]["total"] == 1, "a job no computer fits still runs as one part (and says why it waits)"
