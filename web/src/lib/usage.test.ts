import { describe, expect, it } from "vitest";
import { durationText, oversizeHint, requestedText, sizeText, taskUsageText, usedText } from "./usage";

describe("usage texts", () => {
  it("formats sizes and durations", () => {
    expect(sizeText(600)).toBe("600 MiB");
    expect(sizeText(1536)).toBe("1.5 GiB");
    expect(durationText(42)).toBe("42 s");
    expect(durationText(754)).toBe("12 min 34 s");
    expect(durationText(3600)).toBe("1 h");
    expect(durationText(5400)).toBe("1 h 30 min");
  });

  it("describes one task's run", () => {
    expect(taskUsageText({ peak_memory_mb: 600, avg_cpu: 0.8, wall_seconds: 120, samples: 100 })).toBe(
      "600 MiB · 0.8 CPU · 2 min",
    );
    expect(taskUsageText({ samples: 0, oom_killed: true })).toBe("out of memory");
    expect(taskUsageText({ peak_memory_mb: 38, avg_cpu: null, wall_seconds: 1, samples: 1 })).toBe("38 MiB · 1 s");
    expect(taskUsageText(null)).toBe("–");
  });

  it("puts what the job used next to what it asked for", () => {
    const req = { cpu: 2, memory_mb: 8192, time_limit_s: 7200 };
    const used = { measured: 3, peak_memory_mb: 600, avg_cpu: 1.5, max_wall_seconds: 754 };
    expect(usedText(req, used)).toBe("Most by one task: 600 MiB memory of 8.0 GiB, 1.5 CPU on average of 2, 12 min 34 s of 2 h");
    expect(usedText(req, { measured: 0 })).toBeNull();
    expect(oversizeHint(req, used)).toContain("about 1.2 GiB");
    expect(oversizeHint({ memory_mb: 1024 }, used)).toBeNull();
  });

  it("describes the sizes each task gets", () => {
    expect(requestedText({ cpu: 1, memory_mb: 1792, disk_mb: 2048, gpus: 0, time_limit_s: 1800 })).toBe(
      "1 CPU, 1.8 GiB memory, 2.0 GiB disk, time limit 30 min",
    );
    expect(requestedText(null)).toBeNull();
    expect(requestedText({ cpu: 1, memory_mb: null, first_run: true, time_limit_s: 14400 })).toBe(
      "1 CPU, all the free memory of its computer (first run), time limit 4 h",
    );
  });
});
