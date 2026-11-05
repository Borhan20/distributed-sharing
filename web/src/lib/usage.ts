// What a job's runs used next to what they were given (docs/design/pool-decides.md §4.2).

export type TaskUsage = {
  peak_memory_mb?: number;
  avg_cpu?: number | null;
  wall_seconds?: number;
  samples?: number;
  oom_killed?: boolean;
};
export type JobUsed = {
  measured?: number;
  peak_memory_mb?: number;
  avg_cpu?: number | null;
  max_wall_seconds?: number;
  oom_killed?: number;
};
export type Requested = {
  cpu?: number;
  memory_mb?: number | null;
  first_run?: boolean;
  disk_mb?: number;
  gpus?: number;
  time_limit_s?: number | null;
};

/** The sizes each task gets now: "1 CPU, 1.8 GiB memory, 2.0 GiB disk, time limit 30 min". */
export function requestedText(r: Requested | null | undefined): string | null {
  if (!r || (!r.memory_mb && !r.first_run)) return null;
  const memory = r.first_run ? "all the free memory of its computer (first run)" : `${sizeText(r.memory_mb ?? 0)} memory`;
  const parts = [`${r.cpu ?? 1} CPU`, memory];
  if (r.disk_mb) parts.push(`${sizeText(r.disk_mb)} disk`);
  if (r.gpus) parts.push(`${r.gpus} GPU`);
  if (r.time_limit_s) parts.push(`time limit ${durationText(r.time_limit_s)}`);
  return parts.join(", ");
}

export function sizeText(mb: number): string {
  return mb >= 1024 ? `${(mb / 1024).toFixed(1)} GiB` : `${Math.round(mb)} MiB`;
}

export function durationText(seconds: number): string {
  const s = Math.floor(seconds);
  if (s < 60) return `${s} s`;
  if (s < 3600) return s % 60 ? `${Math.floor(s / 60)} min ${s % 60} s` : `${s / 60} min`;
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return m ? `${h} h ${m} min` : `${h} h`;
}

/** One task's run: "600 MiB · 0.8 CPU · 2 min", or "–" when it was not measured. */
export function taskUsageText(u: TaskUsage | null | undefined): string {
  if (!u || !u.samples) return u?.oom_killed ? "out of memory" : "–";
  const cpu = u.avg_cpu == null ? "" : ` · ${u.avg_cpu} CPU`;
  const text = `${sizeText(u.peak_memory_mb ?? 0)}${cpu} · ${durationText(u.wall_seconds ?? 0)}`;
  return u.oom_killed ? `${text} · out of memory` : text;
}

/** The job's line under "Each task": the most one task used, against what each task was given. */
export function usedText(req: Requested | null | undefined, used: JobUsed | null | undefined): string | null {
  if (!used?.measured) return null;
  const r = req ?? {};
  const of = (v: string | null) => (v ? ` of ${v}` : "");
  const parts = [`${sizeText(used.peak_memory_mb ?? 0)} memory${of(r.memory_mb ? sizeText(r.memory_mb) : null)}`];
  if (used.avg_cpu != null) parts.push(`${used.avg_cpu} CPU on average${of(r.cpu ? String(r.cpu) : null)}`);
  parts.push(`${durationText(used.max_wall_seconds ?? 0)}${of(r.time_limit_s ? durationText(r.time_limit_s) : null)}`);
  return (used.measured > 1 ? "Most by one task: " : "") + parts.join(", ");
}

/** A tip when the job asked for far more memory than it used; asking for less finds a computer sooner. */
export function oversizeHint(req: Requested | null | undefined, used: JobUsed | null | undefined): string | null {
  const asked = req?.memory_mb ?? 0;
  const peak = used?.peak_memory_mb ?? 0;
  if (!used?.measured || asked <= 1024 || peak * 4 >= asked) return null;
  return `It used less than a quarter of its memory. Asking for about ${sizeText(Math.max(256, peak * 2))} finds a computer sooner.`;
}
