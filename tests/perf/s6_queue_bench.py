"""Spike S6: PostgreSQL queue throughput with SKIP LOCKED (plan §9 Phase 0).

Measures the real claim path (`pool_cp.scheduler.core.claim_task`) and the full
task lifecycle (claim -> accept -> started -> commit) with M concurrent
simulated devices, each in its own DB session, against a fresh database.
It bypasses HTTP/WebSocket so it measures the queue, not the network.

    uv run python tests/perf/s6_queue_bench.py --tasks 20000 --workers 1,4,16,32

Prints a markdown table. Numbers depend on hardware; record the machine.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import time
import uuid

import asyncpg
from sqlalchemy import insert, text

from pool_cp.ca import EnvelopeSigner
from pool_cp.config import Settings
from pool_cp.db import make_engine, make_sessionmaker
from pool_cp.migrate import upgrade
from pool_cp.models import Device, DeviceShare, Group, Job, Membership, Task, User
from pool_cp.scheduler import core
from pool_protocol import messages as pm

ADMIN = os.environ.get("POOL_TEST_ADMIN_DSN", "postgresql://postgres@127.0.0.1:55432/postgres")
IMG = "registry.example.org/bench/app@sha256:" + "b" * 64


async def setup(url: str, n_tasks: int, n_devices: int, maker):
    async with maker() as s:
        u = User(subject=f"bench|{uuid.uuid4()}", handle=f"bench-{uuid.uuid4().hex[:6]}")
        g = Group(name=f"bench-{uuid.uuid4().hex[:8]}", display_name="bench")
        s.add_all([u, g])
        await s.flush()
        s.add(Membership(group_id=g.id, user_id=u.id, role="coordinator"))
        devices = []
        for i in range(n_devices):
            d = Device(owner_id=u.id, name=f"d{i}", os="linux", arch="amd64", public_key=os.urandom(32),
                       cert_serial=uuid.uuid4().hex, cert_not_after=pm.TaskEnvelope.model_fields["issued_at"]
                       .annotation.fromisoformat("2030-01-01T00:00:00+00:00"),
                       capabilities={"os": "linux", "tiers": ["T1", "T2"], "gpu_tiers": [], "gpus": []},
                       reported_shares=[{"group": g.name, "active_now": True, "accept_from": "all-members"}])
            s.add(d)
            devices.append(d)
        await s.flush()
        for d in devices:
            s.add(DeviceShare(device_id=d.id, group_id=g.id))
        job = Job(group_id=g.id, submitter_id=u.id, name="bench", shape="array", spec={}, image=IMG, cpu=1,
                  memory_mb=256, disk_mb=0, min_tier_rank=1, data_class="internal", max_retries=0)
        s.add(job)
        await s.flush()
        spec = pm.TaskSpec(image=IMG, command=["true"], resources={"cpu": 1, "memory_mb": 256},
                           max_runtime_s=60).model_dump(mode="json")
        rows = [{"id": uuid.uuid4(), "job_id": job.id, "group_id": g.id, "index": i, "params": {}, "spec": spec,
                 "state": "pending", "epoch": 0, "attempts_count": 0, "failures": 0, "max_retries": 0}
                for i in range(n_tasks)]
        for i in range(0, len(rows), 2000):
            await s.execute(insert(Task), rows[i:i + 2000])
        await s.commit()
        return [d.id for d in devices]


async def worker(maker, settings, signer, device_id, quota, latencies, lifecycle: bool, stop_at: float):
    done = 0
    ready = pm.Ready(free={"cpu": 8, "memory_mb": 8192, "disk_mb": 0, "gpus": 0}, tiers=["T1", "T2"])
    while done < quota and time.perf_counter() < stop_at:
        t0 = time.perf_counter()
        async with maker() as s:
            device = await s.get(Device, device_id)
            offer = await core.claim_task(s, settings, signer, device, ready)
            await s.commit()
        if offer is None:
            break
        if lifecycle:
            async with maker() as s:
                await core.handle_accept(s, settings, device_id, pm.Accept(attempt_id=offer.attempt_id,
                                                                          epoch=offer.epoch, tier="T2"))
                await s.commit()
            async with maker() as s:
                await core.handle_started(s, settings, device_id, pm.Started(attempt_id=offer.attempt_id,
                                                                            epoch=offer.epoch))
                await s.commit()
            async with maker() as s:
                ack = await core.handle_commit(s, settings, device_id, pm.Commit(
                    idempotency_key=offer.attempt_id, task_id=offer.task_id, attempt_id=offer.attempt_id,
                    epoch=offer.epoch, exit_code=0))
                await s.commit()
                assert ack.accepted
        latencies.append(time.perf_counter() - t0)
        done += 1
    return done


async def run_case(n_tasks: int, n_workers: int, lifecycle: bool, seconds: float) -> dict:
    name = f"pool_s6_{uuid.uuid4().hex[:8]}"
    conn = await asyncpg.connect(ADMIN)
    await conn.execute(f'CREATE DATABASE "{name}"')
    await conn.close()
    url = ADMIN.rsplit("/", 1)[0].replace("postgresql://", "postgresql+asyncpg://") + f"/{name}"
    await asyncio.to_thread(upgrade, url)
    engine = make_engine(url)
    engine.pool._pool.maxsize = max(n_workers + 5, 10)  # noqa: SLF001
    maker = make_sessionmaker(engine)
    tmp = f"/tmp/claude-1000/s6-{uuid.uuid4().hex[:6]}"
    settings = Settings(database_url=url, dev_mode=True, data_dir=tmp)
    signer = EnvelopeSigner(settings.signing_key_file, create=True)
    try:
        devices = await setup(url, n_tasks, n_workers, maker)
        latencies: list[float] = []
        stop_at = time.perf_counter() + seconds
        t0 = time.perf_counter()
        counts = await asyncio.gather(*(worker(maker, settings, signer, d, n_tasks, latencies, lifecycle, stop_at)
                                        for d in devices))
        elapsed = time.perf_counter() - t0
        async with maker() as s:
            dup = (await s.execute(text("SELECT count(*) FROM (SELECT task_id FROM attempts GROUP BY task_id "
                                        "HAVING count(*) > 1) x"))).scalar_one()
        total = sum(counts)
        lat = sorted(latencies)
        return {"workers": n_workers, "lifecycle": lifecycle, "tasks": total, "seconds": round(elapsed, 2),
                "per_s": round(total / elapsed, 1),
                "p50_ms": round(statistics.median(lat) * 1000, 1) if lat else None,
                "p99_ms": round(lat[int(len(lat) * 0.99) - 1] * 1000, 1) if lat else None,
                "double_claimed": dup}
    finally:
        await engine.dispose()
        conn = await asyncpg.connect(ADMIN)
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await conn.close()


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", type=int, default=20000)
    ap.add_argument("--workers", default="1,4,16,32")
    ap.add_argument("--seconds", type=float, default=20.0, help="time cap per case")
    args = ap.parse_args()
    print("| mode | workers | tasks done | seconds | tasks/s | p50 ms | p99 ms | double-claimed |")
    print("|---|---|---|---|---|---|---|---|")
    for lifecycle in (False, True):
        for w in (int(x) for x in args.workers.split(",")):
            r = await run_case(args.tasks, w, lifecycle, args.seconds)
            mode = "claim+accept+start+commit" if lifecycle else "claim only"
            print(f"| {mode} | {r['workers']} | {r['tasks']} | {r['seconds']} | {r['per_s']} | {r['p50_ms']} | "
                  f"{r['p99_ms']} | {r['double_claimed']} |", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
