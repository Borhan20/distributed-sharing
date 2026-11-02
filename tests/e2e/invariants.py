"""Invariant checker run after every chaos run (plan §10 "Invariants").

1. Exactly one final state per task; no task is lost (all final).
2. A succeeded task has exactly one succeeded attempt, from the task's current epoch.
3. No non-succeeded task has a succeeded attempt (no stale result leaked).
4. The task epoch equals its highest attempt epoch.
5. No attempt is still active on a final task.
6. The audit hash chain verifies (audit_chain_ok).
"""

from __future__ import annotations

import asyncpg

SCOPE = "($1::uuid[] IS NULL OR t.job_id = ANY($1::uuid[]))"

CHECKS = {
    "task not final": f"""
        SELECT t.id, t.state AS info FROM tasks t
        WHERE {SCOPE} AND t.state NOT IN ('succeeded', 'failed', 'cancelled')""",
    "succeeded task without exactly one succeeded attempt": f"""
        SELECT t.id, count(a.id)::text AS info FROM tasks t
        LEFT JOIN attempts a ON a.task_id = t.id AND a.state = 'succeeded'
        WHERE {SCOPE} AND t.state = 'succeeded' GROUP BY t.id HAVING count(a.id) <> 1""",
    "result committed from a stale epoch": f"""
        SELECT t.id, a.epoch || ' vs task ' || t.epoch AS info FROM tasks t
        JOIN attempts a ON a.task_id = t.id AND a.state = 'succeeded'
        WHERE {SCOPE} AND a.epoch <> t.epoch""",
    "succeeded attempt on a task that did not succeed": f"""
        SELECT t.id, t.state AS info FROM tasks t
        JOIN attempts a ON a.task_id = t.id AND a.state = 'succeeded'
        WHERE {SCOPE} AND t.state <> 'succeeded'""",
    "task epoch differs from its highest attempt epoch": f"""
        SELECT t.id, t.epoch || ' vs ' || max(a.epoch) AS info FROM tasks t
        JOIN attempts a ON a.task_id = t.id
        WHERE {SCOPE} GROUP BY t.id, t.epoch HAVING max(a.epoch) <> t.epoch""",
    "active attempt on a final task": f"""
        SELECT t.id, a.id::text AS info FROM tasks t JOIN attempts a ON a.task_id = t.id
        WHERE {SCOPE} AND a.state IN ('assigned', 'running')
          AND t.state IN ('succeeded', 'failed', 'cancelled')""",
}


async def check(dsn: str, job_ids: list[str] | None = None) -> dict:
    """dsn is a plain postgresql:// URL. Returns {"ok", "violations", "stats"}."""
    conn = await asyncpg.connect(dsn)
    try:
        violations = []
        for name, sql in CHECKS.items():
            for r in await conn.fetch(sql, job_ids):
                violations.append(f"{name}: task {r['id']} ({r['info']})")
        stats = dict(await conn.fetchrow(f"""
            SELECT count(*) FILTER (WHERE state = 'succeeded') AS succeeded,
                   count(*) FILTER (WHERE state = 'failed') AS failed,
                   count(*) FILTER (WHERE state = 'cancelled') AS cancelled, count(*) AS total
            FROM tasks t WHERE {SCOPE}""", job_ids))
        stats["attempts"] = {r["state"]: r["n"] for r in await conn.fetch(f"""
            SELECT a.state, count(*) AS n FROM attempts a JOIN tasks t ON t.id = a.task_id
            WHERE {SCOPE} GROUP BY a.state""", job_ids)}
    finally:
        await conn.close()
    return {"ok": not violations, "violations": violations, "stats": stats}


async def audit_chain_ok(database_url: str) -> tuple[bool, int | None, int]:
    """database_url is the SQLAlchemy asyncpg URL."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from pool_cp.audit import verify_chain
    from pool_cp.db import make_sessionmaker

    engine = create_async_engine(database_url)
    try:
        async with make_sessionmaker(engine)() as s:
            return await verify_chain(s)
    finally:
        await engine.dispose()
