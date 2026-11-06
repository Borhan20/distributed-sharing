"""Private images end to end, with a real registry and the real agent.

A CNCF Distribution registry (Apache-2.0) in token-auth mode, like GHCR and Docker Hub, plus a small token
service (this file) that signs short-lived tokens with a test CA. The image is private: an anonymous pull
is refused. A group with a read-only credential runs a job from it; the agent only ever sees a token for
that one image. A group without credentials cannot use it.

    cd agent && CGO_ENABLED=0 go build -o bin/pool-agent ./cmd/pool-agent
    uv run pytest -m e2e -s tests/e2e/test_private_images.py
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import secrets
import subprocess
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import jwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from .conftest import free_port
from .test_agent_e2e import _requirements, busybox_image, containers, wait_job  # noqa: F401
from .test_object_store import cleanup, make_platform  # noqa: F401

pytestmark = [pytest.mark.e2e, pytest.mark.docker, pytest.mark.asyncio]

REGISTRY_IMAGE = "registry:2.8.3@sha256:a3d8aaa63ed8681a604f1dea0aa03f100d5895b6a58ace528858a7b332415373"
SERVICE, ISSUER = "pool-test-registry", "pool-test-token-issuer"


class TokenService:
    """Issues registry tokens like GHCR's or Docker Hub's: Basic auth in, a signed JWT for one scope out."""

    def __init__(self, users: dict[str, tuple[str, list[str]]]) -> None:
        self.users = users  # name -> (password, actions)
        self.key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "pool test token issuer")])
        now = dt.datetime.now(dt.UTC)
        self.cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(self.key.public_key())
                     .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(minutes=5))
                     .not_valid_after(now + dt.timedelta(days=1))
                     .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                     .sign(self.key, hashes.SHA256()))
        self.issued: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):  # noqa: N802
                q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                user, actions = None, []
                auth = self.headers.get("Authorization", "")
                if auth.startswith("Basic "):
                    name, _, pw = base64.b64decode(auth[6:]).decode().partition(":")
                    if name in outer.users and outer.users[name][0] == pw:
                        user, actions = name, outer.users[name][1]
                    else:
                        self.send_response(401)
                        self.end_headers()
                        return
                access = []
                for scope in q.get("scope", []):
                    kind, repo, wanted = scope.split(":")
                    granted = [a for a in wanted.split(",") if a in actions]
                    access.append({"type": kind, "name": repo, "actions": granted})
                now = int(time.time())
                claims = {"iss": ISSUER, "sub": user or "", "aud": SERVICE, "iat": now, "nbf": now - 10,
                          "exp": now + 300, "jti": secrets.token_hex(8), "access": access}
                x5c = [base64.b64encode(outer.cert.public_bytes(serialization.Encoding.DER)).decode()]
                token = jwt.encode(claims, outer.key, algorithm="ES256", headers={"x5c": x5c})
                outer.issued.append({"user": user, "access": access})
                body = json.dumps({"token": token, "access_token": token, "expires_in": 300}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/token"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def cert_pem(self) -> bytes:
        return self.cert.public_bytes(serialization.Encoding.PEM)


def docker(*args: str, input_text: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, input=input_text, check=check,
                          timeout=300)


@pytest.fixture(scope="module")
def private_registry(tmp_path_factory):
    """A token-auth registry holding one private image; yields (registry host, image, reader credentials)."""
    reader_pw, pusher_pw = secrets.token_hex(12), secrets.token_hex(12)  # generated per run
    tokens = TokenService({"lab-reader": (reader_pw, ["pull"]), "lab-pusher": (pusher_pw, ["pull", "push"])})
    d = tmp_path_factory.mktemp("registry")
    (d / "root.crt").write_bytes(tokens.cert_pem())
    (d / "root.crt").chmod(0o644)
    port, name = free_port(), f"pool-e2e-registry-{uuid.uuid4().hex[:8]}"
    docker("run", "-d", "--name", name, "-p", f"127.0.0.1:{port}:5000",
           "-e", "REGISTRY_AUTH=token", "-e", f"REGISTRY_AUTH_TOKEN_REALM={tokens.url}",
           "-e", f"REGISTRY_AUTH_TOKEN_SERVICE={SERVICE}", "-e", f"REGISTRY_AUTH_TOKEN_ISSUER={ISSUER}",
           "-e", "REGISTRY_AUTH_TOKEN_ROOTCERTBUNDLE=/certs/root.crt",
           "-v", f"{d}:/certs:ro", REGISTRY_IMAGE)
    host = f"127.0.0.1:{port}"
    pusher_cfg, empty_cfg = d / "pusher-docker", d / "anonymous-docker"
    pusher_cfg.mkdir(mode=0o700)
    empty_cfg.mkdir(mode=0o700)
    try:
        for _ in range(60):
            try:
                if httpx.get(f"http://{host}/v2/", timeout=2).status_code == 401:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        docker("pull", "-q", busybox_image())
        repo = f"{host}/lab/private-app"
        docker("tag", busybox_image(), f"{repo}:1")
        docker("--config", str(pusher_cfg), "login", host, "-u", "lab-pusher", "--password-stdin",
               input_text=pusher_pw)
        docker("--config", str(pusher_cfg), "push", "-q", f"{repo}:1")
        digest = docker("image", "inspect", "--format", "{{range .RepoDigests}}{{println .}}{{end}}", f"{repo}:1"
                        ).stdout.split()
        image = next(x for x in digest if x.startswith(repo + "@sha256:"))
        docker("image", "rm", f"{repo}:1", image, check=False)  # the agent must pull it
        yield {"host": host, "image": image, "reader": ("lab-reader", reader_pw), "empty_cfg": str(empty_cfg),
               "tokens": tokens}
    finally:
        docker("rm", "-f", name, check=False)
        tokens.server.shutdown()


async def test_the_image_is_really_private(private_registry):
    r = docker("--config", private_registry["empty_cfg"], "pull", private_registry["image"], check=False)
    out = (r.stderr + r.stdout).lower()
    assert r.returncode != 0 and ("unauthorized" in out or "denied" in out), r.stderr


async def test_group_with_read_only_credentials_runs_the_private_image(private_registry, control_plane_factory,
                                                                        tmp_path, cleanup):  # noqa: F811
    cp, http, agent, _ = await make_platform(control_plane_factory, tmp_path)
    cleanup.append((agent, http))
    user, pw = private_registry["reader"]
    r = await http.put("/api/v1/groups/e2e/registries",
                       json={"registry": private_registry["host"], "username": user, "token": pw})
    assert r.status_code == 200, r.text
    agent.start()
    spec = {"apiVersion": "pool/v1alpha1", "kind": "Job", "metadata": {"name": "private"},
            "spec": {"image": private_registry["image"], "command": ["sh", "-c", "echo ran the private image"],
                     "resources": {"cpu": 0.5, "memory": "64Mi", "disk": "16Mi"}, "retries": 0, "max_runtime": "5m"}}
    job_id = (await http.post("/api/v1/groups/e2e/jobs", json=spec)).json()["id"]
    j = await wait_job(http, job_id, 180)
    logs = "".join(x["chunk"] for x in (await http.get(f"/api/v1/jobs/{job_id}/tasks/0/logs")).json())
    assert j["state"] == "succeeded", (j, logs)
    assert "ran the private image" in logs
    pulls = [i for i in private_registry["tokens"].issued if i["user"] == "lab-reader" and i["access"]]
    assert pulls and all(a["actions"] == ["pull"] and a["name"] == "lab/private-app"
                         for i in pulls for a in i["access"]), "only pull tokens for that one repository"
    audit = (await http.get("/api/v1/groups/e2e/audit")).json()
    assert any(e["action"] == "image.pull_token" for e in audit)

    # The same image in a group without credentials: the agent cannot pull it.
    await http.post("/api/v1/groups", json={"name": "no-creds", "display_name": "No creds"})
    devices = (await http.get("/api/v1/devices")).json()
    await http.put(f"/api/v1/devices/{devices[0]['id']}/shares/no-creds")
    subprocess.run([str(agent_bin()), "share", "no-creds"], env=agent.env, check=True, capture_output=True)
    job2 = (await http.post("/api/v1/groups/no-creds/jobs", json=spec)).json()["id"]
    j2 = await wait_job(http, job2, 180)
    task2 = (await http.get(f"/api/v1/jobs/{job2}/tasks")).json()[0]
    # The image is still on that computer from the first job; the agent must not let this group use it.
    assert j2["state"] == "failed" and "another group" in (task2["result"] or {}).get("error", ""), task2


def agent_bin():
    from .test_agent_e2e import AGENT_BIN

    return AGENT_BIN
