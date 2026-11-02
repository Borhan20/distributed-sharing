// The access token lives in memory, mirrored to sessionStorage so a page reload in the
// same tab keeps you signed in. sessionStorage is per tab and cleared when the tab
// closes; unlike localStorage it is not shared across tabs. Any XSS can still read it,
// which is why the UI renders no HTML from the API (React escapes all text).
const KEY = "pool.access_token";
let current: string | null = null;

function storage(): Storage | null {
  try {
    return typeof window !== "undefined" ? window.sessionStorage : null;
  } catch {
    return null;
  }
}

export function getToken(): string | null {
  if (current) return current;
  current = storage()?.getItem(KEY) ?? null;
  return current;
}

export function setToken(token: string | null): void {
  current = token;
  const s = storage();
  if (!s) return;
  if (token) s.setItem(KEY, token);
  else s.removeItem(KEY);
}

/** Seconds until the JWT expires (negative when expired), or null if it cannot be read. */
export function tokenTtl(token: string): number | null {
  try {
    const payload = JSON.parse(atob(token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/")));
    return typeof payload.exp === "number" ? payload.exp - Date.now() / 1000 : null;
  } catch {
    return null;
  }
}
