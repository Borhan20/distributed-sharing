# pool user guide

pool lets a group of people (a lab, a class, a team) run jobs on each other's
computers. This guide goes from joining a group to running your own code on
the group's computers, with examples. The web UI has a shorter version of this
guide, organised by role, under **Guide**.

**Contents**

1. [The ideas in two minutes](#1-the-ideas-in-two-minutes)
2. [Join a group](#2-join-a-group)
3. [Your first job, in the browser](#3-your-first-job-in-the-browser)
4. [Example: run your own code as a parameter sweep](#4-example-run-your-own-code-as-a-parameter-sweep)
5. [Example: borrow a bigger machine for one job](#5-example-borrow-a-bigger-machine-for-one-job)
6. [The command line](#6-the-command-line)
7. [Share your computer](#7-share-your-computer)
8. [Run a group (coordinators and admins)](#8-run-a-group-coordinators-and-admins)
9. [When something goes wrong](#9-when-something-goes-wrong)
10. [What is not possible yet](#10-what-is-not-possible-yet)

## 1. The ideas in two minutes

| Word | Meaning |
|---|---|
| **Group** | The people who share computers with each other. Invite-only. |
| **Role** | What you may do in a group: coordinator, admin, member, contributor or submitter ([§8](#roles)). |
| **Computer** (device) | A machine a member shares with the group. It runs the **agent**, a small program that takes work from the server. |
| **Job** | Work you send to the group: a container image, a command, and how much CPU, memory and disk it needs. |
| **Task** | One run of a job's command. A job with a **matrix** (e.g. 10 seeds) becomes many tasks that run in parallel on different computers. |
| **Image** | A container image (Docker) with your code and its dependencies, **pinned by digest** (`...@sha256:...`) so every task runs exactly the same software. |
| **`/out`** | The folder where a task writes result files. You download them afterwards. |

How a job runs:

1. You submit it, in the browser or with the `pool` command.
2. The server splits it into tasks.
3. A shared computer that has room and matches the requirements takes a task and runs it in a locked-down container: no network, no access to the owner's files.
4. The task's log and result files come back to you.
5. If a computer goes away mid-task, the task moves to another computer by itself.

## 2. Join a group

You need the address of your group's server (for example
`https://pool.example.org`) and an account on it. On a local test setup, the
address is `http://127.0.0.1:8000` and you sign in with any name (see the
[README](../README.md)).

There are three ways in:

- **With an invite link** (most common). Someone in the group sends you a link. Open it, sign in, and press **Accept invite**. You get the role chosen in the invite.
- **By asking to join.** On the **Groups** page, use **Ask to join a group** with the group's short name (e.g. `ml-test-lab`) and a short message. The group approves you by its own rule.
- **By starting your own group.** On the **Groups** page, use **Create a group** and pick a template. You become its coordinator.

| Template | Who approves new members | New members on probation | Closes by itself |
|---|---|---|---|
| Research team | the coordinator | no | no |
| Dev team | the coordinator | no | no |
| Student group | any 2 members | 7 days | no |
| Course | the coordinator | no | after about 120 days |
| Hackathon | any 1 member | no | after 2 days |

During **probation**, a new member's jobs run only on computers whose owners
accept work from new members.

A new group has no computers. Jobs wait until at least one member
[shares a computer](#7-share-your-computer).

## 3. Your first job, in the browser

1. Open your group and go to **Jobs → Submit a job**. The form already contains a small example job that runs as-is.
2. Press **Submit**. The job page opens.
3. Watch the tasks: *pending* → *running* → *succeeded*. Click a task to see its log (`running seed 0` …) and its output file `result.txt`, with a **Download** button.
4. If tasks stay *pending*, the box **"tasks waiting: why"** tells you why and what would fix it, for example *"No computer is shared with this group yet"*.

This is the example job, in YAML:

```yaml
apiVersion: pool/v1alpha1
kind: Job
metadata:
  name: seed-sweep
spec:
  shape: array
  # A tiny public image, pinned by digest. Replace it with your own image.
  image: docker.io/library/busybox@sha256:73aaf090f3d85aa34ee199857f03fa3a95c8ede2ffd4cc2cdb5b94e566b11662
  # {{seed}} is replaced by each value below, so this job runs 3 tasks.
  command: ["sh", "-c", "echo running seed {{seed}}; echo 'result for seed {{seed}}' > /out/result.txt"]
  matrix:
    seed: [0, 1, 2]
  resources: { cpu: 0.5, memory: 256Mi, disk: 64Mi }
  retries: 2
  max_runtime: 10m
```

| Field | What it does |
|---|---|
| `metadata.name` | A short name: lowercase letters, digits and dashes. |
| `shape` | `single` for one task, `array` for one task per combination of `matrix` values. |
| `image` | Container image, pinned by digest. |
| `command` | What runs inside the container. `{{name}}` is replaced by a `matrix` value. |
| `matrix` | Values to sweep. Several keys multiply: 3 seeds × 2 datasets = 6 tasks. Up to 10,000 tasks. |
| `resources` | Per task: `cpu` (cores, may be fractional), `memory` and `disk` (e.g. `512Mi`, `4Gi`). Optional `gpu: { count: 1 }`. |
| `retries` | How often a failed task is retried (0–20). A task that fails on 2 different computers is marked failed. |
| `max_runtime` | Time limit per task, e.g. `30m` or `2h`. |
| `isolation.min_tier` | Optional: the minimum isolation level, `T1` (hardened container, default), `T2` or `T3`. |
| `data_class` | Optional: `public`, `internal` (default) or `confidential` (runs only on dedicated organisation computers). |

## 4. Example: run your own code as a parameter sweep

Goal: estimate π with a Monte Carlo simulation for 5 random seeds and 2
sample sizes, which is 10 tasks running in parallel on the group's computers,
then combine the results.

**Step 1. Write the program.** Save it as `pi.py`:

```python
import argparse, json, pathlib, random, time

parser = argparse.ArgumentParser()
parser.add_argument("--seed", type=int, required=True)
parser.add_argument("--samples", type=int, required=True)
args = parser.parse_args()

random.seed(args.seed)
start = time.time()
inside = sum(1 for _ in range(args.samples) if random.random() ** 2 + random.random() ** 2 <= 1)
estimate = 4 * inside / args.samples
result = {"seed": args.seed, "samples": args.samples, "pi": estimate, "seconds": round(time.time() - start, 2)}

print(json.dumps(result))  # visible in the task's log
out = pathlib.Path("/out")
out.mkdir(exist_ok=True)
(out / "result.json").write_text(json.dumps(result))
```

**Step 2. Put it in an image.** Save this as `Dockerfile` next to `pi.py`:

```dockerfile
FROM python:3.12-slim
COPY pi.py /app/pi.py
```

**Step 3. Build it and publish it** to a registry the group's computers can
download from, for example the GitHub container registry or Docker Hub. The
image must be public for now, because computers cannot log in to private
registries yet. Replace `YOUR-NAME`:

```bash
docker build -t ghcr.io/YOUR-NAME/pi-sweep:1 .
docker push ghcr.io/YOUR-NAME/pi-sweep:1
docker inspect --format '{{index .RepoDigests 0}}' ghcr.io/YOUR-NAME/pi-sweep:1
# prints ghcr.io/YOUR-NAME/pi-sweep@sha256:...   <- use this as the image
```

**Step 4. Describe the job.** Save as `pi-sweep.yaml`:

```yaml
apiVersion: pool/v1alpha1
kind: Job
metadata:
  name: pi-sweep
spec:
  shape: array
  image: ghcr.io/YOUR-NAME/pi-sweep@sha256:PASTE-THE-DIGEST
  command: ["python", "/app/pi.py", "--seed", "{{seed}}", "--samples", "{{samples}}"]
  matrix:
    seed: [0, 1, 2, 3, 4]
    samples: [1000000, 10000000]
  resources: { cpu: 1, memory: 512Mi, disk: 256Mi }
  retries: 2
  max_runtime: 30m
```

**Step 5. Submit it.** Either paste the YAML into **Jobs → Submit a job** in
the browser, or use the [command line](#6-the-command-line):

```bash
pool submit pi-sweep.yaml --group my-lab
```

**Step 6. Follow it.** Use the job page in the browser, or:

```bash
pool status JOB-ID              # one line per task: state, attempts, which computer
pool logs JOB-ID --task 3 -f    # follow one task's output
```

**Step 7. Collect the results.**

```bash
for i in $(seq 0 9); do pool download JOB-ID --task "$i" -o "results/task-$i"; done
python3 - <<'EOF'
import json, pathlib, statistics
rows = [json.loads(p.read_text()) for p in pathlib.Path("results").glob("task-*/result.json")]
for n in sorted({r["samples"] for r in rows}):
    vals = [r["pi"] for r in rows if r["samples"] == n]
    print(f"{n:>10} samples: pi ~ {statistics.mean(vals):.5f} (spread {statistics.pstdev(vals):.5f})")
EOF
```

`pool download` checks every file's SHA-256 against what the computer
reported, so a damaged or swapped file is never saved.

**Variations**

- **Try more seeds without editing the file:** `pool sweep pi-sweep.yaml --set seed=0..19 --group my-lab` gives 40 tasks. Add `--dry-run` to see the count first.
- **Long tasks:** set `max_runtime` generously and `retries: 3`. If a computer's owner comes back and stops sharing, the task starts again elsewhere. Saving checkpoints to resume from is planned, not ready yet.

## 5. Example: borrow a bigger machine for one job

A single job that needs more than your laptop has: one task, 8 cores and
16 GiB of memory.

```yaml
apiVersion: pool/v1alpha1
kind: Job
metadata:
  name: big-simulation
spec:
  shape: single
  image: ghcr.io/YOUR-NAME/simulation@sha256:PASTE-THE-DIGEST
  command: ["python", "/app/simulate.py", "--threads", "8"]
  resources: { cpu: 8, memory: 16Gi, disk: 10Gi }
  max_runtime: 6h
```

If no shared computer offers that much, the job page says so, for example
*"This job asks for more than any shared computer offers: needs 16 GiB memory
(most offered: 7 GiB)"*. Ask for less, or ask an owner to raise their limit
(`pool-agent share my-lab --memory 20Gi`).

## 6. The command line

The browser does everything a member needs. The `pool` command is handy for
scripts and for many downloads. For now it runs from a checkout of the
repository with [uv](https://docs.astral.sh/uv/):

```bash
uv sync --all-packages
uv run pool --help
```

The commands below are written as `pool ...`; type `uv run pool ...` until it
is packaged.

**Sign in:**

- **Local dev server:** `pool login --server http://127.0.0.1:8000 --dev-user alice --group my-lab`. `--group` sets the default group for later commands.
- **Real server:** `pool login --server https://pool.example.org --token ACCESS-TOKEN`. Sign-in through the browser for the CLI is planned; until then, ask your server admin how to get a token.

| Task | Command |
|---|---|
| Who am I | `pool whoami` |
| My groups | `pool groups list` |
| Create a group | `pool groups create my-lab --template research-team` |
| Ask to join | `pool join my-lab --message "PhD student in the vision lab"` |
| Accept an invite token | `pool invite accept TOKEN` |
| Submit a job | `pool submit job.yaml [--group G] [--wait]` |
| Sweep from the command line | `pool sweep job.yaml --set seed=0..9 --set dataset=small,large` |
| List jobs | `pool jobs list my-lab [--state running]` |
| Job status | `pool status JOB-ID` |
| Logs | `pool logs JOB-ID --task 0 [-f]` |
| Download results | `pool download JOB-ID [--task N] [--path FILE] [-o FOLDER]` |
| Cancel | `pool cancel JOB-ID` |
| Usage in a group | `pool groups usage my-lab --days 7` |

## 7. Share your computer

Anyone with the role coordinator, admin, member or contributor can share a
computer with a group.

**What the group's jobs can and cannot do on your computer:**

- Each task runs in a container as an unprivileged user, with a read-only system, **no network**, and no access to your files.
- It uses at most the CPU, memory and disk you allow.
- You can stop everything at once, and you can see everything that ran.

**What you need:** Linux with Docker (Windows through WSL2 is built but not yet
tested; Macs are not supported for sharing yet).

**Set it up**, following **My computers** in the browser:

1. **Get the agent.** Installers are planned. For now it is built from source: `cd agent && go build -o bin/pool-agent ./cmd/pool-agent`.
2. **Connect it to your account.** On **My computers**, press **Create code** and run the command shown, for example:
   ```bash
   pool-agent enroll --server https://pool.example.org --code ABCDE-FGHJK --name lab-pc
   ```
3. **Choose a group.** This also lists the computer in the group:
   ```bash
   pool-agent share my-lab
   ```
   A new share allows 50% of the CPU, half of the memory (whole GiB), 20 GiB of disk and no GPU, at any time. To set your own limits, run it again with options; only the options you give change:
   ```bash
   pool-agent share my-lab --cpu-percent 75 --memory 8Gi --disk 50Gi --gpu --only-when-idle 10m
   ```
   `--only-when-idle 10m` runs work only after 10 minutes without keyboard or mouse use.
4. **Start it** and leave it running: `pool-agent run`.

**Control it:**

| I want to… | Do this |
|---|---|
| Stop everything right now | `pool-agent kill`, then `pool-agent resume` to allow work again |
| Stop sharing with one group | `pool-agent unshare my-lab`, or **Turn off** on the computer's page in the browser |
| See what is running and who sent it | `pool-agent status` |
| See everything that ran here | the computer's page in the browser, section **What ran here** |
| Change limits | `pool-agent share my-lab --memory 4Gi` |

The settings live in a file on your computer (`pool-agent share` prints its
path). The server can never widen them. **Turn off** in the browser can only
narrow them.

## 8. Run a group (coordinators and admins)

### Roles

| Can… | Coordinator | Admin | Member | Contributor | Submitter |
|---|:-:|:-:|:-:|:-:|:-:|
| See the group's jobs, members, computers and usage | ✓ | ✓ | ✓ | ✓ | ✓ |
| Submit jobs | ✓ | ✓ | ✓ | – | ✓ |
| Share a computer with the group | ✓ | ✓ | ✓ | ✓ | – |
| Vote on join requests (when the rule asks members) | ✓ | ✓ | ✓ | – | – |
| Create invite links, read the audit log, cancel anyone's job | ✓ | ✓ | – | – | – |
| Change settings and roles, remove members, close the group | ✓ | – | – | – | – |

A **contributor** fits a lab machine's account that only shares; a
**submitter** fits a student without a spare computer.

**Two-step sign-in.** On a shared server, coordinators and admins need it for
invites, settings, roles and votes. The first time, a banner at the top of the
page offers **Set up two-step sign-in**: scan the code (or type the key) into an
authenticator app on your phone, then choose **Sign in again with my code**.
From then on you enter a 6-digit code from the app after your password. Members
who only run jobs or share computers are not asked.

### Everyday tasks

- **Invite someone:** tab **Invites** → role, number of uses, expiry → **Create invite** → copy the link and send it. On the CLI: `pool invite create my-lab --role member --max-uses 5 --hours 72`. Admins can invite members, contributors and submitters; only the coordinator can invite admins.
- **Join requests:** tab **Join requests**, or `pool requests list my-lab` and `pool requests vote my-lab REQUEST-ID approve`. The rule is one of: the coordinator alone, any *k* members, or a majority.
- **Roles and removal:** tab **Members**, or `pool members role my-lab bob admin` and `pool members remove my-lab bob --yes`. When someone is removed, work running on their computers for this group stops and goes back to the queue.
- **Fair use:** tab **Usage**, or `pool groups usage my-lab`. When several people's jobs wait, the person who used the least computer time in the last 7 days goes first.
- **Records:** tab **Audit log** (coordinators and admins) lists every important action. It is hash-chained, so changes are detectable.
- **Settings and closing:** tab **Settings** (coordinator only). Close the group when the work is done (`pool groups close my-lab --yes`).

### Server admins

The people listed in `POOL_SERVER_ADMINS` run the server itself. See the
[README](../README.md) and `deploy/`. Two of their actions have no screen yet:

- Mark an organisation-owned, dedicated computer: `PATCH /api/v1/devices/ID` with `{"org_owned_dedicated": true}`. Only such computers run `confidential` jobs.
- Check the audit log's hash chain: `GET /api/v1/audit/verify`.

## 9. When something goes wrong

### "Tasks waiting: why"

The job page explains every waiting job. The same reasons, in short:

| The page says | What to do |
|---|---|
| No computer is shared with this group yet | Someone in the group shares a computer ([§7](#7-share-your-computer)). |
| … shared with this group, but none is online | The owner starts the agent: `pool-agent run`. |
| … the computer's own sharing settings do not include this group | The owner runs `pool-agent share GROUP` on that computer. |
| … online but not available right now | Outside the owner's sharing hours, or they are using it (`--only-when-idle`). Wait, or ask. |
| This job asks for more than any shared computer offers | Lower `resources` in the job, or ask an owner to raise their limit. |
| No available computer can run this job | The job needs something no computer offers (operating system, isolation level, GPU, confidential data) or the computers only accept certain members. |
| … able to run this job | Nothing is wrong; it starts within seconds or when a computer finishes its current work. |
| Waiting to retry after a failure | The last try failed; see the next table. |

### A task failed

Open the task's log. Lines starting with `agent:` come from the computer that
ran it.

| Log shows | Cause | Fix |
|---|---|---|
| `agent: ... pull access denied`, `manifest unknown` | The computer could not download the image | Check the image name and digest, and that the image is public |
| `exit 1` (or another number) and your program's error | Your program failed | Fix the program; try it locally with `docker run --rm IMAGE COMMAND` |
| Result column shows `max_runtime_exceeded` | The task ran out of time | Raise `max_runtime` |
| `agent: outputs kept on the device only` | The server has no working file store | Ask the server admin; results are still in the log |

### The agent

| Message | Fix |
|---|---|
| `server URL must use https` | The server needs HTTPS. Plain http only works for a local dev server with `--insecure-http`. |
| `not enrolled yet` | Run `pool-agent enroll` first. |
| `enrollment code is invalid, used or expired` | Create a new code; a code works once, for 10 minutes. |
| The computer does not appear in the group | Run `pool-agent share GROUP` and make sure `pool-agent run` is running. Your role in the group must allow sharing (not *submitter*). |

## 10. What is not possible yet

- Jobs cannot use the internet, read input files from the server, or use secrets (passwords, tokens). Put small input data into the image.
- Images must be public; private registries are not supported.
- Checkpoints (resuming a long task where it stopped) are planned.
- GPUs and stronger isolation (gVisor) are built but not tested on real hardware.
- Windows computers are untested; Macs cannot share.
- The agent has no installer yet, and the CLI is not packaged.

See [`docs/STATUS.md`](STATUS.md) for the full status and
[`docs/design/easy-onboarding.md`](design/easy-onboarding.md) for what comes
next.
