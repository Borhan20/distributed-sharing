# CLAUDE.md

Guidance for AI coding agents (Claude Code and others) working in this repository. Keep it short and true:
update it in the same change when a command, convention or layout below changes.

## What this is

**pool** is a group compute platform. Members share their computers with a group. Others run Docker
containers on them like `docker run`, and pool decides the sizes, picks the computer and gathers the results.

- The plan: [`plan.md`](plan.md).
- What is built: [`docs/STATUS.md`](docs/STATUS.md). This is the source of truth; read it before you
  suggest next steps.
- Designs, with "as built" sections: [`docs/design/`](docs/design/), in particular `pool-run.md`,
  `pool-decides.md` and `credits.md`.

## Layout

| Folder | What | Language |
|---|---|---|
| `protocol/` | Shared message and job file models, JSON schema, fixtures | Python (pydantic) |
| `control-plane/` | API server, scheduler, credits, registry reader | Python (FastAPI, SQLAlchemy async, alembic) |
| `cli/` | The `pool` command | Python (typer) |
| `agent/` | `pool-agent`: runs jobs in Docker on a member's computer | Go |
| `web/` | Web UI | React + TypeScript (Vite) |
| `tests/e2e/` | End-to-end tests with a real agent and Docker | Python |
| `skills/pool/` | Public agent skill for using pool | Markdown |
| `examples/` | Example apps and job files | mixed |
| `docs/`, `security/` | User, admin and design docs; threat model and review checklist | Markdown |

## Commands

Set up once: `uv sync --all-packages --extra s3`.

Tests need a throwaway PostgreSQL on 127.0.0.1:55432. Check that it is running before you run tests:

```bash
docker run -d --name pool-test-pg -p 127.0.0.1:55432:5432 \
  -e POSTGRES_HOST_AUTH_METHOD=trust -e POSTGRES_DB=pool postgres:17-alpine
```

| What | Command |
|---|---|
| Python lint | `uv run ruff check protocol control-plane cli scripts tests` |
| Python tests | `uv run pytest -q protocol cli control-plane` |
| Protocol schema up to date | `uv run python -m pool_protocol.export --check` (regenerate without `--check`) |
| Go | `cd agent && go vet ./... && go test ./...` |
| Agent binary | `cd agent && CGO_ENABLED=0 go build -o bin/pool-agent ./cmd/pool-agent` (static: apps need it; e2e uses it) |
| End to end | `uv run pytest -q -m e2e tests/e2e` (needs Docker and the agent binary) |
| Web | `cd web && npm run typecheck && npx vitest run` (`npm test` watches and never exits) |
| Web API types | export `openapi.json` from `create_app().openapi()`, then `npm run gen:api` (see `web/README.md`) |
| Migrations | in `control-plane/`: `python -m pool_cp.migrate`, `alembic check`, `alembic downgrade base`, migrate again |

The repository does not use `ruff format`. Lines are at most 120 characters.

## Rules for changes

- **Protocol changes** touch both sides. Edit `protocol/pool_protocol/`, regenerate the schema and fixtures
  with the export command, update the Go types in `agent/protocol/types.go`, and run the Python and Go
  contract tests.
- **Database changes** need a new alembic migration in `control-plane/pool_cp/migrations/versions/`
  (numbered, e.g. `0011_apps.py`). CI checks that the migrations match the models.
- **Security-sensitive code** (auth, access control, crypto, sandbox, policy, enrolment, protocol, tunnels,
  parsing of untrusted files) carries a `SECURITY-SENSITIVE` comment at the top. It needs a human security
  review before merge (plan §8.10, `.github/CODEOWNERS`, `security/review-checklist.md`). Each feature step
  also gets an independent security review. Fixes from that review are listed in the design doc under
  "Changed after the security review".
- **Untrusted input:** task outputs, agent messages, image manifests and anything from a member's computer.
  Cap sizes and time, never let one bad file break a whole response, and escape text before it goes into
  Markdown or a terminal.
- **Tests:** add or update them with every change. Control-plane tests must not reach the internet (conftest
  refuses public registry hosts). Use synthetic data only: no real names, e-mails, hosts, keys or tokens.
- **Dependencies:** state the license; `scripts/check_licenses.py` must pass. No copyleft or
  source-available licenses without maintainer approval.
- **Docs:** write in simple English: short sentences, headings, numbered steps and tables. Update the user
  guide, `docs/STATUS.md` and the design doc's "as built" section in the same change as the code.

## Git

- Work on a feature branch and open a PR against `main`, then check that CI passes.
- Feature work is stacked: each step's branch starts from the previous one, and its PR uses that branch as
  base.
- Never force-push, and never commit `.serena/`, `deploy/.env` or `web/node_modules/`.
