/** A failed API call, with a message that is safe to show to people. */
export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

type ValidationItem = { loc?: (string | number)[]; msg?: string };

/** Turn a FastAPI error body (`{detail: string}` or `{detail: [{loc, msg}]}`) into one readable line. */
export function formatApiError(status: number, body: unknown): string {
  const prefix = status ? `${status}: ` : "";
  if (body && typeof body === "object" && "detail" in body) {
    const detail = (body as { detail: unknown }).detail;
    if (typeof detail === "string") return prefix + detail;
    if (Array.isArray(detail)) {
      const parts = (detail as ValidationItem[]).map((d) => {
        const loc = (d.loc ?? []).filter((x) => x !== "body").join(".");
        return loc ? `${loc}: ${d.msg ?? "invalid"}` : (d.msg ?? "invalid");
      });
      if (parts.length) return prefix + parts.join("; ");
    }
  }
  if (typeof body === "string" && body.trim()) return prefix + body.trim().slice(0, 300);
  switch (status) {
    case 401:
      return "401: you are not signed in or your session expired";
    case 403:
      return "403: your role does not allow this";
    case 404:
      return "404: not found (or you are not a member)";
    case 0:
      return "network error: could not reach the control plane";
    default:
      return `${status}: request failed`;
  }
}

export function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.message;
  if (err instanceof Error) return err.message;
  return String(err);
}
