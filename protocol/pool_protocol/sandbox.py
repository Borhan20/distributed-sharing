"""The container rules every task gets, on a member's computer and with `pool run --local`.

The agent's sandbox (agent/sandbox CreateArgs) and the CLI's local check (cli/pool_cli/run.py) must apply the
same rules, or "it worked with --local" would mean nothing. This list is the one source: `python -m
pool_protocol.export` writes it to fixtures/sandbox_hardening.json, which the Go tests read, and the CLI
imports it.
"""

from __future__ import annotations

UNPRIVILEGED_USER = "65534:65534"
OUTPUTS_PATH = "/out"
SCRATCH_TMPFS = "/tmp:rw,noexec,nosuid,size=256m"  # noqa: S108 - a tmpfs inside the container, not a host path

# Each entry is a run of `docker create` arguments that must appear in that order.
HARDENING_FLAGS: list[list[str]] = [
    ["--user", UNPRIVILEGED_USER],
    ["--cap-drop", "ALL"],
    ["--security-opt", "no-new-privileges"],
    ["--read-only"],
    ["--network", "none"],
    ["--ipc", "private"],
    ["--pids-limit", "512"],
    ["--tmpfs", SCRATCH_TMPFS],
]


def fixture() -> dict:
    return {
        "about": "Container flags every task gets, on a member's computer (agent/sandbox CreateArgs) and with "
                 "`pool run --local` (cli/pool_cli/run.py). Generated from pool_protocol/sandbox.py by "
                 "`python -m pool_protocol.export`; the Go tests read it.",
        "flags": HARDENING_FLAGS,
        "outputs_path": OUTPUTS_PATH,
    }
