# Phase 0/1 implementation notes

| | |
|---|---|
| **Status** | Implemented as a first vertical slice; for team review |
| **Date** | 2 November 2026 |
| **Plan** | [`plan.md`](../../plan.md) (same content as `group_compute_plan.md`) |

This file records how the first slice of the plan is built, and where it
deliberately differs from the plan. Every difference is listed in
[§9](#9-deviations-from-the-plan) so the team can accept or reverse it.

> **Security review required.** Everything under `control-plane/pool_cp/auth`,
> `authz`, `audit`, `ca`, `agent/sandbox`, `agent/policy`, `agent/enroll` and
> `protocol/` is security-sensitive (plan §8.10). None of it has had a human
> security review yet.

---

## 1. Repository layout (plan §15)

```
control-plane/   FastAPI app (pool_cp), scheduler worker, Alembic migrations
protocol/        Pydantic protocol + job-spec models (pool_protocol), JSON Schema, contract fixtures
agent/           Go agent (module "pool/agent")
cli/             Typer CLI (pool_cli) and Python SDK (pool)
web/             React + TypeScript + Vite + TanStack Query
deploy/          docker compose for one server
docs/adr/        ADRs D1-D9
docs/spikes/     Spike reports (S6 measured here; others need hardware)
security/        Threat model, checklists; sandbox tests live in agent/sandbox
tests/e2e/       Multi-agent and chaos tests (pytest, real Postgres, real agents)
```

Python is a uv workspace (root `pyproject.toml`), Python 3.12. Go 1.23.

## 2. Identifiers

- `users.handle`: unique short name from the OIDC `preferred_username` claim
  (falls back to the `sub`). Used in policies (`accept_from`) and envelopes.
  No e-mail or real names are stored.
- `groups.name`: slug `^[a-z0-9][a-z0-9-]{1,62}$`, unique per server.
- Devices, jobs, tasks, attempts: UUIDv4. `attempt_id` **is** the `lease_id`.
- Tiers: `T0` < `T1` < `T2` < `T3` (rank 0-3). "min_tier T1" means rank >= 1.

## 3. Agent protocol v1alpha1 (plan §7.12)

Transport: one WebSocket per enrolled profile at `GET /api/v1/agent/ws`.
Every frame is a JSON object with a `type` discriminator and a `msg_id`
(UUID, used for idempotency and tracing). Field names are snake_case.
Timestamps are RFC 3339 UTC strings. Binary values are standard base64.

The Pydantic models in `protocol/pool_protocol/messages.py` are the source of
truth; `protocol/schema/*.json` is generated from them, and
`protocol/fixtures/*.json` holds one valid example per message type. The Go
types in `agent/protocol` are hand-written and must parse every fixture (the
contract test, plan §10).

### 3.1 Enrollment (HTTPS, before a certificate exists)

`POST /api/v1/agent/enroll`

```json
{"one_time_code": "ABCDE-FGHJK", "csr_pem": "-----BEGIN CERTIFICATE REQUEST-----...",
 "device_facts": {"name": "lab-ws-01", "os": "linux", "arch": "amd64", "agent_version": "0.1.0"}}
```

- The CSR must be signed by an **Ed25519** key generated on the device. The
  private key never leaves the device (file mode 0600).
- The code was created by the signed-in owner (`POST /api/v1/devices/enrollment-codes`),
  is single-use and valid for 10 minutes. Only its SHA-256 is stored.
- Response:

```json
{"device_id": "uuid", "cert_pem": "...", "ca_cert_pem": "...", "cert_not_after": "...",
 "signing_keys": [{"key_id": "k1", "public_key_b64": "..."}], "ws_path": "/api/v1/agent/ws"}
```

The agent pins `ca_cert_pem` and `signing_keys` in its profile.

### 3.2 Session start

1. S→A `challenge {nonce_b64, server_time}`: 32 random bytes.
2. A→S `hello {protocol, agent_version, device_id, profile, cert_pem, nonce_signature_b64,
   capabilities, shares, policy_digest, running}`.
   The signature is Ed25519 over `b"pool-agent-hello-v1:" + nonce`.
   The server checks: certificate issued by its CA, not expired, serial not on
   the deny list, device not revoked, certificate CN == `device_id`, signature
   valid for the certificate's public key.
3. S→A `welcome {device_id, heartbeat_interval_s, lease_ttl_s, server_time, stale}`.
   `stale` lists attempt ids from `running` that the agent must stop now.

If the reverse proxy terminates mTLS, this challenge is a second layer, not a
replacement (see [§9](#9-deviations-from-the-plan)).

### 3.3 Messages

| Dir | `type` | Fields | Notes |
|---|---|---|---|
| S→A | `challenge` | `nonce_b64, server_time` | First frame |
| A→S | `hello` | see above | |
| S→A | `welcome` | `device_id, heartbeat_interval_s, lease_ttl_s, server_time, stale[]` | |
| A→S | `heartbeat` | `running[{attempt_id, epoch, state}], usage{cpu_percent, memory_mb}, owner_active, shares[]` | Every 15 s; renews leases |
| S→A | `heartbeat_ack` | `renewed[], stale[], lease_expires_at` | Agent kills every `stale` attempt |
| A→S | `ready` | `free{cpu, memory_mb, disk_mb, gpus}, tiers[]` | Ask for one task |
| S→A | `offer` | `attempt_id, task_id, epoch, expires_at, envelope{key_id, payload_b64, signature_b64}` | |
| S→A | `no_work` | `retry_after_s` | |
| A→S | `accept` | `attempt_id, epoch, tier` | Tier the agent will use |
| A→S | `decline` | `attempt_id, epoch, reason, detail` | Task goes back to the queue |
| A→S | `started` | `attempt_id, epoch` | Container started (Assigned → Running) |
| A→S | `progress` | `attempt_id, epoch, seq, stream, log_chunk, checkpoint_ref?` | stdout/stderr, ≤ 64 KiB per chunk |
| A→S | `upload_request` | `attempt_id, epoch, artifacts[{path, sha256, size}]` | |
| S→A | `upload_urls` | `attempt_id, urls[{path, sha256, url, expires_at}], reason?` | Empty + `reason` (`no_object_store`, `object_store_not_ready`) when there is no usable store. URLs are signed for `POOL_S3_PUBLIC_ENDPOINT` and cover size and `x-amz-checksum-sha256` |
| A→S | `commit` | `idempotency_key, task_id, attempt_id, epoch, exit_code, outputs[{path, sha256, size}], error?` | Fenced |
| S→A | `commit_ack` | `attempt_id, accepted, reason?` | |
| A→S | `abort` | `attempt_id, epoch, reason, detail` | Owner returned, kill switch, disk full, ... |
| A→S | `renew_cert` | `csr_pem` | Over the authenticated session |
| S→A | `cert_renewed` | `cert_pem, cert_not_after` | |
| S→A | `cancel` | `attempt_id, reason` | |
| S→A | `preempt` | `attempt_id, reason` | |
| S→A | `revoke` | `group, reason` | Stop all work for this group |
| S→A | `error` | `code, message, ref_msg_id?` | |

Decline reasons: `signature_invalid`, `envelope_expired`, `wrong_device`,
`group_not_shared`, `outside_window`, `owner_active`, `submitter_not_accepted`,
`probation_not_accepted`, `tier_not_allowed`, `resources_exceed_limits`,
`egress_not_allowed`, `image_not_allowed`, `kill_switch`, `busy`, `other`.

Abort reasons: `owner_returned`, `kill_switch`, `policy`, `agent_shutdown`,
`disk_full`, `runtime_error`.

### 3.4 Task envelope

`payload_b64` decodes to JSON bytes; the agent verifies the signature over
those exact bytes **before** parsing them (no canonicalisation needed):

```json
{"v": "v1alpha1", "attempt_id": "...", "task_id": "...", "job_id": "...", "epoch": 2,
 "group": "research-team", "group_id": "...", "submitter": "alice", "submitter_probation": false,
 "device_id": "...", "issued_at": "...", "expires_at": "...",
 "spec": {"image": "registry.example.org/x@sha256:<64 hex>", "command": ["python", "train.py"],
          "env": {"SEED": "3"},
          "resources": {"cpu": 4, "memory_mb": 16384, "disk_mb": 10240, "gpus": 0,
                        "gpu_vendor": null, "gpu_memory_mb": 0},
          "min_tier": "T1", "network": {"egress": "none", "allow": []},
          "data_class": "internal", "max_runtime_s": 7200, "outputs_path": "/out",
          "checkpoint": null, "inputs": []}}
```

The agent rejects an envelope whose `device_id` is not its own or whose
`expires_at` has passed.

## 4. Leases, fencing and the task state machine (plan §7.2-7.4)

- `tasks.epoch` only goes up. Each new attempt gets `epoch = task.epoch + 1`.
- An attempt holds a lease (`attempts.lease_expires_at`). An offer and an
  accepted attempt both live for `lease_ttl_s` (default 60 s) and are renewed
  by heartbeats that report the attempt with the right epoch.
- **Fencing rule:** a `started`, `progress`, `upload_request`, `commit` or
  `abort` is applied only if the attempt belongs to the sending device, its
  epoch matches, it is the task's *current* attempt, and its state is
  `assigned` or `running`. Everything else is rejected (and counted).
- **Idempotency:** a repeated `commit` for an attempt that already committed
  returns the original `commit_ack` and changes nothing.
- Task claiming uses `SELECT ... FOR UPDATE OF t SKIP LOCKED` (plan D7).
- The scheduler worker (`python -m pool_cp.scheduler`) expires leases, rolls
  up job states and expires groups. After a server start it waits one lease
  period before expiring anything, so agents can reconnect first (plan §7.4).

Task states: `pending → assigned → running → succeeded | failed | cancelled`,
with `assigned → pending` (declined or lease expired) and
`running → pending` (lease lost, preempted, owner returned).

Retry rules: a non-zero exit or a lost lease (while running) uses one retry.
After a non-zero exit the task waits `min(10 s · 2^(n-1), 10 min)` with ±20 %
jitter; after a lost lease it is requeued at once, so a device that went to
sleep does not delay the job. A task that failed (non-zero
exit) on 2 different devices is marked `failed` at once. Declines and owner
returns do not use a retry.

## 5. Matching (plan §7.1)

On `ready`, the server locks up to 20 candidate tasks with `SKIP LOCKED`,
ordered by: the agent's group priority, then the submitter's CPU-seconds in
that group over the last 7 days (fair share), then submission time. Hard
filters in SQL: group shared (server-side `device_shares` **and** reported by
the agent's local policy as active now), resources fit, tier, OS, data class,
no recent decline by this device. In Python: `accept_from`, probation,
"failed on this device before". The first task that passes is offered.

## 6. Roles (plan §6.2)

coordinator ⊃ admin ⊃ member. `contributor` can only share devices;
`submitter` can only submit jobs.

| Action | Roles |
|---|---|
| Update settings, change roles, remove members, close group | coordinator |
| Create/revoke invites, decide join requests, read audit log | coordinator, admin |
| Vote on join requests (`any-k-members`, `majority`) | coordinator, admin, member |
| Submit, cancel own jobs | coordinator, admin, member, submitter |
| Cancel anyone's job | coordinator, admin |
| Share own device | coordinator, admin, member, contributor |
| Read jobs, devices, members of the group | every member |

Invite links are pre-approved by the admin who made them; join requests
without an invite follow the group's approval rule. MFA (`amr` contains
`mfa`/`otp`/`hwk`) is required for coordinator and admin actions when
`POOL_REQUIRE_MFA_FOR_ADMINS=true` (the default outside dev mode).

## 7. Agent local policy (plan §6.4)

YAML file per profile (`<home>/profiles/<name>/policy.yaml`), same shape as
the plan's example plus:

```yaml
shares:
  - group: research-team
    accept_new_members: false   # probation (plan §6.3 item 3)
images:
  allow: []                     # optional image prefix allowlist (threat #8)
```

The agent reports `shares` (group, limits, accept_from, accept_new_members,
active_now) and `policy_digest` (SHA-256 of the file) in `hello` and
`heartbeat`. The server only uses this to avoid useless offers; the agent
re-checks everything on every offer.

## 8. Sandbox flags (plan §8.3)

T1 (every container): `--user 65534:65534 --cap-drop ALL --security-opt
no-new-privileges --read-only --network none --pids-limit 512 --memory/--memory-swap
<limit> --cpus <n> --tmpfs /tmp:rw,noexec,nosuid,size=256m`, default seccomp,
a new container per attempt, only `/out` (and later `/ckpt`, inputs read-only)
mounted. T2 adds `--runtime runsc`. GPU uses CDI: `--device nvidia.com/gpu=all`.
Jobs asking for egress are declined in this slice (the egress proxy is not built yet).

## 9. Deviations from the plan

| # | Plan says | This slice does | Why / follow-up |
|---|---|---|---|
| X1 | mTLS on the agent WebSocket, certificates from step-ca | Built-in Ed25519 device CA (`pool_cp/ca`) issuing 24 h certificates, plus a certificate-bound challenge on every session. mTLS at the reverse proxy is still recommended. | Works without a proxy in dev and tests. A step-ca backend behind the same interface is follow-up work. |
| X2 | Go protocol types generated from JSON Schema | Hand-written Go types, checked against shared fixtures in CI | The schema is small; a generator can replace them later without changing the wire format. |
| X3 | Logs in the object store | Logs in PostgreSQL (`task_logs`) | Simpler for Phase 1; move to SeaweedFS when volumes grow. |
| X4 | Separate scheduler matches tasks | Matching runs in the API process on `ready` (SQL with `SKIP LOCKED`); the scheduler process handles leases, retries and roll-ups | The pull model makes matching a per-request query. The functions live in `pool_cp/scheduler/` so they can move. |
| X5 | Egress allowlist through a proxy | Jobs asking for egress are declined | Deny by default until the proxy exists. |
| X6 | Windows agent in WSL2 | Code path exists but is **untested** | No Windows host here; spike S2. |
| X7 | Agent uses the Docker Engine Go SDK | Agent calls the `docker` CLI (no shell) behind a `Runtime` interface | `github.com/moby/moby/client` needs Go 1.24; the repo is on Go 1.23. |
| X8 | Audit log for everything | Per-attempt start/end are audit events in one global chain | Simple and complete, but it caps lifecycle throughput (spike S6). |
