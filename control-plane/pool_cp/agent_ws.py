"""Agent WebSocket endpoint (plan §7.12, docs/design/phase1-implementation.md §3).

SECURITY-SENSITIVE: device authentication and fencing. Needs security review (plan §8.10).
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import time
import uuid

from cryptography.exceptions import InvalidSignature
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import ValidationError
from sqlalchemy import select

from pool_protocol import messages as pm

from . import audit, registry, storage
from .ca import CAError, LocalCA
from .image_access import credential_for
from .models import Device, Job, RevokedCert
from .scheduler import core
from .secretbox import SecretBoxError
from .storage import presign_put
from .timeutil import utcnow

log = logging.getLogger(__name__)
router = APIRouter()

CLOSE_UNAUTHENTICATED = 4401
CLOSE_BAD_MESSAGE = 4400
CLOSE_REPLACED = 4409
CLOSE_REVOKED = 4403
CLOSE_RATE = 4429


class AgentConn:
    def __init__(self, ws: WebSocket, device_id: uuid.UUID) -> None:
        self.ws = ws
        self.device_id = device_id
        self._lock = asyncio.Lock()
        self.closed = False

    async def send(self, msg: pm._Msg) -> None:  # noqa: SLF001
        if self.closed:
            return
        async with self._lock:
            try:
                await self.ws.send_text(msg.model_dump_json())
            except (RuntimeError, WebSocketDisconnect):
                self.closed = True

    async def close(self, reason: str, code: int = CLOSE_REVOKED) -> None:
        if not self.closed:
            self.closed = True
            try:
                await self.ws.close(code=code, reason=reason[:100])
            except RuntimeError:
                pass


async def _authenticate(ws: WebSocket, nonce: bytes) -> tuple[pm.Hello, Device, list[str]] | None:
    app = ws.app
    settings = app.state.settings
    ca: LocalCA = app.state.ca
    try:
        raw = await asyncio.wait_for(ws.receive_text(), timeout=15)
        hello = pm.parse_agent_message(raw)
    except (TimeoutError, ValidationError, WebSocketDisconnect, KeyError):
        await ws.close(code=CLOSE_BAD_MESSAGE, reason="expected hello")
        return None
    if not isinstance(hello, pm.Hello):
        await ws.close(code=CLOSE_BAD_MESSAGE, reason="expected hello")
        return None
    try:
        vc = ca.verify(hello.cert_pem)
        if vc.device_id != hello.device_id:
            raise CAError("certificate does not match device_id")
        vc.public_key.verify(
            base64.b64decode(hello.nonce_signature_b64, validate=True), pm.HELLO_SIGNATURE_CONTEXT + nonce
        )
        device_uuid = uuid.UUID(hello.device_id)
    except (CAError, InvalidSignature, ValueError) as exc:
        log.warning("agent authentication failed: %s", exc)
        await ws.close(code=CLOSE_UNAUTHENTICATED, reason="authentication failed")
        return None
    async with app.state.sessionmaker() as s:
        device = (
            await s.execute(select(Device).where(Device.id == device_uuid).with_for_update())
        ).scalar_one_or_none()
        revoked = await s.get(RevokedCert, vc.serial)
        if device is None or device.state != "active" or revoked is not None or device.public_key != vc.public_key_raw:
            await s.rollback()
            await ws.close(code=CLOSE_UNAUTHENTICATED, reason="device unknown or revoked")
            return None
        device.capabilities = hello.capabilities.model_dump(mode="json")
        device.reported_shares = [x.model_dump(mode="json") for x in hello.shares]
        device.policy_digest = hello.policy_digest
        device.agent_version = hello.agent_version
        device.last_seen_at = utcnow()
        await core.sync_shares_from_policy(s, device)
        stale = await core.reconcile_on_hello(s, settings, device.id, hello.running)
        await s.commit()
    return hello, device, stale


@router.websocket("/api/v1/agent/ws")
async def agent_ws(ws: WebSocket) -> None:
    app = ws.app
    settings = app.state.settings
    await ws.accept()
    nonce = os.urandom(32)
    await ws.send_text(pm.Challenge(nonce_b64=base64.b64encode(nonce).decode(), server_time=utcnow()).model_dump_json())
    auth = await _authenticate(ws, nonce)
    if auth is None:
        return
    hello, device, stale = auth
    conn = AgentConn(ws, device.id)
    previous = app.state.agent_conns.get(device.id)
    app.state.agent_conns[device.id] = conn
    if previous is not None:
        await previous.close("replaced by a newer session", CLOSE_REPLACED)
    await conn.send(
        pm.Welcome(
            device_id=str(device.id),
            heartbeat_interval_s=settings.heartbeat_interval_s,
            lease_ttl_s=settings.lease_ttl_s,
            server_time=utcnow(),
            stale=stale,
        )
    )
    log.info("agent connected device=%s profile=%s version=%s", device.id, hello.profile, hello.agent_version)
    window_start, count = time.monotonic(), 0
    try:
        while True:
            raw = await ws.receive_text()
            now = time.monotonic()
            if now - window_start > 1:
                window_start, count = now, 0
            count += 1
            if count > settings.max_ws_messages_per_s:
                await conn.close("rate limit", CLOSE_RATE)
                return
            try:
                msg = pm.parse_agent_message(raw)
            except ValidationError as exc:
                await conn.send(pm.Error(code="bad_message", message=str(exc.errors()[:1])[:300]))
                continue
            try:
                reply = await _dispatch(app, device.id, msg)
            except core.FenceError as exc:
                attempt_id = getattr(msg, "attempt_id", None)
                reply = (
                    pm.Cancel(attempt_id=attempt_id, reason=exc.reason)
                    if attempt_id
                    else pm.Error(code=exc.reason, message="rejected", ref_msg_id=msg.msg_id)
                )
            except _Revoked:
                await conn.close("device revoked", CLOSE_REVOKED)
                return
            for r in reply if isinstance(reply, list) else [reply]:
                if r is not None:
                    await conn.send(r)
    except WebSocketDisconnect:
        pass
    finally:
        if app.state.agent_conns.get(device.id) is conn:
            del app.state.agent_conns[device.id]
        log.info("agent disconnected device=%s", device.id)


class _Revoked(Exception):
    pass


async def _pull_auth(app, s, device_id: uuid.UUID, task, attempt_id: str) -> pm.PullAuth:
    """A token that can pull this task's image only: from the submitter's own stored credential, else the
    group's. The server checks that the token really reads the image before handing it out."""
    settings = app.state.settings
    image = task.spec["image"]
    try:
        ref = registry.parse_image(image)
    except registry.RegistryError as exc:
        return pm.PullAuth(attempt_id=attempt_id, reason=str(exc))
    submitter_id = (await s.execute(select(Job.submitter_id).where(Job.id == task.job_id))).scalar_one()
    try:
        cred = await credential_for(s, app.state.secretbox, user_id=submitter_id, group_id=task.group_id,
                                    registry=ref.registry)
    except SecretBoxError as exc:
        return pm.PullAuth(attempt_id=attempt_id, reason=str(exc))
    if cred is None:
        return pm.PullAuth(attempt_id=attempt_id,
                           reason=f"neither the submitter nor this group has a token for {ref.registry}")
    username, secret, whose = cred
    digest = image.rsplit("@", 1)[1] if "@" in image else ""
    try:
        token, expires = await asyncio.to_thread(registry.pull_token, ref, username, secret,
                                                 allow_private=settings.registry_private_networks)
        readable = await asyncio.to_thread(registry.can_read, ref, digest, token,
                                           allow_private=settings.registry_private_networks)
    except registry.RegistryError as exc:
        return pm.PullAuth(attempt_id=attempt_id, reason=str(exc))
    if not readable:
        owner = "the submitter's" if whose == "submitter" else "the group's"
        return pm.PullAuth(attempt_id=attempt_id, reason=f"{owner} token for {ref.registry} cannot read this image")
    await audit.record(s, actor_type="device", actor_id=device_id, action="image.pull_token", group_id=task.group_id,
                       device_id=device_id, target_type="attempt", target_id=attempt_id,
                       details={"registry": ref.registry, "repository": ref.repository, "credential": whose})
    await s.commit()
    return pm.PullAuth(attempt_id=attempt_id, registry=ref.auth_key, token=token, expires_at=expires)


async def _dispatch(app, device_id: uuid.UUID, msg) -> pm._Msg | list | None:  # noqa: C901
    settings = app.state.settings
    async with app.state.sessionmaker() as s:
        device = await s.get(Device, device_id)
        if device is None or device.state != "active":
            raise _Revoked()
        device.last_seen_at = utcnow()
        match msg:
            case pm.Heartbeat():
                device.owner_active = msg.owner_active
                if msg.shares is not None:
                    device.reported_shares = [x.model_dump(mode="json") for x in msg.shares]
                # Every heartbeat, so a group the owner joins later is picked up without a reconnect.
                await core.sync_shares_from_policy(s, device)
                renewed, stale = await core.renew_leases(s, settings, device_id, msg.running)
                await s.commit()
                return pm.HeartbeatAck(renewed=renewed, stale=stale, lease_expires_at=utcnow() + _ttl(settings))
            case pm.Ready():
                offer = await core.claim_task(s, settings, app.state.signer, device, msg)
                await s.commit()
                return offer or pm.NoWork(retry_after_s=5)
            case pm.Accept():
                await core.handle_accept(s, settings, device_id, msg)
            case pm.Decline():
                await core.handle_decline(s, device_id, msg)
            case pm.Started():
                await core.handle_started(s, settings, device_id, msg)
            case pm.Progress():
                await core.handle_progress(s, device_id, msg)
            case pm.UploadRequest():
                f = await core.fence(s, device_id, msg.attempt_id, msg.epoch)
                if storage.enabled(settings) and app.state.status.get("object_store") != "ready":
                    # The bucket is not there yet; URLs would only fail. Outputs stay on the device.
                    await s.commit()
                    return pm.UploadURLs(attempt_id=msg.attempt_id, urls=[], reason="object_store_not_ready")
                urls = []
                for art in msg.artifacts:
                    url, expires = presign_put(settings, f.task.group_id, art.sha256, art.size)
                    if url is None:
                        await s.commit()
                        return pm.UploadURLs(attempt_id=msg.attempt_id, urls=[], reason="no_object_store")
                    urls.append(pm.UploadURL(path=art.path, sha256=art.sha256, url=url, expires_at=expires))
                await s.commit()
                return pm.UploadURLs(attempt_id=msg.attempt_id, urls=urls)
            case pm.PullAuthRequest():
                # SECURITY-SENSITIVE: hands out registry access. Only for the device's own, current attempt.
                f = await core.fence(s, device_id, msg.attempt_id, msg.epoch)
                return await _pull_auth(app, s, device_id, f.task, msg.attempt_id)
            case pm.Commit():
                ack = await core.handle_commit(s, settings, device_id, msg)
                await s.commit()
                return ack
            case pm.Abort():
                await core.handle_abort(s, settings, device_id, msg)
            case pm.RenewCert():
                ca: LocalCA = app.state.ca
                try:
                    pub = ca.csr_public_key(msg.csr_pem)
                except CAError as exc:
                    return pm.Error(code="bad_csr", message=str(exc), ref_msg_id=msg.msg_id)
                if pub != device.public_key:
                    return pm.Error(
                        code="key_mismatch", message="renewal must use the enrolled key", ref_msg_id=msg.msg_id
                    )
                cert_pem, serial, not_after = ca.issue(pub, str(device_id))
                device.cert_serial, device.cert_not_after = serial, not_after
                await audit.record(
                    s,
                    actor_type="device",
                    actor_id=device_id,
                    action="device.cert_renew",
                    device_id=device_id,
                    target_type="device",
                    target_id=device_id,
                    details={"cert_serial": serial},
                )
                await s.commit()
                return pm.CertRenewed(cert_pem=cert_pem, cert_not_after=not_after)
            case pm.Hello():
                return pm.Error(code="bad_message", message="hello already received", ref_msg_id=msg.msg_id)
        await s.commit()
    return None


def _ttl(settings):
    from datetime import timedelta

    return timedelta(seconds=settings.lease_ttl_s)
