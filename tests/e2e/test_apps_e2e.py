"""Apps end to end (docs/design/pool-run.md step 2), with the real agent and Docker: a web server in a job,
opened on "your" computer by `pool attach -p`, reached only through pool.

1. The computer's owner allows apps in its policy.
2. A busybox web server runs as an app: it has no network at all (only the loopback interface).
3. `pool attach JOB -p HERE:8000` (the real CLI as a process) listens on 127.0.0.1:HERE; each connection goes
   through the server and the agent (`docker exec ... pool-agent pipe`) into the container.
4. Someone else cannot open it. Stopping the job ends `pool attach` and removes the container.

The agent must be a static binary for apps (it runs itself inside the app's container):

    cd agent && CGO_ENABLED=0 go build -o bin/pool-agent ./cmd/pool-agent
    uv run pytest -m e2e -s tests/e2e/test_apps_e2e.py
"""

from __future__ import annotations

import asyncio
import json
import os
import struct
import subprocess
import sys
import time

import httpx
import pytest
import websockets
import yaml
from websockets.exceptions import InvalidStatus

from .conftest import ROOT, free_port
from .test_agent_e2e import AGENT_BIN, _requirements, busybox_image, containers, logs, platform, submit  # noqa: F401

pytestmark = [pytest.mark.e2e, pytest.mark.docker, pytest.mark.asyncio]

APP = (
    "echo IFACES=$(ls /sys/class/net | tr '\\n' ' '); "
    "(nc -w 2 1.1.1.1 53 </dev/null >/dev/null 2>&1 && echo INTERNET=reachable) || echo INTERNET=blocked; "
    "mkdir -p /tmp/www && echo hello-from-the-app > /tmp/www/index.html && "
    "echo serving && exec httpd -f -p 8000 -h /tmp/www"
)


def is_static(path) -> bool:
    """A Linux ELF binary with no program interpreter (PT_INTERP) is statically linked."""
    with open(path, "rb") as f:
        head = f.read(64)
        if head[:4] != b"\x7fELF" or head[4] != 2:  # 64-bit only here
            return False
        phoff, = struct.unpack_from("<Q", head, 32)
        phentsize, phnum = struct.unpack_from("<HH", head, 54)
        f.seek(phoff)
        table = f.read(phentsize * phnum)
    return all(struct.unpack_from("<I", table, i * phentsize)[0] != 3 for i in range(phnum))


@pytest.fixture(scope="module", autouse=True)
def _static_agent(_requirements):  # noqa: F811
    if not is_static(AGENT_BIN):  # an agent built with cgo cannot run apps: build it the way apps need
        subprocess.run(["go", "build", "-o", str(AGENT_BIN), "./cmd/pool-agent"], cwd=ROOT / "agent", check=True,
                       env={**os.environ, "CGO_ENABLED": "0"})


def allow_apps(agent) -> None:
    path = agent.home / "profiles" / "default" / "policy.yaml"
    policy = yaml.safe_load(path.read_text())
    policy["shares"][0]["apps"] = {"allow": True, "max_hours": 1}
    path.write_text(yaml.safe_dump(policy))


async def wait_running(http, job_id: str, timeout: float = 120) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if (await http.get(f"/api/v1/jobs/{job_id}")).json()["tasks"].get("running"):
            return
        await asyncio.sleep(0.5)
    raise AssertionError("the app did not start")


async def test_an_app_is_reached_only_through_pool(platform, tmp_path):  # noqa: F811
    cp, http, agents = platform
    allow_apps(agents[0])
    agents[0].start()
    job_id = await submit(http, {"apiVersion": "pool/v1alpha1", "kind": "Job", "metadata": {"name": "web"},
                                 "spec": {"image": busybox_image(), "command": ["sh", "-c", APP], "ports": [8000],
                                          "resources": {"cpu": 0.5, "memory": "128Mi", "disk": "64Mi"},
                                          "max_runtime": "30m", "retries": 0}})
    await wait_running(http, job_id)

    # pool attach, as you would run it.
    cfg = tmp_path / "cli"
    cfg.mkdir()
    token = http.headers["Authorization"].split()[1]
    (cfg / "config.json").write_text(json.dumps({"server": cp.url, "token": token, "default_group": "e2e"}))
    here = free_port()
    attach = subprocess.Popen([sys.executable, "-m", "pool_cli.main", "attach", job_id, "-p", f"{here}:8000", "-y",
                               "--out", str(tmp_path / "out")], env={**os.environ, "POOL_CONFIG_DIR": str(cfg)},
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        page = None
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                r = await asyncio.to_thread(httpx.get, f"http://127.0.0.1:{here}/", timeout=10)
                if r.status_code == 200:
                    page = r.text
                    break
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.5)
        assert page == "hello-from-the-app\n", "the app's page, through pool"
        # Several connections at once, each its own tunnel.
        pages = await asyncio.gather(*[asyncio.to_thread(httpx.get, f"http://127.0.0.1:{here}/", timeout=20)
                                       for _ in range(5)])
        assert [p.text for p in pages] == ["hello-from-the-app\n"] * 5

        # Someone else cannot open it.
        other = await cp.token("e2e-someone-else")
        url = cp.url.replace("http", "ws", 1) + f"/api/v1/jobs/{job_id}/tunnel?port=8000"
        with pytest.raises(InvalidStatus):
            async with websockets.connect(url, additional_headers={"Authorization": f"Bearer {other}"}):
                pass

        # The app itself has no network: only the loopback interface.
        out = await logs(http, job_id, 0)
        assert "IFACES=lo \n" in out or "IFACES=lo\n" in out, out
        assert "INTERNET=blocked" in out, out

        # Stopping the job ends pool attach and removes the container.
        (await http.post(f"/api/v1/jobs/{job_id}/cancel")).raise_for_status()
        code = await asyncio.to_thread(attach.wait, 60)
        err = attach.stderr.read()
        assert code == 130, err
        assert f"Open http://localhost:{here} for the app's port 8000" in err, err
    finally:
        if attach.poll() is None:
            attach.kill()
    for _ in range(30):
        if not containers():
            break
        await asyncio.sleep(1)
    assert containers() == [], "the app's container is removed"
