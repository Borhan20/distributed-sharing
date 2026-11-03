"""Deleting jobs and groups (docs/user-guide.md §8; decisions of 3 November 2026).

SECURITY-SENSITIVE: data deletion. Needs security review (plan §8.10).

What a deletion keeps:
- the audit log: it is append-only by design (a database trigger refuses deletes);
- for each computer's owner, a short "what ran here" line per run (run_history): group, job, image,
  command, times. Logs, results and stored files are deleted.

Groups get a grace period: deleting hides the group at once and stops its work; its data is purged
after Settings.group_delete_grace_days unless a coordinator or server admin restores it.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from . import audit
from .models import Group
from .timeutil import utcnow

log = logging.getLogger("pool_cp.deletion")

_ARCHIVE = """
    INSERT INTO run_history (attempt_id, device_id, group_name, submitter, job_id, job_name, task_index, image,
                             command, state, end_reason, tier, exit_code, created_at, started_at, ended_at)
    SELECT a.id, a.device_id, g.name, u.handle, j.id, j.name, t.index, t.spec->>'image',
           coalesce(t.spec->'command', '[]'::jsonb), a.state, a.end_reason, a.tier, a.exit_code,
           a.created_at, a.started_at, a.ended_at
    FROM attempts a
    JOIN tasks t ON t.id = a.task_id
    JOIN jobs j ON j.id = t.job_id
    JOIN groups g ON g.id = j.group_id
    JOIN users u ON u.id = j.submitter_id
    WHERE {where}
    ON CONFLICT (attempt_id) DO NOTHING
"""


async def archive_runs_of_job(s: AsyncSession, job_id: uuid.UUID) -> int:
    r = await s.execute(text(_ARCHIVE.format(where="j.id = :job")), {"job": job_id})
    return r.rowcount or 0


async def archive_runs_of_group(s: AsyncSession, group_id: uuid.UUID) -> int:
    r = await s.execute(text(_ARCHIVE.format(where="g.id = :group")), {"group": group_id})
    return r.rowcount or 0


async def outputs_only_in_job(s: AsyncSession, job_id: uuid.UUID, group_id: uuid.UUID) -> list[str]:
    """SHA-256 of output files this job produced that no other job in the group references.

    Objects are content-addressed per group (storage.object_key), so another job with an identical
    output shares the object; it must survive this job's deletion.
    """
    rows = await s.execute(text("""
        SELECT DISTINCT o->>'sha256' AS sha FROM attempts a JOIN tasks t ON t.id = a.task_id,
               jsonb_array_elements(coalesce(a.outputs, '[]'::jsonb)) o
        WHERE t.job_id = :job
        EXCEPT
        SELECT DISTINCT o->>'sha256' FROM attempts a JOIN tasks t ON t.id = a.task_id,
               jsonb_array_elements(coalesce(a.outputs, '[]'::jsonb)) o
        WHERE t.group_id = :group AND t.job_id <> :job
    """), {"job": job_id, "group": group_id})
    return sorted(r.sha for r in rows if r.sha)


async def purge_deleted_groups(s: AsyncSession, now: datetime | None = None) -> list[uuid.UUID]:
    """Delete groups whose grace period is over. Returns their ids, for deleting their stored files
    after the caller commits (the database is the source of truth; leftover files are harmless)."""
    now = now or utcnow()
    groups = (await s.execute(
        select(Group).where(Group.state == "deleted", Group.purge_after.is_not(None), Group.purge_after < now)
        .with_for_update(skip_locked=True)
    )).scalars().all()
    purged = []
    for g in groups:
        kept = await archive_runs_of_group(s, g.id)
        await audit.record(s, actor_type="system", actor_id=None, action="group.purge", group_id=g.id,
                           target_type="group", target_id=g.id,
                           details={"name": g.name, "deleted_at": g.deleted_at.isoformat() if g.deleted_at else None,
                                    "runs_kept_for_owners": kept})
        await s.delete(g)  # cascades: memberships, invites, shares, jobs, tasks, attempts, logs, usage
        purged.append(g.id)
        log.info("purged deleted group %s (%s)", g.name, g.id)
    return purged
