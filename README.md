# pool: share computers within a trusted group

pool lets a group of people (a lab, a class, a team) run jobs on each other's
computers. Someone with a big job sends it to the group; computers that
members share run it in locked-down containers; results come back. Every
computer's owner decides what to share and can stop everything at once.

> **Status: early, not production-ready.** Phase 0 and a first Phase 1 slice of
> the [plan](plan.md) are built and tested. Nothing has had the human security
> review the plan requires (plan §8.10). [`docs/STATUS.md`](docs/STATUS.md)
> lists exactly what is built, tested and missing.

## Try it on your computer

You need **Docker**, **Go 1.23 or newer**, `curl` and `python3` on Linux (macOS is untested).

```bash
git clone https://github.com/Borhan20/distributed-sharing.git
cd distributed-sharing
scripts/dev-up.sh
```

The script:

1. creates local settings with random passwords (`deploy/.env`, never committed);
2. starts the server and a file store in Docker;
3. builds the agent, connects this computer to your account, shares it with a group called `my-lab`, and starts it.

Then open <http://127.0.0.1:8000>, sign in with your user name (dev mode has
no password), open **my-lab → Jobs → Submit a job**, and submit the example
job. It runs on your own computer within seconds.

| | |
|---|---|
| Use another name or group | `scripts/dev-up.sh --user alice --group ml-test-lab` |
| Server only, no sharing | `scripts/dev-up.sh --no-share` |
| Stop all work on this computer now | `agent/bin/pool-agent kill` (and `pool-agent resume`) |
| Stop everything (data is kept) | `scripts/dev-down.sh` |
| Read the guide | <http://127.0.0.1:8000/guide> |

Dev mode lets anyone sign in as anyone. It only listens on `127.0.0.1` and
must never be used on a shared server.

## Use it with other people

One person installs the server on a Linux machine that stays on, ideally with a
domain name pointing to it ([admin guide](docs/admin-guide.md)):

```bash
wget -qO install.sh https://raw.githubusercontent.com/Borhan20/distributed-sharing/main/deploy/install.sh
sudo bash install.sh        # asks for the address and your email; about 5 minutes
```

It sets up HTTPS (automatic certificates with a domain name), sign-in with
registration and two-step sign-in, and the file store, all on one address.

- **Running jobs:** members only need a browser: invite link → register → join.
- **Sharing a computer:** members install the agent and follow **My computers** in the web UI. That is four commands for now; installers with click-to-approve pairing are next ([design](docs/design/easy-onboarding.md)).

## Documentation

| For | Read |
|---|---|
| Everyone using pool | [User guide](docs/user-guide.md), from joining a group to running a parameter sweep, with examples. The same guide by role is in the web UI under **Guide**. |
| People sharing a computer | [`agent/README.md`](agent/README.md): what the agent does on your computer and how to control it |
| People using an AI coding agent | [`skills/`](skills/README.md): a skill that teaches Claude Code, Codex or Gemini CLI to run jobs on pool properly |
| Server admins | [Admin guide](docs/admin-guide.md): install, HTTPS, upgrades, backups, security notes |
| Developers | [Design notes](docs/design/phase1-implementation.md), [ADRs](docs/adr/), [status](docs/STATUS.md), [plan](plan.md) |

## How it works

```
 Browser / CLI ──HTTPS──▶  Server: API + scheduler + PostgreSQL (+ file store, + Keycloak)
                                      ▲
                     outbound only    │  signed tasks, results
                                      │
                 Shared computers: pool-agent ── runs each task in a locked-down container
```

- Computers only connect **out** to the server; they need no open ports.
- Each task runs in a container with no network, no access to the owner's files, a read-only system, and only the CPU, memory and disk the owner allows.
- The owner's local settings always win; the server can never widen them.

## Manual setup (advanced)

What `scripts/dev-up.sh` does, step by step, plus the `pool` command-line tool
(needs [uv](https://docs.astral.sh/uv/)):

```bash
# 1. Server in dev mode
cp deploy/.env.example deploy/.env          # then set POSTGRES_PASSWORD
docker compose -f deploy/compose.yaml up -d --build
# add --profile storage and the POOL_S3_* settings (see deploy/.env.example) for the file store

# 2. Command-line tool
uv sync --all-packages
uv run pool login --server http://127.0.0.1:8000 --dev-user alice
uv run pool groups create research-team --template research-team
uv run pool devices code                    # one-time code for the next step

# 3. Agent on the computer that shares
cd agent && CGO_ENABLED=0 go build -o bin/pool-agent ./cmd/pool-agent
./bin/pool-agent enroll --server http://127.0.0.1:8000 --code ABCDE-FGHJK --insecure-http
./bin/pool-agent share research-team        # also lists the computer in the group
./bin/pool-agent run

# 4. Submit a job, or run a program of your own like `docker run` (builds its Dockerfile)
uv run pool submit examples/hello-array.yaml --wait
uv run pool logs <job-id> --task 0
cd examples/run/go-primes && uv run pool run     # output here, files in ./pool-out
```

`--insecure-http` is accepted only for `localhost` and `127.0.0.1`. A shared
server needs TLS in front of the API and OIDC sign-in
(`deploy/compose.oidc.yaml`).

## Develop and test

```bash
docker run -d --name pool-test-pg -p 127.0.0.1:55432:5432 \
  -e POSTGRES_HOST_AUTH_METHOD=trust -e POSTGRES_DB=pool postgres:17-alpine
uv sync --all-packages --extra s3
uv run pytest -q protocol cli control-plane          # unit, contract, API, security tests
uv run pytest -m e2e -s tests/e2e                    # chaos, real agent and object store end to end
cd agent && go test ./... && go test -tags docker ./sandbox/...
cd web && npm ci && npm run typecheck && npm test -- --run && npm run build
uv run python tests/perf/s6_queue_bench.py           # spike S6
```

CI runs the same steps (`.github/workflows/ci.yml`). Layout: `control-plane/`
(API, scheduler, migrations), `protocol/` (wire protocol and job spec),
`agent/` (Go agent), `cli/` (`pool` CLI and SDK), `web/` (React UI),
`deploy/`, `docs/`, `security/`, `tests/`.

## License

Apache-2.0 (plan D8). See [`LICENSE`](LICENSE) and the dependency notes in
plan §14; `scripts/check_licenses.py` and `web/scripts/licenses.mjs` check
dependencies in CI.
