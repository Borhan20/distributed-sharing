# Implementation status against the plan

| | |
|---|---|
| **Date** | 2 November 2026 |
| **Plan** | [`plan.md`](../plan.md) (same content as `group_compute_plan.md`) |
| **Design notes** | [`docs/design/phase1-implementation.md`](design/phase1-implementation.md) |

Legend: **Done** = built and covered by automated tests run on 2 November
2026. **Partial** = some of it built, or built but not tested end to end.
**Not started** = nothing built. **Blocked here** = needs hardware or people
that were not available.

> No security-sensitive code has had the human security review the plan
> requires (§8.10). Treat everything below as a draft for that review.

## Verification run (2 November 2026, one Linux machine, Docker 29, PostgreSQL 17)

| Suite | Command | Result |
|---|---|---|
| Python lint | `uv run ruff check protocol control-plane cli scripts tests` | clean |
| Protocol schema drift | `uv run python -m pool_protocol.export --check` | up to date |
| Python unit, contract, API, security, CLI | `uv run pytest -q protocol cli control-plane` | 110 passed |
| Migrations | upgrade → `alembic check` → downgrade → upgrade on a fresh DB | clean |
| Go agent | `go vet`, `go test ./...`, `go test -tags docker ./sandbox/... ./session/...`, `GOOS=windows go build` | all pass |
| Go fuzzing | `FuzzDecode` 20 s, `FuzzVerifyEnvelope` 15 s | no failures |
| Web UI | `npm run typecheck`, `npm test -- --run` (20), `npm run build`, `npm run licenses` | all pass |
| End to end | `uv run pytest -m e2e tests/e2e` | 10 passed (chaos, 4 real-agent tests, 3 object-store tests with SeaweedFS incl. downloads, 2 Keycloak OIDC tests) |
| Compose | `docker compose -f deploy/compose.yaml up --build` + the README walk-through with a real agent | 6/6 tasks succeeded |
| Compose with object store | `--profile storage`, bucket created by the API, real agent job (3 November 2026) | output uploaded, stored bytes match the hash |
| One-command setup | `scripts/dev-up.sh` on a fresh agent home and on an existing setup (3 November 2026) | default web job 3/3, download works, waiting job explains itself |
| License check | `scripts/check_licenses.py`, `npm run licenses` | no copyleft or source-available deps |

## Phase 0: Foundations and spikes

| Item | Status | Where / notes |
|---|---|---|
| Interviews (5–8 researchers), hardware inventory | **Blocked here** (needs people) | Templates: `docs/research/` |
| Threat model v1 | Done, sign-off pending | `security/threat-model.md` |
| ADRs D1–D9 | Done (proposed for review) | `docs/adr/0001`–`0009`; deviations in `0010` |
| Repository, CI, CODEOWNERS | Done | `.github/workflows/ci.yml`, `.github/CODEOWNERS` (replace `ORG` placeholders) |
| Protocol schema skeleton | Done | `protocol/` (Pydantic → JSON Schema, shared fixtures, Go contract tests) |
| S1 gVisor `nvproxy` on our GPUs | **Blocked here** (no GPU, no `runsc`) | |
| S2 WSL2 isolation + escape attempts | **Blocked here** (no Windows host) | Checklist: `security/sandbox-tests.md` |
| S3 gVisor inside WSL2 | **Blocked here** | |
| S4 idle detection | Partial | Linux `loginctl` reader built, not validated on desktops; Windows helper not built |
| S5 Headscale per-job tags | Not started (Phase 3) | |
| S6 PostgreSQL queue throughput | Done | `docs/spikes/S6-postgres-queue.md` (~47 lifecycles/s, 9 ms claims at 20k tasks, 0 double claims) |
| S7 HTCondor backend (optional) | Not started | |
| Exit: Windows tier decided, target scale set | Open | Needs S2/S3 and the interviews |

## Phase 1: Borrow a bigger machine (`single`)

| Item | Status | Notes |
|---|---|---|
| OIDC login, Keycloak bundled | Done | Verified against Keycloak 26.0 with `deploy/keycloak/pool-realm.json`; dev IdP for local use only |
| Groups, roles, invites, join requests (3 approval rules), removal | Done | `test_groups.py`, `test_scheduling.py` |
| MFA for coordinators/admins | Done (claim check) | Uses the `amr` claim; confirm Keycloak sets it for your MFA flow |
| Device enrollment (one-time code, short-lived certs) | Done, deviates | Built-in Ed25519 CA instead of step-ca, plus a certificate-bound challenge (X1) |
| Go agent on Linux | Done | Docker CLI runtime (X7) |
| Go agent on Windows/WSL2 | Partial | Compiles; **untested** (X6) |
| Capability reports, local policy, kill switch | Done | Kill switch tested end to end |
| T1 hardened containers | Done | Escape suite + probe from inside a real job |
| T2 gVisor | Partial | Flag path built; **untested** (no `runsc` here) |
| GPU (CDI) | Partial | Flag path built; **untested** (no GPU) |
| Log streaming | Done (polling) | Logs stored in PostgreSQL (X3) |
| Outputs through the object store | Done, with downloads | Tested against SeaweedFS 4.50 (e2e and compose `--profile storage` with a real agent). The API creates the bucket; upload URLs use a separate public address and sign size and SHA-256. Without a store, hashes are reported and files stay on the device |
| Inputs through the object store | Not started | Jobs with `data.inputs` are rejected at submit time so they never run without their data |
| Leases, retries | Done | Plus fencing epochs from Phase 2 |
| Audit log (hash-chained, append-only) and "what ran here" | Done | DB trigger blocks UPDATE/DELETE; tamper test |
| Web UI, CLI, compose deployment | Done | OIDC mode of the UI not tried in a browser against Keycloak |
| Exit: training job on a colleague's GPU machine from the CLI | **Blocked here** (no GPU) | CPU equivalent passes from the CLI |
| Exit: invite → enrolled device → first job < 15 min | Done (scripted, about 1 min) | Needs a timed run with a real new user |
| Exit: sandbox tests (host files, LAN, metadata, Windows drives) | Partial | Linux passes; Windows drives need S2 |
| Exit: security gate | Open | Needs a human security reviewer |

## Easy onboarding, step 0 (`docs/design/easy-onboarding.md`)

| Item | Status | Notes |
|---|---|---|
| Runnable default job in the web form | Done | Same pinned busybox image as `examples/hello-array.yaml` (web test keeps them in sync) |
| Devices page: working commands and all steps | Done | `--insecure-http` only for localhost; warns when the server is plain http elsewhere |
| One-step sharing | Done | `pool-agent share GROUP` / `unshare`; the server records the share from the agent's policy when the owner is a member. Web "Turn off" stays off. **Needs security review** |
| Why a job is waiting | Done | `GET /api/v1/jobs/{id}/waiting`, shown on the job page |
| Download outputs | Done | Job page button, `pool download` (checks SHA-256), `GET /api/v1/jobs/{id}/tasks/{n}/download` |
| User guide | Done | `docs/user-guide.md`; in-app guide by role at `/guide` |
| One-command local setup | Done | `scripts/dev-up.sh` / `scripts/dev-down.sh` |

## Easy onboarding, step 1: one-command server install

| Item | Status | Notes |
|---|---|---|
| `deploy/install.sh` (Ubuntu/Debian) | Done | Two questions; installs Docker; generates every secret; upgrade by re-running. Fresh Ubuntu 24.04 VM: 4 min 23 s |
| One HTTPS address (Caddy) | Done | Sign-in under `/idp`, files under `/pool-artifacts`; Keycloak admin console, master realm and internal ports not public |
| Let's Encrypt with a domain | **Untested** | Needs a public server; the VM used Caddy's internal CA |
| Real sign-in, registration, first admin | Done | One-time password, forced change; only server admins create groups |
| Two-step sign-in for coordinators and admins | Done | Authenticator-app codes; Keycloak `amr` (`deploy/keycloak/configure.py`); web banner. **Needs security review** |
| Web UI learns its sign-in mode at runtime | Done | `GET /api/v1/config` |
| Agent uploads trust a pinned server CA | Done | Needed for servers with a private CA |
| Acceptance test | Done | `tests/vm/server_acceptance.py` (browser sign-in with TOTP, invite, agent job, download); run book in `tests/vm/README.md` |

## Deleting and removing (decisions of 3 November 2026)

| Item | Status | Notes |
|---|---|---|
| Delete a job | Done | Submitter, or coordinator/admin with two-step sign-in. Running work stops; tasks, logs and files (not shared with other jobs) deleted |
| Delete a group, restore for 7 days, purge | Done | Coordinator; hidden at once; scheduler purges after 7 days, including stored files (tested against SeaweedFS) |
| Kept after deletion | Done | Audit log; owners' "what ran here" (`run_history`); usage records |
| Remove a computer from a group / let it back in | Done | Coordinators and admins; the owner cannot override |
| Server admins revoke any computer | Done | Lost or stolen computers; two-step sign-in |

## Private images (decision of 3 November 2026)

| Item | Status | Notes |
|---|---|---|
| Group registry tokens (read-only), encrypted at rest | Done | Images tab, `pool registry`, `PUT /api/v1/groups/{name}/registries`; checked with the registry like `docker login` |
| Short-lived per-image pull tokens for agents | Done | Registry token protocol (GHCR, Docker Hub); offer flag `registry_auth`, messages `pull_auth_request`/`pull_auth`; audited |
| No cross-group use of cached private images | Done | Agent's `private-images.json` and an anonymous registry check |
| Tested | Done | Unit tests with a fake token registry; e2e with a real CNCF Distribution registry in token mode. **Not tested against ghcr.io or Docker Hub themselves** (needs real accounts) |

## Simple jobs and results (decisions of 3 November 2026)

| Item | Status | Notes |
|---|---|---|
| Images by tag | Done | The server pins `name:tag` to `name:tag@sha256:...` at submit time (manifest HEAD, which Docker Hub does not count as a pull: checked with its rate-limit headers). Computers still only get digests. Checked against Docker Hub and GHCR |
| Short job files | Done | `name, image, command, parts, sweep, cpu, memory, disk, gpu, time_limit, ...`; the full form still works. Plain-language errors that name the field |
| Split into parts, `parts: auto` | Done | `{{index}}`/`{{count}}` and `POOL_TASK_INDEX`/`POOL_TASK_COUNT` in every task; auto counts the places on available (else shared) computers that fit the task |
| Optional command | Done | Empty: the image's own ENTRYPOINT/CMD, same hardening |
| Submit form in the browser | Done | Once / Split into parts (Auto) / For each value, sizes, "Show as job file"; "Run again with changes" on every job page |
| My images (personal registry tokens) | Done | Used only for the submitter's own jobs, before the group's token. **Needs security review** |
| Pull tokens checked before use | Done | The server checks that a pull token really reads the image (closes a gap: a token with no access plus a locally cached image) |
| Registry address guard | Done | Only public addresses unless `POOL_REGISTRY_PRIVATE_NETWORKS`; never link-local/metadata; same-host redirects only. DNS rebinding not covered. **Needs security review** |
| Results in one place | Done | Job page and `GET /jobs/{id}/results.zip` (streamed: files, logs, `tasks.csv`, `results.csv`, merged CSVs), results table and CSV, log download; `pool download --all`, `pool results`. **Needs security review** |
| Examples for every kind of work | Done | `examples/`: random runs, sweep, many files, test suite; each run under the agent's container restrictions (3 November 2026); all parse in CI |
| Tested | Done | Unit and API tests (fake registry, fake file store), Go tests incl. Docker, web tests, e2e with a real agent and SeaweedFS (`parts: auto`, env vars, zip, CLI) |

## `pool run`, step 1: programs without ports (docs/design/pool-run.md, 4 November 2026)

| Item | Status | Notes |
|---|---|---|
| `pool run` with `docker run`'s flags | Done | `-e`, `--env-file`, `--cpus`, `-m`, `--disk`, `--gpus`, `-w`, `--entrypoint`, `-d`; refused flags say why; `-p` is refused until step 2 (apps); `-it` is accepted with a note (no terminal yet) |
| Build on your computer, push to the built-in registry | Done | CNCF Distribution 2.8.3 in token mode, sign-in through `/api/v1/registry/token` with the member's pool token; `<group>/<user>/...` only; a code change uploads only the changed layer (measured in e2e) |
| Computers pull with a server-signed token for one image | Done | Also the second check that the image is the submitter's own |
| `args` and `workdir` in jobs | Done | `args` go to the image's ENTRYPOINT like `docker run IMAGE ARGS` (protocol, agent, CLI) |
| `pool run --local` | Done | The same rules as the agent, from one list (`pool_protocol/sandbox.py` → `fixtures/sandbox_hardening.json`, tested in Go and Python); hints from OS error texts |
| `pool attach` for any job | Done | Live output (one task) or progress, Ctrl-C stops, files into `./pool-out` |
| `pool.yaml` | Done | A job file plus `build` and `platform` |
| Registry clean-up | Done | 30 days unused (`POOL_REGISTRY_RETENTION_DAYS`, per group on the Images tab); images of groups that no longer exist. Never an image a waiting or running job needs, or one used during the scan. Runs apart from the scheduler's main loop. Blob garbage collection is an admin command (admin guide) |
| Registry container | Done | Sees only its certificate (own volume), no capabilities, read-only image (also in the e2e test). Computers' pull tokens last 30 minutes, members' 5 |
| Examples | Done | `examples/run/`: 9 kinds of work (training, machine learning, image processing, data report, test suite with JUnit, text processing, statistics, CPU-heavy computation, simulation) in Python, Node.js, Java, R, Go and C++. Each runs with plain `pool run` (no settings) and was run with `pool run --local`; the split ones were also run as 3 parts with the same container rules |
| Agent skill | Done | `skills/pool`: an Agent Skill (agentskills.io format) for Claude Code, Codex and Gemini CLI. Unit tests check its format and that every flag and command it names exists; an e2e test runs its workflow (check, `pool run -d`, wait, attach) |
| Tested | Done | API tests with a registry fake that checks token signatures and access; CLI tests with a fake computer; e2e with the real registry, agent and SeaweedFS (build, push, layer reuse, refused push into another member's space, real delete, `--local`, Ctrl-C stops the container within 10 s, attach). **Needs security review** (registry sign-in and pull tokens) |
| Not yet | – | Sizes chosen by pool (design accepted: `docs/design/pool-decides.md`); ports and the tunnel (step 2), Compose (step 3), replicas (step 4); matching computers to the image's platform; registry storage in SeaweedFS (a volume for now); a storage quota per member; a push-only credential for Docker instead of the member's pool token |

## Ahead of plan (Phase 2 and later pieces already built)

| Item | Status | Notes |
|---|---|---|
| Array jobs from a matrix | Done | `pool sweep --set seed=0..9` |
| Fencing epochs, idempotent commits | Done | Chaos test checks invariants after every run |
| Retries with backoff, always-failing detection (2 devices) | Done | Lost leases requeue without backoff |
| Checkpoint SDK | Partial | `pool.checkpoint` works; the agent does not upload or mount checkpoints yet |
| Reliability score | Partial | Simple moving score; not used for matching yet |
| Matching: hard filters, owner's group priority, fair share | Done | Speed, locality and preemption-risk scoring not built |
| Time windows, owner-return actions | Done in agent | Pause/throttle tested in the sandbox suite, not end to end |
| Devices in several groups / several servers | Done | Agent profiles, `run --all` |
| Chaos test (agent kills, network cuts, server restarts) | Done | Protocol-level agents; plus a real-agent SIGKILL test |
| Group expiry and templates | Partial | Expiry stops jobs; data deletion after retention not built |
| Data classes | Partial | `confidential` → org-owned dedicated device and T2+ only |
| Backup copies of slow tasks, credits, per-group caches | Not started | Designs: credits in `docs/design/credits.md` (proposal), slow-task copies in `docs/design/pool-decides.md` §4.6 (accepted) |
| Secrets, services, overlay network, Kata, T0, gang jobs | Not started (Phases 3–4) | Rejected at submit time with a clear message |
| Fuzzing | Started | Two Go fuzz targets; CI runs them briefly |
| Signed releases, SBOM, pen test | Not started (Phase 5) | |

## Known limits and follow-ups

1. **Security review** of `control-plane/pool_cp/{auth,authz,audit,ca}`, `agent_ws.py`, `agent/{sandbox,policy,enroll}`, `protocol/`.
2. **Log volume per attempt is not capped.** A misbehaving agent could fill the database (max 64 KiB per message, 200 messages/s). Add a per-attempt cap.
3. **Audit chain lock** limits throughput to about 47 task lifecycles/s on one process (S6). Options are in the spike report.
4. **Cancel push** only reaches agents connected to the same API process. Correctness does not depend on it (heartbeats mark the attempt stale within 15 s), but multi-process deployments should use `LISTEN/NOTIFY`.
5. **step-ca backend** for the device CA (X1), the **Docker Go SDK** once on Go 1.24 (X7).
6. **Plan open questions** §12 (target scale, GPUs, Windows-native jobs, owner visibility, retention defaults, university server) are still open. The "what ran here" view currently shows image and command (open question 4).
