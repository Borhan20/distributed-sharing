"""Results put together (docs/design/pool-decides.md §4.8, step E): one test report, a run summary, and when
the job will probably end."""

from __future__ import annotations

import asyncio
import hashlib
import io
import uuid
import zipfile

import pytest
from fake_agent import share
from sqlalchemy import text, update

from pool_cp import junit, storage
from pool_cp.models import Attempt
from pool_protocol import messages as pm

IMG = "registry.example.org/lab/tests:1@sha256:" + "c" * 64


def report(*cases: tuple[str, str, float, str | None]) -> bytes:
    """A JUnit report: (classname, name, seconds, failure message or None)."""
    body = "".join(
        f'<testcase classname="{c}" name="{n}" time="{t}">'
        + (f'<failure message="{m}">trace</failure>' if m else "") + "</testcase>"
        for c, n, t, m in cases)
    return f'<?xml version="1.0"?><testsuites><testsuite name="s">{body}</testsuite></testsuites>'.encode()


def test_parse_and_merge():
    one = report(("t.a", "ok", 0.5, None), ("t.a", "bad", 2.0, "assert 1 == 2"))
    two = b'<testsuite name="x"><testcase name="e" time="1"><error message="boom"/></testcase>' \
          b'<testcase name="s"><skipped/></testcase></testsuite>'
    merged = junit.merge([(0, "junit.xml", one), (1, "reports/junit.xml", two), (1, "other.xml", b"<html/>")])
    s = merged.summary()
    assert (s["tests"], s["passed"], s["failed"], s["error"], s["skipped"], s["files"]) == (4, 1, 1, 1, 1, 2)
    assert s["failures"][0] == {"task": 0, "suite": "t.a", "name": "bad", "status": "failed", "seconds": 2.0,
                                "message": "assert 1 == 2"}
    assert s["slowest"][0]["name"] == "bad"
    assert "## Failed" in junit.markdown(s) and "assert 1 == 2" in junit.markdown(s)


def test_untrusted_reports_are_refused_safely():
    entities = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><testsuite><testcase name="&a;"/></testsuite>'
    assert junit.parse(entities, 0) is None, "no DTDs or entities at all"
    assert junit.parse(b"<testsuite><testcase", 0) is None
    assert junit.parse(b"<testsuite><testcase name='x' time='nan'/></testsuite>", 0)[0].seconds == 0.0
    merged = junit.merge([(3, "junit.xml", b"not xml")])
    assert merged.summary()["unreadable"] == ["task 3: junit.xml"]


@pytest.fixture
def store(monkeypatch):
    objects: dict[str, bytes] = {}

    def copy_object(settings, group_id, sha256, out, max_bytes):  # noqa: ARG001
        if sha256 not in objects:
            raise LookupError(sha256)
        out.write(objects[sha256])
        return len(objects[sha256])

    monkeypatch.setattr(storage, "enabled", lambda settings: True)
    monkeypatch.setattr(storage, "copy_object", copy_object)
    return objects


def put(store: dict, path: str, data: bytes) -> dict:
    sha = hashlib.sha256(data).hexdigest()
    store[sha] = data
    return {"path": path, "sha256": sha, "size": len(data)}


@pytest.mark.asyncio
async def test_one_report_from_every_part_also_the_failing_ones(group_with_device, store):
    coord, g, agent = await group_with_device()
    job = (await coord.post(f"/api/v1/groups/{g}/jobs",
                            {"image": IMG, "parts": 2, "memory": "1Gi", "retries": 0}, expect=201)).json()
    await agent.connect([share(g)])
    good = await agent.ready()
    assert (await agent.run_task(good, outputs=[put(store, "junit.xml", report(("a", "x", 1.0, None)))])).accepted
    bad = await agent.ready()
    out = [put(store, "junit.xml", report(("b", "y", 3.0, "expected 2")))]
    assert (await agent.run_task(bad, exit_code=1, outputs=out)).accepted
    r = await coord.get(f"/api/v1/jobs/{job['id']}/tests")
    assert (r["tests"], r["passed"], r["failed"]) == (2, 1, 1), "the failed part's report counts too"
    assert r["failures"][0]["task"] == 1 and r["failures"][0]["message"] == "expected 2"
    z = zipfile.ZipFile(io.BytesIO((await coord.req("GET", f"/api/v1/jobs/{job['id']}/results.zip", 200)).content))
    assert "expected 2" in z.read("tests.md").decode()
    summary = z.read("summary.md").decode()
    assert "Tests: 1 passed, 1 failed" in summary and "## Failed tasks" in summary
    assert "How the sizes were chosen" in summary, "the submitter sees why"
    await agent.close()


@pytest.mark.asyncio
async def test_when_it_will_probably_end_and_quiet_tasks(group_with_device, db):
    coord, g, agent = await group_with_device()
    job = (await coord.post(f"/api/v1/groups/{g}/jobs",
                            {"image": IMG, "parts": 4, "memory": "1Gi", "cpu": 8}, expect=201)).json()
    await agent.connect([share(g)])
    # Part 0 took 10 minutes.
    offer = await agent.ready()
    await agent.send(pm.Accept(attempt_id=offer.attempt_id, epoch=offer.epoch, tier="T2"))
    await agent.send(pm.Started(attempt_id=offer.attempt_id, epoch=offer.epoch))
    await asyncio.sleep(0.05)
    await db.execute(update(Attempt).where(Attempt.id == uuid.UUID(offer.attempt_id))
                     .values(started_at=text("now() - interval '10 minutes'")))
    await db.commit()
    assert (await agent.commit(offer.attempt_id, offer.task_id, offer.epoch)).accepted
    # Part 1 has run for 40 minutes without printing anything.
    quiet = await agent.ready()
    await agent.send(pm.Accept(attempt_id=quiet.attempt_id, epoch=quiet.epoch, tier="T2"))
    await agent.send(pm.Started(attempt_id=quiet.attempt_id, epoch=quiet.epoch))
    await asyncio.sleep(0.05)
    await db.execute(update(Attempt).where(Attempt.id == uuid.UUID(quiet.attempt_id))
                     .values(started_at=text("now() - interval '40 minutes'")))
    await db.commit()
    await agent.heartbeat([], shares=[share(g)])
    w = await coord.get(f"/api/v1/jobs/{job['id']}/waiting")
    assert w["eta_s"] is not None and "task(s) of about 10 min" in w["eta"], w
    assert any("Task 1 has printed nothing for 40 min" in n for n in w["notes"]), w["notes"]
    await agent.close()
