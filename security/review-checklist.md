# Security review checklists

Two checklists: one for every pull request that touches a security-sensitive
path, and one for the security gate at the end of each phase (plan §8.10).

## 1. Security-sensitive paths

A security reviewer must approve changes to these paths. `.github/CODEOWNERS`
enforces it once the team names are filled in.

| Plan §8.10 path | Repository path |
|---|---|
| `control-plane/auth` | `control-plane/pool_cp/auth/` |
| `control-plane/authz` | `control-plane/pool_cp/authz/` |
| `control-plane/secrets` | `control-plane/pool_cp/secrets/` (not built yet) |
| `control-plane/audit` | `control-plane/pool_cp/audit/` |
| device CA (added in slice 1, X1) | `control-plane/pool_cp/ca/` |
| `agent/sandbox` | `agent/sandbox/` |
| `agent/policy` | `agent/policy/` |
| `agent/enroll` | `agent/enroll/` |
| `protocol/` | `protocol/` |

Also ask for a security reviewer when a change touches data deletion,
crypto, input parsing, or access control anywhere else.

## 2. Per-PR checklist

Copy the relevant parts into the PR description.

### General

- [ ] The PR says which threat-model items (`security/threat-model.md`) it affects.
- [ ] No secrets, keys, tokens, connection strings or real personal data in code, tests, fixtures or logs. Test data is synthetic.
- [ ] New dependencies list their license, and the CI license check passes.
- [ ] Tests cover the failure path, not only the happy path.

### Authentication and identity (`auth/`, `ca/`, `agent/enroll/`)

- [ ] Tokens: signature, issuer, audience, expiry and `nbf` all checked; no `alg: none`; algorithm allowlist fixed in code.
- [ ] Dev-mode code paths cannot be reached unless dev mode is explicitly on, and dev mode is off in every shipped config.
- [ ] One-time codes and invite tokens are stored hashed, single-use where required, time-limited, and compared in constant time.
- [ ] Certificates: issuer checked, validity window checked, deny list checked, subject bound to the device id.
- [ ] Private keys are created on the device, never sent, and written with mode 0600 (or the OS key store).
- [ ] Any header that carries client-certificate details is stripped from incoming requests by the proxy.

### Authorization (`authz/`)

- [ ] Every new endpoint checks group membership **and** role before reading or writing.
- [ ] Object ids from the URL are checked against the caller's group (no IDOR).
- [ ] The role table in `docs/design/phase1-implementation.md` §6 is still true, or the PR updates it.
- [ ] Coordinator and admin actions require MFA when it is enabled.
- [ ] An authorization test exists for at least one role that must be denied.

### Audit (`audit/`)

- [ ] Every state change listed in plan §8.9 writes an audit event.
- [ ] The hash chain stays valid (the verify check passes in tests).
- [ ] No secret values or personal data in audit `details`.

### Protocol (`protocol/`)

- [ ] Schema and fixtures regenerated (`uv run python -m pool_protocol.export`), and both Python and Go contract tests pass.
- [ ] New fields are optional, or the protocol version changes.
- [ ] State-changing messages carry an id for idempotency, and messages about an attempt carry its epoch and are fenced on the server.
- [ ] Size limits exist for every string and list that comes from the network.

### Agent policy and sandbox (`agent/policy/`, `agent/sandbox/`)

- [ ] The agent still verifies the envelope signature, `device_id` and expiry **before** applying policy.
- [ ] Local policy is still enforced even if the server asks for something else.
- [ ] Container flags still include: non-root user, `--cap-drop ALL`, `no-new-privileges`, read-only root, `--network none` (unless the egress proxy is in place), pids/memory/CPU limits, no host mounts except the per-attempt output dir.
- [ ] The sandbox escape suite (`go test -tags docker ./sandbox/...`) passes; new escape vectors get a new test.
- [ ] The kill switch still stops every container the agent started.

### Data deletion

- [ ] Deletion needs explicit coordinator confirmation and follows a configured, visible retention rule.
- [ ] Deletion writes an audit event.
- [ ] Nobody can trigger deletion of another group's data.

## 3. End-of-phase security gate

A phase is not done until every item is ticked or has a written exception
signed by the security reviewer.

- [ ] `security/threat-model.md` updated: new components, new threats, status column refreshed.
- [ ] ADRs written for every security-relevant decision made in the phase.
- [ ] All per-PR checklists for the phase completed.
- [ ] Sandbox escape suite passes on Linux in CI.
- [ ] Manual Windows/WSL2 checks (`security/sandbox-tests.md` §3) done on real hardware, if Windows is in scope for the phase.
- [ ] Authorization tests pass (full matrix in Phase 5).
- [ ] Chaos invariants pass (Phase 2 onward): one final state per task, no stale-epoch commit, no lost task, audit chain valid.
- [ ] Dependency license check and vulnerability scan (Trivy) clean, or exceptions recorded.
- [ ] No open critical or high findings.
- [ ] Phase 5 only: external penetration test done and all critical and high findings fixed.

| Phase | Reviewer | Date | Result |
|---|---|---|---|
| 0 | _pending_ | | |
| 1 | _pending_ | | |
