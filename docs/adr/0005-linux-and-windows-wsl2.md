# ADR 0005: Support Linux and Windows in v1; Windows runs jobs in a dedicated WSL2 distro

| | |
|---|---|
| **Status** | Accepted (proposed for team review) |
| **Date** | 2026-11-02 |
| **Deciders** | Yaana R&D (to confirm at team review) |
| **Plan link** | [`plan.md` §3 D5](../../plan.md#3-key-decisions), §8.3, §8.5, §12 |

## Context

Most lab and office machines run Linux or Windows. macOS is out of scope
for v1 because Docker on macOS cannot use the Apple GPU. Linux jobs need
containers; Windows has no native equivalent with the isolation we want.

## Decision

We will support Linux and Windows devices in v1. On Windows, the agent runs
as a Windows service and runs all jobs inside a dedicated WSL2 distro that
it creates and manages, with automount and interop turned off in
`/etc/wsl.conf`. The container engine inside that distro is rootless Docker
Engine or Podman, not Docker Desktop. A small helper in the user's session
reports idle state.

## Consequences

- Good: covers most lab and office machines.
- Bad: Windows isolation is weaker. All WSL2 distros share one VM and one
  kernel; gVisor and Kata are not available there in v1. **Windows tops out
  at tier T1.** Groups can require `os: linux` for sensitive work.
- Bad: root inside the distro could edit `wsl.conf`. Mitigations: jobs never
  run as root, the engine is rootless, and the agent checks the config at
  every start.
- Follow-up: spikes S2 (escape attempts in WSL2) and S3 (gVisor in WSL2)
  decide the final Windows tier. A Hyper-V VM per device is a research option.
- Status in slice 1: the Windows code path exists but is **untested**; there
  was no Windows host (ADR 0010, X6).

## Alternatives considered

| Option | Why not chosen |
|---|---|
| Linux only | Leaves out many lab and office PCs. |
| Windows containers (process or Hyper-V isolation) | Linux images are what researchers use; GPU support and tooling differ. |
| Docker Desktop on Windows | Proprietary and paid for organizations with 250+ employees or $10M+ revenue (plan §14). |
| Agent-managed Hyper-V VM | Stronger isolation, much more work. Kept as research. |

## Security review

Security-sensitive: sandboxing on Windows. Residual risks must be shown to
owners in plain language before they share a Windows device.
