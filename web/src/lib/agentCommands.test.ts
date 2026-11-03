import { describe, expect, it } from "vitest";
import { enrollCommand, serverKind, shareCommand } from "./agentCommands";

describe("agent commands", () => {
  it("adds --insecure-http only for a localhost dev server", () => {
    expect(enrollCommand("http://127.0.0.1:8000", "ABCDE-FGHJK")).toBe(
      "pool-agent enroll --server http://127.0.0.1:8000 --code ABCDE-FGHJK --name my-computer --insecure-http",
    );
    expect(enrollCommand("http://localhost:8000", "X")).toContain("--insecure-http");
    expect(enrollCommand("https://pool.example.org", "X", "lab-pc")).toBe(
      "pool-agent enroll --server https://pool.example.org --code X --name lab-pc",
    );
  });

  it("recognises servers the agent will refuse", () => {
    expect(serverKind("https://pool.example.org")).toBe("https");
    expect(serverKind("http://[::1]:8000")).toBe("local-dev");
    expect(serverKind("http://192.0.2.10:8000")).toBe("insecure-remote");
  });

  it("quotes unusual values so a copied command stays one argument", () => {
    expect(enrollCommand("https://pool.example.org", "X", "Ana's laptop")).toContain("--name 'Ana'\\''s laptop'");
    expect(shareCommand("ml-test-lab")).toBe("pool-agent share ml-test-lab");
  });
});
