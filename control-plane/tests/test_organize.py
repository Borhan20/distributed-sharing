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


def test_reports_that_try_to_hide_or_blow_up_are_dropped():
    """Found in the security review of step E."""
    # Nested suites: each test once, in its nearest suite (it was once per enclosing suite, and quadratic).
    nested = b'<testsuites><testsuite name="outer"><testsuite name="inner"><testcase name="a"/></testsuite>' \
             b'<testcase name="b"/></testsuite></testsuites>'
    assert [(c.suite, c.name) for c in junit.parse(nested)] == [("inner", "a"), ("outer", "b")]
    deep = b"<testsuites>" + b"<testsuite>" * 8000 + b"</testsuite>" * 8000 + b"</testsuites>"
    assert junit.parse(deep) == []
    # Encodings: only UTF-8 is read, so nothing can hide a DOCTYPE, and odd names cannot break the parser.
    doctype = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "PWNED">]><testsuite><testcase name="&a;"/></testsuite>'
    assert junit.parse(doctype.encode("utf-16")) is None
    assert junit.parse(doctype.encode("utf-16-le")) is None
    assert junit.parse(b"<!--" + b"x" * 5000 + b"-->" + doctype.split("?>", 1)[1].encode()) is None
    for name in ("UCS-2", "hex", "base64", "x-sjis"):
        odd = f'<?xml version="1.0" encoding="{name}"?><testsuite><testcase name="t"/></testsuite>'.encode()
        assert [c.name for c in junit.parse(odd)] == ["t"], name
    assert junit.parse(b"\xef\xbb\xbf<testsuite><testcase name='bom'/></testsuite>")[0].name == "bom"
    assert junit.parse(b"<testsuite><testcase name='\xff'/></testsuite>") is None, "not UTF-8"
    assert junit.parse(b"<html>" + b"<p/>" * 1000 + b"</html>") is None, "not a report: dropped at the root"
    # Text from a task: one line, no control or direction characters, cut before it is cleaned.
    tricky = '<testsuite><testcase name="a\u202eb\u009bc&#10;d"><failure message="' + "x " * 2_000_000 + \
             '"/></testcase></testsuite>'
    case = junit.parse(tricky.encode())[0]
    assert case.name == "a b c d" and len(case.message) == junit.MESSAGE_CHARS
    # Markdown: names and messages cannot add links, images, HTML or table cells.
    s = junit.merge([(0, "junit.xml", report(("c", "![t](http://evil.example/p.png)", 1.0, "a | b &lt;img&gt;")))])
    md = junit.markdown(s.summary())
    assert "![t](http" not in md and "<img>" not in md and "a \\| b" in md


def test_one_report_reads_little_and_is_kept(monkeypatch):
    """The work for one report is bounded and done once (security review of step E)."""
    from types import SimpleNamespace

    from pool_cp.api import results

    reads: list[str] = []
    files = {f"{i:064x}": report(("c", f"t{i}", 1.0, None)) for i in range(600)}

    def read_small(settings, group_id, o, limit):  # noqa: ARG001
        reads.append(o["sha256"])
        return files.get(o["sha256"])

    monkeypatch.setattr(results, "_read_small", read_small)
    job = SimpleNamespace(id=uuid.uuid4(), group_id=uuid.uuid4())

    def row(i: int, sha: str) -> SimpleNamespace:
        return SimpleNamespace(task=SimpleNamespace(index=i, state="failed"), attempt_id=uuid.uuid4(),
                               report_outputs=[{"path": "junit.xml", "sha256": sha, "size": 100}])

    # Many tasks that wrote the same file: it is read and parsed once.
    same = [row(i, f"{0:064x}") for i in range(2000)]
    r = results._tests(None, job, same)
    assert len(reads) == 1 and r.summary()["tests"] == 2000
    # The same tasks again: the kept report, no reads.
    assert results._tests(None, job, same) is r and len(reads) == 1
    # Many different files, also missing ones: every read counts toward the limit.
    reads.clear()
    many = [row(i, f"{i:064x}") for i in range(600)] + [row(600 + i, "f" * 64) for i in range(10)]
    r = results._tests(None, job, many)
    assert len(reads) == results.MAX_REPORT_FILES
    assert r.summary()["unreadable"][0].startswith("110 more file(s) were not read")


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
async def test_a_bad_report_or_error_text_does_not_break_the_results(group_with_device, store):
    """Security review of step E: one odd file broke GET /tests and the zip; an error text could add Markdown."""
    coord, g, agent = await group_with_device()
    job = (await coord.post(f"/api/v1/groups/{g}/jobs",
                            {"image": IMG, "parts": 2, "memory": "1Gi", "retries": 0}, expect=201)).json()
    await agent.connect([share(g)])
    odd = b'<?xml version="1.0" encoding="UCS-2"?><testsuite><testcase name="t"/></testsuite>'
    first = await agent.ready()
    assert (await agent.run_task(first, exit_code=1, outputs=[put(store, "junit.xml", odd)])).accepted
    second = await agent.ready()
    garbage = put(store, "data.xml", b"\xff\xfe<\x00?\x00")
    assert (await agent.run_task(second, exit_code=1, outputs=[garbage])).accepted
    r = await coord.get(f"/api/v1/jobs/{job['id']}/tests")
    assert r["tests"] == 1
    z = zipfile.ZipFile(io.BytesIO((await coord.req("GET", f"/api/v1/jobs/{job['id']}/results.zip", 200)).content))
    assert {"summary.md", "tests.md", "README.txt"} <= set(z.namelist())
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
    # A line now: not quiet any more.
    await agent.send(pm.Progress(attempt_id=quiet.attempt_id, epoch=quiet.epoch, seq=0, log_chunk="step 2\n"))
    await asyncio.sleep(0.1)
    w = await coord.get(f"/api/v1/jobs/{job['id']}/waiting")
    assert not any("Task 1" in n for n in w["notes"]), w["notes"]
    await agent.close()


@pytest.mark.asyncio
async def test_no_time_guess_from_a_limit_you_set_or_with_no_computer(group_with_device):
    coord, g, agent = await group_with_device()
    job = (await coord.post(f"/api/v1/groups/{g}/jobs",
                            {"image": IMG, "parts": 2, "memory": "1Gi", "time_limit": "24h"}, expect=201)).json()
    w = await coord.get(f"/api/v1/jobs/{job['id']}/waiting")
    assert w["eta"] is None, "24 h is your limit, not a learned one; and no computer can run it now"
