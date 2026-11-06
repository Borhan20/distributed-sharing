---
name: pool
description: Runs Docker containers on a group's shared computers with the pool command (pool run, pool submit), waits for them, and collects their output, files and results table. Use when a program, training run, test suite, simulation or batch job needs more CPU, memory or GPU than this computer has, should run many times in parallel (parts, seeds, parameter sweeps, many files), or when the user mentions pool, a pool group, or running work on the lab's, class's or team's computers. Not for hosting services, apps that must be reached on a port, or work that needs the internet while it runs.
license: Apache-2.0
compatibility: Needs the pool command signed in to a pool server, and Docker for building images. Runs Linux containers. Jobs have no network while they run.
metadata:
  project: pool
  version: "0.1"
---

# Run containers on a group's computers with pool

pool runs a container on a computer of the user's group instead of this one.
It works like `docker run`: `docker run IMAGE ARGS` becomes `pool run IMAGE ARGS`.
pool picks the computer. Output comes back to the terminal, and the files the
container writes to `/out` come back to a local folder.

## 1. Check that pool is ready

Run `scripts/check.sh`. It finds the pool command, checks the sign-in and
Docker, and prints what is missing.

The pool command is one of these:

- `pool`, when it is installed;
- `uv run --project /path/to/pool-checkout pool`, from a checkout of the pool repository (the CLI is not packaged yet). Set `POOL_CMD` to this to make the scripts use it.

If the user is not signed in, ask them to run `pool login` themselves. It
needs their server address and an access token. Never ask for the token in
the chat.

## 2. Pick how to run it

| The work | Do this |
|---|---|
| A folder with a `Dockerfile` | `pool run -d [FLAGS] -- [ARGS]` in that folder. pool builds the image on this computer and uploads it to the group's registry. |
| An image that already exists | `pool run -d [FLAGS] IMAGE [ARGS]` |
| The same program many times: parts, seeds, files, test files | A job file with `parts:` or `sweep:`, then `pool submit job.yaml`. See [references/job-files.md](references/job-files.md). |
| Settings used every time | `pool.yaml` next to the `Dockerfile` (a job file plus `build` and `platform`). Flags win over it. |

Arguments after the image, or after `--`, go to the image's own command, as
with `docker run IMAGE ARG...`. `--entrypoint CMD` replaces the command.

## 3. Follow the container rules

Every task runs with these rules. A program that breaks one of them fails.

| Rule | What to do |
|---|---|
| No network while it runs | Install packages, models and data in the `Dockerfile`. The build runs on this computer, with internet. |
| Read-only file system | Write results to `/out` (they come back) and scratch files to `/tmp` (256 MB, deleted, cannot run programs from it). |
| Unprivileged user (uid 65534) | Do not depend on root or on a home folder. Set cache folders to `/tmp` (for example `HOME=/tmp`). |
| One result row per task | Write one JSON object to `/out/result.json`. All tasks' rows become one table. |
| Each task knows its number | `POOL_TASK_INDEX` (0, 1, ...) and `POOL_TASK_COUNT`. Each part takes its share, e.g. every n-th item. |
| No ports and no interactive terminal yet | Servers and web apps do not work yet. Run programs that compute and end. |
| Linux containers, built for `linux/amd64` by default | Use `--platform linux/arm64` only if the group's computers are ARM. |

`pool run` refuses `-v`, `--mount`, `--network`, `--privileged`, `--cap-add`,
`--device`, `-u` and `-p`. The message says why. Copy files into the image
instead of mounting them. `-it` is accepted, but there is no terminal: the
program's input is empty.

Dockerfile patterns for Python, Node, Java, Go, C++, R and others are in
[references/dockerfiles.md](references/dockerfiles.md).

## 4. Run it: the steps

1. **Try it on this computer first:** `pool run --local [FLAGS] -- [ARGS]`. It applies the same rules (no network, read-only, uid 65534, the size limits). It finds most problems in seconds. Read every line that starts with `pool:`: it is a hint.
2. **Ask the user before a big run.** Many parts, GPUs, or more than an hour use other people's computers. Say how many tasks, what size, and how long.
3. **Start it in the background:** `pool run -d [FLAGS] -- [ARGS]` (or `pool submit job.yaml`). Read the job ID from the line `pool: Submitted NAME (N task(s)) to GROUP: job JOB-ID`.
4. **Wait:** `scripts/wait.sh JOB-ID`. It checks `pool status` until the job ends or a timeout passes (default 8 minutes, so it fits in one tool call). Exit 0: the job ended. Exit 2: still running, so call it again. Look at the output meanwhile with `pool logs JOB-ID --task 0`.
5. **Collect, after the job has ended:**
   - `pool attach JOB-ID -o pool-out` saves the `/out` files, prints the output and the hints. Its exit code is the program's exit code.
   - `pool download JOB-ID --all -o DIR` saves every task's files, one folder per task, checked by SHA-256.
   - `pool results JOB-ID -o results.csv` saves all `result.json` rows as one table.
6. **Report to the user:** the final state, where the files are, and any `pool:` hint.

Do not run a long job attached (`pool run` without `-d`, or `pool attach` on
a running job) inside a tool call that can time out. If the tool interrupts it
with Ctrl-C (SIGINT) and there is no terminal, pool cancels the job at once.
To stop a job on purpose, use `pool cancel JOB-ID`.

## 5. Choose the size

| Flag | Default | Notes |
|---|---|---|
| `--cpus N` | 1 | |
| `-m SIZE` | 1g | |
| `--disk SIZE` | 1g | |
| `--gpus N` | 0 | GPUs are not yet tested on real hardware. |
| `--time-limit T` | 1h | e.g. `30m`, `6h` |
| `--retries N` | 0 | Only for flaky programs. A failing program fails the same way again. |

pool does not choose sizes by itself yet. When the user does not know them:

1. Start with the defaults, or measure with `pool run --local`.
2. Exit code 137, or the hint about signal 9, means the program ran out of memory. Run it again with twice the memory.
3. Stopped at the time limit: run it again with a longer `--time-limit`.
4. Ask only for what the program needs. Smaller tasks fit on more computers and start sooner.

## 6. Exit codes

| Command | Exit code |
|---|---|
| `pool run` (attached), `pool attach` | 0: every task succeeded. 130: cancelled. One task: the program's own exit code. Several tasks: 1 if any failed. |
| `pool run -d`, `pool submit` | 0 once the job is submitted. A refused job prints the reason and exits non-zero. |
| `scripts/wait.sh` | 0: the job ended (check its state). 2: still running at the timeout. 1: error. |

## 7. Do not

- Do not put secrets (passwords, tokens, keys) in `-e`, `--env-file` or the image. The computer's owner can read them. Tell the user that jobs cannot use secrets yet.
- Do not cancel, delete or change other people's jobs, or change group settings, members, invites or registry tokens, unless the user asks for exactly that.
- Do not share the user's computer (`pool-agent share`) unless they ask.
- Do not submit a failing job again and again. Read the log and the hints first, then fix the cause.
- Do not invent flags. If unsure, run `pool run --help` or `pool COMMAND --help`.

## 8. When something goes wrong

Read [references/troubleshooting.md](references/troubleshooting.md). It
maps each message to its fix: the job is refused, tasks keep waiting, the
image cannot be pulled, the program fails under the rules, and others.
