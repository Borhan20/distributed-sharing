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
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import secrets
import uuid
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
MAX_TUNNEL_BYTES = 4 * 1024 * 1024 * 1024
MAX_FRAME = 1024 * 1024


@dataclass
class _Pending:
    secret: str
    attempt_id: uuid.UUID
    agent: asyncio.Future = field(default_factory=lambda: asyncio.get_running_loop().create_future())
    done: asyncio.Event = field(default_factory=asyncio.Event)


class Tunnels:
    """The tunnels being opened and the number each person has open (one API process)."""

    def __init__(self) -> None:
        self.pending: dict[str, _Pending] = {}
        self.open: dict[uuid.UUID, int] = {}


def _tunnels(ws: WebSocket) -> Tunnels:
    state = ws.app.state
    if not hasattr(state, "tunnels"):
        state.tunnels = Tunnels()
    return state.tunnels


async def _refuse(ws: WebSocket, code: int, reason: str) -> None:
    await ws.close(code=code, reason=reason[:120])


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
        row = (await s.execute(
            select(Attempt).join(Task, Task.current_attempt_id == Attempt.id)
            .where(Task.job_id == job.id, Attempt.state == "running"))).scalars().first()
        if row is None:
            return await _refuse(ws, 4409, "the app is not running right now")
        conn = app.state.agent_conns.get(row.device_id)
        if conn is None:
            return await _refuse(ws, 4409, "the computer running the app is not connected right now")
        if tunnels.open.get(user.id, 0) >= MAX_TUNNELS_PER_PERSON:
            return await _refuse(ws, 4429, f"at most {MAX_TUNNELS_PER_PERSON} connections at once")
        await audit.record(s, actor_type="user", actor_id=user.id, action="app.tunnel", group_id=job.group_id,
                           device_id=row.device_id, target_type="job", target_id=job.id,
                           details={"port": port, "attempt_id": str(row.id)})
        await s.commit()
        attempt_id, device_id = row.id, row.device_id
    tunnel_id, secret = secrets.token_urlsafe(12), secrets.token_urlsafe(32)
    pending = _Pending(secret=secret, attempt_id=attempt_id)
    tunnels.pending[tunnel_id] = pending
    tunnels.open[user.id] = tunnels.open.get(user.id, 0) + 1
    try:
        await conn.send(pm.TunnelOpen(tunnel_id=tunnel_id, attempt_id=str(attempt_id), port=port, secret=secret))
        try:
            agent_ws = await asyncio.wait_for(asyncio.shield(pending.agent), AGENT_ANSWER_S)
        except TimeoutError:
            return await _refuse(ws, 4504, "the computer running the app did not answer")
        await ws.accept()
        sent = await _relay(ws, agent_ws)
        log.info("tunnel job=%s port=%s device=%s bytes=%s", job_id, port, device_id, sent)
    finally:
        tunnels.pending.pop(tunnel_id, None)
        tunnels.open[user.id] = tunnels.open.get(user.id, 1) - 1
        pending.done.set()


@router.websocket("/api/v1/agent/tunnels/{tunnel_id}")
async def agent_tunnel(ws: WebSocket, tunnel_id: str, secret: str = "") -> None:
    """The computer's side of one tunnel: proven by the one-time secret it got over its agent connection."""
    tunnels = _tunnels(ws)
    pending = tunnels.pending.pop(tunnel_id, None)  # one use only
    if pending is None or not hmac.compare_digest(pending.secret.encode(), secret.encode()):
        return await _refuse(ws, 4403, "unknown tunnel")
    await ws.accept()
    if pending.agent.done():
        return await ws.close()
    pending.agent.set_result(ws)
    await pending.done.wait()  # the person's side copies the bytes; this side stays open until it ends


async def _relay(a: WebSocket, b: WebSocket) -> int:
    """Copy binary frames both ways until either side ends. Returns the bytes copied."""
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

    tasks = [asyncio.create_task(pump(a, b)), asyncio.create_task(pump(b, a))]
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
