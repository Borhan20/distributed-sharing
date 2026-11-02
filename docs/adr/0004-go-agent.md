# ADR 0004: Write the device agent in Go

| | |
|---|---|
| **Status** | Accepted (proposed for team review) |
| **Date** | 2026-11-02 |
| **Deciders** | Yaana R&D (to confirm at team review) |
| **Plan link** | [`plan.md` §3 D4](../../plan.md#3-key-decisions), §5.1, §5.2 |

## Context

The agent runs on owners' Linux and Windows machines. It must be easy to
install, small, long-running and safe. It talks to the container engine,
parses messages from the network and enforces the owner's policy.

## Decision

We will write the agent in Go. It ships as one static binary per OS and CPU
type. It uses a container runtime interface whose first implementation
talks to Docker Engine (Moby) or Podman, rootless. Protocol parsers get
Go native fuzzing in Phase 5.

## Consequences

- Good: single-file install on Linux and Windows; easy cross-compiling.
- Good: memory-safe language for the code that parses untrusted input.
- Good: mature Docker/Moby client libraries and a large container ecosystem in Go.
- Bad: the control plane is Python, so the team works in two languages.
  The protocol contract (Pydantic models, JSON Schema, shared fixtures)
  keeps both sides in step.
- Note: the plan names `github.com/moby/moby/client` because
  `github.com/docker/docker` is deprecated from Docker v29. The first slice
  may call the Docker CLI behind the same interface; see ADR 0010.

## Alternatives considered

| Option | Why not chosen |
|---|---|
| Python agent | Harder to ship as one file; needs a Python runtime on every device. |
| Rust agent | Also memory-safe and small, but the team has less Rust experience and container client libraries are less mature. |

## Security review

Security-sensitive: `agent/sandbox`, `agent/policy`, `agent/enroll` need a
security reviewer (plan §8.10).
