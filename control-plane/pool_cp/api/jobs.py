"""Jobs, tasks, logs, usage and the audit log API."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta
from pathlib import PurePosixPath
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import func, insert, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from pool_protocol.jobspec import JobSpecError, is_pinned, load_job_spec, parse_duration_s
from pool_protocol.messages import TIER_RANK

from .. import audit, builtin_registry, credits, storage
from ..auth import Principal, current_principal, get_session
from ..authz import ANY_MEMBER, MANAGERS, SUBMITTERS, load_group, membership, not_found, require_role
from ..deletion import archive_runs_of_job, outputs_only_in_job
from ..image_access import credential_for
from ..models import Attempt, AuditEvent, Group, ImageInfoCache, Job, Task, TaskLog, UsageLedger, User
from ..registry import RegistryError, inspect_image, parse_image, resolve_digest
from ..registry_cleanup import mark_used
from ..scheduler import sizing
from ..scheduler.core import cancel_tasks
from ..scheduler.explain import auto_parts, explain_waiting, progress
from ..scheduler.usage import SUMMARY_SQL, summary_from_row
from ..secretbox import SecretBoxError
from ..timeutil import utcnow
from .notify import notify_stops

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["jobs"])


class JobOut(BaseModel):
    id: uuid.UUID
    group: str
    name: str
    shape: str
    submitter: str
    image: str
    state: str
    created_at: datetime
    finished_at: datetime | None
    tasks: dict[str, int]
    spec: dict[str, Any] | None = None
    # The size each task was given, and what the measured runs used (docs/design/pool-decides.md §4.2).
    requested: dict[str, Any] | None = None
    used: dict[str, Any] | None = None
    # How the sizes were chosen: which ones pool chose, first run, growth steps, and a reason for each.
    sizing: dict[str, Any] | None = None
    # With credits on: the expected and the highest cost, and what it cost so far (docs/design/credits.md §2).
    credits: dict[str, Any] | None = None
    # What pool read from the image: platforms, CUDA, ports (docs/design/pool-decides.md §4.1).
    image_info: dict[str, Any] | None = None


def _requested(job: Job) -> dict[str, Any]:
    limit = job.max_runtime_s
    if limit is None:  # a job from before pool chose sizes
        try:
            limit = parse_duration_s((job.spec.get("spec") or {}).get("max_runtime") or "1h")
        except (ValueError, TypeError, AttributeError):
            limit = None
    first_run = bool((job.sizing or {}).get("first_run"))
    # A first run gets all the free memory of the computer it runs on: there is no one number to show.
    return {"cpu": job.cpu, "memory_mb": None if first_run else job.memory_mb, "disk_mb": job.disk_mb,
            "gpus": job.gpus, "time_limit_s": limit, "first_run": first_run}


async def _job_out(s: AsyncSession, job: Job, with_spec: bool = False, show_reasons: bool = True,
                   with_credits: bool | None = None) -> JobOut:
    counts = dict(
        (await s.execute(select(Task.state, func.count()).where(Task.job_id == job.id).group_by(Task.state))).all()
    )
    g = await s.get(Group, job.group_id)
    u = await s.get(User, job.submitter_id)
    counts["total"] = sum(counts.values())
    return JobOut(
        id=job.id,
        group=g.name,
        name=job.name,
        shape=job.shape,
        submitter=u.handle,
        image=job.image,
        state=job.state,
        created_at=job.created_at,
        finished_at=job.finished_at,
        tasks=counts,
        spec=job.spec if with_spec else None,
        requested=_requested(job),
        # The reasons quote the submitter's runs in all their groups: only for them and the group's managers.
        sizing=job.sizing if show_reasons or not job.sizing else
        {k: v for k, v in job.sizing.items() if k in ("auto", "first_run", "steps")},
        used=summary_from_row((await s.execute(text(SUMMARY_SQL), {"job": job.id})).first()) if with_spec else None,
        credits=await credits.job_credits(s, job, g) if (with_spec if with_credits is None else with_credits) else None,
        image_info=job.image_info if with_spec else None,
    )


# Reading images talks to registries the submitter chooses: at most this many at once on this server, and never
# for longer than the registry client's own deadline. A busy server skips the read (it is best effort).
_READS = asyncio.Semaphore(4)


async def _read_image(s: AsyncSession, request: Request, p: Principal, g: Group, image: str,
                      builtin: bool) -> dict[str, Any] | None:
    """What the pinned image says about itself (docs/design/pool-decides.md §4.1): platforms, CUDA, ports.
    Read once per image, then from the cache. Best effort: a job is never refused because this failed."""
    if "@" not in image:
        return None
    ref = parse_image(image)
    key = f"{ref.registry}/{ref.repository}@{image.rsplit('@', 1)[1]}"[:512]
    cached = await s.get(ImageInfoCache, key)
    if cached is not None:
        return cached.info
    settings = request.app.state.settings
    cred = None
    if not builtin:
        try:
            cred = await credential_for(s, request.app.state.secretbox, user_id=p.user.id, group_id=g.id,
                                        registry=ref.registry)
        except SecretBoxError:
            return None
    await s.commit()  # do not hold a database connection while a registry answers
    try:
        await asyncio.wait_for(_READS.acquire(), timeout=2)
    except TimeoutError:
        log.info("too many image reads at once: %s is submitted without one", image)
        return None
    try:
        if builtin:
            reg = builtin_registry.Registry(settings, request.app.state.registry_issuer)
            info = await asyncio.to_thread(reg.inspect, image)
        else:
            info = await asyncio.to_thread(inspect_image, image, cred[:2] if cred else None,
                                           allow_private=settings.registry_private_networks)
        data = info.as_dict()
    except Exception as exc:  # noqa: BLE001 (best effort: whatever a registry sends, the job is still submitted)
        log.info("could not read the image %s: %s", image, exc)
        return None
    finally:
        _READS.release()
    try:
        async with s.begin_nested():
            await s.execute(pg_insert(ImageInfoCache).values(image=key, info=data).on_conflict_do_nothing())
    except Exception:  # noqa: BLE001 (the cache is only a cache)
        log.warning("could not cache what %s says", image, exc_info=True)
    return data


def _architectures(info: dict[str, Any] | None, os_name: str | None) -> list[str] | None:
    """The CPU types (amd64, arm64, ...) the image has for the job's operating system; None: not known."""
    if not info:
        return None
    wanted = os_name or "linux"
    arches = sorted({p.split("/")[1] for p in info.get("platforms") or [] if p.count("/") >= 1
                     and p.split("/")[0] == wanted})
    return arches or None


async def _pin_image(s: AsyncSession, request: Request, p: Principal, g: Group, image: str) -> str:
    """A tag becomes tag@digest: the version it names now, looked up with the submitter's or the group's
    token if one is stored (else anonymously). Every task of the job then runs exactly this image."""
    settings = request.app.state.settings
    try:
        ref = parse_image(image)
        cred = await credential_for(s, request.app.state.secretbox, user_id=p.user.id, group_id=g.id,
                                    registry=ref.registry)
        return await asyncio.to_thread(resolve_digest, image, cred[:2] if cred else None,
                                       allow_private=settings.registry_private_networks)
    except (RegistryError, SecretBoxError) as exc:
        raise HTTPException(422, f"image: {exc}") from None


async def _pin_builtin_image(request: Request, p: Principal, g: Group, image: str) -> str:
    """An image in the built-in registry: only the submitter's own, in this group, pinned through the server's
    internal address (the registry holds it, so no outside lookup)."""
    settings = request.app.state.settings
    try:
        ref = parse_image(image)
        mine = builtin_registry.namespace(g.name, p.user.handle)
    except RegistryError as exc:
        raise HTTPException(422, f"image: {exc}") from None
    if not builtin_registry.valid_repository(ref.repository):
        raise HTTPException(422, f"image: {ref.repository!r} is not a valid name in the group's registry")
    if not ref.repository.startswith(mine + "/"):
        raise HTTPException(422, f"image: in this group's registry you can use your own images, under "
                                 f"{builtin_registry.address(settings)}/{mine}/")
    if is_pinned(image):
        return image
    reg = builtin_registry.Registry(settings, request.app.state.registry_issuer)
    try:
        return await asyncio.to_thread(reg.pin, image)
    except RegistryError as exc:
        raise HTTPException(422, f"image: {exc}") from None


@router.post("/groups/{name}/jobs", response_model=JobOut, status_code=201)
async def submit_job(
    name: str,
    request: Request,
    spec: dict[str, Any] = Body(...),
    p: Principal = Depends(current_principal),
    s: AsyncSession = Depends(get_session),
):
    """Submit a job in the full or the short form. Tags are pinned to a digest and `parts: auto` becomes a
    number here; the stored spec keeps what was asked for under `as_submitted`."""
    g = await load_group(s, name)
    m = await require_role(s, g, p, SUBMITTERS)
    settings = request.app.state.settings
    try:
        js = load_job_spec(spec)
    except JobSpecError as exc:
        raise HTTPException(422, str(exc)) from None
    if js.metadata.group and js.metadata.group != g.name:
        raise HTTPException(422, f"the job names group {js.metadata.group!r} but is submitted to {g.name!r}")
    recent = (
        await s.execute(
            select(func.count())
            .select_from(Job)
            .where(
                Job.group_id == g.id, Job.submitter_id == p.user.id, Job.created_at > utcnow() - timedelta(minutes=1)
            )
        )
    ).scalar_one()
    if recent >= settings.submit_rate_per_minute:  # plan §7.7: stop accidental floods
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "submission rate limit reached; try again shortly")
    builtin = builtin_registry.is_builtin(settings, js.spec.image)
    # The allowlist is checked first, so the server never contacts a registry outside it. The group's own
    # built-in registry is always allowed.
    if (not builtin and g.registry_allowlist
            and not any(js.spec.image.startswith(prefix) for prefix in g.registry_allowlist)):
        raise HTTPException(422, "image registry is not on this group's allowlist")
    as_submitted: dict[str, Any] = {"image": js.spec.image, "parts": js.spec.parts}
    if builtin:
        js.spec.image = await _pin_builtin_image(request, p, g, js.spec.image)
    elif not is_pinned(js.spec.image):
        js.spec.image = await _pin_image(s, request, p, g, js.spec.image)
    info = await _read_image(s, request, p, g, js.spec.image, builtin)
    res = js.resources()
    given = js.given_sizes()
    as_submitted["sizes"] = {k: v for k, v in (("cpu", js.spec.resources.cpu), ("memory", js.spec.resources.memory),
                                               ("disk", js.spec.resources.disk), ("time_limit", js.spec.max_runtime))
                             if v is not None}
    # Sizes left out are pool's to choose, from this person's earlier runs (docs/design/pool-decides.md §4.3).
    decision = await sizing.decide(
        s, user_id=p.user.id, group=g, image=js.spec.image, command=js.spec.command, args=js.spec.args,
        given={"cpu": res.cpu if given["cpu"] else None, "memory_mb": res.memory_mb if given["memory"] else None,
               "disk_mb": res.disk_mb if given["disk"] else None,
               "max_runtime_s": parse_duration_s(js.spec.max_runtime) if given["time_limit"] else None},
        info=info, gpu_given=given["gpu"])
    job = Job(
        group_id=g.id,
        submitter_id=p.user.id,
        name=js.metadata.name,
        shape=js.spec.shape,
        image=js.spec.image,
        cpu=decision.cpu,
        memory_mb=decision.memory_mb,
        disk_mb=decision.disk_mb,
        max_runtime_s=decision.max_runtime_s,
        sizing=decision.sizing,
        image_info=info,
        architectures=_architectures(info, js.spec.os),
        gpus=decision.gpus if decision.gpus is not None else res.gpus,
        gpu_memory_mb=res.gpu_memory_mb,
        min_tier_rank=TIER_RANK[js.spec.isolation.min_tier],
        os=js.spec.os,
        data_class=js.spec.data_class,
        egress=js.spec.network.egress != "none",
        max_retries=js.spec.retries,
    )
    if js.spec.parts == "auto":
        on_probation = m.probation_until is not None and m.probation_until > utcnow()
        js.spec.parts, as_submitted["note"] = await auto_parts(s, settings, job, p.user.handle, on_probation)
    stored = js.model_dump(mode="json")
    stored["as_submitted"] = as_submitted
    job.spec = stored
    s.add(job)
    await s.flush()
    params = js.task_params()
    count = len(params)
    rows = [
        {
            "id": uuid.uuid4(),
            "job_id": job.id,
            "group_id": g.id,
            "index": i,
            "params": prm,
            "spec": js.render_task(prm, i, count).model_dump(mode="json"),
            "state": "pending",
            "epoch": 0,
            "attempts_count": 0,
            "failures": 0,
            "max_retries": js.spec.retries,
        }
        for i, prm in enumerate(params)
    ]
    for i in range(0, len(rows), 1000):
        await s.execute(insert(Task), rows[i : i + 1000])
    if builtin:
        ref = parse_image(job.image)
        await mark_used(s, ref.repository, job.image.rsplit("@", 1)[1], g.id, builtin_registry.tag_of(job.image))
    await audit.record(
        s,
        actor_type="user",
        actor_id=p.user.id,
        action="job.submit",
        group_id=g.id,
        target_type="job",
        target_id=job.id,
        details={
            "name": job.name,
            "shape": job.shape,
            "tasks": len(rows),
            "image": job.image,
            "data_class": job.data_class,
        },
    )
    await s.commit()
    return await _job_out(s, job, with_credits=True)  # the cost estimate, for `pool run` and `pool submit`


@router.get("/groups/{name}/jobs", response_model=list[JobOut])
async def list_jobs(
    name: str,
    limit: int = 100,
    state: str | None = None,
    p: Principal = Depends(current_principal),
    s: AsyncSession = Depends(get_session),
):
    g = await load_group(s, name)
    m = await require_role(s, g, p, ANY_MEMBER, require_active=False)
    q = select(Job).where(Job.group_id == g.id)
    if state:
        q = q.where(Job.state == state)
    rows = (await s.execute(q.order_by(Job.created_at.desc()).limit(min(limit, 500)))).scalars().all()
    return [await _job_out(s, j, show_reasons=j.submitter_id == p.user.id or m.role in MANAGERS) for j in rows]


async def _load_job(s: AsyncSession, job_id: uuid.UUID, p: Principal) -> tuple[Job, Group, str]:
    job = await s.get(Job, job_id)
    if job is None:
        raise not_found("job")
    g = await s.get(Group, job.group_id)
    m = await membership(s, g, p) if g.state != "deleted" else None
    if m is None:
        raise not_found("job")
    return job, g, m.role


@router.get("/jobs/{job_id}", response_model=JobOut)
async def get_job(job_id: uuid.UUID, p: Principal = Depends(current_principal), s: AsyncSession = Depends(get_session)):
    job, _, role = await _load_job(s, job_id, p)
    return await _job_out(s, job, with_spec=True, show_reasons=job.submitter_id == p.user.id or role in MANAGERS)


class WaitingReason(BaseModel):
    code: str
    message: str
    hint: str | None = None


class JobWaitingOut(BaseModel):
    pending_tasks: int
    reasons: list[WaitingReason]
    # When the job will probably end, from its finished tasks or earlier runs (pool-decides §4.8); None: unknown.
    eta_s: int | None = None
    eta: str | None = None
    notes: list[str] = []  # e.g. tasks that printed nothing for a long time


@router.get("/jobs/{job_id}/waiting", response_model=JobWaitingOut)
async def job_waiting(job_id: uuid.UUID, request: Request, p: Principal = Depends(current_principal),
                      s: AsyncSession = Depends(get_session)):
    """Why tasks of this job are waiting, in plain language. Empty when nothing waits."""
    job, _, _ = await _load_job(s, job_id, p)
    pending, reasons = await explain_waiting(s, request.app.state.settings, job)
    more = await progress(s, request.app.state.settings, job)
    return JobWaitingOut(pending_tasks=pending, reasons=[WaitingReason(**r.__dict__) for r in reasons], **more)


class DownloadOut(BaseModel):
    path: str
    size: int
    url: str
    expires_at: datetime


# SECURITY-SENSITIVE: access control for job outputs. Needs security review (plan §8.10).
@router.get("/jobs/{job_id}/tasks/{index}/download", response_model=DownloadOut)
async def download_output(job_id: uuid.UUID, index: int, path: str, request: Request,
                          p: Principal = Depends(current_principal), s: AsyncSession = Depends(get_session)):
    """A short-lived link to download one output file of a finished task (group members only)."""
    job, g, _ = await _load_job(s, job_id, p)
    task = (await s.execute(select(Task).where(Task.job_id == job.id, Task.index == index))).scalar_one_or_none()
    outputs = ((task.result or {}).get("outputs") or []) if task is not None else []
    out = next((o for o in outputs if o.get("path") == path), None)
    if out is None:
        raise not_found("output")
    settings = request.app.state.settings
    if not storage.enabled(settings):
        raise HTTPException(status.HTTP_409_CONFLICT, "This server has no file store, so output files stay on the "
                                                      "computer that ran the task.")
    try:
        exists = await asyncio.to_thread(storage.object_exists, settings, g.id, out["sha256"])
    except Exception as exc:  # store down or misconfigured: say so instead of a broken link
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "The file store is not reachable right now.") from exc
    if not exists:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This file was not uploaded: the file store was not available "
                                                       "when the task finished. Its log says more.")
    url = storage.presign_get(settings, g.id, out["sha256"], filename=PurePosixPath(path).name)
    return DownloadOut(path=path, size=out.get("size", 0), url=url,
                       expires_at=utcnow() + timedelta(seconds=settings.presign_ttl_s))


class JobDeletedOut(BaseModel):
    deleted: bool
    runs_kept_for_owners: int
    files_deleted: int


# SECURITY-SENSITIVE: data deletion and its access control. Needs security review (plan §8.10).
@router.delete("/jobs/{job_id}", response_model=JobDeletedOut)
async def delete_job(job_id: uuid.UUID, request: Request, p: Principal = Depends(current_principal),
                     s: AsyncSession = Depends(get_session)):
    """Delete a job: its tasks, logs and stored output files. Running work stops first.

    The submitter may delete their own job; coordinators and admins any job in the group (with two-step
    sign-in). Kept: the audit log, and a "what ran here" line for each computer's owner.
    """
    settings = request.app.state.settings
    job, g, _ = await _load_job(s, job_id, p)
    if job.submitter_id != p.user.id:
        await require_role(s, g, p, MANAGERS, require_active=False, mfa_required=settings.require_mfa_for_admins)
    stops = await cancel_tasks(s, Task.job_id == job.id, "job_deleted")
    kept = await archive_runs_of_job(s, job.id)
    files = await outputs_only_in_job(s, job.id, g.id)
    tasks = (await s.execute(select(func.count()).select_from(Task).where(Task.job_id == job.id))).scalar()
    await audit.record(s, actor_type="user", actor_id=p.user.id, action="job.delete", group_id=g.id,
                       target_type="job", target_id=job.id,
                       details={"name": job.name, "tasks": tasks, "stopped_attempts": len(stops),
                                "runs_kept_for_owners": kept, "files": len(files)})
    await s.delete(job)  # cascades to tasks, attempts and logs; usage records stay (fair share)
    await s.commit()
    await notify_stops(request.app, stops, "job_deleted")
    deleted_files = 0
    if files:
        try:
            deleted_files = await asyncio.to_thread(storage.delete_objects, settings, g.id, files)
        except Exception:  # the store is down: the job is gone; its files become unreachable leftovers
            log.warning("could not delete stored files of job %s", job_id, exc_info=True)
    return JobDeletedOut(deleted=True, runs_kept_for_owners=kept, files_deleted=deleted_files)


class TaskOut(BaseModel):
    id: uuid.UUID
    index: int
    params: dict[str, Any]
    state: str
    epoch: int
    attempts: int
    failures: int
    result: dict[str, Any] | None
    current_attempt_id: uuid.UUID | None
    device: str | None
    updated_at: datetime
    usage: dict[str, Any] | None = None  # what the task's finished run used (the result's attempt)
    given: dict[str, Any] | None = None  # the sizes that run had (cpu, memory_mb, disk_mb, max_runtime_s)


@router.get("/jobs/{job_id}/tasks", response_model=list[TaskOut])
async def list_tasks(
    job_id: uuid.UUID, p: Principal = Depends(current_principal), s: AsyncSession = Depends(get_session)
):
    job, _, _ = await _load_job(s, job_id, p)
    from ..models import Device

    rows = (
        await s.execute(
            select(Task, Device.name)
            .outerjoin(Attempt, Attempt.id == Task.current_attempt_id)
            .outerjoin(Device, Device.id == Attempt.device_id)
            .where(Task.job_id == job.id)
            .order_by(Task.index)
        )
    ).all()
    return [
        TaskOut(
            id=t.id,
            index=t.index,
            params=t.params,
            state=t.state,
            epoch=t.epoch,
            attempts=t.attempts_count,
            failures=t.failures,
            result=t.result,
            current_attempt_id=t.current_attempt_id,
            device=dname,
            updated_at=t.updated_at,
            usage=(t.result or {}).get("usage"),
            given=(t.result or {}).get("given"),
        )
        for t, dname in rows
    ]


@router.post("/jobs/{job_id}/cancel", response_model=JobOut)
async def cancel_job(
    job_id: uuid.UUID,
    request: Request,
    p: Principal = Depends(current_principal),
    s: AsyncSession = Depends(get_session),
):
    job, g, role = await _load_job(s, job_id, p)
    if job.submitter_id != p.user.id and role not in MANAGERS:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "only the submitter or a group manager can cancel")
    stops = await cancel_tasks(s, Task.job_id == job.id, "user_cancelled")
    if job.state in ("pending", "running"):
        job.state = "cancelled"
        job.finished_at = utcnow()
    await audit.record(
        s,
        actor_type="user",
        actor_id=p.user.id,
        action="job.cancel",
        group_id=g.id,
        target_type="job",
        target_id=job.id,
        details={"stopped_attempts": len(stops)},
    )
    await s.commit()
    await notify_stops(request.app, stops, "user_cancelled")
    return await _job_out(s, job)


class LogLine(BaseModel):
    id: int
    attempt_id: uuid.UUID
    seq: int
    stream: str
    chunk: str
    created_at: datetime


@router.get("/jobs/{job_id}/tasks/{index}/logs", response_model=list[LogLine])
async def task_logs(
    job_id: uuid.UUID,
    index: int,
    attempt_id: uuid.UUID | None = None,
    after_id: int = 0,
    limit: int = 1000,
    p: Principal = Depends(current_principal),
    s: AsyncSession = Depends(get_session),
):
    job, _, _ = await _load_job(s, job_id, p)
    task = (await s.execute(select(Task).where(Task.job_id == job.id, Task.index == index))).scalar_one_or_none()
    if task is None:
        raise not_found("task")
    if attempt_id is None:
        attempt_id = (
            await s.execute(
                select(Attempt.id)
                .where(Attempt.task_id == task.id, Attempt.started_at.is_not(None))
                .order_by(Attempt.epoch.desc())
                .limit(1)
            )
        ).scalar()
        if attempt_id is None:
            return []
    else:
        a = await s.get(Attempt, attempt_id)
        if a is None or a.task_id != task.id:
            raise not_found("attempt")
    rows = (
        (
            await s.execute(
                select(TaskLog)
                .where(TaskLog.attempt_id == attempt_id, TaskLog.id > after_id)
                .order_by(TaskLog.id)
                .limit(min(limit, 5000))
            )
        )
        .scalars()
        .all()
    )
    return [
        LogLine(id=r.id, attempt_id=r.attempt_id, seq=r.seq, stream=r.stream, chunk=r.chunk, created_at=r.created_at)
        for r in rows
    ]


# ---------------------------------------------------------------- usage


@router.get("/groups/{name}/usage")
async def group_usage(
    name: str, days: int = 7, p: Principal = Depends(current_principal), s: AsyncSession = Depends(get_session)
) -> list[dict[str, Any]]:
    """Usage ledger, visible to every member (plan §12: owners pay the electricity)."""
    g = await load_group(s, name)
    await require_role(s, g, p, ANY_MEMBER, require_active=False)
    rows = (
        await s.execute(
            select(User.handle, func.sum(UsageLedger.cpu_seconds), func.sum(UsageLedger.gpu_seconds), func.count())
            .join(User, User.id == UsageLedger.user_id)
            .where(UsageLedger.group_id == g.id, UsageLedger.created_at > utcnow() - timedelta(days=min(days, 365)))
            .group_by(User.handle)
            .order_by(User.handle)
        )
    ).all()
    return [
        {"member": h, "cpu_hours": round((c or 0) / 3600, 4), "gpu_hours": round((gpu or 0) / 3600, 4), "attempts": n}
        for h, c, gpu, n in rows
    ]


# ---------------------------------------------------------------- audit


class AuditOut(BaseModel):
    id: int
    ts: datetime
    actor_type: str
    actor: str | None
    action: str
    device_id: uuid.UUID | None
    target_type: str | None
    target_id: str | None
    details: dict[str, Any]
    hash: str


async def _audit_rows(s: AsyncSession, cond, after_id: int, limit: int) -> list[AuditOut]:
    rows = (
        (
            await s.execute(
                select(AuditEvent).where(cond, AuditEvent.id > after_id).order_by(AuditEvent.id).limit(min(limit, 5000))
            )
        )
        .scalars()
        .all()
    )
    user_ids = {r.actor_id for r in rows if r.actor_type == "user" and r.actor_id}
    handles = {}
    if user_ids:
        handles = dict(
            (await s.execute(select(User.id, User.handle).where(User.id.in_([uuid.UUID(x) for x in user_ids])))).all()
        )
    return [
        AuditOut(
            id=r.id,
            ts=r.ts,
            actor_type=r.actor_type,
            actor=handles.get(uuid.UUID(r.actor_id), r.actor_id)
            if r.actor_type == "user" and r.actor_id
            else r.actor_id,
            action=r.action,
            device_id=r.device_id,
            target_type=r.target_type,
            target_id=r.target_id,
            details=r.details,
            hash=r.hash,
        )
        for r in rows
    ]


@router.get("/groups/{name}/audit", response_model=list[AuditOut])
async def group_audit(
    name: str,
    after_id: int = 0,
    limit: int = 500,
    p: Principal = Depends(current_principal),
    s: AsyncSession = Depends(get_session),
):
    g = await load_group(s, name)
    await require_role(s, g, p, MANAGERS, require_active=False)
    return await _audit_rows(s, AuditEvent.group_id == g.id, after_id, limit)


@router.get("/groups/{name}/audit/export")
async def group_audit_export(
    name: str, p: Principal = Depends(current_principal), s: AsyncSession = Depends(get_session)
):
    g = await load_group(s, name)
    await require_role(s, g, p, MANAGERS, require_active=False)
    rows = await _audit_rows(s, AuditEvent.group_id == g.id, 0, 5000)
    await audit.record(
        s, actor_type="user", actor_id=p.user.id, action="audit.export", group_id=g.id, details={"rows": len(rows)}
    )
    await s.commit()
    return JSONResponse(
        [r.model_dump(mode="json") for r in rows],
        headers={"Content-Disposition": f'attachment; filename="audit-{g.name}.json"'},
    )


@router.get("/devices/{device_id}/audit", response_model=list[AuditOut])
async def device_audit(
    device_id: uuid.UUID,
    after_id: int = 0,
    limit: int = 500,
    p: Principal = Depends(current_principal),
    s: AsyncSession = Depends(get_session),
):
    from ..models import Device

    d = await s.get(Device, device_id)
    if d is None or d.owner_id != p.user.id:
        raise not_found("device")
    return await _audit_rows(s, AuditEvent.device_id == d.id, after_id, limit)


@router.get("/audit/verify")
async def audit_verify(
    request: Request, p: Principal = Depends(current_principal), s: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    if p.user.handle not in request.app.state.settings.server_admins:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "server admins only")
    ok, bad, n = await audit.verify_chain(s)
    return {"ok": ok, "first_bad_id": bad, "rows_checked": n}
