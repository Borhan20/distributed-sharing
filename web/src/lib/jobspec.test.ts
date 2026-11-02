import { describe, expect, it } from "vitest";
import { EXAMPLE_JOB, JobParseError, parseJobText, taskCount } from "./jobspec";

const DIGEST = "registry.example.org/x/app@sha256:" + "a".repeat(64);

describe("parseJobText", () => {
  it("parses the built-in YAML example and counts tasks", () => {
    const doc = parseJobText(EXAMPLE_JOB);
    expect(doc.metadata.name).toBe("seed-sweep");
    expect(doc.kind).toBe("Job");
    expect(taskCount(doc)).toBe(3);
  });

  it("accepts JSON as well as YAML", () => {
    const doc = parseJobText(JSON.stringify({ metadata: { name: "j" }, spec: { image: DIGEST, command: ["echo"] } }));
    expect(doc.apiVersion).toBe("pool/v1alpha1");
    expect(taskCount(doc)).toBe(1);
  });

  it("computes the matrix product like plan §6.5 (10 x 3 = 30)", () => {
    const yaml = `metadata: {name: sweep}
spec:
  shape: array
  image: ${DIGEST}
  command: [run, "{{seed}}", "{{dataset}}"]
  matrix:
    seed: [0,1,2,3,4,5,6,7,8,9]
    dataset: [imdb, amazon_polarity, ag_news]
`;
    expect(taskCount(parseJobText(yaml))).toBe(30);
  });

  it.each([
    ["", "empty"],
    ["- a\n- b", "mapping"],
    ["metadata: {name: x}\nspec: {image: 'python:3.12', command: [x]}", "pinned by digest"],
    [`metadata: {name: x}\nspec: {image: '${DIGEST}', command: []}`, "command"],
    [`spec: {image: '${DIGEST}', command: [x]}`, "metadata.name"],
    [`kind: Pod\nmetadata: {name: x}\nspec: {image: '${DIGEST}', command: [x]}`, "kind must be Job"],
    ["metadata: {name: x\nspec: [", "not valid YAML"],
  ])("rejects %j", (text, needle) => {
    expect(() => parseJobText(text)).toThrow(JobParseError);
    expect(() => parseJobText(text)).toThrow(needle);
  });

  it("rejects a group mismatch", () => {
    const text = `metadata: {name: x, group: other}\nspec: {image: '${DIGEST}', command: [x]}`;
    expect(() => parseJobText(text, "mine")).toThrow(/submitting to "mine"/);
    expect(parseJobText(text).metadata.group).toBe("other");
  });
});
