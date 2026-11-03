/**
 * How this server signs people in, read from GET /api/v1/config before the app renders,
 * so one build of the UI works for dev mode and for real sign-in on any server address.
 * Build-time VITE_* variables still win, for the Vite dev server pointed at another API.
 */
export type RuntimeConfig =
  | { auth: "dev" }
  | { auth: "oidc"; oidc_authority: string; oidc_client_id: string };

let current: RuntimeConfig | null = null;

export async function loadRuntimeConfig(fetchFn: typeof fetch = fetch): Promise<RuntimeConfig> {
  const env = import.meta.env;
  if (env.VITE_AUTH_MODE === "oidc" && env.VITE_OIDC_AUTHORITY && env.VITE_OIDC_CLIENT_ID) {
    current = { auth: "oidc", oidc_authority: env.VITE_OIDC_AUTHORITY, oidc_client_id: env.VITE_OIDC_CLIENT_ID };
    return current;
  }
  const r = await fetchFn("/api/v1/config", { headers: { Accept: "application/json" } });
  if (!r.ok) throw new Error(`the server answered ${r.status} for /api/v1/config`);
  const body = (await r.json()) as Partial<{ auth: string; oidc_authority: string; oidc_client_id: string }>;
  if (body.auth === "oidc" && body.oidc_authority && body.oidc_client_id) {
    current = { auth: "oidc", oidc_authority: body.oidc_authority, oidc_client_id: body.oidc_client_id };
  } else if (body.auth === "dev") {
    current = { auth: "dev" };
  } else {
    throw new Error("the server sent an unexpected sign-in configuration");
  }
  return current;
}

export function runtimeConfig(): RuntimeConfig {
  if (!current) throw new Error("runtime configuration not loaded yet");
  return current;
}

/** For tests. */
export function setRuntimeConfig(c: RuntimeConfig | null): void {
  current = c;
}
