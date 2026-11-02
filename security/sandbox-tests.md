# Sandbox test suite

The sandbox suite checks that a job cannot harm the device it runs on
(plan §8.3, Phase 1 exit criteria). It has an automated Linux part and a
manual Windows part.

> **Status.** The automated suite is part of the first slice. The Windows
> checks have **not** been run: there was no Windows host (ADR 0010, X6).
> Passing these tests shows the listed vectors are blocked; it does not
> prove the sandbox is escape-proof.

## 1. Automated suite (Linux)

Location: `agent/sandbox/`, Go tests behind the build tag `docker`. They
start real containers with the same options the agent uses for real jobs.

```bash
cd agent
go test -tags docker ./sandbox/...          # needs a Docker or Podman daemon
go test -tags docker -run Escape -v ./sandbox/...
```

Tests are skipped, not passed, when no container engine is reachable. CI
must run them on a runner that has one.

### What each test tries

| Vector | Attempt inside the container | Expected result |
|---|---|---|
| Root user | `id -u` | Not 0 (runs as 65534) |
| Capabilities | Read `CapEff` from `/proc/self/status` | All zero |
| Privilege escalation | Read `NoNewPrivs` from `/proc/self/status` | 1 |
| Root filesystem | Write a file under `/` or `/etc` | Fails (read-only) |
| Host files | Look for host paths (home directories, `/etc/shadow` of the host, the Docker socket) | Not present; only `/out` and `/tmp` are writable |
| Docker socket | Look for `/var/run/docker.sock` or `/run/podman/podman.sock` | Not present |
| Outbound internet | Connect to a public IP | Fails (`--network none`) |
| Cloud metadata | Connect to `169.254.169.254` | Fails |
| LAN | Connect to the host's LAN gateway or a private range | Fails |
| Fork bomb | Spawn processes past the limit | Stopped by the pids limit; host unaffected |
| Memory | Allocate past the limit | Container is killed (OOM); host unaffected |
| Runtime limit | Sleep past `max_runtime_s` | Agent stops the container |
| Output path | Write to `/out` | Works, and nothing outside the per-attempt directory changes on the host |
| Cross-attempt leak | Second attempt looks for the first attempt's files | Not present (new container, new output dir) |
| T2 only | Run the same tests with `--runtime runsc` | Same results; skipped when `runsc` is not installed |

When you find a new escape vector, add a test for it first, then fix it.

## 2. Gaps in the automated suite

| Gap | Why | Plan |
|---|---|---|
| GPU containers | No GPU on the build machine | Spike S1 on our GPUs; add GPU cases to CI on a GPU runner |
| GPU memory between groups | Needs real GPUs and a method to read leftover memory | Research item (threat #7) |
| Kata (T3) | Phase 3 | Add the same tests with the Kata runtime |
| Egress proxy | Not built (X5) | When built: allowlisted domain works; LAN, link-local and metadata IPs still blocked |
| Disk quota per container | Depends on storage driver | Decide in Phase 1; add a disk-fill test |

## 3. Manual Windows / WSL2 checks (spike S2)

Run on a real Windows 11 machine with the agent's dedicated WSL2 distro.
Record results in `docs/spikes/S2-wsl2-isolation.md`.

Setup checks:

- [ ] The agent created a dedicated distro; it is not the owner's own distro.
- [ ] `/etc/wsl.conf` in the distro has `[automount] enabled = false`, `mountFsTab = false`, `[interop] enabled = false`, `appendWindowsPath = false`.
- [ ] The agent re-checks `wsl.conf` at start and refuses to run jobs if it was changed.
- [ ] The container engine inside the distro is rootless Docker Engine or Podman, not Docker Desktop.
- [ ] No Linux NVIDIA driver is installed inside the distro; GPU access is through CDI (`nvidia-ctk cdi generate`).

Escape attempts from inside a job container:

- [ ] `ls /mnt/c`, `/mnt/d` and any `/mnt/*`: nothing from Windows drives.
- [ ] Try to run `cmd.exe`, `powershell.exe`, `explorer.exe` by name and by `/init` interop paths: fails.
- [ ] Look for `\\wsl$` / `\\wsl.localhost` paths of other distros: not reachable.
- [ ] Try to reach the Windows host IP and the LAN: fails (`--network none`).
- [ ] Repeat every Linux test in §1.

From inside the distro (outside containers), as the engine's user:

- [ ] Cannot become root without a password.
- [ ] Cannot edit `/etc/wsl.conf`.

Owner-facing checks:

- [ ] The owner can see what is running and stop it with the kill switch.
- [ ] Residual risks (shared WSL2 kernel, T1 maximum) are shown to the owner before sharing.
