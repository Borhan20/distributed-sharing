"""Push stop signals to connected agents.

Correctness does not depend on this: an attempt that is no longer current is
reported as `stale` in the next heartbeat_ack and the agent stops it. Pushing
just makes it faster when the agent is connected to this API process.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable

from fastapi import FastAPI

from pool_protocol import messages as pm

log = logging.getLogger(__name__)


async def notify_stops(app: FastAPI, stops: Iterable[tuple[str, uuid.UUID]], reason: str, *, group: str | None = None,
                       revoke_devices: Iterable[uuid.UUID] | None = None) -> None:
    conns = getattr(app.state, "agent_conns", {})
    for attempt_id, device_id in stops:
        conn = conns.get(device_id)
        if conn is not None:
            await conn.send(pm.Cancel(attempt_id=attempt_id, reason=reason))
    if group and revoke_devices:
        for device_id in revoke_devices:
            conn = conns.get(device_id)
            if conn is not None:
                await conn.send(pm.Revoke(group=group, reason=reason))


async def disconnect_device(app: FastAPI, device_id: uuid.UUID, reason: str) -> None:
    conn = getattr(app.state, "agent_conns", {}).get(device_id)
    if conn is not None:
        await conn.close(reason)
