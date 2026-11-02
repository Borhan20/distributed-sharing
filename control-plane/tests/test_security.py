"""Enrollment, device authentication, revocation, audit chain and MFA (plan §8.6, §8.9)."""

import base64
import uuid
from datetime import timedelta

import pytest
import websockets
from conftest import gname, job_spec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fake_agent import FakeAgent, share
from sqlalchemy import select, text, update

from pool_cp import audit
from pool_cp.auth import Principal
from pool_cp.authz import require_role
from pool_cp.ca import CAError, LocalCA
from pool_cp.models import AuditEvent, EnrollmentCode, Group, Membership, User
from pool_cp.timeutil import utcnow
from pool_protocol import messages as pm

pytestmark = pytest.mark.asyncio


async def test_enrollment_code_single_use_and_expiry(user, http, base_url, db):
    owner = await user()
    code = (await owner.post("/api/v1/devices/enrollment-codes", expect=201)).json()["code"]
    a = FakeAgent(base_url)
    assert (await a.enroll(http, code.lower())).status_code == 200  # case/dash tolerant
    b = FakeAgent(base_url)
    assert (await b.enroll(http, code)).status_code == 401  # single use
    code2 = (await owner.post("/api/v1/devices/enrollment-codes", expect=201)).json()["code"]
    await db.execute(
        update(EnrollmentCode)
        .where(
            EnrollmentCode.owner_id == uuid.UUID((await owner.get("/api/v1/me"))["id"]),
            EnrollmentCode.used_at.is_(None),
        )
        .values(expires_at=utcnow() - timedelta(seconds=1))
    )
    await db.commit()
    assert (await b.enroll(http, code2)).status_code == 401  # expired
    assert (await b.enroll(http, "ZZZZZ-ZZZZZ")).status_code == 401
    devices = await owner.get("/api/v1/devices")
    assert [d["id"] for d in devices] == [a.device_id]


async def test_enroll_rejects_non_ed25519_or_bad_csr(user, http, base_url):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    owner = await user()
    code = (await owner.post("/api/v1/devices/enrollment-codes", expect=201)).json()["code"]
    ec_key = ec.generate_private_key(ec.SECP256R1())
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "x")]))
        .sign(ec_key, hashes.SHA256())
        .public_bytes(serialization.Encoding.PEM)
        .decode()
    )
    body = {
        "one_time_code": code,
        "csr_pem": csr,
        "device_facts": {"name": "x", "os": "linux", "arch": "amd64", "agent_version": "t"},
    }
    r = await http.post("/api/v1/agent/enroll", json=body)
    assert r.status_code == 400 and "Ed25519" in r.text
    body["csr_pem"] = "not a csr"
    assert (await http.post("/api/v1/agent/enroll", json=body)).status_code == 400


async def _expect_rejected(agent: FakeAgent, **kw):
    with pytest.raises((websockets.ConnectionClosed, AssertionError)):
        await agent.connect([], **kw)


async def test_ws_rejects_wrong_key_foreign_cert_and_mismatched_device(user, enrolled, base_url, tmp_path):
    owner = await user()
    agent = await enrolled(owner)
    # 1. Signature by a different key than the certificate's.
    await _expect_rejected(agent, sign_with=Ed25519PrivateKey.generate())
    # 2. Certificate from a different CA (attacker's own CA) with the right device id.
    rogue = LocalCA(tmp_path / "k", tmp_path / "c", timedelta(hours=1), create=True)
    from pool_cp.ca import raw_public

    pem, _, _ = rogue.issue(raw_public(agent.key.public_key()), agent.device_id)
    await _expect_rejected(agent, cert_pem=pem)
    # 3. Valid certificate presented for another device id.
    other = await enrolled(owner, "other")
    victim_id, agent.device_id = agent.device_id, other.device_id
    await _expect_rejected(agent)
    agent.device_id = victim_id
    # Sanity: the genuine agent still gets in.
    assert (await agent.connect([])).device_id == victim_id
    await agent.close()


async def test_revocation_denies_and_requeues(group_with_device, db):
    coord, g, agent = await group_with_device()
    await coord.post(f"/api/v1/groups/{g}/jobs", job_spec(), expect=201)
    await agent.connect([share(g)])
    o = await agent.ready()
    await agent.send(pm.Accept(attempt_id=o.attempt_id, epoch=1, tier="T2"))
    await agent.heartbeat([(o.attempt_id, 1, "assigned")])
    d = (await coord.post(f"/api/v1/devices/{agent.device_id}/revoke", expect=200)).json()
    assert d["state"] == "revoked"
    with pytest.raises(websockets.ConnectionClosed):
        for _ in range(5):
            await agent.recv(timeout=5)
    await _expect_rejected(agent)
    tasks = await coord.get(f"/api/v1/jobs/{(await coord.get(f'/api/v1/groups/{g}/jobs'))[0]['id']}/tasks")
    assert tasks[0]["state"] == "pending"


async def test_cert_renewal_keeps_key(user, enrolled):
    owner = await user()
    agent = await enrolled(owner)
    await agent.connect([])
    await agent.send(pm.RenewCert(csr_pem=agent.csr_pem(Ed25519PrivateKey.generate())))
    err = await agent.recv_type(pm.Error)
    assert err.code == "key_mismatch"
    await agent.send(pm.RenewCert(csr_pem=agent.csr_pem()))
    renewed = await agent.recv_type(pm.CertRenewed)
    assert renewed.cert_pem != agent.cert_pem
    await agent.close()
    agent.cert_pem = renewed.cert_pem
    assert (await agent.connect([])).device_id == agent.device_id
    await agent.close()


async def test_audit_chain_verifies_and_is_append_only(user, db):
    root = await user("root-admin")
    c = await user()
    await c.post("/api/v1/groups", {"name": gname(), "display_name": "x"}, expect=201)
    res = await root.get("/api/v1/audit/verify")
    assert res["ok"] and res["rows_checked"] > 0
    await c.req("GET", "/api/v1/audit/verify", expect=403)
    last_id = (await db.execute(select(AuditEvent.id).order_by(AuditEvent.id.desc()).limit(1))).scalar_one()
    with pytest.raises(Exception, match="append-only"):
        await db.execute(text("UPDATE audit_events SET action = 'x' WHERE id = :i"), {"i": last_id})
    await db.rollback()
    with pytest.raises(Exception, match="append-only"):
        await db.execute(text("DELETE FROM audit_events WHERE id = :i"), {"i": last_id})
    await db.rollback()


async def test_audit_tampering_is_detected(db, user):
    """Simulate a DB superuser bypassing the trigger; the hash chain still catches it."""
    root = await user("root-admin")
    ev = (await db.execute(select(AuditEvent).order_by(AuditEvent.id.desc()).limit(1))).scalar_one()
    original, ev_id = ev.action, ev.id
    await db.execute(text("ALTER TABLE audit_events DISABLE TRIGGER audit_events_no_update_delete"))
    await db.execute(text("UPDATE audit_events SET action = 'forged' WHERE id = :i"), {"i": ev_id})
    await db.commit()
    try:
        res = await root.get("/api/v1/audit/verify")
        assert res["ok"] is False and res["first_bad_id"] == ev_id
    finally:
        await db.execute(text("UPDATE audit_events SET action = :a WHERE id = :i"), {"a": original, "i": ev_id})
        await db.execute(text("ALTER TABLE audit_events ENABLE TRIGGER audit_events_no_update_delete"))
        await db.commit()
    assert (await root.get("/api/v1/audit/verify"))["ok"] is True


async def test_mfa_required_for_privileged_roles(db):
    u = User(subject=f"t|{uuid.uuid4()}", handle=f"mfa-{uuid.uuid4().hex[:6]}")
    g = Group(name=gname(), display_name="x")
    db.add_all([u, g])
    await db.flush()
    db.add(Membership(group_id=g.id, user_id=u.id, role="coordinator"))
    await db.flush()
    no_mfa = Principal(user=u, claims={"amr": ["pwd"]})
    with_mfa = Principal(user=u, claims={"amr": ["pwd", "otp"]})
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await require_role(db, g, no_mfa, {"coordinator"}, mfa_required=True)
    assert exc.value.status_code == 403
    assert (await require_role(db, g, with_mfa, {"coordinator"}, mfa_required=True)).role == "coordinator"
    assert (await require_role(db, g, no_mfa, {"coordinator"}, mfa_required=False)).role == "coordinator"
    await db.rollback()


async def test_ca_rejects_expired_cert(tmp_path):
    ca = LocalCA(tmp_path / "k", tmp_path / "c", timedelta(seconds=-1), create=True)
    from pool_cp.ca import raw_public

    pem, _, _ = ca.issue(raw_public(Ed25519PrivateKey.generate().public_key()), str(uuid.uuid4()))
    with pytest.raises(CAError, match="expired"):
        ca.verify(pem)


async def test_envelope_bound_to_device(group_with_device):
    coord, g, agent = await group_with_device()
    await coord.post(f"/api/v1/groups/{g}/jobs", job_spec(), expect=201)
    await agent.connect([share(g)])
    o = await agent.ready()
    env = agent.open_envelope(o)
    assert env.device_id == agent.device_id
    assert env.expires_at > env.issued_at
    tampered = base64.b64encode(base64.b64decode(o.envelope.payload_b64).replace(b'"epoch":1', b'"epoch":9'))
    o.envelope.payload_b64 = tampered.decode()
    with pytest.raises(Exception):
        agent.open_envelope(o)
    await agent.close()


async def test_audit_record_hash_matches_recompute(db):
    ev = await audit.record(db, actor_type="system", actor_id=None, action="test.event", details={"n": 1})
    await db.commit()
    again = audit.compute_hash(
        ev.prev_hash,
        ev.ts,
        ev.actor_type,
        ev.actor_id,
        ev.action,
        ev.group_id,
        ev.device_id,
        ev.target_type,
        ev.target_id,
        ev.details,
    )
    assert again == ev.hash
