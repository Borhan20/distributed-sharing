import { parse as parseYaml } from "yaml";

export type JobDocument = {
  apiVersion?: string;
  kind?: string;
  metadata: { name: string; group?: string };
  spec: Record<string, unknown>;
};

export class JobParseError extends Error {}

const DIGEST = /^[^\s@]+@sha256:[0-9a-f]{64}$/;

/**
 * Parse a job file (YAML or JSON, plan §6.5) and run quick client-side checks.
 * The server does the authoritative validation; this only catches obvious mistakes early.
 */
export function parseJobText(text: string, group?: string): JobDocument {
  if (!text.trim()) throw new JobParseError("the job file is empty");
  let doc: unknown;
  try {
    doc = parseYaml(text); // YAML is a superset of JSON, so this also accepts JSON
  } catch (e) {
    throw new JobParseError(`not valid YAML or JSON: ${e instanceof Error ? e.message.split("\n")[0] : e}`);
  }
  if (!doc || typeof doc !== "object" || Array.isArray(doc)) {
    throw new JobParseError("the job file must be a mapping with metadata and spec");
  }
  const d = doc as Partial<JobDocument>;
  if (d.kind !== undefined && d.kind !== "Job") throw new JobParseError(`kind must be Job, not ${d.kind}`);
  if (!d.metadata || typeof d.metadata !== "object" || typeof d.metadata.name !== "string") {
    throw new JobParseError("metadata.name is required");
  }
  if (!d.spec || typeof d.spec !== "object") throw new JobParseError("spec is required");
  const image = (d.spec as Record<string, unknown>).image;
  if (typeof image !== "string" || !DIGEST.test(image)) {
    throw new JobParseError("spec.image must be pinned by digest: <repo>@sha256:<64 hex>");
  }
  const command = (d.spec as Record<string, unknown>).command;
  if (!Array.isArray(command) || command.length === 0) {
    throw new JobParseError("spec.command must be a non-empty list");
  }
  if (group && d.metadata.group && d.metadata.group !== group) {
    throw new JobParseError(`metadata.group is "${d.metadata.group}" but you are submitting to "${group}"`);
  }
  return {
    apiVersion: d.apiVersion ?? "pool/v1alpha1",
    kind: "Job",
    metadata: { ...d.metadata },
    spec: d.spec as Record<string, unknown>,
  };
}

/** Number of tasks a matrix expands to (product of value counts); 1 without a matrix. */
export function taskCount(doc: JobDocument): number {
  const matrix = doc.spec.matrix as Record<string, unknown[]> | undefined;
  if (!matrix) return 1;
  return Object.values(matrix).reduce((n, v) => n * (Array.isArray(v) ? v.length : 0), 1);
}

/**
 * The job the form starts with. It must run as-is on any shared Linux computer,
 * so a new user's first job succeeds. The image digest matches examples/hello-array.yaml.
 */
export const EXAMPLE_JOB = `apiVersion: pool/v1alpha1
kind: Job
metadata:
  name: seed-sweep
spec:
  shape: array
  # A tiny public image, pinned by digest. Replace it with your own image.
  image: docker.io/library/busybox@sha256:73aaf090f3d85aa34ee199857f03fa3a95c8ede2ffd4cc2cdb5b94e566b11662
  # {{seed}} is replaced by each value below, so this job runs 3 tasks.
  command: ["sh", "-c", "echo running seed {{seed}}; echo 'result for seed {{seed}}' > /out/result.txt"]
  matrix:
    seed: [0, 1, 2]
  resources: { cpu: 0.5, memory: 256Mi, disk: 64Mi }
  retries: 2
  max_runtime: 10m
`;
