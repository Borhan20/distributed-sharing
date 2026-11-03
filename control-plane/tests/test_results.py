"""A job's results in one place: zip of everything, results table and CSV, log files (3 November 2026).

The object store is replaced by a dict here (the real one is tested in tests/e2e/test_object_store.py)."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import zipfile

import pytest
from fake_agent import share

from pool_cp import storage

pytestmark = pytest.mark.asyncio

IMG = "registry.example.org/lab/pi:1@sha256:" + "b" * 64


@pytest.fixture
def store(monkeypatch):
    """sha256 -> bytes, behind storage.copy_object (same checks: missing -> LookupError, bad hash -> ValueError)."""
    objects: dict[str, bytes] = {}

    def copy_object(settings, group_id, sha256, out, max_bytes):  # noqa: ARG001
        if sha256 not in objects:
            raise LookupError(sha256)
        data = objects[sha256]
        if len(data) > max_bytes:
            raise ValueError("too large")
        out.write(data)
        if hashlib.sha256(data).hexdigest() != sha256:
            raise ValueError("does not match")
        return len(data)

    monkeypatch.setattr(storage, "enabled", lambda settings: True)
    monkeypatch.setattr(storage, "copy_object", copy_object)
    return objects


def put(store: dict, path: str, data: bytes) -> dict:
    sha = hashlib.sha256(data).hexdigest()
    store[sha] = data
    return {"path": path, "sha256": sha, "size": len(data)}


async def run_sweep(owner, group, agent, store) -> str:
    job = {"name": "pi", "image": IMG, "command": "pi --seed {{seed}}", "sweep": {"seed": "0..2"}, "retries": 0}
    job_id = (await owner.post(f"/api/v1/groups/{group}/jobs", job, expect=201)).json()["id"]
    await agent.connect([share(group)])
    for _ in range(3):
        offer = await agent.ready()
        env = agent.open_envelope(offer)
        seed = int(env.spec.command[-1])
        if seed == 2:  # this one fails: no results, but its log is kept
            await agent.run_task(offer, exit_code=1, logs="seed 2 failed\n", outputs=[])
            continue
        outputs = [put(store, "result.json", json.dumps({"seed": seed, "pi": 3.1 + seed / 100,
                                                         "detail": {"n": seed * 10}}).encode()),
                   put(store, "data.csv", f"x,y\n{seed},{seed * 2}\n".encode()),
                   put(store, "plots/curve.txt", f"curve {seed}\n".encode())]
        if seed == 1:
            bad = put(store, "broken.bin", b"original")
            store[bad["sha256"]] = b"tampered"  # the store holds something else than the computer reported
            outputs.append(bad)
        await agent.run_task(offer, logs=f"seed {seed} ok\n", outputs=outputs)
    return job_id


async def test_results_table_csv_zip_and_logs(group_with_device, store, user):
    owner, group, agent = await group_with_device()
    job_id = await run_sweep(owner, group, agent, store)

    table = await owner.get(f"/api/v1/jobs/{job_id}/results")
    assert table["columns"] == ["task", "seed", "pi", "detail.n"]
    assert table["rows"] == [{"task": 0, "seed": 0, "pi": 3.1, "detail.n": 0},
                             {"task": 1, "seed": 1, "pi": 3.11, "detail.n": 10}]
    assert table["missing"] == 1, "the failed task wrote no result.json"

    r = await owner.req("GET", f"/api/v1/jobs/{job_id}/results.csv", expect=200)
    assert r.headers["content-type"].startswith("text/csv") and "pi-results.csv" in r.headers["content-disposition"]
    assert list(csv.reader(io.StringIO(r.text))) == [["task", "seed", "pi", "detail.n"], ["0", "0", "3.1", "0"],
                                                     ["1", "1", "3.11", "10"]]

    r = await owner.req("GET", f"/api/v1/jobs/{job_id}/results.zip", expect=200)
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = set(z.namelist())
    assert {"README.txt", "tasks.csv", "results.csv", "combined/data.csv", "task-000_seed=0/result.json",
            "task-000_seed=0/plots/curve.txt", "task-000_seed=0/log.txt", "task-002_seed=2/log.txt"} <= names
    assert "task-001_seed=1/broken.bin" not in names, "a file that fails its checksum is left out"
    assert "broken.bin" in z.read("README.txt").decode()
    assert z.read("task-002_seed=2/log.txt").decode() == "seed 2 failed\n"
    assert list(csv.reader(io.StringIO(z.read("combined/data.csv").decode()))) == [
        ["task", "seed", "x", "y"], ["0", "0", "0", "0"], ["1", "1", "1", "2"]]
    summary = list(csv.DictReader(io.StringIO(z.read("tasks.csv").decode())))
    assert [(t["task"], t["seed"], t["state"], t["files"]) for t in summary] == [
        ("0", "0", "succeeded", "3"), ("1", "1", "succeeded", "4"), ("2", "2", "failed", "0")]
    assert summary[0]["computer"] and summary[2]["exit_code"] == "1"

    log = await owner.req("GET", f"/api/v1/jobs/{job_id}/tasks/1/log.txt", expect=200)
    assert log.text == "seed 1 ok\n" and "attachment" in log.headers["content-disposition"]
    await owner.req("GET", f"/api/v1/jobs/{job_id}/tasks/9/log.txt", expect=404)

    outsider = await user()
    for path in ("results", "results.csv", "results.zip", "tasks/0/log.txt"):
        await outsider.req("GET", f"/api/v1/jobs/{job_id}/{path}", expect=404)


async def test_results_need_a_file_store_and_respect_the_size_cap(group_with_device, store, settings, monkeypatch):
    owner, group, agent = await group_with_device()
    job_id = await run_sweep(owner, group, agent, store)
    monkeypatch.setattr(settings, "results_zip_max_mb", 0)
    r = await owner.req("GET", f"/api/v1/jobs/{job_id}/results.zip", expect=413)
    assert "pool download" in r.json()["detail"]
    monkeypatch.setattr(storage, "enabled", lambda settings: False)
    r = await owner.req("GET", f"/api/v1/jobs/{job_id}/results.zip", expect=409)
    assert "no file store" in r.json()["detail"]
    await owner.req("GET", f"/api/v1/jobs/{job_id}/results", expect=409)
    await owner.req("GET", f"/api/v1/jobs/{job_id}/tasks/0/log.txt", expect=200), "logs need no file store"


async def test_different_csv_columns_are_not_merged(group_with_device, store):
    owner, group, agent = await group_with_device()
    job = {"image": IMG, "parts": 2, "command": "x"}
    job_id = (await owner.post(f"/api/v1/groups/{group}/jobs", job, expect=201)).json()["id"]
    await agent.connect([share(group)])
    for header in (b"a,b\n1,2\n", b"a,c\n1,2\n"):
        await agent.run_task(await agent.ready(), outputs=[put(store, "out.csv", header)])
    z = zipfile.ZipFile(io.BytesIO((await owner.req("GET", f"/api/v1/jobs/{job_id}/results.zip", expect=200)).content))
    assert "combined/out.csv" not in z.namelist()
    assert "different columns" in z.read("README.txt").decode()
    assert {"task-000/out.csv", "task-001/out.csv"} <= set(z.namelist())


async def test_cli_downloads_every_task_and_prints_the_table(group_with_device, store, cli, tmp_path, monkeypatch):
    import httpx

    owner, group, agent = await group_with_device()
    job_id = await run_sweep(owner, group, agent, store)
    by_sha = {sha: data for sha, data in store.items()}

    class FakeStream:  # the presigned URL points at the store; serve the dict's bytes instead
        def __init__(self, url):
            self.status_code, self.data = 200, by_sha[url.rsplit("/", 1)[-1].split("?")[0]]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def iter_bytes(self):
            yield self.data

    monkeypatch.setattr(storage, "presign_get", lambda settings, gid, sha, filename=None: f"http://store/{sha}")
    monkeypatch.setattr(storage, "object_exists", lambda settings, gid, sha: True)
    monkeypatch.setattr(httpx, "stream", lambda method, url, timeout: FakeStream(url))
    cli("login", "--server", cli.server, "--dev-user", owner.username)
    res = cli("download", job_id, "--all", "-o", str(tmp_path / "res"), ok=False)
    assert "checksum does not match" in str(res.exception), "the tampered file stops the download loudly"
    assert (tmp_path / "res/task-000_seed=0/result.json").exists()
    assert json.loads((tmp_path / "res/task-000_seed=0/result.json").read_text())["seed"] == 0
    assert (tmp_path / "res/task-000_seed=0/plots/curve.txt").read_text() == "curve 0\n"
    out = cli("results", job_id).output
    assert out.splitlines() == ["task,seed,pi,detail.n", "0,0,3.1,0", "1,1,3.11,10"]


async def test_large_files_stream_into_the_zip(group_with_device, store, monkeypatch):
    import os

    from pool_cp.api import results

    monkeypatch.setattr(results, "MAX_CHECKED_BYTES", 1024)  # treat files over 1 KiB as large
    owner, group, agent = await group_with_device()
    job_id = (await owner.post(f"/api/v1/groups/{group}/jobs", {"image": IMG}, expect=201)).json()["id"]
    await agent.connect([share(group)])
    big = os.urandom(3 * 1024 * 1024 + 17)  # several 1 MiB response chunks, not compressible
    damaged = put(store, "damaged.bin", b"x" * 4096)
    store[damaged["sha256"]] = b"y" * 4096
    await agent.run_task(await agent.ready(), outputs=[put(store, "big.bin", big), damaged])
    r = await owner.req("GET", f"/api/v1/jobs/{job_id}/results.zip", expect=200)
    z = zipfile.ZipFile(io.BytesIO(r.content))
    assert z.read("task-000/big.bin") == big
    readme = z.read("README.txt").decode()
    assert "damaged.bin: does not match" in readme and "do not trust it" in readme
