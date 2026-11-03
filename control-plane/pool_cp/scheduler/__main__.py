"""Scheduler worker: lease expiry, retries, job roll-ups, group expiry (plan §5.1, §7.4).

    python -m pool_cp.scheduler

Only one worker is active at a time (PostgreSQL advisory lock); extra copies wait.
"""

from __future__ import annotations

import asyncio
import logging
import signal

from sqlalchemy import text

from .. import storage
from ..config import get_settings
from ..db import make_engine, make_sessionmaker
from ..deletion import purge_deleted_groups
from ..timeutil import utcnow
from . import core

log = logging.getLogger("pool_cp.scheduler")
_LEADER_LOCK = 0x706F6F6D


async def run(stop: asyncio.Event, settings=None, on_tick=None) -> None:
    settings = settings or get_settings()
    engine = make_engine(settings.database_url)
    maker = make_sessionmaker(engine)
    async with engine.connect() as leader:
        while not stop.is_set():
            got = (await leader.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": _LEADER_LOCK})).scalar()
            await leader.commit()
            if got:
                break
            log.info("another scheduler is active; waiting")
            try:
                await asyncio.wait_for(stop.wait(), timeout=5)
            except TimeoutError:
                pass
        if stop.is_set():
            return
        # Plan §7.4: after a restart, give agents one lease period to reconnect before expiring anything.
        grace_until = utcnow().timestamp() + settings.lease_ttl_s
        log.info("scheduler active; lease expiry starts in %ss", settings.lease_ttl_s)
        api_was_down = False
        while not stop.is_set():
            try:
                async with maker() as s:
                    api_up = await core.api_alive(s, settings)
                if not api_up:
                    if not api_was_down:
                        log.warning("no API process is up; lease expiry paused")
                    api_was_down = True
                elif api_was_down:
                    # Plan §7.4: after the control plane comes back, wait one lease period for agents to
                    # reconnect and report before expiring anything.
                    api_was_down = False
                    grace_until = utcnow().timestamp() + settings.lease_ttl_s
                    log.info("API is back; lease expiry resumes in %ss", settings.lease_ttl_s)
                if api_up and utcnow().timestamp() >= grace_until:
                    async with maker() as s:
                        n = await core.expire_leases(s, settings)
                        await s.commit()
                        if n:
                            log.info("expired %d leases", n)
                async with maker() as s:
                    await core.expire_groups(s)
                    await s.commit()
                async with maker() as s:
                    purged = await purge_deleted_groups(s)
                    await s.commit()
                for gid in purged:  # stored files go after the database rows; leftovers are harmless
                    try:
                        await asyncio.to_thread(storage.delete_group_objects, settings, gid)
                    except Exception:
                        log.warning("could not delete stored files of purged group %s", gid, exc_info=True)
                async with maker() as s:
                    await core.rollup_jobs(s)
                    await s.commit()
            except Exception:  # keep the loop alive; errors are logged with traceback
                log.exception("scheduler iteration failed")
            if on_tick is not None:
                on_tick()
            try:
                await asyncio.wait_for(stop.wait(), timeout=settings.scheduler_interval_s)
            except TimeoutError:
                pass
    await engine.dispose()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    stop = asyncio.Event()

    async def _main() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        await run(stop)

    asyncio.run(_main())


if __name__ == "__main__":
    main()
