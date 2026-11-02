import { describe, expect, it } from "vitest";
import { ApiError, errorMessage, formatApiError } from "./errors";

describe("formatApiError", () => {
  it("uses a string detail", () => {
    expect(formatApiError(409, { detail: "a group with this name already exists" })).toBe(
      "409: a group with this name already exists",
    );
  });

  it("joins FastAPI validation errors and drops the 'body' prefix", () => {
    const body = {
      detail: [
        { loc: ["body", "name"], msg: "String should match pattern", type: "x" },
        { loc: ["body", "spec", "image"], msg: "must be pinned", type: "y" },
      ],
    };
    expect(formatApiError(422, body)).toBe("422: name: String should match pattern; spec.image: must be pinned");
  });

  it("falls back to friendly text per status", () => {
    expect(formatApiError(401, null)).toMatch(/not signed in/);
    expect(formatApiError(403, {})).toMatch(/role/);
    expect(formatApiError(404, undefined)).toMatch(/not a member/);
    expect(formatApiError(0, null)).toMatch(/network error/);
    expect(formatApiError(500, null)).toBe("500: request failed");
  });

  it("uses plain-text bodies, trimmed", () => {
    expect(formatApiError(502, "  Bad Gateway  ")).toBe("502: Bad Gateway");
  });

  it("errorMessage handles ApiError, Error and other values", () => {
    expect(errorMessage(new ApiError(403, "403: nope"))).toBe("403: nope");
    expect(errorMessage(new Error("boom"))).toBe("boom");
    expect(errorMessage("x")).toBe("x");
  });
});
