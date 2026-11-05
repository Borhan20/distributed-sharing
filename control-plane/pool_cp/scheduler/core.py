"""Matching, leases, fencing and retries (plan §7.1-7.4).

Every function takes an AsyncSession and runs inside the caller's transaction.
The fencing rule (docs/design/phase1-implementation.md §4): a message about an
attempt is applied only if the attempt belongs to the sending device, its
epoch matches, it is the task's current attempt, and it is still active.
"""

from __future__ import annotations

import base64
import json
import logging
import random
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from pool_protocol import messages as pm

from .. import audit, builtin_registry, credits
from ..ca import EnvelopeSigner
from ..config import Settings
from ..image_access import has_credential
from ..models import (
    ACTIVE_ATTEMPT_STATES,
    Attempt,
    Device,
    DeviceShare,
    Group,
    Job,
    Membership,
    Task,
    TaskLog,
    UsageLedger,
)
from ..registry import RegistryError, parse_image
from ..timeutil import utcnow
from . import sizing
from .usage import bounded_usage

log = logging.getLogger(__name__)

SHARE_ROLES = ("coordinator", "admin", "member", "contributor")


@dataclass
class Fenced:
    attempt: Attempt
    task: Task


class FenceError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------


async def eligible_groups(session: AsyncSession, device: Device) -> list[tuple[Group, dict[str, Any]]]:
    """Groups this device may serve now, in the agent's priority order.

    Needs (1) a server-side share by the owner, (2) the owner still a member
    with a role that may share, (3) an active group, and (4) the agent's local
    policy reporting the group as active right now.
    """
    reported = [s for s in (device.reported_shares or []) if s.get("active_now")]
    if not reported or device.owner_active is None:
        return []
    names = [s["group"] for s in reported]
    rows = (await session.execute(
        select(Group)
        .join(DeviceShare, DeviceShare.group_id == Group.id)
        .join(Membership, (Membership.group_id == Group.id) & (Membership.user_id == device.owner_id))
        .where(DeviceShare.device_id == device.id, DeviceShare.enabled.is_(True), Group.state == "active",
               Group.name.in_(names), Membership.role.in_(SHARE_ROLES))
    )).scalars().all()
    by_name = {g.name: g for g in rows}
    return [(by_name[s["group"]], s) for s in reported if s["group"] in by_name]


# SECURITY-SENSITIVE: grants a device access to group work. Needs security review (plan §8.10).
async def sync_shares_from_policy(session: AsyncSession, device: Device) -> list[str]:
    """One-step sharing: record a server-side share for each group the device's local policy offers.

    Only for active groups where the owner is a member with a role that may
    share, and only when no share row exists yet. A share the owner turned off
    in the web UI keeps its row (enabled = false) and therefore stays off.
    Returns the names of the groups that were newly shared.
    """
    if device.state != "active":
        return []
    names = sorted({s["group"] for s in device.reported_shares or [] if isinstance(s.get("group"), str)})
    if not names:
        return []
    candidates = (await session.execute(
        select(Group.id, Group.name)
        .join(Membership, (Membership.group_id == Group.id) & (Membership.user_id == device.owner_id))
        .outerjoin(DeviceShare, (DeviceShare.group_id == Group.id) & (DeviceShare.device_id == device.id))
        .where(Group.name.in_(names), Group.state == "active", Membership.role.in_(SHARE_ROLES),
               DeviceShare.device_id.is_(None))
    )).all()
    created = []
    for group_id, name in candidates:
        # ON CONFLICT: a hello and a heartbeat from the same device may race.
        inserted = (await session.execute(
            pg_insert(DeviceShare).values(device_id=device.id, group_id=group_id, enabled=True)
            .on_conflict_do_nothing().returning(DeviceShare.group_id)
        )).scalar()
        if inserted is None:
            continue
        await audit.record(session, actor_type="device", actor_id=device.id, action="device.share",
                           group_id=group_id, device_id=device.id, target_type="device", target_id=device.id,
                           details={"source": "local_policy"})
        created.append(name)
    if created:
        log.info("device %s shared with %s from its local policy", device.id, ", ".join(created))
    return created


def _tier_rank(tiers: list[str]) -> int:
    return max((pm.TIER_RANK[t] for t in tiers if t in pm.TIER_RANK), default=-1)


async def claim_task(session: AsyncSession, settings: Settings, signer: EnvelopeSigner, device: Device,
                     ready: pm.Ready) -> pm.Offer | None:
    """Pick one pending task for this device and create an attempt (lease) for it."""
    if device.state != "active":
        return None
    groups = await eligible_groups(session, device)
    if not groups:
        return None
    caps = device.capabilities or {}
    allowed = [t for t in ready.tiers if t in (caps.get("tiers") or [])]
    tier_rank = _tier_rank(allowed)
    gpu_tier_rank = _tier_rank([t for t in allowed if t in (caps.get("gpu_tiers") or [])])
    if tier_rank < 0:
        return None
    max_gpu_mem = max((g.get("memory_mb", 0) for g in caps.get("gpus") or []), default=0)
    now = utcnow()
    # The agent's share settings per group, in its priority order (plan §6.4, §7.7).
    shares = [{"group_id": str(g.id), "prio": i, "accept_all": sh.get("accept_from", "all-members") == "all-members",
               "handles": [] if sh.get("accept_from", "all-members") == "all-members" else list(sh["accept_from"]),
               "accept_new": bool(sh.get("accept_new_members", False))} for i, (g, sh) in enumerate(groups)]

    params = {
        "shares": json.dumps(shares),
        "since": now - timedelta(days=7),
        "now": now,
        "cpu": ready.free.cpu,
        "mem": ready.free.memory_mb,
        "disk": ready.free.disk_mb if ready.free.disk_mb else 2**31 - 1,
        "gpus": ready.free.gpus,
        "gpu_mem": max_gpu_mem,
        "tier": tier_rank,
        "gpu_tier": gpu_tier_rank,
        "os": caps.get("os", device.os),
        "arch": caps.get("arch") or device.arch,
        # Placement (pool-decides §4.4): images this computer has, and whether it often loses tasks.
        "images": list(ready.images),
        "unreliable": (device.reliability if device.reliability is not None else 1.0) < 0.5,
        "dedicated": device.org_owned_dedicated,
        "device": device.id,
        "decline_since": now - timedelta(seconds=settings.decline_cooldown_s),
        # A first run gets a computer's whole free memory (sizing). For its first minute it waits for one of
        # the computers that offer the most; after that, any computer will do.
        "best_mem": await _best_offered_memory(session, settings, [g.name for g, _ in groups], now),
        "first_wait": now - timedelta(seconds=FIRST_RUN_PREFERENCE_S),
    }
    # Step 1: rank the active jobs this device may run (jobs are few; tasks can
    # be many). Order: the agent's group priority, then fair share (least
    # CPU-seconds in the last 7 days for the submitter in that group, plan
    # §7.7), then submission order. All hard filters except per-task ones.
    query = text("""
        WITH sh AS (
            SELECT * FROM jsonb_to_recordset(CAST(:shares AS jsonb))
                AS x(group_id uuid, prio int, accept_all bool, handles jsonb, accept_new bool)
        ), usage AS (
            -- Use counts CPU time and GPU time (one GPU hour as 10 CPU hours, as in credits): asking for a GPU
            -- has a price in fair share too.
            SELECT group_id, user_id, sum(cpu_seconds + 10 * gpu_seconds) AS cpu_s
            FROM usage_ledger
            WHERE created_at > :since AND group_id IN (SELECT group_id FROM sh) AND NOT pool_paid
            GROUP BY group_id, user_id
        ), bal AS (
            SELECT group_id, user_id, balance FROM credit_balances WHERE group_id IN (SELECT group_id FROM sh)
        )
        SELECT j.id, u.handle, (m.probation_until IS NOT NULL AND m.probation_until > :now) AS on_probation,
               coalesce((j.sizing->>'first_run')::boolean, false) AS first_run, j.memory_mb, j.group_id
        FROM jobs j
        JOIN sh ON sh.group_id = j.group_id
        JOIN groups g ON g.id = j.group_id
        JOIN users u ON u.id = j.submitter_id
        JOIN memberships m ON m.group_id = j.group_id AND m.user_id = j.submitter_id
        LEFT JOIN usage us ON us.group_id = j.group_id AND us.user_id = j.submitter_id
        LEFT JOIN bal ON bal.group_id = j.group_id AND bal.user_id = j.submitter_id
        WHERE j.state IN ('pending', 'running')
          -- Credits (docs/design/credits.md §3.7): out of credits, a member waits when the group says so.
          AND NOT (g.credits_enabled AND g.out_of_credits = 'wait' AND coalesce(bal.balance, g.credit_start) <= 0)
          AND NOT (j.id = ANY(CAST(:skip AS uuid[])))  -- jobs already tried for this computer (paging)
          AND j.cpu <= :cpu AND j.memory_mb <= :mem AND j.disk_mb <= :disk AND j.gpus <= :gpus
          AND j.gpu_memory_mb <= :gpu_mem
          AND ((j.gpus = 0 AND j.min_tier_rank <= :tier) OR (j.gpus > 0 AND j.min_tier_rank <= :gpu_tier))
          AND (j.os IS NULL OR j.os = :os)
          -- The image has a version for this computer's CPU type (pool-decides §4.1); NULL: not known.
          AND (j.architectures IS NULL OR CAST(:arch AS text) = ANY(j.architectures))
          AND (j.data_class <> 'confidential' OR (:dedicated AND :tier >= 2))
          AND NOT j.egress
          AND (sh.accept_all OR sh.handles ? u.handle)
          AND (m.probation_until IS NULL OR m.probation_until <= :now OR sh.accept_new)
          -- A task it may start now. A first run starts with part 0 alone, until part 0 has ended in any way.
          AND EXISTS (SELECT 1 FROM tasks p WHERE p.job_id = j.id AND p.state = 'pending' AND p.not_before <= :now
                      AND (NOT coalesce((j.sizing->>'first_run')::boolean, false) OR p.index = 0
                 OR EXISTS (SELECT 1 FROM tasks z WHERE z.job_id = j.id AND z.index = 0
                            AND z.state IN ('succeeded', 'failed', 'cancelled'))))
          -- A first run (sizing): one per person at a time, on a computer that offers about the most memory.
          AND NOT (coalesce((j.sizing->>'first_run')::boolean, false) AND (
                (:mem < :best_mem * 0.75 AND j.created_at > :first_wait)
                OR EXISTS (SELECT 1 FROM attempts fa JOIN tasks ft ON ft.id = fa.task_id
                           JOIN jobs fj ON fj.id = ft.job_id
                           WHERE fj.submitter_id = j.submitter_id AND fa.state IN ('assigned', 'running')
                             AND coalesce((fa.resources->>'first_run')::boolean, false))))
        -- The owner's group priority; members with credits before those without (credits §3.8); on a computer
        -- with a GPU, GPU work first (GPU time counts in fair share); then fair share in bands that double
        -- (0-1 h, 1-3 h, 3-7 h, ...), and inside a band the best fit (pool-decides §4.4): work that needs more
        -- than half of this computer's free memory first; then exact fair share; then tasks whose image this
        -- computer already has; short tasks first on a computer that often loses them; then the oldest.
        -- The best fit only breaks near-ties: asking for more memory never jumps people who used much less.
        ORDER BY sh.prio, (g.credits_enabled AND coalesce(bal.balance, g.credit_start) <= 0),
                 (j.gpus > 0) DESC,
                 floor(ln(1 + coalesce(us.cpu_s, 0) / 3600.0) / ln(2)),
                 (j.memory_mb > :mem / 2.0) DESC,
                 coalesce(us.cpu_s, 0),
                 (split_part(j.image, '@', 2) = ANY(CAST(:images AS text[]))) DESC,
                 CASE WHEN :unreliable THEN coalesce(j.max_runtime_s, 3600) ELSE 0 END,
                 j.created_at
        LIMIT 10
        """)
    # Step 2: lock one runnable task of the best job with SKIP LOCKED, so
    # concurrent agents never get the same task and never wait for each other
    # (plan D7). Index order (ix_tasks_job_pending) keeps this O(1) for large
    # arrays. Tasks that already failed on this device are only taken when
    # nothing else is left.
    # A first run starts with part 0 alone; the other parts are sized from it (sizing.learn_first_run).
    pick = """
        SELECT t.id FROM tasks t
        WHERE t.job_id = :job AND t.state = 'pending' AND t.not_before <= :now
          AND (NOT :first_run OR t.index = 0
               OR EXISTS (SELECT 1 FROM tasks z WHERE z.job_id = :job AND z.index = 0
                          AND z.state IN ('succeeded', 'failed', 'cancelled')))
          AND NOT EXISTS (
              SELECT 1 FROM attempts a
              WHERE a.task_id = t.id AND a.device_id = :device
                AND (a.state = 'failed' OR (a.state = 'declined' AND a.created_at > :decline_since)))
        ORDER BY t.index
        LIMIT 1
        FOR UPDATE OF t SKIP LOCKED
    """
    fallback = """
        SELECT t.id FROM tasks t
        WHERE t.job_id = :job AND t.state = 'pending' AND t.not_before <= :now
          AND (NOT :first_run OR t.index = 0
               OR EXISTS (SELECT 1 FROM tasks z WHERE z.job_id = :job AND z.index = 0
                          AND z.state IN ('succeeded', 'failed', 'cancelled')))
          AND NOT EXISTS (
              SELECT 1 FROM attempts a
              WHERE a.task_id = t.id AND a.device_id = :device
                AND a.state = 'declined' AND a.created_at > :decline_since)
        ORDER BY t.index
        LIMIT 1
        FOR UPDATE OF t SKIP LOCKED
    """
    row = None
    group_free: dict[uuid.UUID, int] = {}
    device.last_ready_at = now
    tried: list[uuid.UUID] = []
    for _page in range(CLAIM_PAGES):
        jobs = (await session.execute(query, {**params, "skip": tried})).all()
        if not jobs:
            break
        tried += [j[0] for j in jobs]
        row = await _first_startable(session, device, ready, groups, jobs, group_free, pick, fallback, now,
                                     params["decline_since"])
        if row is not None:
            break
    if row is None:
        return None
    task_id, submitter_handle, on_probation = row
    task = await session.get(Task, task_id)
    group = next(g for g, _ in groups if g.id == task.group_id)
    return await _assign(session, settings, signer, device, task, submitter_handle, group, on_probation, now,
                         free_memory_mb=await _group_free_memory(session, device, group, ready.free.memory_mb))


CLAIM_PAGES = 5  # 10 jobs a page


async def _first_startable(session: AsyncSession, device: Device, ready: pm.Ready, groups: list, jobs: list,
                           group_free: dict, pick: str, fallback: str, now: datetime,
                           decline_since: datetime) -> tuple | None:
    """Lock a task of the first job in `jobs` this computer can start now: (task id, submitter, on probation)."""
    for job_id, submitter_handle, on_probation, first_run, memory_mb, group_id in jobs:
        g = next(g for g, _ in groups if g.id == group_id)
        if g.credits_enabled and g.out_of_credits == "wait" and not await _can_pay(session, g, job_id):
            continue
        if first_run:
            # A first run needs this computer's share for its group to have room (more than the last try had).
            if group_id not in group_free:
                g = next(g for g, _ in groups if g.id == group_id)
                group_free[group_id] = await _group_free_memory(session, device, g, ready.free.memory_mb)
            if group_free[group_id] < memory_mb:
                continue
        args = {"job": job_id, "now": now, "device": device.id, "decline_since": decline_since,
                "first_run": first_run}
        task_id = (await session.execute(text(pick), args)).scalar()
        if task_id is None:
            task_id = (await session.execute(text(fallback), args)).scalar()
        if task_id is not None:
            return task_id, submitter_handle, on_probation
    return None


FIRST_RUN_PREFERENCE_S = 60


def _share_memory(device: Device, group: str) -> int:
    """The memory this computer offers to the group: its share's limit, within the computer."""
    total = int((device.capabilities or {}).get("memory_mb") or 0)
    share = next((r for r in device.reported_shares or [] if r.get("group") == group), None) or {}
    offered = int((share.get("limits") or {}).get("memory_mb") or 0)
    return min(offered, total) if total else offered


async def _can_pay(session: AsyncSession, group: Group, job_id: uuid.UUID) -> bool:
    """With "wait": the submitter's balance covers an hour of this task on top of what they already run."""
    job = await session.get(Job, job_id)
    hourly = credits.cost(credits.rates(group), cpu=job.cpu, memory_mb=job.memory_mb, gpus=job.gpus, seconds=3600)
    return await credits.enough_for(session, group, job.submitter_id, hourly)


async def _best_offered_memory(session: AsyncSession, settings: Settings, groups: list[str], now: datetime) -> int:
    """The most memory an online computer offers to any of these groups. Only computers the server shares with
    the group count: a device naming a group it is not in cannot hold that group's first runs back."""
    since = now - timedelta(seconds=settings.heartbeat_interval_s * 3)
    rows = (await session.execute(
        select(Device, Group.name).join(DeviceShare, DeviceShare.device_id == Device.id)
        .join(Group, Group.id == DeviceShare.group_id)
        .where(Device.state == "active", Device.last_seen_at > since, DeviceShare.enabled,
               DeviceShare.removed.is_(False), Group.name.in_(groups)))).all()
    return max((_share_memory(d, g) for d, g in rows), default=0)


async def _group_free_memory(session: AsyncSession, device: Device, group: Group, free_mb: int) -> int:
    """What a first run on this computer may have: the free memory the device reports, but never more than its
    share for this group minus what this group's tasks already hold there (the agent checks the same)."""
    held = (await session.execute(
        select(func.coalesce(func.sum(text("coalesce((attempts.resources->>'memory_mb')::bigint, jobs.memory_mb)")), 0))
        .select_from(Attempt).join(Task, Task.id == Attempt.task_id).join(Job, Job.id == Task.job_id)
        .where(Attempt.device_id == device.id, Task.group_id == group.id,
               Attempt.state.in_(ACTIVE_ATTEMPT_STATES)))).scalar_one()
    return max(0, min(int(free_mb), _share_memory(device, group.name) - int(held)))


async def _private_image(session: AsyncSession, settings: Settings, task: Task, image: str) -> bool:
    """Whether the agent must ask for a pull token instead of pulling anonymously: the image is in the
    built-in registry, or the submitter or the group stored a token for its registry."""
    if builtin_registry.is_builtin(settings, image):
        return True
    try:
        ref = parse_image(image)
    except RegistryError:
        return False
    submitter_id = (await session.execute(select(Job.submitter_id).where(Job.id == task.job_id))).scalar_one()
    return await has_credential(session, user_id=submitter_id, group_id=task.group_id, registry=ref.registry)


async def _assign(session: AsyncSession, settings: Settings, signer: EnvelopeSigner, device: Device, task: Task,
                  submitter_handle: str, group: Group, on_probation: bool, now: datetime,
                  free_memory_mb: int | None = None) -> pm.Offer:
    epoch = task.epoch + 1
    job = await session.get(Job, task.job_id)
    # The sizes this attempt gets: the job's sizes now (pool may have changed them since the task was made).
    given = sizing.attempt_resources(job, int(task.spec.get("max_runtime_s") or 3600), free_memory_mb)
    resources = {**task.spec.get("resources", {}), "cpu": given["cpu"], "memory_mb": given["memory_mb"],
                 "disk_mb": given["disk_mb"]}
    if job.gpus and not resources.get("gpus"):  # pool chose a GPU from the image (a CUDA image)
        resources.update(gpus=job.gpus, gpu_vendor=resources.get("gpu_vendor") or "nvidia")
    spec = {**task.spec, "max_runtime_s": given["max_runtime_s"], "resources": resources}
    if group.credits_enabled:
        given["rates"] = credits.rates(group)
    attempt = Attempt(id=uuid.uuid4(), task_id=task.id, device_id=device.id, epoch=epoch, state="assigned",
                      lease_expires_at=now + timedelta(seconds=settings.lease_ttl_s), resources=given)
    session.add(attempt)
    task.epoch = epoch
    task.state = "assigned"
    task.current_attempt_id = attempt.id
    task.attempts_count += 1
    expires = now + timedelta(seconds=settings.lease_ttl_s)
    envelope = pm.TaskEnvelope(
        attempt_id=str(attempt.id), task_id=str(task.id), job_id=str(task.job_id), epoch=epoch, group=group.name,
        group_id=str(group.id), submitter=submitter_handle, submitter_probation=on_probation,
        device_id=str(device.id), issued_at=now, expires_at=expires, spec=pm.TaskSpec.model_validate(spec),
        registry_auth=await _private_image(session, settings, task, task.spec["image"]),
    )
    payload = envelope.model_dump_json().encode()
    await session.flush()
    return pm.Offer(
        attempt_id=str(attempt.id), task_id=str(task.id), epoch=epoch, expires_at=expires,
        envelope=pm.SignedEnvelope(key_id=signer.key_id, payload_b64=base64.b64encode(payload).decode(),
                                   signature_b64=signer.sign(payload)),
    )


# --------------------------------------------------------------------------
# Fencing
# --------------------------------------------------------------------------


async def fence(session: AsyncSession, device_id: uuid.UUID, attempt_id: str, epoch: int,
                states: tuple[str, ...] = ACTIVE_ATTEMPT_STATES) -> Fenced:
    try:
        aid = uuid.UUID(attempt_id)
    except ValueError:
        raise FenceError("unknown_attempt") from None
    attempt = (await session.execute(select(Attempt).where(Attempt.id == aid).with_for_update()
                                     .execution_options(populate_existing=True))).scalar_one_or_none()
    if attempt is None or attempt.device_id != device_id:
        raise FenceError("unknown_attempt")
    task = (await session.execute(select(Task).where(Task.id == attempt.task_id).with_for_update())).scalar_one()
    if attempt.epoch != epoch or task.epoch != epoch or task.current_attempt_id != attempt.id:
        raise FenceError("stale_epoch")
    if attempt.state not in states:
        raise FenceError(f"attempt_{attempt.state}")
    return Fenced(attempt, task)


def _backoff(settings: Settings, failures: int) -> timedelta:
    base = min(settings.retry_base_s * (2 ** max(failures - 1, 0)), settings.retry_max_s)
    return timedelta(seconds=base * random.uniform(0.8, 1.2))  # noqa: S311 (jitter, not security)


def _end_attempt(attempt: Attempt, state: str, reason: str, now: datetime) -> None:
    attempt.state = state
    attempt.end_reason = reason
    attempt.ended_at = now


async def _record_usage(session: AsyncSession, attempt: Attempt, task: Task, now: datetime,
                        pool_paid: bool = False, outcome: str = "ran") -> None:
    """Fair share (usage_ledger) and credits (docs/design/credits.md §3.2) for an attempt that ended."""
    if not attempt.started_at:
        return
    await credits.charge_attempt(session, attempt, task, now, outcome=outcome)
    job = await session.get(Job, task.job_id)
    seconds = max((now - attempt.started_at).total_seconds(), 0.0)
    cpu = float((attempt.resources or {}).get("cpu") or job.cpu)
    session.add(UsageLedger(group_id=task.group_id, user_id=job.submitter_id, device_id=attempt.device_id,
                            attempt_id=attempt.id, cpu_seconds=seconds * cpu, gpu_seconds=seconds * job.gpus,
                            pool_paid=pool_paid))


async def _update_reliability(session: AsyncSession, device_id: uuid.UUID, ok: bool) -> None:
    # Exponentially weighted: lost leases lower it, clean finishes raise it.
    await session.execute(
        update(Device).where(Device.id == device_id)
        .values(reliability=Device.reliability * 0.9 + (0.1 if ok else 0.0))
    )


async def _requeue_or_fail(session: AsyncSession, settings: Settings, task: Task, *, count_failure: bool,
                           crashed: bool, now: datetime, backoff: bool = True) -> None:
    """Return a task to the queue after its attempt ended without success."""
    task.current_attempt_id = None
    if count_failure:
        task.failures += 1
    if crashed:
        distinct = (await session.execute(
            select(func.count(func.distinct(Attempt.device_id)))
            .where(Attempt.task_id == task.id, Attempt.state == "failed")
        )).scalar_one()
        if distinct >= settings.always_fail_devices:
            task.state = "failed"
            task.finished_at = now
            task.result = {"reason": f"failed on {distinct} different devices"}
            return
    if task.failures > task.max_retries:
        task.state = "failed"
        task.finished_at = now
        task.result = {"reason": "no retries left"}
        return
    task.state = "pending"
    # Backoff only after the job itself failed; a lost device should not delay the retry.
    task.not_before = now + _backoff(settings, task.failures) if (count_failure and backoff) else now


# --------------------------------------------------------------------------
# Agent message handlers
# --------------------------------------------------------------------------


async def handle_accept(session: AsyncSession, settings: Settings, device_id: uuid.UUID, msg: pm.Accept) -> None:
    f = await fence(session, device_id, msg.attempt_id, msg.epoch, ("assigned",))
    f.attempt.tier = msg.tier
    f.attempt.lease_expires_at = utcnow() + timedelta(seconds=settings.lease_ttl_s)


async def handle_decline(session: AsyncSession, device_id: uuid.UUID, msg: pm.Decline) -> None:
    f = await fence(session, device_id, msg.attempt_id, msg.epoch, ("assigned",))
    now = utcnow()
    _end_attempt(f.attempt, "declined", msg.reason, now)
    f.task.state = "pending"
    f.task.current_attempt_id = None


async def handle_started(session: AsyncSession, settings: Settings, device_id: uuid.UUID, msg: pm.Started) -> Fenced:
    f = await fence(session, device_id, msg.attempt_id, msg.epoch)
    now = utcnow()
    if f.attempt.state == "assigned":
        f.attempt.state = "running"
        f.attempt.started_at = now
        f.task.state = "running"
        job = await session.get(Job, f.task.job_id)
        if job.state == "pending":
            job.state = "running"
        await audit.record(session, actor_type="device", actor_id=device_id, action="attempt.start",
                           group_id=f.task.group_id, device_id=device_id, target_type="attempt",
                           target_id=f.attempt.id, details={"task_id": str(f.task.id), "epoch": f.attempt.epoch,
                                                            "job_id": str(f.task.job_id)})
    f.attempt.lease_expires_at = now + timedelta(seconds=settings.lease_ttl_s)
    return f


async def handle_progress(session: AsyncSession, device_id: uuid.UUID, msg: pm.Progress) -> None:
    f = await fence(session, device_id, msg.attempt_id, msg.epoch)
    stmt = text("INSERT INTO task_logs (attempt_id, seq, stream, chunk) VALUES (:a, :s, :st, :c) "
                "ON CONFLICT ON CONSTRAINT uq_task_log_seq DO NOTHING")
    await session.execute(stmt, {"a": f.attempt.id, "s": msg.seq, "st": msg.stream,
                                 "c": msg.log_chunk.replace("\x00", "")})


async def handle_commit(session: AsyncSession, settings: Settings, device_id: uuid.UUID,
                        msg: pm.Commit) -> pm.CommitAck:
    # Idempotent replay: the same attempt already committed with this key.
    try:
        aid = uuid.UUID(msg.attempt_id)
    except ValueError:
        return pm.CommitAck(attempt_id=msg.attempt_id, accepted=False, reason="unknown_attempt")
    prior = await session.get(Attempt, aid)
    if prior is not None and prior.device_id == device_id and prior.commit_key == msg.idempotency_key:
        return pm.CommitAck(attempt_id=msg.attempt_id, accepted=True, reason="duplicate")
    try:
        f = await fence(session, device_id, msg.attempt_id, msg.epoch)
    except FenceError as exc:
        log.info("rejected commit attempt=%s epoch=%s reason=%s", msg.attempt_id, msg.epoch, exc.reason)
        return pm.CommitAck(attempt_id=msg.attempt_id, accepted=False, reason=exc.reason)
    if str(f.task.id) != msg.task_id:
        return pm.CommitAck(attempt_id=msg.attempt_id, accepted=False, reason="task_mismatch")
    now = utcnow()
    a, t = f.attempt, f.task
    if a.started_at is None:
        a.started_at = now
    a.exit_code = msg.exit_code
    a.outputs = [o.model_dump() for o in msg.outputs]
    a.commit_key = msg.idempotency_key
    job = await session.get(Job, t.job_id)
    given = a.resources or {"cpu": job.cpu, "memory_mb": job.memory_mb, "disk_mb": job.disk_mb}
    if msg.usage is not None:
        a.usage = bounded_usage(msg.usage, cpu=given["cpu"], memory_mb=given["memory_mb"], disk_mb=given["disk_mb"],
                                exit_code=msg.exit_code,
                                observed_s=(now - (a.started_at or a.created_at or now)).total_seconds())
    oom = bool(a.usage and a.usage.get("oom_killed"))
    timed_out = msg.exit_code != 0 and (msg.error or "").startswith("max_runtime_exceeded")
    resized = False
    try:
        # A size pool chose was too small: pool tries again with more (sizing.grow), and this try is not the
        # person's failure. It still counts for fair share until a try with the bigger size succeeds.
        resized = (oom and sizing.grow(job, "memory", given)) or (timed_out and sizing.grow(job, "time", given))
        if given.get("first_run") and not resized:
            parts = (await session.execute(select(func.count()).where(Task.job_id == job.id))).scalar_one()
            observed = (now - (a.started_at or now)).total_seconds()
            sizing.learn_first_run(job, a.usage, given, t.index, parts, observed_s=observed,
                                   ceiling=await sizing.group_ceiling(session, await session.get(Group, job.group_id)))
    except Exception:  # sizing must never cost a result: log it, and keep the sizes as they are
        log.exception("sizing failed for attempt %s; sizes unchanged", a.id)
    await _record_usage(session, a, t, now)
    if msg.exit_code == 0:
        _end_attempt(a, "succeeded", "exit_0", now)
        t.state = "succeeded"
        t.finished_at = now
        t.current_attempt_id = a.id  # the winning attempt stays current
        t.result = {"attempt_id": str(a.id), "epoch": a.epoch, "exit_code": 0, "outputs": a.outputs,
                    "usage": a.usage, "given": a.resources}
        await _update_reliability(session, device_id, True)
        # The bigger size worked: the tries that were too small were pool's mistake. They leave fair share, and
        # pool gives the person back what they paid for them (the owner keeps what they earned).
        resized_tries = (await session.execute(
            select(Attempt).join(UsageLedger, UsageLedger.attempt_id == Attempt.id)
            .where(Attempt.task_id == t.id, Attempt.end_reason.in_(("oom_resized", "timeout_resized")),
                   UsageLedger.pool_paid.is_(False)))).scalars().all()
        for tried in resized_tries:
            await credits.refund_pool_retry(session, tried, t, now)
        await session.execute(
            update(UsageLedger).where(UsageLedger.attempt_id.in_([x.id for x in resized_tries]))
            .values(pool_paid=True))
    elif resized:
        # Not the person's failure: no failure counted, no backoff, and not a failing device either.
        _end_attempt(a, "preempted", "oom_resized" if oom else "timeout_resized", now)
        await _requeue_or_fail(session, settings, t, count_failure=False, crashed=False, now=now)
    else:
        _end_attempt(a, "failed", "oom" if oom else f"exit_{msg.exit_code}", now)
        await _requeue_or_fail(session, settings, t, count_failure=True, crashed=True, now=now)
        if t.state == "failed":
            error = msg.error
            if oom and not error:
                error = f"out of memory: the program needed more than its {given['memory_mb']} MiB"
            t.result = {**(t.result or {}), "attempt_id": str(a.id), "exit_code": msg.exit_code,
                        "error": error, "usage": a.usage, "oom_killed": oom, "given": a.resources}
    await audit.record(session, actor_type="device", actor_id=device_id, action="attempt.end",
                       group_id=t.group_id, device_id=device_id, target_type="attempt", target_id=a.id,
                       details={"task_id": str(t.id), "epoch": a.epoch, "exit_code": msg.exit_code,
                                "task_state": t.state})
    return pm.CommitAck(attempt_id=msg.attempt_id, accepted=True)


NO_PENALTY_ABORTS = {"owner_returned", "kill_switch", "policy", "agent_shutdown", "disk_full"}


async def handle_abort(session: AsyncSession, settings: Settings, device_id: uuid.UUID, msg: pm.Abort) -> None:
    f = await fence(session, device_id, msg.attempt_id, msg.epoch)
    now = utcnow()
    penalty = msg.reason not in NO_PENALTY_ABORTS
    # The owner took the computer back (or it stopped): nothing is charged for the lost part.
    await _record_usage(session, f.attempt, f.task, now, outcome="ran" if penalty else "lost")
    _end_attempt(f.attempt, "failed" if penalty else "preempted", f"abort_{msg.reason}", now)
    await _requeue_or_fail(session, settings, f.task, count_failure=penalty, crashed=False, now=now)
    if f.task.state == "failed" and msg.detail:
        # Keep the device's explanation where people look: the task's result on the job page.
        f.task.result = {**(f.task.result or {}), "attempt_id": str(f.attempt.id), "error": msg.detail[:500]}
    await audit.record(session, actor_type="device", actor_id=device_id, action="attempt.abort",
                       group_id=f.task.group_id, device_id=device_id, target_type="attempt",
                       target_id=f.attempt.id, details={"reason": msg.reason, "detail": msg.detail[:500]})


async def renew_leases(session: AsyncSession, settings: Settings, device_id: uuid.UUID,
                       running: list[pm.RunningAttempt]) -> tuple[list[str], list[str]]:
    """Heartbeat: renew leases the agent still holds; everything else is stale (agent must stop it)."""
    renewed: list[str] = []
    stale: list[str] = []
    if not running:
        return renewed, stale
    ids = []
    for r in running:
        try:
            ids.append(uuid.UUID(r.attempt_id))
        except ValueError:
            stale.append(r.attempt_id)
    rows = {a.id: (a, t) for a, t in (await session.execute(
        select(Attempt, Task).join(Task, Task.id == Attempt.task_id).where(Attempt.id.in_(ids))
    )).all()}
    expires = utcnow() + timedelta(seconds=settings.lease_ttl_s)
    good = []
    for r in running:
        try:
            aid = uuid.UUID(r.attempt_id)
        except ValueError:
            continue
        pair = rows.get(aid)
        if pair is None:
            stale.append(r.attempt_id)
            continue
        a, t = pair
        if (a.device_id == device_id and a.epoch == r.epoch == t.epoch and t.current_attempt_id == a.id
                and a.state in ACTIVE_ATTEMPT_STATES):
            good.append(a.id)
            renewed.append(r.attempt_id)
        else:
            stale.append(r.attempt_id)
    if good:
        await session.execute(update(Attempt).where(Attempt.id.in_(good), Attempt.state.in_(ACTIVE_ATTEMPT_STATES))
                              .values(lease_expires_at=expires))
    return renewed, stale


async def reconcile_on_hello(session: AsyncSession, settings: Settings, device_id: uuid.UUID,
                             running: list[pm.RunningAttempt]) -> list[str]:
    """New session: attempts the server thinks are active here but the agent no longer runs are lost."""
    renewed, stale = await renew_leases(session, settings, device_id, running)
    keep = {uuid.UUID(a) for a in renewed}
    active = (await session.execute(
        select(Attempt).where(Attempt.device_id == device_id, Attempt.state.in_(ACTIVE_ATTEMPT_STATES))
        .with_for_update().execution_options(populate_existing=True)
    )).scalars().all()
    for a in active:
        if a.id not in keep:
            await lose_attempt(session, settings, a, reason="agent_restarted")
    return stale


TIME_LIMIT_GRACE_S = 300


async def enforce_time_limits(session: AsyncSession, settings: Settings, limit: int = 200) -> int:
    """Attempts that run well past their time limit end on the server too (the agent stops them itself; this
    is for one that does not): the task goes back to the queue, and nothing more is charged for it."""
    now = utcnow()
    rows = (await session.execute(
        select(Attempt).where(
            Attempt.state == "running", Attempt.started_at.isnot(None),
            text("attempts.started_at + make_interval(secs => coalesce((attempts.resources->>'max_runtime_s')"
                 f"::float8, 86400) + {TIME_LIMIT_GRACE_S}) < :now"))
        .with_for_update(skip_locked=True).limit(limit), {"now": now})).scalars().all()
    for a in rows:
        await lose_attempt(session, settings, a, reason="time_limit")
    return len(rows)


async def lose_attempt(session: AsyncSession, settings: Settings, attempt: Attempt, reason: str) -> None:
    task = (await session.execute(select(Task).where(Task.id == attempt.task_id).with_for_update())).scalar_one()
    now = utcnow()
    was_running = attempt.state == "running"
    await _record_usage(session, attempt, task, now, outcome="lost")
    _end_attempt(attempt, "lost", reason, now)
    if task.current_attempt_id == attempt.id and task.state in ("assigned", "running"):
        await _requeue_or_fail(session, settings, task, count_failure=was_running, crashed=False, now=now,
                               backoff=False)
    if was_running:
        await _update_reliability(session, attempt.device_id, False)


async def api_alive(session: AsyncSession, settings: Settings) -> bool:
    """True if any API process wrote a heartbeat recently (see app._api_heartbeat)."""
    latest = (await session.execute(text("SELECT max(at) FROM service_heartbeats WHERE service = 'api'"))).scalar()
    return bool(latest and latest > utcnow() - timedelta(seconds=3 * settings.api_heartbeat_s))


async def expire_leases(session: AsyncSession, settings: Settings, limit: int = 500) -> int:
    """Scheduler loop: attempts whose lease ran out go back to the queue (plan §7.4)."""
    now = utcnow()
    rows = (await session.execute(
        select(Attempt).where(Attempt.state.in_(ACTIVE_ATTEMPT_STATES), Attempt.lease_expires_at < now)
        .order_by(Attempt.lease_expires_at).limit(limit).with_for_update(skip_locked=True)
    )).scalars().all()
    for a in rows:
        await lose_attempt(session, settings, a, reason="lease_expired")
    return len(rows)


async def cancel_tasks(session: AsyncSession, task_filter, reason: str) -> list[tuple[str, uuid.UUID]]:
    """Cancel non-terminal tasks. Returns (attempt_id, device_id) of attempts to stop."""
    now = utcnow()
    tasks = (await session.execute(
        select(Task).where(task_filter, Task.state.in_(("pending", "assigned", "running"))).with_for_update()
    )).scalars().all()
    to_stop: list[tuple[str, uuid.UUID]] = []
    for t in tasks:
        if t.current_attempt_id:
            a = (await session.execute(select(Attempt).where(Attempt.id == t.current_attempt_id).with_for_update()
                                       .execution_options(populate_existing=True))).scalar_one_or_none()
            if a and a.state in ACTIVE_ATTEMPT_STATES:
                # The computer worked until now: it counts for fair share, and it is paid for in credits.
                await _record_usage(session, a, t, now)
                _end_attempt(a, "cancelled", reason, now)
                to_stop.append((str(a.id), a.device_id))
        t.state = "cancelled"
        t.finished_at = now
    return to_stop


async def rollup_jobs(session: AsyncSession) -> int:
    """Set job state from its tasks. Returns the number of jobs that changed."""
    changed = 0
    jobs = (await session.execute(select(Job).where(Job.state.in_(("pending", "running"))))).scalars().all()
    for job in jobs:
        counts = dict((await session.execute(
            select(Task.state, func.count()).where(Task.job_id == job.id).group_by(Task.state)
        )).all())
        total = sum(counts.values())
        active = counts.get("pending", 0) + counts.get("assigned", 0) + counts.get("running", 0)
        new = job.state
        if total and active == 0:
            if counts.get("failed"):
                new = "failed"
            elif counts.get("cancelled"):
                new = "cancelled"
            else:
                new = "succeeded"
            job.finished_at = utcnow()
        elif counts.get("running") or counts.get("assigned") or counts.get("succeeded") or counts.get("failed"):
            new = "running"
        if new != job.state:
            job.state = new
            changed += 1
            if new in ("succeeded", "failed", "cancelled"):
                await audit.record(session, actor_type="system", actor_id=None, action="job.finish",
                                   group_id=job.group_id, target_type="job", target_id=job.id,
                                   details={"state": new, "tasks": counts})
    return changed


async def expire_groups(session: AsyncSession) -> list[tuple[str, uuid.UUID]]:
    """Groups past `expires_at` stop running jobs (plan §6.3 item 5). Data is kept; deletion is separate."""
    now = utcnow()
    groups = (await session.execute(
        select(Group).where(Group.state == "active", Group.expires_at.is_not(None), Group.expires_at < now)
        .with_for_update(skip_locked=True)
    )).scalars().all()
    stops: list[tuple[str, uuid.UUID]] = []
    for g in groups:
        g.state = "expired"
        stops += await cancel_tasks(session, Task.group_id == g.id, "group_expired")
        await audit.record(session, actor_type="system", actor_id=None, action="group.expire", group_id=g.id,
                           target_type="group", target_id=g.id, details={"retention_days": g.retention_days})
    return stops


def envelope_payload(offer: pm.Offer) -> dict[str, Any]:
    return json.loads(base64.b64decode(offer.envelope.payload_b64))


async def append_agent_log(session: AsyncSession, attempt_id: uuid.UUID, text_: str) -> None:
    seq = (await session.execute(
        select(func.coalesce(func.max(TaskLog.seq), -1) + 1).where(TaskLog.attempt_id == attempt_id,
                                                                     TaskLog.stream == "system")
    )).scalar_one()
    session.add(TaskLog(attempt_id=attempt_id, seq=seq, stream="system", chunk=text_))
