# ADR 0007: PostgreSQL is the source of truth and the job queue in v1

| | |
|---|---|
| **Status** | Accepted (proposed for team review) |
| **Date** | 2026-11-02 |
| **Deciders** | Yaana R&D (to confirm at team review) |
| **Plan link** | [`plan.md` §3 D7](../../plan.md#3-key-decisions), §5.1, §7.2, Phase 0 spike S6 |

## Context

The control plane must store users, groups, devices, jobs, tasks, attempts,
leases and the audit log. It also needs a queue of pending tasks that
several API workers can claim at once without handing one task to two
devices. Every extra system is one more thing each organization must run.

## Decision

We will keep all state in PostgreSQL and use it as the task queue. A device
claims a task with `SELECT ... FOR UPDATE SKIP LOCKED` inside a transaction
that also creates the attempt and bumps the task's epoch. Schema changes go
through Alembic migrations.

## Consequences

- Good: one fewer system to run, back up and secure.
- Good: claiming a task, creating its lease and writing the audit event can
  happen in one transaction.
- Bad: throughput is bounded by one PostgreSQL primary. Spike S6 measures
  claim throughput at our target scale (see `docs/spikes/`).
- Bad: the hash-chained audit log serializes audited writes with an advisory
  lock. Fine at pilot scale; revisit if S6 or load tests show contention.
- Follow-up: a high-availability guide in Phase 5. If a dedicated queue is
  ever needed, the claim functions are isolated in the scheduler module.

## Alternatives considered

| Option | Why not chosen |
|---|---|
| Redis or RabbitMQ queue next to PostgreSQL | Two systems to keep consistent; lease and attempt creation would no longer be one transaction. |
| Kafka | Heavy for a self-hosted single server; ordering semantics we don't need. |
| Temporal / Celery | No device management or owner policies; adds a large dependency (plan §4). |

## Security review

Not security-sensitive in itself. The audit table design is (plan §8.9).
