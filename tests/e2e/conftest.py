"""End-to-end fixtures: real control-plane processes (API + scheduler) on a fresh database."""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import asyncpg
import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
ADMIN_DSN = os.environ.get("POOL_TEST_ADMIN_DSN", "postgresql://postgres@127.0.0.1:55432/postgres")
sys.path.insert(0, str(ROOT / "control-plane" / "tests"))  # fake_agent helpers


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ControlPlane:
    """API server and scheduler as separate OS processes, so tests can kill and restart them."""

    def __init__(self, dsn: str, data_dir: Path, log_dir: Path, **env: str) -> None:
        self.dsn = dsn
        self.database_url = dsn.replace("postgresql://", "postgresql+asyncpg://")
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.log_dir = log_dir
        self.env = {**os.environ, "POOL_DEV_MODE": "true", "POOL_DATABASE_URL": self.database_url,
                    "POOL_DATA_DIR": str(data_dir), "POOL_ENROLL_RATE_PER_MINUTE": "100000",
                    "POOL_SUBMIT_RATE_PER_MINUTE": "1000", **env}
        self.api: subprocess.Popen | None = None
        self.scheduler: subprocess.Popen | None = None
        self.restarts = 0

    def _spawn(self, name: str, args: list[str]) -> subprocess.Popen:
        log = open(self.log_dir / f"{name}.log", "ab")  # noqa: SIM115
        return subprocess.Popen(args, env=self.env, stdout=log, stderr=subprocess.STDOUT, cwd=ROOT)

    def start_api(self) -> None:
        self.api = self._spawn("api", [sys.executable, "-m", "uvicorn", "pool_cp.app:create_app", "--factory",
                                       "--host", "127.0.0.1", "--port", str(self.port), "--ws-max-size", "1048576",
                                       "--log-level", "warning"])
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                if httpx.get(self.url + "/readyz", timeout=1).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        raise RuntimeError("API did not start; see api.log")

    def kill_api(self) -> None:
        if self.api and self.api.poll() is None:
            self.api.send_signal(signal.SIGKILL)
            self.api.wait(10)

    def restart_api(self) -> None:
        self.kill_api()
        self.start_api()
        self.restarts += 1

    def start_scheduler(self) -> None:
        self.scheduler = self._spawn("scheduler", [sys.executable, "-m", "pool_cp.scheduler"])

    def stop(self) -> None:
        for p in (self.api, self.scheduler):
            if p and p.poll() is None:
                p.terminate()
                try:
                    p.wait(10)
                except subprocess.TimeoutExpired:
                    p.kill()

    async def token(self, username: str) -> str:
        async with httpx.AsyncClient(base_url=self.url) as c:
            r = await c.post("/dev/token", json={"username": username})
            r.raise_for_status()
            return r.json()["access_token"]


@pytest.fixture
def fresh_db():
    name = f"pool_e2e_{uuid.uuid4().hex[:8]}"

    async def create():
        c = await asyncpg.connect(ADMIN_DSN)
        await c.execute(f'CREATE DATABASE "{name}"')
        await c.close()

    async def drop():
        c = await asyncpg.connect(ADMIN_DSN)
        await c.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await c.close()

    asyncio.run(create())
    dsn = ADMIN_DSN.rsplit("/", 1)[0] + f"/{name}"
    from pool_cp.migrate import upgrade

    upgrade(dsn.replace("postgresql://", "postgresql+asyncpg://"))
    yield dsn
    if not os.environ.get("POOL_TEST_KEEP_DB"):
        asyncio.run(drop())


@pytest.fixture
def control_plane_factory(fresh_db, tmp_path):
    planes: list[ControlPlane] = []

    def make(**env: str) -> ControlPlane:
        cp = ControlPlane(fresh_db, tmp_path / "keys", tmp_path, **env)
        planes.append(cp)
        return cp

    yield make
    for cp in planes:
        cp.stop()
