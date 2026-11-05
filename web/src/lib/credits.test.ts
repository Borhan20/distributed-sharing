import { describe, expect, it } from "vitest";
import { creditKindText, creditsText, jobCostText } from "./credits";

describe("credits texts", () => {
  it("rounds to two decimals", () => {
    expect(creditsText(20)).toBe("20");
    expect(creditsText(18.7512)).toBe("18.75");
    expect(creditsText(-1.25)).toBe("-1.25");
  });
  it("names each kind of change", () => {
    expect(creditKindText("earn")).toBe("Your computer ran their job");
    expect(creditKindText("other")).toBe("other");
  });
  it("describes a job's cost", () => {
    expect(jobCostText({ expected: 3.3, most: 10, spent: 0 })).toBe("about 3.30 credits, at most 10");
    expect(jobCostText({ expected: null, most: 10, spent: 2.5 })).toBe("at most 10; cost so far: 2.50");
    expect(jobCostText(null)).toBeNull();
  });
});
