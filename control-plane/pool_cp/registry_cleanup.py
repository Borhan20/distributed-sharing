"""Clean-up of the built-in registry (docs/design/pool-run.md §3.1): images that nobody used for the group's
retention (POOL_REGISTRY_RETENTION_DAYS, or the group's own setting) are deleted, and so are images of groups
that no longer exist. "Used" means submitted in a job or pulled by a computer.

SECURITY-SENSITIVE: deletes members' images. Needs security review (plan §8.10).

Never deleted: an image that a job still waiting or running refers to, and an image used while the scan runs
(the database row is deleted first, only if it is still old at that moment; then the image).

Deleting removes the image's manifest. The registry frees the disk space of layers nothing refers to any more
when its garbage collection runs (docs/admin-guide.md).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from sqlalchemy import delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .builtin_registry import Registry, is_builtin, owner
from .config import Settings
from .models import Group, Job, RegistryImage
from .registry import RegistryError, parse_image
from .timeutil import utcnow

log = logging.getLogger("pool_cp.registry_cleanup")
_ACTIVE = ("pending", "running")


async def mark_used(s: AsyncSession, repository: str, digest: str, group_id, tag: str | None = None) -> None:  # noqa: ANN001
    """Record that an image was used now (a job was submitted with it, or a computer pulled it)."""
    now = utcnow()
    stmt = pg_insert(RegistryImage).values(repository=repository, digest=digest, tag=tag, group_id=group_id,
                                          first_seen_at=now, last_used_at=now)
    await s.execute(stmt.on_conflict_do_update(
        index_elements=["repository", "digest"],
        set_={"last_used_at": now, "group_id": group_id, "tag": func.coalesce(stmt.excluded.tag, RegistryImage.tag)}))


async def _active_images(s: AsyncSession, settings: Settings) -> set[tuple[str, str]]:
    """(repository, digest) of built-in images that waiting or running jobs refer to."""
    out = set()
    for (image,) in (await s.execute(select(Job.image).where(Job.state.in_(_ACTIVE)))).all():
        if "@" in image and is_builtin(settings, image):
            try:
                out.add((parse_image(image).repository, image.rsplit("@", 1)[1]))
            except RegistryError:
                pass
    return out


async def clean_up(maker: async_sessionmaker, settings: Settings, registry: Registry) -> dict[str, int]:
    """One pass over the registry, in short transactions of its own. Returns counts: images seen, deleted."""
    async with maker() as s:
        known = {(r.repository, r.tag): r.digest for r in (await s.execute(select(RegistryImage))).scalars() if r.tag}
        # Jobs submitted from now on mark their image as used, which the row-first delete below sees.
        active = await _active_images(s, settings)
    seen = deleted = 0
    for repository in await asyncio.to_thread(registry.repositories):
        who = owner(repository)
        if who is None:
            continue  # not a member's image; pool never grants pushes outside <group>/<handle>/
        digests: dict[str, str] = {}
        for tag in await asyncio.to_thread(registry.tags, repository):
            digest = known.get((repository, tag)) or await asyncio.to_thread(registry.digest, repository, tag)
            if digest:
                digests.setdefault(digest, tag)
        for digest, tag in digests.items():
            seen += 1
            if (repository, digest) in active:
                continue
            if await _delete_if_unused(maker, settings, registry, who[0], repository, digest, tag):
                deleted += 1
    return {"seen": seen, "deleted": deleted}


async def _delete_if_unused(maker: async_sessionmaker, settings: Settings, registry: Registry, group_name: str,
                            repository: str, digest: str, tag: str) -> bool:
    now = utcnow()
    async with maker() as s:
        group = (await s.execute(select(Group).where(Group.name == group_name))).scalar_one_or_none()
        await s.execute(pg_insert(RegistryImage).values(  # first sight: the retention starts now
            repository=repository, digest=digest, tag=tag, group_id=group.id if group else None, first_seen_at=now
        ).on_conflict_do_nothing())
        await s.commit()
        days = (group.registry_retention_days if group else None) or settings.registry_retention_days
        cutoff = now - timedelta(days=days)
        old = func.coalesce(RegistryImage.last_used_at, RegistryImage.first_seen_at) < cutoff
        # The row goes first, and only if it is still old now: an image used during the scan stays.
        gone = (await s.execute(delete(RegistryImage).where(
            RegistryImage.repository == repository, RegistryImage.digest == digest,
            or_(old, group is None)).returning(RegistryImage.digest))).first()
        if gone is None:
            return False
        if group is None and (await s.execute(select(Group.id).where(Group.name == group_name))).first():
            await s.rollback()  # the group was created meanwhile
            return False
        if not await asyncio.to_thread(registry.delete, repository, digest):
            await s.rollback()
            return False
        await s.commit()
    log.info("registry: deleted %s@%s (%s)", repository, digest,
             "its group is gone" if group is None else f"unused for {days} days")
    return True
