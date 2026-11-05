"""What a run used (docs/design/pool-decides.md §4.2).

The device measures each attempt with `docker stats` and sends the numbers with its commit. They are the
device's claim, so the server caps every number at what the task was given, and at the time the server itself
saw the attempt run, before it stores them. A device can then make a task look smaller than it was, but never
bigger than its limits, longer than it ran, or out of memory when it was not.
"""

from __future__ import annotations

from typing import Any

from pool_protocol import messages as pm

# The device's clock and the server's differ a little, and the device stops measuring only after the end.
WALL_SLACK_S = 60.0


def bounded_usage(raw: pm.RunUsage, *, cpu: float, memory_mb: int, disk_mb: int, exit_code: int,
                  observed_s: float) -> dict[str, Any]:
    """The stored form of a device's measurement: every number within what the task was given."""
    capped: list[str] = []

    def cap(name: str, value: float, limit: float) -> float:
        if value > limit:
            capped.append(name)
            return limit
        return value

    wall = cap("wall_seconds", raw.wall_seconds, max(observed_s, 0.0) + WALL_SLACK_S)
    peak_cpu = cap("peak_cpu", raw.peak_cpu, cpu)
    cpu_seconds = cap("cpu_seconds", raw.cpu_seconds, wall * cpu)
    peak_memory = cap("peak_memory_mb", raw.peak_memory_mb, memory_mb)
    output_bytes = cap("output_bytes", raw.output_bytes, disk_mb * 1024 * 1024)
    # The kernel kills a process that goes over the limit, so the run cannot have ended well. A child killed
    # while the main program succeeded does not count: the task did not fail for lack of memory.
    oom = bool(raw.oom_killed) and exit_code != 0
    return {
        "peak_memory_mb": int(peak_memory),
        "cpu_seconds": round(cpu_seconds, 2),
        "avg_cpu": round(cpu_seconds / wall, 2) if wall > 0 else 0.0,
        "peak_cpu": round(peak_cpu, 2),
        "wall_seconds": round(wall, 2),
        "output_bytes": int(output_bytes),
        "image_mb": int(raw.image_mb),
        "oom_killed": oom,
        "samples": int(raw.samples),
        # Numbers the device reported above the limits: odd for an honest device, so they are kept visible.
        "capped": capped,
    }


def summarize(usages: list[dict[str, Any]]) -> dict[str, Any] | None:
    """A job's use over its measured attempts: the highest memory, the average CPU, the longest run."""
    known = [u for u in usages if u]
    rows = [u for u in known if u.get("samples", 0) > 0]
    if not rows:
        return {"measured": 0, "oom_killed": sum(1 for u in known if u.get("oom_killed"))} if known else None
    walls = [u["wall_seconds"] for u in rows]
    total_wall = sum(walls)
    return {
        "measured": len(rows),
        "peak_memory_mb": max(u["peak_memory_mb"] for u in rows),
        "avg_cpu": round(sum(u["cpu_seconds"] for u in rows) / total_wall, 2) if total_wall > 0 else 0.0,
        "peak_cpu": max(u["peak_cpu"] for u in rows),
        "max_wall_seconds": max(walls),
        "max_output_bytes": max(u.get("output_bytes", 0) for u in rows),
        "oom_killed": sum(1 for u in known if u.get("oom_killed")),
    }
