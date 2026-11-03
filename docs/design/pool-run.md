# Your app on the group's computers (`pool run`): design proposal

| | |
|---|---|
| **Status** | Proposed, for review |
| **Date** | 3 November 2026 |
| **Plan links** | Use cases #6, #9 and #10 (plan §2.3), services (§7.10), Phase 3. Deviates from the plan's network design for this case, see [§8](#8-deviation-from-the-plan) |

## 1. Goal

Run what you already run on your own computer (an app in any framework, a
training script, a whole stack from a Compose file) the same way, with `pool`
in front. It runs on a computer of your group that has the CPU, memory or GPU
you lack, and it feels like your own machine:

- the app's ports open at `localhost` on your computer,
- its output streams to your terminal, and Ctrl-C stops it,
- the files it writes come back to your folder.

The YAML jobs (`parts`, `sweep`, results in one zip) stay as they are. This is a
second way in, for programs you work with interactively.

### Decisions already made (3 November 2026)

- **Only the person who starts an app can open it.** Not the group, not the computer's owner, not the internet.
- **The environment comes from your Dockerfile.** Compose files are supported too, for apps that are a full stack (web, database, cache, worker).
- **Order:** a single app with a port first, then Compose, then several copies (replicas). Step 1 below lays the base for these steps.
- **Strong isolation for apps:** apps and stacks run under gVisor (T2), so images that start as root (PostgreSQL, Redis) work unchanged and still cannot touch the owner's kernel ([§3.5](#35-compose-stacks)).
- **Registry clean-up after 30 days unused**, adjustable by the server admin and per group ([§3.1](#31-images-built-on-your-computer-kept-by-the-server)).
- **Apps must be testable, with or without a UI** ([§2.7](#27-testing-an-app-with-or-without-a-ui)).
- **One job, the same everywhere:** a job from the browser form, from a job file (`pool submit`) and from `pool run` is the same job, behaves like `docker run` on your own computer, and everything it shows (output, files, ports) is visible from your computer ([§2.0](#20-one-job-three-ways-to-start-it)).

### What it cannot do

| Not possible | Why | Instead |
|---|---|---|
| One process using several computers' CPU, memory or GPU | Another computer's memory is 1,000–10,000 times slower to reach than your own | Each copy runs on one computer; scale with replicas (stateless apps) or `parts`/`sweep` |
| Hosting an app for the public | Members' computers come and go, and owners' machines must never be exposed | Use it yourself, through pool |
| Internet access from the app while it runs | Tasks have no network, to protect owners' networks | Put dependencies and models in the image; data inputs are a separate design |
| Replicas that share in-memory state or a local SQLite file | Copies run on different computers | A Compose stack with its own database, on one computer |
| Keeping a database's data after the app stops (v1) | Volumes live on the borrowed computer and are deleted with the app | Export what you need to `/out` |
| Mobile apps in an emulator | The Android emulator needs hardware virtualization (KVM), which the sandbox must not give | Test on your own device or emulator, against the app's API through pool |
| Load tests through the tunnel | The tunnel is for one person using the app | Run the load generator (e.g. k6) as a service in the stack |

## 2. What it is like

### 2.0 One job, three ways to start it

The YAML jobs of the user guide and `pool run` are not two systems. There is
one kind of job, and three ways to start it:

| Start it from | Good for | Then |
|---|---|---|
| The browser form | No terminal needed | The job page: output, results, Download all |
| A job file: `pool submit job.yaml` | Saving and sharing a job, sweeps | The same job page |
| `pool run`, with `docker run`'s flags | Working on your own code, apps | Stays attached: live output, ports at `localhost`, files into your folder |

A job file gets one new field, `port:` (and later `replicas:`), so a job from
the browser or from `pool submit` can be an app too. `pool.yaml` in a project
is just such a job file.

**Any job can be attached from your terminal**, wherever it was started:
`pool attach JOB` streams its output, opens its ports at `localhost`, and saves
its `/out` files when it ends. The job page shows this command for every app.

**The same flags as `docker run`.** If it runs with `docker run` on your
computer, you replace `docker` with `pool`:

| `docker run ...` | `pool run ...` | Note |
|---|---|---|
| `IMAGE COMMAND` | same | A tag is pinned when you submit, as for every job |
| `--build .` (`pool` only) | builds your Dockerfile | §3.1 |
| `-p 8000:8000` | same | The app's port 8000 at your `localhost:8000` |
| `-e KEY=value`, `--env-file .env` | same | Values reach the computer; not for real secrets (§3.8) |
| `--gpus 1` | same | Plus `--cpu`, `--memory`, `--disk` for the size |
| `-w /app`, `--entrypoint` | same | |
| `-it` | later | An interactive terminal needs the tunnel of step 2 |
| `-v ./data:/data` | refused | Your folder is not on that computer: put the files in the image (data inputs come later) |
| `--network host`, `--privileged`, `--cap-add`, `--device`, `-v /var/run/docker.sock:...` | refused | They would open the owner's computer; the message says why |

`docker compose up` becomes `pool up` the same way (§2.3).

**Check it on your own computer first: `pool run --local`.** It runs the same
job with your own Docker and pool's rules: no network, read-only file system,
an unprivileged user, `/out` as the only writable folder, and the size limits.
Something that would fail on a member's computer fails here first, with the
same message, before you wait for a computer:

```text
$ pool run --local -- python train.py
[app] OSError: [Errno 30] Read-only file system: '/app/cache'
pool: the container's file system is read-only. Write to /out (kept) or /tmp (scratch).
```

### 2.1 A script or a training run

```text
$ cd my-training
$ pool run --gpu 1 --memory 16Gi -- python train.py --epochs 20
Building the image (Dockerfile) ... done, 1 layer changed (2 MB uploaded)
Running on lab-gpu (RTX 3060, 12 GiB), group ml-lab
epoch 1/20  loss 2.31
...
Done in 41 min. 3 files saved to ./pool-out/  (model.pt, metrics.json, log.txt)
```

- **Ctrl-C** stops it, after asking once.
- **`--detach`** leaves it running; `pool attach APP` shows the output again.
- A second run uploads only what changed: Docker sends only the image layers that changed, which for a code edit is the small layer with your code.

### 2.2 A web app, any framework

```text
$ cd my-api
$ pool run --port 8000 --cpu 4 --memory 8Gi -- uvicorn main:app --host 0.0.0.0 --port 8000
Running on lab-pc (8 CPU, 32 GiB). Open http://localhost:8000  (only you can reach it)
[app] INFO:     Uvicorn running on http://0.0.0.0:8000
[app] INFO:     127.0.0.1 - "GET /docs HTTP/1.1" 200
```

The same works for any app that listens on a port: `npm start` (port 3000),
`java -jar app.jar`, `python manage.py runserver 0.0.0.0:8000`, Streamlit,
Gradio, Jupyter or TensorBoard. pool does not need to know the framework. It
forwards TCP connections, so HTTP, WebSockets and gRPC all work.

The app must listen on `0.0.0.0` (all addresses) inside the container, not on
`127.0.0.1`, or pool cannot reach it. `pool run` checks this and says so.

### 2.3 A full stack from a Compose file

```text
$ cd my-shop          # compose.yaml: web (build: .), db (postgres), cache (redis)
$ pool up
Building web ... done
Running the stack on lab-pc (web, db, cache). Open http://localhost:8080  (web)
[db]    database system is ready to accept connections
[web]   Listening on 0.0.0.0:8080
```

- All services run together on one computer, on a private network where they find each other by service name (`db:5432`).
- Only the ports listed under `ports:` are forwarded to your computer. The rest stay inside the stack.
- `pool up -d`, `pool logs web`, `pool down` work like their Docker Compose counterparts.

### 2.4 Saving the settings

`pool run` without flags reads `pool.yaml` in the project folder. It uses the
same short form as job files, plus `port` and `replicas`:

```yaml
name: my-api
command: uvicorn main:app --host 0.0.0.0 --port 8000
port: 8000
cpu: 4
memory: 8Gi
time_limit: 8h
group: ml-lab
```

### 2.5 When the owner needs their computer back

```text
lab-pc's owner needs it back. Moving the app to lab-pc-2 ... running again. http://localhost:8000 still works.
```

The app restarts on another computer, and connections that were open drop.
Volumes do not move in v1, so a Compose stack's database starts empty again,
and `pool up` warns about this before it starts.

### 2.6 In the browser

The job page lists your running apps with their computer, time left, a
**Stop** button and the command that reopens the ports (`pool attach APP`).
Opening an app directly in the browser through the server needs a separate
address per app (a domain name with wildcard DNS). That comes later.

### 2.7 Testing an app, with or without a UI

The app runs on a member's computer; the server only passes the connection on.
pool does not need to know whether a program has a UI: the flags say what to
open.

| Program | Start it | Try it yourself | Test it automatically |
|---|---|---|---|
| Script or training, no UI | `pool run -- python train.py` | Output in your terminal | Exit code; reports in `/out` |
| API or backend (REST, gRPC, WebSocket) | `pool run --port 8000 -- ...` | `curl`, Postman or a browser at `localhost:8000` | Your tests on your computer against `localhost:8000`, or a test service in the stack |
| Web app with a UI | `pool run --port 3000 -- ...` or `pool up` | Your own browser at `http://localhost:3000` | A `tests` service in the stack (Playwright, Cypress, Selenium); screenshots, videos and reports come back from `/out` |
| Desktop GUI app (Qt, Tkinter, Electron, Java Swing) | `pool run --desktop -- python app.py` | Its window in your browser at `localhost:6080`: a virtual screen you click and type in | Headless UI tests on the same virtual screen; screenshots come back |

**`pool test`** starts a stack, runs its `tests` service against it, and
reports pass or fail with the service's exit code, like `docker compose up
--exit-code-from tests`. The reports land in `./pool-out/`. Large test suites
can still be split over computers with `parts`.

**`--desktop`** adds a virtual screen to the app's container network: a
sidecar container with Xvfb, a VNC server and noVNC (a VNC client in the
browser), whose port 6080 is forwarded like any other. The app draws on that
screen (`DISPLAY` is set), so its image needs no extra packages. There is no
GPU-accelerated graphics; drawing is done in software.

## 3. How it works

```text
your computer                      pool server                         a member's computer
-------------                      -----------                         -------------------
pool run ── docker build (yours) ─► built-in registry ◄── pull (token) ── agent
localhost:8000 ◄── tunnel (WebSocket) ── relay ── tunnel (WebSocket) ──► agent ──► app:8000
                     both sides connect out; nothing on either computer accepts connections from outside
```

### 3.1 Images: built on your computer, kept by the server

- `pool run` runs `docker build` on **your** computer, with your Docker and your network.
- It pushes the image to the **group's built-in registry** on the pool server and pins it by digest.
- Computers pull it with the short-lived, one-image tokens that private images already use.
- Nobody needs a Docker Hub or GHCR account. Images can still come from there: `--image ghcr.io/you/app:1`, with the token under My images.

Why not build on the member's computer instead: a Dockerfile's `RUN` lines
are arbitrary commands, and builds need the internet (`pip install`). Running
them on an owner's computer with network access is what pool's sandbox exists
to prevent.

The registry:
- **Software:** CNCF Distribution (Apache-2.0), the registry the private-image e2e test already runs.
- **Storage:** the server's existing file store (SeaweedFS).
- **Sign-in:** pool's server issues the registry's short-lived tokens, the same token-auth mode the e2e test uses.
- **Names:** repositories are `<group>/<user>/<app>`. A member can push only under their own name. Computers get a pull token only for the image of the app they run.
- **Clean-up:** images that no app or job has used for 30 days are deleted. The server admin sets the default (`POOL_REGISTRY_RETENTION_DAYS`), and a coordinator can change it for the group on the **Images** tab. Each group has a storage quota, set the same way.

### 3.2 An app is a job of a new shape

An app is a job with one long-running task (shape `app`, the plan's `service`
for one person). On top of a task, it has:

- **ports** to forward,
- a **time limit**: 8 hours by default, at most what the owner allows,
- a **placement** only on computers whose owner allows apps (§3.7),
- a **heartbeat from your `pool` command**: if your computer goes away, the app stops after a grace period (15 minutes, unless detached).

Its log, its `/out` files and the "what ran here" record work as for any task.
Usage counts toward fair share by the hour, like tasks.

### 3.3 Network on the member's computer

Batch tasks keep `--network none`. An app gets its own Docker network, created
with `--internal`, so there is no route out of it:

- **The agent reaches the app's port:** checked on 3 November 2026 with Docker 29; an `--internal` network's container answered the host on its port.
- **The app cannot reach the internet:** same check, a request out to the internet failed.
- **The app must not reach the owner's own services** (anything listening on the owner's computer or LAN). Not yet confirmed. Step 2 does not ship until a test proves it. If Docker's defaults do not block it, the agent adds a firewall rule for the app's network, or reaches the app through a relay inside its container network instead.
- **Isolation between stacks:** each app or stack has its own network, so apps never see each other.

### 3.4 The tunnel

- Your `pool` command listens on `localhost:PORT` and, for each connection, opens a WebSocket to the server: `/api/v1/apps/{id}/tunnel?port=8000`.
- The server accepts it **only from the person who started the app**.
- The agent keeps one WebSocket open for its own side, `/api/v1/agent/tunnel`, signed in with its device certificate and accepted only for its current attempt.
- The server joins the two and copies bytes both ways.

Both ends connect out, so it works behind home routers, NAT and firewalls.
Nothing new opens on anyone's computer. Traffic is encrypted (HTTPS) on both
legs. The server sees it, as it already sees job logs.

Limits: throughput is bounded by the server's own bandwidth, which is plenty
for using and testing an app, not for serving the public. Per person, the
number of open tunnels and the bytes moved are capped and recorded. v1 joins
tunnels inside one API process; several processes would need sticky routing.

### 3.5 Compose stacks

`pool up`:
1. runs `docker compose config --format json` on your computer, which reads every Compose feature (profiles, `.env`, overrides) into one normalized form,
2. builds the `build:` services locally and pushes them (§3.1),
3. sends the server an **app spec**: each service with its pinned image, command, environment, dependencies, health check, named volumes, and the ports to forward.

The agent creates the containers itself, with pool's hardening. It never runs
`docker compose` on the member's computer, so a Compose file cannot ask for
more than pool allows:

| Allowed | Refused, with a clear message at `pool up` |
|---|---|
| `image`, `build`, `command`, `entrypoint`, `working_dir` | `privileged`, `cap_add`, `devices`, `security_opt`, `sysctls` |
| `environment`, `env_file` (values are sent to the computer; not for real secrets, §3.8) | `network_mode`, `pid` or `ipc` set to `host`, extra networks |
| `depends_on` (also "healthy"), `healthcheck`, `restart` | Host folders mounted (`./data:/data`): put files in the image, or use data inputs later |
| Named volumes, `tmpfs` (inside the disk limit, deleted with the stack) | Ports published on the member's computer (`ports:` is forwarded to you instead) |
| `ports` (forwarded to your `localhost`), `deploy.resources.limits` | – |

**Strong isolation (decided 3 November 2026).** Apps and stacks run under
gVisor (T2). gVisor puts a kernel of its own, written in user space, between the
containers and the owner's computer, so a service may run as root inside its
container: PostgreSQL, Redis and other images that start as root and drop to
their own user work unchanged. Batch tasks keep T1 (a hardened container,
unprivileged user).

What it costs:
- **Fewer computers:** apps go only to computers with gVisor (`runsc`). The Linux installer adds it; inside WSL2 it is not yet known to work (plan spike S3).
- **Speed:** disk- and network-heavy services, such as databases, run slower.
- **GPUs:** an app that needs a GPU depends on gVisor's GPU support (plan spike S1).

The gVisor code path is built but untested here (no `runsc` on the test
machine). Step 3 starts with a spike on the test VM: PostgreSQL, Redis, a
FastAPI app and Playwright under `runsc`, measured against T1.

**Resources** are the sum of the services' limits, or the `--cpu`/`--memory`
given to `pool up`. The whole stack must fit on one computer.

### 3.6 Replicas (step 4)

`pool run --replicas 3` starts the same single-container app on three different
computers. Your local port spreads new connections over the copies. A copy that
goes away is replaced. This suits stateless APIs and inference servers. Each
copy has its own memory and files.

### 3.7 Owners stay in control

- **Opt-in:** apps run only where the owner allowed them, e.g. `pool-agent share ml-lab --allow-apps --app-hours 8` or a switch in the tray app. The default is off, because an app holds the computer for hours, unlike a task.
- **What ran here:** apps appear there like tasks, with the image, the person, the start and the end.
- **Owner's tools:** the kill switch, owner-return actions and limits apply as for tasks. When the owner needs the computer back, the app moves (§2.5).

### 3.8 Security (needs human review)

| Risk | Answer |
|---|---|
| The app reaches the owner's computer or network | Internal network per app; a test before step 2 ships (§3.3) |
| Someone else opens my app | Tunnel only for the person who started it; the device side only for the current attempt; tested |
| One app reaches another | One network per app or stack |
| Compose asks for host access | The agent creates containers itself; refused options (§3.5) |
| A container escape from a service running as root | gVisor (T2) for every app and stack (§3.5) |
| A malicious image | Pinned digests; registry namespaces per person; the usual sandbox |
| Secrets in `environment` | The computer's owner can read them (`docker inspect`). Warned at `pool up`. Real secret delivery is a separate plan item |
| Using the server as a free proxy | No outbound internet from apps; caps and records per person |
| Holding computers all day | Time limits, owner opt-in, fair share by the hour |

## 4. What gets built

| Part | New |
|---|---|
| `pool` command | `run` (with `docker run`'s flags and `--local`), `up`, `down`, `test`, `attach`, `logs`, `ps`; reading `pool.yaml`; local build and push; tunnel listener |
| Server | Built-in registry service and its token issuer; app shape and app spec checks; tunnel endpoints for user and agent; placement only on computers that allow apps |
| Agent | One internal network per app; several containers per app under gVisor; health checks; the `--desktop` sidecar; tunnel client; `--allow-apps` and `--app-hours` in the policy |
| Web | Running apps on the job page and the computer's page; Stop |
| Deploy | The registry in `compose.yaml` and the server installer; `runsc` in the agent's Linux installer |
| Docs | User guide section "Run your app", examples (a FastAPI app, a Node app, a web + PostgreSQL + Redis stack with Playwright tests, a desktop app), admin notes |

## 5. Delivery order and acceptance checks

| Step | Scope | Done when (measured) |
|---|---|---|
| 1. `pool run` for programs without ports | Local build, built-in registry, run, live output, Ctrl-C, `/out` back, `pool.yaml`; `docker run` flags (refused ones explained); `pool run --local`; `pool attach` for any job, also one started in the browser | A Dockerfile project's script runs on another computer with one command; a code-only change uploads only the changed layer; Ctrl-C stops the container within 10 s; a program that writes outside `/out` fails the same way with `--local` and on a member's computer; a job submitted in the browser can be attached from the terminal (tests) |
| 2. One app with ports | App shape and the `port:` field in job files, owner opt-in, internal network, tunnel, moving on owner return | A FastAPI app and a Node app on another computer answer at `localhost`; another user cannot open the tunnel; the app cannot reach the owner's services or the internet (tests) |
| 3. Compose stacks and testing | gVisor spike first; `pool up/down/test`, app spec, refused options | The spike's numbers are in the design; a web + PostgreSQL + Redis example stack runs under gVisor on one computer; `pool test` passes and fails with the tests service's exit code; every refused option has a test |
| 3b. Desktop apps | `--desktop` sidecar | A Tkinter and a Qt example app can be used in the browser at `localhost:6080` |
| 4. Replicas | Several copies, connection spreading, replacement | Three copies serve one local port; stopping one computer keeps the port answering (test) |

Each step ships with its guide section, an example that is run for real, and
end-to-end tests with the real agent.

## 6. Open questions

1. **gVisor on Windows (WSL2):** if it does not work there, Windows computers run batch tasks only.
2. **Registry quota:** the default storage per group (clean-up is decided: 30 days unused, adjustable).
3. **Opening apps in the browser:** needs a domain name with wildcard DNS; when one exists, `https://APP.pool.example.org` only for you.
4. **Data inputs** for large datasets and model weights: a separate design, since they matter for training as much as for apps.
5. **Sharing an app with group members**, later: the plan's services were meant to be reachable inside the group.

## 7. Not changing

The YAML jobs, parts and sweeps, results in one zip, private images and My
images all stay as they are. An app reuses them: the same scheduler, fair
share, owner limits, logs, `/out` and audit log.

## 8. Deviation from the plan

The plan (§7.10, Phase 3) reaches services through a WireGuard overlay
(Headscale), inside the group. This design instead relays through the pool
server, only for the person who started the app:

- **Nothing to install:** no WireGuard on members' computers.
- **Works anywhere:** behind any home router or firewall, without opening ports.
- **No new component** to run or secure.
- **The access rule is the decision itself:** one person, which is simple to check.

The overlay stays the plan for services shared with a group and for `gang`
jobs. It would be recorded as an ADR when step 2 is accepted.
