import { afterEach, describe, expect, it } from "vitest";
import { loadRuntimeConfig, runtimeConfig, setRuntimeConfig } from "./runtimeConfig";

const reply = (status: number, body: unknown) =>
  (async () => new Response(JSON.stringify(body), { status })) as unknown as typeof fetch;

describe("runtime config", () => {
  afterEach(() => setRuntimeConfig(null));

  it("uses what the server says", async () => {
    const c = await loadRuntimeConfig(
      reply(200, { auth: "oidc", oidc_authority: "https://pool.example.org/auth/realms/pool", oidc_client_id: "pool-web" }),
    );
    expect(c).toEqual({ auth: "oidc", oidc_authority: "https://pool.example.org/auth/realms/pool", oidc_client_id: "pool-web" });
    expect(runtimeConfig().auth).toBe("oidc");
    expect((await loadRuntimeConfig(reply(200, { auth: "dev" }))).auth).toBe("dev");
  });

  it("never falls back to dev sign-in when the answer is missing or odd", async () => {
    await expect(loadRuntimeConfig(reply(502, {}))).rejects.toThrow("502");
    await expect(loadRuntimeConfig(reply(200, { auth: "oidc" }))).rejects.toThrow("unexpected");
  });
});
