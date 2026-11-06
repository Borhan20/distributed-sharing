"""Database schema (plan §6.1). PostgreSQL is both the source of truth and the job queue (D7)."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
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
    Numeric,
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
    registry_retention_days: Mapped[int | None] = mapped_column(Integer)  # None: the server's default
    # The time limit of a task nobody set one for, on a first run (docs/design/pool-decides.md §10).
    default_time_limit_s: Mapped[int] = mapped_column(Integer, server_default="14400", default=14400)
    # Credits (docs/design/credits.md): earn by sharing, spend by using.
    credits_enabled: Mapped[bool] = mapped_column(Boolean, server_default="false", default=False)
    credit_start: Mapped[Decimal] = mapped_column(Numeric(18, 4), server_default="20", default=Decimal(20))
    credit_rates: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default='{"cpu_hour": 1, "gib_hours_per_credit": 4, "gpu_hour": 10}',
        default=lambda: {"cpu_hour": 1, "gib_hours_per_credit": 4, "gpu_hour": 10})
    out_of_credits: Mapped[str] = mapped_column(String(16), server_default="lowest", default="lowest")
    standby_percent: Mapped[int] = mapped_column(Integer, server_default="0", default=0)
    # Groups whose earned credits members may move in (credits.md §3.6): none unless the coordinator names them.
    credits_from: Mapped[list[str]] = mapped_column(ARRAY(String(63)), server_default="{}", default=list)
    credits_out: Mapped[bool] = mapped_column(Boolean, server_default="true", default=True)
    credits_since: Mapped[datetime | None] = mapped_column(TS)  # when credits were last turned on
    state: Mapped[str] = mapped_column(String(16), default="active")  # active | expired | closed | deleted
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    closed_at: Mapped[datetime | None] = mapped_column(TS)
    # Deleting a group hides it at once; its data is purged after purge_after unless it is restored.
    deleted_at: Mapped[datetime | None] = mapped_column(TS)
    purge_after: Mapped[datetime | None] = mapped_column(TS)
    state_before_delete: Mapped[str | None] = mapped_column(String(16))
    __table_args__ = (
        CheckConstraint(_in("approval_rule", APPROVAL_RULES), name="ck_groups_approval_rule"),
        CheckConstraint("state IN ('active','expired','closed','deleted')", name="ck_groups_state"),
        CheckConstraint("approval_k >= 1", name="ck_groups_k"),
        CheckConstraint("out_of_credits IN ('lowest', 'wait')", name="ck_groups_out_of_credits"),
        CheckConstraint("standby_percent BETWEEN 0 AND 100", name="ck_groups_standby_percent"),
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
    last_ready_at: Mapped[datetime | None] = mapped_column(TS)  # it last asked for work (credits: "earn while waiting")
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
    # A coordinator or admin removed this computer from the group: it stays out (the owner cannot
    # turn it back on) until one of them lets it back in.
    removed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    removed_at: Mapped[datetime | None] = mapped_column(TS)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    # "Earn while waiting" (credits): paid up to this moment.
    standby_paid_until: Mapped[datetime | None] = mapped_column(TS)


class GroupRegistryCredential(Base):
    """A group's read-only credential for one container registry (private images).

    The secret is encrypted with the server's secrets key (secretbox.py) and never leaves the server:
    agents only get short-lived pull tokens for one image (registry.py).
    """

    __tablename__ = "group_registry_credentials"
    group_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True)
    registry_host: Mapped[str] = mapped_column(String(255), primary_key=True)
    username: Mapped[str] = mapped_column(String(255))
    secret: Mapped[bytes] = mapped_column(LargeBinary)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    updated_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())


class UserRegistryCredential(Base):
    """A member's own read-only credential for one registry, used only for jobs that member submits
    (for images in a personal account). Encrypted like the group's credentials; never returned."""

    __tablename__ = "user_registry_credentials"
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    registry_host: Mapped[str] = mapped_column(String(255), primary_key=True)
    username: Mapped[str] = mapped_column(String(255))
    secret: Mapped[bytes] = mapped_column(LargeBinary)
    updated_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())


class RegistryImage(Base):
    """An image in the built-in registry, for clean-up: deleted after the group's retention without use."""

    __tablename__ = "registry_images"
    repository: Mapped[str] = mapped_column(String(255), primary_key=True)  # <group>/<handle>/<name>
    digest: Mapped[str] = mapped_column(String(80), primary_key=True)
    tag: Mapped[str | None] = mapped_column(String(128))  # the tag it was pushed with, so scans skip a lookup
    group_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("groups.id", ondelete="SET NULL"))
    first_seen_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    last_used_at: Mapped[datetime | None] = mapped_column(TS)


class RunHistory(Base):
    """What ran on a device, kept for its owner after the job or group is deleted (plan §8.1 principle 5).

    One row per attempt, copied from attempts/tasks/jobs just before they are deleted. Logs, results and
    files are not kept. The live view reads attempts; this table only holds runs whose job is gone.
    """

    __tablename__ = "run_history"
    attempt_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"))
    group_name: Mapped[str] = mapped_column(String(63))
    submitter: Mapped[str] = mapped_column(String(64))
    job_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    job_name: Mapped[str] = mapped_column(String(63))
    task_index: Mapped[int] = mapped_column(Integer)
    image: Mapped[str] = mapped_column(Text)
    command: Mapped[list[str]] = mapped_column(JSONB)
    state: Mapped[str] = mapped_column(String(16))
    end_reason: Mapped[str | None] = mapped_column(String(64))
    tier: Mapped[str | None] = mapped_column(String(4))
    exit_code: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(TS)
    started_at: Mapped[datetime | None] = mapped_column(TS)
    ended_at: Mapped[datetime | None] = mapped_column(TS)
    deleted_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    __table_args__ = (Index("ix_run_history_device", "device_id", "created_at"),)


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
    # Each task's time limit now; None on jobs from before sizes were chosen by pool (the task spec has it).
    max_runtime_s: Mapped[int | None] = mapped_column(Integer)
    # How the sizes were chosen and why (scheduler.sizing): auto fields, first run, growth steps, reasons.
    sizing: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
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
    # What the run used, as the device measured it, capped at what the task was given (scheduler.usage).
    usage: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # What this attempt was given: cpu, memory_mb, disk_mb, max_runtime_s, first_run (scheduler.sizing).
    resources: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # Credits charged so far for this attempt, and for how many seconds of it (credits.charge_attempt).
    charged: Mapped[Decimal] = mapped_column(Numeric(18, 4), server_default="0", default=Decimal(0))
    charged_s: Mapped[float] = mapped_column(Float, server_default="0", default=0.0)
    # Of `charged`: what the submitter paid with free credits, and what the owner got as credits that stay in the
    # group. A refund gives each back in the same kind (credits.charge_attempt).
    charged_free: Mapped[Decimal] = mapped_column(Numeric(18, 4), server_default="0", default=Decimal(0))
    earned_free: Mapped[Decimal] = mapped_column(Numeric(18, 4), server_default="0", default=Decimal(0))
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
    # A retry because pool chose too small a size: shown in the group's usage, left out of fair share.
    pool_paid: Mapped[bool] = mapped_column(Boolean, server_default="false", default=False)
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


class CreditBalance(Base):
    """A member's credits in one group: the sum of their credit_entries, kept here so it can be locked."""

    __tablename__ = "credit_balances"
    group_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    balance: Mapped[Decimal] = mapped_column(Numeric(18, 4), server_default="0", default=Decimal(0))
    free: Mapped[Decimal] = mapped_column(Numeric(18, 4), server_default="0", default=Decimal(0))  # of balance
    updated_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), onupdate=func.now())
    __table_args__ = (CheckConstraint("free >= 0", name="ck_credit_balances_free"),)


class CreditEntry(Base):
    """Append-only: one change of a member's credits. Rows are never updated or deleted."""

    __tablename__ = "credit_entries"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    group_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    free: Mapped[Decimal] = mapped_column(Numeric(18, 4), server_default="0", default=Decimal(0))  # of amount
    # start, grant, request, give, receive, spend, earn, pool_retry, move_out, move_in, standby
    kind: Mapped[str] = mapped_column(String(16))
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    attempt_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    device_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    counterparty_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    request_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    __table_args__ = (Index("ix_credit_entries_group_user", "group_id", "user_id", "id"),
                      Index("ix_credit_entries_job", "job_id"))


class CreditRequest(Base):
    __tablename__ = "credit_requests"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    group_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    reason: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(16), server_default="open", default="open")
    granted: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    decided_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    decided_at: Mapped[datetime | None] = mapped_column(TS)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    __table_args__ = (
        CheckConstraint("state IN ('open', 'approved', 'declined')", name="ck_credit_requests_state"),
        CheckConstraint("amount > 0", name="ck_credit_requests_amount"),
        Index("uq_credit_requests_open", "group_id", "user_id", unique=True, postgresql_where="state = 'open'"),
    )

