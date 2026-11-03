"""Acceptance check for a server installed with deploy/install.sh (docs/design/easy-onboarding.md, step 1).

Run from another machine against the installed server, the way people would use it:

    python tests/vm/server_acceptance.py --address 10.0.0.5 --ca pool-ca.crt \
        --admin admin --admin-password ONE-TIME-PASSWORD --agent agent/bin/pool-agent

1. HTTPS on one address: the API, sign-in and the file store answer; http redirects to https;
   Keycloak's admin console and master realm are not public; internal ports are closed.
2. Real sign-in in headless Chrome: the admin signs in with the one-time password and must choose a
   new one; a second person registers on the sign-in page.
3. Server rules: only the server admin may create groups (POOL_GROUP_CREATORS=admins).
4. A computer joins over HTTPS (pinned CA), runs a job, and its output downloads through the same address.

Needs google-chrome, the `websockets` Python package and Docker on this machine (for the agent's jobs).
Synthetic accounts only. Prints one line per check; exits non-zero on the first failure.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.parse

import httpx
import websockets

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import totp  # noqa: E402

IMAGE = "docker.io/library/busybox@sha256:73aaf090f3d85aa34ee199857f03fa3a95c8ede2ffd4cc2cdb5b94e566b11662"


def ok(msg: str) -> None:
    print(f"  ok  {msg}", flush=True)


def check(cond: bool, msg: str) -> None:
    if not cond:
        print(f"FAIL  {msg}", flush=True)
        sys.exit(1)
    ok(msg)


# --------------------------------------------------------------------------- browser


class Browser:
    """Headless Chrome over the DevTools protocol, trusting nothing but what we tell it."""

    def __init__(self) -> None:
        self.proc = None
        self.ws = None
        self.n = 0

    async def __aenter__(self) -> Browser:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        self.profile = tempfile.mkdtemp()
        # The test CA is not in Chrome's store; the HTTPS checks above verify certificates with it.
        args = [shutil.which("google-chrome"), "--headless=new", f"--remote-debugging-port={port}",
                f"--user-data-dir={self.profile}", "--ignore-certificate-errors", "--no-first-run",
                "--window-size=1200,1000", "about:blank"]
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                pages = httpx.get(f"http://127.0.0.1:{port}/json", timeout=1).json()
                url = next(p["webSocketDebuggerUrl"] for p in pages if p["type"] == "page")
                break
            except Exception:  # noqa: BLE001 - Chrome still starting
                await asyncio.sleep(0.2)
        self.ws = await websockets.connect(url, max_size=2**24)
        await self.call("Page.enable")
        return self

    async def __aexit__(self, *exc) -> None:
        await self.ws.close()
        self.proc.terminate()
        self.proc.wait(10)
        shutil.rmtree(self.profile, ignore_errors=True)

    async def call(self, method: str, **params):
        self.n += 1
        await self.ws.send(json.dumps({"id": self.n, "method": method, "params": params}))
        while True:
            msg = json.loads(await self.ws.recv())
            if msg.get("id") == self.n:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    async def js(self, expr: str):
        r = await self.call("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
        return r.get("result", {}).get("value")

    async def goto(self, url: str) -> None:
        await self.call("Page.navigate", url=url)
        await asyncio.sleep(2)

    async def wait_for(self, expr: str, what: str, timeout: float = 30) -> None:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if await self.js(expr):
                return
            await asyncio.sleep(0.5)
        raise AssertionError(f"timed out waiting for {what}; page: {await self.js('location.href')}")

    async def fill_and_submit(self, fields: dict[str, str],
                              submit: str = "input[type=submit],button[type=submit]") -> None:
        for name, value in fields.items():
            await self.js(f"""(() => {{ const e = document.querySelector('[name="{name}"]');
                e.value = {json.dumps(value)}; e.dispatchEvent(new Event('input', {{bubbles: true}})); }})()""")
        await self.js(f"document.querySelector({json.dumps(submit)}).click()")
        await asyncio.sleep(2)

    async def app_sign_in(self, base: str) -> None:
        """From the pool web app to Keycloak's sign-in page."""
        await self.goto(base + "/")
        await self.wait_for("[...document.querySelectorAll('button')].some(b => b.textContent.trim() === 'Sign in')",
                            "the app's Sign in button")
        await self.js("[...document.querySelectorAll('button')].find(b => b.textContent.trim() === 'Sign in').click()")
        await self.wait_for("!!document.querySelector('#kc-form-login, #kc-register-form')", "Keycloak's sign-in page")

    async def token(self, previous: str | None = None) -> str:
        cond = "!!sessionStorage.getItem('pool.access_token')"
        if previous:
            cond += f" && sessionStorage.getItem('pool.access_token') !== {json.dumps(previous)}"
        await self.wait_for(cond, "the app to receive a token", 45)
        return await self.js("sessionStorage.getItem('pool.access_token')")

    async def click_button(self, text: str, timeout: float = 30) -> None:
        find = f"[...document.querySelectorAll('button')].find(b => b.textContent.trim() === {json.dumps(text)})"
        await self.wait_for(f"!!{find}", f"the button {text!r}", timeout)
        await self.js(f"{find}.click()")
        await asyncio.sleep(2)


# --------------------------------------------------------------------------- checks


def https_checks(address: str, ca: str) -> None:
    base = f"https://{address}"
    with httpx.Client(verify=ca, timeout=15, follow_redirects=False) as c:
        ready = c.get(base + "/readyz").json()
        check(ready == {"ok": True, "object_store": "ready"}, f"HTTPS with the server's CA: /readyz {ready}")
        cfg = c.get(base + "/api/v1/config").json()
        check(cfg == {"auth": "oidc", "oidc_authority": f"{base}/idp/realms/pool", "oidc_client_id": "pool-web"},
              "web UI is told to use the server's sign-in")
        disco = c.get(base + "/idp/realms/pool/.well-known/openid-configuration").json()
        check(disco["issuer"] == f"{base}/idp/realms/pool", "Keycloak issuer is the public address")
        for private in ("/idp/admin/", "/idp/realms/master/.well-known/openid-configuration"):
            check(c.get(base + private).status_code == 404, f"{private} is not public")
        r = c.get(base + "/pool-artifacts/")
        check(r.status_code in (403, 401),
              f"file store answers on the same address and refuses anonymous access ({r.status_code})")
        hsts = c.get(base + "/").headers.get("strict-transport-security", "")
        check("max-age" in hsts, "HSTS header set")
    r = httpx.get(f"http://{address}/guide", follow_redirects=False, timeout=10)
    redirected = r.status_code in (301, 302, 307, 308) and r.headers["location"].startswith("https://")
    check(redirected, "http redirects to https")
    for port in (8000, 8080, 8333, 5432, 9000):
        with socket.socket() as s:
            s.settimeout(3)
            closed = s.connect_ex((address, port)) != 0
        check(closed, f"port {port} is not reachable from outside")


async def browser_sign_in(address: str, admin: str, one_time: str) -> tuple[str, str, str]:
    base = f"https://{address}"
    new_password = secrets.token_urlsafe(16)
    async with Browser() as b:
        await b.app_sign_in(base)
        await b.fill_and_submit({"username": admin, "password": one_time})
        await b.wait_for("!!document.querySelector('[name=password-new]')", "the 'choose a new password' page")
        ok("first sign-in with the one-time password asks for a new password")
        await b.fill_and_submit({"password-new": new_password, "password-confirm": new_password})
        password_token = await b.token()
        ok("admin signed in to the web app")

        # Two-step sign-in, as the banner offers it to coordinators and admins.
        await b.click_button("Set up two-step sign-in")
        await b.wait_for("!!document.querySelector('[name=totp]')", "the authenticator setup page")
        await b.js("document.querySelector('#mode-manual')?.click()")
        await asyncio.sleep(1.5)
        key = (await b.js("document.querySelector('#kc-totp-secret-key')?.textContent") or "").strip()
        check(bool(key), "Keycloak shows a key to type into an authenticator app")
        first = totp.code(key)
        await b.fill_and_submit({"totp": first, "userLabel": "test phone"})
        setup_token = await b.token(previous=password_token)
        await b.click_button("Sign in again with my code")
        await b.fill_and_submit({"username": admin, "password": new_password})
        await b.wait_for("!!document.querySelector('[name=otp]')", "the one-time code page")
        while totp.code(key) == first:  # a code works only once
            await asyncio.sleep(2)
        await b.fill_and_submit({"otp": totp.code(key)})
        admin_token = await b.token(previous=setup_token)
        ok("admin set up an authenticator app and signed in with a code")

    member = f"member-{secrets.token_hex(3)}"
    member_password = secrets.token_urlsafe(16)
    async with Browser() as b:
        await b.app_sign_in(base)
        await b.wait_for("!!document.querySelector('a[href*=registration]')", "a Register link")
        await b.js("document.querySelector('a[href*=registration]').click()")
        await b.wait_for("!!document.querySelector('#kc-register-form')", "the registration form")
        await b.fill_and_submit({"username": member, "email": f"{member}@example.org", "firstName": "Test",
                                 "lastName": "Member", "password": member_password,
                                 "password-confirm": member_password})
        member_token = await b.token()
        ok(f"a new person registered on the sign-in page ({member})")
    return admin_token, member_token, member


def api_rules(address: str, ca: str, admin_token: str, member_token: str) -> str:
    base = f"https://{address}"
    admin = httpx.Client(base_url=base, verify=ca, timeout=30, headers={"Authorization": f"Bearer {admin_token}"})
    member = httpx.Client(base_url=base, verify=ca, timeout=30, headers={"Authorization": f"Bearer {member_token}"})
    me = admin.get("/api/v1/me").json()
    check(me["server_admin"] and me["can_create_groups"], f"admin is server admin ({me['handle']})")
    check(me["mfa"] and me["mfa_required"], "the server sees the admin's two-step sign-in")
    check(member.get("/api/v1/me").json()["can_create_groups"] is False, "a registered stranger cannot create groups")
    check(member.post("/api/v1/groups", json={"name": "not-allowed", "display_name": "x"}).status_code == 403,
          "the server refuses it too")
    group = f"lab-{secrets.token_hex(3)}"
    check(admin.post("/api/v1/groups", json={"name": group, "display_name": "Lab"}).status_code == 201,
          f"admin created group {group}")
    r = admin.post(f"/api/v1/groups/{group}/invites", json={"role": "member", "max_uses": 1, "expires_in_hours": 24})
    check(r.status_code == 201, f"admin created an invite link ({r.status_code} {r.text[:120]})")
    r = member.post("/api/v1/invites/accept", json={"token": r.json()["token"]})
    check(r.status_code == 200 and r.json()["my_role"] == "member", "the registered person accepted it and is a member")
    return group


def agent_job(address: str, ca: str, admin_token: str, group: str, agent_bin: str) -> None:
    base = f"https://{address}"
    admin = httpx.Client(base_url=base, verify=ca, timeout=30, headers={"Authorization": f"Bearer {admin_token}"})
    home = tempfile.mkdtemp(prefix="pool-agent-accept-")
    env = {**os.environ, "POOL_AGENT_HOME": home}
    code = admin.post("/api/v1/devices/enrollment-codes").json()["code"]
    ca_abs = os.path.abspath(ca)
    r = subprocess.run([agent_bin, "enroll", "--server", base, "--code", code, "--name", "acceptance-pc",
                        "--server-ca", ca_abs], env=env, capture_output=True, text=True, timeout=60)
    check(r.returncode == 0, f"agent enrolled over HTTPS with the pinned CA {r.stderr.strip()[:200]}")
    r = subprocess.run([agent_bin, "share", group, "--cpu-percent", "50", "--memory", "1Gi"], env=env,
                       capture_output=True, text=True, timeout=30)
    check(r.returncode == 0, "pool-agent share")
    log = open(os.path.join(home, "agent.log"), "wb")  # noqa: SIM115
    proc = subprocess.Popen([agent_bin, "run"], env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        deadline = time.time() + 60
        while time.time() < deadline:
            devs = admin.get("/api/v1/devices").json()
            if any(d["online"] and group in d["shared_with"] for d in devs):
                break
            time.sleep(1)
        else:
            check(False, "computer online and listed in the group")
        ok("computer online and listed in the group (one-step sharing)")
        spec = {"apiVersion": "pool/v1alpha1", "kind": "Job", "metadata": {"name": "accept"},
                "spec": {"image": IMAGE, "command": ["sh", "-c", "echo hello from the vm test > /out/result.txt"],
                         "resources": {"cpu": 0.5, "memory": "128Mi", "disk": "64Mi"}, "max_runtime": "5m"}}
        job = admin.post(f"/api/v1/groups/{group}/jobs", json=spec).raise_for_status().json()["id"]
        for _ in range(180):
            j = admin.get(f"/api/v1/jobs/{job}").json()
            if j["state"] in ("succeeded", "failed", "cancelled"):
                break
            time.sleep(1)
        logs = "".join(x["chunk"] for x in admin.get(f"/api/v1/jobs/{job}/tasks/0/logs").json())
        check(j["state"] == "succeeded", f"job ran on the computer ({j['state']}) {logs.strip()[-200:]}")
        check("uploaded 1 of 1 outputs" in logs, "agent uploaded its output through https://ADDRESS/pool-artifacts")
        link = admin.get(f"/api/v1/jobs/{job}/tasks/0/download", params={"path": "result.txt"}).json()
        check(urllib.parse.urlsplit(link["url"]).netloc == address, "download link uses the one public address")
        body = httpx.get(link["url"], verify=ca, timeout=30).content
        check(body == b"hello from the vm test\n", "downloaded output matches")
    finally:
        proc.terminate()
        proc.wait(20)
        shutil.rmtree(home, ignore_errors=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--address", required=True)
    ap.add_argument("--ca", required=True, help="the server's CA certificate (deploy/pool-ca.crt)")
    ap.add_argument("--admin", required=True)
    ap.add_argument("--admin-password", required=True, help="the one-time password the installer printed")
    ap.add_argument("--agent", required=True, help="path to a pool-agent binary")
    a = ap.parse_args()
    ssl.create_default_context(cafile=a.ca)  # fail early on a bad CA file
    started = time.time()
    print("1. HTTPS on one address")
    https_checks(a.address, a.ca)
    print("2. Sign-in in a real browser")
    admin_token, member_token, _ = asyncio.run(browser_sign_in(a.address, a.admin, a.admin_password))
    print("3. Server rules")
    group = api_rules(a.address, a.ca, admin_token, member_token)
    print("4. A computer joins and runs a job")
    agent_job(a.address, a.ca, admin_token, group, a.agent)
    print(f"all checks passed in {time.time() - started:.0f} s")


if __name__ == "__main__":
    main()
