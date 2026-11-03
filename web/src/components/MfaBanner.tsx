import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { client, unwrap } from "../api/client";
import { authMode, useAuth } from "../auth/AuthContext";

const SETUP_FLAG = "pool.mfa_setup_started";
const PRIVILEGED = new Set(["coordinator", "admin"]);

function flag(): boolean {
  try {
    return sessionStorage.getItem(SETUP_FLAG) === "1";
  } catch {
    return false;
  }
}

/**
 * Coordinators and admins need two-step sign-in (an authenticator app code) for invites, settings and
 * votes (plan §8.6). Keycloak asks for a code only from people who set one up, so this banner offers
 * both steps: set up an authenticator app, then sign in again with a code. Members never see it.
 */
export function MfaBanner() {
  const { me, loginOidc } = useAuth();
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (!me?.mfa) return;
    try {
      sessionStorage.removeItem(SETUP_FLAG); // two-step sign-in is in use; the reminder is done
    } catch {
      /* ignore */
    }
  }, [me?.mfa]);
  const groups = useQuery({
    queryKey: ["groups"],
    queryFn: () => unwrap(client.GET("/api/v1/groups")),
    enabled: !!me && authMode() === "oidc" && !!me.mfa_required && !me.mfa,
  });
  if (!me || authMode() !== "oidc" || !me.mfa_required || me.mfa) return null;
  const roles = (groups.data ?? []).map((g) => g.my_role ?? "").filter((r) => PRIVILEGED.has(r));
  if (!me.server_admin && roles.length === 0) return null;
  const setupStarted = flag();
  const go = (opts: Parameters<typeof loginOidc>[1], setup: boolean) => {
    setBusy(true);
    try {
      if (setup) sessionStorage.setItem(SETUP_FLAG, "1");
      else sessionStorage.removeItem(SETUP_FLAG);
    } catch {
      /* storage unavailable: the banner still works, it just cannot remember the first step */
    }
    void loginOidc(window.location.pathname, opts).catch(() => setBusy(false));
  };
  return (
    <div className="mfa-banner" role="note">
      {setupStarted ? (
        <p>
          <strong>Authenticator app added.</strong> Now sign in once more with a code from it to use invites,
          group settings and votes.
        </p>
      ) : (
        <p>
          <strong>Your role needs two-step sign-in</strong> for invites, group settings and votes: a 6-digit code
          from an authenticator app on your phone, in addition to your password. Set it up once.
        </p>
      )}
      <div className="row wrap">
        <button type="button" className={setupStarted ? "secondary" : ""} disabled={busy}
                onClick={() => go({ kcAction: "CONFIGURE_TOTP" }, true)}>
          Set up two-step sign-in
        </button>
        <button type="button" className={setupStarted ? "" : "secondary"} disabled={busy}
                onClick={() => go({ prompt: "login" }, false)}>
          Sign in again with my code
        </button>
      </div>
    </div>
  );
}
