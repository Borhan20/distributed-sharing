import { useEffect, useState } from "react";
import { NavLink, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { AUTH_MODE, useAuth } from "./auth/AuthContext";
import { ErrorBox, Loading } from "./components/ui";
import { AcceptInvitePage } from "./pages/AcceptInvite";
import { DevicePage } from "./pages/DevicePage";
import { DevicesPage } from "./pages/Devices";
import { GroupPage } from "./pages/GroupPage";
import { GroupsPage } from "./pages/Groups";
import { JobPage } from "./pages/JobPage";
import { Login } from "./pages/Login";

function OidcCallback() {
  const { completeOidc } = useAuth();
  const navigate = useNavigate();
  const [error, setError] = useState<unknown>(null);
  useEffect(() => {
    completeOidc()
      .then((to) => navigate(to, { replace: true }))
      .catch(setError);
  }, [completeOidc, navigate]);
  return error ? <ErrorBox error={error} /> : <Loading what="Finishing sign-in" />;
}

export function App() {
  const { token, me, loading, error, logout } = useAuth();
  const location = useLocation();
  const publicRoute = location.pathname === "/invite" || location.pathname === "/auth/callback";

  let body;
  if (location.pathname === "/auth/callback") body = <OidcCallback />;
  else if (location.pathname === "/invite") body = <AcceptInvitePage />;
  else if (!token) body = <Login returnTo={location.pathname} />;
  else if (loading && !me) body = <Loading />;
  else
    body = (
      <Routes>
        <Route path="/" element={<GroupsPage />} />
        <Route path="/groups/:name" element={<GroupPage />} />
        <Route path="/jobs/:id" element={<JobPage />} />
        <Route path="/devices" element={<DevicesPage />} />
        <Route path="/devices/:id" element={<DevicePage />} />
        <Route path="*" element={<p>Page not found.</p>} />
      </Routes>
    );

  return (
    <>
      {AUTH_MODE === "dev" && (
        <div className="dev-banner" role="note">
          DEV MODE — no password. Anyone can sign in as anyone. Never use this on a shared server.
        </div>
      )}
      <header className="topbar">
        <NavLink to="/" className="brand">
          pool
        </NavLink>
        {token && !publicRoute && (
          <nav aria-label="Main">
            <NavLink to="/" end>
              Groups
            </NavLink>
            <NavLink to="/devices">My devices</NavLink>
          </nav>
        )}
        <div className="spacer" />
        {token && (
          <div className="user">
            <span>
              {me?.handle ?? "…"}
              {me && !me.mfa && <span className="muted" title="This session did not use multi-factor authentication"> (no MFA)</span>}
            </span>
            <button type="button" className="secondary small" onClick={logout}>
              Sign out
            </button>
          </div>
        )}
      </header>
      <main>
        {error && token && <ErrorBox error={new Error(error)} />}
        {body}
      </main>
    </>
  );
}
