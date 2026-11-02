# Architecture decision records

Each ADR records one decision, why we made it, and what it costs. Start a
new one from [`0000-template.md`](0000-template.md) and give it the next
number. Never edit an accepted ADR's decision; write a new ADR that
supersedes it.

All ADRs below are drafts for team review. "Accepted" means the plan
already commits to the decision; the team still has to confirm it.

| ADR | Decision | Plan | Status |
|---|---|---|---|
| [0001](0001-open-source-self-hosted.md) | Open source, self-hosted control plane per organization | D1 | Accepted (for review) |
| [0002](0002-central-control-plane.md) | One central control plane per server, not peer-to-peer | D2 | Accepted (for review) |
| [0003](0003-agents-pull-work.md) | Agents pull work over outbound connections only | D3 | Accepted (for review) |
| [0004](0004-go-agent.md) | Write the device agent in Go | D4 | Accepted (for review) |
| [0005](0005-linux-and-windows-wsl2.md) | Linux and Windows in v1; Windows jobs in a dedicated WSL2 distro | D5 | Accepted (for review) |
| [0006](0006-trusted-groups-only.md) | Trusted, invite-only groups; every job still sandboxed | D6 | Accepted (for review) |
| [0007](0007-postgresql-state-and-queue.md) | PostgreSQL is the source of truth and the job queue | D7 | Accepted (for review) |
| [0008](0008-apache-2-license.md) | Apache-2.0 license | D8 | Accepted (for review; needs counsel) |
| [0009](0009-fastapi-and-react.md) | FastAPI for the control plane, React for the UI | D9 | Accepted (for review) |
| [0010](0010-phase1-implementation-deviations.md) | Deviations X1-X6 in the first implementation slice | – | Proposed |
