import { useState, type FormEvent } from "react";
import { AUTH_MODE, useAuth } from "../auth/AuthContext";
import { ErrorBox } from "../components/ui";

export function Login({ returnTo }: { returnTo?: string }) {
  const { loginDev, loginOidc } = useAuth();
  const [username, setUsername] = useState("");
  const [mfa, setMfa] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  if (AUTH_MODE === "oidc") {
    return (
      <section className="card narrow">
        <h1>Sign in</h1>
        <p>You sign in with your organization's identity provider.</p>
        <ErrorBox error={error} />
        <button
          type="button"
          onClick={() => loginOidc(returnTo).catch(setError)}
        >
          Sign in
        </button>
      </section>
    );
  }

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await loginDev(username.trim(), mfa);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="card narrow">
      <h1>Sign in (dev mode)</h1>
      <p className="muted">
        Dev mode gives a token for any username without a password. It only works when the control plane runs
        with <code>POOL_DEV_MODE=true</code>.
      </p>
      <form onSubmit={submit}>
        <label>
          Username
          <input
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            pattern="[a-z0-9][a-z0-9._\-]*"
            title="lower-case letters, digits, dot, dash, underscore"
            placeholder="alice"
            required
            autoFocus
          />
        </label>
        <label className="check">
          <input type="checkbox" checked={mfa} onChange={(e) => setMfa(e.target.checked)} /> Pretend MFA was used
        </label>
        <ErrorBox error={error} />
        <button type="submit" disabled={busy || !username.trim()}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </section>
  );
}

export function AuthCallback() {
  // Rendered at /auth/callback; App handles the redirect once completeOidc resolves.
  return <p className="muted">Finishing sign-in…</p>;
}
