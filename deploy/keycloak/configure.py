"""SECURITY-SENSITIVE: authentication configuration. Needs security review (plan §8.10).

Make Keycloak report two-step sign-in in its tokens (plan §8.6), so pool can require it for
coordinators and admins. Idempotent; deploy/install.sh runs it on every install and upgrade.

    KEYCLOAK_ADMIN_PASSWORD=... python3 deploy/keycloak/configure.py --url http://127.0.0.1:8080/idp

What it sets, through Keycloak's admin REST API:
- the "Authentication Method Reference (AMR)" mapper (oidc-amr-mapper) on pool's sign-in clients, which
  puts an `amr` claim in tokens;
- references on the realm's browser sign-in flow: the password step reports "pwd", the authenticator-app
  step (one-time code) reports "otp". The pool server treats "otp" as two-step sign-in.

Keycloak's default browser flow already asks for a one-time code only from people who set up an
authenticator app, so members who never set one up are not affected.
Standard library only (it runs on the server before anything else is installed).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

CLIENTS = ("pool-web", "pool-cli")
# Authenticator in the browser flow -> the amr value it reports. maxAge: how long (seconds) a step counts
# for later sign-ins in the same Keycloak session.
REFERENCES = {"auth-username-password-form": "pwd", "auth-otp-form": "otp"}
MAX_AGE_S = "43200"


class Admin:
    def __init__(self, base: str, realm: str, user: str, password: str) -> None:
        self.base, self.realm = base.rstrip("/"), realm
        data = urllib.parse.urlencode({"grant_type": "password", "client_id": "admin-cli",
                                       "username": user, "password": password}).encode()
        tok = self._call("POST", f"{self.base}/realms/master/protocol/openid-connect/token", data,
                         {"Content-Type": "application/x-www-form-urlencoded"})
        self.token = tok["access_token"]

    def _call(self, method: str, url: str, data: bytes | None = None, headers: dict | None = None):
        if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
            raise SystemExit(f"refusing non-http URL {url!r}")
        req = urllib.request.Request(url, data=data, method=method, headers=headers or {})  # noqa: S310 (checked above)
        with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 (checked above)
            body = r.read()
        return json.loads(body) if body else None

    def api(self, method: str, path: str, body=None):
        data = json.dumps(body).encode() if body is not None else None
        return self._call(method, f"{self.base}/admin/realms/{self.realm}{path}", data,
                          {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})


def ensure_amr_mappers(kc: Admin) -> list[str]:
    changed = []
    for client_id in CLIENTS:
        found = kc.api("GET", f"/clients?clientId={urllib.parse.quote(client_id)}")
        if not found:
            continue
        cid = found[0]["id"]
        mappers = kc.api("GET", f"/clients/{cid}/protocol-mappers/models") or []
        if any(m.get("protocolMapper") == "oidc-amr-mapper" for m in mappers):
            continue
        kc.api("POST", f"/clients/{cid}/protocol-mappers/models", {
            "name": "authentication methods (amr)", "protocol": "openid-connect", "protocolMapper": "oidc-amr-mapper",
            "config": {"access.token.claim": "true", "id.token.claim": "true", "introspection.token.claim": "true"},
        })
        changed.append(f"amr mapper on {client_id}")
    return changed


def ensure_references(kc: Admin) -> list[str]:
    realm = kc.api("GET", "")
    flow = realm.get("browserFlow") or "browser"
    executions = kc.api("GET", f"/authentication/flows/{urllib.parse.quote(flow)}/executions") or []
    changed, seen = [], set()
    for ex in executions:
        ref = REFERENCES.get(ex.get("providerId"))
        if ref is None:
            continue
        seen.add(ex["providerId"])
        wanted = {"default.reference.value": ref, "default.reference.maxAge": MAX_AGE_S}
        if ex.get("authenticationConfig"):
            cfg = kc.api("GET", f"/authentication/config/{ex['authenticationConfig']}")
            if all(cfg.get("config", {}).get(k) == v for k, v in wanted.items()):
                continue
            cfg.setdefault("config", {}).update(wanted)
            kc.api("PUT", f"/authentication/config/{cfg['id']}", cfg)
        else:
            kc.api("POST", f"/authentication/executions/{ex['id']}/config",
                   {"alias": f"pool-amr-{ref}", "config": wanted})
        changed.append(f"{ex['providerId']} reports {ref}")
    missing = set(REFERENCES) - seen
    if missing:
        raise SystemExit(f"the browser flow {flow!r} has no {', '.join(sorted(missing))} step; "
                         "two-step sign-in cannot be reported")
    return changed


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True, help="Keycloak base URL, e.g. http://127.0.0.1:8080/idp")
    ap.add_argument("--realm", default="pool")
    a = ap.parse_args()
    password = os.environ.get("KEYCLOAK_ADMIN_PASSWORD")
    if not password:
        raise SystemExit("set KEYCLOAK_ADMIN_PASSWORD")
    try:
        kc = Admin(a.url, a.realm, os.environ.get("KEYCLOAK_ADMIN", "admin"), password)
        changed = ensure_amr_mappers(kc) + ensure_references(kc)
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"Keycloak admin API: {exc.code} {exc.reason} for {exc.url}") from None
    print("two-step sign-in reporting: " + ("; ".join(changed) if changed else "already set up"))


if __name__ == "__main__":
    sys.exit(main())
