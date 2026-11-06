"""Why is a job waiting? Plain-language reasons for the job page (docs/design/easy-onboarding.md §6).

The checks mirror the scheduler's matching (core.eligible_groups and
core.claim_task) and the agent's limit arithmetic (agent/policy checkResources):
server-side shares, the owner's membership, the agent's reported local policy
and capabilities, and the job's requirements. Free capacity at this moment is
not stored, so when some computer could run the job the answer is "it starts
when one has room".

Reasons only describe computers by count and size, never by owner, and the
endpoint is limited to group members, who can already list the group's
computers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from pool_protocol import messages as pm

from ..config import Settings
from ..models import Attempt, AuditEvent, Device, DeviceShare, Group, Job, Membership, Task, TaskLog, User
from ..timeutil import utcnow
from .core import SHARE_ROLES


@dataclass
class Reason:
    code: str
    message: str
    hint: str | None = None


def _computers(n: int) -> str:
    return "1 computer is" if n == 1 else f"{n} computers are"


def _size(mb: float) -> str:
    if mb >= 1024:
        return f"{mb / 1024:.1f}".removesuffix(".0") + " GiB"
    return f"{int(mb)} MiB"


def _cpu(x: float) -> str:
    return f"{x:.2f}".rstrip("0").rstrip(".") + " CPU"


def _online(d: Device, heartbeat_s: int) -> bool:
    return bool(d.last_seen_at and d.last_seen_at > utcnow() - timedelta(seconds=heartbeat_s * 3))


def _reported(d: Device, group: str) -> dict[str, Any] | None:
    return next((s for s in d.reported_shares or [] if s.get("group") == group), None)


def _offer(d: Device, share: dict[str, Any]) -> tuple[float, float, float]:
    """CPU, memory MiB and disk MiB this share offers in total (agent: checkResources)."""
    caps = d.capabilities or {}
    limits = share.get("limits") or {}
    cores = caps.get("cpu_cores") or 0
    cpu = cores * (limits.get("cpu_percent") or 0) / 100
    mem = limits.get("memory_mb") or 0
    if caps.get("memory_mb"):
        mem = min(mem, caps["memory_mb"])
    return cpu, mem, limits.get("disk_mb") or 0


def _tier_rank(tiers: list[str]) -> int:
    return max((pm.TIER_RANK[t] for t in tiers if t in pm.TIER_RANK), default=-1)


def _blocker(job: Job, d: Device, share: dict[str, Any], submitter: str, on_probation: bool) -> str | None:
    """The first rule that stops this computer from running the job, or None if it can."""
    caps = d.capabilities or {}
    if job.os and caps.get("os", d.os) != job.os:
        return "os"
    if job.architectures and (caps.get("arch") or d.arch) not in job.architectures:
        return "platform"
    if _tier_rank(caps.get("tiers") or []) < job.min_tier_rank:
        return "tier"
    if job.data_class == "confidential" and not (d.org_owned_dedicated and _tier_rank(caps.get("tiers") or []) >= 2):
        return "confidential"
    accept = share.get("accept_from", "all-members")
    if accept != "all-members" and submitter not in (accept or []):
        return "submitter"
    if on_probation and not share.get("accept_new_members"):
        return "probation"
    cpu, mem, disk = _offer(d, share)
    if job.cpu > cpu + 1e-9 or job.memory_mb > mem or job.disk_mb > disk:
        return "resources"
    if job.gpus:
        limits = share.get("limits") or {}
        gpus = caps.get("gpus") or []
        if (not limits.get("gpu") or len(gpus) < job.gpus
                or any(job.gpu_memory_mb > (g.get("memory_mb") or 0) for g in gpus)
                or _tier_rank(caps.get("gpu_tiers") or []) < job.min_tier_rank):
            return "gpu"
    return None


_BLOCKER_TEXT = {
    "os": "It needs a different operating system.",
    "platform": "Its image is built for another kind of computer (CPU type).",
    "tier": "It needs stronger isolation than the owners allow.",
    "confidential": "It holds confidential data, which runs only on dedicated organisation computers.",
    "submitter": "The computers only accept jobs from certain members.",
    "probation": "The computers do not accept jobs from new members yet.",
    "gpu": "It needs a GPU that the shared computers do not offer.",
    "resources": "It asks for more CPU, memory or disk than they offer.",
}


async def _last_failure(s: AsyncSession, job: Job) -> Reason | None:
    """The most recent attempt that ended without success, with the agent's own explanation if logged."""
    row = (await s.execute(
        select(Attempt, Device.name).join(Task, Task.id == Attempt.task_id).join(Device, Device.id == Attempt.device_id)
        .where(Task.job_id == job.id, Attempt.state.in_(("failed", "preempted", "lost", "declined")))
        .order_by(Attempt.ended_at.desc().nulls_last(), Attempt.created_at.desc()).limit(1)
    )).first()
    if row is None:
        return None
    attempt, device_name = row
    detail = (await s.execute(
        select(TaskLog.chunk).where(TaskLog.attempt_id == attempt.id, TaskLog.stream == "agent")
        .order_by(TaskLog.id.desc()).limit(1)
    )).scalar()
    if not detail:  # it failed before it started (no log): the agent's reason is in its abort message
        details = (await s.execute(
            select(AuditEvent.details).where(AuditEvent.action == "attempt.abort",
                                             AuditEvent.target_id == str(attempt.id))
            .order_by(AuditEvent.id.desc()).limit(1)
        )).scalar()
        detail = (details or {}).get("detail")
    what = (attempt.end_reason or attempt.state).replace("_", " ")
    message = f"The last try on {device_name} ended: {what}."
    if detail:
        message += " " + detail.strip().splitlines()[-1][:300]
    return Reason("last_failure", message, "The task log on this page has the full output.")


async def _group_devices(s: AsyncSession, group_id) -> list:  # noqa: ANN001
    """(device, share enabled, share removed) for every active device shared with the group by a member."""
    return (await s.execute(
        select(Device, DeviceShare.enabled, DeviceShare.removed)
        .join(DeviceShare, DeviceShare.device_id == Device.id)
        .join(Membership, (Membership.group_id == group_id) & (Membership.user_id == Device.owner_id))
        .where(DeviceShare.group_id == group_id, Device.state == "active", Membership.role.in_(SHARE_ROLES))
    )).all()


def _places(job: Job, d: Device, share: dict[str, Any]) -> int:
    """How many tasks of this job's size the share fits at once (the agent's limit arithmetic)."""
    cpu, mem, disk = _offer(d, share)
    n = min(int(cpu / job.cpu + 1e-9), int(mem // job.memory_mb), int(disk // max(job.disk_mb, 1)))
    if job.gpus:
        n = min(n, len((d.capabilities or {}).get("gpus") or []) // job.gpus)
    return max(n, 0)


async def auto_parts(s: AsyncSession, settings: Settings, job: Job, submitter: str,
                     on_probation: bool) -> tuple[int, str]:
    """`parts: auto`: as many parts as the group's computers can run at the same time, and why.

    Counts the computers that could run this job and are available now; if none is, the ones that are
    shared and could run it when they come back. Never fewer than 1 or more than settings.auto_parts_max.
    """
    group = await s.get(Group, job.group_id)
    shared = [(d, sh) for d, enabled, _ in await _group_devices(s, job.group_id)
              if enabled and (sh := _reported(d, group.name))]
    fit = [(d, sh) for d, sh in shared if _blocker(job, d, sh, submitter, on_probation) is None]
    now = [(d, sh) for d, sh in fit
           if _online(d, settings.heartbeat_interval_s) and sh.get("active_now") and d.owner_active is not None]
    pool, when = (now, "available now") if now else (fit, "shared with the group")
    places = sum(_places(job, d, sh) for d, sh in pool)
    if places == 0:
        return 1, "No shared computer can run this job yet, so it runs as 1 part."
    n = min(places, settings.auto_parts_max)
    computers = "1 computer" if len(pool) == 1 else f"{len(pool)} computers"
    return n, f"Split into {n} part(s): room for {places} at once on {computers} {when}."


async def _first_run_reasons(s: AsyncSession, job: Job, pending: int) -> list[Reason]:
    """Why a first run waits although computers fit it (scheduler.sizing, claim_task)."""
    if not (job.sizing or {}).get("first_run"):
        return []
    part0 = (await s.execute(select(Task.state).where(Task.job_id == job.id, Task.index == 0))).scalar()
    if part0 in ("assigned", "running"):
        return [Reason("first_part", "Part 0 runs first, to learn how much memory, CPU and time each part needs. "
                                     "The other parts start when it ends, with that size.")]
    busy = (await s.execute(
        select(func.count()).select_from(Attempt).join(Task, Task.id == Attempt.task_id)
        .join(Job, Job.id == Task.job_id)
        .where(Job.submitter_id == job.submitter_id, Attempt.state.in_(("assigned", "running")),
               Attempt.resources["first_run"].as_boolean())
    )).scalar_one()
    if busy:
        return [Reason("one_first_run",
                       "Its submitter has another first run going. pool runs one first run per person at a time, "
                       "because a first run takes all the free memory of a computer.",
                       "It starts when the other one ends. To skip the wait, set the memory yourself (-m).")]
    return []


async def explain_waiting(s: AsyncSession, settings: Settings, job: Job) -> tuple[int, list[Reason]]:
    """Return (pending task count, reasons). No reasons when nothing is waiting."""
    now = utcnow()
    pending, ready_now, next_try = (await s.execute(
        select(func.count(), func.count().filter(Task.not_before <= now), func.min(Task.not_before))
        .where(Task.job_id == job.id, Task.state == "pending")
    )).one()
    if job.state not in ("pending", "running") or pending == 0:
        return pending, []
    failure = await _last_failure(s, job)
    if ready_now == 0:
        reasons = [Reason("retry_backoff", f"Waiting to retry after a failure. Next try at {next_try:%H:%M:%S} UTC.")]
        return pending, reasons + ([failure] if failure else [])

    group = await s.get(Group, job.group_id)
    if group.state != "active":
        return pending, [Reason("group_inactive", "This group is not active, so its jobs do not run.",
                                "A coordinator can reactivate the group.")]
    if job.egress:
        return pending, [Reason("egress", "Jobs that need internet access cannot run yet.",
                                "Remove network.egress from the job.")]

    rows = await _group_devices(s, job.group_id)
    shared = [d for d, enabled, _ in rows if enabled]
    share_cmd = f"`pool-agent share {group.name}`"
    if not shared:
        message = "No computer is shared with this group yet."
        removed = sum(1 for _, enabled, rem in rows if rem)
        turned_off = sum(1 for _, enabled, rem in rows if not enabled and not rem)
        if turned_off:
            message += f" {_computers(turned_off)} switched off for this group by the owner."
        if removed:
            message += f" {_computers(removed)} removed from this group by a coordinator or admin."
        return pending, [Reason("no_devices", message,
                                f"Anyone in the group can share a computer: install the agent, then run {share_cmd}. "
                                "The Devices page shows the steps.")]

    online = [d for d in shared if _online(d, settings.heartbeat_interval_s)]
    if not online:
        return pending, [Reason("offline", f"{_computers(len(shared))} shared with this group, but none is online.",
                                "The agent has to be running on it (`pool-agent run`).")]

    in_policy = [(d, sh) for d in online if (sh := _reported(d, group.name))]
    if not in_policy:
        return pending, [Reason("not_in_policy",
                                f"{_computers(len(online))} shared with this group on the server, but the "
                                "computer's own sharing settings do not include this group.",
                                f"On that computer, run {share_cmd}.")]

    available = [(d, sh) for d, sh in in_policy if sh.get("active_now") and d.owner_active is not None]
    if not available:
        return pending, [Reason("not_available_now",
                                f"{_computers(len(in_policy))} online but not available right now: outside the "
                                "owner's sharing hours, or the owner is using it.",
                                "The job starts by itself when one becomes available.")]

    submitter, probation_until = (await s.execute(
        select(User.handle, Membership.probation_until).join(Membership, Membership.user_id == User.id)
        .where(User.id == job.submitter_id, Membership.group_id == job.group_id)
    )).one()
    on_probation = probation_until is not None and probation_until > now
    blockers: dict[str, int] = {}
    fits = 0
    for d, sh in available:
        why = _blocker(job, d, sh, submitter, on_probation)
        if why is None:
            fits += 1
        else:
            blockers[why] = blockers.get(why, 0) + 1

    if fits:
        first_run = await _first_run_reasons(s, job, pending)
        if first_run:
            return pending, first_run + ([failure] if failure else [])
        reasons = [Reason("waiting_for_room",
                          f"{_computers(fits)} able to run this job. It starts within seconds, or as soon as one "
                          "finishes its current work.")]
        return pending, reasons + ([failure] if failure else [])

    if set(blockers) == {"resources"}:
        offers = [_offer(d, sh) for d, sh in available]
        short = []
        if job.cpu > max(o[0] for o in offers) + 1e-9:
            short.append(f"{_cpu(job.cpu)} (most offered: {_cpu(max(o[0] for o in offers))})")
        if job.memory_mb > max(o[1] for o in offers):
            short.append(f"{_size(job.memory_mb)} memory (most offered: {_size(max(o[1] for o in offers))})")
        if job.disk_mb > max(o[2] for o in offers):
            short.append(f"{_size(job.disk_mb)} disk (most offered: {_size(max(o[2] for o in offers))})")
        if not short:  # each limit fits on some computer, but no single computer fits them all
            short.append(f"{_cpu(job.cpu)}, {_size(job.memory_mb)} memory and {_size(job.disk_mb)} disk together")
        message = "This job asks for more than any shared computer offers: needs " + "; ".join(short) + "."
        if (job.sizing or {}).get("first_run") and (job.sizing or {}).get("steps"):
            message = (f"The last try ran out of memory with all the free memory of a computer "
                       f"({_size(job.memory_mb - 1)}). It waits for a computer that offers more.")
        hint = (f"Ask for less in the job's resources, or ask a computer's owner to raise the limit, "
                f"e.g. `pool-agent share {group.name} --cpu-percent 75 --memory 8Gi`.")
        return pending, [Reason("too_big", message, hint)]

    if set(blockers) == {"platform"}:
        kinds = ", ".join(job.architectures or [])
        return pending, [Reason("platform", f"Its image is built for {kinds}, and no shared computer is.",
                                f"Build it for the group's computers: `pool run --platform linux/"
                                f"{(available[0][0].capabilities or {}).get('arch') or available[0][0].arch}`.")]
    sentences = " ".join(_BLOCKER_TEXT[code] for code in sorted(blockers))
    return pending, [Reason("no_match", f"No available computer can run this job. {sentences}",
                            "Compare the job's requirements with the group's computers on the group page.")]
