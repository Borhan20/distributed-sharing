# pool-agent

The Go agent that runs on each shared Linux or Windows computer (plan §5.1).
It enrolls the device, keeps one outbound WebSocket to the control plane, and
checks every task against the **owner's local policy**. Accepted tasks run in
hardened containers, and the agent reports logs and results.

> **Security review required.** `enroll/`, `policy/` and `sandbox/` are
> security-sensitive (plan §8.10). None of this code has had a human security
> review yet.

Protocol: v1alpha1, as described in
[`docs/design/phase1-implementation.md`](../docs/design/phase1-implementation.md)
and defined by `protocol/pool_protocol/messages.py`. The Go types in
`protocol/` are written by hand (design doc deviation X2). The contract tests
check them against `protocol/fixtures/` and `protocol/schema/`.

## Build

```sh
cd agent
go build -o bin/pool-agent ./cmd/pool-agent      # Linux
GOOS=windows go build -o bin/pool-agent.exe ./cmd/pool-agent   # compiles; UNTESTED on Windows
```

Go 1.23. Docker Engine on the device. Use rootless mode; the agent warns if
the engine is not rootless. Docker Desktop is **not** supported (plan §5.2).

## Use

```sh
# 1. In the web UI, the owner creates a one-time code (valid 10 minutes).
pool-agent enroll --server https://pool.example.org --code ABCDE-FGHJK --name lab-ws-01

# 2. Edit the local policy. A new device shares NOTHING until the owner adds a share.
$EDITOR ~/.config/pool-agent/profiles/default/policy.yaml

# 3. Run (as a systemd service in production).
pool-agent run                 # one profile
pool-agent run --all           # every enrolled server profile (plan §7.11)

pool-agent status              # what runs here and who sent it
pool-agent kill                # kill switch: stop everything now, accept nothing
pool-agent resume              # release the kill switch
```

- **Home directory:** `$POOL_AGENT_HOME`, else `~/.config/pool-agent` on Linux
  and `%ProgramData%\pool-agent` on Windows. Each profile lives in
  `profiles/<name>/` (directory mode 0700). It holds:
  - `device.key`: Ed25519, mode 0600; never leaves the device
  - `cert.pem`: the 24 h device certificate
  - `ca.pem`: the pinned device CA
  - `profile.json`: server URL, device id, pinned server signing keys
  - `policy.yaml`
  - `state.json`
  - `work/`
- **Plain http** is allowed only with `--insecure-http`, and only for
  `localhost`, `127.0.0.1` or `::1`. Use it for dev servers and tests.
  Everything else must use https/wss. Use `--server-ca` to pin a private TLS
  CA. The device certificate is also offered as a TLS client certificate, so
  a reverse proxy can enforce mTLS.
- **Certificate renewal:** when less than 8 hours of validity remain, the agent
  sends `renew_cert` over the authenticated session. If the certificate has
  expired (device offline for more than 24 h), the owner must enroll again.

## Local policy reference (plan §6.4)

```yaml
shares:
  - group: research-team              # group name on the server
    when:
      weekdays: "19:00-08:00"         # all-day | never | "HH:MM-HH:MM[,HH:MM-HH:MM]"
      weekends: all-day
      only_when_idle: 10m             # Go duration; no keyboard/mouse input for this long
    limits: { cpu_percent: 75, memory: 24Gi, gpu: true, disk: 100Gi }
    accept_from: all-members          # or a list of user handles: [alice, bob]
    accept_new_members: false         # accept jobs from members on probation
    on_owner_return: checkpoint-and-stop   # or: pause | throttle
priority: [research-team]             # order used when several groups want the machine
isolation:
  allow_tiers: [T1, T2, T3]           # T0 (bare metal) is off by default
egress: deny                          # deny | allow (jobs asking for egress are still declined, design doc X5)
kill_switch: true                     # accepted for compatibility; the kill switch always works
images:
  allow: []                           # optional image prefix allowlist
idle: auto                            # auto | assume-idle | assume-active
always_on: false                      # owner marks machines that never sleep (services, Phase 3)
```

Rules:

- Unknown keys are errors, so a typo cannot silently change what the owner
  meant.
- An empty or comment-only file shares nothing.
- **Time windows:**
  - Times are in local time.
  - If neither `weekdays` nor `weekends` is set, the share is active on every
    day.
  - If only one is set, the other day type is never active.
  - A range that crosses midnight is checked against the current day's type.
    For example, Monday 02:00 counts as inside a weekday "19:00-08:00" window.
- **Idle:**
  - An unknown idle state counts as "owner active", which is conservative.
  - `auto` uses systemd-logind `IdleHint`/`IdleSinceHint`. This is **to be
    validated in spike S4**.
  - Windows has no idle detection yet, because the user-session helper is not
    built. Shares with `only_when_idle` therefore never run there.
- **Policy reloads:** the file is re-read when it changes. If the new file is
  invalid, the previous policy stays in force.
- **What the server sees:** the agent reports its shares and the policy's
  SHA-256 in `hello` and `heartbeat`. The server uses this only to avoid
  useless offers. The agent re-checks every offer, whatever the server says.

### Admission order on every offer

1. Kill switch is off.
2. Ed25519 signature check against the keys pinned at enrollment. The payload
   is parsed only after this check passes.
3. The envelope matches the offer, has this device's id, and has not expired.
4. The group is shared.
5. The image is pinned by digest (and matches `images.allow`, if set).
6. The task asks for no egress.
7. The share is active now: its time window is open and the owner is idle.
8. The submitter is in `accept_from`, and probation is accepted where needed.
9. A tier is available: the strongest allowed tier at or above `min_tier`.
   GPU jobs can only use GPU tiers.
10. CPU, memory, disk and GPU fit within the share limits and the device's
    free capacity (shared across profiles).

Decline reasons are the ones listed in the design doc §3.3.

## Sandbox (plan §8.3)

| Tier | How |
|---|---|
| T1 | `docker create` with `--user 65534:65534 --cap-drop ALL --security-opt no-new-privileges --read-only --network none --ipc private --pids-limit 512 --memory/--memory-swap --cpus --tmpfs /tmp:rw,noexec,nosuid,size=256m`, default seccomp, a new container per attempt |
| T2 | T1 plus `--runtime runsc`, offered only when `docker info` lists a `runsc` runtime |
| GPU | `--device nvidia.com/gpu=all` (CDI). GPU tier is T1 only. **UNTESTED**: no GPU here |
| T0, T3 | Not implemented |

**How a container is set up:**

- **Only mount:** `/out`. It is bind-mounted from
  `work/mounts/<attempt>/out` (mode 0777 inside a 0700 parent).
- **Command:** the job's `command` replaces the image `ENTRYPOINT`, as with a
  Kubernetes `command`.
- **Environment:** the agent adds `POOL_ATTEMPT_ID`, `POOL_TASK_ID`,
  `POOL_EPOCH` and `POOL_OUTPUT_DIR`.

**After the container stops:**

- **Collecting outputs:** the agent runs `docker cp <container>:/out -`, which
  reads files whatever their owner or mode. It copies regular files into an
  agent-owned directory and hashes them with SHA-256. Symlinks and devices are
  skipped. The limit is 1000 files, and total size is capped at the task's
  disk request.
- **Uploading:** the agent sends `upload_request` and uploads to the presigned
  URLs. Only https URLs are used, or http when the profile itself is a
  localhost dev profile, so the server cannot point the agent at arbitrary
  LAN addresses. When the URL signs `x-amz-checksum-sha256`, the agent sends
  the file's SHA-256 in that header, and the store rejects a body that does
  not match. Then it commits.
- **Cleaning up:** the agent user cannot delete files that uid 65534 created
  inside nested directories. If deletion fails, a short-lived container runs
  as that same uid, with the same pinned image, no network and no
  capabilities, and runs `rm -rf /out/`. This needs `rm` in the image; if it
  is missing, the directory is left behind and a warning is logged.

**Not enforced yet:**

- Disk usage inside a running container (plan §7.4 "disk full").
- Ownership of leftover containers: the agent cannot reattach to them after a
  restart, so it removes them at start-up. Their leases expire on the server,
  and the tasks are retried.

### Docker client choice

The plan names the Engine Go SDK `github.com/moby/moby/client`. That module
exists (latest v0.6.2), but it needs **Go 1.24**, and this repository uses Go
1.23.6. The agent therefore calls the `docker` CLI through `exec.Command`.
Every argument is passed separately and no shell is used. The `Runtime`
interface in `sandbox/sandbox.go` keeps the SDK switch local to `sandbox/`.

## Kill switch

- `pool-agent kill` creates `<home>/KILL` and removes every container with the
  label `pool.agent=1` at once.
- A running agent checks for the file every second. It sends
  `abort {reason: kill_switch}` for each attempt, declines offers with
  `kill_switch`, and sends no `ready` until `pool-agent resume` removes the
  file.

## Behaviour when the connection drops (plan §7.4)

- Running attempts continue while the agent is disconnected.
- `started`, `progress` and `abort` messages are buffered. Up to 8 MiB of logs
  is kept, and the oldest log chunks are dropped first.
- A `commit` is kept until the server sends `commit_ack`, and it is re-sent
  after reconnecting. Commits are idempotent on the server.
- No new work starts while disconnected, because offers only arrive over a
  live session.
- Reconnects use exponential backoff from 1 s to 60 s with ±20 % jitter.
- `hello` reports running attempts. Any the server marks `stale` (in
  `welcome` or `heartbeat_ack`) are killed, and no result is sent for them.

## Tests

```sh
go vet ./... && go test -race ./...         # unit, contract, session (fake control plane)
go test -tags docker ./sandbox/... ./session/...   # sandbox escape suite + real-container session test
GOOS=windows go build ./...
```

The escape suite (`sandbox/sandbox_docker_test.go`) runs real containers. It
asserts that:

- the container runs as uid/gid 65534;
- `CapEff`, `CapPrm` and `CapBnd` are 0 and `NoNewPrivs` is 1;
- the root filesystem and `/etc` are read-only, and `/tmp` is `noexec`;
- the only network interface is `lo`;
- the internet, `169.254.169.254`, a LAN address and DNS are unreachable;
- a host marker file, the Docker socket, the home directory and `/mnt/c` are
  not visible;
- the pids and memory limits hold;
- outputs are collected (including a file with mode 0600) and cleaned up;
- the maximum runtime is enforced;
- kill, kill-all, pause, unpause and throttle work.

## Untested

- **Windows** (`sandbox/wsl.go`, `idle/idle_windows.go`,
  `sysinfo/sysinfo_windows.go`): compiles only. The WSL2 runtime drives
  `wsl.exe -d pool-runtime -u pool -- docker ...`, and the agent checks
  `/etc/wsl.conf` (automount and interop off) at every start. Spike S2 must
  validate this.
- **Private key on Windows:** file modes do not protect the key, which
  inherits the ACL of `%ProgramData%\pool-agent`. Restricting that ACL, or
  using OS-protected storage, is open Phase 0 work (plan §8.6).
- **GPUs:** detection through `nvidia-smi`, and CDI `--device nvidia.com/gpu=all`.
- **gVisor T2:** `runsc` is not installed here, so only the flag is
  unit-tested.
- **Linux idle detection:** only tested with a faked `loginctl` (spike S4).
- **Owner-return `pause` and `throttle`:** the Docker calls are tested, but
  the end-to-end path is tested only for `checkpoint-and-stop`.
- **Rootless engine:** the local engine is rootful, so the agent only warns.

## Dependencies

| Module | Version | License (checked in the module cache) |
|---|---|---|
| `github.com/gorilla/websocket` | v1.5.3 | BSD-2-Clause |
| `gopkg.in/yaml.v3` | v3.0.1 | MIT and Apache-2.0 (dual, per file) |

Everything else is the Go standard library. Both licenses are compatible
with an Apache-2.0 release (plan §14).
