import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { client, unwrap } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { ErrorBox } from "../components/ui";
import { Login } from "./Login";

/** /invite#token=... — the token is in the URL fragment, so browsers never send it to servers or logs. */
export function AcceptInvitePage() {
  const { token: session } = useAuth();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const inviteToken = useMemo(() => new URLSearchParams(window.location.hash.slice(1)).get("token") ?? "", []);
  const [manual, setManual] = useState("");
  const accept = useMutation({
    mutationFn: (t: string) => unwrap(client.POST("/api/v1/invites/accept", { body: { token: t } })),
    onSuccess: (g) => {
      void qc.invalidateQueries({ queryKey: ["groups"] });
      history.replaceState(null, "", window.location.pathname); // drop the token from the address bar
      navigate(`/groups/${g.name}`);
    },
  });

  useEffect(() => {
    if (!inviteToken) return;
    // Keep the token across an OIDC redirect without putting it in any request.
    sessionStorage.setItem("pool.pending_invite", inviteToken);
  }, [inviteToken]);

  const t = inviteToken || sessionStorage.getItem("pool.pending_invite") || "";
  if (!session) {
    return (
      <>
        <p>Sign in to accept the invite.</p>
        <Login returnTo="/invite" />
      </>
    );
  }
  return (
    <section className="card narrow">
      <h1>Accept invite</h1>
      {t ? (
        <p>You were invited to a group. Accepting adds you with the role chosen by whoever made the invite.</p>
      ) : (
        <label>
          Invite token
          <input value={manual} onChange={(e) => setManual(e.target.value.trim())} autoComplete="off" />
        </label>
      )}
      <ErrorBox error={accept.error} />
      <button
        type="button"
        disabled={accept.isPending || !(t || manual)}
        onClick={() => {
          sessionStorage.removeItem("pool.pending_invite");
          accept.mutate(t || manual);
        }}
      >
        Accept invite
      </button>
    </section>
  );
}
