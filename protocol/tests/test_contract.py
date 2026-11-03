"""Contract tests (plan §10): every fixture parses, round-trips and the signature vector verifies.

The Go agent runs the same fixtures in agent/protocol/contract_test.go.
"""

import base64
import json
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from pool_protocol import messages as m

FIX = Path(__file__).resolve().parent.parent / "fixtures"
AGENT_TYPES = {"hello", "heartbeat", "ready", "accept", "decline", "started", "progress", "upload_request",
               "pull_auth_request", "commit", "abort", "renew_cert"}


@pytest.mark.parametrize("path", sorted((FIX / "messages").glob("*.json")), ids=lambda p: p.stem)
def test_fixture_parses_with_direction_adapter(path: Path):
    raw = path.read_text()
    data = json.loads(raw)
    if data["type"] in AGENT_TYPES:
        msg = m.parse_agent_message(raw)
        with pytest.raises(ValueError):
            m.parse_server_message(raw)
    else:
        msg = m.parse_server_message(raw)
        with pytest.raises(ValueError):
            m.parse_agent_message(raw)
    assert msg.type == data["type"] == path.stem
    # Round trip keeps every field the fixture sets.
    again = json.loads(msg.model_dump_json())
    for key in data:
        assert key in again, key


def test_every_message_type_has_a_fixture():
    names = {p.stem for p in (FIX / "messages").glob("*.json")}
    assert names == set(m.ALL_MESSAGE_TYPES)


def test_unknown_fields_are_ignored_but_required_fields_enforced():
    base = json.loads((FIX / "messages" / "ready.json").read_text())
    msg = m.parse_agent_message(json.dumps({**base, "future_field": 1}))
    assert msg.type == "ready"
    del base["free"]
    with pytest.raises(ValueError):
        m.parse_agent_message(json.dumps(base))


def test_signature_vector_verifies_and_detects_tampering():
    vec = json.loads((FIX / "signed_envelope.json").read_text())
    pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(vec["public_key_b64"]))
    payload = base64.b64decode(vec["envelope"]["payload_b64"])
    sig = base64.b64decode(vec["envelope"]["signature_b64"])
    pub.verify(sig, payload)
    env = m.TaskEnvelope.model_validate_json(payload)
    assert env.epoch == 2
    with pytest.raises(InvalidSignature):
        pub.verify(sig, base64.b64decode(vec["tampered_payload_b64"]))


def test_task_envelope_fixture():
    env = m.TaskEnvelope.model_validate_json((FIX / "task_envelope.json").read_text())
    assert env.spec.resources.gpus == 1 and env.spec.min_tier == "T1"
