"""Groups, members, invites, join requests and approvals (plan §6.2-6.3)."""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .. import audit
from ..auth import Principal, current_principal, get_session
from ..authz import (
    ANY_MEMBER,
    COORDINATOR,
    MANAGERS,
    MFA_NEEDED,
    VOTERS,
    load_group,
    membership,
    not_found,
    require_role,
)
from ..ca import random_token, sha256_hex
from ..models import Approval, Device, DeviceShare, Group, Invite, JoinRequest, Membership, Task, User
from ..scheduler.core import cancel_tasks
from ..timeutil import utcnow
from .notify import notify_stops

router = APIRouter(prefix="/api/v1", tags=["groups"])

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
Role = Literal["coordinator", "admin", "member", "contributor", "submitter"]
Rule = Literal["coordinator-only", "any-k-members", "majority"]

# Plan §6.3 item 6: templates with sensible defaults.
TEMPLATES: dict[str, dict] = {
    "research-team": {"approval_rule": "coordinator-only", "approval_k": 1, "probation_days": 0,
                      "retention_days": 90, "expires_in_days": None},
    "dev-team": {"approval_rule": "coordinator-only", "approval_k": 1, "probation_days": 0,
                 "retention_days": 30, "expires_in_days": None},
    "student-group": {"approval_rule": "any-k-members", "approval_k": 2, "probation_days": 7,
                      "retention_days": 30, "expires_in_days": None},
    "course": {"approval_rule": "coordinator-only", "approval_k": 1, "probation_days": 0,
               "retention_days": 30, "expires_in_days": 120},
    "hackathon": {"approval_rule": "any-k-members", "approval_k": 1, "probation_days": 0,
                  "retention_days": 7, "expires_in_days": 2},
}


class GroupCreate(BaseModel):
    name: str
    display_name: str = Field(min_length=1, max_length=128)
    template: Literal["research-team", "dev-team", "student-group", "course", "hackathon"] | None = None
    approval_rule: Rule | None = None
    approval_k: int | None = Field(default=None, ge=1, le=100)
    probation_days: int | None = Field(default=None, ge=0, le=365)
    retention_days: int | None = Field(default=None, ge=1, le=3650)
    expires_at: datetime | None = None
    registry_allowlist: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not NAME_RE.match(v):
            raise ValueError("name must match ^[a-z0-9][a-z0-9-]{1,62}$")
        return v


class GroupUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=128)
    approval_rule: Rule | None = None
    approval_k: int | None = Field(default=None, ge=1, le=100)
    probation_days: int | None = Field(default=None, ge=0, le=365)
    retention_days: int | None = Field(default=None, ge=1, le=3650)
    expires_at: datetime | None = None
    registry_allowlist: list[str] | None = Field(default=None, max_length=50)
    # Days the built-in registry keeps an image nobody uses; null: the server's default.
    registry_retention_days: int | None = Field(default=None, ge=1, le=3650)
    # The time limit of a task on its first run, when nobody set one (docs/design/pool-decides.md §10).
    default_time_limit_s: int | None = Field(default=None, ge=60, le=7 * 24 * 3600)


class GroupOut(BaseModel):
    id: uuid.UUID
    name: str
    display_name: str
    template: str | None
    approval_rule: str
    approval_k: int
    probation_days: int
    retention_days: int
    expires_at: datetime | None
    registry_allowlist: list[str]
    registry_retention_days: int | None = None
    default_time_limit_s: int = 14400
    state: str
    created_at: datetime
    my_role: str | None = None


def _group_out(g: Group, role: str | None) -> GroupOut:
    return GroupOut(id=g.id, name=g.name, display_name=g.display_name, template=g.template,
                    approval_rule=g.approval_rule, approval_k=g.approval_k, probation_days=g.probation_days,
                    retention_days=g.retention_days, expires_at=g.expires_at,
                    registry_allowlist=list(g.registry_allowlist or []),
                    registry_retention_days=g.registry_retention_days,
                    default_time_limit_s=g.default_time_limit_s or 14400, state=g.state, created_at=g.created_at,
                    my_role=role)


@router.get("/me")
async def me(request: Request, p: Principal = Depends(current_principal)) -> dict:
    settings = request.app.state.settings
    admin = p.user.handle in settings.server_admins
    return {"id": str(p.user.id), "handle": p.user.handle, "mfa": p.has_mfa, "server_admin": admin,
            "can_create_groups": admin or settings.group_creators == "anyone",
            # Coordinators and admins need two-step sign-in for privileged actions on this server.
            "mfa_required": bool(settings.require_mfa_for_admins)}


@router.get("/groups", response_model=list[GroupOut])
async def list_groups(p: Principal = Depends(current_principal), s: AsyncSession = Depends(get_session)):
    rows = (await s.execute(
        select(Group, Membership.role).join(Membership, Membership.group_id == Group.id)
        .where(Membership.user_id == p.user.id, Group.state != "deleted").order_by(Group.name)
    )).all()
    return [_group_out(g, r) for g, r in rows]


@router.post("/groups", response_model=GroupOut, status_code=201)
async def create_group(body: GroupCreate, request: Request, p: Principal = Depends(current_principal),
                       s: AsyncSession = Depends(get_session)):
    settings = request.app.state.settings
    if settings.group_creators == "admins" and p.user.handle not in settings.server_admins:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "On this server only server admins create groups. Ask one to "
                                                       "create the group and invite you as its coordinator.")
    defaults = TEMPLATES.get(body.template or "research-team")
    expires_at = body.expires_at
    if expires_at is None and defaults["expires_in_days"]:
        expires_at = utcnow() + timedelta(days=defaults["expires_in_days"])
    g = Group(name=body.name, display_name=body.display_name, template=body.template,
              approval_rule=body.approval_rule or defaults["approval_rule"],
              approval_k=body.approval_k or defaults["approval_k"],
              probation_days=body.probation_days if body.probation_days is not None else defaults["probation_days"],
              retention_days=body.retention_days or defaults["retention_days"], expires_at=expires_at,
              registry_allowlist=body.registry_allowlist)
    s.add(g)
    try:
        await s.flush()
    except IntegrityError:
        await s.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "a group with this name already exists") from None
    s.add(Membership(group_id=g.id, user_id=p.user.id, role="coordinator"))
    await audit.record(s, actor_type="user", actor_id=p.user.id, action="group.create", group_id=g.id,
                       target_type="group", target_id=g.id,
                       details={"name": g.name, "template": g.template, "approval_rule": g.approval_rule})
    await s.commit()
    return _group_out(g, "coordinator")


@router.get("/groups/{name}", response_model=GroupOut)
async def get_group(name: str, p: Principal = Depends(current_principal), s: AsyncSession = Depends(get_session)):
    g = await load_group(s, name)
    m = await require_role(s, g, p, ANY_MEMBER, require_active=False)
    return _group_out(g, m.role)


@router.patch("/groups/{name}", response_model=GroupOut)
async def update_group(name: str, body: GroupUpdate, request: Request, p: Principal = Depends(current_principal),
                       s: AsyncSession = Depends(get_session)):
    g = await load_group(s, name)
    m = await require_role(s, g, p, COORDINATOR, mfa_required=request.app.state.settings.require_mfa_for_admins)
    changes = body.model_dump(exclude_unset=True)
    if "default_time_limit_s" in changes and changes["default_time_limit_s"] is None:
        raise HTTPException(422, "default_time_limit_s needs a number of seconds (60 to 604800)")
    for k, v in changes.items():
        setattr(g, k, v)
    await audit.record(s, actor_type="user", actor_id=p.user.id, action="group.update", group_id=g.id,
                       target_type="group", target_id=g.id, details=changes)
    await s.commit()
    return _group_out(g, m.role)


class CloseBody(BaseModel):
    confirm: bool = False


@router.post("/groups/{name}/close", response_model=GroupOut)
async def close_group(name: str, body: CloseBody, request: Request, p: Principal = Depends(current_principal),
                      s: AsyncSession = Depends(get_session)):
    """Close a group: stop all its jobs. Data is kept for `retention_days` (deletion is a separate step)."""
    g = await load_group(s, name)
    m = await require_role(s, g, p, COORDINATOR, mfa_required=request.app.state.settings.require_mfa_for_admins)
    if not body.confirm:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "closing a group needs {\"confirm\": true}")
    g.state = "closed"
    g.closed_at = utcnow()
    stops = await cancel_tasks(s, Task.group_id == g.id, "group_closed")
    await audit.record(s, actor_type="user", actor_id=p.user.id, action="group.close", group_id=g.id,
                       target_type="group", target_id=g.id, details={"cancelled_attempts": len(stops)})
    await s.commit()
    await notify_stops(request.app, stops, "group_closed")
    return _group_out(g, m.role)


# ---------------------------------------------------------------- deleting and restoring


class DeleteGroupBody(BaseModel):
    confirm_name: str = Field(description="The group's name, typed again to confirm")


class DeletedGroupOut(BaseModel):
    name: str
    display_name: str
    deleted_at: datetime
    purge_after: datetime


# SECURITY-SENSITIVE: data deletion and its access control. Needs security review (plan §8.10).
@router.delete("/groups/{name}", response_model=DeletedGroupOut)
async def delete_group(name: str, body: DeleteGroupBody, request: Request,
                       p: Principal = Depends(current_principal), s: AsyncSession = Depends(get_session)):
    """Delete a group: it disappears at once and its work stops; its data is purged after the grace
    period (Settings.group_delete_grace_days) unless a coordinator or server admin restores it."""
    settings = request.app.state.settings
    g = await load_group(s, name)
    await require_role(s, g, p, COORDINATOR, require_active=False, mfa_required=settings.require_mfa_for_admins)
    if body.confirm_name != g.name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "type the group's name exactly to confirm")
    stops = await cancel_tasks(s, Task.group_id == g.id, "group_deleted")
    now = utcnow()
    g.state_before_delete, g.state = g.state, "deleted"
    g.deleted_at, g.purge_after = now, now + timedelta(days=settings.group_delete_grace_days)
    await audit.record(s, actor_type="user", actor_id=p.user.id, action="group.delete", group_id=g.id,
                       target_type="group", target_id=g.id,
                       details={"name": g.name, "purge_after": g.purge_after.isoformat(),
                                "stopped_attempts": len(stops)})
    await s.commit()
    await notify_stops(request.app, stops, "group_deleted")
    return DeletedGroupOut(name=g.name, display_name=g.display_name, deleted_at=g.deleted_at,
                           purge_after=g.purge_after)


@router.get("/deleted-groups", response_model=list[DeletedGroupOut])
async def deleted_groups(request: Request, p: Principal = Depends(current_principal),
                         s: AsyncSession = Depends(get_session)):
    """Deleted groups that can still be restored: the ones you coordinate (server admins: all)."""
    q = select(Group).where(Group.state == "deleted").order_by(Group.deleted_at.desc())
    if p.user.handle not in request.app.state.settings.server_admins:
        q = q.join(Membership, (Membership.group_id == Group.id) & (Membership.user_id == p.user.id)) \
             .where(Membership.role == "coordinator")
    return [DeletedGroupOut(name=g.name, display_name=g.display_name, deleted_at=g.deleted_at,
                            purge_after=g.purge_after) for g in (await s.execute(q)).scalars().all()]


@router.post("/groups/{name}/restore", response_model=GroupOut)
async def restore_group(name: str, request: Request, p: Principal = Depends(current_principal),
                        s: AsyncSession = Depends(get_session)):
    """Undo a deletion during the grace period. Jobs that were stopped stay cancelled."""
    settings = request.app.state.settings
    g = await load_group(s, name, include_deleted=True)
    m = await membership(s, g, p)
    admin = p.user.handle in settings.server_admins
    if not admin and (m is None or m.role != "coordinator"):
        raise not_found()
    if g.state != "deleted":
        raise HTTPException(status.HTTP_409_CONFLICT, "this group is not deleted")
    if settings.require_mfa_for_admins and not p.has_mfa:
        raise HTTPException(status.HTTP_403_FORBIDDEN, MFA_NEEDED)
    if g.purge_after and g.purge_after <= utcnow():
        raise HTTPException(status.HTTP_410_GONE, "too late: this group is being purged")
    g.state, g.state_before_delete = g.state_before_delete or "closed", None
    g.deleted_at = g.purge_after = None
    await audit.record(s, actor_type="user", actor_id=p.user.id, action="group.restore", group_id=g.id,
                       target_type="group", target_id=g.id, details={"name": g.name, "by_server_admin": admin})
    await s.commit()
    return _group_out(g, m.role if m else None)


# ---------------------------------------------------------------- members


class MemberOut(BaseModel):
    user_id: uuid.UUID
    handle: str
    role: str
    probation_until: datetime | None
    joined_at: datetime


@router.get("/groups/{name}/members", response_model=list[MemberOut])
async def list_members(name: str, p: Principal = Depends(current_principal), s: AsyncSession = Depends(get_session)):
    g = await load_group(s, name)
    await require_role(s, g, p, ANY_MEMBER, require_active=False)
    rows = (await s.execute(
        select(Membership, User).join(User, User.id == Membership.user_id)
        .where(Membership.group_id == g.id).order_by(User.handle)
    )).all()
    return [MemberOut(user_id=u.id, handle=u.handle, role=m.role, probation_until=m.probation_until,
                      joined_at=m.created_at) for m, u in rows]


class RoleChange(BaseModel):
    role: Role


async def _member_by_handle(s: AsyncSession, g: Group, handle: str) -> tuple[Membership, User]:
    row = (await s.execute(
        select(Membership, User).join(User, User.id == Membership.user_id)
        .where(Membership.group_id == g.id, User.handle == handle)
    )).first()
    if row is None:
        raise not_found("member")
    return row[0], row[1]


async def _coordinator_count(s: AsyncSession, g: Group) -> int:
    return (await s.execute(select(func.count()).select_from(Membership)
                            .where(Membership.group_id == g.id, Membership.role == "coordinator"))).scalar_one()


@router.patch("/groups/{name}/members/{handle}", response_model=MemberOut)
async def change_role(name: str, handle: str, body: RoleChange, request: Request,
                      p: Principal = Depends(current_principal), s: AsyncSession = Depends(get_session)):
    g = await load_group(s, name)
    await require_role(s, g, p, COORDINATOR, mfa_required=request.app.state.settings.require_mfa_for_admins)
    m, u = await _member_by_handle(s, g, handle)
    if m.role == "coordinator" and body.role != "coordinator" and await _coordinator_count(s, g) == 1:
        raise HTTPException(status.HTTP_409_CONFLICT, "a group needs at least one coordinator")
    old = m.role
    m.role = body.role
    await audit.record(s, actor_type="user", actor_id=p.user.id, action="member.role", group_id=g.id,
                       target_type="user", target_id=u.id, details={"from": old, "to": body.role})
    await s.commit()
    return MemberOut(user_id=u.id, handle=u.handle, role=m.role, probation_until=m.probation_until,
                     joined_at=m.created_at)


@router.delete("/groups/{name}/members/{handle}", status_code=204)
async def remove_member(name: str, handle: str, request: Request, p: Principal = Depends(current_principal),
                        s: AsyncSession = Depends(get_session)):
    """Removal takes effect immediately (plan §6.3 item 4)."""
    g = await load_group(s, name)
    is_self = handle == p.user.handle
    if is_self:
        await require_role(s, g, p, ANY_MEMBER, require_active=False)
    else:
        await require_role(s, g, p, COORDINATOR, require_active=False,
                           mfa_required=request.app.state.settings.require_mfa_for_admins)
    m, u = await _member_by_handle(s, g, handle)
    if m.role == "coordinator" and await _coordinator_count(s, g) == 1:
        raise HTTPException(status.HTTP_409_CONFLICT, "the last coordinator cannot leave; close the group instead")
    from ..models import Job  # local import keeps the module graph simple

    # 1. Cancel their running and queued jobs.
    stops = await cancel_tasks(
        s, Task.job_id.in_(select(Job.id).where(Job.group_id == g.id, Job.submitter_id == u.id)), "member_removed")
    # 2. Their devices stop serving the group.
    device_ids = (await s.execute(select(Device.id).where(Device.owner_id == u.id))).scalars().all()
    unshared = 0
    if device_ids:
        shares = (await s.execute(select(DeviceShare).where(DeviceShare.group_id == g.id,
                                                            DeviceShare.device_id.in_(device_ids)))).scalars().all()
        for sh in shares:
            await s.delete(sh)
            unshared += 1
        from ..models import Attempt

        running_here = (await s.execute(
            select(Task.id).join(Attempt, Attempt.id == Task.current_attempt_id)
            .where(Task.group_id == g.id, Attempt.device_id.in_(device_ids),
                   Task.state.in_(("assigned", "running")))
        )).scalars().all()
        if running_here:
            # Requeue (not cancel): the job belongs to someone else and can run elsewhere.
            from ..scheduler.core import lose_attempt

            for tid in running_here:
                t = await s.get(Task, tid)
                a = await s.get(Attempt, t.current_attempt_id)
                stops.append((str(a.id), a.device_id))
                await lose_attempt(s, request.app.state.settings, a, reason="owner_removed")
    await s.delete(m)
    # 3. Group secrets would be flagged for rotation here (secrets arrive in phase 3).
    await audit.record(s, actor_type="user", actor_id=p.user.id, action="member.remove", group_id=g.id,
                       target_type="user", target_id=u.id,
                       details={"self": is_self, "cancelled_attempts": len(stops), "devices_unshared": unshared,
                                "secrets_flagged": 0})
    await s.commit()
    await notify_stops(request.app, stops, "member_removed", group=g.name, revoke_devices=device_ids)


# ---------------------------------------------------------------- invites


class InviteCreate(BaseModel):
    role: Literal["admin", "member", "contributor", "submitter"] = "member"
    max_uses: int = Field(default=1, ge=1, le=500)
    expires_in_hours: int = Field(default=72, ge=1, le=24 * 30)


class InviteOut(BaseModel):
    id: uuid.UUID
    role: str
    max_uses: int
    uses: int
    expires_at: datetime
    revoked: bool
    token: str | None = None  # only returned once, at creation


@router.post("/groups/{name}/invites", response_model=InviteOut, status_code=201)
async def create_invite(name: str, body: InviteCreate, request: Request, p: Principal = Depends(current_principal),
                        s: AsyncSession = Depends(get_session)):
    g = await load_group(s, name)
    m = await require_role(s, g, p, MANAGERS, mfa_required=request.app.state.settings.require_mfa_for_admins)
    if body.role == "admin" and m.role != "coordinator":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "only a coordinator can invite admins")
    token = random_token()
    inv = Invite(group_id=g.id, token_hash=sha256_hex(token), role=body.role, max_uses=body.max_uses,
                 expires_at=utcnow() + timedelta(hours=body.expires_in_hours), created_by=p.user.id)
    s.add(inv)
    await s.flush()
    await audit.record(s, actor_type="user", actor_id=p.user.id, action="invite.create", group_id=g.id,
                       target_type="invite", target_id=inv.id,
                       details={"role": body.role, "max_uses": body.max_uses, "expires_at": inv.expires_at})
    await s.commit()
    return InviteOut(id=inv.id, role=inv.role, max_uses=inv.max_uses, uses=0, expires_at=inv.expires_at,
                     revoked=False, token=token)


@router.get("/groups/{name}/invites", response_model=list[InviteOut])
async def list_invites(name: str, p: Principal = Depends(current_principal), s: AsyncSession = Depends(get_session)):
    g = await load_group(s, name)
    await require_role(s, g, p, MANAGERS, require_active=False)
    rows = (await s.execute(select(Invite).where(Invite.group_id == g.id).order_by(Invite.created_at.desc()))).scalars()
    return [InviteOut(id=i.id, role=i.role, max_uses=i.max_uses, uses=i.uses, expires_at=i.expires_at,
                      revoked=i.revoked) for i in rows]


@router.delete("/groups/{name}/invites/{invite_id}", status_code=204)
async def revoke_invite(name: str, invite_id: uuid.UUID, p: Principal = Depends(current_principal),
                        s: AsyncSession = Depends(get_session)):
    g = await load_group(s, name)
    await require_role(s, g, p, MANAGERS, require_active=False)
    inv = await s.get(Invite, invite_id)
    if inv is None or inv.group_id != g.id:
        raise not_found("invite")
    inv.revoked = True
    await audit.record(s, actor_type="user", actor_id=p.user.id, action="invite.revoke", group_id=g.id,
                       target_type="invite", target_id=inv.id)
    await s.commit()


class InviteAccept(BaseModel):
    token: str = Field(min_length=10, max_length=128)


async def _add_member(s: AsyncSession, g: Group, user: User, role: str) -> Membership:
    probation = utcnow() + timedelta(days=g.probation_days) if g.probation_days else None
    m = Membership(group_id=g.id, user_id=user.id, role=role, probation_until=probation)
    s.add(m)
    await s.flush()
    return m


@router.post("/invites/accept", response_model=GroupOut)
async def accept_invite(body: InviteAccept, p: Principal = Depends(current_principal),
                        s: AsyncSession = Depends(get_session)):
    inv = (await s.execute(select(Invite).where(Invite.token_hash == sha256_hex(body.token))
                           .with_for_update())).scalar_one_or_none()
    if inv is None or inv.revoked or inv.uses >= inv.max_uses or inv.expires_at < utcnow():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "invite is invalid, used up or expired")
    g = await s.get(Group, inv.group_id)
    if g.state == "deleted":
        raise not_found("invite")
    if g.state != "active":
        raise HTTPException(status.HTTP_409_CONFLICT, f"group is {g.state}")
    if await membership(s, g, p):
        raise HTTPException(status.HTTP_409_CONFLICT, "you are already a member")
    inv.uses += 1
    m = await _add_member(s, g, p.user, inv.role)
    await audit.record(s, actor_type="user", actor_id=p.user.id, action="member.join", group_id=g.id,
                       target_type="user", target_id=p.user.id, details={"via": "invite", "invite_id": str(inv.id),
                                                                       "role": inv.role})
    await s.commit()
    return _group_out(g, m.role)


# ---------------------------------------------------------------- join requests


class JoinRequestCreate(BaseModel):
    message: str = Field(default="", max_length=500)


class JoinRequestOut(BaseModel):
    id: uuid.UUID
    handle: str
    message: str
    state: str
    approvals: int
    rejections: int
    needed: int
    created_at: datetime


class Vote(BaseModel):
    decision: Literal["approve", "reject"]


async def _needed(s: AsyncSession, g: Group) -> int:
    if g.approval_rule == "coordinator-only":
        return 1
    if g.approval_rule == "any-k-members":
        return g.approval_k
    voters = (await s.execute(select(func.count()).select_from(Membership)
                              .where(Membership.group_id == g.id, Membership.role.in_(VOTERS)))).scalar_one()
    return voters // 2 + 1


async def _jr_out(s: AsyncSession, g: Group, jr: JoinRequest) -> JoinRequestOut:
    votes = dict((await s.execute(select(Approval.decision, func.count()).where(Approval.join_request_id == jr.id)
                                  .group_by(Approval.decision))).all())
    u = await s.get(User, jr.user_id)
    return JoinRequestOut(id=jr.id, handle=u.handle, message=jr.message, state=jr.state,
                          approvals=votes.get("approve", 0), rejections=votes.get("reject", 0),
                          needed=await _needed(s, g), created_at=jr.created_at)


@router.post("/groups/{name}/join-requests", response_model=JoinRequestOut, status_code=201)
async def request_join(name: str, body: JoinRequestCreate, p: Principal = Depends(current_principal),
                       s: AsyncSession = Depends(get_session)):
    g = await load_group(s, name)
    if g.state != "active":
        raise not_found()
    if await membership(s, g, p):
        raise HTTPException(status.HTTP_409_CONFLICT, "you are already a member")
    jr = JoinRequest(group_id=g.id, user_id=p.user.id, message=body.message)
    s.add(jr)
    try:
        await s.flush()
    except IntegrityError:
        await s.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "you already have a pending request") from None
    await audit.record(s, actor_type="user", actor_id=p.user.id, action="join_request.create", group_id=g.id,
                       target_type="join_request", target_id=jr.id)
    await s.commit()
    return await _jr_out(s, g, jr)


@router.get("/groups/{name}/join-requests", response_model=list[JoinRequestOut])
async def list_join_requests(name: str, state: str = "pending", p: Principal = Depends(current_principal),
                             s: AsyncSession = Depends(get_session)):
    g = await load_group(s, name)
    m = await require_role(s, g, p, VOTERS, require_active=False)
    if g.approval_rule == "coordinator-only" and m.role not in MANAGERS:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "only coordinators and admins decide in this group")
    rows = (await s.execute(select(JoinRequest).where(JoinRequest.group_id == g.id, JoinRequest.state == state)
                            .order_by(JoinRequest.created_at))).scalars().all()
    return [await _jr_out(s, g, jr) for jr in rows]


@router.post("/groups/{name}/join-requests/{request_id}/votes", response_model=JoinRequestOut)
async def vote(name: str, request_id: uuid.UUID, body: Vote, request: Request,
               p: Principal = Depends(current_principal), s: AsyncSession = Depends(get_session)):
    g = await load_group(s, name)
    m = await require_role(s, g, p, VOTERS, mfa_required=request.app.state.settings.require_mfa_for_admins)
    if g.approval_rule == "coordinator-only" and m.role not in MANAGERS:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "only coordinators and admins decide in this group")
    jr = (await s.execute(select(JoinRequest).where(JoinRequest.id == request_id, JoinRequest.group_id == g.id)
                          .with_for_update())).scalar_one_or_none()
    if jr is None:
        raise not_found("join request")
    if jr.state != "pending":
        raise HTTPException(status.HTTP_409_CONFLICT, f"request is already {jr.state}")
    if jr.user_id == p.user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "you cannot vote on your own request")
    existing = (await s.execute(select(Approval).where(Approval.join_request_id == jr.id,
                                                       Approval.approver_id == p.user.id))).scalar_one_or_none()
    if existing:
        existing.decision = body.decision
    else:
        s.add(Approval(join_request_id=jr.id, approver_id=p.user.id, decision=body.decision))
    await s.flush()
    await audit.record(s, actor_type="user", actor_id=p.user.id, action="join_request.vote", group_id=g.id,
                       target_type="join_request", target_id=jr.id, details={"decision": body.decision})
    out = await _jr_out(s, g, jr)
    decided = None
    if body.decision == "reject" and (m.role in MANAGERS or out.rejections >= out.needed):
        decided = "rejected"
    elif out.approvals >= out.needed:
        decided = "approved"
    if decided:
        jr.state = decided
        jr.decided_at = utcnow()
        if decided == "approved":
            u = await s.get(User, jr.user_id)
            await _add_member(s, g, u, "member")
            await audit.record(s, actor_type="user", actor_id=p.user.id, action="member.join", group_id=g.id,
                               target_type="user", target_id=u.id,
                               details={"via": "join_request", "rule": g.approval_rule})
        else:
            await audit.record(s, actor_type="user", actor_id=p.user.id, action="join_request.reject",
                               group_id=g.id, target_type="join_request", target_id=jr.id)
    await s.commit()
    return await _jr_out(s, g, jr)
