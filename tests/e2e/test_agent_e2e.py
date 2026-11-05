"""The real Go agent end to end (plan §9 Phase 1 exit criteria), with Docker containers.

1. Enroll two agents with one-time codes, share them, run a 6-task array job.
2. Sandbox checks from inside a submitted job: non-root, no capabilities,
   no-new-privileges, read-only root, no network, no host files.
3. Kill one agent mid-task (SIGKILL): the lease expires, the task moves to the
   other agent with a higher epoch, the job still finishes, invariants hold.
4. Kill switch: running work stops at once and the task is requeued.

    cd agent && go build -o bin/pool-agent ./cmd/pool-agent
    uv run pytest -m e2e -s tests/e2e/test_agent_e2e.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import httpx
import pytest
import yaml

from .conftest import ROOT
from .invariants import audit_chain_ok, check

pytestmark = [pytest.mark.e2e, pytest.mark.docker, pytest.mark.asyncio]

AGENT_BIN = ROOT / "agent" / "bin" / "pool-agent"
EXAMPLE = ROOT / "examples" / "hello-array.yaml"


def busybox_image() -> str:
    return yaml.safe_load(EXAMPLE.read_text())["spec"]["image"]


@pytest.fixture(scope="module", autouse=True)
def _requirements():
    if not shutil.which("docker") or subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        pytest.skip("docker daemon not available")
    if not AGENT_BIN.exists():
        if not shutil.which("go"):
            pytest.skip("go toolchain not available to build the agent")
        subprocess.run(["go", "build", "-o", str(AGENT_BIN), "./cmd/pool-agent"], cwd=ROOT / "agent", check=True)
    subprocess.run(["docker", "pull", "-q", busybox_image()], check=True, capture_output=True)


class Agent:
    def __init__(self, home: Path, name: str, server: str, log_dir: Path) -> None:
        self.home, self.name, self.server = home, name, server
        self.log = log_dir / f"agent-{name}.log"
        self.env = {**os.environ, "POOL_AGENT_HOME": str(home)}
        self.proc: subprocess.Popen | None = None
        self.device_id: str | None = None

    def cli(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run([str(AGENT_BIN), *args], env=self.env, capture_output=True, text=True,
                              timeout=60, check=check)

    def enroll(self, code: str) -> None:
        out = self.cli("enroll", "--server", self.server, "--code", code, "--name", self.name, "--insecure-http")
        assert out.returncode == 0, out.stderr

    def write_policy(self, group: str) -> None:
        policy = {
            "shares": [{"group": group,
                        "limits": {"cpu_percent": 100, "memory": "2Gi", "gpu": False, "disk": "2Gi"},
                        "accept_from": "all-members", "on_owner_return": "checkpoint-and-stop"}],
            "priority": [group],
            "isolation": {"allow_tiers": ["T1", "T2"]},
            "egress": "deny",
            "idle": "assume-idle",
        }
        (self.home / "profiles" / "default" / "policy.yaml").write_text(yaml.safe_dump(policy))

    def start(self) -> None:
        log = open(self.log, "ab")  # noqa: SIM115
        self.proc = subprocess.Popen([str(AGENT_BIN), "run"], env=self.env, stdout=log,
                                     stderr=subprocess.STDOUT)

    def kill9(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGKILL)
            self.proc.wait(10)

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(20)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def containers(label: str = "pool.attempt") -> list[str]:
    out = subprocess.run(["docker", "ps", "-aq", "--filter", f"label={label}"], capture_output=True, text=True)
    return out.stdout.split()


@pytest.fixture
async def platform(control_plane_factory, tmp_path):
    cp = control_plane_factory(POOL_LEASE_TTL_S="10", POOL_HEARTBEAT_INTERVAL_S="2", POOL_RETRY_BASE_S="1",
                               POOL_RETRY_MAX_S="2", POOL_SCHEDULER_INTERVAL_S="0.5", POOL_API_HEARTBEAT_S="1")
    cp.start_api()
    cp.start_scheduler()
    token = await cp.token("e2e-researcher")
    http = httpx.AsyncClient(base_url=cp.url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    (await http.post("/api/v1/groups", json={"name": "e2e", "display_name": "E2E"})).raise_for_status()
    agents = []
    for i in range(2):
        a = Agent(tmp_path / f"agent{i}", f"e2e-dev-{i}", cp.url, tmp_path)
        code = (await http.post("/api/v1/devices/enrollment-codes")).json()["code"]
        await asyncio.to_thread(a.enroll, code)
        a.write_policy("e2e")
        agents.append(a)
    devices = {d["name"]: d["id"] for d in (await http.get("/api/v1/devices")).json()}
    for a in agents:
        a.device_id = devices[a.name]
        (await http.put(f"/api/v1/devices/{a.device_id}/shares/e2e")).raise_for_status()
    yield cp, http, agents
    for a in agents:
        a.stop()
    await http.aclose()
    for cid in containers():
        subprocess.run(["docker", "rm", "-f", cid], capture_output=True)


async def submit(http, spec: dict) -> str:
    r = await http.post("/api/v1/groups/e2e/jobs", json=spec)
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def wait_job(http, job_id: str, timeout: float = 180) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        j = (await http.get(f"/api/v1/jobs/{job_id}")).json()
        if j["state"] in ("succeeded", "failed", "cancelled"):
            return j
        await asyncio.sleep(1)
    raise AssertionError(f"job {job_id} did not finish: {j}")


async def logs(http, job_id: str, index: int) -> str:
    return "".join(r["chunk"] for r in (await http.get(f"/api/v1/jobs/{job_id}/tasks/{index}/logs")).json())


def job(name: str, command: list[str], **extra) -> dict:
    spec = {"image": busybox_image(), "command": command, "resources": {"cpu": 0.5, "memory": "128Mi",
                                                                       "disk": "64Mi"},
            "retries": 2, "max_runtime": "5m"}
    spec.update(extra)
    return {"apiVersion": "pool/v1alpha1", "kind": "Job", "metadata": {"name": name}, "spec": spec}


async def test_array_job_runs_on_real_agents(platform):
    cp, http, agents = platform
    for a in agents:
        a.start()
    spec = yaml.safe_load(EXAMPLE.read_text())
    job_id = await submit(http, spec)
    j = await wait_job(http, job_id)
    assert j["state"] == "succeeded", j
    tasks = (await http.get(f"/api/v1/jobs/{job_id}/tasks")).json()
    assert len(tasks) == 6 and all(t["state"] == "succeeded" for t in tasks)
    out = await logs(http, job_id, 1)
    assert "seed=0 dataset=large" in out, out
    outputs = tasks[1]["result"]["outputs"]
    assert [o["path"] for o in outputs] == ["result.json"] and len(outputs[0]["sha256"]) == 64
    history = (await http.get(f"/api/v1/devices/{agents[0].device_id}/history")).json()
    history += (await http.get(f"/api/v1/devices/{agents[1].device_id}/history")).json()
    assert len([h for h in history if h["state"] == "succeeded"]) == 6
    assert all(h["submitter"] == "e2e-researcher" for h in history)
    result = await check(cp.dsn, [job_id])
    assert result["ok"], result["violations"]
    assert (await audit_chain_ok(cp.database_url))[0]
    assert containers() == [], "containers must be removed after each attempt"


async def test_sandbox_from_inside_a_job(platform, tmp_path):
    cp, http, agents = platform
    agents[0].start()
    marker = tmp_path / "host-secret.txt"
    marker.write_text("host data")
    probe = (
        "echo uid=$(id -u); grep -E '^(CapEff|NoNewPrivs):' /proc/self/status; "
        "(touch /rootfs-write 2>/dev/null && echo ROOTFS=writable) || echo ROOTFS=readonly; "
        "(wget -q -T 2 -O- http://169.254.169.254/ >/dev/null 2>&1 && echo METADATA=reachable) "
        "|| echo METADATA=blocked; "
        "(nc -w 2 1.1.1.1 53 </dev/null >/dev/null 2>&1 && echo INTERNET=reachable) || echo INTERNET=blocked; "
        "echo IFACES=$(ls /sys/class/net | tr '\\n' ' '); "
        f"(cat {marker} 2>/dev/null && echo HOSTFILE=visible) || echo HOSTFILE=hidden; "
        "echo ok > /out/probe.txt && echo OUT=writable"
    )
    job_id = await submit(http, job("sandbox-probe", ["sh", "-c", probe]))
    j = await wait_job(http, job_id, 120)
    out = await logs(http, job_id, 0)
    assert j["state"] == "succeeded", out
    assert "uid=65534" in out, out
    assert "CapEff:\t0000000000000000" in out, out
    assert "NoNewPrivs:\t1" in out, out
    for line in ("ROOTFS=readonly", "METADATA=blocked", "INTERNET=blocked", "HOSTFILE=hidden", "OUT=writable"):
        assert line in out, out
    assert "IFACES=lo" in out.replace("lo ", "lo"), out


async def test_agent_killed_mid_task_job_still_finishes(platform):
    cp, http, (a0, a1) = platform
    a0.start()
    job_id = await submit(http, job("survive", ["sh", "-c", "echo start; sleep 6; echo done"]))
    # Wait until a0 runs it, then kill the agent process with SIGKILL.
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        tasks = (await http.get(f"/api/v1/jobs/{job_id}/tasks")).json()
        if tasks[0]["state"] == "running":
            break
        await asyncio.sleep(0.5)
    assert tasks[0]["state"] == "running" and tasks[0]["epoch"] == 1
    a0.kill9()
    a1.start()
    j = await wait_job(http, job_id, 120)
    assert j["state"] == "succeeded"
    tasks = (await http.get(f"/api/v1/jobs/{job_id}/tasks")).json()
    assert tasks[0]["epoch"] >= 2 and tasks[0]["device"] == a1.name
    # The killed agent comes back: its stale attempt is rejected and its orphan container cleaned up.
    a0.start()
    await asyncio.sleep(8)
    result = await check(cp.dsn, [job_id])
    assert result["ok"], result["violations"]
    hist0 = (await http.get(f"/api/v1/devices/{a0.device_id}/history")).json()
    assert hist0[0]["state"] in ("lost", "cancelled")
    for _ in range(20):
        if not containers(f"pool.attempt={hist0[0]['attempt_id']}"):
            break
        await asyncio.sleep(1)
    assert containers(f"pool.attempt={hist0[0]['attempt_id']}") == [], "orphan container left behind"


async def test_kill_switch_stops_work(platform):
    cp, http, (a0, _) = platform
    a0.start()
    job_id = await submit(http, job("long", ["sh", "-c", "sleep 120"]))
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if (await http.get(f"/api/v1/jobs/{job_id}/tasks")).json()[0]["state"] == "running":
            break
        await asyncio.sleep(0.5)
    t0 = time.monotonic()
    out = await asyncio.to_thread(a0.cli, "kill")
    assert out.returncode == 0, out.stderr
    assert containers() == [], "kill switch must remove containers immediately"
    for _ in range(20):
        task = (await http.get(f"/api/v1/jobs/{job_id}/tasks")).json()[0]
        if task["state"] == "pending":
            break
        await asyncio.sleep(0.5)
    assert task["state"] == "pending" and task["failures"] == 0, task  # no retry used (design doc §4)
    assert time.monotonic() - t0 < 15
    # While the switch is on, the agent asks for no work.
    await asyncio.sleep(4)
    assert (await http.get(f"/api/v1/jobs/{job_id}/tasks")).json()[0]["state"] == "pending"
    assert (await asyncio.to_thread(a0.cli, "resume")).returncode == 0
    for _ in range(30):
        if (await http.get(f"/api/v1/jobs/{job_id}/tasks")).json()[0]["state"] == "running":
            break
        await asyncio.sleep(1)
    assert (await http.get(f"/api/v1/jobs/{job_id}/tasks")).json()[0]["state"] == "running"
    await http.post(f"/api/v1/jobs/{job_id}/cancel")


async def test_each_run_is_measured_and_out_of_memory_is_named(platform):
    """docs/design/pool-decides.md step A: the real agent measures each run; the job shows asked vs used."""
    cp, http, agents = platform
    agents[0].start()
    hold = "a=$(dd if=/dev/zero bs=1048576 count=100 2>/dev/null | tr '\\000' 'a'); sleep 4; echo ${#a}"
    ok = await submit(http, job("measured", ["sh", "-c", hold], resources={"cpu": 1, "memory": "512Mi"}))
    oom = await submit(http, job("too-big", ["sh", "-c", hold], resources={"cpu": 1, "memory": "64Mi"}, retries=0))
    j = await wait_job(http, ok)
    assert j["state"] == "succeeded", j
    assert j["requested"]["memory_mb"] == 512 and j["used"]["measured"] == 1
    assert 90 <= j["used"]["peak_memory_mb"] <= 512, j["used"]
    assert j["used"]["max_wall_seconds"] >= 4 and j["used"]["avg_cpu"] <= 1.0
    task = (await http.get(f"/api/v1/jobs/{ok}/tasks")).json()[0]
    assert task["usage"]["samples"] >= 2 and task["usage"]["capped"] in ([], ["peak_cpu"]), task["usage"]
    j = await wait_job(http, oom)
    task = (await http.get(f"/api/v1/jobs/{oom}/tasks")).json()[0]
    assert j["state"] == "failed" and task["result"]["oom_killed"] is True, task
    assert "out of memory" in task["result"]["error"]
