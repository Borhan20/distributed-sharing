"""Devices: enrollment, revocation, sharing and the "what ran here" view (plan §6.4, §8.6, §8.9)."""

from __future__ import annotations

import secrets
import time
import uuid
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pool_protocol import messages as pm

from .. import audit
from ..auth import Principal, current_principal, get_session
from ..authz import ANY_MEMBER, MFA_NEEDED, SHARERS, load_group, not_found, require_role
from ..ca import CAError, LocalCA, sha256_hex
from ..models import Attempt, Device, DeviceShare, EnrollmentCode, Group, Job, RevokedCert, Task, User
from ..scheduler.core import ACTIVE_ATTEMPT_STATES, lose_attempt
from ..timeutil import utcnow
from .notify import disconnect_device, notify_stops

router = APIRouter(prefix="/api/v1", tags=["devices"])

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I


def _normalise_code(code: str) -> str:
    return code.replace("-", "").replace(" ", "").upper()


def online(d: Device, heartbeat_s: int) -> bool:
    return bool(d.last_seen_at and d.last_seen_at > utcnow() - timedelta(seconds=heartbeat_s * 3))


class EnrollmentCodeOut(BaseModel):
    code: str
    expires_at: datetime


@router.post("/devices/enrollment-codes", response_model=EnrollmentCodeOut, status_code=201)
async def create_enrollment_code(request: Request, p: Principal = Depends(current_principal),
                                 s: AsyncSession = Depends(get_session)):
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(10))  # ~50 bits, valid 10 minutes, single use
    code = f"{raw[:5]}-{raw[5:]}"
    expires = utcnow() + timedelta(seconds=request.app.state.settings.enrollment_code_ttl_s)
    s.add(EnrollmentCode(code_hash=sha256_hex(raw), owner_id=p.user.id, expires_at=expires))
    await audit.record(s, actor_type="user", actor_id=p.user.id, action="device.enrollment_code",
                       details={"expires_at": expires})
    await s.commit()
    return EnrollmentCodeOut(code=code, expires_at=expires)


class _RateLimiter:
    """Small in-process limiter for unauthenticated endpoints (per client address)."""

    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self.hits: dict[str, list[float]] = defaultdict(list)

    def check(self, key: str) -> None:
        now = time.monotonic()
        hits = [t for t in self.hits[key] if now - t < 60]
        if len(hits) >= self.per_minute:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many enrollment attempts")
        hits.append(now)
        self.hits[key] = hits



@router.post("/agent/enroll", response_model=pm.EnrollResponse, tags=["agent"])
async def enroll(body: pm.EnrollRequest, request: Request, s: AsyncSession = Depends(get_session)):
    limiter = request.app.state.__dict__.setdefault(
        "_enroll_limiter", _RateLimiter(request.app.state.settings.enroll_rate_per_minute))
    limiter.check(request.client.host if request.client else "unknown")
    ca: LocalCA = request.app.state.ca
    try:
        public_key = ca.csr_public_key(body.csr_pem)
    except CAError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None
    code = (await s.execute(select(EnrollmentCode).where(
        EnrollmentCode.code_hash == sha256_hex(_normalise_code(body.one_time_code))).with_for_update()
    )).scalar_one_or_none()
    if code is None or code.used_at is not None or code.expires_at < utcnow():
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "enrollment code is invalid, used or expired")
    device_id = uuid.uuid4()
    cert_pem, serial, not_after = ca.issue(public_key, str(device_id))
    f = body.device_facts
    s.add(Device(id=device_id, owner_id=code.owner_id, name=f.name, os=f.os, arch=f.arch,
                 agent_version=f.agent_version, public_key=public_key, cert_serial=serial, cert_not_after=not_after))
    code.used_at = utcnow()
    code.device_id = device_id
    await s.flush()
    await audit.record(s, actor_type="user", actor_id=code.owner_id, action="device.enroll", device_id=device_id,
                       target_type="device", target_id=device_id,
                       details={"name": f.name, "os": f.os, "arch": f.arch, "cert_serial": serial})
    await s.commit()
    signer = request.app.state.signer
    return pm.EnrollResponse(device_id=str(device_id), cert_pem=cert_pem, ca_cert_pem=ca.cert_pem,
                             cert_not_after=not_after,
                             signing_keys=[pm.SigningKey(key_id=signer.key_id, public_key_b64=signer.public_key_b64)])


class DeviceOut(BaseModel):
    id: uuid.UUID
    name: str
    owner: str
    os: str
    arch: str
    agent_version: str
    state: str
    online: bool
    owner_active: bool
    always_on: bool
    org_owned_dedicated: bool
    reliability: float
    capabilities: dict[str, Any]
    reported_shares: list[dict[str, Any]]
    policy_digest: str
    shared_with: list[str]
    turned_off: list[str] = Field(description="Groups the owner switched off on the server")
    cert_not_after: datetime
    last_seen_at: datetime | None


async def _device_out(s: AsyncSession, d: Device, hb: int) -> DeviceOut:
    owner = await s.get(User, d.owner_id)
    rows = (await s.execute(select(Group.name, DeviceShare.enabled).join(DeviceShare, DeviceShare.group_id == Group.id)
                            .where(DeviceShare.device_id == d.id))).all()
    shared = [name for name, enabled in rows if enabled]
    turned_off = sorted(name for name, enabled in rows if not enabled)
    return DeviceOut(id=d.id, name=d.name, owner=owner.handle, os=d.os, arch=d.arch, agent_version=d.agent_version,
                     state=d.state, online=online(d, hb), owner_active=d.owner_active, always_on=d.always_on,
                     org_owned_dedicated=d.org_owned_dedicated, reliability=round(d.reliability, 3),
                     capabilities=d.capabilities or {}, reported_shares=d.reported_shares or [],
                     policy_digest=d.policy_digest, shared_with=sorted(shared), turned_off=turned_off,
                     cert_not_after=d.cert_not_after,
                     last_seen_at=d.last_seen_at)


async def _own_device(s: AsyncSession, device_id: uuid.UUID, p: Principal) -> Device:
    d = await s.get(Device, device_id)
    if d is None or d.owner_id != p.user.id:
        raise not_found("device")
    return d


@router.get("/devices", response_model=list[DeviceOut])
async def list_devices(request: Request, p: Principal = Depends(current_principal),
                       s: AsyncSession = Depends(get_session)):
    rows = (await s.execute(select(Device).where(Device.owner_id == p.user.id).order_by(Device.created_at))).scalars()
    hb = request.app.state.settings.heartbeat_interval_s
    return [await _device_out(s, d, hb) for d in rows]


@router.get("/devices/{device_id}", response_model=DeviceOut)
async def get_device(device_id: uuid.UUID, request: Request, p: Principal = Depends(current_principal),
                     s: AsyncSession = Depends(get_session)):
    d = await _own_device(s, device_id, p)
    return await _device_out(s, d, request.app.state.settings.heartbeat_interval_s)


class DevicePatch(BaseModel):
    always_on: bool | None = None
    org_owned_dedicated: bool | None = None


@router.patch("/devices/{device_id}", response_model=DeviceOut)
async def patch_device(device_id: uuid.UUID, body: DevicePatch, request: Request,
                       p: Principal = Depends(current_principal), s: AsyncSession = Depends(get_session)):
    settings = request.app.state.settings
    is_admin = p.user.handle in settings.server_admins
    d = await s.get(Device, device_id)
    if d is None or (d.owner_id != p.user.id and not is_admin):
        raise not_found("device")
    if body.org_owned_dedicated is not None:
        # Confidential data may only run on these devices (plan §8.8), so owners cannot set it themselves.
        if not is_admin:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "only server admins can mark devices org-owned")
        if settings.require_mfa_for_admins and not p.has_mfa:
            raise HTTPException(status.HTTP_403_FORBIDDEN, MFA_NEEDED)
        d.org_owned_dedicated = body.org_owned_dedicated
    if body.always_on is not None:
        if d.owner_id != p.user.id:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "only the owner can change this")
        d.always_on = body.always_on
    await audit.record(s, actor_type="user", actor_id=p.user.id, action="device.update", device_id=d.id,
                       target_type="device", target_id=d.id, details=body.model_dump(exclude_unset=True))
    await s.commit()
    return await _device_out(s, d, settings.heartbeat_interval_s)


@router.post("/devices/{device_id}/revoke", response_model=DeviceOut)
async def revoke_device(device_id: uuid.UUID, request: Request, p: Principal = Depends(current_principal),
                        s: AsyncSession = Depends(get_session)):
    """Stolen laptop (threat #4): deny-list the certificate now and requeue its work."""
    d = await _own_device(s, device_id, p)
    if d.state == "revoked":
        return await _device_out(s, d, request.app.state.settings.heartbeat_interval_s)
    d.state = "revoked"
    d.revoked_at = utcnow()
    s.add(RevokedCert(serial=d.cert_serial, device_id=d.id))
    active = (await s.execute(select(Attempt).where(Attempt.device_id == d.id,
                                                    Attempt.state.in_(ACTIVE_ATTEMPT_STATES)))).scalars().all()
    for a in active:
        await lose_attempt(s, request.app.state.settings, a, reason="device_revoked")
    await audit.record(s, actor_type="user", actor_id=p.user.id, action="device.revoke", device_id=d.id,
                       target_type="device", target_id=d.id,
                       details={"cert_serial": d.cert_serial, "requeued_attempts": len(active)})
    await s.commit()
    await disconnect_device(request.app, d.id, "device_revoked")
    return await _device_out(s, d, request.app.state.settings.heartbeat_interval_s)


@router.put("/devices/{device_id}/shares/{group_name}", response_model=DeviceOut)
async def share_device(device_id: uuid.UUID, group_name: str, request: Request,
                       p: Principal = Depends(current_principal), s: AsyncSession = Depends(get_session)):
    d = await _own_device(s, device_id, p)
    if d.state != "active":
        raise HTTPException(status.HTTP_409_CONFLICT, "device is revoked")
    g = await load_group(s, group_name)
    await require_role(s, g, p, SHARERS)
    share = await s.get(DeviceShare, (d.id, g.id))
    if share is None:
        s.add(DeviceShare(device_id=d.id, group_id=g.id, enabled=True))
    else:
        share.enabled = True
    await audit.record(s, actor_type="user", actor_id=p.user.id, action="device.share", group_id=g.id,
                       device_id=d.id, target_type="device", target_id=d.id)
    await s.commit()
    return await _device_out(s, d, request.app.state.settings.heartbeat_interval_s)


@router.delete("/devices/{device_id}/shares/{group_name}", response_model=DeviceOut)
async def unshare_device(device_id: uuid.UUID, group_name: str, request: Request,
                         p: Principal = Depends(current_principal), s: AsyncSession = Depends(get_session)):
    d = await _own_device(s, device_id, p)
    g = (await s.execute(select(Group).where(Group.name == group_name))).scalar_one_or_none()
    share = await s.get(DeviceShare, (d.id, g.id)) if g else None
    if share is None or not share.enabled:
        raise not_found("share")
    # Keep the row, switched off: the agent's local policy may still offer this group, and
    # sync_shares_from_policy only creates missing rows, so the owner's "off" here stays off.
    share.enabled = False
    stops = []
    active = (await s.execute(
        select(Attempt).join(Task, Task.id == Attempt.task_id)
        .where(Attempt.device_id == d.id, Attempt.state.in_(ACTIVE_ATTEMPT_STATES), Task.group_id == g.id)
    )).scalars().all()
    for a in active:
        stops.append((str(a.id), d.id))
        await lose_attempt(s, request.app.state.settings, a, reason="device_unshared")
    await audit.record(s, actor_type="user", actor_id=p.user.id, action="device.unshare", group_id=g.id,
                       device_id=d.id, target_type="device", target_id=d.id, details={"requeued": len(stops)})
    await s.commit()
    await notify_stops(request.app, stops, "device_unshared", group=g.name, revoke_devices=[d.id])
    return await _device_out(s, d, request.app.state.settings.heartbeat_interval_s)


class GroupDeviceOut(BaseModel):
    id: uuid.UUID
    name: str
    owner: str
    os: str
    online: bool
    active_now: bool
    tiers: list[str]
    cpu_cores: int | None
    memory_mb: int | None
    gpus: int
    always_on: bool


@router.get("/groups/{name}/devices", response_model=list[GroupDeviceOut])
async def group_devices(name: str, request: Request, p: Principal = Depends(current_principal),
                        s: AsyncSession = Depends(get_session)):
    g = await load_group(s, name)
    await require_role(s, g, p, ANY_MEMBER, require_active=False)
    rows = (await s.execute(
        select(Device, User.handle).join(DeviceShare, DeviceShare.device_id == Device.id)
        .join(User, User.id == Device.owner_id)
        .where(DeviceShare.group_id == g.id, DeviceShare.enabled.is_(True), Device.state == "active")
        .order_by(Device.name)
    )).all()
    hb = request.app.state.settings.heartbeat_interval_s
    out = []
    for d, owner in rows:
        caps = d.capabilities or {}
        share = next((x for x in d.reported_shares or [] if x.get("group") == g.name), None)
        out.append(GroupDeviceOut(id=d.id, name=d.name, owner=owner, os=d.os, online=online(d, hb),
                                  active_now=bool(share and share.get("active_now")),
                                  tiers=caps.get("tiers", []), cpu_cores=caps.get("cpu_cores"),
                                  memory_mb=caps.get("memory_mb"), gpus=len(caps.get("gpus") or []),
                                  always_on=d.always_on))
    return out


class HistoryRow(BaseModel):
    attempt_id: uuid.UUID
    state: str
    end_reason: str | None
    tier: str | None
    group: str
    submitter: str
    job_id: uuid.UUID
    job_name: str
    task_index: int
    image: str
    command: list[str]
    created_at: datetime
    started_at: datetime | None
    ended_at: datetime | None
    exit_code: int | None


@router.get("/devices/{device_id}/history", response_model=list[HistoryRow])
async def device_history(device_id: uuid.UUID, limit: int = 200, p: Principal = Depends(current_principal),
                         s: AsyncSession = Depends(get_session)):
    """What ran here (plan §8.1 principle 5): every attempt on this device and who sent it."""
    d = await _own_device(s, device_id, p)
    rows = (await s.execute(
        select(Attempt, Task, Job, Group.name, User.handle)
        .join(Task, Task.id == Attempt.task_id).join(Job, Job.id == Task.job_id)
        .join(Group, Group.id == Job.group_id).join(User, User.id == Job.submitter_id)
        .where(Attempt.device_id == d.id).order_by(Attempt.created_at.desc()).limit(min(limit, 1000))
    )).all()
    return [HistoryRow(attempt_id=a.id, state=a.state, end_reason=a.end_reason, tier=a.tier, group=gname,
                       submitter=handle, job_id=j.id, job_name=j.name, task_index=t.index, image=t.spec["image"],
                       command=t.spec["command"], created_at=a.created_at, started_at=a.started_at,
                       ended_at=a.ended_at, exit_code=a.exit_code) for a, t, j, gname, handle in rows]


class DeviceShareIn(BaseModel):
    enabled: bool = Field(default=True)
