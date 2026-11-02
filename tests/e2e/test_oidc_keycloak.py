"""OIDC login against a real Keycloak (plan §8.6) using deploy/keycloak/pool-realm.json.

Starts Keycloak in a throwaway container, creates a synthetic user and a
temporary password-grant client through the admin API (test only; the realm
file itself has neither), then checks that the control plane's verifier
accepts the token and rejects tampered or wrong-audience tokens.
"""

from __future__ import annotations

import secrets
import shutil
import subprocess
import time
from pathlib import Path

import httpx
import pytest

from pool_cp.auth import TokenError, TokenVerifier

pytestmark = [pytest.mark.e2e, pytest.mark.docker]
ROOT = Path(__file__).resolve().parents[2]
IMAGE = "quay.io/keycloak/keycloak:26.0"


@pytest.fixture(scope="module")
def keycloak():
    if not shutil.which("docker"):
        pytest.skip("docker not available")
    admin_pw = secrets.token_urlsafe(16)
    name = f"pool-kc-{secrets.token_hex(4)}"
    port = 18080 + secrets.randbelow(1000)
    subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", name, "-p", f"127.0.0.1:{port}:8080",
         "-e", "KC_BOOTSTRAP_ADMIN_USERNAME=admin", "-e", f"KC_BOOTSTRAP_ADMIN_PASSWORD={admin_pw}",
         "-v", f"{ROOT / 'deploy/keycloak/pool-realm.json'}:/opt/keycloak/data/import/pool-realm.json:ro",
         IMAGE, "start-dev", "--import-realm"], check=True, capture_output=True)
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 180
        while time.time() < deadline:
            try:
                if httpx.get(f"{base}/realms/pool/.well-known/openid-configuration", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(2)
        else:
            raise RuntimeError("keycloak did not start")
        yield base, admin_pw
    finally:
        subprocess.run(["docker", "stop", name], capture_output=True)


def _admin_token(base: str, pw: str) -> str:
    r = httpx.post(f"{base}/realms/master/protocol/openid-connect/token",
                   data={"grant_type": "password", "client_id": "admin-cli", "username": "admin", "password": pw})
    r.raise_for_status()
    return r.json()["access_token"]


def test_keycloak_token_is_accepted(keycloak):
    base, admin_pw = keycloak
    h = {"Authorization": f"Bearer {_admin_token(base, admin_pw)}"}
    user_pw = secrets.token_urlsafe(16)
    r = httpx.post(f"{base}/admin/realms/pool/users", headers=h, json={
        "username": "synthetic-researcher", "enabled": True, "emailVerified": True,
        "firstName": "Synthetic", "lastName": "Researcher", "email": "synthetic-researcher@example.org",
        "credentials": [{"type": "password", "value": user_pw, "temporary": False}]})
    assert r.status_code in (201, 409), r.text
    # Temporary test-only client with the password grant (never in the realm file).
    r = httpx.post(f"{base}/admin/realms/pool/clients", headers=h, json={
        "clientId": "pool-test", "publicClient": True, "directAccessGrantsEnabled": True,
        "standardFlowEnabled": False,
        "protocolMappers": [{"name": "aud", "protocol": "openid-connect", "protocolMapper": "oidc-audience-mapper",
                             "config": {"included.custom.audience": "pool-api", "access.token.claim": "true"}}]})
    assert r.status_code in (201, 409), r.text
    tok = httpx.post(f"{base}/realms/pool/protocol/openid-connect/token", data={
        "grant_type": "password", "client_id": "pool-test", "username": "synthetic-researcher",
        "password": user_pw, "scope": "openid"}).json()["access_token"]

    v = TokenVerifier(issuer=f"{base}/realms/pool", audience="pool-api")
    claims = v.verify(tok)
    assert claims["preferred_username"] == "synthetic-researcher"
    # Wrong audience, tampered signature and wrong issuer are rejected.
    with pytest.raises(TokenError):
        TokenVerifier(issuer=f"{base}/realms/pool", audience="other-api").verify(tok)
    head, body, sig = tok.split(".")
    with pytest.raises(TokenError):
        v.verify(f"{head}.{body}.{sig[:-4]}AAAA")
    with pytest.raises(TokenError):
        TokenVerifier(issuer=f"{base}/realms/other", audience="pool-api",
                      jwks_url=f"{base}/realms/pool/protocol/openid-connect/certs").verify(tok)


def test_realm_clients_are_configured(keycloak):
    base, admin_pw = keycloak
    h = {"Authorization": f"Bearer {_admin_token(base, admin_pw)}"}
    clients = {c["clientId"]: c for c in httpx.get(f"{base}/admin/realms/pool/clients", headers=h).json()}
    web, cli = clients["pool-web"], clients["pool-cli"]
    assert web["publicClient"] and web["standardFlowEnabled"] and not web["directAccessGrantsEnabled"]
    assert web["attributes"].get("pkce.code.challenge.method") == "S256"
    assert cli["attributes"].get("oauth2.device.authorization.grant.enabled") == "true"
