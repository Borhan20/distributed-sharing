# ADR 0006: Trusted, invite-only groups; every job is still sandboxed

| | |
|---|---|
| **Status** | Accepted (proposed for team review) |
| **Date** | 2026-11-02 |
| **Deciders** | Yaana R&D (to confirm at team review) |
| **Plan link** | [`plan.md` §3 D6](../../plan.md#3-key-decisions), §1 Non-goals, §6.3, §8.1 |

## Context

Public volunteer computing needs payments, reputation, cheat detection and
result verification at scale (BOINC-style). Our users are research teams,
classes and dev teams whose members know each other.

## Decision

We will support only trusted, invite-only groups. People join with an
invite link or a join request approved under the group's rule
(`coordinator-only`, `any-k-members` or `majority`). There is no public
marketplace, no payments and no crypto tokens. We still sandbox every job,
because trusting a person is not the same as trusting their code.

## Consequences

- Good: no payment system, no cheat detection, simpler fairness (fair share
  and optional credits with no money).
- Good: result verification can be opt-in (`verify: 2`) instead of always on.
- Bad: a compromised member account or a malicious dependency in a job image
  is still a real threat. Mitigations: sandbox tiers, deny-by-default
  network, owner `accept_from` lists, probation, fast removal, audit log.
- Bad: removal must take effect immediately: cancel running jobs, stop the
  person's devices serving the group, flag reachable secrets for rotation.

## Alternatives considered

| Option | Why not chosen |
|---|---|
| Open marketplace with strangers | Needs payments, reputation and verification; much larger attack surface. Out of scope. |
| Trusted groups without sandboxing | One bad image or stolen account could harm every member's device. |

## Security review

Security-sensitive: membership, approvals and removal are access control
(`control-plane/pool_cp/authz`).
