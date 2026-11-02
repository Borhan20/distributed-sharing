"""A protocol-level fake agent for tests and load spikes (no containers)."""

from __future__ import annotations

import asyncio
import base64
import json
import uuid
from typing import Any

import httpx
import websockets
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.x509.oid import NameOID

from pool_protocol import messages as pm


def share(group: str, active: bool = True, accept_from: Any = "all-members", accept_new: bool = False) -> dict:
    return {"group": group, "limits": {"cpu_percent": 100, "memory_mb": 65536, "disk_mb": 100000, "gpu": False},
            "accept_from": accept_from, "accept_new_members": accept_new, "active_now": active}


CAPS = {"os": "linux", "arch": "amd64", "cpu_cores": 8, "memory_mb": 32768, "disk_mb": 200000, "gpus": [],
        "tiers": ["T1", "T2"], "gpu_tiers": [], "always_on": False}
FREE = {"cpu": 8, "memory_mb": 32768, "disk_mb": 100000, "gpus": 0}


class FakeAgent:
    def __init__(self, base_url: str, name: str = "fake") -> None:
        self.base_url = base_url
        self.name = name
        self.key = Ed25519PrivateKey.generate()
        self.device_id: str | None = None
        self.cert_pem: str | None = None
        self.signing_keys: dict[str, Ed25519PublicKey] = {}
        self.ws = None
        self.welcome: pm.Welcome | None = None
        self.inbox: list = []

    def csr_pem(self, key: Ed25519PrivateKey | None = None) -> str:
        csr = (x509.CertificateSigningRequestBuilder()
               .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "pending")]))
               .sign(key or self.key, None))
        return csr.public_bytes(serialization.Encoding.PEM).decode()

    async def enroll(self, http: httpx.AsyncClient, code: str) -> httpx.Response:
        body = {"one_time_code": code, "csr_pem": self.csr_pem(),
                "device_facts": {"name": self.name, "os": "linux", "arch": "amd64", "agent_version": "0.1.0-test"}}
        r = await http.post("/api/v1/agent/enroll", json=body)
        if r.status_code == 200:
            data = r.json()
            self.device_id = data["device_id"]
            self.cert_pem = data["cert_pem"]
            self.signing_keys = {k["key_id"]: Ed25519PublicKey.from_public_bytes(base64.b64decode(k["public_key_b64"]))
                                 for k in data["signing_keys"]}
        return r

    async def connect(self, shares: list[dict], *, caps: dict | None = None, running: list[dict] | None = None,
                      sign_with: Ed25519PrivateKey | None = None, cert_pem: str | None = None) -> pm.Welcome:
        ws_url = self.base_url.replace("http", "ws", 1) + "/api/v1/agent/ws"
        self.ws = await websockets.connect(ws_url, max_size=2**20)
        challenge = pm.parse_server_message(await self.ws.recv())
        assert isinstance(challenge, pm.Challenge)
        nonce = base64.b64decode(challenge.nonce_b64)
        sig = (sign_with or self.key).sign(pm.HELLO_SIGNATURE_CONTEXT + nonce)
        hello = pm.Hello(agent_version="0.1.0-test", device_id=self.device_id or str(uuid.uuid4()), profile="default",
                         cert_pem=cert_pem or self.cert_pem or "", nonce_signature_b64=base64.b64encode(sig).decode(),
                         capabilities=caps or CAPS, shares=shares, policy_digest="sha256:" + "0" * 64,
                         running=running or [])
        await self.ws.send(hello.model_dump_json())
        msg = await self.recv()
        assert isinstance(msg, pm.Welcome), msg
        self.welcome = msg
        return msg

    async def send(self, msg: pm._Msg) -> None:  # noqa: SLF001
        await self.ws.send(msg.model_dump_json())

    async def recv(self, timeout: float = 10):
        return pm.parse_server_message(await asyncio.wait_for(self.ws.recv(), timeout))

    async def recv_type(self, *types, timeout: float = 10):
        """Receive until a message of one of `types` arrives; others are kept in `inbox`."""
        for i, m in enumerate(self.inbox):
            if isinstance(m, types):
                return self.inbox.pop(i)
        while True:
            m = await self.recv(timeout)
            if isinstance(m, types):
                return m
            self.inbox.append(m)

    async def ready(self, free: dict | None = None, tiers: list[str] | None = None):
        await self.send(pm.Ready(free=free or FREE, tiers=tiers or ["T1", "T2"]))
        return await self.recv_type(pm.Offer, pm.NoWork)

    def open_envelope(self, offer: pm.Offer) -> pm.TaskEnvelope:
        payload = base64.b64decode(offer.envelope.payload_b64)
        self.signing_keys[offer.envelope.key_id].verify(base64.b64decode(offer.envelope.signature_b64), payload)
        return pm.TaskEnvelope.model_validate_json(payload)

    async def heartbeat(self, running: list[tuple[str, int, str]], owner_active: bool = False,
                        shares: list[dict] | None = None) -> pm.HeartbeatAck:
        await self.send(pm.Heartbeat(running=[{"attempt_id": a, "epoch": e, "state": s} for a, e, s in running],
                                     owner_active=owner_active, shares=shares))
        return await self.recv_type(pm.HeartbeatAck)

    async def run_task(self, offer: pm.Offer, exit_code: int = 0, logs: str = "hello\n") -> pm.CommitAck:
        await self.send(pm.Accept(attempt_id=offer.attempt_id, epoch=offer.epoch, tier="T2"))
        await self.send(pm.Started(attempt_id=offer.attempt_id, epoch=offer.epoch))
        await self.send(pm.Progress(attempt_id=offer.attempt_id, epoch=offer.epoch, seq=0, log_chunk=logs))
        return await self.commit(offer.attempt_id, offer.task_id, offer.epoch, exit_code)

    async def commit(self, attempt_id: str, task_id: str, epoch: int, exit_code: int = 0,
                     key: str | None = None) -> pm.CommitAck:
        await self.send(pm.Commit(idempotency_key=key or f"{task_id}:{attempt_id}", task_id=task_id,
                                  attempt_id=attempt_id, epoch=epoch, exit_code=exit_code,
                                  outputs=[{"path": "result.txt", "sha256": "e" * 64, "size": 3}]))
        return await self.recv_type(pm.CommitAck)

    async def close(self) -> None:
        if self.ws is not None:
            await self.ws.close()
            self.ws = None


def dumps(o: Any) -> str:
    return json.dumps(o, default=str)
