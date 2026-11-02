import createClient, { type Middleware } from "openapi-fetch";
import type { components, paths } from "./schema";
import { ApiError, formatApiError } from "./errors";
import { getToken } from "../auth/token";

export type Schemas = components["schemas"];
export type Group = Schemas["GroupOut"];
export type Member = Schemas["MemberOut"];
export type Invite = Schemas["InviteOut"];
export type JoinRequest = Schemas["JoinRequestOut"];
export type Device = Schemas["DeviceOut"];
export type GroupDevice = Schemas["GroupDeviceOut"];
export type HistoryRow = Schemas["HistoryRow"];
export type Job = Schemas["JobOut"];
export type Task = Schemas["TaskOut"];
export type LogLine = Schemas["LogLine"];
export type AuditEvent = Schemas["AuditOut"];

const auth: Middleware = {
  async onRequest({ request }) {
    const token = getToken();
    if (token) request.headers.set("Authorization", `Bearer ${token}`);
    return request;
  },
};

export const client = createClient<paths>({ baseUrl: window.location.origin });
client.use(auth);

type Result<T> = { data?: T; error?: unknown; response: Response };

/** Unwrap an openapi-fetch result: return data or throw an ApiError with a readable message. */
export async function unwrap<T>(p: Promise<Result<T>>): Promise<T> {
  let r: Result<T>;
  try {
    r = await p;
  } catch (e) {
    throw new ApiError(0, formatApiError(0, null) + (e instanceof Error ? ` (${e.message})` : ""));
  }
  if (!r.response.ok) {
    if (r.response.status === 401) window.dispatchEvent(new Event("pool:unauthorized"));
    throw new ApiError(r.response.status, formatApiError(r.response.status, r.error));
  }
  return r.data as T;
}
