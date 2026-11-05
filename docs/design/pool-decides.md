# pool decides: give it a container, it works out the rest — design proposal

| | |
|---|---|
| **Status** | Proposal. Not agreed yet. Nothing here is built. |
| **Date** | 6 November 2026 |
| **Builds on** | [`pool run`](pool-run.md) (step 1 built), plan §7.7 (scheduler and fair share) |

## 1. Goal

You give pool one thing: a container. pool works out everything else.

- **You give:** an image, or a folder with a `Dockerfile` or a `compose.yaml`.
- **pool decides:** CPU, memory, disk, time limit, ports, which computer, how to split the work, what to do when it fails, and how to put the results together.
- **pool explains** each decision in one line. You can change any decision with a flag.

Most people do not know how much memory or time their program needs. They
should not have to. pool measures it, learns it and uses it next time.

It should feel like your own computer: `docker run IMAGE` becomes
`pool run IMAGE`.

### Rules for how pool decides

1. **One input.** Only the image is required. Everything else is optional.
2. **Measure, do not guess.** Sizes come from real runs, not from forms.
3. **A wrong guess is pool's problem, not yours.** When a run fails because pool chose too little, pool tries again with more. That retry does not count as your failure, and its time does not count against your fair share.
4. **Explain every decision.** For example: "Memory 2 GiB: the last 5 runs of this image used at most 1.4 GiB."
5. **What you give is kept.** pool never changes a value you set. It only tells you when the value looks wrong.
6. **Owners stay in control.** No decision goes beyond what a computer's owner allows.
7. **Simple rules first.** Measurements and rules that a person can check. No black-box model. Machine learning for predictions is a later option, once there is enough data.

## 2. What it is like

**First run of an image.** pool does not know the size yet:

```text
$ pool run ghcr.io/you/simulation:3
pool: First run of this image. Starting with 1 CPU (more if the computer is free), 2 GiB memory.
pool: Running on lab-pc.
...
pool: Out of memory at 2 GiB. Trying again with 4 GiB on lab-pc-2. This does not count as a failure.
...
pool: Done in 12 min. It used 3.1 GiB memory, 3.8 CPUs, 400 MB disk.
pool: Saved for next time: 4 CPUs, 4 GiB memory, time limit 40 min.
pool: 3 file(s) saved to pool-out/
```

**Second run.** pool knows it now:

```text
$ pool run ghcr.io/you/simulation:3
pool: Size from 1 earlier run: 4 CPUs, 4 GiB memory, time limit 40 min.
pool: Running on lab-pc (it has this image already, so it starts at once).
```

**An app.** The image's `Dockerfile` has `EXPOSE 3000` and `EXPOSE 8000`:

```text
$ cd my-shop
$ pool run
pool: The image opens ports 3000 and 8000: running it as an app until you stop it.
pool: Running on lab-pc. Only you can open it:
pool:   http://localhost:3000
pool:   http://localhost:8000
```

**Many parts.** The program reads `POOL_TASK_INDEX` and `POOL_TASK_COUNT`:

```text
$ pool run --parts auto
pool: Room for 14 parts at once on 5 computers: splitting into 14.
pool: Starting part 0 first, to learn its size.
pool: Part 0 uses 1 CPU and 600 MB. Starting the other 13.
...
pool: 14 of 14 done in 9 min. Part 6 was slow on lab-old; a copy on lab-pc-2 finished first.
```

## 3. What you give, and what pool decides

| Setting | You may give | If you do not, pool decides from |
|---|---|---|
| Image | required | – |
| Command, arguments | `--entrypoint`, words after the image | the image's own command |
| CPU | `--cpus` | earlier runs ([§4.3](#43-choose-the-size)) |
| Memory | `-m` | earlier runs; on a first run, a start size that grows |
| Disk | `--disk` | image size plus what earlier runs wrote |
| Time limit | `--time-limit` | earlier runs; on a first run, the owner's maximum |
| GPU | `--gpus` | whether the image is built for NVIDIA CUDA ([§4.1](#41-read-the-image-before-it-runs)) |
| Ports | `-p` | the image's `EXPOSE` lines, or `ports:` in `compose.yaml` |
| Program or app | – | with ports: an app, runs until you stop it. Without: a program, runs until it ends. |
| Platform (amd64, arm64) | `--platform` | the group's computers (they already report it) |
| Which computer | never | placement rules ([§4.4](#44-choose-the-computer)) |
| Number of parts | `--parts N` | room on the group's computers and the size of one part ([§4.5](#45-split-the-work)) |
| Retries | `--retries` | the kind of failure ([§4.7](#47-failures-find-the-cause-then-act)) |

When you give a value, it is firm. If it looks wrong, pool says so:
*"You asked for 8 GiB; it used 600 MB. Asking for less finds a computer
sooner."*

## 4. How pool decides

### 4.1 Read the image (before it runs)

pool reads the image's settings from the registry. No run is needed.

| In the image | What pool learns |
|---|---|
| `EXPOSE 8000` | It is an app. Open port 8000. |
| Built on an NVIDIA CUDA image | It probably needs a GPU. (To check: which settings the CUDA base images carry, so the test is reliable.) |
| `HEALTHCHECK` | How to tell when an app is ready. |
| Size of its layers | Disk needed, and how long the download takes. |
| Architecture (amd64, arm64) | Which computers can run it. |
| `ENTRYPOINT`, `CMD`, `WORKDIR` | How it starts. |

The image comes from the user, so pool treats all of this as untrusted data.
Nothing in an image can raise a limit above what an owner allows.

### 4.2 Measure every run

While a task runs, the agent on the computer reads the container's own
counters (the numbers `docker stats` shows). It sends them with the result:

| Measured | Used for |
|---|---|
| Highest memory use | memory next time |
| CPU used: average and highest, in cores | CPU next time |
| Disk written (`/out`, `/tmp`) plus image size | disk next time |
| Run time | time limit, waiting time estimates, splitting |
| How it ended: exit code, out of memory (Docker marks this as `OOMKilled`), out of time, disk full | what to do next ([§4.7](#47-failures-find-the-cause-then-act)) |

To check on the test computer: whether the kernel's "highest memory" counter
is there (newer Linux kernels have one), or whether the agent must sample
every few seconds.

**What pool remembers it under.** A new version of your code usually needs
about the same as the last one. So pool remembers the numbers under the image
name and the command, not under one exact version. It uses the closest match
that has data:

1. the same image name, command and arguments,
2. the same image name and command,
3. the same image name.

A general image like `python:3.12-slim` runs very different programs. That is
why the command is part of the match.

### 4.3 Choose the size

Every resource gets two numbers, like Kubernetes' "requests" and "limits":

- **Reserved:** what pool counts when it places the task on a computer.
- **Allowed:** the most the container may use.

| Resource | No earlier runs | With earlier runs | Reserved vs allowed |
|---|---|---|---|
| Memory | 2 GiB. On out of memory: double it and try again. | Highest use seen × 1.25 | The same. Memory cannot be shared back, so it grows by trying again, never by going over. |
| CPU | Reserve 1 | Average use seen, rounded up | Allowed up to the owner's CPU limit. A program that can use 8 cores gets them when the computer is free, and pool learns that. |
| Disk | Image size + 2 GiB | Highest written × 1.5 + image size | The same. |
| Time limit | The owner's maximum. A program with no output and no CPU use for 30 min is reported as "probably stuck". | 3 × the longest run seen, at least 30 min | – |

These numbers are starting points. They will be tuned with real runs.

**CPU above the reserved amount needs one change on the agent.** All of pool's
containers on a computer must share one parent control group with the owner's
CPU limit. Then the owner's limit holds for all of them together, however much
each one uses. (Docker's `--cgroup-parent` does this. To check on the test
computer.) Until then, allowed CPU stays equal to reserved CPU. pool then
learns from the run: a program that used all its CPU the whole time gets more
next time.

**Growth stops.** At most 3 automatic size steps, and never above the biggest
computer in the group that is shared now. After that, pool stops and says
what the program needs and which computer comes closest.

### 4.4 Choose the computer

Computers ask the server for work. Today a computer gets the first task that
fits, in fair-share order. With this design, it gets the task that fits it
best.

| Rule | Hard or preferred | Why |
|---|---|---|
| Fits: size, OS, platform, GPU, isolation, the owner's rules | hard (as today) | – |
| Fair share between people | first (as today) | Who goes next does not change. |
| **Best fit:** a GPU computer gets GPU tasks first; a big computer gets big tasks first | preferred | A small task on the only 64 GiB computer blocks the big job behind it. |
| **Image already there** | preferred | No download, so it starts in seconds. |
| **Likely to stay online** for the task's expected time (from the computer's online history) | preferred | Fewer tasks have to move. |
| **Reliable:** few failed or lost tasks (already counted today) | preferred | Fewer retries. |
| **Fast for this image** (run times measured per computer) | preferred | Shorter jobs. |

For "image already there", each computer tells the server which pool images
it holds when it asks for work.

### 4.5 Split the work

pool can only split a program that reads `POOL_TASK_INDEX` and
`POOL_TASK_COUNT` and does its own share. pool cannot split a program that
does not. It runs that one once, on a computer big enough.

For a program that can be split:

1. **How many parts:** as many as fit at once on the computers that are available now. With known run times: fewer parts when each part would take under a minute, because starting a task costs time too.
2. **Try one part first.** On a first run, part 0 starts alone. When it has run for a minute, or ended, the other parts start with its measured size. If part 0 runs out of memory, only one part has to be redone, not all of them.
3. **Sweeps** work the same way: one combination of values first.

### 4.6 Slow and stuck tasks

- **Slow task:** most parts are done, and one has run twice as long as the typical part. pool starts a copy on a free computer. The first to finish wins, and the other is stopped. pool does this only on computers nobody else is waiting for.
- **Stuck program:** no output and no CPU use for 30 minutes. pool tells you. It stops the task 30 minutes later, unless you say to keep it. Apps are not checked this way, because an app that waits for requests is normal.

### 4.7 Failures: find the cause, then act

| What happened | How pool knows | What pool does | Your failure? |
|---|---|---|---|
| Out of memory, pool chose the size | Docker's `OOMKilled` mark | Tries again with twice the memory ([§4.3](#43-choose-the-size)) | No |
| Out of memory, you gave `-m` | Docker's `OOMKilled` mark | Stops. Says how much it reached. | Yes |
| Out of time, pool chose the limit | time limit reached | Tries again with twice the time, up to the owner's maximum | No |
| Disk full | "No space left on device" | Tries again with more disk | No |
| Image cannot be downloaded | the agent's pull error | Stops. Says the fix (name, token). | – |
| Built for the wrong platform | "exec format error" | Runs it only on computers of the image's platform | No |
| The computer went away | its lease ran out | Moves the task to another computer (as today) | No |
| Fails on one computer, works on others | per-computer history | Lowers that computer's score. Tells its owner if it keeps happening. | No |
| The program itself fails | the same error on 2 computers | Stops. Shows the error and a hint (read-only file system, no network, ...). | Yes |

### 4.8 Put the results together

What exists today stays: the results table from `result.json`, merged CSV
files, one zip, logs. New:

- **Test report.** Every `junit.xml` in `/out` is merged into one report: tests passed and failed, the slowest tests, and each failing test with its part and log. Most test tools can write JUnit XML (pytest, Maven, Gradle, Node's test runner, and others through plugins).
- **Run summary** on the job page and as `summary.md` in the zip:
  - what ran where, and for how long,
  - the size asked for (or chosen) next to the size used,
  - every retry and its reason,
  - what pool learned for next time.
- **Waiting time.** With known run times, pool shows how long a job will probably take: "about 25 min: 40 parts of 5 min, 8 at once."

### 4.9 See and change each decision

- Each decision is one line, in the terminal and on the job page.
- A flag overrides any decision.
- `pool run --dry-run` shows the decisions without running anything: size, ports, the computers that fit, and the expected wait.

## 5. Every kind of container

| Kind | Example | You type | pool decides | You get back | Works today? |
|---|---|---|---|---|---|
| Script or batch program | data clean-up, a report | `pool run` | size, computer | output, files | Yes. You set sizes by hand for now. |
| Training run | PyTorch, scikit-learn | `pool run` | size, GPU | model files, metrics | Yes. GPUs are untested. |
| Many runs with other seeds or settings | Monte Carlo, hyperparameter search | `pool run --parts auto`, or `sweep:` in a job file | number of parts, size | results table | Yes |
| Many files or items | images, documents, student submissions | `pool run --parts auto` | number of parts | files per part, merged CSV | Yes |
| Test suite | pytest, JUnit, Jest, Go tests | `pool run --parts 4` | size | one merged test report | Yes. The merged report is new. |
| Compile or build | a large C++ build | `pool run` | CPU | build output in `/out` | Yes |
| Rendering and media | 3D frames, video conversion | `pool run --parts auto` | number of parts | frames, files | Yes, with your own image |
| Statistics | R, Julia | `pool run` | size | tables, plots | Yes |
| Web API or backend | FastAPI, Spring Boot, Express, Go | `pool run` | ports from `EXPOSE` | `localhost:PORT` | Step 2 of `pool run` |
| Web app with a UI | Django, Rails, Next.js | `pool run` | ports | your browser at `localhost` | Step 2 |
| Frontend + backend + database | `compose.yaml` with web, api, db | `pool run` | one computer for all, the ports | `localhost` ports | Step 3 |
| A database to test against | PostgreSQL, Redis | `pool run postgres:17` | port 5432, stronger isolation | `localhost:5432` | Step 2 |
| Notebook | Jupyter | `pool run` | port 8888, GPU | your browser | Step 2 |
| Model server | an inference server, with the model inside the image | `pool run` | port, GPU | an API at `localhost` | Step 2 |
| End-to-end UI tests | Playwright against the stack | `pool test` | – | test report, screenshots | Step 3 |
| Desktop app | Qt, Tkinter | `pool run --desktop` | – | its window in your browser | Step 3b |
| Interactive shell | `pool run -it IMAGE sh` | – | – | a terminal | Step 2 |

**Not possible, by design:**

| Not possible | Why |
|---|---|
| Internet while it runs | Protects owners' networks. Install everything in the image. |
| One process on several computers (MPI, multi-computer training) | Tasks cannot talk to each other. |
| Privileged containers (Docker in Docker, Kubernetes in Docker) | They would control the owner's computer. |
| Host devices (USB, cameras) | Not on the borrowed computer. |
| Apps that run only on Windows or macOS | Members' computers run Linux containers. |
| Hosting for the public | Members' computers come and go. |

## 6. Examples

Examples that run today are in [`examples/run/`](../../examples/run). Each one runs with plain
`pool run`. The examples for apps (APIs in several languages, a web +
database stack, Jupyter, a model server, Playwright tests) come with step 2
and step 3 of `pool run`. Each one is run for real before it is added.

## 7. What gets built

| Part | New |
|---|---|
| Agent | Measure each run and send it with the result. Report the pool images it holds. One parent control group for CPU above the reserved amount. |
| Protocol | Usage numbers in the `commit` message. Images held, in the `ready` message. Reserved and allowed CPU in the task's resources. |
| Server | Read the image at submit. Remember sizes per image and command. Size rules. Part 0 first. Try again with more. Placement scores. Copies of slow tasks. Failure kinds. Run summary. Merged test report. |
| `pool` command | All sizes optional. `--parts`. `--dry-run`. One line per decision. |
| Web | Size in the form defaults to "Let pool decide". Each decision with its reason on the job page. |
| Docs | User guide: "You only need the image." Examples for every kind. The agent skill ([`skills/pool`](../../skills/pool)). |

## 8. Delivery order

| Step | Scope | Done when (measured, with tests) |
|---|---|---|
| A. Measure | The agent measures every run. The job page shows size asked next to size used. | Measured memory and CPU are within 10% of `docker stats` for the test programs. |
| B. Size by itself | Sizes from earlier runs. Try again with more after out of memory or out of time. | A program that needs 3 GiB, started with no size, ends by itself. The second run starts with the right size. |
| C. Read the image | Ports, GPU and platform from the image. | An image with `EXPOSE` runs as an app. This needs step 2 of `pool run`. |
| D. Place better | Best fit, image already there, reliable and fast computers. | In a test with one big and one small computer, a big task is not blocked by small ones. Tasks go to computers that have the image. |
| E. Organize | Part 0 first, copies of slow tasks, merged test report, run summary, waiting time. | A 20-part job with one slow computer ends sooner than without copies. Test reports from 4 parts become one. |

Steps A and B can go before or with step 2 of `pool run`. Step C needs it.

## 9. Needs a human review (security and fairness)

| Risk | Answer |
|---|---|
| Someone fails on purpose to grow their size and take a whole computer | Growth only after a real out-of-memory mark. At most 3 steps. Never above the owner's limit. |
| CPU above the reserved amount goes past the owner's limit | One parent control group per computer with the owner's limit. Tested on the test computer before it ships. |
| A dishonest agent reports false numbers | Sizes use the middle value over several computers. Odd values from one computer are ignored. |
| Image settings are untrusted | They only choose within the owner's limits. They never widen them. |
| Retries on pool's wrong guess cost the group time | They do not count against you, but they do count in the group's usage, so owners see it. |

## 10. Open questions

1. **Start memory on a first run:** 2 GiB, or the most a free computer offers? More means fewer retries, but fewer computers fit.
2. **Time limit when nothing is known:** the owner's maximum, or a group default (for example 4 h)?
3. **Who learns from whom:** are the learned sizes shared by everyone in the group, or kept per person?
4. **Credits** (earn by sharing, spend by using) are a separate design. Its open questions are still waiting for an answer.
