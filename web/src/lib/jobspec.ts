import { parse as parseYaml, stringify as stringifyYaml } from "yaml";

/**
 * Jobs in the browser (docs/user-guide.md §3). The submit form edits a `JobForm`; it is sent as the short
 * job file (`formToJob`), the same YAML the command line takes. The server does the authoritative checks,
 * pins tags to a digest and decides `parts: auto`.
 */

export type RunMode = "once" | "parts" | "sweep";
// "auto": leave the sizes out, so pool chooses them from your earlier runs (docs/design/pool-decides.md).
export type Size = "auto" | "small" | "medium" | "large" | "custom";

export type SweepRow = { name: string; values: string };

export type JobForm = {
  name: string;
  image: string;
  command: string;
  mode: RunMode;
  parts: "auto" | number;
  sweep: SweepRow[];
  size: Size;
  cpu: number;
  memory: string;
  disk: string;
  gpu: number;
  timeLimit: string;
  retries: number;
};

export const SIZES: Record<Exclude<Size, "custom" | "auto">, { label: string; cpu: number; memory: string; disk: string }> = {
  small: { label: "Small: 0.5 CPU, 512 MiB memory, 1 GiB disk", cpu: 0.5, memory: "512Mi", disk: "1Gi" },
  medium: { label: "Medium: 2 CPU, 4 GiB memory, 10 GiB disk", cpu: 2, memory: "4Gi", disk: "10Gi" },
  large: { label: "Large: 8 CPU, 16 GiB memory, 20 GiB disk", cpu: 8, memory: "16Gi", disk: "20Gi" },
};

/**
 * A tiny public image, written the way people write their own: name and tag. The server pins it when the
 * job is submitted (on 3 November 2026 busybox:1.36 was the digest in examples/hello-array.yaml).
 */
export const EXAMPLE_IMAGE = "busybox:1.36";

/**
 * The job the form starts with. It must run as-is on any shared Linux computer, so a new user's first job
 * succeeds: a tiny public image, split automatically, each part writing one result file.
 */
export const EXAMPLE_FORM: JobForm = {
  name: "hello",
  image: EXAMPLE_IMAGE,
  command: `sh -c 'echo "part {{index}} of {{count}}"; echo "result of part {{index}}" > /out/result.txt'`,
  mode: "parts",
  parts: "auto",
  sweep: [{ name: "seed", values: "0..4" }],
  size: "auto",
  cpu: SIZES.small.cpu,
  memory: SIZES.small.memory,
  disk: SIZES.small.disk,
  gpu: 0,
  timeLimit: "",
  retries: 2,
};

export class JobParseError extends Error {}

const IMAGE = /^[A-Za-z0-9][A-Za-z0-9._/:-]*(@sha256:[0-9a-f]{64})?$/;

/** '0..4' -> [0..4]; 'a, b' -> ['a', 'b']; numbers become numbers (as on the server and the CLI). */
export function parseValues(text: string): (string | number)[] {
  const t = text.trim();
  const range = /^(-?\d+)\s*\.\.\s*(-?\d+)$/.exec(t);
  if (range) {
    const lo = Number(range[1]);
    const hi = Number(range[2]);
    if (hi < lo) throw new JobParseError(`${t} is empty: write it low..high`);
    if (hi - lo >= 10000) throw new JobParseError(`${t} has more than 10000 values`);
    return Array.from({ length: hi - lo + 1 }, (_, i) => lo + i);
  }
  return t
    .split(",")
    .map((v) => v.trim())
    .filter(Boolean)
    .map((v) => (/^-?\d+(\.\d+)?$/.test(v) ? Number(v) : v));
}

/** The short job file for a form, ready to send or to show as YAML. */
export function formToJob(f: JobForm): Record<string, unknown> {
  const job: Record<string, unknown> = {};
  if (f.name.trim()) job.name = f.name.trim();
  job.image = f.image.trim();
  if (f.command.trim()) job.command = f.command.trim();
  if (f.mode === "parts") job.parts = f.parts;
  if (f.mode === "sweep") {
    const sweep: Record<string, string | (string | number)[]> = {};
    for (const row of f.sweep) {
      if (!row.name.trim()) continue;
      const v = row.values.trim();
      sweep[row.name.trim()] = /^-?\d+\s*\.\.\s*-?\d+$/.test(v) ? v.replace(/\s+/g, "") : parseValues(v);
    }
    job.sweep = sweep;
  }
  if (f.size !== "auto") {
    const size = f.size === "custom" ? f : SIZES[f.size];
    job.cpu = size.cpu;
    job.memory = size.memory;
    job.disk = size.disk;
  }
  if (f.gpu > 0) job.gpu = f.gpu;
  if (f.timeLimit.trim()) job.time_limit = f.timeLimit.trim();
  job.retries = f.retries;
  return job;
}

export function formToYaml(f: JobForm): string {
  return stringifyYaml(formToJob(f), { lineWidth: 0 });
}

/** Quick checks before sending; the server checks everything again. Returns problems in plain words. */
export function formProblems(f: JobForm): string[] {
  const out: string[] = [];
  if (!f.image.trim()) out.push("Give an image, e.g. ghcr.io/your-org/app:1.");
  else if (!IMAGE.test(f.image.trim())) out.push("The image is not a valid name, e.g. ghcr.io/your-org/app:1.");
  if (f.name.trim() && !/^[a-z0-9][a-z0-9-]{0,62}$/.test(f.name.trim())) {
    out.push("The name may use lowercase letters, digits and dashes.");
  }
  if (f.mode === "parts" && f.parts !== "auto" && !(Number.isInteger(f.parts) && f.parts >= 1 && f.parts <= 10000)) {
    out.push("Parts must be Auto or a whole number from 1 to 10000.");
  }
  if (f.mode === "sweep") {
    const rows = f.sweep.filter((r) => r.name.trim());
    if (rows.length === 0) out.push("Add at least one value to sweep over.");
    for (const r of rows) {
      if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(r.name.trim())) out.push(`"${r.name}" is not a valid name.`);
      else if (["index", "count"].includes(r.name.trim())) out.push(`"${r.name}" is built in; use another name.`);
      try {
        if (parseValues(r.values).length === 0) out.push(`Give values for ${r.name}.`);
      } catch (e) {
        out.push(e instanceof Error ? e.message : String(e));
      }
    }
  }
  const known = new Set(["index", "count", ...(f.mode === "sweep" ? f.sweep.map((r) => r.name.trim()) : [])]);
  for (const m of f.command.matchAll(/\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}/g)) {
    if (!known.has(m[1])) out.push(`{{${m[1]}}} in the command has no values.`);
  }
  if (f.size === "custom" && !(f.cpu > 0)) out.push("CPU must be more than 0.");
  return out;
}

/** How many tasks a form makes, or null for Auto (the server decides). */
export function formTaskCount(f: JobForm): number | null {
  if (f.mode === "once") return 1;
  if (f.mode === "parts") return f.parts === "auto" ? null : f.parts;
  let n = 1;
  for (const r of f.sweep) {
    if (!r.name.trim()) continue;
    try {
      n *= parseValues(r.values).length;
    } catch {
      return 0;
    }
  }
  return n;
}

function sizeOf(cpu: number, memory: string, disk: string): Size {
  for (const [k, v] of Object.entries(SIZES)) {
    if (v.cpu === cpu && v.memory === memory && v.disk === disk) return k as Size;
  }
  return "custom";
}

function valuesText(v: unknown): string {
  if (Array.isArray(v)) return v.join(", ");
  return String(v ?? "");
}

/** A command list as you would type it in a terminal (quotes where needed). */
export function commandText(command: unknown): string {
  if (typeof command === "string") return command;
  if (!Array.isArray(command)) return "";
  return command
    .map((w) => {
      const s = String(w);
      if (s === "") return "''";
      return /^[A-Za-z0-9_@%+=:,./{}-]+$/.test(s) ? s : `'${s.replace(/'/g, `'\\''`)}'`;
    })
    .join(" ");
}

/**
 * A form from a job file in either form, or from a job's stored spec (then `as_submitted` gives back the
 * tag and `parts: auto` the person asked for). Used by "Run again with changes" and the YAML tab.
 */
export function jobToForm(doc: Record<string, unknown>): JobForm {
  const full = "spec" in doc || "metadata" in doc;
  const spec = (full ? doc.spec : doc) as Record<string, unknown>;
  if (!spec || typeof spec !== "object") throw new JobParseError("the job has no spec");
  const meta = (full ? doc.metadata : doc) as Record<string, unknown> | undefined;
  const asked = (doc.as_submitted ?? {}) as { image?: string; parts?: "auto" | number | null };
  const resources = (full ? spec.resources : spec) as Record<string, unknown> | undefined;
  // Sizes left out (null in a stored job) were pool's to choose: "Run again" leaves them to pool again.
  const auto = resources?.cpu == null && resources?.memory == null && resources?.disk == null;
  const cpu = Number(resources?.cpu ?? SIZES.small.cpu);
  const memory = String(resources?.memory ?? SIZES.small.memory);
  const disk = String(resources?.disk ?? SIZES.small.disk);
  const gpuRaw = resources?.gpu;
  const gpu = typeof gpuRaw === "number" ? gpuRaw : Number((gpuRaw as { count?: number } | undefined)?.count ?? 0);
  const matrix = (spec.matrix ?? spec.sweep) as Record<string, unknown> | undefined;
  const parts = asked.parts ?? (spec.parts as "auto" | number | undefined);
  const mode: RunMode = matrix && Object.keys(matrix).length ? "sweep" : parts ? "parts" : "once";
  const image = String(asked.image ?? spec.image ?? "");
  return {
    name: String((meta as { name?: string } | undefined)?.name ?? ""),
    image,
    command: commandText(spec.command),
    mode,
    parts: parts === "auto" || parts === undefined || parts === null ? "auto" : Number(parts),
    sweep: matrix ? Object.entries(matrix).map(([name, v]) => ({ name, values: valuesText(v) })) : [],
    size: auto ? "auto" : sizeOf(cpu, memory, disk),
    cpu,
    memory,
    disk,
    gpu,
    timeLimit: String(spec.max_runtime ?? spec.time_limit ?? ""),
    retries: Number(spec.retries ?? 3),
  };
}

/** Parse a pasted job file (YAML or JSON, short or full form) for the YAML tab. */
export function parseJobText(text: string, group?: string): Record<string, unknown> {
  if (!text.trim()) throw new JobParseError("the job file is empty");
  let doc: unknown;
  try {
    doc = parseYaml(text); // YAML is a superset of JSON, so this also accepts JSON
  } catch (e) {
    throw new JobParseError(`not valid YAML or JSON: ${e instanceof Error ? e.message.split("\n")[0] : e}`);
  }
  if (!doc || typeof doc !== "object" || Array.isArray(doc)) {
    throw new JobParseError("a job file is a mapping, e.g. image: ..., command: ...");
  }
  const d = doc as Record<string, unknown>;
  const full = "spec" in d || "metadata" in d;
  const spec = (full ? d.spec : d) as Record<string, unknown> | undefined;
  if (!spec || typeof spec !== "object") throw new JobParseError("spec is required");
  if (typeof spec.image !== "string" || !IMAGE.test(spec.image.trim())) {
    throw new JobParseError("image must be a name like ghcr.io/your-org/app:1");
  }
  const named = full ? (d.metadata as { group?: string } | undefined)?.group : (d.group as string | undefined);
  if (group && named && named !== group) {
    throw new JobParseError(`the job names group "${named}" but you are submitting to "${group}"`);
  }
  return d;
}

/** Tasks a parsed job file makes, or null when the server decides (parts: auto). */
export function taskCount(doc: Record<string, unknown>): number | null {
  const full = "spec" in doc || "metadata" in doc;
  const spec = (full ? doc.spec : doc) as Record<string, unknown>;
  if (spec.parts === "auto") return null;
  if (typeof spec.parts === "number") return spec.parts;
  const matrix = (spec.matrix ?? spec.sweep) as Record<string, unknown> | undefined;
  if (!matrix) return 1;
  return Object.values(matrix).reduce<number>((n, v) => {
    if (Array.isArray(v)) return n * v.length;
    try {
      return n * parseValues(String(v)).length;
    } catch {
      return 0;
    }
  }, 1);
}

/** 'ghcr.io/lab/app:1@sha256:abcd…' -> { name: 'ghcr.io/lab/app:1', digest: 'sha256:abcd…' }. */
export function splitImage(image: string): { name: string; digest: string | null } {
  const at = image.indexOf("@");
  return at < 0 ? { name: image, digest: null } : { name: image.slice(0, at), digest: image.slice(at + 1) };
}

/** What pool read from an image: "built for amd64 and arm64 · NVIDIA CUDA · opens port 8000/tcp". */
export function imageInfoText(info: { platforms?: string[]; cuda?: boolean; ports?: string[] } | null | undefined): string | null {
  if (!info?.platforms?.length) return null;
  const arches = [...new Set(info.platforms.filter((p) => p.startsWith("linux/")).map((p) => p.split("/")[1]))];
  const parts = [`built for ${(arches.length ? arches : info.platforms).join(", ")}`];
  if (info.cuda) parts.push("NVIDIA CUDA");
  if (info.ports?.length) parts.push(`opens port ${info.ports.slice(0, 5).join(", ")}`);
  return parts.join(" · ");
}

