# ADR 0010: Deviations from the plan in the first implementation slice

| | |
|---|---|
| **Status** | Proposed (team to accept or reverse each item) |
| **Date** | 2026-11-02 |
| **Deciders** | Yaana R&D; security reviewer for X1 and X5 |
| **Plan link** | [`docs/design/phase1-implementation.md` §9](../design/phase1-implementation.md#9-deviations-from-the-plan), plan §5.2, §7.12, §8.6, §8.7 |

## Context

The first slice of Phase 0/1 was built on one Linux machine with Docker and
PostgreSQL, without step-ca, a reverse proxy, a Windows host or an object
store. A few parts of the plan were simplified to get a working, testable
vertical slice. Each change is recorded here so the team can decide.

## Decision

We will accept these six deviations for slice 1 only. Each one has a
follow-up item.

| # | Plan says | Slice 1 does | Follow-up |
|---|---|---|---|
| X1 | mTLS on the agent WebSocket; certificates from step-ca | A built-in Ed25519 device CA (`control-plane/pool_cp/ca`) issues 24 h certificates. Every session also proves key possession: the agent signs `"pool-agent-hello-v1:" + nonce` with its device key, and the server checks the certificate chain, expiry, deny list, revocation and CN = device id. mTLS at the reverse proxy is still recommended as a second layer. | Add a step-ca backend behind the same CA interface. Decide whether the built-in CA stays for small setups. |
| X2 | Go protocol types generated from JSON Schema | Hand-written Go types in `agent/protocol`, checked against the shared fixtures in `protocol/fixtures/` in CI | Replace with generated types if the schema grows. The wire format does not change. |
| X3 | Logs in the object store | Logs in PostgreSQL (`task_logs`) | Move to SeaweedFS when log volume grows. |
| X4 | A separate scheduler process matches tasks to devices | Matching runs in the API process when an agent sends `ready` (one SQL query with `SKIP LOCKED`). The scheduler process expires leases, applies retries and rolls up job states. | The matching functions live in `pool_cp/scheduler/` and can move to the worker or to Go. |
| X5 | Egress allowlist enforced by an agent-run proxy; LAN and metadata IPs always blocked | Jobs that ask for any egress are declined (`egress_not_allowed`). All containers run with `--network none`. | Build the egress proxy (Phase 2/3) before allowing egress. |
| X6 | Go agent on Windows inside a dedicated WSL2 distro | The code path exists but is **untested**; there was no Windows host | Spike S2 on real Windows hardware before any Windows pilot. |
| X7 | Agent uses the Docker Engine Go SDK (`github.com/moby/moby/client`) | The agent calls the `docker` CLI (no shell) behind a `Runtime` interface | The SDK module needs Go 1.24; the repository uses Go 1.23. Move to the SDK when the toolchain is upgraded. |

## Consequences

- Good: a working end-to-end slice that can be tested on one Linux machine.
- Bad (X1): the built-in CA is new security-sensitive code. It needs a
  security review. The CA private key must be protected like any CA key.
  The application-layer challenge does not hide traffic metadata; TLS on
  the connection is still required in any real deployment.
- Bad (X3): log storage grows the database.
- Bad (X4): matching load lands on the API process.
- Bad (X5): jobs that need to download data at run time cannot run yet.
  Inputs must come through the object store.
- Bad (X6): Windows is not ready for a pilot.
- Bad (X7): the agent depends on the `docker` binary on the PATH and parses its output.

## Alternatives considered

| Option | Why not chosen |
|---|---|
| Wait for step-ca, a proxy and a Windows host before building | Delays every other part of Phase 1. |
| Allow egress without a proxy | Breaks "deny by default" (plan §8.1) and the "LAN and metadata always blocked" rule. |

## Security review

**Required** for X1 (device identity and CA) and X5 (network isolation).
None of this code has had a human security review yet.
