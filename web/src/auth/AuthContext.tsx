import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { UserManager, WebStorageStateStore } from "oidc-client-ts";
import { client, unwrap } from "../api/client";
import { getToken, setToken, tokenTtl } from "./token";

export type AuthMode = "dev" | "oidc";
export const AUTH_MODE: AuthMode = import.meta.env.VITE_AUTH_MODE === "oidc" ? "oidc" : "dev";

let manager: UserManager | null = null;
export function oidcManager(): UserManager {
  if (!manager) {
    const authority = import.meta.env.VITE_OIDC_AUTHORITY;
    const clientId = import.meta.env.VITE_OIDC_CLIENT_ID;
    if (!authority || !clientId) throw new Error("VITE_OIDC_AUTHORITY and VITE_OIDC_CLIENT_ID must be set");
    manager = new UserManager({
      authority,
      client_id: clientId,
      redirect_uri: `${window.location.origin}/auth/callback`,
      post_logout_redirect_uri: window.location.origin,
      response_type: "code", // authorization code flow; oidc-client-ts always uses PKCE (S256) for it
      scope: "openid profile",
      userStore: new WebStorageStateStore({ store: window.sessionStorage }),
      stateStore: new WebStorageStateStore({ store: window.sessionStorage }),
      automaticSilentRenew: false,
    });
  }
  return manager;
}

type Me = { id: string; handle: string; mfa: boolean; server_admin: boolean };

type AuthState = {
  token: string | null;
  me: Me | null;
  loading: boolean;
  error: string | null;
  loginDev: (username: string, mfa: boolean) => Promise<void>;
  loginOidc: (returnTo?: string) => Promise<void>;
  completeOidc: () => Promise<string>;
  logout: () => void;
};

const Ctx = createContext<AuthState | null>(null);

function validStoredToken(): string | null {
  const t = getToken();
  if (!t) return null;
  const ttl = tokenTtl(t);
  if (ttl !== null && ttl < 30) {
    setToken(null);
    return null;
  }
  return t;
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [token, setTok] = useState<string | null>(validStoredToken);
  const [me, setMe] = useState<Me | null>(null);
  const [loading, setLoading] = useState<boolean>(!!token);
  const [error, setError] = useState<string | null>(null);

  const apply = useCallback((t: string | null) => {
    setToken(t);
    setTok(t);
    if (!t) setMe(null);
  }, []);

  useEffect(() => {
    const onUnauthorized = () => apply(null);
    window.addEventListener("pool:unauthorized", onUnauthorized);
    return () => window.removeEventListener("pool:unauthorized", onUnauthorized);
  }, [apply]);

  useEffect(() => {
    if (!token) {
      setLoading(false);
      return;
    }
    let cancelled = false;
    setLoading(true);
    unwrap(client.GET("/api/v1/me"))
      .then((m) => !cancelled && setMe(m as unknown as Me))
      .catch((e) => !cancelled && setError(String(e.message ?? e)))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [token]);

  const loginDev = useCallback(
    async (username: string, mfa: boolean) => {
      setError(null);
      const res = await unwrap(client.POST("/dev/token", { body: { username, mfa } }));
      const t = (res as { access_token?: string }).access_token;
      if (!t) throw new Error("the dev identity provider returned no token");
      apply(t);
    },
    [apply],
  );

  const loginOidc = useCallback(async (returnTo?: string) => {
    await oidcManager().signinRedirect({ state: { returnTo: returnTo ?? window.location.pathname } });
  }, []);

  const completeOidc = useCallback(async () => {
    const user = await oidcManager().signinRedirectCallback();
    apply(user.access_token);
    const st = user.state as { returnTo?: string } | undefined;
    return st?.returnTo && st.returnTo.startsWith("/") ? st.returnTo : "/";
  }, [apply]);

  const logout = useCallback(() => {
    apply(null);
    if (AUTH_MODE === "oidc") void oidcManager().removeUser();
  }, [apply]);

  const value = useMemo(
    () => ({ token, me, loading, error, loginDev, loginOidc, completeOidc, logout }),
    [token, me, loading, error, loginDev, loginOidc, completeOidc, logout],
  );
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useAuth(): AuthState {
  const v = useContext(Ctx);
  if (!v) throw new Error("useAuth outside AuthProvider");
  return v;
}
