"""Database schema (plan §6.1). PostgreSQL is both the source of truth and the job queue (D7)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base

ROLES = ("coordinator", "admin", "member", "contributor", "submitter")
APPROVAL_RULES = ("coordinator-only", "any-k-members", "majority")
TASK_STATES = ("pending", "assigned", "running", "succeeded", "failed", "cancelled")
TERMINAL_TASK_STATES = ("succeeded", "failed", "cancelled")
ATTEMPT_STATES = ("assigned", "running", "succeeded", "failed", "lost", "declined", "cancelled", "preempted")
ACTIVE_ATTEMPT_STATES = ("assigned", "running")


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


def _in(col: str, values: tuple[str, ...]) -> str:
    return f"{col} IN ({', '.join(repr(v) for v in values)})"


TS = DateTime(timezone=True)


class User(Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    subject: Mapped[str] = mapped_column(String(255), unique=True)  # OIDC iss|sub
    handle: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())


class Group(Base):
    __tablename__ = "groups"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(63), unique=True)
    display_name: Mapped[str] = mapped_column(String(128))
    template: Mapped[str | None] = mapped_column(String(32))
    approval_rule: Mapped[str] = mapped_column(String(32), default="coordinator-only")
    approval_k: Mapped[int] = mapped_column(Integer, default=1)
    probation_days: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[datetime | None] = mapped_column(TS)
    retention_days: Mapped[int] = mapped_column(Integer, default=30)
    registry_allowlist: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    state: Mapped[str] = mapped_column(String(16), default="active")  # active | expired | closed
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    closed_at: Mapped[datetime | None] = mapped_column(TS)
    __table_args__ = (
        CheckConstraint(_in("approval_rule", APPROVAL_RULES), name="ck_groups_approval_rule"),
        CheckConstraint("state IN ('active','expired','closed')", name="ck_groups_state"),
        CheckConstraint("approval_k >= 1", name="ck_groups_k"),
    )


class Membership(Base):
    __tablename__ = "memberships"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    group_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(16))
    probation_until: Mapped[datetime | None] = mapped_column(TS)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    __table_args__ = (
        UniqueConstraint("group_id", "user_id", name="uq_membership"),
        CheckConstraint(_in("role", ROLES), name="ck_membership_role"),
    )


class Invite(Base):
    __tablename__ = "invites"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    group_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    role: Mapped[str] = mapped_column(String(16))
    max_uses: Mapped[int] = mapped_column(Integer)
    uses: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[datetime] = mapped_column(TS)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())


class JoinRequest(Base):
    __tablename__ = "join_requests"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    group_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    message: Mapped[str] = mapped_column(String(500), default="")
    state: Mapped[str] = mapped_column(String(16), default="pending")  # pending | approved | rejected
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    decided_at: Mapped[datetime | None] = mapped_column(TS)
    __table_args__ = (
        Index("uq_join_request_pending", "group_id", "user_id", unique=True,
              postgresql_where="state = 'pending'"),
    )


class Approval(Base):
    __tablename__ = "approvals"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    join_request_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("join_requests.id", ondelete="CASCADE"))
    approver_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    decision: Mapped[str] = mapped_column(String(8))  # approve | reject
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    __table_args__ = (UniqueConstraint("join_request_id", "approver_id", name="uq_approval"),)


class EnrollmentCode(Base):
    __tablename__ = "enrollment_codes"
    code_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    expires_at: Mapped[datetime] = mapped_column(TS)
    used_at: Mapped[datetime | None] = mapped_column(TS)
    device_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))


class Device(Base):
    __tablename__ = "devices"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(64))
    os: Mapped[str] = mapped_column(String(16))
    arch: Mapped[str] = mapped_column(String(32))
    agent_version: Mapped[str] = mapped_column(String(32), default="")
    public_key: Mapped[bytes] = mapped_column(LargeBinary)
    cert_serial: Mapped[str] = mapped_column(String(64))
    cert_not_after: Mapped[datetime] = mapped_column(TS)
    state: Mapped[str] = mapped_column(String(16), default="active")  # active | revoked
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    reported_shares: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    policy_digest: Mapped[str] = mapped_column(String(80), default="")
    owner_active: Mapped[bool] = mapped_column(Boolean, default=False)
    always_on: Mapped[bool] = mapped_column(Boolean, default=False)
    org_owned_dedicated: Mapped[bool] = mapped_column(Boolean, default=False)  # server admins only
    reliability: Mapped[float] = mapped_column(Float, default=1.0)
    last_seen_at: Mapped[datetime | None] = mapped_column(TS)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    revoked_at: Mapped[datetime | None] = mapped_column(TS)


class RevokedCert(Base):
    """Immediate deny list (plan §8.6)."""

    __tablename__ = "revoked_certs"
    serial: Mapped[str] = mapped_column(String(64), primary_key=True)
    device_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    revoked_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())


class DeviceShare(Base):
    """Server-side record that a device is shared with a group.

    Created when the owner shares in the web UI, or automatically when the
    agent's local policy offers a group the owner belongs to (one-step sharing,
    docs/design/easy-onboarding.md). enabled = false means the owner switched
    it off on the server. The local policy is still the final say (plan §6.4):
    a task is only offered when this row is enabled and the policy allows it.
    """

    __tablename__ = "device_shares"
    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), primary_key=True)
    group_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    group_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"), index=True)
    submitter_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String(63))
    shape: Mapped[str] = mapped_column(String(16))
    spec: Mapped[dict[str, Any]] = mapped_column(JSONB)
    image: Mapped[str] = mapped_column(Text)
    # Denormalised requirements used by the matching query.
    cpu: Mapped[float] = mapped_column(Float)
    memory_mb: Mapped[int] = mapped_column(Integer)
    disk_mb: Mapped[int] = mapped_column(Integer)
    gpus: Mapped[int] = mapped_column(Integer, default=0)
    gpu_memory_mb: Mapped[int] = mapped_column(Integer, default=0)
    min_tier_rank: Mapped[int] = mapped_column(Integer)
    os: Mapped[str | None] = mapped_column(String(16))
    data_class: Mapped[str] = mapped_column(String(16))
    egress: Mapped[bool] = mapped_column(Boolean, default=False)
    max_retries: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(16), default="pending")
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(TS)
    __table_args__ = (Index("ix_jobs_active", "group_id", "created_at",
                            postgresql_where="state IN ('pending','running')"),)


class Task(Base):
    __tablename__ = "tasks"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"))
    group_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"))
    index: Mapped[int] = mapped_column(Integer)
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    spec: Mapped[dict[str, Any]] = mapped_column(JSONB)  # rendered TaskSpec
    state: Mapped[str] = mapped_column(String(16), default="pending")
    epoch: Mapped[int] = mapped_column(Integer, default=0)
    attempts_count: Mapped[int] = mapped_column(Integer, default=0)
    failures: Mapped[int] = mapped_column(Integer, default=0)
    max_retries: Mapped[int] = mapped_column(Integer)
    current_attempt_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    not_before: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), onupdate=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(TS)
    __table_args__ = (
        UniqueConstraint("job_id", "index", name="uq_task_index"),
        CheckConstraint(_in("state", TASK_STATES), name="ck_task_state"),
        Index("ix_tasks_queue", "group_id", "not_before", postgresql_where="state = 'pending'"),
        Index("ix_tasks_job_pending", "job_id", "index", postgresql_where="state = 'pending'"),
    )


class Attempt(Base):
    """One run of a task on one device. `id` is the lease id; `epoch` fences commits (plan §7.2)."""

    __tablename__ = "attempts"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    epoch: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(16), default="assigned")
    tier: Mapped[str | None] = mapped_column(String(4))
    lease_expires_at: Mapped[datetime] = mapped_column(TS)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(TS)
    ended_at: Mapped[datetime | None] = mapped_column(TS)
    exit_code: Mapped[int | None] = mapped_column(Integer)
    outputs: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    end_reason: Mapped[str | None] = mapped_column(String(64))
    commit_key: Mapped[str | None] = mapped_column(String(128))
    __table_args__ = (
        UniqueConstraint("task_id", "epoch", name="uq_attempt_epoch"),
        CheckConstraint(_in("state", ATTEMPT_STATES), name="ck_attempt_state"),
        Index("ix_attempts_active_lease", "lease_expires_at", postgresql_where="state IN ('assigned','running')"),
    )


class TaskLog(Base):
    __tablename__ = "task_logs"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    attempt_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("attempts.id", ondelete="CASCADE"))
    seq: Mapped[int] = mapped_column(Integer)
    stream: Mapped[str] = mapped_column(String(8))
    chunk: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    __table_args__ = (UniqueConstraint("attempt_id", "stream", "seq", name="uq_task_log_seq"),)


class UsageLedger(Base):
    __tablename__ = "usage_ledger"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    group_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"))
    attempt_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), unique=True)
    cpu_seconds: Mapped[float] = mapped_column(Float)
    gpu_seconds: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    __table_args__ = (Index("ix_usage_group_user_time", "group_id", "user_id", "created_at"),)


class AuditEvent(Base):
    """Append-only, hash-chained (plan §8.9). Rows are never updated or deleted."""

    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(TS)
    actor_type: Mapped[str] = mapped_column(String(16))  # user | device | system
    actor_id: Mapped[str | None] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64))
    group_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    device_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    target_type: Mapped[str | None] = mapped_column(String(32))
    target_id: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64), unique=True)


class ServiceHeartbeat(Base):
    """Liveness of API processes; the scheduler pauses lease expiry while no API is up (plan §7.4)."""

    __tablename__ = "service_heartbeats"
    instance: Mapped[str] = mapped_column(String(64), primary_key=True)
    service: Mapped[str] = mapped_column(String(16))
    at: Mapped[datetime] = mapped_column(TS)
