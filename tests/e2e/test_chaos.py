"""Chaos test (plan §9 Phase 2 exit, §10): random agent kills, network cuts and server restarts.

Every task must end in exactly one final state, and no result from a stale
epoch may ever be committed. Agents here are protocol-level fakes (no
containers) so the run is fast and repeatable; the Go agent has its own
end-to-end test in test_agent_e2e.py.

    uv run pytest -m e2e tests/e2e/test_chaos.py -s
"""

from __future__ import annotations

import asyncio
import base64
import random
import time
from collections import defaultdict

import httpx
import pytest
import websockets
from fake_agent import CAPS, FREE, FakeAgent, share

from pool_protocol import messages as pm

from .invariants import audit_chain_ok, check

pytestmark = [pytest.mark.e2e, pytest.mark.asyncio]

IMG = "registry.example.org/chaos/app@sha256:" + "c" * 64
P_CRASH, P_PARTITION, P_FAIL = 0.05, 0.05, 0.05


class Ledger:
    """What agents observed: every accepted commit, every rejected stale commit."""

    def __init__(self) -> None:
        self.accepted: dict[str, list[tuple[str, int, int]]] = defaultdict(list)
        self.rejected: list[tuple[str, str]] = []
        self.events: dict[str, int] = defaultdict(int)


class ChaosAgent:
    def __init__(self, base: FakeAgent, group: str, ledger: Ledger, rng: random.Random) -> None:
        self.base = base
        self.group = group
        self.ledger = ledger
        self.rng = rng
        self.running: dict[str, tuple[str, int]] = {}  # attempt -> (task, epoch)
        self.cancelled: set[str] = set()
        self.partitioned_until = 0.0
        self.ws = None
        self.queue: asyncio.Queue = asyncio.Queue()
        self.acks: dict[str, asyncio.Future] = {}

    async def _send(self, msg) -> None:
        await self.ws.send(msg.model_dump_json())

    async def _reader(self) -> None:
        async for raw in self.ws:
            msg = pm.parse_server_message(raw)
            if isinstance(msg, (pm.Offer, pm.NoWork)):
                await self.queue.put(msg)
            elif isinstance(msg, pm.CommitAck):
                fut = self.acks.pop(msg.attempt_id, None)
                if fut and not fut.done():
                    fut.set_result(msg)
            elif isinstance(msg, pm.HeartbeatAck):
                self.cancelled.update(msg.stale)
            elif isinstance(msg, (pm.Cancel, pm.Preempt)):
                self.cancelled.add(msg.attempt_id)

    async def _heartbeats(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            if time.monotonic() < self.partitioned_until:
                continue  # network cut: nothing reaches the server
            running = [{"attempt_id": a, "epoch": e, "state": "running"} for a, (_, e) in self.running.items()]
            await self._send(pm.Heartbeat(running=running))

    async def _connect(self) -> None:
        ws_url = self.base.base_url.replace("http", "ws", 1) + "/api/v1/agent/ws"
        self.ws = await websockets.connect(ws_url, open_timeout=5)
        ch = pm.parse_server_message(await asyncio.wait_for(self.ws.recv(), 5))
        sig = self.base.key.sign(pm.HELLO_SIGNATURE_CONTEXT + base64.b64decode(ch.nonce_b64))
        running = [{"attempt_id": a, "epoch": e, "state": "running"} for a, (_, e) in self.running.items()]
        await self._send(pm.Hello(agent_version="chaos", device_id=self.base.device_id, profile="default",
                                  cert_pem=self.base.cert_pem, nonce_signature_b64=base64.b64encode(sig).decode(),
                                  capabilities=CAPS, shares=[share(self.group)], policy_digest="sha256:" + "0" * 64,
                                  running=running))
        welcome = pm.parse_server_message(await asyncio.wait_for(self.ws.recv(), 5))
        assert isinstance(welcome, pm.Welcome)
        self.cancelled.update(welcome.stale)

    async def _work_one(self) -> None:
        await self._send(pm.Ready(free=FREE, tiers=["T1", "T2"]))
        msg = await asyncio.wait_for(self.queue.get(), 10)
        if isinstance(msg, pm.NoWork):
            await asyncio.sleep(0.3)
            return
        self.base.open_envelope(msg)  # signature check
        a, t, e = msg.attempt_id, msg.task_id, msg.epoch
        await self._send(pm.Accept(attempt_id=a, epoch=e, tier="T2"))
        await self._send(pm.Started(attempt_id=a, epoch=e))
        self.running[a] = (t, e)
        dice = self.rng.random()
        if dice < P_CRASH:  # process dies mid-task and forgets everything
            self.ledger.events["crash"] += 1
            self.running.clear()
            await self.ws.close()
            raise ConnectionError("crash")
        if dice < P_CRASH + P_PARTITION:  # long network cut while still computing
            self.ledger.events["partition"] += 1
            self.partitioned_until = time.monotonic() + 4.5  # > lease TTL (3 s)
            await asyncio.sleep(4.6)
        else:
            await asyncio.sleep(self.rng.uniform(0.2, 1.5))
        exit_code = 1 if self.rng.random() < P_FAIL else 0
        if a in self.cancelled:
            self.running.pop(a, None)
            self.ledger.events["stopped_stale"] += 1
            return
        await self._commit(a, t, e, exit_code)

    async def _commit(self, a: str, t: str, e: int, exit_code: int) -> None:
        fut = asyncio.get_running_loop().create_future()
        self.acks[a] = fut
        await self._send(pm.Commit(idempotency_key=f"{t}:{a}", task_id=t, attempt_id=a, epoch=e,
                                   exit_code=exit_code))
        ack = await asyncio.wait_for(fut, 10)
        self.running.pop(a, None)
        if ack.accepted:
            if (a, e, exit_code) not in self.ledger.accepted[t]:  # a replayed commit is idempotent
                self.ledger.accepted[t].append((a, e, exit_code))
        else:
            self.ledger.rejected.append((a, ack.reason or ""))

    async def _finish_inflight(self) -> None:
        """Containers kept running while the connection was down; report their results now."""
        for a, (t, e) in list(self.running.items()):
            if a in self.cancelled:
                self.running.pop(a, None)
                self.ledger.events["stopped_stale"] += 1
                continue
            self.ledger.events["finished_after_reconnect"] += 1
            await self._commit(a, t, e, 0)

    async def life(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            reader = hb = None
            try:
                await self._connect()
                reader = asyncio.create_task(self._reader())
                hb = asyncio.create_task(self._heartbeats())
                await self._finish_inflight()
                while not stop.is_set():
                    if reader.done():
                        raise ConnectionError("reader stopped")
                    await self._work_one()
            except (ConnectionError, OSError, TimeoutError, websockets.WebSocketException, AssertionError):
                self.ledger.events["reconnect"] += 1
            finally:
                for task in (reader, hb):
                    if task:
                        task.cancel()
                if self.ws is not None:
                    await self.ws.close()
                for fut in self.acks.values():
                    fut.cancel()
                self.acks.clear()
                self.queue = asyncio.Queue()
            await asyncio.sleep(self.rng.uniform(0.2, 1.0))  # reconnect backoff with jitter


async def test_chaos_invariants(control_plane_factory):
    seed = int(time.time())
    rng = random.Random(seed)
    cp = control_plane_factory(POOL_LEASE_TTL_S="3", POOL_HEARTBEAT_INTERVAL_S="1", POOL_RETRY_BASE_S="0.2",
                               POOL_RETRY_MAX_S="1", POOL_SCHEDULER_INTERVAL_S="0.5", POOL_API_HEARTBEAT_S="0.5",
                               POOL_DECLINE_COOLDOWN_S="1")
    cp.start_api()
    cp.start_scheduler()
    token = await cp.token("chaos-coord")
    headers = {"Authorization": f"Bearer {token}"}
    n_tasks, n_agents = 120, 8
    async with httpx.AsyncClient(base_url=cp.url, headers=headers, timeout=20) as http:
        (await http.post("/api/v1/groups", json={"name": "chaos", "display_name": "Chaos"})).raise_for_status()
        agents = []
        for i in range(n_agents):
            code = (await http.post("/api/v1/devices/enrollment-codes")).json()["code"]
            fa = FakeAgent(cp.url, f"chaos-{i}")
            assert (await fa.enroll(http, code)).status_code == 200
            (await http.put(f"/api/v1/devices/{fa.device_id}/shares/chaos")).raise_for_status()
            agents.append(ChaosAgent(fa, "chaos", Ledger(), random.Random(rng.random())))
        job = {"apiVersion": "pool/v1alpha1", "kind": "Job", "metadata": {"name": "chaos"},
               "spec": {"shape": "array", "image": IMG, "command": ["run", "{{i}}"],
                        "matrix": {"i": list(range(n_tasks))}, "retries": 6, "max_runtime": "5m",
                        "resources": {"cpu": 1, "memory": "256Mi"}}}
        r = await http.post("/api/v1/groups/chaos/jobs", json=job)
        r.raise_for_status()
        job_id = r.json()["id"]

    stop = asyncio.Event()
    lives = [asyncio.create_task(a.life(stop)) for a in agents]
    started = time.monotonic()
    restarts_at = [8.0, 20.0]
    deadline = started + 240
    final = None
    while time.monotonic() < deadline:
        await asyncio.sleep(1)
        elapsed = time.monotonic() - started
        if restarts_at and elapsed > restarts_at[0]:
            restarts_at.pop(0)
            await asyncio.to_thread(cp.restart_api)  # server restart while agents are mid-task
        try:
            token = await cp.token("chaos-coord")
            async with httpx.AsyncClient(base_url=cp.url, timeout=5) as c:
                j = (await c.get(f"/api/v1/jobs/{job_id}", headers={"Authorization": f"Bearer {token}"})).json()
        except httpx.HTTPError:
            continue
        if j["state"] in ("succeeded", "failed", "cancelled") and not restarts_at:
            final = j
            break
    stop.set()
    await asyncio.wait(lives, timeout=15)
    for t in lives:
        t.cancel()

    result = await check(cp.dsn, [job_id])
    chain = await audit_chain_ok(cp.database_url)
    events = defaultdict(int)
    rejected = []
    accepted = defaultdict(list)
    for a in agents:
        for k, v in a.ledger.events.items():
            events[k] += v
        rejected += a.ledger.rejected
        for t, lst in a.ledger.accepted.items():
            accepted[t] += lst
    print(f"\nseed={seed} elapsed={time.monotonic() - started:.1f}s api_restarts={cp.restarts} "
          f"events={dict(events)} stale_commits_rejected={len(rejected)} stats={result['stats']}")
    assert final is not None, f"job did not finish: {result['stats']}"
    assert result["ok"], result["violations"][:10]
    assert chain[0], f"audit chain broken at {chain[1]}"
    # Client view agrees: at most one accepted successful commit per task, from the final epoch.
    async with httpx.AsyncClient(base_url=cp.url, timeout=10) as c:
        token = await cp.token("chaos-coord")
        tasks = (await c.get(f"/api/v1/jobs/{job_id}/tasks", headers={"Authorization": f"Bearer {token}"})).json()
    epoch_of = {t["id"]: (t["state"], t["epoch"]) for t in tasks}
    for task_id, commits in accepted.items():
        ok = [c for c in commits if c[2] == 0]
        assert len(ok) <= 1, f"task {task_id} has {len(ok)} accepted successful commits"
        if ok:
            state, epoch = epoch_of[task_id]
            assert state == "succeeded" and ok[0][1] == epoch
    assert events["partition"] + events["crash"] > 0, "chaos did not happen; adjust probabilities"
