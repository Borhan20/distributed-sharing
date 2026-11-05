# pool decides: give it a container, it works out the rest — design proposal

| | |
|---|---|
| **Status** | Accepted on 6 November 2026, with the decisions in [§10](#10-decisions). Nothing here is built yet. |
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
4. **Explain every decision.** For example: "Memory 2 GiB: your last 5 runs of this image used at most 1.4 GiB."
5. **What you give is kept.** pool never changes a value you set. It only tells you when the value looks wrong.
6. **Owners stay in control.** No decision goes beyond what a computer's owner allows.
7. **Simple rules first.** Measurements and rules that a person can check. No black-box model. Machine learning for predictions is a later option, once there is enough data.

## 2. What it is like

**First run of an image.** pool does not know the size yet:

```text
$ pool run ghcr.io/you/simulation:3
pool: Your first run of this image. Running on lab-pc, the computer with the most free memory:
pool:   12 GiB memory, 1 CPU (more when the computer is free), time limit 4 h (the group's default).
...
pool: Done in 12 min. It used 3.1 GiB memory, 3.8 CPUs, 400 MB disk.
pool: Saved for your next runs: 4 CPUs, 4 GiB memory, time limit 40 min.
pool: 3 file(s) saved to pool-out/
```

**Second run.** pool knows it now:

```text
$ pool run ghcr.io/you/simulation:3
pool: Size from your 1 earlier run: 4 CPUs, 4 GiB memory, time limit 40 min.
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
| CPU | `--cpus` | your earlier runs ([§4.3](#43-choose-the-size)) |
| Memory | `-m` | your earlier runs; on your first run, the most a free computer offers |
| Disk | `--disk` | image size plus what your earlier runs wrote |
| Time limit | `--time-limit` | your earlier runs; on your first run, the group's default time limit |
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

**What pool remembers it under.** The numbers are kept **per person**
(decided 6 November 2026). Two people who run the same image may do very
different work with it, so one person's runs never size another person's.

A new version of your code usually needs about the same as the last one. So
pool remembers the numbers under the image name and the command, not under
one exact version. It uses the closest match that has data, always among your
own runs:

1. the same image name, command and arguments,
2. the same image name and command,
3. the same image name.

A general image like `python:3.12-slim` runs very different programs. That is
why the command is part of the match. With no match, it is your first run of
this image.

### 4.3 Choose the size

Every resource gets two numbers, like Kubernetes' "requests" and "limits":

- **Reserved:** what pool counts when it places the task on a computer.
- **Allowed:** the most the container may use.

| Resource | No earlier runs | With earlier runs | Reserved vs allowed |
|---|---|---|---|
| Memory | The most a free computer offers: pool picks the shared computer with the most free memory now, and gives the task all of it, within the owner's limit. On out of memory: the next bigger computer. | Highest use seen × 1.25 | The same. Memory cannot be shared back, so it grows by moving to a bigger computer, never by going over. |
| CPU | Reserve 1 | Average use seen, rounded up | Allowed up to the owner's CPU limit. A program that can use 8 cores gets them when the computer is free, and pool learns that. |
| Disk | Image size + 2 GiB | Highest written × 1.5 + image size | The same. |
| Time limit | The group's default time limit: 4 hours, unless the coordinator changes it on the group's **Settings** tab. Never more than the owner's maximum. A program with no output and no CPU use for 30 min is reported as "probably stuck". | 3 × the longest run seen, at least 30 min | – |

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

**A first run holds a whole computer's free memory.** That is the price of
not asking. To keep it small:

- Only one task per person runs as a first run at a time. A job with parts runs part 0 first ([§4.5](#45-split-the-work)); the others wait for its numbers.
- Other people's tasks still use the computer's free CPU.
- With credits on, a first run costs what it used, not what it held (see [credits](credits.md)).

### 4.4 Choose the computer

Computers ask the server for work. Today a computer gets the first task that
fits, in fair-share order. With this design, it gets the task that fits it
best.

| Rule | Hard or preferred | Why |
|---|---|---|
| Fits: size, OS, platform, GPU, isolation, the owner's rules | hard (as today) | – |
| Fair share between people | first (as today), in bands | Who goes next does not change between people with clearly different use (as built: §11 step D). |
| **Best fit:** a GPU computer gets GPU tasks first; a big computer gets big tasks first | preferred, among near-equals | A small task on the only 64 GiB computer blocks the big job behind it. |
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
| Out of memory, pool chose the size | Docker's `OOMKilled` mark | Tries again on a computer with more free memory ([§4.3](#43-choose-the-size)) | No |
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
| A first run holds a computer's whole free memory | One first run per person at a time; part 0 first for jobs with parts ([§4.3](#43-choose-the-size)). |

## 10. Decisions

Made on 6 November 2026:

| Question | Decision |
|---|---|
| Memory on a first run | The most a free computer offers ([§4.3](#43-choose-the-size)). |
| Time limit when nothing is known | The group's default time limit, 4 hours to start with. The coordinator can change it on the group's **Settings** tab. |
| Who learns from whom | Learned sizes are kept per person ([§4.2](#42-measure-every-run)). |
| Credits | A separate design: [credits.md](credits.md). |

## 11. As built

### Step A: measure (built 6 November 2026)

| Topic | Choice | Why |
|---|---|---|
| Source | `docker stats --format '{{json .}}'`, one reading about every second, for the whole run | The numbers people know. Works the same for Docker on Linux and in WSL2. |
| Memory | The highest reading. Docker leaves out page cache that the kernel can free. | That is the memory the program needs, not the files it read. |
| CPU time | The average over the time the readings cover, times the whole run time | `docker stats` needs 1–2 s before its first reading. Adding up only the readings gave 55% of the real time in tests; the average gives 85–100%. |
| Idle readings | Readings of `0B / 0.00%` are skipped | Docker sends them before the program starts and after it ends. Counting them lowered the average CPU. |
| Short runs | CPU needs two readings; a run with one shows memory and time only. A run under about 2 s may show nothing. | CPU is the change between two readings. Showing "CPU 0" would be wrong. |
| Out of memory | Docker's `OOMKilled` mark, read after the end, and only when the exit code is not 0 | A child process killed while the program still succeeded is not a failed task. |
| Wire | `usage` in the `commit` message (optional) | Older agents send nothing, and nothing changes for them. |
| Trust | The server caps every number at the task's limits and at the time it saw the attempt run. It lists a field as odd only when it was more than 10% over (Docker shows 101% for one busy CPU). | A device may make a run look smaller, never bigger or longer. It can claim "out of memory" for an attempt it failed, as it could already fail any attempt; step B grows sizes at most 3 steps and never past owners' limits. |
| A bad value | A `usage` out of range is dropped; the result itself still counts | Before, the server refused the whole result, and the task could stay "running". |
| Cost | The job's summary is added up by the database | The job page refreshes every few seconds. |
| Where it shows | Job page (**Used** line and column), `pool run`, `pool attach`, `pool status`, `pool run --local` | `--local` measures too, so people learn a size before they ask the group. |

Not built in step A: measuring the agent's own disk writes outside `/out`
(the file system is read-only, and `/tmp` counts as memory), and per-computer
run times for "fast for this image" ([§4.4](#44-choose-the-computer)).

### Step B: size by itself (built 6 November 2026)

| Topic | Choice | Why |
|---|---|---|
| What is "left out" | `cpu`, `memory`, `disk`, `time_limit` missing from the job (form: **Let pool decide**, the default; `pool run` without `--cpus`, `-m`, `--disk`, `--time-limit`) | The job file stays the one input. A size given is firm. |
| History | The person's own measured attempts (newest 200), filtered by image name without tag or digest; the closest of: same command and arguments, same command, same image; at most 10 runs | §10: per person. A general image (`python`) runs many programs, so the command counts. |
| Memory | Highest use × 1.25, in 64 MiB steps. Out-of-memory runs newer than the last good run count as "needs twice that". | A later success shows the real need. |
| CPU | Average use × 1.2, in half CPUs; twice the last run's CPU if it used 85% or more of it | Burst above the reserved CPU (§4.3) is not built, so pool learns from the next run instead. |
| Time limit | 3 × the longest run, at least 30 min, at most 7 days. First run: the group's `default_time_limit_s` (4 h; **Settings** tab, coordinator) | §10. |
| Disk | Image + 1.5 × the largest output, at least 1 GiB; first run 1 GiB | Disk only decides where a task fits; the file system is read-only. A first run asked for 2 GiB at first, more than a small share offers, so it never started (found by the e2e test). |
| First run | The job holds a 256 MiB placeholder for matching; each attempt gets the computer's whole free memory. For 60 s the job only goes to a computer with at least 75% of the most memory any online computer offers. | §10: "the most a free computer offers". After a minute any computer will do, so a busy big computer cannot block it. |
| One first run per person | A first-run job is skipped while the person has another first-run attempt assigned or running | A first run holds a whole computer. |
| Part 0 first | While the job is a first run, only task 0 is offered. When it ends (not out of memory or time), the job's sizes are set from it. | §4.5. Before, it waited only until the end, not "after a minute": measuring while it runs would need usage in heartbeats. |
| Growing | Out of memory or out of time on a size pool chose: memory × 2 (a first run: a computer with more free memory than it had), time × 2; at most 3 steps. The attempt ends as `preempted` (`oom_resized`, `timeout_resized`), the task's failures do not change, and its usage row is `pool_paid` (left out of fair share). | §3 rule 3. |
| Each attempt's size | Stored on the attempt (`attempts.resources`) and put into the signed task envelope | The job's sizes can change while it runs; the device only ever gets concrete numbers. |
| Reasons | `jobs.sizing.reasons`: one line per size at submit, more lines when pool grows or learns. `decided` marks where the later ones start. | §4.9. Shown by `pool run`, `pool submit`, `pool attach` and the job page. |

**Changed after the security review of step B:**

| Problem found | Now |
|---|---|
| Part 0 of a first run failed without a result (e.g. its image could not be pulled): the other parts waited for ever, and such jobs took the scheduler's slots | Once part 0 has ended in any way, the other parts may start (as first runs). A job is only ranked when it has a task it may start now. |
| A run with one reading has no CPU figure, and sizing crashed on it (submit gave an error, the device's connection dropped) | CPU comes only from runs with a figure. Sizing errors are logged and never cost a result. One message the server cannot handle no longer drops the device's connection. |
| A first run took the free memory the device claimed | It gets at most the device's share for that group, minus what that group's tasks already hold there. A device whose share is too small is skipped. |
| A dishonest device could make sizes huge | Learned sizes never go above the biggest computer shared with the group. Runs with capped numbers are ignored, and so is a device whose runs peaked more than 2 × above every other device. Image sizes are capped at 1 TiB. |
| Tries pool caused were left out of fair share at once, so timing out on purpose avoided fair share | They count until a try with the bigger size succeeds; then they leave fair share. Cancelled runs now count too. |
| Parts that ran out of memory together used up the growth steps | A try with a size already grown by another part counts as pool's, without a step. |
| Sizing reasons showed numbers from the submitter's other groups to everyone | Only the submitter and the group's coordinators and admins see the reasons. |
| An explicit time limit of 0, a negative one, or huge sizes gave a server error | They are refused with a plain message (1 s to 365 days; at most 4096 CPUs, 1024 TiB). |

Not built in step B: CPU above the reserved amount (needs one parent control
group per computer), "stuck" detection, `--dry-run`, growing the disk, and an
owner's maximum time limit (the agent has none yet; pool's own limits stop at
7 days). "One first run per person" can let two through when two computers ask
at the same moment.

### Step C: read the image (built 7 November 2026)

| Topic | Choice | Why |
|---|---|---|
| What is read | Platforms (from the index; attestation entries left out), CUDA (`CUDA_VERSION` or `NVIDIA_REQUIRE_CUDA` in the config's environment, as NVIDIA's CUDA images set them, checked on `nvidia/cuda:12.4.1-base-ubuntu22.04`), `EXPOSE` ports, health check, compressed size | §4.1. Values of environment variables are never kept. |
| How | At submit, after the tag is pinned: the manifest (and, for an index, the linux/amd64 one) and the config, each checked against its digest | Content-addressed: whoever served it, the bytes must hash to the digest. |
| Blobs on a CDN | One redirect is followed, only to a public `https` address, without the registry's token | Docker Hub sends config files from its CDN (a 307 to another host). The client still never follows other cross-host redirects. |
| Cost | Once per digest (`image_infos` table) | Docker Hub counts manifest reads against its pull limit. |
| Failure | Best effort: the job is submitted without it, and runs on any computer | A registry hiccup must not refuse a job. |
| Platforms | `jobs.architectures`: the image's CPU types for the job's OS; the scheduler sends tasks only to computers of those types | Stops "exec format error". |
| `pool run --platform` | Default: the kind most of the group's computers are (`GET /groups/{name}/platforms`); with `--local`, this computer's own | Before, always `linux/amd64`. |
| GPU | A CUDA image with no `gpu` given asks for 1 GPU when a computer in the group shares one; otherwise it runs on the CPU with a reason | A GPU nobody has would make it wait for ever. GPUs are still untested on real hardware. |
| Ports | A reason line: "Apps that listen on a port do not work yet" | Apps come with step 2 of `pool run`. |
| Big images | A first run's disk: about 3 × the compressed size | Unpacked layers are bigger than the download. |

**Changed after the security review of step C:**

| Problem found | Now |
|---|---|
| A registry that sends its answer very slowly could hold the server's threads and database connections (any member could point a job at one) | Each lookup has one deadline (20 s to read an image, 30 s to pin a tag), checked as each piece arrives. At most 4 image reads run at once; when busy, the read is skipped. The database connection is released before the registry is asked. |
| Some broken answers still refused a job with a server error | Any failure while reading is logged, and the job is submitted without the image's information. |
| Values read from an image were not checked | Platforms must look like `linux/amd64` (at most 32), ports like `8000/tcp`, sizes must be whole numbers; JSON with `Infinity` or deep nesting is refused; child digests are checked before they are fetched. |
| A computer's CPU type reached members' terminals and suggested commands as sent | It must look like `amd64` (letters, digits, `_`, at most 16), and `pool run` only uses and prints platform names of that form. |
| The cache was keyed by digest alone | It is keyed by registry, repository and digest. |
| Reading a config from the group's own registry followed redirects with its token | One hop, without the token, and the config is checked against its digest. |

Docker Hub counts manifest reads against the stored account's pull allowance;
pool reads each image once (the cache), and the computers' pulls use the same
allowance.

Not built in step C: using the health check (it matters for apps), and
checking that an image's architecture matches a computer's CPU variant
(arm/v6 vs arm/v7); the scheduler compares the CPU type only.

### Step D: place better (built 7 November 2026)

Computers ask for work, so pool picks the best task for the computer that asks:

| Order | Rule | Why |
|---|---|---|
| 1 | The owner's group priority | As before. |
| 2 | Members with credits before members without (credits §3.8) | As before. |
| 3 | On a computer with a GPU: GPU work first | A GPU waiting behind CPU work is wasted. GPU time counts in fair share (1 GPU hour = 10 CPU hours), so asking for a GPU has a price. |
| 4 | Fair share in bands that double: under 1 hour of use, 1–3, 3–7, … | Fair share stays first between people with clearly different use. |
| 5 | Inside a band: work that needs more than half of this computer's free memory first | A small task on the only big computer blocks the big job behind it. Only among near-equals, so asking for more memory never jumps someone who used much less. |
| 6 | Exact fair share | As before. |
| 7 | Work whose image this computer already has | The agent sends the digests of up to 100 images it started recently with each request for work. No download, so it starts at once. |
| 8 | On a computer that often loses tasks (reliability under 0.5): short work first | Fewer long tasks have to move. |
| 9 | The oldest first | As before. |

The scheduler looks at 10 jobs at a time and up to 5 pages: jobs this computer
cannot start now (with "wait", ones the member cannot pay an hour of; first
runs without room) no longer leave it without work.

**Changed after the security review of step D:** at first, rules 3 and 5 came
before fair share, and "big" was measured against this computer's free memory.
A member could then jump the queue by asking for more memory than needed, and
a "size ladder" of jobs took almost all of a computer from someone who had used
much less. Also, 10 jobs nobody could pay for left a computer without work, an
invalid image entry made the server refuse a computer's request for work, an
image counted as "already there" only after its task ended, and a huge memory
size overflowed a calculation. All are fixed and tested.

Not built in step D: "likely to stay online" from a computer's online history,
and "fast for this image" from measured run times per computer.

