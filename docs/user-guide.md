# pool user guide

pool lets a group of people (a lab, a class, a team) run jobs on each other's
computers. This guide goes from joining a group to running your own code on
the group's computers and collecting the results, with an example for each
kind of work. The web UI has a shorter version of this guide, organised by
role, under **Guide**.

**Contents**

1. [The ideas in two minutes](#1-the-ideas-in-two-minutes)
2. [Join a group](#2-join-a-group)
3. [Your first job, in the browser](#3-your-first-job-in-the-browser)
4. [Write a job](#4-write-a-job)
5. [Examples for every kind of work](#5-examples-for-every-kind-of-work)
6. [Private images](#6-private-images)
7. [Collect the results](#7-collect-the-results)
8. [Run your program from your terminal: `pool run`](#8-run-your-program-from-your-terminal-pool-run)
9. [The command line](#9-the-command-line)
10. [Share your computer](#10-share-your-computer)
11. [Run a group (coordinators and admins)](#11-run-a-group-coordinators-and-admins)
12. [When something goes wrong](#12-when-something-goes-wrong)
13. [What is not possible yet](#13-what-is-not-possible-yet)

## 1. The ideas in two minutes

| Word | Meaning |
|---|---|
| **Group** | The people who share computers with each other. Invite-only. |
| **Role** | What you may do in a group: coordinator, admin, member, contributor or submitter ([§11](#roles)). |
| **Computer** (device) | A machine a member shares with the group. It runs the **agent**, a small program that takes work from the server. |
| **Image** | A container image (Docker) with your code and what it needs, named like `ghcr.io/your-org/app:1`. When you submit, the server fixes the exact version the tag names at that moment, so every task runs the same software. |
| **Job** | Work you send to the group: an image, a command, how to run it, and how much CPU, memory and disk each task needs. |
| **Task** | One run of the command. A job runs **once**, **split into parts**, or **for each value** of a sweep; each part or value is one task, and tasks run in parallel on different computers. |
| **`/out`** | The folder where a task writes result files. A `result.json` there becomes one row of the job's results table. |

How a job runs:

1. You submit it, in the browser or with the `pool` command.
2. The server splits it into tasks. **You never choose computers**: each task goes to a shared computer that has room and matches what the task needs.
3. The task runs there in a locked-down container: no network, no access to the owner's files.
4. If a computer goes away mid-task, the task moves to another computer by itself.
5. You download everything at once: every task's files and log in one zip, and all `result.json` files as one table.

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
[shares a computer](#10-share-your-computer).

## 3. Your first job, in the browser

1. Open your group and go to **Jobs → Submit a job**. The form already holds a small example that runs as-is.
2. Press **Submit job**. The job page opens.
3. Watch the tasks: *pending* → *running* → *succeeded*. Click **Logs** on a task to see its output (`part 0 of 4` …). When a task ends, its **Used** column shows the memory, CPU and time it used. The **Used** line under **Spec** puts the most one task used next to what each task asked for.
4. Under **Results**, press **Download all (zip)** to get every task's files and log at once ([§7](#7-collect-the-results)).
5. If tasks stay *pending*, the box **"tasks waiting: why"** tells you why and what would fix it, for example *"No computer is shared with this group yet"*.

The form:

| Field | What to put there |
|---|---|
| **Image** | The image with your code, as you push it: `ghcr.io/your-org/app:1`, `yourname/app:v2` (Docker Hub) or `python:3.12-slim`. Private? See [§6](#6-private-images). |
| **Command** | What to run, as you would type it in a terminal: `python /app/main.py --part {{index}}`. Leave it empty to run the image's own command. |
| **How to run** | **Once**: one task. **Split into parts**: copies of the same command, **Auto** picks as many as the group's computers can run at once. **For each value**: one task per value, e.g. `seed` = `0..4`. |
| **Size of each task** | **Let pool decide** (the default): pool chooses CPU, memory and disk from your earlier runs ([§4](#how-pool-chooses-sizes)). Or Small, Medium, Large or Custom, if you know what your program needs. |
| **Time limit per task** | Empty: pool decides. Or e.g. `30m`, `2h`. A task that runs longer is stopped. |
| **More options** | Name (default: from the image), retries after a failure, GPUs per task. |

**Show as job file** turns the form into the same job as a short YAML file,
which you can save, send to a colleague, or submit with `pool submit`. The
**Job file (YAML)** tab takes such a file. On any job page, **Run again with
changes** opens the form filled with that job, so you or a colleague can change
one value and submit it again.

## 4. Write a job

A job file is a short YAML file. This one splits a Python program into parts:

```yaml
name: my-sweep                       # optional; default: from the image name
image: ghcr.io/your-org/app:1        # name and tag, as you push it
command: python /app/main.py --part {{index}} --of {{count}}
parts: auto                          # or a number; or use sweep: instead
cpu: 1
memory: 1Gi
time_limit: 30m
```

| Field | What it does | Default |
|---|---|---|
| `image` | The container image. A tag (`:1`, `:latest`) is fixed to its exact version (digest) when you submit; a name with `@sha256:...` is used as is. | required |
| `command` | One string, split like a terminal does (quotes work; pipes and `&&` need `sh -c '...'`), or a list `["python", "main.py"]`. | the image's own command |
| `parts` | Split into this many copies of the command, or `auto`. | run once |
| `sweep` | One task per combination of values: `seed: 0..9`, `size: [small, large]` or `size: small, large`. Several keys multiply: 10 × 2 = 20 tasks. Up to 10,000. | – |
| `cpu`, `memory`, `disk` | Per task: cores (may be fractional), and sizes like `512Mi` or `4Gi`. | pool decides |
| `gpu` | GPUs per task. | `0` |
| `time_limit` | Per task, e.g. `30m`, `2h`. | pool decides |
| `retries` | How often a failed task is tried again (0–20). A task that fails on 2 different computers is marked failed. | `3` |
| `name`, `group` | The job's name; the group (or pass `--group`). | from the image |
| `isolation`, `data_class`, `os` | Optional: minimum isolation `T1` (default), `T2` or `T3`; `public`, `internal` (default) or `confidential` (only on dedicated organisation computers); `linux` or `windows`. | – |

### How pool chooses sizes

Leave out `cpu`, `memory`, `disk` or `time_limit`, and pool chooses them. A
size you give is kept as it is.

| Your runs of this image | What each task gets |
|---|---|
| None yet (a **first run**) | All the free memory of the computer it runs on, 1 CPU, and the group's default time limit (4 hours, unless the coordinator changed it) |
| One or more | Memory: the most your runs used × 1.25. CPU: what they used on average, or twice as much if the last run used all it had. Time limit: 3 × your longest run, at least 30 minutes. |

- **Your own runs only.** pool looks at your earlier runs of the same image (any version), and best of all the ones with the same command. Other people's runs never size your tasks.
- **Part 0 first.** On a first run of a job with parts, part 0 runs alone. The other parts start when it ends, with the size it used.
- **One first run at a time.** A first run takes all the free memory of a computer, so you have one at a time. To skip the wait, set the memory yourself.
- **Too small?** If a task runs out of memory or time with a size pool chose, pool tries it again with more: twice the memory (on a first run, a computer with more free memory) or twice the time. At most 3 times. These tries are not counted as your failures, and not in fair share.
- **Why?** The job page (**Each task → Chosen by pool: why**), `pool run` and `pool submit` say why each size was chosen.

**pool also reads the image** before it runs (once per image version):

| The image | What pool does |
|---|---|
| Has versions for some kinds of computer (amd64, arm64) | Sends its tasks only to computers of those kinds. The job page shows them under **Image**. |
| Is built on NVIDIA CUDA | Asks for 1 GPU, if a computer in the group shares one. Otherwise it runs on the CPU, and the job page says so. |
| Opens ports (`EXPOSE`) | Says so. Apps that listen on a port do not work yet ([§13](#13-what-is-not-possible-yet)). |
| Is big | A first run asks for more disk: about 3 × the download. |

**Which computer.** You never choose one. When a computer asks for work, pool
gives it the task that fits it best:

1. The owner's own priority for their groups comes first.
2. With credits, members who have credits come before those who have none.
3. A computer with a GPU takes GPU work first. (GPU time counts in fair share:
   one GPU hour as 10 CPU hours.)
4. Fair share: whoever used the least in the last 7 days. People whose use is
   close (in bands: under 1 hour, 1–3 hours, 3–7 hours, …) count as equal here.
5. Among equals: work that needs more than half of this computer's free memory
   first, then the least use, then work whose image the computer already has
   (it starts without a download), short work on a computer that often loses
   its tasks, then the oldest.

So asking for more memory than you need never moves you ahead of someone who
used much less.

**Each task knows its number.** In the command, `{{index}}` is the task's
number (0, 1, 2, …) and `{{count}}` the number of tasks. Inside the container
the same values are in the environment variables `POOL_TASK_INDEX` and
`POOL_TASK_COUNT`, so a program can pick its share of the work itself:

```python
import os
part, parts = int(os.environ["POOL_TASK_INDEX"]), int(os.environ["POOL_TASK_COUNT"])
random.seed(part)                # Monte Carlo: every part its own seed
my_files = all_files[part::parts]  # a list of work: every part takes every n-th item
```

With `sweep`, each name is also a placeholder: `--seed {{seed}}`.

**`parts: auto`** counts the shared computers that can run a task of this size
and are available now (or, if none is, the ones that are shared), and makes as
many parts as fit on them at once. The job page says what it chose, for example
*"Split into 8 part(s): room for 8 at once on 2 computers available now."*

**Which way to run?**

| Your work | Use |
|---|---|
| One program, one result (a simulation, a training run, a build) | run once (no `parts`, no `sweep`) |
| The same program many times with different random seeds | `parts` (seed = `POOL_TASK_INDEX`) |
| A list of files, items or tests to work through | `parts` (each part takes every n-th item) |
| Try combinations of settings (seeds × learning rates) | `sweep` |

Splitting only works when the program can do part of the work. A program that
cannot (one big training run, for example) runs **once**, on a computer that
is big enough ([§5.5](#55-borrow-a-bigger-machine)).

**YAML tip.** A command that contains `: ` or ` #` must go on the next line
after `>-`, or YAML misreads it:

```yaml
command: >-
  sh -c 'echo "{\"part\": {{index}}}" > /out/result.json'
```

The full form of a job file (`apiVersion`, `kind`, `metadata`, `spec`, as in
[`examples/hello-array.yaml`](../examples/hello-array.yaml)) is accepted too.

## 5. Examples for every kind of work

Every example is in [`examples/`](../examples), ready to run. Replace
`your-org` with your organization or Docker Hub name.

| Example | Kind of work | Typical groups |
|---|---|---|
| [5.1](#51-many-random-runs-without-building-an-image) Many random runs | `parts`, no image to build | anyone, first try |
| [5.2](#52-your-own-code-as-a-parameter-sweep) Your own code as a parameter sweep | `sweep` | research teams, students |
| [5.3](#53-process-many-files) Process many files | `parts` over a list | research, courses (grading submissions) |
| [5.4](#54-split-a-test-suite) Split a test suite | `parts` over test files | dev teams, hackathons |
| [5.5](#55-borrow-a-bigger-machine) Borrow a bigger machine | once | anyone |

### 5.1 Many random runs, without building an image

Estimate π with a Monte Carlo simulation, using a public Python image. Each
part uses its number as the random seed and writes one `result.json`.
Save as `monte-carlo-pi.yaml` ([`examples/monte-carlo-pi.yaml`](../examples/monte-carlo-pi.yaml)):

```yaml
name: monte-carlo-pi
image: python:3.12-slim
command: |
  python -c "
  import json, os, random
  part = int(os.environ['POOL_TASK_INDEX'])
  random.seed(part)
  n = 2_000_000
  inside = sum(random.random() ** 2 + random.random() ** 2 <= 1 for _ in range(n))
  json.dump({'part': part, 'samples': n, 'pi': 4 * inside / n}, open('/out/result.json', 'w'))
  print('part', part, 'done')
  "
parts: auto
cpu: 1
memory: 256Mi
time_limit: 15m
```

Paste it into **Job file (YAML)** and submit, or run `pool submit
monte-carlo-pi.yaml --group my-lab`. When it is done, the job page shows one
table row per part (`part`, `samples`, `pi`); **Download results table (CSV)**
gives the same table as a file.

### 5.2 Your own code as a parameter sweep

Run your program for 5 seeds and 2 sample sizes: 10 tasks
([`examples/pi-sweep/`](../examples/pi-sweep)).

**Step 1. Write the program**, `pi.py`. It prints its result to the log and
writes it to `/out/result.json`:

```python
import argparse, json, pathlib, random, time

parser = argparse.ArgumentParser()
parser.add_argument("--seed", type=int, required=True)
parser.add_argument("--samples", type=int, required=True)
args = parser.parse_args()

random.seed(args.seed)
start = time.time()
inside = sum(1 for _ in range(args.samples) if random.random() ** 2 + random.random() ** 2 <= 1)
result = {"seed": args.seed, "samples": args.samples, "pi": 4 * inside / args.samples,
          "seconds": round(time.time() - start, 2)}

print(json.dumps(result))                                   # visible in the task's log
pathlib.Path("/out/result.json").write_text(json.dumps(result))  # one row of the results table
```

**Step 2. Put it in an image.** `Dockerfile` next to `pi.py`:

```dockerfile
FROM python:3.12-slim
COPY pi.py /app/pi.py
```

**Step 3. Build it and push it**, to ghcr.io or Docker Hub, with your own
account. The image can stay **private** ([§6](#6-private-images)):

```bash
docker login ghcr.io                 # once, with your own account (Docker Hub: docker login)
docker build -t ghcr.io/your-org/pi-sweep:1 .
docker push ghcr.io/your-org/pi-sweep:1
```

**Step 4. Describe the job**, `pi-sweep.yaml`:

```yaml
name: pi-sweep
image: ghcr.io/your-org/pi-sweep:1
command: python /app/pi.py --seed {{seed}} --samples {{samples}}
sweep:
  seed: 0..4
  samples: [1000000, 10000000]
cpu: 1
memory: 512Mi
time_limit: 30m
retries: 2
```

In the browser, the same job is **How to run: For each value** with two rows,
`seed` = `0..4` and `samples` = `1000000, 10000000`.

**Step 5. Submit it** in the browser or with `pool submit pi-sweep.yaml --group
my-lab`. Pushed a new `:1` later? The next job you submit uses it; running jobs
keep the version they started with.

**Step 6. Collect the results**: the results table has one row per task with
`seed`, `samples`, `pi` and `seconds` ([§7](#7-collect-the-results)). To try
more seeds without editing the file: `pool sweep pi-sweep.yaml --set seed=0..19
--group my-lab` (40 tasks; add `--dry-run` to see the count first).

### 5.3 Process many files

Count the lines and words of a folder of text files, split over the group's
computers ([`examples/many-files/`](../examples/many-files)). The same pattern
fits grading a pile of submissions or converting a set of images.

The program takes every n-th file, starting at its own number, so the parts
share the work without overlap, and writes a CSV with the same columns in
every part:

```python
import csv, os, pathlib

part = int(os.environ["POOL_TASK_INDEX"])
parts = int(os.environ["POOL_TASK_COUNT"])
files = sorted(pathlib.Path("/data").glob("*.txt"))

with open("/out/summary.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["file", "lines", "words"])
    for path in files[part::parts]:
        text = path.read_text()
        w.writerow([path.name, len(text.splitlines()), len(text.split())])
```

Jobs cannot read files from the server yet, so the files go into the image:

```dockerfile
FROM python:3.12-slim
COPY process.py /app/process.py
COPY data/ /data/
```

```yaml
name: count-words
image: ghcr.io/your-org/count-words:1
command: python /app/process.py
parts: auto
cpu: 1
memory: 512Mi
time_limit: 1h
```

**Download all** then contains `combined/summary.csv`: every part's
`summary.csv` in one file, with the task number in front of each row.

### 5.4 Split a test suite

Run a slow test suite on several computers at once
([`examples/test-suite/`](../examples/test-suite)). The image holds the code,
its tests and pytest:

```dockerfile
FROM python:3.12-slim
RUN pip install --no-cache-dir pytest==8.3.3
COPY tests/ /app/tests/
WORKDIR /app
```

Part *i* of *n* runs every n-th test file; a part without files has nothing to
do. `-p no:cacheprovider` because the container's file system is read-only:

```yaml
name: tests
image: ghcr.io/your-org/app-tests:1
command: >-
  sh -c 'files=$(ls tests/test_*.py | awk -v n="$POOL_TASK_COUNT" -v i="$POOL_TASK_INDEX" "(NR - 1) % n == i");
  [ -n "$files" ] || exit 0;
  pytest -q -p no:cacheprovider --junitxml=/out/junit.xml $files'
parts: 3
cpu: 1
memory: 1Gi
time_limit: 30m
retries: 0
```

`retries: 0`, so a failing test is reported at once instead of being retried.
A failed part shows the pytest output in its log; **Download all** has each
part's `junit.xml`.

### 5.5 Borrow a bigger machine

One task that needs more than your laptop has, here 8 cores and 16 GiB:

```yaml
name: big-simulation
image: ghcr.io/your-org/simulation:1
command: python /app/simulate.py --threads 8
cpu: 8
memory: 16Gi
disk: 10Gi
time_limit: 6h
```

If no shared computer offers that much, the job page says so, for example
*"This job asks for more than any shared computer offers: needs 16 GiB memory
(most offered: 7 GiB)"*. Ask for less, or ask an owner to raise their limit
(`pool-agent share my-lab --memory 20Gi`).

## 6. Private images

Public images (like `python:3.12-slim`) need nothing. For a **private** image,
the server needs a **read-only token** for its registry. Nobody puts a password
in a job file, and the computers that run the tasks never see the token: for
each task, the server exchanges it for access to that one image that expires
within minutes.

There are two places for a token:

| Where | Who adds it | Used for | Fits |
|---|---|---|---|
| **My images** (top bar), or `pool registry set docker.io --username YOU --mine` | you | only jobs **you** submit, in all your groups | images in your own account, e.g. `yourname/app:1` on Docker Hub |
| The group's **Images** tab, or `pool registry set ghcr.io --username READER --group my-lab` | a coordinator or admin | every job in the group | images in the group's organization; everyone pushes with their own account |

When both exist, your own token is used for your jobs. When you submit a job,
the server looks up the image's tag with the token, so a wrong token or name
is reported at once: *"... was not found, or it is private. ... add a
read-only token for docker.io under My images."*

**How to make a read-only token** (checked against Docker's and GitHub's
documentation on 3 November 2026; check theirs if the steps have changed):

- **Docker Hub:** at app.docker.com, **your avatar → Account settings →
  Personal access tokens → Generate new token**. Access permissions: **Repo
  Read-only**. Do not keep the default, *Repo Public Read-only*: it cannot read
  private images. The account name is your Docker ID.
- **GitHub (ghcr.io):** **Settings → Developer settings → Personal access
  tokens → Tokens (classic) → Generate new token (classic)**, with only the
  `read:packages` scope. Fine-grained tokens do not work with ghcr.io. The
  account name is your GitHub user name.

Both sites show the token only once, so copy it right away. Give it an expiry
date, and when it expires, store a new one the same way. A token belongs to
the account that made it: it stops working if that account loses access to
the images. For a group token, an account made for this, with read access to
the organization's images, is best. Docker Hub tokens cannot be limited to one
repository, so a personal token can read all private images in your account;
pool still only ever gives a computer access to the one image a task needs.

The server checks every token with the registry, stores it encrypted, and never
shows it again. Before it gives a computer pull access, it checks that the
token really reads that image. A computer that already holds a private image
for one group does not let another group's task use it unless that group can
read the image too.

## 7. Collect the results

Each task writes its files to `/out`. Everything it prints goes to its log.

**In the browser**, on the job page under **Results**:

- **Download all (zip)**: everything at once.
- The **results table**: every task's `/out/result.json` in one table, with the task's sweep values in front. **Download results table (CSV)** saves it.
- **Logs → Download log** on a task saves that task's whole log.

The zip holds:

```text
README.txt                  what is inside, and any file left out (and why)
tasks.csv                   one line per task: values, state, tries, computer, run time
results.csv                 every result.json in one table
combined/summary.csv        CSV files with the same name and columns, from all tasks, merged
summary.md                  the run: what ran where, size given and used, why, failed tasks
tests.md                    every task's JUnit XML test report merged into one (when there are any)
task-000_seed=0/result.json one folder per task: its files and log.txt
task-000_seed=0/log.txt
task-001_seed=1/...
```

For the results table, write one JSON object per task to `/out/result.json`,
for example `{"seed": 3, "accuracy": 0.91}`. Nested values become columns like
`metrics.loss`.

**On the command line:**

```bash
pool download JOB-ID --all -o results/   # every task's files, in the same folders as the zip
pool results JOB-ID -o results.csv       # the results table
pool logs JOB-ID --task 3                # one task's log
pool tests JOB-ID                        # every task's test report merged: passed, failed, slowest
```

`pool download` checks every file's SHA-256 against what the computer
reported, so a damaged or swapped file is never saved. The zip does the same:
it leaves such a file out (files over 64 MiB are checked while they stream, so
they are marked instead) and says so in `README.txt`.

**Test reports.** If your tasks write JUnit XML (most test tools can, e.g.
`pytest --junitxml=/out/junit.xml`), pool merges every task's report into one:
the job page shows a **Tests** section, `pool tests JOB-ID` prints it, the zip
has `tests.md`, and `pool run` ends with `Tests: 120 passed, 2 failed`. Reports
of failed tasks count too.

| Limit for test reports | Value |
|---|---|
| File | UTF-8 XML, no DOCTYPE, at most 10 MiB |
| Tests | 50,000 per job |
| Files read for one job | 500, or 64 MiB in all (the rest are listed as not read) |

**When will it be done?** While a job runs, the job page and `pool attach`
say how long it will probably take, from its finished tasks (or your earlier
runs): `about 25 min more: 40 task(s) of about 5 min, 8 at a time`. A task that
prints nothing for 30 minutes gets a note; its time limit still stops it.

**Combine further** in a spreadsheet, or in Python:

```python
import pandas as pd
df = pd.read_csv("results.csv")
print(df.groupby("samples")["pi"].agg(["mean", "std"]))
```

Files stay on the server's file store. A server without one keeps only the
logs; the files stay on the computers that ran the tasks. A very large job
(over 2 GiB of files, by default) is too big for one zip: use `pool download
--all`.

## 8. Run your program from your terminal: `pool run`

`pool run` works like `docker run`. The difference: your program runs on a
computer of your group, not on yours. Use it when your computer does not have
enough CPU, memory or a GPU.

It works for programs in any language. pool only sees a container, a command
and its arguments.

### What you need

- Docker on your computer.
- The `pool` command, signed in ([§9](#9-the-command-line)).

### Run your program

1. Go to the folder with your `Dockerfile`.
2. Run `pool run`. Put your program's arguments after `--`.
3. Watch the output in your terminal. Press **Ctrl-C** to stop.
4. When it ends, find its files in `./pool-out`.

```text
$ cd my-training
$ pool run --gpus 1 -m 16g -- --epochs 20
pool: Building Dockerfile for linux/amd64 ...
pool: Uploaded 1 new layer(s), 5 already there.
pool: Submitted train (1 task(s)) to ml-lab: job 6f1c...
pool: Running on lab-gpu
epoch 1/20  loss 2.31
...
pool: 3 file(s) saved to pool-out/
pool: done
```

### What happens

| Step | Where | What |
|---|---|---|
| Build | Your computer | Docker builds your `Dockerfile`. |
| Upload | The group's registry, on the pool server | The image is saved under your name. Only the parts that changed are uploaded, usually just your code. |
| Run | A computer of your group | The program runs there, in a locked container. |
| Results | Your computer | The output streams to your terminal. The files in `/out` come back to `./pool-out`. |

You do not need a Docker Hub or GitHub account. Only you can use your images.

### Arguments

The words after `--` go to the image's own command, the same as with
`docker run IMAGE ARG...`.

Example: the Dockerfile has `ENTRYPOINT ["python", "/app/train.py"]`. Then
`pool run -- --epochs 20` runs `python /app/train.py --epochs 20`.

You can also use an image you already have. Then no Dockerfile is needed:

```bash
pool run python:3.12-slim python -c "print(1)"
pool run ghcr.io/you/app:1 --port 8080      # a private image: see §6
```

### Flags

The flags are the same as for `docker run`:

| Flag | What it does | Default |
|---|---|---|
| `-e KEY=VALUE`, `-e KEY`, `--env-file FILE` | Set environment variables. Do not use them for real secrets: the computer's owner can read them. | – |
| `--cpus 4` | CPU cores | pool decides ([§4](#how-pool-chooses-sizes)) |
| `-m 8g` | Memory | pool decides |
| `--disk 20g` | Disk | pool decides |
| `--gpus 1` | GPUs | 0 |
| `-w /app` | Working directory in the container | the image's |
| `--entrypoint sh` | Run another command than the image's | the image's |
| `--time-limit 2h` | Stop the job after this time | pool decides |
| `--retries 1` | Try again after a failure | 0 |
| `-d` | Start the job and return at once | – |
| `-o DIR` | Where the files go | `./pool-out` |
| `--name NAME` | Job name | the folder name |
| `--group GROUP` | Group | from `pool login --group` |
| `--build DIR`, `-f FILE` | What to build | `.` and `Dockerfile` |
| `--platform linux/arm64` | Build for this kind of computer | the kind most of the group's computers are (with `--local`: yours) |
| `--local` | Run it on **your** computer, with the rules of a member's computer | – |

Some `docker run` flags are refused. The message says why:

| Flag | Why it is refused | Do this instead |
|---|---|---|
| `-v`, `--mount` | Your folders are not on that computer. | Copy the files into the image (`COPY` in the Dockerfile). |
| `--network` | Jobs have no network. | Put everything the program needs into the image. |
| `--privileged`, `--cap-add`, `--device`, `-u` | They would give the job power over the owner's computer. | – |
| `-p` | Apps that listen on a port do not work yet. Nobody could reach the port: jobs have no network. | Run programs that compute and write files to `/out`. Apps (web apps, APIs, databases) are planned ([design](design/pool-run.md#5-delivery-order-and-acceptance-checks)). |

`-it` is accepted, but there is no interactive terminal yet: the program's
output streams to you, and its input is empty.

### Any language, any kind of work

The Dockerfile builds your program. These examples are ready to run, in
[`examples/run/`](../examples/run). Each one needs no settings: go to its
folder and type `pool run`.

| Example | Kind of work | Language | What comes back |
|---|---|---|---|
| [`python-train`](../examples/run/python-train) | Training a model | Python | `model.json`, `result.json` |
| [`sklearn-classifier`](../examples/run/sklearn-classifier) | Machine learning with a library | Python, scikit-learn | accuracy, scores, confusion matrix |
| [`python-images`](../examples/run/python-images) | Image processing | Python, Pillow | thumbnails, edge pictures |
| [`node-report`](../examples/run/node-report) | Data processing: a report from a dataset | JavaScript (Node.js) | `result.json`, a CSV |
| [`node-tests`](../examples/run/node-tests) | A test suite with a test report | JavaScript (Node.js) | `junit.xml` |
| [`java-wordcount`](../examples/run/java-wordcount) | Text processing | Java | top words as CSV |
| [`r-stats`](../examples/run/r-stats) | Statistics | R | confidence intervals |
| [`go-primes`](../examples/run/go-primes) | A CPU-heavy computation | Go | `result.json` |
| [`cpp-heat`](../examples/run/cpp-heat) | A simulation | C++ | `heat.csv` |

Compiled languages (Go, C++, Java, and also Rust and .NET) build in one stage
and run in a small image. Most examples read `POOL_TASK_INDEX` and
`POOL_TASK_COUNT`, so they can also be split into parts.

Two rules for every language:

- Write your results to `/out`.
- Size the memory of your runtime from the container's limit. Java does this
  by itself, but uses only 25% for its heap: add `-XX:MaxRAMPercentage=75`.
  For Node, use `--max-old-space-size`.

### Check on your computer first: `--local`

`pool run --local` runs the job on your computer. It uses the same rules as a
member's computer:

- no network,
- a read-only file system,
- an unprivileged user (uid 65534),
- only `/out` and `/tmp` can be written,
- the CPU and memory you asked for.

So a program that would fail there fails here first. pool shows a hint:

```text
$ pool run --local -- --epochs 2
OSError: [Errno 30] Read-only file system: '/app/cache'
pool: The container's file system is read-only. Write to /out (kept, comes back to you) or /tmp (scratch).
```

The hints work for every language. They also show after a run on a member's
computer.

### See what your program used

After every run, pool says how much memory, CPU and time the program used,
next to what it was given. This works with `--local` too, so you can find out
a program's size on your own computer before you ask the group for it:

```text
$ pool run --local
...
pool: It used: memory 116 MiB of 1.0 GiB, CPU 0.62 of 1 on average, run time 2 s of 1 h.
```

| You see | What it means | Do this |
|---|---|---|
| Memory far below what it was given | You asked for too much | Ask for less (`-m`). Smaller tasks find a computer sooner. |
| `Out of memory: the program needed more than its limit` | The program hit its memory limit and was stopped | Ask for more (`-m`). |
| CPU close to what it was given, the whole time | The program could use more cores | Try `--cpus` with more, if the program uses several threads. |

How it is measured: the computer reads `docker stats` about once a second.
Very short programs (under 2 seconds) may show no numbers.

### Save your settings: `pool.yaml`

Put the settings you always use into `pool.yaml`, next to your Dockerfile. It
is a job file ([§4](#4-write-a-job)) with two extra fields: `build` and
`platform`. Flags on the command line win over the file.

```yaml
name: train
cpu: 2
memory: 4Gi
time_limit: 2h
```

Then `pool run` alone is enough. Most programs need no `pool.yaml`: pool
chooses the sizes ([§4](#how-pool-chooses-sizes)). Use it when you want fixed
sizes. The program's own options can go there too (`args: --epochs 300`), but
a program with good defaults does not need them.

### Stop, run in the background, follow again

| You want to | Do this |
|---|---|
| Stop the job | Press **Ctrl-C**. pool asks first. In a script, it stops at once. |
| Start it and do something else | `pool run -d ...` |
| Follow a job again | `pool attach JOB`. It shows the output, then saves the files. It works for every job, also one from the browser. The job page shows the command. |

### Your images in the group's registry

- Your images are named `SERVER/GROUP/YOU/NAME:run-DATE`.
- Each group has its own space. Your images in one group are not in another.
- An image is deleted 30 days after it was last used. Using it in a job keeps
  it.
- A coordinator can change the number of days on the group's **Images** tab.

## 9. The command line

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
| Submit a job (short or full job file) | `pool submit job.yaml [--group G] [--wait]` |
| Run a program like `docker run` ([§8](#8-run-your-program-from-your-terminal-pool-run)) | `pool run [FLAGS] [IMAGE] [ARG...]`, `pool run [FLAGS] -- [ARG...]` (builds ./Dockerfile), `pool run --local ...` |
| Follow a job and save its files | `pool attach JOB-ID [-o FOLDER]` |
| Sweep from the command line | `pool sweep job.yaml --set seed=0..9 --set dataset=small,large` |
| List jobs | `pool jobs list my-lab [--state running]` |
| Job status | `pool status JOB-ID` |
| Logs | `pool logs JOB-ID --task 0 [-f]` |
| Download results | `pool download JOB-ID --all [-o FOLDER]` (every task) or `pool download JOB-ID [--task N] [--path FILE] [-o FOLDER]` |
| Results table (CSV) | `pool results JOB-ID [-o results.csv]` |
| Test report (all tasks' JUnit XML merged) | `pool tests JOB-ID` |
| My own registry tokens (private images) | `pool registry list --mine` / `pool registry set docker.io --username YOU --mine` / `pool registry remove docker.io --mine` |
| The group's registry tokens | `pool registry list my-lab` / `pool registry set ghcr.io --username READER --group my-lab` / `pool registry remove ghcr.io --group my-lab` |
| Cancel | `pool cancel JOB-ID` |
| Delete a job | `pool jobs delete JOB-ID` |
| Delete / restore a group | `pool groups delete my-lab` / `pool groups deleted` / `pool groups restore my-lab` |
| Usage in a group | `pool groups usage my-lab --days 7` |
| Credits ([§10](#credits-share-to-earn-use-to-spend)) | `pool credits`, `pool credits history`, `pool credits give USER N`, `pool credits ask N --reason TEXT`, `pool credits move N --from G1 --to G2` |

### AI coding agents

Claude Code, Codex, Gemini CLI and other agents that read Agent Skills can use
pool for you. Install the skill in [`skills/`](../skills/README.md). It
teaches the agent the container rules, how to run and wait for a job without
cancelling it by accident, how to collect the files, and how to fix common
errors. Sign in yourself first (`pool login`). The agent never asks for your
token.

## 10. Share your computer

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
narrow them. A group's coordinator can also remove your computer from their
group; your computer's page then says so, and only they can let it back in.

### Credits: share to earn, use to spend

In a group that uses credits, the more you share, the more you can use.

| What happens | Credits |
|---|---|
| Your computer runs someone else's task | You **earn** what they pay |
| Your task runs on someone else's computer | You **pay**: 1 credit for 1 CPU for 1 hour, 1 credit for 4 GiB of memory for 1 hour, 10 for a GPU for 1 hour |
| Your task runs on your own computer | Free |
| A size pool chose was too small, and pool tries again | You pay for the small try at first. When a bigger try works, the small try's charge is reversed: you get it back, and the owner gives back what it earned. |
| A task runs longer than its time limit | Never billed for more than its time limit |
| The computer went away during your task | Nothing for the lost part |
| You join the group | Free **start credits** (20 by default, at most 100) |

When pool chose the size, you pay for what the task used, not for what it
held. A first run, which holds a whole computer's free memory, costs only what
it used.

**Where you see it:** the group's **Credits** tab (your balance and history),
the job page (**Credits**: the expected cost and what it cost), and after
`pool run` (`It cost 1.25 credits.`).

**Out of credits?** Your jobs still run, but only on computers nobody with
credits is waiting for. (A coordinator can choose instead that they wait.) To
get credits:

1. Share a computer: `pool-agent share GROUP`.
2. Ask a member to give you some.
3. Ask the coordinator: **Credits → Ask the coordinator**, or `pool credits ask 50 --reason "thesis experiments"`.

| You want to | In the browser (**Credits** tab) | On the command line |
|---|---|---|
| See your credits | **Your credits** | `pool credits` |
| See what changed | **History** | `pool credits history` |
| Give credits to a member | **Give credits to a member** | `pool credits give bob 10` |
| Ask the coordinator | **Ask the coordinator for credits** | `pool credits ask 50 --reason "..."` |
| Use earned credits in another group | – | `pool credits move 30 --from my-lab --to vision-lab` |

**Free and earned credits.** Start credits, credits from the coordinator and
"earn while waiting" are **free**: they are spent first, and they stay in their
group. **Earned** credits can also move to another group you belong to, but
only if that group's coordinator has named this group under **Settings →
Credits** (none by default). They are converted by the two groups' CPU rates.
Credits you earn from someone who paid with credits they did not have stay in
the group.

## 11. Run a group (coordinators and admins)

### Roles

| Can… | Coordinator | Admin | Member | Contributor | Submitter |
|---|:-:|:-:|:-:|:-:|:-:|
| See the group's jobs, members, computers and usage | ✓ | ✓ | ✓ | ✓ | ✓ |
| Submit jobs | ✓ | ✓ | ✓ | – | ✓ |
| Share a computer with the group | ✓ | ✓ | ✓ | ✓ | – |
| Vote on join requests (when the rule asks members) | ✓ | ✓ | ✓ | – | – |
| Create invite links, read the audit log, cancel or delete anyone's job | ✓ | ✓ | – | – | – |
| Remove a computer from the group, and let it back in | ✓ | ✓ | – | – | – |
| See every member's credits | ✓ | ✓ | – | – | – |
| Give free credits, answer requests for credits | ✓ | – | – | – | – |
| Change settings and roles, remove members, close, delete or restore the group | ✓ | – | – | – | – |

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
- **Fair use:** tab **Usage**, or `pool groups usage my-lab`. When several people's jobs wait, the person who used the least computer time in the last 7 days goes first. With credits on, people with credits go before people without.
- **Credits:** tab **Settings → Credits** turns them on, and sets the start credits (at most 100), what happens when a member has none left (lowest priority, or wait), "earn while waiting", which groups' earned credits members may bring in (none by default), and whether credits earned here may go out. Rates stay close to 1 credit per CPU hour (0.1 to 10). They are on for new Dev team, Student group and Hackathon groups, and off for Research team and Course. Turning them on gives every member their start credits.
- **Free credits:** tab **Credits**. **Give free credits** to a member (not to yourself), with a reason. Under **Requests for credits**, approve a request (all of it, or less) or decline it. On the CLI: `pool credits grant bob 30 --reason "thesis"`, `pool credits requests`, `pool credits approve ID [AMOUNT]`, `pool credits decline ID`. Everything is in the audit log.
- **Records:** tab **Audit log** (coordinators and admins) lists every important action. It is hash-chained, so changes are detectable.
- **Time limit of a first run:** tab **Settings** (coordinator only). The longest a task may run when someone runs an image for the first time and sets no limit. 4 hours by default.
- **Settings and closing:** tab **Settings** (coordinator only). Close the group when the work is done (`pool groups close my-lab --yes`): work stops, everything stays visible.
- **Private images:** tab **Images** → store one read-only token for the group's registry ([§6](#6-private-images)). Members with images in their own account add their own token under **My images** instead.
- **Removing a computer:** tab **Devices** → **Remove** (coordinators and admins). Its work for the group moves to other computers, and it stays out until you choose **Let back in** under "Removed from this group". Its owner sees why on their computer's page.
- **Deleting a job:** **Delete job** on the job page; the submitter can delete their own, coordinators and admins any (`pool jobs delete JOB-ID`). Its tasks, logs and output files are deleted.
- **Deleting the group:** tab **Settings** → **Delete group**, typing its name (`pool groups delete my-lab`). It disappears at once and its work stops. For **7 days** a coordinator or server admin can restore it from **Recently deleted groups** on the Groups page (`pool groups restore my-lab`); after that its jobs, logs and files are purged.
- **What deleting keeps:** the audit log (it cannot be deleted) and, for each computer's owner, a short "what ran here" line per run (group, job, image, time), marked *(deleted)*. Usage records stay, so deleting jobs does not change fair share.

### Server admins

The people listed in `POOL_SERVER_ADMINS` run the server itself. See the
[README](../README.md) and `deploy/`. Two of their actions have no screen yet:

- Mark an organisation-owned, dedicated computer: `PATCH /api/v1/devices/ID` with `{"org_owned_dedicated": true}`. Only such computers run `confidential` jobs.
- Check the audit log's hash chain: `GET /api/v1/audit/verify`.

## 12. When something goes wrong

### "Tasks waiting: why"

The job page explains every waiting job. The same reasons, in short:

| The page says | What to do |
|---|---|
| No computer is shared with this group yet | Someone in the group shares a computer ([§10](#10-share-your-computer)). |
| … shared with this group, but none is online | The owner starts the agent: `pool-agent run`. |
| … the computer's own sharing settings do not include this group | The owner runs `pool-agent share GROUP` on that computer. |
| … online but not available right now | Outside the owner's sharing hours, or they are using it (`--only-when-idle`). Wait, or ask. |
| This job asks for more than any shared computer offers | Lower `resources` in the job, or ask an owner to raise their limit. |
| No available computer can run this job | The job needs something no computer offers (operating system, isolation level, GPU, confidential data) or the computers only accept certain members. |
| … able to run this job | Nothing is wrong; it starts within seconds or when a computer finishes its current work. |
| Its image is built for amd64, and no shared computer is | The group's computers are another kind (e.g. arm64). Build for them: `pool run --platform linux/arm64`, or ask for a computer of that kind. |
| Part 0 runs first, to learn how much … each part needs | A first run: the other parts start when part 0 ends ([§4](#how-pool-chooses-sizes)). |
| You have another first run going | Wait, or set the memory yourself (`memory:` or `-m`). |
| The last try ran out of memory with all the free memory of a computer | The program needs more memory than that computer had. It waits for a computer that offers more. Ask an owner to raise their limit, or make the program use less. |
| Waiting to retry after a failure | The last try failed; see the next table. |

### The job is refused when you submit it

| The message says | What to do |
|---|---|
| `image: ... was not found, or it is private` | Check the name and tag (`docker push` prints them). If the image is private, add a read-only token ([§6](#6-private-images)). |
| `image: ... refused the stored credentials` | The token is wrong or expired: store a new one. |
| `image: cannot reach the registry` | The server cannot reach the registry right now; try again, or ask the server admin. |
| `image: ... is on a private network` | The registry is inside your network; a server admin can allow that (`POOL_REGISTRY_PRIVATE_NETWORKS=true`). |
| `unknown field(s) ...` | A typo in the job file; the message lists the fields a job may use ([§4](#4-write-a-job)). |
| `unknown placeholders: {{seed}}` | The command uses a name that has no values: add it to `sweep`, or use `{{index}}`. |

### `pool run`

| Message | What to do |
|---|---|
| `this server has no built-in registry` | Push your image elsewhere (ghcr.io, Docker Hub) and give it: `pool run IMAGE ...` |
| `pushing the image ... failed: ... unauthorized` | Sign in again: `pool login ...` (the access token expired) |
| `pushing the image ... failed: ... x509: certificate signed by unknown authority` | The server uses its own certificate authority: your Docker must trust it (ask the server admin, [admin guide](admin-guide.md)) |
| `exec format error` in the job's log | The image was built for another kind of computer. pool now sends a job only to computers its image has a version for, so this happens only when pool could not read the image. Build with `--platform` for the group's computers. |
| `pool run does not take -v ...` (or another flag) | The message says why; most often, copy the files into the image in the Dockerfile |

### A task failed

Open the task's log. Lines starting with `agent:` come from the computer that
ran it.

| Log shows | Cause | Fix |
|---|---|---|
| `agent: ... pull access denied`, `manifest unknown` | The computer could not download the image | Check the image name. For a private image, add a token under **My images**, or ask a coordinator for one on the **Images** tab |
| `this image is private: this computer pulled it for another group` | The image belongs to someone else's registry | Use an image you can read, or add a token that can read it ([§6](#6-private-images)) |
| `the server gave no pull token (...)` | The token is missing, expired or cannot read this image | Replace it under **My images** (your own) or on the **Images** tab (the group's) |
| `out of memory: the program needed more than its ... MiB` | The program used more memory than the task asked for | Ask for more memory (`memory:` or `-m`). The job page's **Used** line shows how much it reached. |
| `exit 1` (or another number) and your program's error | Your program failed | Fix the program; try it locally with `pool run --local` |
| Result column shows `max_runtime_exceeded` | The task ran out of time | Raise `time_limit` |
| `agent: outputs kept on the device only` | The server has no working file store | Ask the server admin; results are still in the log |

### The agent

| Message | Fix |
|---|---|
| `server URL must use https` | The server needs HTTPS. Plain http only works for a local dev server with `--insecure-http`. |
| `not enrolled yet` | Run `pool-agent enroll` first. |
| `enrollment code is invalid, used or expired` | Create a new code; a code works once, for 10 minutes. |
| The computer does not appear in the group | Run `pool-agent share GROUP` and make sure `pool-agent run` is running. Your role in the group must allow sharing (not *submitter*). |

## 13. What is not possible yet

- Jobs cannot use the internet, read input files from the server, or use secrets (passwords, tokens). Put small input data into the image.
- Private images work with registries that use token authentication (ghcr.io, Docker Hub and similar), not with registries that only accept a password on every request.
- Checkpoints (resuming a long task where it stopped) are planned.
- Combining results runs on your side (the results table, the merged CSV files, or your own script). A final step that runs on the group's computers after all tasks is not built.
- Apps that listen on a port (web apps, APIs, databases, notebooks) do not work yet, and there is no interactive terminal. Programs that compute and write files work now. Apps are planned ([design](design/pool-run.md)).
- pool chooses sizes from your earlier runs, reads the image (its platforms, CUDA, ports) and places tasks where they fit best, but it does not yet merge test reports or copy slow tasks. See [`design/pool-decides.md`](design/pool-decides.md).
- There are no credits yet: sharing a computer does not give you more use. A design is proposed: [`design/credits.md`](design/credits.md).
- `parts` splits work only for programs that can do part of it (by `POOL_TASK_INDEX`); one program cannot be spread over several computers, since tasks cannot talk to each other.
- GPUs and stronger isolation (gVisor) are built but not tested on real hardware.
- Windows computers are untested; Macs cannot share.
- The agent has no installer yet, and the CLI is not packaged.

See [`docs/STATUS.md`](STATUS.md) for the full status and
[`docs/design/easy-onboarding.md`](design/easy-onboarding.md) for what comes
next.
