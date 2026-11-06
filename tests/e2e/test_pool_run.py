"""`pool run` end to end (docs/design/pool-run.md, step 1), with the real pieces: the `pool` command as a
process, Docker building on "your" computer, a CNCF Distribution registry in token mode signing in through the
API, the real agent pulling with a server-signed token, and SeaweedFS for the files.

    cd agent && go build -o bin/pool-agent ./cmd/pool-agent
    uv run pytest -m e2e -s tests/e2e/test_pool_run.py
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import platform
import re
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest

from pool_cp import builtin_registry
from pool_cp.config import Settings

from .conftest import free_port
from .test_agent_e2e import _requirements, busybox_image, containers, wait_job  # noqa: F401
from .test_object_store import cleanup, make_platform, seaweedfs, wait_object_store  # noqa: F401

pytestmark = [pytest.mark.e2e, pytest.mark.docker, pytest.mark.asyncio]

REGISTRY_IMAGE = "registry:2.8.3@sha256:a3d8aaa63ed8681a604f1dea0aa03f100d5895b6a58ace528858a7b332415373"
SKILL_SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "pool" / "scripts"
PLATFORM = "linux/arm64" if platform.machine() in ("aarch64", "arm64") else "linux/amd64"

RUN_SH = """\
case "$1" in
  sleep) echo "sleeping"; sleep "$2" ;;
  write-app) echo "trying /app"; echo x > /app/x ;;
  *) echo "args: $*"; echo "part $POOL_TASK_INDEX of $POOL_TASK_COUNT"; echo "{version}" > /out/version.txt
     mkdir -p /out/sub; echo "$*" > /out/sub/args.txt ;;
esac
"""


def pool(cfg_dir, cwd, *args, check=True, timeout=300) -> subprocess.CompletedProcess:
    env = {**os.environ, "POOL_CONFIG_DIR": str(cfg_dir)}
    env.pop("POOL_TOKEN", None)
    env.pop("POOL_SERVER", None)
    r = subprocess.run([sys.executable, "-m", "pool_cli.main", *args], cwd=cwd, env=env, capture_output=True,
                       text=True, timeout=timeout)
    if check:
        assert r.returncode == 0, f"pool {' '.join(args)} -> {r.returncode}\n{r.stdout}\n{r.stderr}"
    return r


@pytest.fixture
async def run_platform(seaweedfs, control_plane_factory, tmp_path, cleanup):  # noqa: F811
    port = free_port()
    host = f"127.0.0.1:{port}"
    cp, http, agent, group_id = await make_platform(
        control_plane_factory, tmp_path, POOL_REGISTRY_ADDRESS=host, POOL_REGISTRY_INTERNAL_URL=f"http://{host}",
        **seaweedfs.env())
    cleanup.append((agent, http))
    await wait_object_store(cp.url, "ready")
    cert = tmp_path / "keys" / "registry.crt"
    assert cert.exists(), "the API creates the registry's certificate on its first start"
    name = f"pool-e2e-run-registry-{uuid.uuid4().hex[:8]}"
    # The same hardening as deploy/compose.yaml gives it: no capabilities, a read-only image, no new privileges.
    subprocess.run(["docker", "run", "-d", "--name", name, "-p", f"{host}:5000", "--cap-drop", "ALL",
                    "--read-only", "--tmpfs", "/tmp", "--security-opt", "no-new-privileges",
                    "-e", "REGISTRY_AUTH=token", "-e", f"REGISTRY_AUTH_TOKEN_REALM={cp.url}/api/v1/registry/token",
                    "-e", "REGISTRY_AUTH_TOKEN_SERVICE=pool-registry", "-e", "REGISTRY_AUTH_TOKEN_ISSUER=pool-server",
                    "-e", "REGISTRY_AUTH_TOKEN_ROOTCERTBUNDLE=/certs/registry.crt",
                    "-e", "REGISTRY_STORAGE_DELETE_ENABLED=true", "-v", f"{cert}:/certs/registry.crt:ro",
                    REGISTRY_IMAGE], check=True, capture_output=True)
    try:
        for _ in range(60):
            try:
                if httpx.get(f"http://{host}/v2/", timeout=2).status_code == 401:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        agent.start()
        token = await cp.token("e2e-researcher")
        cfg = tmp_path / "cli"
        cfg.mkdir()
        (cfg / "config.json").write_text(json.dumps({"server": cp.url, "token": token, "default_group": "e2e"}))
        project = tmp_path / "project"
        project.mkdir()
        (project / "Dockerfile").write_text(f"FROM {busybox_image()}\nCOPY run.sh /app/run.sh\n"
                                            'ENTRYPOINT ["sh", "/app/run.sh"]\n')
        (project / "run.sh").write_text(RUN_SH.format(version="v1"))
        yield {"cp": cp, "http": http, "host": host, "cfg": cfg, "project": project, "keys": tmp_path / "keys"}
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


async def test_pool_run_builds_pushes_runs_and_brings_the_files_back(run_platform):
    p = run_platform
    r = await asyncio.to_thread(pool, p["cfg"], p["project"], "run", "--platform", PLATFORM, "-e", "MODE=e2e", "--",
                                "hello", "world")
    assert "args: hello world" in r.stdout and "part 0 of 1" in r.stdout, r.stdout
    assert "Uploaded 2 new layer(s), 0 already there" in r.stderr and "Running on e2e-store-dev" in r.stderr
    assert (p["project"] / "pool-out" / "version.txt").read_text() == "v1\n"
    assert (p["project"] / "pool-out" / "sub" / "args.txt").read_text() == "hello world\n"

    # A code change uploads only the layer that changed.
    (p["project"] / "run.sh").write_text(RUN_SH.format(version="v2"))
    r = await asyncio.to_thread(pool, p["cfg"], p["project"], "run", "--platform", PLATFORM, "-o", "out2", "--", "x")
    assert "Uploaded 1 new layer(s), 1 already there" in r.stderr, r.stderr
    assert (p["project"] / "out2" / "version.txt").read_text() == "v2\n"

    # The image is in my own space of the group's registry, and nobody else can push there.
    jobs = (await p["http"].get("/api/v1/groups/e2e/jobs")).json()
    image = jobs[0]["image"]
    assert image.startswith(f"{p['host']}/e2e/e2e-researcher/project:run-") and "@sha256:" in image
    other = await p["cp"].token("someone-else")
    cfg = p["cfg"].parent / "other-docker"
    cfg.mkdir()
    (cfg / "config.json").write_text(json.dumps({"auths": {p["host"]: {
        "auth": base64.b64encode(f"someone-else:{other}".encode()).decode()}}}))
    tag = image.split("@")[0]
    subprocess.run(["docker", "tag", tag, f"{p['host']}/e2e/e2e-researcher/project:stolen"], check=True)
    push = subprocess.run(["docker", "--config", str(cfg), "push", f"{p['host']}/e2e/e2e-researcher/project:stolen"],
                          capture_output=True, text=True)
    assert push.returncode != 0 and ("denied" in push.stderr.lower() or "unauthorized" in push.stderr.lower())

    # The server's clean-up can delete it in the real registry (Distribution's "delete" action).
    settings = Settings(dev_mode=True, data_dir=p["keys"], registry_address=p["host"],
                        registry_internal_url=f"http://{p['host']}")
    reg = builtin_registry.Registry(settings, builtin_registry.Issuer(
        settings.registry_key_file, settings.registry_cert_file, "pool-server", "pool-registry", create=False))
    repo, digest = "e2e/e2e-researcher/project", image.rsplit("@", 1)[1]
    assert repo in reg.repositories() and digest == reg.digest(repo, tag.rsplit(":", 1)[1])
    assert reg.delete(repo, digest) and reg.digest(repo, tag.rsplit(":", 1)[1]) is None


async def test_pool_run_local_fails_like_a_members_computer_would(run_platform):
    p = run_platform
    r = await asyncio.to_thread(pool, p["cfg"], p["project"], "run", "--local", "--platform", PLATFORM, "--",
                                "write-app", check=False)
    assert r.returncode != 0 and "trying /app" in r.stdout
    assert "read-only" in r.stdout.lower() and "Write to /out" in r.stderr, (r.stdout, r.stderr)
    r = await asyncio.to_thread(pool, p["cfg"], p["project"], "run", "--local", "--platform", PLATFORM, "--", "ok")
    assert (p["project"] / "pool-out" / "version.txt").read_text() == "v1\n", "files come back here too"


async def test_ctrl_c_stops_the_job_and_its_container(run_platform):
    p = run_platform
    env = {**os.environ, "POOL_CONFIG_DIR": str(p["cfg"])}
    proc = subprocess.Popen([sys.executable, "-m", "pool_cli.main", "run", "--platform", PLATFORM, "--", "sleep",
                             "300"], cwd=p["project"], env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + 180
    while not containers():
        assert time.monotonic() < deadline and proc.poll() is None, proc.communicate()
        await asyncio.sleep(0.5)
    proc.send_signal(signal.SIGINT)
    out, err = proc.communicate(timeout=30)
    assert proc.returncode == 130 and "stopped" in err, (out, err)
    deadline = time.monotonic() + 10
    while containers():
        assert time.monotonic() < deadline, "the container should be gone within 10 s"
        await asyncio.sleep(0.5)
    job = (await p["http"].get("/api/v1/groups/e2e/jobs")).json()[0]
    assert job["tasks"].get("cancelled") == 1


async def test_attach_follows_a_job_submitted_elsewhere(run_platform):
    p = run_platform
    await asyncio.to_thread(pool, p["cfg"], p["project"], "run", "--platform", PLATFORM, "-d", "--", "first")
    image = (await p["http"].get("/api/v1/groups/e2e/jobs")).json()[0]["image"]
    # As the browser would: the same image, submitted through the API.
    r = await p["http"].post("/api/v1/groups/e2e/jobs", json={"image": image, "args": ["from", "browser"]})
    assert r.status_code == 201, r.text
    job_id = r.json()["id"]
    r = await asyncio.to_thread(pool, p["cfg"], p["project"], "attach", job_id, "-o", "attached")
    assert "args: from browser" in r.stdout
    assert (p["project"] / "attached" / "sub" / "args.txt").read_text() == "from browser\n"
    assert (await wait_job(p["http"], job_id, 60))["tasks"]["succeeded"] == 1


def _job_id(stderr: str) -> str:
    m = re.search(r"to \S+: job (\S+)$", stderr, re.M)
    assert m, stderr
    return m.group(1)


def _script(env, *args, timeout=180) -> subprocess.CompletedProcess:
    return subprocess.run([str(SKILL_SCRIPTS / args[0]), *args[1:]], env=env, capture_output=True, text=True,
                          timeout=timeout)


async def test_the_agent_skill_checks_waits_and_collects(run_platform):
    """skills/pool, step by step as SKILL.md tells an agent: check.sh, pool run -d, wait.sh, pool attach."""
    p = run_platform
    env = {**os.environ, "POOL_CONFIG_DIR": str(p["cfg"]), "POOL_CMD": f"{sys.executable} -m pool_cli.main"}
    env.pop("POOL_TOKEN", None)
    env.pop("POOL_SERVER", None)
    r = await asyncio.to_thread(_script, env, "check.sh")
    assert r.returncode == 0 and "OK   signed in: e2e-researcher" in r.stdout, r.stdout + r.stderr

    r = await asyncio.to_thread(pool, p["cfg"], p["project"], "run", "--platform", PLATFORM, "-d", "--", "skill")
    job_id = _job_id(r.stderr)
    r = await asyncio.to_thread(_script, env, "wait.sh", job_id, "120")
    assert r.returncode == 0 and f"job {job_id} ended: succeeded" in r.stdout, r.stdout + r.stderr
    await asyncio.to_thread(pool, p["cfg"], p["project"], "attach", job_id, "-o", "collected")
    assert (p["project"] / "collected" / "sub" / "args.txt").read_text() == "skill\n"

    # Still running at the timeout: exit 2, and the job is left alone (waiting never cancels).
    r = await asyncio.to_thread(pool, p["cfg"], p["project"], "run", "--platform", PLATFORM, "-d", "--", "sleep", "300")
    slow = _job_id(r.stderr)
    r = await asyncio.to_thread(_script, env, "wait.sh", slow, "1")
    assert r.returncode == 2 and "still" in r.stdout, r.stdout + r.stderr
    assert (await p["http"].get(f"/api/v1/jobs/{slow}")).json()["state"] in ("pending", "running")
    await asyncio.to_thread(pool, p["cfg"], p["project"], "cancel", slow)
    assert (await wait_job(p["http"], slow, 60))["state"] == "cancelled"

    # Errors exit 1 with the reason: an unknown job, and no sign-in.
    r = await asyncio.to_thread(_script, env, "wait.sh", str(uuid.uuid4()), "5")
    assert r.returncode == 1 and r.stderr, r.stdout + r.stderr
    nobody = p["cfg"].parent / "nobody"
    nobody.mkdir()
    r = await asyncio.to_thread(_script, {**env, "POOL_CONFIG_DIR": str(nobody)}, "check.sh")
    assert r.returncode == 1 and "MISSING sign-in" in r.stdout, r.stdout + r.stderr
