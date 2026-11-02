# ADR 0008: License the project under Apache-2.0

| | |
|---|---|
| **Status** | Accepted (proposed for team review; needs counsel review before public release) |
| **Date** | 2026-11-02 |
| **Deciders** | Yaana R&D and legal counsel |
| **Plan link** | [`plan.md` §3 D8](../../plan.md#3-key-decisions), §14 |

## Context

The project will be published as open source (ADR 0001). The license must
be compatible with the core dependencies and acceptable to companies and
universities. Contributors' patents are a concern for organizations.

## Decision

We will license the project under the Apache License 2.0. The full text is
in `LICENSE`; attribution goes in `NOTICE`. CI runs an automated license
check that fails on dependencies outside an allowlist.

## Consequences

- Good: explicit patent grant from contributors.
- Good: compatible with the chosen core stack: MIT, BSD, Apache-2.0, the
  PostgreSQL license and MPL-2.0 (plan §14).
- Bad: we must keep copyleft and source-available code out:
  - BUSL-1.1: HashiCorp Nomad and Vault (use OpenBao instead of Vault).
  - AGPL-3.0 (Grafana, Garage, NetBird servers) only as separate,
    unmodified, optional services.
  - Docker Desktop is never bundled or required.
- Note: Python dependencies under LGPL (for example psycopg) are avoided in
  the first slice (asyncpg, Apache-2.0, is used). If an LGPL dependency is
  needed, counsel should confirm how we use it.
- **This is not legal advice.** Counsel must review the license and the
  dependency list before the public release.

## Alternatives considered

| Option | Why not chosen |
|---|---|
| MIT | No explicit patent grant. |
| GPL-3.0 / AGPL-3.0 | Would discourage company use and conflicts with how many users want to embed or extend it. |
| MPL-2.0 | File-level copyleft; workable, but less familiar to the target organizations. |

## Security review

Not security-sensitive.
