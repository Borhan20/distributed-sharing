"""Measure a local run the way the agent does (agent/sandbox/stats.go): read `docker stats`, keep the highest
memory, add up CPU time. `pool run --local` prints the result, so you learn a program's size before you ask
the group for it (docs/design/pool-decides.md §4.2)."""

from __future__ import annotations

import json
import math
import subprocess
import threading
import time

_UNITS = [("KiB", 1 << 10), ("MiB", 1 << 20), ("GiB", 1 << 30), ("TiB", 1 << 40),
          ("kB", 1e3), ("KB", 1e3), ("MB", 1e6), ("GB", 1e9), ("TB", 1e12), ("B", 1)]
MAX_GAP_S = 30.0


def parse_size(text: str) -> int | None:
    for suffix, mult in _UNITS:
        if text.endswith(suffix):
            try:
                v = float(text[: -len(suffix)].strip())
            except ValueError:
                return None
            return int(v * mult) if v >= 0 and math.isfinite(v) else None
    return None


def parse_line(line: str) -> tuple[float, int] | None:
    """(CPU percent, memory bytes) from one `docker stats --format '{{json .}}'` line, or None."""
    i, j = line.find("{"), line.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        raw = json.loads(line[i : j + 1])
        cpu = float(str(raw["CPUPerc"]).strip().rstrip("%"))
    except (ValueError, KeyError, TypeError):
        return None
    mem = parse_size(str(raw.get("MemUsage", "")).split("/")[0].strip())
    if mem is None or cpu < 0 or not math.isfinite(cpu):
        return None
    return cpu, mem


class Meter:
    """Follows one container's `docker stats` stream in a thread until stop()."""

    def __init__(self, docker: str, container: str) -> None:
        self.docker, self.container = docker, container
        self.peak_mem = 0
        self.cpu_s = 0.0
        self.covered = 0.0
        self.samples = 0
        self._last: float | None = None
        self._stop = threading.Event()
        self._proc: subprocess.Popen | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> Meter:
        self._thread.start()
        return self

    def add(self, cpu: float, mem: int, at: float) -> None:
        self.peak_mem = max(self.peak_mem, mem)
        if self._last is not None and 0 < at - self._last <= MAX_GAP_S:
            self.cpu_s += cpu / 100 * (at - self._last)
            self.covered += at - self._last
        self._last = at
        self.samples += 1

    def _run(self) -> None:
        # `docker run` creates the container a moment after it starts: try until it exists or we are stopped.
        while not self._stop.is_set():
            self._proc = subprocess.Popen([self.docker, "stats", "--format", "{{json .}}", self.container],
                                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            for line in self._proc.stdout:
                reading = parse_line(line)
                if reading:
                    self.add(*reading, time.monotonic())
            self._proc.wait()
            if self.samples:
                return
            self._stop.wait(0.2)

    def stop(self, wall_s: float) -> dict | None:
        self._stop.set()
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
        if self._thread.ident is not None:
            self._thread.join(timeout=5)
        if not self.samples:
            return None
        cpu_s = self.cpu_s / self.covered * wall_s if self.covered else 0.0
        return {"measured": 1, "peak_memory_mb": -(-self.peak_mem // (1 << 20)),
                "avg_cpu": round(cpu_s / wall_s, 2) if wall_s else 0.0, "max_wall_seconds": wall_s}
