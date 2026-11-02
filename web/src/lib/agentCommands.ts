/**
 * Commands shown to people setting up the sharing agent. They must work when
 * copied as-is, so they follow the agent's own rules (agent/enroll/profile.go):
 * plain http is accepted only for a localhost dev server, with --insecure-http.
 */

const LOCAL_HOSTS = new Set(["localhost", "127.0.0.1", "[::1]"]);

export type ServerKind = "https" | "local-dev" | "insecure-remote";

export function serverKind(origin: string): ServerKind {
  const u = new URL(origin);
  if (u.protocol === "https:") return "https";
  return LOCAL_HOSTS.has(u.hostname) ? "local-dev" : "insecure-remote";
}

/** Quote a value for a POSIX shell only when it needs it. */
function shellArg(value: string): string {
  return /^[A-Za-z0-9._:/@=+-]+$/.test(value) ? value : `'${value.replace(/'/g, `'\\''`)}'`;
}

export function enrollCommand(origin: string, code: string, deviceName = "my-computer"): string {
  const parts = ["pool-agent", "enroll", "--server", shellArg(origin), "--code", shellArg(code), "--name", shellArg(deviceName)];
  if (serverKind(origin) === "local-dev") parts.push("--insecure-http");
  return parts.join(" ");
}

export function shareCommand(group: string): string {
  return `pool-agent share ${shellArg(group)}`;
}

export const RUN_COMMAND = "pool-agent run";
