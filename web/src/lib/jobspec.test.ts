import { describe, expect, it } from "vitest";
import exampleYaml from "../../../examples/hello-array.yaml?raw";
import { parse as parseYaml } from "yaml";
import {
  EXAMPLE_FORM,
  EXAMPLE_IMAGE,
  JobParseError,
  formProblems,
  formTaskCount,
  formToJob,
  formToYaml,
  jobToForm,
  parseJobText,
  parseValues,
  splitImage,
  taskCount,
  type JobForm,
} from "./jobspec";

const DIGEST = "registry.example.org/x/app@sha256:" + "a".repeat(64);
const form = (over: Partial<JobForm> = {}): JobForm => ({ ...EXAMPLE_FORM, ...over });

describe("the submit form", () => {
  it("starts with a job that can really run: auto parts of a pinned public image", () => {
    expect(formProblems(EXAMPLE_FORM)).toEqual([]);
    expect(formTaskCount(EXAMPLE_FORM)).toBeNull(); // auto: the server decides
    const job = formToJob(EXAMPLE_FORM);
    expect(job).toMatchObject({ name: "hello", parts: "auto", cpu: 0.5, memory: "512Mi", time_limit: "10m" });
    // Written with a tag, as people push their own images; the server pins it at submit time.
    expect(EXAMPLE_IMAGE).toBe("busybox:1.36");
    expect(exampleYaml).toContain("busybox@sha256:");
  });

  it("sends the short job file the command line uses", () => {
    const f = form({
      name: "pi",
      image: "ghcr.io/lab/pi:1",
      command: "python /app/pi.py --seed {{seed}} --samples {{samples}}",
      mode: "sweep",
      sweep: [
        { name: "seed", values: "0..4" },
        { name: "samples", values: "1000000, 10000000" },
      ],
      size: "medium",
    });
    expect(formProblems(f)).toEqual([]);
    expect(formTaskCount(f)).toBe(10);
    expect(formToJob(f)).toEqual({
      name: "pi",
      image: "ghcr.io/lab/pi:1",
      command: "python /app/pi.py --seed {{seed}} --samples {{samples}}",
      sweep: { seed: "0..4", samples: [1000000, 10000000] },
      cpu: 2,
      memory: "4Gi",
      disk: "10Gi",
      time_limit: "10m",
      retries: 2,
    });
    expect(parseYaml(formToYaml(f))).toEqual(formToJob(f));
  });

  it("runs once without parts or sweep, and an empty command means the image's own", () => {
    const job = formToJob(form({ mode: "once", command: "" }));
    expect(job).not.toHaveProperty("parts");
    expect(job).not.toHaveProperty("sweep");
    expect(job).not.toHaveProperty("command");
    expect(formTaskCount(form({ mode: "parts", parts: 6 }))).toBe(6);
  });

  it.each([
    [{ image: "" }, "Give an image"],
    [{ image: "python 3.12" }, "not a valid name"],
    [{ name: "Bad Name" }, "lowercase"],
    [{ mode: "parts" as const, parts: 0 }, "Parts must be"],
    [{ command: "run {{seed}}", mode: "once" as const }, "{{seed}}"],
    [{ mode: "sweep" as const, sweep: [{ name: "index", values: "1,2" }] }, "built in"],
    [{ mode: "sweep" as const, sweep: [{ name: "seed", values: "5..1" }] }, "empty"],
    [{ size: "custom" as const, cpu: 0 }, "CPU"],
  ])("explains %j", (over, needle) => {
    expect(formProblems(form(over)).join(" ")).toContain(needle);
  });

  it("gives back the form of a submitted job, with its tag and Auto (Run again with changes)", () => {
    const stored = {
      apiVersion: "pool/v1alpha1",
      kind: "Job",
      metadata: { name: "pi" },
      spec: {
        shape: "array",
        image: "ghcr.io/lab/pi:1@sha256:" + "b".repeat(64),
        command: ["sh", "-c", "echo {{index}} > /out/r.txt"],
        parts: 8,
        resources: { cpu: 2, memory: "4Gi", disk: "10Gi", gpu: null },
        max_runtime: "30m",
        retries: 1,
      },
      as_submitted: { image: "ghcr.io/lab/pi:1", parts: "auto" },
    };
    const f = jobToForm(stored);
    expect(f).toMatchObject({ name: "pi", image: "ghcr.io/lab/pi:1", mode: "parts", parts: "auto", size: "medium" });
    expect(f.command).toBe("sh -c 'echo {{index}} > /out/r.txt'");
    expect(f).toMatchObject({ timeLimit: "30m", retries: 1 });
    const sweep = jobToForm({ image: DIGEST, sweep: { seed: [0, 1, 2] }, cpu: 3, memory: "1Gi" });
    expect(sweep).toMatchObject({ mode: "sweep", sweep: [{ name: "seed", values: "0, 1, 2" }], size: "custom", cpu: 3 });
  });
});

describe("job files pasted as YAML", () => {
  it("accepts the short form, the full form and JSON", () => {
    expect(taskCount(parseJobText(`image: ${DIGEST}\nparts: 4`))).toBe(4);
    expect(taskCount(parseJobText(`image: lab/app:1\nparts: auto`))).toBeNull();
    expect(taskCount(parseJobText(`image: lab/app:1\nsweep: {seed: 0..9, d: [a, b]}`))).toBe(20);
    expect(taskCount(parseJobText(exampleYaml))).toBe(6);
    expect(taskCount(parseJobText(JSON.stringify({ metadata: { name: "j" }, spec: { image: DIGEST } })))).toBe(1);
  });

  it.each([
    ["", "empty"],
    ["- a\n- b", "mapping"],
    ["image: 'python 3.12'", "image must be"],
    ["command: run", "image must be"],
    ["metadata: {name: x\nspec: [", "not valid YAML"],
  ])("rejects %j", (text, needle) => {
    expect(() => parseJobText(text)).toThrow(JobParseError);
    expect(() => parseJobText(text)).toThrow(needle);
  });

  it("rejects a group mismatch in either form", () => {
    expect(() => parseJobText(`image: ${DIGEST}\ngroup: other`, "mine")).toThrow(/submitting to "mine"/);
    expect(() => parseJobText(`metadata: {name: x, group: other}\nspec: {image: '${DIGEST}'}`, "mine")).toThrow();
  });
});

describe("helpers", () => {
  it("parses values like the server", () => {
    expect(parseValues("0..3")).toEqual([0, 1, 2, 3]);
    expect(parseValues("small, large")).toEqual(["small", "large"]);
    expect(parseValues("1, 2.5, x")).toEqual([1, 2.5, "x"]);
  });
  it("splits an image into the name people wrote and its digest", () => {
    expect(splitImage("ghcr.io/lab/app:1@sha256:abc")).toEqual({ name: "ghcr.io/lab/app:1", digest: "sha256:abc" });
    expect(splitImage("busybox")).toEqual({ name: "busybox", digest: null });
  });
});
