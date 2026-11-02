# ADR 0009: FastAPI for the control plane, React for the web UI

| | |
|---|---|
| **Status** | Accepted (proposed for team review) |
| **Date** | 2026-11-02 |
| **Deciders** | Yaana R&D (to confirm at team review) |
| **Plan link** | [`plan.md` §3 D9](../../plan.md#3-key-decisions), §5.1, §5.2 |

## Context

The control plane needs a REST API for the UI and CLI, a WebSocket endpoint
for agents, and strict input validation. The protocol must be shared with
the Go agent. The team already knows Python and React.

## Decision

We will build the API server with FastAPI and Pydantic, SQLAlchemy and
Alembic for the database, and Typer for the CLI. The web UI is React with
TypeScript, Vite and TanStack Query. FastAPI's OpenAPI document generates a
typed client for the UI. Pydantic models generate JSON Schema for the agent
protocol.

## Consequences

- Good: the team can move fast in familiar tools.
- Good: one set of Pydantic models validates input, documents the API and
  defines the protocol schema.
- Good: the scheduler is a separate Python process, so hot paths can move to
  Go later if profiling shows a need.
- Bad: Python is slower than Go for the scheduler. Spike S6 and the Phase 2
  load tests check whether that matters.
- Bad: two languages share the protocol. Contract tests with shared JSON
  fixtures run in CI on both sides.

## Alternatives considered

| Option | Why not chosen |
|---|---|
| Go for the whole control plane | One language end to end, but slower to build the API and UI glue; less team experience. |
| Django | Heavier; async WebSocket support and typed schema generation are less direct. |
| Server-rendered UI | The UI needs live job and log views; a typed SPA fits better. |

## Security review

The API server holds authentication and authorization code
(`control-plane/pool_cp/auth`, `authz`), which needs a security reviewer.
