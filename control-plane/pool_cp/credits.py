"""Credits: share to earn, use to spend (docs/design/credits.md).

- One credit is one CPU core for one hour. Memory and GPUs count as CPU hours at the group's rates.
- When a task ends on someone else's computer, its submitter pays and the computer's owner earns the same
  amount. Running on your own computer costs nothing and earns nothing.
- A size pool chose is paid by what the task used; a size the person set by what it reserved.
- Not charged in the end: a try that was too small for a size pool chose, once a bigger try succeeded (pool pays
  the person back; the owner keeps what they earned), and the part of a task lost when its computer went away.
- Free credits (start credits, grants, approved requests) are counted apart: they are spent first, stay free
  when given, and cannot move to another group.

Every change is one row in credit_entries (never changed or deleted) and one update of the member's row in
credit_balances, in the same transaction. Giving, moving and approving lock the balance rows first, so the same
credits are never spent twice.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Attempt, CreditBalance, CreditEntry, Device, DeviceShare, Group, Job, Task

DEFAULT_RATES = {"cpu_hour": 1, "gib_hours_per_credit": 4, "gpu_hour": 10}
# New groups: credits on for peer groups, off where one side shares and the other uses (credits.md §3.9).
TEMPLATE_CREDITS: dict[str, tuple[bool, int]] = {
    "research-team": (False, 20), "dev-team": (True, 20), "student-group": (True, 20), "course": (False, 20),
    "hackathon": (True, 10),
}
CHARGE_EVERY_S = 3600  # long tasks are charged every hour while they run
MAX_AMOUNT = Decimal(1_000_000)
ZERO = Decimal(0)
CENT = Decimal("0.0001")


class CreditError(ValueError):
    """A credit change that is not allowed; the message says why in plain words."""


def q(x: float | Decimal) -> Decimal:
    return Decimal(str(x)).quantize(CENT, rounding=ROUND_HALF_UP)


def rates(group: Group) -> dict[str, float]:
    r = {**DEFAULT_RATES, **(group.credit_rates or {})}
    return {k: float(r[k]) for k in DEFAULT_RATES}


def cost(group: Group, *, cpu: float, memory_mb: float, gpus: int, seconds: float) -> Decimal:
    """(CPU cores + memory GiB / gib_hours_per_credit + GPUs × gpu_hour) × hours, at the group's rates."""
    r = rates(group)
    per_hour = (cpu * r["cpu_hour"] + (memory_mb / 1024) / max(r["gib_hours_per_credit"], 1e-9)
                + gpus * r["gpu_hour"])
    return q(max(per_hour, 0.0) * max(seconds, 0.0) / 3600)


# --------------------------------------------------------------------------
# Balances and entries
# --------------------------------------------------------------------------


async def balance_row(s: AsyncSession, group: Group, user_id: uuid.UUID, *, lock: bool = False) -> CreditBalance:
    """The member's balance in this group. The first time, with the group's start credits (if credits are on)."""
    stmt = select(CreditBalance).where(CreditBalance.group_id == group.id, CreditBalance.user_id == user_id)
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    row = (await s.execute(stmt)).scalar_one_or_none()
    if row is not None:
        return row
    start = q(group.credit_start or 0) if group.credits_enabled else ZERO
    inserted = (await s.execute(
        pg_insert(CreditBalance).values(group_id=group.id, user_id=user_id, balance=start, free=start)
        .on_conflict_do_nothing().returning(CreditBalance.user_id))).scalar_one_or_none()
    if inserted is not None and start > 0:
        s.add(CreditEntry(group_id=group.id, user_id=user_id, amount=start, free=start, kind="start",
                          note="start credits"))
    return (await s.execute(stmt.execution_options(populate_existing=True))).scalar_one()


async def _apply(s: AsyncSession, group: Group, user_id: uuid.UUID, amount: Decimal, free: Decimal, kind: str,
                 **links: Any) -> None:
    """Change a balance by `amount`, of which `free` is free credits, and write the entry. The row is locked
    (callers that change two balances lock both first, in _lock_pair's order, so they cannot deadlock)."""
    row = await balance_row(s, group, user_id, lock=True)
    row.balance = q(row.balance + amount)
    row.free = q(max(row.free + free, ZERO))
    s.add(CreditEntry(group_id=group.id, user_id=user_id, amount=q(amount), free=q(free), kind=kind, **links))


async def _spend(s: AsyncSession, group: Group, user_id: uuid.UUID, amount: Decimal, kind: str,
                 **links: Any) -> Decimal:
    """Take `amount` (may go below zero), free credits first. Returns the free part taken."""
    row = await balance_row(s, group, user_id, lock=True)
    free = min(row.free, amount) if amount > 0 else ZERO
    await _apply(s, group, user_id, -amount, -free, kind, **links)
    return free


# --------------------------------------------------------------------------
# Charging for runs
# --------------------------------------------------------------------------


def _run_size(job: Job, attempt: Attempt) -> tuple[float, float]:
    """CPU and memory a run is paid for: what it used for sizes pool chose, what it held for sizes given."""
    given = attempt.resources or {}
    cpu = float(given.get("cpu") or job.cpu)
    memory = float(given.get("memory_mb") or job.memory_mb)
    auto = set((job.sizing or {}).get("auto") or [])
    used = attempt.usage or {}
    if used.get("samples"):
        if "cpu" in auto and used.get("avg_cpu") is not None:
            cpu = min(cpu, float(used["avg_cpu"]))
        if "memory" in auto:
            memory = min(memory, float(used.get("peak_memory_mb") or memory))
    elif "memory" in auto and given.get("first_run"):
        # A first run holds a whole computer's free memory; without a measurement, pay for the job's size now.
        memory = float(job.memory_mb)
    return cpu, memory


async def charge_attempt(s: AsyncSession, attempt: Attempt, task: Task, now: datetime, *,
                         outcome: str) -> None:
    """Charge (or settle) an attempt. outcome: "ran" (it ended: success, the program's failure, or a size pool
    chose that was too small; see refund_pool_retry), "running" (hourly, while it runs) or "lost" (the computer
    went away, or the try was stopped for the owner)."""
    if attempt.started_at is None:
        return
    group = await s.get(Group, task.group_id)
    if not group.credits_enabled:
        return
    job = await s.get(Job, task.job_id)
    device = await s.get(Device, attempt.device_id)
    if device is None or device.owner_id == job.submitter_id:
        return  # your own computer: free
    seconds = max((now - attempt.started_at).total_seconds(), 0.0)
    await _lock_pair(s, group, job.submitter_id, group, device.owner_id)
    cpu, memory = _run_size(job, attempt)
    total = cost(group, cpu=cpu, memory_mb=memory, gpus=job.gpus, seconds=seconds)
    links = {"job_id": job.id, "attempt_id": attempt.id, "device_id": device.id}
    if outcome == "lost":
        total = ZERO  # nothing for the lost part: hourly charges come back
    delta = q(total - attempt.charged)
    if delta == 0:
        return
    await _spend(s, group, job.submitter_id, delta, "spend", **links, counterparty_id=device.owner_id)
    await _apply(s, group, device.owner_id, delta, ZERO, "earn", **links, counterparty_id=job.submitter_id)
    attempt.charged = q(attempt.charged + delta)
    attempt.charged_s = seconds


async def refund_pool_retry(s: AsyncSession, attempt: Attempt, task: Task) -> None:
    """A try was too small for a size pool chose, and a bigger try then succeeded: pool gives the person back
    what they paid for the small try. The owner keeps what they earned: their computer did the work. (Until a
    bigger try succeeds the person pays, so failing on purpose makes no free credits.)"""
    if not attempt.charged:
        return
    group = await s.get(Group, task.group_id)
    if not group.credits_enabled:
        return
    job = await s.get(Job, task.job_id)
    await _apply(s, group, job.submitter_id, attempt.charged, ZERO, "pool_retry", job_id=job.id,
                 attempt_id=attempt.id, device_id=attempt.device_id,
                 note="a size pool chose was too small: paid back by pool")


async def charge_running(s: AsyncSession, now: datetime) -> int:
    """Charge long tasks every hour while they run, so balances stay up to date. Settled when they end."""
    rows = (await s.execute(
        select(Attempt, Task).join(Task, Task.id == Attempt.task_id).join(Group, Group.id == Task.group_id)
        .where(Attempt.state == "running", Attempt.started_at.isnot(None), Group.credits_enabled,
               Attempt.started_at < now - timedelta(seconds=CHARGE_EVERY_S))
        .with_for_update(of=Attempt, skip_locked=True).limit(500)
    )).all()
    n = 0
    for attempt, task in rows:
        if (now - attempt.started_at).total_seconds() - attempt.charged_s >= CHARGE_EVERY_S:
            await charge_attempt(s, attempt, task, now, outcome="running")
            n += 1
    return n


async def pay_standby(s: AsyncSession, now: datetime, heartbeat_s: int) -> int:
    """'Earn while waiting': owners earn standby_percent of the CPU rate for an online computer that is shared
    with the group, available, and runs nothing for it. Paid up to `now`, at most 2 minutes back."""
    since = now - timedelta(seconds=heartbeat_s * 3)
    rows = (await s.execute(
        select(DeviceShare, Device, Group).join(Device, Device.id == DeviceShare.device_id)
        .join(Group, Group.id == DeviceShare.group_id)
        .where(Group.credits_enabled, Group.standby_percent > 0, Group.state == "active", DeviceShare.enabled,
               DeviceShare.removed.is_(False), Device.state == "active", Device.last_seen_at > since)
        .with_for_update(of=DeviceShare, skip_locked=True)
    )).all()
    paid = 0
    for share, device, group in rows:
        reported = next((r for r in device.reported_shares or [] if r.get("group") == group.name), None)
        start = max(share.standby_paid_until or now, now - timedelta(minutes=2))
        share.standby_paid_until = now
        if not reported or not reported.get("active_now"):
            continue
        busy = (await s.execute(
            select(func.count()).select_from(Attempt).join(Task, Task.id == Attempt.task_id)
            .where(Attempt.device_id == device.id, Task.group_id == group.id,
                   Attempt.state.in_(("assigned", "running"))))).scalar_one()
        if busy:
            continue
        cores = ((device.capabilities or {}).get("cpu_cores") or 0) * ((reported.get("limits") or {})
                                                                         .get("cpu_percent") or 0) / 100
        amount = q(cores * rates(group)["cpu_hour"] * group.standby_percent / 100
                   * max((now - start).total_seconds(), 0.0) / 3600)
        if amount > 0:
            await _apply(s, group, device.owner_id, amount, ZERO, "standby", device_id=device.id,
                         note="online and waiting for work")
            paid += 1
    return paid


# --------------------------------------------------------------------------
# Giving, moving, free credits
# --------------------------------------------------------------------------


def amount_of(value: float | Decimal) -> Decimal:
    amount = q(value)
    if amount <= 0 or amount > MAX_AMOUNT:
        raise CreditError(f"the amount must be more than 0 and at most {MAX_AMOUNT:,}")
    return amount


async def _lock_pair(s: AsyncSession, group_a: Group, user_a: uuid.UUID, group_b: Group,
                     user_b: uuid.UUID) -> tuple[CreditBalance, CreditBalance]:
    """Lock two balance rows in one fixed order, so two transfers the other way round cannot deadlock."""
    a_key, b_key = (str(group_a.id), str(user_a)), (str(group_b.id), str(user_b))
    if a_key <= b_key:
        a = await balance_row(s, group_a, user_a, lock=True)
        b = await balance_row(s, group_b, user_b, lock=True)
    else:
        b = await balance_row(s, group_b, user_b, lock=True)
        a = await balance_row(s, group_a, user_a, lock=True)
    return a, b


async def give(s: AsyncSession, group: Group, giver: uuid.UUID, receiver: uuid.UUID, value: float,
               note: str | None) -> Decimal:
    amount = amount_of(value)
    if giver == receiver:
        raise CreditError("you cannot give credits to yourself")
    mine, _ = await _lock_pair(s, group, giver, group, receiver)
    if mine.balance < amount:
        raise CreditError(f"you have {mine.balance.normalize():f} credits in {group.name}")
    free = min(mine.free, amount)
    await _apply(s, group, giver, -amount, -free, "give", counterparty_id=receiver, actor_id=giver, note=note)
    await _apply(s, group, receiver, amount, free, "receive", counterparty_id=giver, actor_id=giver, note=note)
    return amount


async def move(s: AsyncSession, user_id: uuid.UUID, source: Group, target: Group, value: float) -> Decimal:
    amount = amount_of(value)
    if source.id == target.id:
        raise CreditError("choose two different groups")
    if not (source.credits_enabled and target.credits_enabled):
        raise CreditError("both groups must use credits")
    if not source.credits_out:
        raise CreditError(f"{source.name} does not let credits go to other groups")
    if not target.credits_in:
        raise CreditError(f"{target.name} does not take credits from other groups")
    row, _ = await _lock_pair(s, source, user_id, target, user_id)
    earned = row.balance - row.free
    if earned < amount:
        raise CreditError(f"you can move only earned credits: you have {max(earned, ZERO).normalize():f} earned "
                          f"credits in {source.name} (free credits stay in their group)")
    await _apply(s, source, user_id, -amount, ZERO, "move_out", actor_id=user_id, note=f"to {target.name}")
    await _apply(s, target, user_id, amount, ZERO, "move_in", actor_id=user_id, note=f"from {source.name}")
    return amount


async def grant(s: AsyncSession, group: Group, actor: uuid.UUID, receiver: uuid.UUID, value: float, kind: str,
                note: str, request_id: uuid.UUID | None = None) -> Decimal:
    """Free credits from the coordinator (kind "grant", or "request" for an approved request)."""
    amount = amount_of(value)
    if actor == receiver:
        raise CreditError("a coordinator cannot give free credits to themselves")
    await balance_row(s, group, receiver, lock=True)
    await _apply(s, group, receiver, amount, amount, kind, actor_id=actor, request_id=request_id, note=note)
    return amount


async def start_all(s: AsyncSession, group: Group, user_ids: list[uuid.UUID]) -> None:
    """Credits were turned on: every member gets their start credits (once)."""
    for uid in user_ids:
        await balance_row(s, group, uid)


# --------------------------------------------------------------------------
# What a job costs
# --------------------------------------------------------------------------


async def job_credits(s: AsyncSession, job: Job, group: Group) -> dict[str, Any] | None:
    """Expected and highest cost of a job, and what it has cost so far."""
    if not group.credits_enabled:
        return None
    tasks = (await s.execute(select(func.count()).where(Task.job_id == job.id))).scalar_one()
    limit = job.max_runtime_s or 3600
    memory = job.memory_mb
    most = cost(group, cpu=job.cpu, memory_mb=memory, gpus=job.gpus, seconds=limit) * tasks
    sizing = job.sizing or {}
    expected = None
    if sizing.get("runs") and not sizing.get("first_run"):
        # Learned time limits are 3 × the longest run: a third of it is what a run usually takes.
        expected = cost(group, cpu=job.cpu, memory_mb=memory / 1.25, gpus=job.gpus, seconds=limit / 3) * tasks
    spent = (await s.execute(
        select(func.coalesce(func.sum(CreditEntry.amount), 0))
        .where(CreditEntry.job_id == job.id, CreditEntry.user_id == job.submitter_id,
               CreditEntry.kind == "spend"))).scalar_one()
    return {"expected": float(q(expected)) if expected is not None else None, "most": float(q(most)),
            "spent": float(-q(spent))}
