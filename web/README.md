# pool web UI

React + TypeScript + Vite + TanStack Query (plan §5.2). It shows what plan §5.1 lists: groups, members, join
requests, invites, devices, sharing, jobs, logs, the per-device "what ran here" view and the audit log.

## Develop

```bash
npm install
npm run gen:api        # regenerate src/api/schema.d.ts from openapi.json
npm run dev            # http://localhost:5173, proxies /api and /dev to http://127.0.0.1:8000 (POOL_API to change)
npm run typecheck
npm test -- --run      # vitest: job-file parsing and API error formatting
npm run build          # -> dist/, which the control plane serves at / when it exists
npm run licenses       # prints each direct dependency's license, fails on anything not allowlisted
```

`openapi.json` is exported from the control plane (`create_app().openapi()`). Re-export it and run
`npm run gen:api` whenever the API changes; `npm run typecheck` then shows every place that needs updating.

## Sign-in modes

Set `VITE_AUTH_MODE` at build time (see `.env.example`):

| Mode | How | Use |
|---|---|---|
| `dev` (default) | Username box → `POST /dev/token`. A red "DEV MODE — no password" banner is always shown. | Local development only. Works only when the control plane runs with `POOL_DEV_MODE=true`. |
| `oidc` | Authorization code flow with PKCE through `oidc-client-ts`. Needs `VITE_OIDC_AUTHORITY` and `VITE_OIDC_CLIENT_ID`; the IdP must allow the redirect URI `<origin>/auth/callback`. | Real deployments (Keycloak or any OIDC provider). Not yet tested against a live IdP. |

**Token storage trade-off.** The access token is held in memory and mirrored to `sessionStorage`. The mirror
means a reload in the same tab keeps you signed in, and the token is gone when the tab closes; it is not shared
between tabs the way `localStorage` would be. Any script running on the page (XSS) could still read it, as it
could read memory. To limit that, the UI never renders HTML from the API; React escapes all text. An HttpOnly
cookie session through a backend-for-frontend would be stronger, but the API currently accepts bearer tokens
only. Expired tokens are dropped at start-up, and any 401 signs you out.

**Invite links** look like `/invite#token=…`. The token is in the URL fragment, so browsers never send it to the
server, proxies or access logs. During an OIDC redirect it is kept in `sessionStorage`, then removed from the
address bar after it is accepted.

## Destructive actions

Revoking a device, removing a member and leaving a group open a confirmation dialog that names the target.
Closing a group also asks you to type the group's name.

## Dependencies and licenses

Direct dependencies, checked from each package's own `package.json` with `npm run licenses`:

| Package | Version installed | License |
|---|---|---|
| react, react-dom | 18.3.1 | MIT |
| react-router-dom | 6.30.6 | MIT |
| @tanstack/react-query | 5.105.2 | MIT |
| openapi-fetch | 0.13.8 | MIT |
| oidc-client-ts | 3.5.0 | Apache-2.0 |
| yaml | 2.9.1 | ISC |
| openapi-typescript (dev) | 7.13.0 | MIT |
| vite (dev) | 5.4.21 | MIT |
| @vitejs/plugin-react (dev) | 4.7.0 | MIT |
| vitest (dev) | 2.1.9 | MIT |
| typescript (dev) | 5.6.3 | Apache-2.0 |
| @types/react, @types/react-dom (dev) | 18.3.x | MIT |

Nothing is flagged. Transitive dependencies are not audited here; add a full license scan (for example
`license-checker`) to CI before release (plan §14).

Versions are deliberately one major behind the newest releases (React 19, Vite 8, TypeScript 7 exist): these are
the versions this code was written and tested against. Upgrade on purpose, not by accident.

## Not done yet

- No component or browser (end-to-end) tests; only the pure helpers have unit tests.
- OIDC mode has not been run against a real IdP.
- Group settings cover the approval rule, probation and retention only (not expiry or the registry allowlist).
- Logs are read by polling (`after_id` cursor every 2 s), not streamed.
