# ADR 0002: One central control plane per server, not peer-to-peer

| | |
|---|---|
| **Status** | Accepted (proposed for team review) |
| **Date** | 2026-11-02 |
| **Deciders** | Yaana R&D (to confirm at team review) |
| **Plan link** | [`plan.md` §3 D2](../../plan.md#3-key-decisions), §5, §7 |

## Context

The original idea was torrent-like sharing between devices. Scheduling,
leases, group membership, approvals and the audit log all need one
consistent view of state. Devices sleep, switch off and change networks
often.

## Decision

We will run one central control plane per server. It holds all state in
PostgreSQL and makes all scheduling decisions. Devices never coordinate
jobs directly with each other. Peer-to-peer transfer is kept only as a
later research item for moving large datasets (plan §7.8, Phase 4).

## Consequences

- Good: one source of truth makes leases, fencing epochs and "exactly one
  committed result" simple to enforce and to test.
- Good: easier to secure (one place for authorization and audit) and to debug.
- Bad: the control plane is a single point of failure. Mitigations: agents
  finish running tasks and queue results while it is down; a PostgreSQL
  high-availability guide comes in Phase 5.
- Bad: all control traffic goes through the server, even between two
  devices on the same LAN. Acceptable for control messages; bulk data uses
  the object store.

## Alternatives considered

| Option | Why not chosen |
|---|---|
| Fully peer-to-peer (gossip, DHT) | Hard to get consistent leases, approvals and an audit log. Larger attack surface on each device. |
| Hybrid with peer-elected leaders | Complex failure modes for little gain at our target scale. |

## Security review

Touches authorization and audit design. The control plane becomes the
highest-value target (threat #8 in plan §8.2); the agent's local policy and
pinned signing key limit what a compromised server can do.
