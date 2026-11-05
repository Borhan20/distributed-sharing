"""Credits in the API (docs/design/credits.md §4): balances, history, giving, asking, granting, moving."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .. import audit, credits
from ..auth import Principal, current_principal, get_session
from ..authz import ANY_MEMBER, COORDINATOR, MANAGERS, load_group, require_role
from ..models import CreditBalance, CreditEntry, CreditRequest, Device, Group, Membership, User
from ..timeutil import utcnow

router = APIRouter(prefix="/api/v1", tags=["credits"])


class RequestOut(BaseModel):
    id: uuid.UUID
    member: str
    amount: float
    reason: str
    state: str
    granted: float | None
    created_at: datetime
    decided_at: datetime | None


class CreditsOut(BaseModel):
    group: str
    enabled: bool
    balance: float
    free: float  # of the balance: start credits, grants and approved requests (they stay in the group)
    earned: float  # of the balance: can move to another group
    start: float
    rates: dict[str, float]
    out_of_credits: str
    standby_percent: int
    credits_in: bool
    credits_out: bool
    open_request: RequestOut | None = None


class EntryOut(BaseModel):
    id: int
    kind: str
    amount: float
    free: float
    note: str | None
    job_id: uuid.UUID | None
    device: str | None
    counterparty: str | None
    created_at: datetime


class MemberCreditsOut(BaseModel):
    member: str
    balance: float
    free: float
    earned: float


class GiveIn(BaseModel):
    to: str = Field(min_length=1, max_length=64)
    amount: float = Field(gt=0, le=1_000_000)
    note: str | None = Field(default=None, max_length=200)


class AskIn(BaseModel):
    amount: float = Field(gt=0, le=1_000_000)
    reason: str = Field(min_length=1, max_length=500)


class GrantIn(BaseModel):
    to: str = Field(min_length=1, max_length=64)
    amount: float = Field(gt=0, le=1_000_000)
    reason: str = Field(min_length=1, max_length=500)


class ApproveIn(BaseModel):
    amount: float | None = Field(default=None, gt=0, le=1_000_000)  # less than asked: approve part of it


class MoveIn(BaseModel):
    from_group: str
    to_group: str
    amount: float = Field(gt=0, le=1_000_000)


class MoveOut(BaseModel):
    source: CreditsOut
    target: CreditsOut


def _f(x: Decimal | None) -> float:
    return float(x or 0)


def _request_out(r: CreditRequest, handle: str) -> RequestOut:
    return RequestOut(id=r.id, member=handle, amount=_f(r.amount), reason=r.reason, state=r.state,
                      granted=_f(r.granted) if r.granted is not None else None, created_at=r.created_at,
                      decided_at=r.decided_at)


async def _credits_out(s: AsyncSession, g: Group, user: User) -> CreditsOut:
    row = await credits.balance_row(s, g, user.id) if g.credits_enabled else None
    req = (await s.execute(select(CreditRequest).where(
        CreditRequest.group_id == g.id, CreditRequest.user_id == user.id, CreditRequest.state == "open"))
    ).scalar_one_or_none()
    balance, free = (row.balance, row.free) if row else (Decimal(0), Decimal(0))
    return CreditsOut(group=g.name, enabled=g.credits_enabled, balance=_f(balance), free=_f(min(free, max(balance, 0))),
                      earned=_f(max(balance - free, Decimal(0))), start=_f(g.credit_start), rates=credits.rates(g),
                      out_of_credits=g.out_of_credits, standby_percent=g.standby_percent, credits_in=g.credits_in,
                      credits_out=g.credits_out, open_request=_request_out(req, user.handle) if req else None)


def _require_enabled(g: Group) -> None:
    if not g.credits_enabled:
        raise HTTPException(status.HTTP_409_CONFLICT, f"{g.name} does not use credits")


async def _member(s: AsyncSession, g: Group, handle: str) -> User:
    user = (await s.execute(
        select(User).join(Membership, Membership.user_id == User.id)
        .where(User.handle == handle, Membership.group_id == g.id))).scalar_one_or_none()
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{handle} is not a member of {g.name}")
    return user


def _bad(exc: credits.CreditError) -> HTTPException:
    return HTTPException(422, str(exc))


@router.get("/groups/{name}/credits", response_model=CreditsOut)
async def my_credits(name: str, p: Principal = Depends(current_principal), s: AsyncSession = Depends(get_session)):
    g = await load_group(s, name)
    await require_role(s, g, p, ANY_MEMBER)
    out = await _credits_out(s, g, p.user)
    await s.commit()  # start credits, the first time
    return out


@router.get("/groups/{name}/credits/history", response_model=list[EntryOut])
async def my_history(name: str, limit: int = 50, before_id: int | None = None,
                     p: Principal = Depends(current_principal), s: AsyncSession = Depends(get_session)):
    g = await load_group(s, name)
    await require_role(s, g, p, ANY_MEMBER)
    q = (select(CreditEntry, Device.name, User.handle)
         .outerjoin(Device, Device.id == CreditEntry.device_id)
         .outerjoin(User, User.id == CreditEntry.counterparty_id)
         .where(CreditEntry.group_id == g.id, CreditEntry.user_id == p.user.id))
    if before_id is not None:
        q = q.where(CreditEntry.id < before_id)
    rows = (await s.execute(q.order_by(CreditEntry.id.desc()).limit(max(1, min(limit, 500))))).all()
    return [EntryOut(id=e.id, kind=e.kind, amount=_f(e.amount), free=_f(e.free), note=e.note, job_id=e.job_id,
                     device=dname, counterparty=handle, created_at=e.created_at) for e, dname, handle in rows]


@router.get("/groups/{name}/credits/members", response_model=list[MemberCreditsOut])
async def member_credits(name: str, p: Principal = Depends(current_principal), s: AsyncSession = Depends(get_session)):
    """Every member's balance: for coordinators and admins (credits.md §7, privacy)."""
    g = await load_group(s, name)
    await require_role(s, g, p, MANAGERS)
    rows = (await s.execute(
        select(User.handle, CreditBalance.balance, CreditBalance.free)
        .join(Membership, Membership.user_id == User.id)
        .outerjoin(CreditBalance, (CreditBalance.user_id == User.id) & (CreditBalance.group_id == g.id))
        .where(Membership.group_id == g.id).order_by(User.handle))).all()
    return [MemberCreditsOut(member=h, balance=_f(b), free=_f(min(f or 0, max(b or 0, 0))),
                             earned=_f(max((b or 0) - (f or 0), 0))) for h, b, f in rows]


@router.post("/groups/{name}/credits/give", response_model=CreditsOut)
async def give_credits(name: str, body: GiveIn, p: Principal = Depends(current_principal),
                       s: AsyncSession = Depends(get_session)):
    g = await load_group(s, name)
    await require_role(s, g, p, ANY_MEMBER)
    _require_enabled(g)
    to = await _member(s, g, body.to)
    try:
        amount = await credits.give(s, g, p.user.id, to.id, body.amount, body.note)
    except credits.CreditError as exc:
        raise _bad(exc) from None
    await audit.record(s, actor_type="user", actor_id=p.user.id, action="credits.give", group_id=g.id,
                       target_type="user", target_id=to.id, details={"to": to.handle, "amount": float(amount)})
    out = await _credits_out(s, g, p.user)
    await s.commit()
    return out


@router.post("/groups/{name}/credits/requests", response_model=RequestOut, status_code=201)
async def ask_for_credits(name: str, body: AskIn, p: Principal = Depends(current_principal),
                          s: AsyncSession = Depends(get_session)):
    g = await load_group(s, name)
    await require_role(s, g, p, ANY_MEMBER)
    _require_enabled(g)
    r = CreditRequest(group_id=g.id, user_id=p.user.id, amount=credits.amount_of(body.amount), reason=body.reason)
    s.add(r)
    try:
        await s.flush()
    except IntegrityError:
        await s.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "you already have an open request in this group") from None
    await audit.record(s, actor_type="user", actor_id=p.user.id, action="credits.ask", group_id=g.id,
                       target_type="credit_request", target_id=r.id, details={"amount": body.amount})
    await s.commit()
    await s.refresh(r)
    return _request_out(r, p.user.handle)


@router.get("/groups/{name}/credits/requests", response_model=list[RequestOut])
async def list_requests(name: str, state: str = "open", p: Principal = Depends(current_principal),
                        s: AsyncSession = Depends(get_session)):
    """The coordinator sees every member's requests; a member sees their own."""
    g = await load_group(s, name)
    m = await require_role(s, g, p, ANY_MEMBER)
    q = (select(CreditRequest, User.handle).join(User, User.id == CreditRequest.user_id)
         .where(CreditRequest.group_id == g.id))
    if state != "all":
        q = q.where(CreditRequest.state == state)
    if m.role != "coordinator":
        q = q.where(CreditRequest.user_id == p.user.id)
    rows = (await s.execute(q.order_by(CreditRequest.created_at.desc()).limit(200))).all()
    return [_request_out(r, h) for r, h in rows]


async def _open_request(s: AsyncSession, g: Group, request_id: uuid.UUID) -> tuple[CreditRequest, User]:
    row = (await s.execute(
        select(CreditRequest, User).join(User, User.id == CreditRequest.user_id)
        .where(CreditRequest.id == request_id, CreditRequest.group_id == g.id).with_for_update(of=CreditRequest)
    )).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such request")
    if row[0].state != "open":
        raise HTTPException(status.HTTP_409_CONFLICT, f"this request is already {row[0].state}")
    return row[0], row[1]


@router.post("/groups/{name}/credits/requests/{request_id}/approve", response_model=RequestOut)
async def approve_request(name: str, request_id: uuid.UUID, body: ApproveIn, request: Request,
                          p: Principal = Depends(current_principal), s: AsyncSession = Depends(get_session)):
    g = await load_group(s, name)
    await require_role(s, g, p, COORDINATOR, mfa_required=request.app.state.settings.require_mfa_for_admins)
    _require_enabled(g)
    r, member = await _open_request(s, g, request_id)
    amount = credits.amount_of(body.amount if body.amount is not None else r.amount)
    if amount > r.amount:
        raise HTTPException(422, "you can approve at most the amount asked for")
    try:
        await credits.grant(s, g, p.user.id, member.id, float(amount), "request", r.reason, request_id=r.id)
    except credits.CreditError as exc:
        raise _bad(exc) from None
    r.state, r.granted, r.decided_by, r.decided_at = "approved", amount, p.user.id, utcnow()
    await audit.record(s, actor_type="user", actor_id=p.user.id, action="credits.approve", group_id=g.id,
                       target_type="credit_request", target_id=r.id,
                       details={"member": member.handle, "asked": float(r.amount), "granted": float(amount)})
    await s.commit()
    return _request_out(r, member.handle)


@router.post("/groups/{name}/credits/requests/{request_id}/decline", response_model=RequestOut)
async def decline_request(name: str, request_id: uuid.UUID, request: Request,
                          p: Principal = Depends(current_principal), s: AsyncSession = Depends(get_session)):
    g = await load_group(s, name)
    await require_role(s, g, p, COORDINATOR, mfa_required=request.app.state.settings.require_mfa_for_admins)
    r, member = await _open_request(s, g, request_id)
    r.state, r.decided_by, r.decided_at = "declined", p.user.id, utcnow()
    await audit.record(s, actor_type="user", actor_id=p.user.id, action="credits.decline", group_id=g.id,
                       target_type="credit_request", target_id=r.id, details={"member": member.handle})
    await s.commit()
    return _request_out(r, member.handle)


@router.post("/groups/{name}/credits/grant", response_model=MemberCreditsOut)
async def grant_credits(name: str, body: GrantIn, request: Request, p: Principal = Depends(current_principal),
                        s: AsyncSession = Depends(get_session)):
    g = await load_group(s, name)
    await require_role(s, g, p, COORDINATOR, mfa_required=request.app.state.settings.require_mfa_for_admins)
    _require_enabled(g)
    to = await _member(s, g, body.to)
    try:
        amount = await credits.grant(s, g, p.user.id, to.id, body.amount, "grant", body.reason)
    except credits.CreditError as exc:
        raise _bad(exc) from None
    await audit.record(s, actor_type="user", actor_id=p.user.id, action="credits.grant", group_id=g.id,
                       target_type="user", target_id=to.id,
                       details={"to": to.handle, "amount": float(amount), "reason": body.reason})
    row = await credits.balance_row(s, g, to.id)
    out = MemberCreditsOut(member=to.handle, balance=_f(row.balance), free=_f(row.free),
                           earned=_f(max(row.balance - row.free, Decimal(0))))
    await s.commit()
    return out


@router.post("/me/credits/move", response_model=MoveOut)
async def move_credits(body: MoveIn, p: Principal = Depends(current_principal), s: AsyncSession = Depends(get_session)):
    source = await load_group(s, body.from_group)
    target = await load_group(s, body.to_group)
    await require_role(s, source, p, ANY_MEMBER)
    await require_role(s, target, p, ANY_MEMBER)
    try:
        amount = await credits.move(s, p.user.id, source, target, body.amount)
    except credits.CreditError as exc:
        raise _bad(exc) from None
    for g, direction in ((source, "out"), (target, "in")):
        await audit.record(s, actor_type="user", actor_id=p.user.id, action=f"credits.move_{direction}",
                           group_id=g.id, target_type="user", target_id=p.user.id,
                           details={"from": source.name, "to": target.name, "amount": float(amount)})
    out = MoveOut(source=await _credits_out(s, source, p.user), target=await _credits_out(s, target, p.user))
    await s.commit()
    return out
