"""What a run used (docs/design/pool-decides.md §4.2).

The device measures each attempt with `docker stats` and sends the numbers with its commit. They are the
device's claim, so the server caps every number at what the task was given, and at the time the server itself
saw the attempt run, before it stores them. A device can then make a task look smaller than it was, but never
bigger than its limits or longer than it ran.

A device can also claim "out of memory" for an attempt it failed, as it could already fail any attempt it runs
with any error text. Whoever acts on that claim must allow for it: pool grows a size it chose at most a few
steps and never past the owners' limits (scheduler.sizing).
"""

from __future__ import annotations

from typing import Any

from pool_protocol import messages as pm

# The device's clock and the server's differ a little, and the device stops measuring only after the end.
WALL_SLACK_S = 60.0
# Docker's CPU percentage goes a little over the --cpus limit now and then (101% for 1 CPU). Values are still
# capped at the limit, but only ones more than this far over are listed as odd.
FLAG_TOLERANCE = 1.10


def bounded_usage(raw: pm.RunUsage, *, cpu: float, memory_mb: int, disk_mb: int, exit_code: int,
                  observed_s: float) -> dict[str, Any]:
    """The stored form of a device's measurement: every number within what the task was given."""
    capped: list[str] = []

    def cap(name: str, value: float, limit: float, tolerance: float = 1.0) -> float:
        if value > limit:
            if value > limit * tolerance:
                capped.append(name)
            return limit
        return value

    wall = cap("wall_seconds", raw.wall_seconds, max(observed_s, 0.0) + WALL_SLACK_S)
    peak_cpu = cap("peak_cpu", raw.peak_cpu, cpu, FLAG_TOLERANCE)
    cpu_seconds = cap("cpu_seconds", raw.cpu_seconds, wall * cpu, FLAG_TOLERANCE)
    peak_memory = cap("peak_memory_mb", raw.peak_memory_mb, memory_mb)
    output_bytes = cap("output_bytes", raw.output_bytes, disk_mb * 1024 * 1024)
    # The kernel kills a process that goes over the limit, so the run cannot have ended well. A child killed
    # while the main program succeeded does not count: the task did not fail for lack of memory.
    oom = bool(raw.oom_killed) and exit_code != 0
    return {
        "peak_memory_mb": int(peak_memory),
        "cpu_seconds": round(cpu_seconds, 2),
        # CPU needs two readings (it is the change between them): a run with one has no CPU figure.
        "avg_cpu": round(cpu_seconds / wall, 2) if wall > 0 and raw.samples >= 2 else None,
        "peak_cpu": round(peak_cpu, 2),
        "wall_seconds": round(wall, 2),
        "output_bytes": int(output_bytes),
        "image_mb": int(raw.image_mb),
        "oom_killed": oom,
        "samples": int(raw.samples),
        # Numbers the device reported above the limits: odd for an honest device, so they are kept visible.
        "capped": capped,
    }


# The same summary as summarize(), computed by the database, so a job page that refreshes every few seconds
# does not load every attempt's measurement. :job is the job's id.
SUMMARY_SQL = """
SELECT count(*) AS known,
       count(*) FILTER (WHERE (a.usage->>'samples')::int > 0) AS measured,
       max((a.usage->>'peak_memory_mb')::bigint) FILTER (WHERE (a.usage->>'samples')::int > 0) AS peak_memory_mb,
       sum((a.usage->>'cpu_seconds')::float8) FILTER (WHERE (a.usage->>'samples')::int > 1) AS cpu_seconds,
       sum((a.usage->>'wall_seconds')::float8) FILTER (WHERE (a.usage->>'samples')::int > 1) AS wall_seconds,
       max((a.usage->>'peak_cpu')::float8) FILTER (WHERE (a.usage->>'samples')::int > 1) AS peak_cpu,
       max((a.usage->>'wall_seconds')::float8) FILTER (WHERE (a.usage->>'samples')::int > 0) AS max_wall_seconds,
       max((a.usage->>'output_bytes')::bigint) FILTER (WHERE (a.usage->>'samples')::int > 0) AS max_output_bytes,
       count(*) FILTER (WHERE (a.usage->>'oom_killed')::boolean) AS oom_killed
FROM attempts a JOIN tasks t ON t.id = a.task_id
WHERE t.job_id = :job AND a.usage IS NOT NULL
"""


def summary_from_row(row: Any) -> dict[str, Any] | None:
    """summarize()'s result from SUMMARY_SQL's row."""
    if not row or not row.known:
        return None
    if not row.measured:
        return {"measured": 0, "oom_killed": int(row.oom_killed)}
    return {
        "measured": int(row.measured),
        "peak_memory_mb": int(row.peak_memory_mb),
        "avg_cpu": round(row.cpu_seconds / row.wall_seconds, 2) if row.wall_seconds else None,
        "peak_cpu": float(row.peak_cpu) if row.peak_cpu is not None else None,
        "max_wall_seconds": float(row.max_wall_seconds),
        "max_output_bytes": int(row.max_output_bytes or 0),
        "oom_killed": int(row.oom_killed),
    }


def summarize(usages: list[dict[str, Any]]) -> dict[str, Any] | None:
    """A job's use over its measured attempts: the highest memory, the average CPU, the longest run."""
    known = [u for u in usages if u]
    rows = [u for u in known if u.get("samples", 0) > 0]
    if not rows:
        return {"measured": 0, "oom_killed": sum(1 for u in known if u.get("oom_killed"))} if known else None
    walls = [u["wall_seconds"] for u in rows]
    cpu_rows = [u for u in rows if u.get("samples", 0) > 1]
    cpu_wall = sum(u["wall_seconds"] for u in cpu_rows)
    return {
        "measured": len(rows),
        "peak_memory_mb": max(u["peak_memory_mb"] for u in rows),
        "avg_cpu": round(sum(u["cpu_seconds"] for u in cpu_rows) / cpu_wall, 2) if cpu_wall > 0 else None,
        "peak_cpu": max((u["peak_cpu"] for u in cpu_rows), default=None),
        "max_wall_seconds": max(walls),
        "max_output_bytes": max(u.get("output_bytes", 0) for u in rows),
        "oom_killed": sum(1 for u in known if u.get("oom_killed")),
    }
