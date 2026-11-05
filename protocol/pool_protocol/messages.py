"""Agent <-> server protocol, version v1alpha1.

These models are the source of truth for the wire format (plan §7.12).
`python -m pool_protocol.export` writes JSON Schema to protocol/schema/ and the
Go agent's hand-written types are checked against protocol/fixtures/.

SECURITY-SENSITIVE: changes here need a security reviewer (plan §8.10).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator

PROTOCOL_VERSION = "v1alpha1"
HELLO_SIGNATURE_CONTEXT = b"pool-agent-hello-v1:"
MAX_LOG_CHUNK_BYTES = 64 * 1024

Tier = Literal["T0", "T1", "T2", "T3"]
TIER_RANK: dict[str, int] = {"T0": 0, "T1": 1, "T2": 2, "T3": 3}

DeclineReason = Literal[
    "signature_invalid",
    "envelope_expired",
    "wrong_device",
    "group_not_shared",
    "outside_window",
    "owner_active",
    "submitter_not_accepted",
    "probation_not_accepted",
    "tier_not_allowed",
    "resources_exceed_limits",
    "egress_not_allowed",
    "image_not_allowed",
    "kill_switch",
    "busy",
    "other",
]

AbortReason = Literal[
    "owner_returned",
    "kill_switch",
    "policy",
    "agent_shutdown",
    "disk_full",
    "runtime_error",
]


def _new_id() -> str:
    return str(uuid.uuid4())


class _Model(BaseModel):
    # Unknown fields are ignored so that a newer peer can add optional fields
    # without breaking an older one. Required fields are still enforced.
    model_config = ConfigDict(extra="ignore")


# --------------------------------------------------------------------------
# Shared value types
# --------------------------------------------------------------------------


class Resources(_Model):
    cpu: float = Field(ge=0, description="CPU cores")
    memory_mb: int = Field(ge=0)
    disk_mb: int = Field(ge=0, default=0)
    gpus: int = Field(ge=0, default=0)


class GPUInfo(_Model):
    vendor: str
    model: str
    memory_mb: int = Field(ge=0)


class Capabilities(_Model):
    os: Literal["linux", "windows"]
    arch: str = Field(pattern=r"^[a-z0-9_]{1,16}$")  # GOARCH: amd64, arm64, ...
    cpu_cores: int = Field(ge=1)
    memory_mb: int = Field(ge=0)
    disk_mb: int = Field(ge=0)
    gpus: list[GPUInfo] = Field(default_factory=list)
    tiers: list[Tier] = Field(description="Isolation tiers available and allowed by local policy")
    gpu_tiers: list[Tier] = Field(default_factory=list, description="Tiers that can also expose a GPU")
    always_on: bool = False


class ShareLimits(_Model):
    cpu_percent: int = Field(ge=0, le=100)
    memory_mb: int = Field(ge=0)
    disk_mb: int = Field(ge=0)
    gpu: bool = False


class ShareInfo(_Model):
    """What the agent's local policy allows for one group (reported, not trusted)."""

    group: str
    limits: ShareLimits
    accept_from: Literal["all-members"] | list[str] = "all-members"
    accept_new_members: bool = False
    active_now: bool = Field(description="Time window active and idle requirement met right now")


class RunningAttempt(_Model):
    attempt_id: str
    epoch: int = Field(ge=1)
    state: Literal["assigned", "running"]


class Usage(_Model):
    cpu_percent: float = Field(ge=0, default=0)
    memory_mb: int = Field(ge=0, default=0)


class SignedEnvelope(_Model):
    key_id: str
    payload_b64: str
    signature_b64: str


class ArtifactRef(_Model):
    path: str = Field(description="Path relative to the outputs directory")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(ge=0)


class UploadURL(_Model):
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    url: str
    expires_at: datetime


# --------------------------------------------------------------------------
# Task envelope (signed by the server, verified by the agent)
# --------------------------------------------------------------------------


class TaskResources(_Model):
    cpu: float = Field(gt=0)
    memory_mb: int = Field(gt=0)
    disk_mb: int = Field(ge=0, default=0)
    gpus: int = Field(ge=0, default=0)
    gpu_vendor: str | None = None
    gpu_memory_mb: int = Field(ge=0, default=0)


class TaskNetwork(_Model):
    egress: Literal["none", "allowlist"] = "none"
    allow: list[str] = Field(default_factory=list)


class TaskCheckpoint(_Model):
    path: str = "/ckpt"
    every_s: int = Field(gt=0, default=600)


class TaskInput(_Model):
    ref: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    mount: str
    url: str | None = None


class TaskSpec(_Model):
    image: str
    command: list[str] = Field(default_factory=list, description="Replaces the image's ENTRYPOINT; empty: keep it")
    args: list[str] = Field(default_factory=list,
                            description="Arguments after the command, or for the image's own ENTRYPOINT when "
                                        "command is empty (like `docker run IMAGE ARGS`); empty: the image's CMD")
    workdir: str | None = Field(default=None, description="Working directory inside the container")
    env: dict[str, str] = Field(default_factory=dict)
    task_index: int = Field(default=0, ge=0, description="This task's number in its job (POOL_TASK_INDEX)")
    task_count: int = Field(default=1, ge=1, description="Tasks in the job (POOL_TASK_COUNT)")
    resources: TaskResources
    min_tier: Tier = "T1"
    network: TaskNetwork = Field(default_factory=TaskNetwork)
    data_class: Literal["public", "internal", "confidential"] = "internal"
    max_runtime_s: int = Field(gt=0)
    outputs_path: str = "/out"
    checkpoint: TaskCheckpoint | None = None
    inputs: list[TaskInput] = Field(default_factory=list)


class TaskEnvelope(_Model):
    v: Literal["v1alpha1"] = PROTOCOL_VERSION
    attempt_id: str
    task_id: str
    job_id: str
    epoch: int = Field(ge=1)
    group: str
    group_id: str
    submitter: str
    submitter_probation: bool = False
    device_id: str
    issued_at: datetime
    expires_at: datetime
    spec: TaskSpec
    registry_auth: bool = Field(
        default=False,
        description="The image is private: ask for a short-lived pull token with pull_auth_request before pulling",
    )


# --------------------------------------------------------------------------
# Enrollment (HTTPS)
# --------------------------------------------------------------------------


class DeviceFacts(_Model):
    name: str = Field(min_length=1, max_length=64)
    os: Literal["linux", "windows"]
    arch: str = Field(max_length=32)
    agent_version: str = Field(max_length=32)


class EnrollRequest(_Model):
    one_time_code: str = Field(min_length=6, max_length=32)
    csr_pem: str = Field(max_length=8192)
    device_facts: DeviceFacts


class SigningKey(_Model):
    key_id: str
    public_key_b64: str


class EnrollResponse(_Model):
    device_id: str
    cert_pem: str
    ca_cert_pem: str
    cert_not_after: datetime
    signing_keys: list[SigningKey]
    ws_path: str = "/api/v1/agent/ws"


# --------------------------------------------------------------------------
# WebSocket messages
# --------------------------------------------------------------------------


class _Msg(_Model):
    msg_id: str = Field(default_factory=_new_id)


class Challenge(_Msg):
    type: Literal["challenge"] = "challenge"
    nonce_b64: str
    server_time: datetime


class Hello(_Msg):
    type: Literal["hello"] = "hello"
    protocol: Literal["v1alpha1"] = PROTOCOL_VERSION
    agent_version: str
    device_id: str
    profile: str
    cert_pem: str
    nonce_signature_b64: str
    capabilities: Capabilities
    shares: list[ShareInfo] = Field(default_factory=list)
    policy_digest: str
    running: list[RunningAttempt] = Field(default_factory=list)


class Welcome(_Msg):
    type: Literal["welcome"] = "welcome"
    device_id: str
    heartbeat_interval_s: int
    lease_ttl_s: int
    server_time: datetime
    stale: list[str] = Field(default_factory=list)


class Heartbeat(_Msg):
    type: Literal["heartbeat"] = "heartbeat"
    running: list[RunningAttempt] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    owner_active: bool = False
    shares: list[ShareInfo] | None = None


class HeartbeatAck(_Msg):
    type: Literal["heartbeat_ack"] = "heartbeat_ack"
    renewed: list[str] = Field(default_factory=list)
    stale: list[str] = Field(default_factory=list)
    lease_expires_at: datetime


class Ready(_Msg):
    type: Literal["ready"] = "ready"
    free: Resources
    tiers: list[Tier]
    # Digests of images the computer ran recently and still has (pool-decides §4.4): a preference only.
    images: list[Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]] = Field(default_factory=list,
                                                                                 max_length=200)


class Offer(_Msg):
    type: Literal["offer"] = "offer"
    attempt_id: str
    task_id: str
    epoch: int
    expires_at: datetime
    envelope: SignedEnvelope


class NoWork(_Msg):
    type: Literal["no_work"] = "no_work"
    retry_after_s: int = 5


class Accept(_Msg):
    type: Literal["accept"] = "accept"
    attempt_id: str
    epoch: int
    tier: Tier


class Decline(_Msg):
    type: Literal["decline"] = "decline"
    attempt_id: str
    epoch: int
    reason: DeclineReason
    detail: str = ""


class Started(_Msg):
    type: Literal["started"] = "started"
    attempt_id: str
    epoch: int


class Progress(_Msg):
    type: Literal["progress"] = "progress"
    attempt_id: str
    epoch: int
    seq: int = Field(ge=0)
    stream: Literal["stdout", "stderr", "agent"] = "stdout"
    log_chunk: str = Field(max_length=MAX_LOG_CHUNK_BYTES)
    checkpoint_ref: str | None = None


class UploadRequest(_Msg):
    type: Literal["upload_request"] = "upload_request"
    attempt_id: str
    epoch: int
    artifacts: list[ArtifactRef] = Field(max_length=1000)


class UploadURLs(_Msg):
    type: Literal["upload_urls"] = "upload_urls"
    attempt_id: str
    urls: list[UploadURL] = Field(default_factory=list)
    reason: str | None = None


class RunUsage(_Model):
    """What one attempt used, as `docker stats` measured it on the device (docs/design/pool-decides.md §4.2).

    It is the device's claim: the server caps each number at what the task was given before it uses it."""

    peak_memory_mb: int = Field(ge=0, le=2**31)
    cpu_seconds: float = Field(ge=0, le=1e9)
    peak_cpu: float = Field(ge=0, le=4096)
    wall_seconds: float = Field(ge=0, le=1e8)
    output_bytes: int = Field(ge=0, le=2**50, default=0)
    image_mb: int = Field(ge=0, le=2**31, default=0)
    oom_killed: bool = False
    samples: int = Field(ge=0, le=2**31, default=0)


class Commit(_Msg):
    type: Literal["commit"] = "commit"
    idempotency_key: str
    task_id: str
    attempt_id: str
    epoch: int
    exit_code: int
    outputs: list[ArtifactRef] = Field(default_factory=list, max_length=1000)
    error: str | None = Field(default=None, max_length=4096)
    usage: RunUsage | None = None  # absent from agents that cannot measure (or are older)

    @field_validator("usage", mode="wrap")
    @classmethod
    def _usage_is_optional(cls, v: Any, handler: Any) -> RunUsage | None:
        """A measurement out of range is dropped, not the result: the exit code and outputs still count."""
        try:
            return handler(v)
        except ValidationError:
            return None


class CommitAck(_Msg):
    type: Literal["commit_ack"] = "commit_ack"
    attempt_id: str
    accepted: bool
    reason: str | None = None


class Abort(_Msg):
    type: Literal["abort"] = "abort"
    attempt_id: str
    epoch: int
    reason: AbortReason
    detail: str = ""


class RenewCert(_Msg):
    type: Literal["renew_cert"] = "renew_cert"
    csr_pem: str = Field(max_length=8192)


class CertRenewed(_Msg):
    type: Literal["cert_renewed"] = "cert_renewed"
    cert_pem: str
    cert_not_after: datetime


class Cancel(_Msg):
    type: Literal["cancel"] = "cancel"
    attempt_id: str
    reason: str = ""


class Preempt(_Msg):
    type: Literal["preempt"] = "preempt"
    attempt_id: str
    reason: str = ""


class Revoke(_Msg):
    type: Literal["revoke"] = "revoke"
    group: str
    reason: str = ""


class PullAuthRequest(_Msg):
    """A -> S: a short-lived token to pull this attempt's private image (sent only if registry_auth)."""

    type: Literal["pull_auth_request"] = "pull_auth_request"
    attempt_id: str
    epoch: int = Field(ge=1)


class PullAuth(_Msg):
    """S -> A: a bearer token for this one image (pull only), never the group's stored credentials.

    `registry` is the key for Docker's config `auths` (e.g. "ghcr.io", "https://index.docker.io/v1/").
    Without a token, `reason` says why; the agent then fails the attempt instead of pulling anonymously.
    """

    type: Literal["pull_auth"] = "pull_auth"
    attempt_id: str
    registry: str | None = None
    token: str | None = None
    expires_at: datetime | None = None
    reason: str | None = None


class Error(_Msg):
    type: Literal["error"] = "error"
    code: str
    message: str
    ref_msg_id: str | None = None


AgentMessage = Annotated[
    Hello
    | Heartbeat
    | Ready
    | Accept
    | Decline
    | Started
    | Progress
    | UploadRequest
    | PullAuthRequest
    | Commit
    | Abort
    | RenewCert,
    Field(discriminator="type"),
]

ServerMessage = Annotated[
    Challenge
    | Welcome
    | HeartbeatAck
    | Offer
    | NoWork
    | UploadURLs
    | PullAuth
    | CommitAck
    | CertRenewed
    | Cancel
    | Preempt
    | Revoke
    | Error,
    Field(discriminator="type"),
]

AGENT_MESSAGE_ADAPTER: TypeAdapter[AgentMessage] = TypeAdapter(AgentMessage)
SERVER_MESSAGE_ADAPTER: TypeAdapter[ServerMessage] = TypeAdapter(ServerMessage)

ALL_MESSAGE_TYPES: dict[str, type[_Msg]] = {
    m.model_fields["type"].default: m
    for m in (
        Challenge,
        Hello,
        Welcome,
        Heartbeat,
        HeartbeatAck,
        Ready,
        Offer,
        NoWork,
        Accept,
        Decline,
        Started,
        Progress,
        UploadRequest,
        UploadURLs,
        PullAuthRequest,
        PullAuth,
        Commit,
        CommitAck,
        Abort,
        RenewCert,
        CertRenewed,
        Cancel,
        Preempt,
        Revoke,
        Error,
    )
}


def parse_agent_message(raw: str | bytes) -> AgentMessage:
    return AGENT_MESSAGE_ADAPTER.validate_json(raw)


def parse_server_message(raw: str | bytes) -> ServerMessage:
    return SERVER_MESSAGE_ADAPTER.validate_json(raw)
