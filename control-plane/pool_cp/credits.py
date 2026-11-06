"""Credits: share to earn, use to spend (docs/design/credits.md).

- One credit is one CPU core for one hour. Memory and GPUs count as CPU hours at the group's rates (within
  RATE_LIMITS). A run is billed at the rates in force when it started.
- When a task ends on someone else's computer, its submitter pays and the computer's owner earns the same
  amount. Running on your own computer costs nothing and earns nothing. A run is never billed for longer than its
  time limit.
- A size pool chose is paid by what the task used; a size the person set by what it reserved.
- Reversed in full (nothing is made, nothing is lost): a try that was too small for a size pool chose, once a
  bigger try succeeded, and the part of a task lost when its computer went away.
- Free credits (start credits, grants, approved requests, "earn while waiting") stay in their group. Credits an
  owner earns from someone who paid with credits they did not have (a debt) stay in the group too. Only the rest
  can move, and only to a group whose coordinator accepts credits from this group.

Every change is one row in credit_entries (rows are never changed) and one update of the member's row in
credit_balances, in the same transaction. Every balance change locks the balance rows first (two at a time in
_lock_pair's fixed order, so they cannot deadlock), so the same credits are never spent twice.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Attempt, CreditBalance, CreditEntry, Device, DeviceShare, Group, Job, Membership, Task

log = logging.getLogger(__name__)

DEFAULT_RATES = {"cpu_hour": 1, "gib_hours_per_credit": 4, "gpu_hour": 10}
# What a coordinator may set. Narrow on purpose: a group cannot price a CPU hour at 1000 credits and move them.
RATE_LIMITS = {"cpu_hour": (0.1, 10.0), "gib_hours_per_credit": (0.5, 64.0), "gpu_hour": (0.0, 100.0)}
MAX_START = Decimal(100)  # start credits per member, at most
# New groups: credits on for peer groups, off where one side shares and the other uses (credits.md §3.9).
TEMPLATE_CREDITS: dict[str, tuple[bool, int]] = {
    "research-team": (False, 20), "dev-team": (True, 20), "student-group": (True, 20), "course": (False, 20),
    "hackathon": (True, 10),
}
CHARGE_EVERY_S = 3600  # long tasks are charged every hour while they run
TIME_SLACK_S = 60.0  # a run is billed at most its time limit plus this
STANDBY_EVERY_S = 60  # "earn while waiting" is paid once a minute
STANDBY_MAX_CORES = 16.0
STANDBY_MAX_BACK_S = 180  # after a pause, at most this much is paid back
MAX_AMOUNT = Decimal(1_000_000)
ZERO = Decimal(0)
CENT = Decimal("0.0001")


class CreditError(ValueError):
    """A credit change that is not allowed; the message says why in plain words."""


def q(x: float | Decimal) -> Decimal:
    return Decimal(str(x)).quantize(CENT, rounding=ROUND_HALF_UP)


def rates(group: Group) -> dict[str, float]:
    r = {**DEFAULT_RATES, **(group.credit_rates or {})}
    return {k: min(max(float(r[k]), lo), hi) for k, (lo, hi) in RATE_LIMITS.items()}


def cost(r: dict[str, float], *, cpu: float, memory_mb: float, gpus: int, seconds: float) -> Decimal:
    """(CPU cores × cpu_hour + memory GiB / gib_hours_per_credit + GPUs × gpu_hour) × hours."""
    per_hour = (cpu * r["cpu_hour"] + (memory_mb / 1024) / max(r["gib_hours_per_credit"], 1e-9)
                + gpus * r["gpu_hour"])
    return q(min(max(per_hour, 0.0) * max(seconds, 0.0) / 3600, float(MAX_AMOUNT)))


# --------------------------------------------------------------------------
# Balances and entries
# --------------------------------------------------------------------------


async def balance_row(s: AsyncSession, group: Group, user_id: uuid.UUID, *, lock: bool = False) -> CreditBalance:
    """The member's balance in this group (a row with 0 the first time; start credits come with give_start)."""
    stmt = select(CreditBalance).where(CreditBalance.group_id == group.id, CreditBalance.user_id == user_id)
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    row = (await s.execute(stmt)).scalar_one_or_none()
    if row is not None:
        return row
    await s.execute(pg_insert(CreditBalance).values(group_id=group.id, user_id=user_id, balance=ZERO, free=ZERO)
                    .on_conflict_do_nothing())
    return (await s.execute(stmt.execution_options(populate_existing=True))).scalar_one()


async def give_start(s: AsyncSession, group: Group, user_id: uuid.UUID) -> None:
    """Start credits, once per member and group (credits.md §3.4): when they join a group that uses credits, or
    when credits are turned on. At most MAX_START, so nobody can hand themselves a fortune."""
    if not group.credits_enabled:
        return
    await balance_row(s, group, user_id, lock=True)
    given = (await s.execute(select(func.count()).select_from(CreditEntry).where(
        CreditEntry.group_id == group.id, CreditEntry.user_id == user_id, CreditEntry.kind == "start"))).scalar_one()
    amount = min(q(group.credit_start or 0), MAX_START)
    if not given and amount > 0:
        await _apply(s, group, user_id, amount, amount, "start", note="start credits")


async def _apply(s: AsyncSession, group: Group, user_id: uuid.UUID, amount: Decimal, free: Decimal, kind: str,
                 **links: Any) -> None:
    """Change a balance by `amount`, of which `free` is free credits, and write the entry. The row is locked."""
    row = await balance_row(s, group, user_id, lock=True)
    row.balance = q(row.balance + amount)
    row.free = q(row.free + free)
    if row.free < 0:  # an accounting bug, not something a person can cause: refuse rather than hide it
        raise CreditError("credit accounting error: free credits below zero")
    s.add(CreditEntry(group_id=group.id, user_id=user_id, amount=q(amount), free=q(free), kind=kind, **links))


async def _spend(s: AsyncSession, group: Group, user_id: uuid.UUID, amount: Decimal, kind: str,
                 **links: Any) -> tuple[Decimal, Decimal]:
    """Take `amount` (> 0): free credits first, then earned ones, then a debt (the balance may go below zero).
    Returns (free part, debt part)."""
    row = await balance_row(s, group, user_id, lock=True)
    free = min(row.free, amount)
    earned = min(max(row.balance - row.free, ZERO), amount - free)
    await _apply(s, group, user_id, -amount, -free, kind, **links)
    return free, amount - free - earned


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


async def charge_attempt(s: AsyncSession, attempt: Attempt, task: Task, now: datetime, *, outcome: str,
                         note: str | None = None) -> None:
    """Charge (or settle) an attempt. outcome: "ran" (it ended: a success or the program's failure), "running"
    (hourly, while it runs) or "lost" (everything charged comes back: the computer went away, the owner took it
    back, or pool's size was too small and a bigger try then succeeded)."""
    if attempt.started_at is None:
        return
    # Charges for one attempt never overlap: lock it and read what was charged so far afresh (the scheduler's
    # hourly charge may have just changed it).
    await s.flush()
    attempt = (await s.execute(select(Attempt).where(Attempt.id == attempt.id).with_for_update()
                               .execution_options(populate_existing=True))).scalar_one()
    group = await s.get(Group, task.group_id)
    if not group.credits_enabled and not attempt.charged:
        return  # credits are off: nothing new is charged (what was charged is still settled)
    job = await s.get(Job, task.job_id)
    device = await s.get(Device, attempt.device_id)
    if device is None or device.owner_id == job.submitter_id:
        return  # your own computer: free
    given = attempt.resources or {}
    limit_s = float(given.get("max_runtime_s") or job.max_runtime_s or 3600) + TIME_SLACK_S
    since = max(attempt.started_at, group.credits_since or attempt.started_at)
    seconds = min(max((now - since).total_seconds(), 0.0), limit_s)  # never more than its time limit
    if outcome == "lost":
        total = ZERO
    elif not group.credits_enabled:
        total = attempt.charged  # turned off while it ran: keep what was charged, add nothing
    else:
        cpu, memory = _run_size(job, attempt)
        r = given.get("rates") or rates(group)  # the rates when it started: later changes never re-price it
        total = cost(r, cpu=cpu, memory_mb=memory, gpus=job.gpus, seconds=seconds)
    delta = q(total - attempt.charged)
    if delta == 0:
        attempt.charged_s = max(attempt.charged_s, seconds)
        return
    await _lock_pair(s, group, job.submitter_id, group, device.owner_id)
    links = {"job_id": job.id, "attempt_id": attempt.id, "device_id": device.id}
    if delta > 0:
        free, debt = await _spend(s, group, job.submitter_id, delta, "spend", **links,
                                  counterparty_id=device.owner_id, note=note)
        # Credits the submitter did not have (a debt) stay in the group for the owner: they cannot move.
        await _apply(s, group, device.owner_id, delta, debt, "earn", **links, counterparty_id=job.submitter_id,
                     note=note)
        attempt.charged_free = q(attempt.charged_free + free)
        attempt.earned_free = q(attempt.earned_free + debt)
    else:
        back = -delta
        # Each side gets back the same kind it gave: free credits stay free.
        free_back = min(attempt.charged_free, back)
        owner_free_back = min(attempt.earned_free, back)
        await _apply(s, group, job.submitter_id, back, free_back, "spend", **links,
                     counterparty_id=device.owner_id, note=note or "charge returned")
        await _apply(s, group, device.owner_id, -back, -owner_free_back, "earn", **links,
                     counterparty_id=job.submitter_id, note=note or "charge returned")
        attempt.charged_free = q(attempt.charged_free - free_back)
        attempt.earned_free = q(attempt.earned_free - owner_free_back)
    attempt.charged = q(attempt.charged + delta)
    attempt.charged_s = seconds


async def refund_pool_retry(s: AsyncSession, attempt: Attempt, task: Task, now: datetime) -> None:
    """A try was too small for a size pool chose, and a bigger try then succeeded: the charge for the small try
    is reversed in full. Nothing is made: whatever a program or a device claims, no credits come from nowhere."""
    await charge_attempt(s, attempt, task, now, outcome="lost",
                         note="a size pool chose was too small: this try is not charged")


async def charge_running(maker: Any, now: datetime) -> int:  # noqa: ANN401 (an async sessionmaker)
    """Charge long tasks every hour while they run, so balances stay up to date; settled when they end. One
    small transaction per attempt, oldest charge first."""
    async with maker() as s:
        ids = (await s.execute(
            select(Attempt.id).join(Task, Task.id == Attempt.task_id).join(Group, Group.id == Task.group_id)
            .where(Attempt.state == "running", Attempt.started_at.isnot(None), Group.credits_enabled,
                   text(f"extract(epoch from (:now - attempts.started_at)) - attempts.charged_s >= {CHARGE_EVERY_S}"))
            .order_by(Attempt.charged_s, Attempt.started_at).limit(500), {"now": now})).scalars().all()
    n = 0
    for aid in ids:
        try:
            async with maker() as s:
                attempt = (await s.execute(select(Attempt).where(Attempt.id == aid, Attempt.state == "running")
                                           .with_for_update(skip_locked=True))).scalar_one_or_none()
                if attempt is None:
                    continue
                await charge_attempt(s, attempt, await s.get(Task, attempt.task_id), now, outcome="running")
                await s.commit()
                n += 1
        except Exception:  # one attempt must not stop the others
            log.exception("hourly charge failed for attempt %s", aid)
    return n


async def pay_standby(maker: Any, now: datetime, heartbeat_s: int) -> int:  # noqa: ANN401
    """'Earn while waiting' (credits.md §3.3): an owner earns standby_percent of the group's CPU rate for the
    cores they share, while their computer waits for work. The computer must have asked for work recently,
    accept work from all members, run nothing, and belong to someone who may share. Its cores count at most
    STANDBY_MAX_CORES, shared out over all the groups that pay it. Free credits: they stay in the group."""
    since = now - timedelta(seconds=heartbeat_s * 3)
    async with maker() as s:
        rows = (await s.execute(
            select(DeviceShare.device_id, DeviceShare.group_id).join(Device, Device.id == DeviceShare.device_id)
            .join(Group, Group.id == DeviceShare.group_id)
            .join(Membership, (Membership.group_id == Group.id) & (Membership.user_id == Device.owner_id))
            .where(Group.credits_enabled, Group.standby_percent > 0, Group.state == "active", DeviceShare.enabled,
                   DeviceShare.removed.is_(False), Device.state == "active", Device.last_seen_at > since,
                   Device.last_ready_at > since, Membership.role.in_(("coordinator", "admin", "member",
                                                                         "contributor")))
        )).all()
    by_device: dict[uuid.UUID, list[uuid.UUID]] = {}
    for device_id, group_id in rows:
        by_device.setdefault(device_id, []).append(group_id)
    paid = 0
    for device_id, group_ids in by_device.items():
        try:
            async with maker() as s:
                device = await s.get(Device, device_id)
                busy = (await s.execute(select(func.count()).select_from(Attempt).where(
                    Attempt.device_id == device_id, Attempt.state.in_(("assigned", "running"))))).scalar_one()
                eligible = []
                for gid in group_ids:
                    group = await s.get(Group, gid)
                    reported = next((r for r in device.reported_shares or [] if r.get("group") == group.name), None)
                    if reported and reported.get("active_now") and reported.get("accept_from") == "all-members":
                        eligible.append((group, reported))
                for group, reported in eligible:
                    share = (await s.execute(select(DeviceShare).where(
                        DeviceShare.device_id == device_id, DeviceShare.group_id == group.id).with_for_update()
                    )).scalar_one()
                    if share.standby_paid_until is None or busy:  # the first time, or busy: count from now
                        share.standby_paid_until = now
                        continue
                    start = max(share.standby_paid_until, now - timedelta(seconds=STANDBY_MAX_BACK_S))
                    if busy:
                        share.standby_paid_until = now
                        continue
                    seconds = (now - start).total_seconds()
                    if seconds < STANDBY_EVERY_S:
                        continue
                    share.standby_paid_until = now
                    cores = min(float((device.capabilities or {}).get("cpu_cores") or 0)
                                * float((reported.get("limits") or {}).get("cpu_percent") or 0) / 100,
                                STANDBY_MAX_CORES)
                    amount = q(cores * rates(group)["cpu_hour"] * group.standby_percent / 100 * seconds / 3600
                               / len(eligible))
                    if amount > 0:
                        await _apply(s, group, device.owner_id, amount, amount, "standby", device_id=device.id,
                                     note="online and waiting for work")
                        paid += 1
                await s.commit()
        except Exception:
            log.exception("'earn while waiting' failed for device %s", device_id)
    return paid


# --------------------------------------------------------------------------
# Giving, moving, free credits
# --------------------------------------------------------------------------


def amount_of(value: float | Decimal) -> Decimal:
    amount = q(value)
    if amount <= 0 or amount > MAX_AMOUNT:
        raise CreditError(f"the amount must be at least 0.0001 and at most {MAX_AMOUNT:,}")
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
    """Move earned credits to another group, converted by the two groups' CPU rates. Returns what arrived."""
    amount = amount_of(value)
    if source.id == target.id:
        raise CreditError("choose two different groups")
    if not (source.credits_enabled and target.credits_enabled):
        raise CreditError("both groups must use credits")
    if not source.credits_out:
        raise CreditError(f"{source.name} does not let credits go to other groups")
    if source.name not in (target.credits_from or []):
        raise CreditError(f"{target.name} does not take credits from {source.name}: its coordinator can add "
                          f"{source.name} on the Settings tab")
    row, _ = await _lock_pair(s, source, user_id, target, user_id)
    earned = row.balance - row.free
    if earned < amount:
        raise CreditError(f"you can move only earned credits: you have {max(earned, ZERO).normalize():f} earned "
                          f"credits in {source.name} (free credits stay in their group)")
    arrived = q(amount * Decimal(str(rates(target)["cpu_hour"])) / Decimal(str(rates(source)["cpu_hour"])))
    if arrived <= 0:
        raise CreditError("the amount is too small to move")
    await _apply(s, source, user_id, -amount, ZERO, "move_out", actor_id=user_id, note=f"to {target.name}")
    await _apply(s, target, user_id, arrived, ZERO, "move_in", actor_id=user_id, note=f"from {source.name}")
    return arrived


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
        await give_start(s, group, uid)


async def enough_for(s: AsyncSession, group: Group, user_id: uuid.UUID, hourly: Decimal) -> bool:
    """With "wait" (credits.md §3.7): whether the member's balance covers an hour of this task on top of an hour
    of each task they already run in the group. Checked when a task is handed out."""
    row = (await s.execute(select(CreditBalance.balance).where(CreditBalance.group_id == group.id,
                                                                CreditBalance.user_id == user_id))).scalar()
    balance = row if row is not None else ZERO
    running = (await s.execute(
        select(Job.cpu, Job.memory_mb, Job.gpus).select_from(Attempt).join(Task, Task.id == Attempt.task_id)
        .join(Job, Job.id == Task.job_id)
        .where(Task.group_id == group.id, Job.submitter_id == user_id, Attempt.state.in_(("assigned", "running"))))
    ).all()
    r = rates(group)
    held = sum((cost(r, cpu=c, memory_mb=m, gpus=g, seconds=3600) for c, m, g in running), ZERO)
    return balance - held - hourly >= 0


# --------------------------------------------------------------------------
# What a job costs
# --------------------------------------------------------------------------


async def job_credits(s: AsyncSession, job: Job, group: Group) -> dict[str, Any] | None:
    """Expected and highest cost of a job, and what it has cost so far."""
    if not group.credits_enabled:
        return None
    tasks = (await s.execute(select(func.count()).where(Task.job_id == job.id))).scalar_one()
    limit = job.max_runtime_s or 3600
    r = rates(group)
    most = cost(r, cpu=job.cpu, memory_mb=job.memory_mb, gpus=job.gpus, seconds=limit) * tasks
    sizing = job.sizing or {}
    expected = None
    if sizing.get("runs") and not sizing.get("first_run"):
        # Learned time limits are 3 × the longest run: a third of it is what a run usually takes.
        expected = cost(r, cpu=job.cpu, memory_mb=job.memory_mb / 1.25, gpus=job.gpus, seconds=limit / 3) * tasks
    spent = (await s.execute(
        select(func.coalesce(func.sum(CreditEntry.amount), 0))
        .where(CreditEntry.job_id == job.id, CreditEntry.user_id == job.submitter_id,
               CreditEntry.kind == "spend"))).scalar_one()
    return {"expected": float(q(expected)) if expected is not None else None, "most": float(q(most)),
            "spent": float(-q(spent))}
