"""Tunnels to apps (docs/design/pool-run.md step 2).

SECURITY-SENSITIVE: opens a path from a person's computer to a program running on a member's computer.
Needs security review (plan §8.10).

An app runs with no network at all. To reach one of its ports:

1. The person who started it connects a WebSocket to /api/v1/jobs/{job}/tunnel?port=P with their token. Only
   that person may (not the group, not the computer's owner), only for a port the app has, and only while it runs.
2. The server picks a one-time tunnel id and secret and sends them to the computer running the app, over its
   signed-in agent connection (message tunnel_open).
3. The agent connects to /api/v1/agent/tunnels/{id}?secret=S and pipes the bytes into the app's container
   (`docker exec ... pool-agent pipe 127.0.0.1:P`).
4. The server copies bytes between the two WebSockets until either side closes.

Both sides connect out to the server, so nothing new listens on anyone's computer. Tunnels are joined inside one
API process (several processes would need sticky routing).

Limits (each connection is a `docker exec` on the owner's computer, outside the share's limits): at most
MAX_TUNNELS_PER_PERSON and MAX_TUNNELS_PER_DEVICE open at once, OPENS_PER_MINUTE new ones per person, and every
open tunnel is checked again every RECHECK_S seconds (the attempt still runs on that computer, the person is
still a member), and closed when it is not. The audit log gets one row per person, app and port every
AUDIT_EVERY_S seconds, with the number of connections.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import secrets
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from fastapi import APIRouter, WebSocket
from sqlalchemy import select

from pool_protocol import messages as pm

from .. import audit
from ..auth import TokenError, upsert_user
from ..models import Attempt, Group, Job, Membership, Task

log = logging.getLogger(__name__)
router = APIRouter(tags=["apps"])

AGENT_ANSWER_S = 15.0
MAX_TUNNELS_PER_PERSON = 32
MAX_TUNNELS_PER_DEVICE = 32  # the agent refuses more too
OPENS_PER_MINUTE = 120  # a browser opens a few connections per page
RECHECK_S = 15.0
AUDIT_EVERY_S = 600.0
MAX_TUNNEL_BYTES = 4 * 1024 * 1024 * 1024
MAX_FRAME = 1024 * 1024
SECRET_HEADER = "x-pool-tunnel-secret"  # noqa: S105 (a header's name)


@dataclass
class _Pending:
    secret: str
    attempt_id: uuid.UUID
    agent: asyncio.Future = field(default_factory=lambda: asyncio.get_running_loop().create_future())
    done: asyncio.Event = field(default_factory=asyncio.Event)


class Tunnels:
    """The tunnels being opened and open in this API process, and the counters behind the limits.

    Counters change only without an await between the check and the change, so tunnels opened at the same moment
    cannot pass a limit together (found in the security review)."""

    def __init__(self) -> None:
        self.pending: dict[str, _Pending] = {}
        self.open: dict[uuid.UUID, int] = {}  # per person
        self.per_device: dict[uuid.UUID, int] = {}
        self.opened: dict[uuid.UUID, deque[float]] = {}  # per person: when each recent tunnel opened
        self.audited: dict[tuple[uuid.UUID, uuid.UUID, int], tuple[float, int]] = {}  # last row, connections since

    def take(self, user: uuid.UUID, device: uuid.UUID) -> str | None:
        """Count one more tunnel for this person and computer, or say why not. No await inside."""
        now = time.monotonic()
        recent = self.opened.setdefault(user, deque())
        while recent and recent[0] < now - 60:
            recent.popleft()
        if len(recent) >= OPENS_PER_MINUTE:
            return f"at most {OPENS_PER_MINUTE} new connections a minute"
        if self.open.get(user, 0) >= MAX_TUNNELS_PER_PERSON:
            return f"at most {MAX_TUNNELS_PER_PERSON} connections at once"
        if self.per_device.get(device, 0) >= MAX_TUNNELS_PER_DEVICE:
            return "the computer running the app has too many connections open"
        recent.append(now)
        self.open[user] = self.open.get(user, 0) + 1
        self.per_device[device] = self.per_device.get(device, 0) + 1
        return None

    def give_back(self, user: uuid.UUID, device: uuid.UUID) -> None:
        for counts, key in ((self.open, user), (self.per_device, device)):
            left = counts.get(key, 1) - 1
            if left > 0:
                counts[key] = left
            else:
                counts.pop(key, None)
        if not self.open.get(user) and not self.opened.get(user):
            self.opened.pop(user, None)

    def audit_due(self, user: uuid.UUID, job: uuid.UUID, port: int) -> int:
        """0 if this connection is counted in a later audit row, else the connections the row stands for."""
        now = time.monotonic()
        key = (user, job, port)
        last, since = self.audited.get(key, (0.0, 0))
        if last and now - last < AUDIT_EVERY_S:
            self.audited[key] = (last, since + 1)
            return 0
        if len(self.audited) > 10_000:  # forget old entries
            self.audited = {k: v for k, v in self.audited.items() if now - v[0] < AUDIT_EVERY_S}
        self.audited[key] = (now, 0)
        return since + 1


def _tunnels(ws: WebSocket) -> Tunnels:
    state = ws.app.state
    if not hasattr(state, "tunnels"):
        state.tunnels = Tunnels()
    return state.tunnels


async def _refuse(ws: WebSocket, code: int, reason: str) -> None:
    await ws.close(code=code, reason=reason[:120])


async def _running_attempt(s, job: Job) -> Attempt | None:  # noqa: ANN001
    return (await s.execute(
        select(Attempt).join(Task, Task.current_attempt_id == Attempt.id)
        .where(Task.job_id == job.id, Attempt.state == "running"))).scalars().first()


@router.websocket("/api/v1/jobs/{job_id}/tunnel")
async def user_tunnel(ws: WebSocket, job_id: uuid.UUID, port: int) -> None:
    """One connection from the person's computer to an app's port."""
    app = ws.app
    scheme, _, token = ws.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        return await _refuse(ws, 4401, "sign in first: pool login")
    try:
        claims = app.state.token_verifier.verify(token.strip())
    except TokenError:
        return await _refuse(ws, 4401, "your sign-in is not valid or has expired: pool login")
    tunnels = _tunnels(ws)
    taken: tuple[uuid.UUID, uuid.UUID] | None = None  # (person, computer) once counted; given back at the end
    pending: _Pending | None = None
    tunnel_id = secrets.token_urlsafe(12)
    try:
        async with app.state.sessionmaker() as s:
            user = await upsert_user(s, claims, app.state.settings.oidc_username_claim)
            job = await s.get(Job, job_id)
            member = job and (await s.execute(select(Membership).where(
                Membership.group_id == job.group_id, Membership.user_id == user.id))).scalar_one_or_none()
            group = job and await s.get(Group, job.group_id)
            if not job or not member or not group or group.state == "deleted":
                return await _refuse(ws, 4404, "no such job")
            if job.submitter_id != user.id:
                return await _refuse(ws, 4403, "only the person who started an app can open it")
            if port not in (job.ports or []):
                return await _refuse(ws, 4404, f"the app has no port {port}")
            row = await _running_attempt(s, job)
            if row is None:
                return await _refuse(ws, 4409, "the app is not running right now")
            conn = app.state.agent_conns.get(row.device_id)
            if conn is None:
                return await _refuse(ws, 4409, "the computer running the app is not connected right now")
            attempt_id, device_id, user_id = row.id, row.device_id, user.id
            if (why := tunnels.take(user_id, device_id)) is not None:  # checked and counted with no await between
                return await _refuse(ws, 4429, why)
            taken = (user_id, device_id)
            if count := tunnels.audit_due(user_id, job.id, port):
                await audit.record(s, actor_type="user", actor_id=user_id, action="app.tunnel",
                                   group_id=job.group_id, device_id=device_id, target_type="job", target_id=job.id,
                                   details={"port": port, "attempt_id": str(attempt_id), "connections": count})
                await s.commit()
        pending = _Pending(secret=secrets.token_urlsafe(32), attempt_id=attempt_id)
        tunnels.pending[tunnel_id] = pending
        await conn.send(pm.TunnelOpen(tunnel_id=tunnel_id, attempt_id=str(attempt_id), port=port,
                                      secret=pending.secret))
        try:
            agent_ws = await asyncio.wait_for(asyncio.shield(pending.agent), AGENT_ANSWER_S)
        except TimeoutError:
            return await _refuse(ws, 4504, "the computer running the app did not answer")
        await ws.accept()

        async def still_allowed() -> bool:
            """The attempt still runs on the same connected computer, and the person may still open it."""
            if app.state.agent_conns.get(device_id) is not conn:
                return False
            async with app.state.sessionmaker() as s2:
                ok = (await s2.execute(
                    select(Attempt.id).join(Task, Task.current_attempt_id == Attempt.id)
                    .join(Job, Job.id == Task.job_id).join(Group, Group.id == Job.group_id)
                    .join(Membership, (Membership.group_id == Job.group_id) & (Membership.user_id == user_id))
                    .where(Attempt.id == attempt_id, Attempt.state == "running", Attempt.device_id == device_id,
                           Group.state != "deleted"))).first()
            return ok is not None

        sent = await _relay(ws, agent_ws, still_allowed)
        log.info("tunnel job=%s port=%s device=%s bytes=%s", job_id, port, device_id, sent)
    finally:
        if pending is not None:
            tunnels.pending.pop(tunnel_id, None)
            pending.done.set()
        if taken is not None:
            tunnels.give_back(*taken)


@router.websocket("/api/v1/agent/tunnels/{tunnel_id}")
async def agent_tunnel(ws: WebSocket, tunnel_id: str) -> None:
    """The computer's side of one tunnel: proven by the one-time secret it got over its agent connection. The
    secret comes in a header: URLs end up in logs."""
    tunnels = _tunnels(ws)
    secret = ws.headers.get(SECRET_HEADER, "")
    pending = tunnels.pending.get(tunnel_id)
    if pending is None or not hmac.compare_digest(pending.secret.encode(), secret.encode()):
        return await _refuse(ws, 4403, "unknown tunnel")  # a wrong secret does not use the tunnel up
    if tunnels.pending.pop(tunnel_id, None) is not pending:  # one use only
        return await _refuse(ws, 4403, "unknown tunnel")
    await ws.accept()
    if pending.agent.done():
        return await ws.close()
    pending.agent.set_result(ws)
    await pending.done.wait()  # the person's side copies the bytes; this side stays open until it ends


async def _relay(a: WebSocket, b: WebSocket, still_allowed: Callable[[], Awaitable[bool]]) -> int:
    """Copy binary frames both ways until either side ends, or the tunnel is no longer allowed. Returns the
    bytes copied."""
    copied = [0]

    async def pump(src: WebSocket, dst: WebSocket) -> None:
        while True:
            msg = await src.receive()
            if msg["type"] == "websocket.disconnect":
                return
            data = msg.get("bytes")
            if not data:
                continue
            if len(data) > MAX_FRAME:
                return
            copied[0] += len(data)
            if copied[0] > MAX_TUNNEL_BYTES:
                return
            await dst.send_bytes(data)

    async def watch() -> None:
        while True:
            await asyncio.sleep(RECHECK_S)
            try:
                if not await still_allowed():
                    return
            except Exception:  # noqa: BLE001 (cannot check: close rather than keep it open unchecked)
                log.exception("tunnel check failed")
                return

    tasks = [asyncio.create_task(pump(a, b)), asyncio.create_task(pump(b, a)), asyncio.create_task(watch())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        for side in (a, b):
            try:
                await side.close()
            except Exception:  # noqa: BLE001, S110 (already closed)
                pass
    return copied[0]
