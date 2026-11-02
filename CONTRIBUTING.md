# Contributing to pool

Thank you for helping. This file explains how to set up, test and submit
changes. Please also read the [code of conduct](CODE_OF_CONDUCT.md) and the
[security policy](SECURITY.md).

## Repository layout

See [`docs/design/phase1-implementation.md` §1](docs/design/phase1-implementation.md#1-repository-layout-plan-15).
In short: `control-plane/` (FastAPI), `protocol/` (shared models), `cli/`
(Typer CLI and SDK), `agent/` (Go agent), `web/` (React UI), `deploy/`,
`docs/`, `security/`, `tests/e2e/`.

## Development setup

You need:

- Python 3.12 and [uv](https://docs.astral.sh/uv/)
- Go 1.23 or newer
- Node.js 22 and npm (for the web UI)
- Docker Engine or Podman (for PostgreSQL, sandbox tests and end-to-end tests).
  Do not use or require Docker Desktop (plan §14).

Python is a uv workspace. From the repository root:

```bash
uv sync --all-packages --extra s3     # creates .venv with all Python packages
```

Start a throwaway PostgreSQL for tests (bound to localhost only):

```bash
docker run -d --name pool-test-pg -p 127.0.0.1:55432:5432 \
  -e POSTGRES_HOST_AUTH_METHOD=trust -e POSTGRES_DB=pool postgres:17-alpine
```

Go agent:

```bash
cd agent && go build ./... && go test ./...
```

Web UI:

```bash
cd web && npm ci && npm run dev
```

Check each component's README for the exact environment variables. Never
commit real credentials; use the `.env.example` placeholders.

## Tests

Nothing is "done" until its tests pass in CI (plan §10).

| What | Command |
|---|---|
| Python unit and integration | `uv run pytest -q protocol control-plane cli` |
| Protocol contract (Python) | `uv run pytest -q protocol` |
| Protocol schema up to date | `uv run python -m pool_protocol.export --check` |
| Go unit and contract | `cd agent && go test ./...` |
| Sandbox escape suite | `cd agent && go test -tags docker ./sandbox/...` |
| End-to-end and chaos | `uv run pytest -q -m e2e tests/e2e` |
| Lint | `uv run ruff check .` and `cd agent && go vet ./...` |

If you change the protocol, regenerate the schema and fixtures with
`uv run python -m pool_protocol.export`, and make sure both the Python and
Go contract tests pass.

Use synthetic data in tests, fixtures and examples. No real names, e-mail
addresses, hostnames, keys or tokens.

## Pull requests

- Keep each PR small and focused. Explain *why* in the description.
- Fill in the PR template, including the security section.
- Add or update tests. Say how you verified the change.
- Add an ADR in `docs/adr/` for any decision that changes the architecture.
- New dependencies: state the license. The CI license check must pass, and
  copyleft or source-available licenses need maintainer approval (plan §14).

### Security review rule

Changes to these paths need approval from a security reviewer before merge
(plan §8.10, enforced by `.github/CODEOWNERS`):

`control-plane/pool_cp/auth/`, `authz/`, `audit/`, `ca/`, `secrets/`,
`agent/sandbox/`, `agent/policy/`, `agent/enroll/`, `protocol/`.

Also ask for a security review for anything touching login, crypto, input
parsing, access control or data deletion elsewhere. Use the checklist in
[`security/review-checklist.md`](security/review-checklist.md).

## Sign-off (DCO) or CLA

**Team to decide.** Until the team chooses between a Developer Certificate
of Origin sign-off (`git commit -s`) and a contributor license agreement,
no sign-off is required. This section will be updated before the public
release.

## Licensing

By contributing, you agree that your contribution is licensed under the
Apache License 2.0 (see [LICENSE](LICENSE)).
