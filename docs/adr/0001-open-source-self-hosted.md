# ADR 0001: Open source, self-hosted control plane per organization

| | |
|---|---|
| **Status** | Accepted (proposed for team review) |
| **Date** | 2026-11-02 |
| **Deciders** | Yaana R&D (to confirm at team review) |
| **Plan link** | [`plan.md` §3 D1](../../plan.md#3-key-decisions), §1 Goals, §14 |

## Context

The platform runs code from group members on personal and office computers.
The job data can be internal or confidential research data. Companies and
universities usually cannot send that data, or even job metadata, through a
third-party service. Goal 1 in the plan says "no central company in the
middle".

## Decision

We will publish the project as open source and design it to be self-hosted.
Each organization runs its own control plane (API, scheduler, PostgreSQL,
identity, device CA, object store). There is no hosted service run by us.

## Consequences

- Good: no central party sees job data, logs or device lists.
- Good: fits company and university policies that require on-premises data.
- Good: other groups can audit and improve the code.
- Bad: every organization must run and upgrade its own server. We need a
  simple one-server `docker compose` deployment, backups and upgrade notes.
- Bad: security fixes reach users only when each organization upgrades. We
  need signed releases and a clear `SECURITY.md` disclosure process.
- Follow-up: devices that belong to two organizations need several agent
  profiles ([plan §7.11](../../plan.md#711-one-device-several-servers)),
  because there is no federation in v1.

## Alternatives considered

| Option | Why not chosen |
|---|---|
| Hosted SaaS run by us | A central party would see data from every group. Many target organizations cannot use it. |
| Federation between servers in v1 | Much harder to secure and reason about. Agent profiles cover the cross-organization use case for now. |
| Closed source, self-hosted | Owners must trust the code that runs on their machines. Open code makes that trust checkable. |

## Security review

Not security-sensitive in itself. It raises the bar for release signing and
the disclosure process (ADR 0008, `SECURITY.md`).
