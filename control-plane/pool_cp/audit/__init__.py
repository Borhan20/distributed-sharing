"""Append-only, hash-chained audit log (plan §8.9).

SECURITY-SENSITIVE: needs security review before merge (plan §8.10).

Each row stores the hash of the previous row. Inserts take a transaction-level
advisory lock so the chain stays linear across concurrent requests; a database
trigger (see the initial migration) rejects UPDATE and DELETE on the table.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import AuditEvent
from ..timeutil import utcnow

GENESIS = "0" * 64
_LOCK_KEY = 0x706F6F6C  # "pool"


def _canonical(ev: dict[str, Any]) -> bytes:
    return json.dumps(ev, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode()


def compute_hash(prev_hash: str, ts: datetime, actor_type: str, actor_id: str | None, action: str,
                 group_id: uuid.UUID | None, device_id: uuid.UUID | None, target_type: str | None,
                 target_id: str | None, details: dict[str, Any]) -> str:
    body = {
        "prev": prev_hash,
        "ts": ts.isoformat(timespec="microseconds"),
        "actor_type": actor_type,
        "actor_id": actor_id,
        "action": action,
        "group_id": str(group_id) if group_id else None,
        "device_id": str(device_id) if device_id else None,
        "target_type": target_type,
        "target_id": target_id,
        "details": details,
    }
    return hashlib.sha256(_canonical(body)).hexdigest()


async def record(
    session: AsyncSession,
    *,
    actor_type: str,
    actor_id: str | uuid.UUID | None,
    action: str,
    group_id: uuid.UUID | None = None,
    device_id: uuid.UUID | None = None,
    target_type: str | None = None,
    target_id: str | uuid.UUID | None = None,
    details: dict[str, Any] | None = None,
) -> AuditEvent:
    """Append one event in the caller's transaction (it commits or rolls back with it)."""
    await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _LOCK_KEY})
    prev = (await session.execute(select(AuditEvent.hash).order_by(AuditEvent.id.desc()).limit(1))).scalar()
    prev = prev or GENESIS
    ts = utcnow()  # microsecond precision, same as PostgreSQL timestamptz
    details = json.loads(_canonical(details or {}))  # normalise to JSON types before hashing
    actor = str(actor_id) if actor_id is not None else None
    target = str(target_id) if target_id is not None else None
    h = compute_hash(prev, ts, actor_type, actor, action, group_id, device_id, target_type, target, details)
    ev = AuditEvent(ts=ts, actor_type=actor_type, actor_id=actor, action=action, group_id=group_id,
                    device_id=device_id, target_type=target_type, target_id=target, details=details,
                    prev_hash=prev, hash=h)
    session.add(ev)
    await session.flush()
    return ev


async def verify_chain(session: AsyncSession, batch: int = 5000) -> tuple[bool, int | None, int]:
    """Recompute the whole chain. Returns (ok, first_bad_id, rows_checked)."""
    prev = GENESIS
    last_id = 0
    checked = 0
    while True:
        rows = (await session.execute(
            select(AuditEvent).where(AuditEvent.id > last_id).order_by(AuditEvent.id).limit(batch)
        )).scalars().all()
        if not rows:
            return True, None, checked
        for ev in rows:
            expected = compute_hash(prev, ev.ts, ev.actor_type, ev.actor_id, ev.action, ev.group_id,
                                    ev.device_id, ev.target_type, ev.target_id, ev.details)
            if ev.prev_hash != prev or ev.hash != expected:
                return False, ev.id, checked
            prev = ev.hash
            last_id = ev.id
            checked += 1
