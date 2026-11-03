"""Outputs through a real object store: SeaweedFS in Docker, the real agent, the real API.

1. The API creates the bucket itself, using the permissions from deploy/seaweedfs-s3.json.example.
2. Presigned URLs carry the public address while the API talks to the store at its internal
   address; the agent uploads a job's output and the stored bytes match the reported hash.
3. The store rejects a body that does not match the signed SHA-256, so a device cannot
   overwrite another task's content-addressed output.
4. When the store is configured but unreachable, jobs still succeed and the agent is told
   why its outputs stayed on the device.

    cd agent && go build -o bin/pool-agent ./cmd/pool-agent
    uv run pytest -m e2e -s tests/e2e/test_object_store.py
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import subprocess
import time
import uuid

import httpx
import pytest
from typer.testing import CliRunner

from pool_cli.main import app as cli_app
from pool_cp import storage
from pool_cp.config import Settings

from .conftest import ROOT, free_port
from .test_agent_e2e import Agent, _requirements, containers, job, logs, submit, wait_job  # noqa: F401

pytestmark = [pytest.mark.e2e, pytest.mark.docker, pytest.mark.asyncio]

# Pinned to the version this test was run against; keep in step with deploy/compose.yaml.
SEAWEEDFS_IMAGE = "chrislusf/seaweedfs:4.50"
BUCKET = "pool-artifacts"


class Store:
    def __init__(self, port: int, access_key: str, secret_key: str) -> None:
        self.port, self.access_key, self.secret_key = port, access_key, secret_key
        self.internal = f"http://127.0.0.1:{port}"
        self.public = f"http://localhost:{port}"  # a different host name than the API uses

    def env(self) -> dict[str, str]:
        return {"POOL_S3_ENDPOINT": self.internal, "POOL_S3_PUBLIC_ENDPOINT": self.public,
                "POOL_S3_ACCESS_KEY": self.access_key, "POOL_S3_SECRET_KEY": self.secret_key}

    def settings(self) -> Settings:
        return Settings(dev_mode=True, s3_endpoint=self.internal, s3_public_endpoint=self.public,
                        s3_access_key=self.access_key, s3_secret_key=self.secret_key)

    def client(self):
        return storage._internal_client(self.settings())


@pytest.fixture(scope="module")
def seaweedfs(tmp_path_factory):
    """A throwaway SeaweedFS whose identity comes from the shipped example file (fresh random keys)."""
    access_key, secret_key = secrets.token_hex(10), secrets.token_hex(20)  # generated per run, never stored
    config = json.loads((ROOT / "deploy" / "seaweedfs-s3.json.example").read_text())
    config["identities"][0]["credentials"] = [{"accessKey": access_key, "secretKey": secret_key}]
    path = tmp_path_factory.mktemp("seaweedfs") / "s3.json"
    path.write_text(json.dumps(config))
    path.chmod(0o644)
    port = free_port()
    name = f"pool-e2e-seaweedfs-{uuid.uuid4().hex[:8]}"
    subprocess.run(["docker", "run", "-d", "--name", name, "-p", f"127.0.0.1:{port}:8333",
                    "-v", f"{path}:/etc/seaweedfs/s3.json:ro", SEAWEEDFS_IMAGE,
                    "server", "-dir=/data", "-s3", "-s3.config=/etc/seaweedfs/s3.json"],
                   check=True, capture_output=True)
    store = Store(port, access_key, secret_key)
    try:
        deadline = time.time() + 90
        while True:  # the S3 port answers (403 for anonymous) once the filer is up
            try:
                if httpx.get(store.internal, timeout=2).status_code in (200, 403):
                    break
            except httpx.HTTPError:
                pass
            if time.time() > deadline:
                raise RuntimeError("SeaweedFS did not start: " + subprocess.run(
                    ["docker", "logs", "--tail", "30", name], capture_output=True, text=True).stderr)
            time.sleep(0.5)
        yield store
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


async def wait_object_store(cp_url: str, want: str, timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    state = None
    while time.monotonic() < deadline:
        state = httpx.get(cp_url + "/readyz", timeout=5).json().get("object_store")
        if state == want:
            return
        await asyncio.sleep(0.5)
    raise AssertionError(f"object_store stayed {state!r}, wanted {want!r}")


async def make_platform(control_plane_factory, tmp_path, **env: str):
    cp = control_plane_factory(POOL_HEARTBEAT_INTERVAL_S="2", POOL_SCHEDULER_INTERVAL_S="0.5",
                               POOL_API_HEARTBEAT_S="1", **env)
    cp.start_api()
    cp.start_scheduler()
    token = await cp.token("e2e-researcher")
    http = httpx.AsyncClient(base_url=cp.url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    r = await http.post("/api/v1/groups", json={"name": "e2e", "display_name": "E2E"})
    r.raise_for_status()
    group_id = r.json()["id"]
    agent = Agent(tmp_path / "agent0", "e2e-store-dev", cp.url, tmp_path)
    code = (await http.post("/api/v1/devices/enrollment-codes")).json()["code"]
    await asyncio.to_thread(agent.enroll, code)
    agent.write_policy("e2e")
    agent.device_id = {d["name"]: d["id"] for d in (await http.get("/api/v1/devices")).json()}[agent.name]
    (await http.put(f"/api/v1/devices/{agent.device_id}/shares/e2e")).raise_for_status()
    return cp, http, agent, group_id


@pytest.fixture
async def cleanup():
    items: list = []
    yield items
    for agent, http in items:
        agent.stop()
        await http.aclose()
    for cid in containers():
        subprocess.run(["docker", "rm", "-f", cid], capture_output=True)


async def test_api_creates_bucket_and_agent_uploads_outputs(seaweedfs, control_plane_factory, tmp_path, cleanup):
    client = seaweedfs.client()
    if BUCKET in [b["Name"] for b in client.list_buckets().get("Buckets", [])]:
        client.delete_bucket(Bucket=BUCKET)  # module-scoped store: start from a missing bucket
    cp, http, agent, group_id = await make_platform(control_plane_factory, tmp_path, **seaweedfs.env())
    cleanup.append((agent, http))
    await wait_object_store(cp.url, "ready")
    assert BUCKET in [b["Name"] for b in client.list_buckets()["Buckets"]], "the API should create the bucket"

    agent.start()
    content = b"pool object store e2e\n"
    job_id = await submit(http, job("upload", ["sh", "-c", "printf 'pool object store e2e\\n' > /out/result.txt"]))
    j = await wait_job(http, job_id, 120)
    assert j["state"] == "succeeded", j
    task = (await http.get(f"/api/v1/jobs/{job_id}/tasks")).json()[0]
    (out,) = task["result"]["outputs"]
    assert out["path"] == "result.txt" and out["sha256"] == hashlib.sha256(content).hexdigest()
    text = await logs(http, job_id, 0)
    assert "uploaded 1 of 1 outputs" in text, text

    stored = client.get_object(Bucket=BUCKET, Key=storage.object_key(uuid.UUID(group_id), out["sha256"]))
    assert stored["Body"].read() == content

    # Members download it through a short-lived link on the public address, saved under its own name.
    link = (await http.get(f"/api/v1/jobs/{job_id}/tasks/0/download", params={"path": "result.txt"})).json()
    assert link["url"].startswith(seaweedfs.public + "/") and link["size"] == len(content)
    async with httpx.AsyncClient(timeout=10) as c:
        got = await c.get(link["url"])
    assert got.status_code == 200 and got.content == content
    assert 'filename="result.txt"' in got.headers.get("content-disposition", ""), got.headers

    # And with the CLI, which also checks the SHA-256.
    runner = CliRunner(env={"POOL_CONFIG_DIR": str(tmp_path / "cli"), "POOL_TOKEN": None, "POOL_SERVER": None})
    login = runner.invoke(cli_app, ["login", "--server", cp.url, "--dev-user", "e2e-researcher"])
    assert login.exit_code == 0, login.output
    res = runner.invoke(cli_app, ["download", job_id, "-o", str(tmp_path / "dl")])
    assert res.exit_code == 0 and "sha256 ok" in res.output, (res.output, res.exception)
    assert (tmp_path / "dl" / "result.txt").read_bytes() == content


async def test_store_rejects_body_that_does_not_match_the_signed_hash(seaweedfs):
    settings = seaweedfs.settings()
    assert await asyncio.to_thread(storage.ensure_bucket, settings) in ("created", "exists")
    assert await asyncio.to_thread(storage.ensure_bucket, settings) == "exists"
    real, fake = b"real output\n", b"fake output\n"  # same length, so only the checksum can catch it
    sha = hashlib.sha256(real).hexdigest()
    gid = uuid.uuid4()
    url, _ = storage.presign_put(settings, gid, sha, len(real))
    honest = {"x-amz-checksum-sha256": storage.checksum_header(sha)}
    async with httpx.AsyncClient(timeout=10) as c:
        assert (await c.put(url, content=fake, headers=honest)).status_code == 400  # BadDigest
        assert (await c.put(url, content=fake)).status_code == 403  # checksum header is signed
        assert (await c.put(url.replace("localhost", "127.0.0.1"), content=real, headers=honest)).status_code == 403
        assert (await c.put(url, content=real, headers=honest)).status_code == 200
    body = seaweedfs.client().get_object(Bucket=BUCKET, Key=storage.object_key(gid, sha))["Body"].read()
    assert body == real


async def test_unreachable_store_does_not_block_jobs(control_plane_factory, tmp_path, cleanup):
    dead = f"http://127.0.0.1:{free_port()}"  # nothing listens here
    cp, http, agent, _ = await make_platform(
        control_plane_factory, tmp_path, POOL_S3_ENDPOINT=dead, POOL_S3_ACCESS_KEY="placeholder",
        POOL_S3_SECRET_KEY="placeholder")
    cleanup.append((agent, http))
    assert httpx.get(cp.url + "/readyz", timeout=5).json() == {"ok": True, "object_store": "not ready"}
    agent.start()
    job_id = await submit(http, job("no-store", ["sh", "-c", "echo hi > /out/result.txt"]))
    j = await wait_job(http, job_id, 120)
    assert j["state"] == "succeeded", j
    text = await logs(http, job_id, 0)
    assert "server: object_store_not_ready" in text, text
    r = await http.get(f"/api/v1/jobs/{job_id}/tasks/0/download", params={"path": "result.txt"})
    assert r.status_code == 503 and "not reachable" in r.json()["detail"], r.text
