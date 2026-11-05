"""pool chooses the sizes a job leaves out (docs/design/pool-decides.md §4.2, §4.3, §10).

Rules, in short:
- A size the person gave is kept as it is.
- Otherwise pool looks at the same person's earlier measured runs of the same image (and command): memory is
  the highest use × 1.25, CPU the average use (or twice as much if the last run used all it had), disk what it
  wrote × 1.5 plus the image, the time limit 3 × the longest run.
- With no earlier run ("first run"), the task gets all the free memory of the computer it runs on, 1 CPU, and
  the group's default time limit. Only one first run per person runs at a time, and a job with parts runs
  part 0 first; the other parts are sized from it.
- When a size pool chose was too small (out of memory, out of time), the task is tried again with more, at most
  MAX_GROWTH_STEPS times. That retry does not count as the person's failure, nor toward fair share.
Every choice gets a reason in plain words, shown on the job page and by `pool run`.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Attempt, Device, DeviceShare, Group, Job, Task

FIRST_RUN_PLACEHOLDER_MB = 256  # for matching only: a first run gets the computer's whole free memory
MIN_MEMORY_MB = 128
MEMORY_STEP_MB = 64
UNMEASURED_MEMORY_MB = 1024  # a run too short to measure: the old default
MIN_DISK_MB = 1024
FIRST_RUN_DISK_MB = 1024  # disk only decides where a task fits: ask for little
MIN_TIME_S = 30 * 60
MAX_TIME_S = 7 * 24 * 3600
MAX_CPU = 16.0
MAX_GROWTH_STEPS = 3
HISTORY_ROWS = 200
# Hard ceilings, whatever any device claims: they keep every number inside the database's 32-bit columns.
CEILING_MEMORY_MB = 1 << 22  # 4 TiB
CEILING_DISK_MB = 1 << 26  # 64 TiB
OUTLIER = 2.0  # a device whose runs peaked this many times above all other devices' runs is not believed
RUNS_USED = 10
CPU_BOUND = 0.85  # a run that used this share of its CPU on average could have used more


def size_text(mb: float) -> str:
    return f"{mb / 1024:.1f} GiB".replace(".0 GiB", " GiB") if mb >= 1024 else f"{mb:.0f} MiB"


def time_text(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min"
    h, m = s // 3600, s % 3600 // 60
    return f"{h} h {m} min" if m else f"{h} h"


def cpu_text(cpu: float) -> str:
    return f"{cpu:g} CPU"


def repository(image: str) -> str:
    """The image's name without tag or digest: what a new version of the same code shares with the old."""
    name = image.split("@", 1)[0]
    head, _, last = name.rpartition("/")
    if ":" in last:
        last = last.split(":", 1)[0]
    return f"{head}/{last}" if head else last


def _round_up(x: float, step: int) -> int:
    return int(math.ceil(x / step) * step)


def _ceil_half(x: float) -> float:
    return math.ceil(x * 2) / 2


@dataclass
class Run:
    """One earlier attempt with a measurement."""

    usage: dict[str, Any]
    cpu: float
    memory_mb: int
    command: Any
    args: Any
    device_id: uuid.UUID | None = None


@dataclass
class Ceiling:
    """The biggest task the group's computers can take: learned sizes never go above it (§4.3)."""

    memory_mb: int = CEILING_MEMORY_MB
    disk_mb: int = CEILING_DISK_MB
    cpu: float = MAX_CPU


async def group_ceiling(s: AsyncSession, group: Group) -> Ceiling:
    """The most memory, disk and CPU one computer shared with the group offers (its share, within the computer)."""
    rows = (await s.execute(
        select(Device).join(DeviceShare, DeviceShare.device_id == Device.id)
        .where(DeviceShare.group_id == group.id, DeviceShare.enabled, DeviceShare.removed.is_(False),
               Device.state == "active"))).scalars().all()
    memory = disk = 0
    cpu = 0.0
    for d in rows:
        caps = d.capabilities or {}
        share = next((r for r in d.reported_shares or [] if r.get("group") == group.name), None) or {}
        limits = share.get("limits") or {}
        mem = int(limits.get("memory_mb") or 0)
        if caps.get("memory_mb"):
            mem = min(mem, int(caps["memory_mb"]))
        memory = max(memory, mem)
        disk = max(disk, int(limits.get("disk_mb") or 0))
        cpu = max(cpu, float(caps.get("cpu_cores") or 0) * float(limits.get("cpu_percent") or 0) / 100)
    return Ceiling(memory_mb=min(memory or CEILING_MEMORY_MB, CEILING_MEMORY_MB),
                   disk_mb=min(disk or CEILING_DISK_MB, CEILING_DISK_MB), cpu=min(cpu or MAX_CPU, MAX_CPU))


@dataclass
class Decision:
    cpu: float
    memory_mb: int
    disk_mb: int
    max_runtime_s: int
    sizing: dict[str, Any] = field(default_factory=dict)
    gpus: int | None = None  # set when pool chose it from the image (a CUDA image)


async def _history(s: AsyncSession, user_id: uuid.UUID, image: str) -> list[Run]:
    rows = (await s.execute(
        select(Attempt.usage, Attempt.resources, Attempt.device_id, Job.cpu, Job.memory_mb, Job.image, Job.spec)
        .join(Task, Task.id == Attempt.task_id).join(Job, Job.id == Task.job_id)
        .where(Job.submitter_id == user_id, Attempt.usage.isnot(None))
        .order_by(Attempt.ended_at.desc().nulls_last()).limit(HISTORY_ROWS)
    )).all()
    repo = repository(image)
    runs = []
    for usage, given, device_id, cpu, memory_mb, job_image, spec in rows:
        if repository(job_image) != repo:
            continue
        given = given or {}
        inner = (spec or {}).get("spec") or {}
        runs.append(Run(usage=usage, cpu=float(given.get("cpu") or cpu),
                        memory_mb=int(given.get("memory_mb") or memory_mb),
                        command=inner.get("command") or [], args=inner.get("args") or [], device_id=device_id))
    return runs


def _believable(runs: list[Run]) -> list[Run]:
    """Runs whose numbers pool trusts (§9): none the server had to cap, and no device far above all others."""
    runs = [r for r in runs if not r.usage.get("capped")]
    peaks: dict[Any, float] = {}
    for r in runs:
        if r.usage.get("samples"):
            peaks[r.device_id] = max(peaks.get(r.device_id, 0.0), float(r.usage.get("peak_memory_mb") or 0))
    if len(peaks) < 2:
        return runs
    odd = {d for d, peak in peaks.items()
           if peak > OUTLIER * max((p for other, p in peaks.items() if other != d), default=0.0)}
    return [r for r in runs if r.device_id not in odd]


def _closest(runs: list[Run], command: Any, args: Any) -> tuple[list[Run], str | None]:
    """The person's runs that match best: same command and arguments, then same command, then the image."""
    for label, same in (("image, command and arguments", lambda r: r.command == command and r.args == args),
                        ("image and command", lambda r: r.command == command),
                        ("image", lambda r: True)):
        found = [r for r in runs if same(r)][:RUNS_USED]
        if found:
            return found, label
    return [], None


def _runs_text(n: int) -> str:
    return "your last run" if n == 1 else f"your last {n} runs"


def _from_runs(runs: list[Run], ceiling: Ceiling | None = None) -> tuple[dict[str, Any], dict[str, str]]:
    """Sizes and reasons from earlier runs (at least one), never above the group's biggest computer."""
    ceiling = ceiling or Ceiling()
    runs = _believable(runs) or runs[:0]
    if not runs:
        return {"cpu": 1.0, "memory_mb": UNMEASURED_MEMORY_MB, "disk_mb": MIN_DISK_MB, "max_runtime_s": MIN_TIME_S}, {
            "memory": "the earlier runs' numbers were not believable", "cpu": "nothing measured yet",
            "disk": "the smallest size", "time_limit": "the shortest limit"}
    measured = [r for r in runs if r.usage.get("samples", 0) > 0]
    n = len(runs)
    sizes: dict[str, Any] = {}
    why: dict[str, str] = {}
    # Memory. Only out-of-memory runs newer than the last good run count: a later success shows the real need.
    last_good = next((i for i, r in enumerate(runs) if not r.usage.get("oom_killed") and r.usage.get("samples")),
                     len(runs))
    ooms = [r.memory_mb for r in runs[:last_good] if r.usage.get("oom_killed")]
    peak = max((r.usage.get("peak_memory_mb", 0) for r in measured), default=0)
    if measured or ooms:
        need = peak * 1.25
        sizes["memory_mb"] = max(MIN_MEMORY_MB, _round_up(max(need, max(ooms, default=0) * 2), MEMORY_STEP_MB))
        why["memory"] = (f"{_runs_text(n).capitalize()} used at most {size_text(peak)}" if not ooms else
                         f"a run ran out of memory at {size_text(max(ooms))}")
    else:
        sizes["memory_mb"] = UNMEASURED_MEMORY_MB
        why["memory"] = f"{_runs_text(n).capitalize()} ended too quickly to measure"
    # CPU: only runs long enough to have a CPU figure (two readings or more).
    with_cpu = [r for r in measured if r.usage.get("avg_cpu") is not None]
    if with_cpu:
        last = with_cpu[0]
        avg = max(float(r.usage["avg_cpu"]) for r in with_cpu)
        if float(last.usage["avg_cpu"]) >= CPU_BOUND * last.cpu:
            sizes["cpu"] = min(MAX_CPU, last.cpu * 2)
            why["cpu"] = f"your last run used all of its {cpu_text(last.cpu)}"
        else:
            sizes["cpu"] = min(MAX_CPU, max(0.5, _ceil_half(avg * 1.2)))
            why["cpu"] = f"{_runs_text(len(with_cpu))} used {avg:g} CPU on average"
    else:
        sizes["cpu"] = 1.0
        why["cpu"] = "nothing measured yet"
    # Disk: the files it wrote and the image.
    out_mb = max((r.usage.get("output_bytes", 0) for r in runs), default=0) / (1 << 20)
    image_mb = max((r.usage.get("image_mb", 0) for r in runs), default=0)
    sizes["disk_mb"] = max(MIN_DISK_MB, _round_up(out_mb * 1.5 + image_mb, 256))
    why["disk"] = "the image and the files your runs wrote"
    # Time limit: 3 × the longest run.
    longest = max((r.usage.get("wall_seconds", 0.0) for r in runs), default=0.0)
    sizes["max_runtime_s"] = min(MAX_TIME_S, max(MIN_TIME_S, _round_up(longest * 3, 60)))
    why["time_limit"] = f"3 × your longest run ({time_text(longest)})"
    # Never more than the biggest computer the group has (§4.3).
    for key, top in (("memory_mb", ceiling.memory_mb), ("disk_mb", ceiling.disk_mb)):
        if sizes[key] > top:
            sizes[key] = top
            why[key.split("_")[0]] += f", but the biggest computer in the group offers {size_text(top)}"
    if sizes["cpu"] > ceiling.cpu:
        sizes["cpu"] = max(0.5, math.floor(ceiling.cpu * 2) / 2)
        why["cpu"] += f", but the biggest computer in the group offers {ceiling.cpu:g} CPU"
    return sizes, why


async def group_has_gpu(s: AsyncSession, group: Group) -> bool:
    """Whether a computer shared with the group offers a GPU to it."""
    rows = (await s.execute(
        select(Device).join(DeviceShare, DeviceShare.device_id == Device.id)
        .where(DeviceShare.group_id == group.id, DeviceShare.enabled, DeviceShare.removed.is_(False),
               Device.state == "active"))).scalars().all()
    for d in rows:
        share = next((r for r in d.reported_shares or [] if r.get("group") == group.name), None) or {}
        if (d.capabilities or {}).get("gpus") and (share.get("limits") or {}).get("gpu"):
            return True
    return False


APP_TIME_S = 8 * 3600  # an app runs until it is stopped: at most this long unless the person says otherwise


async def decide(s: AsyncSession, *, user_id: uuid.UUID, group: Group, image: str, command: Any, args: Any,
                 given: dict[str, Any], info: dict[str, Any] | None = None, gpu_given: bool = True,
                 app: bool = False) -> Decision:
    """Sizes for a new job. `given` holds the sizes the person set (cpu, memory_mb, disk_mb, max_runtime_s);
    None means pool decides."""
    auto = [k for k, f in (("cpu", "cpu"), ("memory", "memory_mb"), ("disk", "disk_mb"),
                           ("time_limit", "max_runtime_s")) if given.get(f) is None]
    runs, match = ([], None)
    if auto:
        runs, match = _closest(await _history(s, user_id, image), command, args)
    first_run = "memory" in auto and not runs
    if runs:
        sizes, why = _from_runs(runs, await group_ceiling(s, group))
    else:
        sizes = {"cpu": 1.0, "memory_mb": FIRST_RUN_PLACEHOLDER_MB, "disk_mb": FIRST_RUN_DISK_MB,
                 "max_runtime_s": group.default_time_limit_s}
        why = {"memory": "this is your first run of this image",
               "cpu": "the start for a first run", "disk": "room for the image and its files",
               "time_limit": "the group's default for a first run"}
    if app and "time_limit" in auto:
        # An app's run time says nothing about the next one: it runs until it is stopped.
        sizes["max_runtime_s"] = APP_TIME_S
        why["time_limit"] = "an app runs until you stop it, at most this long"
    if info and first_run and info.get("size_mb") and "disk" in auto:
        # A big image needs room on the computer: about 3 × its download, unpacked.
        sizes["disk_mb"] = min(max(FIRST_RUN_DISK_MB, _round_up(info["size_mb"] * 3, 256)), CEILING_DISK_MB)
        why["disk"] = f"room for the image ({size_text(info['size_mb'])} to download) and its files"
    reasons = []
    out: dict[str, Any] = {}
    for key, f, label, text in (("memory", "memory_mb", "Memory", size_text), ("cpu", "cpu", "CPU", lambda c: f"{c:g}"),
                                ("disk", "disk_mb", "Disk", size_text),
                                ("time_limit", "max_runtime_s", "Time limit", time_text)):
        if key in auto:
            out[f] = sizes[f]
            if key == "memory" and first_run:
                reasons.append("Memory: all the free memory of the computer it runs on, because this is your "
                               "first run of this image.")
            else:
                reasons.append(f"{label} {text(sizes[f])}: {why[key]}.")
        else:
            out[f] = given[f]
            reasons.append(f"{label} {text(given[f])}: as you asked.")
    gpus = None
    if info and info.get("cuda") and not gpu_given:
        if await group_has_gpu(s, group):
            gpus = 1
            reasons.append("GPU 1: the image is built for NVIDIA CUDA.")
        else:
            reasons.append("GPU none: the image is built for NVIDIA CUDA, but no computer in the group shares a "
                           "GPU, so it runs on the CPU.")
    if app:
        reasons.append("An app: it runs until you stop it, and only you can reach its ports (`pool attach`).")
    if gpus:
        auto.append("gpu")
    sizing = {"auto": auto, "first_run": first_run, "runs": len(runs), "match": match, "steps": 0,
              "reasons": reasons, "decided": len(reasons)}  # reasons after `decided` came while it ran
    return Decision(cpu=float(out["cpu"]), memory_mb=int(out["memory_mb"]), disk_mb=int(out["disk_mb"]),
                    max_runtime_s=int(out["max_runtime_s"]), sizing=sizing, gpus=gpus)


# --------------------------------------------------------------------------
# While the job runs
# --------------------------------------------------------------------------


def attempt_resources(job: Job, spec_max_runtime_s: int, free_memory_mb: int | None) -> dict[str, Any]:
    """What one attempt is given: the job's sizes, and on a first run all the free memory the computer offers
    this group (`free_memory_mb`, which the caller works out from the group's share, not the device's claim)."""
    first_run = bool((job.sizing or {}).get("first_run"))
    memory = job.memory_mb
    if first_run and free_memory_mb:
        memory = min(max(job.memory_mb, int(free_memory_mb)), CEILING_MEMORY_MB)
    return {"cpu": job.cpu, "memory_mb": memory, "disk_mb": job.disk_mb,
            "max_runtime_s": job.max_runtime_s or spec_max_runtime_s, "first_run": first_run}


def _note(job: Job, reason: str, **changes: Any) -> None:
    sizing = dict(job.sizing or {})
    sizing.update(changes)
    sizing["reasons"] = [*sizing.get("reasons", []), reason]
    job.sizing = sizing


def learn_first_run(job: Job, usage: dict[str, Any] | None, given: dict[str, Any], part: int, parts: int,
                    observed_s: float = 0.0, ceiling: Ceiling | None = None) -> None:
    """Size the job's other tasks from its first measured run (part 0 first, §4.5). Without a measurement (an
    older agent, or one the server could not use) nothing is learned: the other parts run as first runs too."""
    sizing = job.sizing or {}
    if not sizing.get("first_run"):
        return
    if not usage or usage.get("capped"):
        if not sizing.get("unmeasured_noted"):
            _note(job, f"Part {part} sent no measurement pool can use: the other parts run as first runs too.",
                  unmeasured_noted=True)
        return
    # The run took at least as long as the server saw it run.
    usage = {**usage, "wall_seconds": max(float(usage.get("wall_seconds") or 0), observed_s)}
    run = Run(usage=usage, cpu=float(given.get("cpu") or job.cpu),
              memory_mb=int(given.get("memory_mb") or job.memory_mb), command=None, args=None)
    sizes, _ = _from_runs([run], ceiling)
    auto = sizing.get("auto", [])
    if "memory" in auto:
        job.memory_mb = sizes["memory_mb"]
    if "cpu" in auto:
        job.cpu = sizes["cpu"]
    if "disk" in auto:
        job.disk_mb = sizes["disk_mb"]
    if "time_limit" in auto:
        job.max_runtime_s = sizes["max_runtime_s"]
    who = "It" if parts == 1 else f"Part {part}"
    if usage and usage.get("samples"):
        cpu = f" and {usage['avg_cpu']:g} CPU" if usage.get("avg_cpu") is not None else ""
        used = (f"{who} used {size_text(usage.get('peak_memory_mb', 0))} memory{cpu} "
                f"in {time_text(usage.get('wall_seconds', 0))}")
    else:
        used = f"{who} ended too quickly to measure"
    rest = "the next runs get" if parts == 1 else "the other parts get"
    _note(job, f"{used}: {rest} {size_text(job.memory_mb)} memory, {cpu_text(job.cpu)}, "
               f"time limit {time_text(job.max_runtime_s or 0)}.", first_run=False)


def grow(job: Job, what: str, given: dict[str, Any]) -> bool:
    """A size pool chose was too small (`what`: "memory" or "time"): make it bigger for the next try. False
    when pool may not grow it: the person set it, MAX_GROWTH_STEPS were used, or it is at the maximum."""
    sizing = job.sizing or {}
    key = {"memory": "memory", "time": "time_limit"}[what]
    if key not in sizing.get("auto", []):
        return False
    steps = int(sizing.get("steps", 0))
    # Another part already made it bigger than this try had: pool's old guess, so not the person's failure,
    # and no step is used (parts that run out at the same moment must not use up the steps).
    if what == "memory":
        had = int(given.get("memory_mb") or job.memory_mb)
        already = job.memory_mb > had
    else:
        had = int(given.get("max_runtime_s") or job.max_runtime_s or MIN_TIME_S)
        already = (job.max_runtime_s or 0) > had
    if already:
        return True
    if steps >= MAX_GROWTH_STEPS:
        _note(job, f"It ran out of {what} {steps + 1} times. pool stopped growing it: set the size yourself.")
        return False
    if what == "memory":
        if had >= CEILING_MEMORY_MB:
            return False
        if sizing.get("first_run"):
            # It had a whole computer's free memory: the next try needs a computer with more.
            job.memory_mb = had + 1
            reason = f"It ran out of memory at {size_text(had)}: the next try waits for a computer with more."
        else:
            job.memory_mb = min(_round_up(had * 2, MEMORY_STEP_MB), CEILING_MEMORY_MB)
            reason = f"It ran out of memory at {size_text(had)}: the next try gets {size_text(job.memory_mb)}."
    else:
        if had >= MAX_TIME_S:
            return False
        job.max_runtime_s = min(MAX_TIME_S, had * 2)
        reason = f"It ran out of time at {time_text(had)}: the next try gets {time_text(job.max_runtime_s)}."
    _note(job, reason, steps=steps + 1)
    return True
