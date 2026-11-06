"""Write JSON Schema files and contract fixtures.

uv run python -m pool_protocol.export            # schema + fixtures (keeps existing signature vector)
uv run python -m pool_protocol.export --check    # fail if schema/ is out of date (CI)
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path

from pydantic import TypeAdapter

from . import messages as m
from . import sandbox
from .jobspec import JobSpec

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_DIR = ROOT / "schema"
FIXTURE_DIR = ROOT / "fixtures"

T0 = "2026-11-02T12:00:00Z"
T1 = "2026-11-02T12:01:00Z"
DEV = "2b1d8a63-5c0e-4a51-9f1e-7f8a3a1c2d44"
ATT = "6f9e2c71-0d3b-4c8e-a1f5-3b2e9d7c6a10"
TASK = "c3a7e5d2-8b14-4f69-9e0a-1d2c3b4a5f60"
JOB = "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d"
GRP = "0e1d2c3b-4a59-4687-b5a4-c3d2e1f0a9b8"
SHA = "a" * 64
CERT = "-----BEGIN CERTIFICATE-----\nMIIB...example...\n-----END CERTIFICATE-----\n"
CSR = "-----BEGIN CERTIFICATE REQUEST-----\nMIIB...example...\n-----END CERTIFICATE REQUEST-----\n"

SHARE = {
    "group": "research-team",
    "limits": {"cpu_percent": 75, "memory_mb": 24576, "disk_mb": 102400, "gpu": True},
    "accept_from": "all-members",
    "accept_new_members": False,
    "active_now": True,
}

EXAMPLES: dict[str, dict] = {
    "challenge": {
        "type": "challenge",
        "msg_id": "m-1",
        "nonce_b64": base64.b64encode(b"\x01" * 32).decode(),
        "server_time": T0,
    },
    "hello": {
        "type": "hello",
        "msg_id": "m-2",
        "protocol": "v1alpha1",
        "agent_version": "0.1.0",
        "device_id": DEV,
        "profile": "default",
        "cert_pem": CERT,
        "nonce_signature_b64": base64.b64encode(b"\x02" * 64).decode(),
        "capabilities": {
            "os": "linux",
            "arch": "amd64",
            "cpu_cores": 16,
            "memory_mb": 65536,
            "disk_mb": 512000,
            "gpus": [{"vendor": "nvidia", "model": "example-gpu", "memory_mb": 24576}],
            "tiers": ["T1", "T2"],
            "gpu_tiers": ["T1"],
            "always_on": False,
        },
        "shares": [SHARE, {**SHARE, "group": "uni-ml-lab", "accept_from": ["alice", "bob"]}],
        "policy_digest": "sha256:" + SHA,
        "running": [{"attempt_id": ATT, "epoch": 2, "state": "running"}],
    },
    "welcome": {
        "type": "welcome",
        "msg_id": "m-3",
        "device_id": DEV,
        "heartbeat_interval_s": 15,
        "lease_ttl_s": 60,
        "server_time": T0,
        "stale": [],
    },
    "heartbeat": {
        "type": "heartbeat",
        "msg_id": "m-4",
        "running": [{"attempt_id": ATT, "epoch": 2, "state": "running"}],
        "usage": {"cpu_percent": 37.5, "memory_mb": 2048},
        "owner_active": False,
        "shares": [SHARE],
    },
    "heartbeat_ack": {"type": "heartbeat_ack", "msg_id": "m-5", "renewed": [ATT], "stale": [], "lease_expires_at": T1},
    "ready": {
        "type": "ready",
        "msg_id": "m-6",
        "free": {"cpu": 8, "memory_mb": 16384, "disk_mb": 102400, "gpus": 1},
        "tiers": ["T1", "T2"],
        "images": ["sha256:" + "a" * 64],
    },
    "offer": {
        "type": "offer",
        "msg_id": "m-7",
        "attempt_id": ATT,
        "task_id": TASK,
        "epoch": 2,
        "expires_at": T1,
        "envelope": {
            "key_id": "k1",
            "payload_b64": base64.b64encode(b"{}").decode(),
            "signature_b64": base64.b64encode(b"\x03" * 64).decode(),
        },
    },
    "no_work": {"type": "no_work", "msg_id": "m-8", "retry_after_s": 5},
    "accept": {"type": "accept", "msg_id": "m-9", "attempt_id": ATT, "epoch": 2, "tier": "T2"},
    "decline": {
        "type": "decline",
        "msg_id": "m-10",
        "attempt_id": ATT,
        "epoch": 2,
        "reason": "outside_window",
        "detail": "weekday window 19:00-08:00",
    },
    "started": {"type": "started", "msg_id": "m-11", "attempt_id": ATT, "epoch": 2},
    "progress": {
        "type": "progress",
        "msg_id": "m-12",
        "attempt_id": ATT,
        "epoch": 2,
        "seq": 0,
        "stream": "stdout",
        "log_chunk": "epoch 1/10 loss=0.42\n",
        "checkpoint_ref": None,
    },
    "upload_request": {
        "type": "upload_request",
        "msg_id": "m-13",
        "attempt_id": ATT,
        "epoch": 2,
        "artifacts": [{"path": "result.json", "sha256": SHA, "size": 123}],
    },
    "upload_urls": {
        "type": "upload_urls",
        "msg_id": "m-14",
        "attempt_id": ATT,
        "urls": [
            {"path": "result.json", "sha256": SHA, "url": "https://objects.example.org/presigned", "expires_at": T1}
        ],
        "reason": None,
    },
    "pull_auth_request": {
        "type": "pull_auth_request",
        "msg_id": "m-14a",
        "attempt_id": ATT,
        "epoch": 2,
    },
    "pull_auth": {
        "type": "pull_auth",
        "msg_id": "m-14b",
        "attempt_id": ATT,
        "registry": "ghcr.io",
        "token": "example-short-lived-bearer-token",
        "expires_at": T1,
        "reason": None,
    },
    "commit": {
        "type": "commit",
        "msg_id": "m-15",
        "idempotency_key": f"{TASK}:{ATT}",
        "task_id": TASK,
        "attempt_id": ATT,
        "epoch": 2,
        "exit_code": 0,
        "outputs": [{"path": "result.json", "sha256": SHA, "size": 123}],
        "error": None,
        "usage": {"peak_memory_mb": 1400, "cpu_seconds": 2280.5, "peak_cpu": 3.9, "wall_seconds": 720.0,
                  "output_bytes": 4096, "image_mb": 180, "oom_killed": False, "samples": 712},
    },
    "commit_ack": {
        "type": "commit_ack",
        "msg_id": "m-16",
        "attempt_id": ATT,
        "accepted": False,
        "reason": "stale_epoch",
    },
    "abort": {
        "type": "abort",
        "msg_id": "m-17",
        "attempt_id": ATT,
        "epoch": 2,
        "reason": "owner_returned",
        "detail": "keyboard input",
    },
    "renew_cert": {"type": "renew_cert", "msg_id": "m-18", "csr_pem": CSR},
    "cert_renewed": {"type": "cert_renewed", "msg_id": "m-19", "cert_pem": CERT, "cert_not_after": T1},
    "cancel": {"type": "cancel", "msg_id": "m-20", "attempt_id": ATT, "reason": "user_cancelled"},
    "preempt": {"type": "preempt", "msg_id": "m-21", "attempt_id": ATT, "reason": "backup_copy_won"},
    "revoke": {"type": "revoke", "msg_id": "m-22", "group": "research-team", "reason": "device_unshared"},
    "error": {"type": "error", "msg_id": "m-23", "code": "bad_message", "message": "unknown type", "ref_msg_id": "m-9"},
}

ENVELOPE_EXAMPLE = {
    "v": "v1alpha1",
    "attempt_id": ATT,
    "task_id": TASK,
    "job_id": JOB,
    "epoch": 2,
    "group": "research-team",
    "group_id": GRP,
    "submitter": "alice",
    "submitter_probation": False,
    "device_id": DEV,
    "issued_at": T0,
    "expires_at": T1,
    "spec": {
        "image": "registry.example.org/research/probe@sha256:" + "b" * 64,
        "command": ["python", "train_probe.py", "--seed", "3", "--dataset", "imdb"],
        "args": ["--epochs", "20"],
        "workdir": "/app",
        "env": {"PYTHONUNBUFFERED": "1"},
        "task_index": 3,
        "task_count": 30,
        "resources": {
            "cpu": 4,
            "memory_mb": 16384,
            "disk_mb": 10240,
            "gpus": 1,
            "gpu_vendor": "nvidia",
            "gpu_memory_mb": 8192,
        },
        "min_tier": "T1",
        "network": {"egress": "none", "allow": []},
        "data_class": "internal",
        "max_runtime_s": 7200,
        "outputs_path": "/out",
        "checkpoint": {"path": "/ckpt", "every_s": 600},
        "inputs": [{"ref": "sha256:" + "c" * 64, "mount": "/data", "url": None}],
    },
}


def _schemas() -> dict[str, dict]:
    out = {
        "agent-message": TypeAdapter(m.AgentMessage).json_schema(),
        "server-message": TypeAdapter(m.ServerMessage).json_schema(),
        "task-envelope": m.TaskEnvelope.model_json_schema(),
        "enroll-request": m.EnrollRequest.model_json_schema(),
        "enroll-response": m.EnrollResponse.model_json_schema(),
        "job-spec": JobSpec.model_json_schema(),
    }
    return out


def _dump(obj: dict) -> str:
    return json.dumps(obj, indent=2, sort_keys=True) + "\n"


def write_schema(check: bool) -> bool:
    SCHEMA_DIR.mkdir(exist_ok=True)
    ok = True
    for name, schema in _schemas().items():
        path = SCHEMA_DIR / f"{name}.schema.json"
        text = _dump(schema)
        if check:
            if not path.exists() or path.read_text() != text:
                print(f"out of date: {path.relative_to(ROOT)}", file=sys.stderr)
                ok = False
        else:
            path.write_text(text)
    return ok


def write_fixtures(force_signature: bool) -> None:
    msg_dir = FIXTURE_DIR / "messages"
    msg_dir.mkdir(parents=True, exist_ok=True)
    for name, example in EXAMPLES.items():
        model = m.ALL_MESSAGE_TYPES[name]
        model.model_validate(example)  # every fixture must be valid
        (msg_dir / f"{name}.json").write_text(_dump(example))
    m.TaskEnvelope.model_validate(ENVELOPE_EXAMPLE)
    (FIXTURE_DIR / "task_envelope.json").write_text(_dump(ENVELOPE_EXAMPLE))
    (FIXTURE_DIR / "sandbox_hardening.json").write_text(_dump(sandbox.fixture()))

    sig_path = FIXTURE_DIR / "signed_envelope.json"
    if sig_path.exists() and not force_signature:
        return
    # Ephemeral key: generated, used once, discarded. Only the public key is stored.
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    key = Ed25519PrivateKey.generate()
    payload = json.dumps(ENVELOPE_EXAMPLE, separators=(",", ":")).encode()
    vector = {
        "comment": "Contract vector: Ed25519 signature over payload bytes. Ephemeral key; private half discarded.",
        "public_key_b64": base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode(),
        "envelope": {
            "key_id": "test-1",
            "payload_b64": base64.b64encode(payload).decode(),
            "signature_b64": base64.b64encode(key.sign(payload)).decode(),
        },
        "tampered_payload_b64": base64.b64encode(payload.replace(b'"epoch":2', b'"epoch":3')).decode(),
    }
    sig_path.write_text(_dump(vector))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--force-signature", action="store_true")
    args = ap.parse_args()
    if args.check:
        hardening = FIXTURE_DIR / "sandbox_hardening.json"
        hardening_ok = hardening.exists() and hardening.read_text() == _dump(sandbox.fixture())
        if not hardening_ok:
            print(f"out of date: {hardening.relative_to(ROOT)}")
        return 0 if (write_schema(check=True) and hardening_ok) else 1
    write_schema(check=False)
    write_fixtures(args.force_signature)
    print(f"wrote {SCHEMA_DIR.relative_to(ROOT)}/ and {FIXTURE_DIR.relative_to(ROOT)}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
