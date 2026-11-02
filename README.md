# pool: group compute platform

Trusted people pool their Linux and Windows computers and run jobs on each
other's machines. The device owner stays in control. See [`plan.md`](plan.md)
for the R&D plan (`group_compute_plan.md` has the same content, reformatted).

> **Status: Phase 0 done and a first Phase 1 slice, not production-ready.**
> Nothing here has had the human security review the plan requires
> (plan §8.10). See [`docs/STATUS.md`](docs/STATUS.md) for exactly what is
> built, tested and still missing.

## Layout (plan §15)

| Path | What |
|---|---|
| `control-plane/` | FastAPI API server, scheduler worker, Alembic migrations (`pool_cp`) |
| `protocol/` | Agent protocol v1alpha1 and job spec (Pydantic), JSON Schema, contract fixtures |
| `agent/` | Go agent for Linux (and Windows/WSL2, untested): enrollment, policy, sandbox, kill switch |
| `cli/` | `pool` CLI and the Python SDK (`pool.checkpoint`) |
| `web/` | React + TypeScript UI |
| `deploy/` | Dockerfile and docker compose for one server |
| `docs/` | ADRs, design notes, spike reports, status |
| `security/` | Threat model v1, review checklists, sandbox test notes |
| `tests/` | End-to-end, chaos and performance tests |

## Try it on one machine

Needs Docker, [uv](https://docs.astral.sh/uv/), Go 1.23 and Node 22.

```bash
# 1. Control plane in DEV MODE (built-in login without passwords; never on a shared server)
cp deploy/.env.example deploy/.env          # set POSTGRES_PASSWORD
docker compose -f deploy/compose.yaml up -d --build
open http://127.0.0.1:8000                  # web UI

# 2. CLI
uv sync --all-packages
uv run pool login --server http://127.0.0.1:8000 --dev-user alice
uv run pool groups create research-team --template research-team
uv run pool devices code                    # prints a one-time enrollment code

# 3. Agent on the machine that shares its compute
cd agent && go build -o bin/pool-agent ./cmd/pool-agent
./bin/pool-agent enroll --server http://127.0.0.1:8000 --code ABCDE-FGHJK --insecure-http
#    edit the policy file the agent printed (it shares nothing until you add a group), then:
uv run pool devices share <device-id> research-team
./bin/pool-agent run

# 4. Submit a job (images must be pinned by digest)
uv run pool submit examples/hello-array.yaml --wait
uv run pool logs <job-id> --task 0
```

`--insecure-http` is only accepted for `localhost`/`127.0.0.1`. A shared
server needs TLS in front of the API and OIDC login
(`deploy/compose.oidc.yaml`).

## Develop and test

```bash
docker run -d --name pool-test-pg -p 127.0.0.1:55432:5432 \
  -e POSTGRES_HOST_AUTH_METHOD=trust -e POSTGRES_DB=pool postgres:17-alpine
uv sync --all-packages --extra s3
uv run pytest -q protocol cli control-plane          # unit, contract, API, security tests
uv run pytest -m e2e -s tests/e2e                    # chaos + real agent end to end
cd agent && go test ./... && go test -tags docker ./sandbox/...
cd web && npm ci && npm run typecheck && npm test -- --run && npm run build
uv run python tests/perf/s6_queue_bench.py           # spike S6
```

CI runs the same steps (`.github/workflows/ci.yml`).

## License

Apache-2.0 (plan D8). See [`LICENSE`](LICENSE) and the dependency notes in
plan §14; `scripts/check_licenses.py` and `web/scripts/licenses.mjs` check
dependencies in CI.
