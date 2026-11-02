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
| Python unit, contract, API, security, CLI | `uv run pytest -q protocol cli control-plane` | 96 passed |
| Migrations | upgrade → `alembic check` → downgrade → upgrade on a fresh DB | clean |
| Go agent | `go vet`, `go test ./...`, `go test -tags docker ./sandbox/... ./session/...`, `GOOS=windows go build` | all pass |
| Go fuzzing | `FuzzDecode` 20 s, `FuzzVerifyEnvelope` 15 s | no failures |
| Web UI | `npm run typecheck`, `npm test -- --run` (16), `npm run build`, `npm run licenses` | all pass |
| End to end | `uv run pytest -m e2e tests/e2e` | 10 passed (chaos, 4 real-agent tests, 3 object-store tests with SeaweedFS, 2 Keycloak OIDC tests) |
| Compose | `docker compose -f deploy/compose.yaml up --build` + the README walk-through with a real agent | 6/6 tasks succeeded |
| Compose with object store | `--profile storage`, bucket created by the API, real agent job (3 November 2026) | output uploaded, stored bytes match the hash |
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
| Outputs through the object store | Done | Tested against SeaweedFS 4.50 (e2e and compose `--profile storage` with a real agent). The API creates the bucket; upload URLs use a separate public address and sign size and SHA-256. Without a store, hashes are reported and files stay on the device |
| Inputs through the object store | Not started | Jobs with `data.inputs` are rejected at submit time so they never run without their data |
| Leases, retries | Done | Plus fencing epochs from Phase 2 |
| Audit log (hash-chained, append-only) and "what ran here" | Done | DB trigger blocks UPDATE/DELETE; tamper test |
| Web UI, CLI, compose deployment | Done | OIDC mode of the UI not tried in a browser against Keycloak |
| Exit: training job on a colleague's GPU machine from the CLI | **Blocked here** (no GPU) | CPU equivalent passes from the CLI |
| Exit: invite → enrolled device → first job < 15 min | Done (scripted, about 1 min) | Needs a timed run with a real new user |
| Exit: sandbox tests (host files, LAN, metadata, Windows drives) | Partial | Linux passes; Windows drives need S2 |
| Exit: security gate | Open | Needs a human security reviewer |

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
| Backup copies of slow tasks, credits, per-group caches | Not started | |
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
